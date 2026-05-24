"""
ArthurOS Services - LearningLogger (Smart Executor)
---------------------------------------------------
Memoria de qué estrategias funcionan y cuáles fallan.

Funciones:
  * Log JSONL append-only de cada intento (``var/missions/learning/<kind>.jsonl``).
  * Stats agregadas en memoria + cache JSON (``stats.json``):
        kind → strategy → {ok, fail, last_ok_at, last_fail_at, score}
  * Re-ranker: dada una lista ``[preferred] + fallbacks`` y un ``kind``,
    devuelve la lista REORDENADA poniendo primero las estrategias con
    mejor *score* histórico, manteniendo las nuevas (sin historia) en
    su orden original.

Reglas (PRD):
  - Si una estrategia falla repetidamente, **bajarla** de prioridad.
  - Si una estrategia tiene éxito, **subirla**.
  - El ranking nunca elimina estrategias: siempre intentamos todas si
    hace falta; sólo cambia el orden.

Score: ``ok / (ok + fail)`` con suavizado bayesiano (1 ok + 1 fail
ficticios) para que una estrategia con 1/0 no se "fije" al tope para
siempre.

Diseño:
  * Append-only JSONL: trivial de auditar y de migrar.
  * Stats reconstruibles: si borras ``stats.json`` se rehidratan
    leyendo el JSONL la próxima vez.
  * Pure-data: no tiene dependencias de Qt/Playwright/UIA — se puede
    importar en tests headless sin coste.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.logger import log
from app.core.paths import VAR_DIR


_LEARN_DIR = VAR_DIR / "missions" / "learning"
_STATS_FILE = _LEARN_DIR / "stats.json"


# ──────────────────────────────────────────────────────────────────────
# Estructuras
# ──────────────────────────────────────────────────────────────────────

@dataclass
class LearningRecord:
    """Una observación de ejecución para alimentar el aprendizaje."""

    record_id: str
    timestamp: float
    run_id: str
    kind: str                     # step kind: "open_url", ...
    step_id: str
    strategy_used: str
    success: bool
    retries: int = 0
    duration_ms: int = 0
    correction_applied: Optional[str] = None  # nombre de receta de recovery
    state_before: Dict[str, Any] = None       # type: ignore[assignment]
    state_after: Dict[str, Any] = None        # type: ignore[assignment]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Compactamos: quitamos campos None grandes para mantener
        # JSONL legible.
        if d.get("state_before") is None:
            d.pop("state_before", None)
        if d.get("state_after") is None:
            d.pop("state_after", None)
        return d


# ──────────────────────────────────────────────────────────────────────
# LearningLogger
# ──────────────────────────────────────────────────────────────────────

class LearningLogger:
    """Singleton-friendly: una instancia por proceso es suficiente.

    El re-ranker es **pure**: no muta el orden recibido, devuelve uno nuevo.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # stats[kind][strategy] = {"ok": int, "fail": int,
        #                          "last_ok_at": float, "last_fail_at": float}
        self._stats: Dict[str, Dict[str, Dict[str, Any]]] = {}
        try:
            _LEARN_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._load_stats()

    # ── API pública: registro ────────────────────────────────────
    def record_success(
        self,
        run_id: str,
        kind: str,
        step_id: str,
        strategy: str,
        retries: int = 0,
        duration_ms: int = 0,
        state_before: Optional[Dict[str, Any]] = None,
        state_after: Optional[Dict[str, Any]] = None,
        correction_applied: Optional[str] = None,
    ) -> None:
        rec = LearningRecord(
            record_id=str(uuid.uuid4()),
            timestamp=time.time(),
            run_id=run_id,
            kind=kind,
            step_id=step_id,
            strategy_used=strategy,
            success=True,
            retries=retries,
            duration_ms=duration_ms,
            correction_applied=correction_applied,
            state_before=state_before,  # type: ignore[arg-type]
            state_after=state_after,    # type: ignore[arg-type]
        )
        self._append(rec)
        self._bump(kind, strategy, ok=True)

    def record_failure(
        self,
        run_id: str,
        kind: str,
        step_id: str,
        strategy: str,
        error: str,
        retries: int = 0,
        duration_ms: int = 0,
        state_before: Optional[Dict[str, Any]] = None,
        state_after: Optional[Dict[str, Any]] = None,
    ) -> None:
        rec = LearningRecord(
            record_id=str(uuid.uuid4()),
            timestamp=time.time(),
            run_id=run_id,
            kind=kind,
            step_id=step_id,
            strategy_used=strategy,
            success=False,
            retries=retries,
            duration_ms=duration_ms,
            error=str(error)[:300],
            state_before=state_before,  # type: ignore[arg-type]
            state_after=state_after,    # type: ignore[arg-type]
        )
        self._append(rec)
        self._bump(kind, strategy, ok=False)

    # ── API pública: ranking ─────────────────────────────────────
    def score(self, kind: str, strategy: str) -> float:
        """Score 0..1 con suavizado bayesiano (Beta(1,1))."""
        with self._lock:
            s = self._stats.get(kind, {}).get(strategy)
        if not s:
            return 0.5  # neutro: sin historia, no penalizamos.
        ok = int(s.get("ok", 0))
        fail = int(s.get("fail", 0))
        return (ok + 1) / (ok + fail + 2)

    def stats_for(self, kind: str) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._stats.get(kind, {}))

    def rank_strategies(
        self,
        kind: str,
        strategies: List[str],
        *,
        skip: Optional[List[str]] = None,
    ) -> List[str]:
        """Reordena ``strategies`` según score histórico.

        Reglas:
          - Estrategias en ``skip`` (estrategias ya fallidas en este
            mismo step) van al **final**, en su orden original. Esto
            implementa la regla "no repetir la misma estrategia fallida
            más de 2 veces" cuando el caller acumula fallos.
          - Para el resto, ordenamos por (score, -index_original) de
            forma estable: si hay empate, gana la que estaba antes.
        """
        skip_set = set(skip or [])
        ranked: List[tuple] = []
        skipped: List[str] = []
        for idx, s in enumerate(strategies):
            if s in skip_set:
                skipped.append(s)
                continue
            sc = self.score(kind, s)
            ranked.append((-sc, idx, s))
        ranked.sort()
        out = [s for _, _, s in ranked] + skipped
        return out

    # ── Internos ─────────────────────────────────────────────────
    def _append(self, rec: LearningRecord) -> None:
        path = _LEARN_DIR / f"{rec.kind}.jsonl"
        try:
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            log.debug(f"learning append falló (ignorado): {e}")

    def _bump(self, kind: str, strategy: str, ok: bool) -> None:
        with self._lock:
            kbucket = self._stats.setdefault(kind, {})
            sbucket = kbucket.setdefault(strategy, {"ok": 0, "fail": 0})
            if ok:
                sbucket["ok"] = int(sbucket.get("ok", 0)) + 1
                sbucket["last_ok_at"] = time.time()
            else:
                sbucket["fail"] = int(sbucket.get("fail", 0)) + 1
                sbucket["last_fail_at"] = time.time()
        self._save_stats()

    def _load_stats(self) -> None:
        try:
            if _STATS_FILE.exists():
                with open(_STATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                if isinstance(data, dict):
                    with self._lock:
                        self._stats = data
        except Exception as e:
            log.debug(f"learning load stats falló (ignorado): {e}")

    def _save_stats(self) -> None:
        try:
            with self._lock:
                snapshot = json.dumps(self._stats, indent=2, ensure_ascii=False)
            tmp = _STATS_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(snapshot)
            tmp.replace(_STATS_FILE)
        except Exception as e:
            log.debug(f"learning save stats falló (ignorado): {e}")


# ──────────────────────────────────────────────────────────────────────
# Singleton conveniente
# ──────────────────────────────────────────────────────────────────────

_default_logger: Optional[LearningLogger] = None
_default_lock = threading.Lock()


def get_learning_logger() -> LearningLogger:
    """Devuelve un ``LearningLogger`` compartido por proceso."""
    global _default_logger
    if _default_logger is None:
        with _default_lock:
            if _default_logger is None:
                _default_logger = LearningLogger()
    return _default_logger


__all__ = [
    "LearningRecord",
    "LearningLogger",
    "get_learning_logger",
]
