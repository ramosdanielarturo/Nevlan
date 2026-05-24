"""
ArthurOS Services - CheckpointManager (Smart Executor)
------------------------------------------------------
Guarda *checkpoints* por bloque cuando un paso clave termina exitoso.

Un checkpoint es la **prueba** de que llegamos a un estado verificable
(p.ej. ``chrome_profile_loaded``, ``youtube_loaded``,
``search_results_visible``). Si un paso posterior falla y la
recuperación tampoco logra arreglarlo, el ``RecoveryEngine`` puede
volver al último checkpoint válido en lugar de explotar la misión
entera.

Persistencia:
  * En memoria por ejecución (``self._mem``) — fast path para el
    Executor.
  * En disco (``var/missions/checkpoints/<run_id>.json``) — diagnóstico
    post-mortem y debug. NUNCA se lee como fuente de verdad para el
    Executor en runtime; el Executor solo confía en su memoria.

Diseño:
  * Un ``Checkpoint`` solo guarda lo que es **estable**: app, ventana,
    URL, pistas de texto visible, ids de step/bloque, timestamp,
    ``recovery_hint``. NO guardamos coords absolutas (cambian con
    monitor) ni screenshots completos.
  * ``recovery_hint`` es un string libre que la receta de recuperación
    puede usar como pista textual ("perfil_seleccionado", "url=youtube").
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.logger import log
from app.core.paths import VAR_DIR
from app.services.missions.state_detector import StateSnapshot


_CHECKPOINTS_DIR = VAR_DIR / "missions" / "checkpoints"


# ──────────────────────────────────────────────────────────────────────
# Datos
# ──────────────────────────────────────────────────────────────────────

@dataclass
class Checkpoint:
    key: str                            # "chrome_profile_loaded"
    run_id: str
    step_id: str
    block_id: Optional[str] = None
    app: Optional[str] = None
    window_title: Optional[str] = None
    url: Optional[str] = None
    visible_text_keys: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    recovery_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_state(
        cls,
        key: str,
        run_id: str,
        step_id: str,
        state: StateSnapshot,
        block_id: Optional[str] = None,
        recovery_hint: str = "",
    ) -> "Checkpoint":
        # Pequeña curaduría de pistas textuales: nombres UIA visibles
        # cortos, sin duplicados.
        keys: List[str] = []
        for nm in (state.uia_visible_names or [])[:20]:
            nm = (nm or "").strip()
            if 2 <= len(nm) <= 40 and nm not in keys:
                keys.append(nm)
        return cls(
            key=key,
            run_id=run_id,
            step_id=step_id,
            block_id=block_id,
            app=state.active_app,
            window_title=state.active_window_title,
            url=state.browser_url,
            visible_text_keys=keys,
            recovery_hint=recovery_hint,
        )


# ──────────────────────────────────────────────────────────────────────
# Manager
# ──────────────────────────────────────────────────────────────────────

class CheckpointManager:
    """Memoria por-ejecución + persistencia perezosa en disco."""

    def __init__(self, run_id: Optional[str] = None) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        self._mem: Dict[str, Checkpoint] = {}
        # Mantenemos historia para análisis: cada save añade una entrada
        # cronológica; ``last(key)`` siempre devuelve la más reciente.
        self._history: List[Checkpoint] = []
        self._lock = threading.RLock()
        try:
            _CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ── API pública ──────────────────────────────────────────────
    # Alias amigables de checkpoints: el `execution_contracts` guarda
    # ``<dominio>_loaded`` (e.g. ``youtube.com_loaded``), pero el
    # RecoveryEngine y los humanos hablan de ``youtube_loaded``. Dejar
    # ambos evita un "¿por qué no encuentra el checkpoint?" silencioso.
    _ALIASES: Dict[str, str] = {
        "youtube.com_loaded": "youtube_loaded",
        "www.youtube.com_loaded": "youtube_loaded",
        "google.com_loaded": "google_loaded",
        "github.com_loaded": "github_loaded",
    }

    def save(self, cp: Checkpoint) -> None:
        with self._lock:
            self._mem[cp.key] = cp
            alias = self._ALIASES.get(cp.key)
            if alias and alias not in self._mem:
                self._mem[alias] = cp
            self._history.append(cp)
        log.info(f"✅ checkpoint '{cp.key}' guardado (step={cp.step_id})")
        self._persist()

    def save_from_state(
        self,
        key: str,
        step_id: str,
        state: StateSnapshot,
        block_id: Optional[str] = None,
        recovery_hint: str = "",
    ) -> Checkpoint:
        cp = Checkpoint.from_state(
            key=key,
            run_id=self.run_id,
            step_id=step_id,
            state=state,
            block_id=block_id,
            recovery_hint=recovery_hint,
        )
        self.save(cp)
        return cp

    def get(self, key: str) -> Optional[Checkpoint]:
        with self._lock:
            return self._mem.get(key)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._mem

    def last_for_block(self, block_id: str) -> Optional[Checkpoint]:
        """Último checkpoint guardado para ese bloque (si existe)."""
        with self._lock:
            for cp in reversed(self._history):
                if cp.block_id == block_id:
                    return cp
        return None

    def latest(self) -> Optional[Checkpoint]:
        with self._lock:
            return self._history[-1] if self._history else None

    def all_keys(self) -> List[str]:
        with self._lock:
            return list(self._mem.keys())

    def clear(self) -> None:
        with self._lock:
            self._mem.clear()
            self._history.clear()
        # Borramos el archivo persistido también para no dejar basura
        # entre runs si alguien reusa el run_id.
        path = self._path()
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

    # ── Persistencia ─────────────────────────────────────────────
    def _path(self) -> Path:
        return _CHECKPOINTS_DIR / f"{self.run_id}.json"

    def _persist(self) -> None:
        """Escribe la historia entera en JSON. Atómico vía rename."""
        try:
            path = self._path()
            tmp = path.with_suffix(".json.tmp")
            data = {
                "run_id": self.run_id,
                "saved_at": time.time(),
                "checkpoints": [cp.to_dict() for cp in self._history],
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(path)
        except Exception as e:
            log.debug(f"checkpoint persist falló (ignorado): {e}")


__all__ = [
    "Checkpoint",
    "CheckpointManager",
]
