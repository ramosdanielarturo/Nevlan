"""
ArthurOS Services - TargetMemory (Auto-Learning Executor)
---------------------------------------------------------
Recuerda CÓMO encontrar un target con éxito.

Idea: antes de resolver un target, consultamos esta memoria. Si tenemos
una resolución que funcionó N veces (selectores DOM, identificadores
UIA, anclas OCR, hash visual, estrategia ganadora), la **probamos
primero**. Si valida, ahorramos tiempo y reducimos preguntas al usuario.

Estructura por entrada:

    {
      "target": "youtube_search_box",
      "domain": "youtube.com",
      "app": "chrome.exe",
      "successful_selectors": [
        "input[name='search_query']",
        "#search-input input"
      ],
      "successful_uia": {
        "name": "Buscar", "control_type": "Edit"
      },
      "ocr_anchor": "Buscar",
      "visual_asset_hash": "abc123...",
      "last_successful_strategy": "dom_input",
      "success_count": 12,
      "failure_count": 1
    }

API principal:
  - lookup(target, domain, app) -> TargetMemoryEntry | None
  - record_success(target, ..., strategy=..., selectors=..., uia=..., ...)
  - record_failure(target, ..., strategy=...)
  - prefer_strategies(target, base_strategies) -> reordenado
  - lookup_with_fallback(target_signature_dict) -> entry compatible

Persistencia: delegada a `ExecutionMemoryStore.target_resolutions`.
NO duplicamos la "confidence" intra-mission del CompiledStep — esa sigue
viviendo en el modelo de misión. Esto es la **memoria global** entre
ejecuciones, agnóstica a una misión específica.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.core.logger import log
from app.services.missions.execution_memory_store import (
    ExecutionMemoryStore,
    get_execution_memory_store,
)


# ──────────────────────────────────────────────────────────────────────
# DTO
# ──────────────────────────────────────────────────────────────────────

@dataclass
class TargetMemoryEntry:
    semantic_target: str
    domain: str = ""
    app: str = ""
    successful_selectors: List[str] = field(default_factory=list)
    successful_uia: Dict[str, Any] = field(default_factory=dict)
    ocr_anchor: Optional[str] = None
    visual_asset_hash: Optional[str] = None
    last_successful_strategy: Optional[str] = None
    success_count: int = 0
    failure_count: int = 0
    last_success_at: Optional[float] = None
    last_failure_at: Optional[float] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "TargetMemoryEntry":
        sel = row.get("successful_selectors") or []
        if isinstance(sel, str):
            try:
                import json as _json
                sel = _json.loads(sel)
            except Exception:
                sel = []
        uia = row.get("successful_uia") or {}
        if isinstance(uia, str):
            try:
                import json as _json
                uia = _json.loads(uia)
            except Exception:
                uia = {}
        return cls(
            semantic_target=str(row.get("semantic_target") or ""),
            domain=str(row.get("domain") or ""),
            app=str(row.get("app") or ""),
            successful_selectors=list(sel) if isinstance(sel, list) else [],
            successful_uia=dict(uia) if isinstance(uia, dict) else {},
            ocr_anchor=row.get("ocr_anchor"),
            visual_asset_hash=row.get("visual_asset_hash"),
            last_successful_strategy=row.get("last_successful_strategy"),
            success_count=int(row.get("success_count") or 0),
            failure_count=int(row.get("failure_count") or 0),
            last_success_at=row.get("last_success_at"),
            last_failure_at=row.get("last_failure_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "semantic_target": self.semantic_target,
            "domain": self.domain,
            "app": self.app,
            "successful_selectors": list(self.successful_selectors),
            "successful_uia": dict(self.successful_uia),
            "ocr_anchor": self.ocr_anchor,
            "visual_asset_hash": self.visual_asset_hash,
            "last_successful_strategy": self.last_successful_strategy,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
        }

    @property
    def reliability(self) -> float:
        """Reliability 0..1 con suavizado bayesiano."""
        ok = self.success_count
        fail = self.failure_count
        return (ok + 1) / (ok + fail + 2)


# ──────────────────────────────────────────────────────────────────────
# TargetMemory
# ──────────────────────────────────────────────────────────────────────

class TargetMemory:
    """Recordatorio de cómo encontrar targets con éxito."""

    def __init__(self, store: Optional[ExecutionMemoryStore] = None) -> None:
        self.store = store or get_execution_memory_store()

    # ── Lookup ─────────────────────────────────────────────────
    def lookup(
        self, semantic_target: str,
        *, domain: Optional[str] = None, app: Optional[str] = None,
    ) -> Optional[TargetMemoryEntry]:
        if not semantic_target:
            return None
        # Probamos exact match → solo domain → solo app → global.
        candidates = (
            (domain, app), (domain, None), (None, app), (None, None),
        )
        for d, a in candidates:
            row = self.store.get_target_resolution(
                semantic_target=semantic_target, domain=d, app=a,
            )
            if row:
                return TargetMemoryEntry.from_row(row)
        return None

    def lookup_with_fallback(
        self, target_signature: Optional[Dict[str, Any]],
    ) -> Optional[TargetMemoryEntry]:
        """Si no hay `semantic_target` directo pero tenemos un dict
        ``target_signature`` (como el de ``target_signature.build_target_signature``),
        intentamos extraer las claves estándar y buscar.
        """
        if not target_signature:
            return None
        target = (
            target_signature.get("semantic_target")
            or target_signature.get("target")
            or target_signature.get("label")
            or ""
        )
        domain = (
            target_signature.get("domain")
            or target_signature.get("browser_domain")
            or ""
        )
        app = (
            target_signature.get("app")
            or target_signature.get("active_app")
            or ""
        )
        if not target:
            return None
        return self.lookup(str(target), domain=str(domain), app=str(app))

    # ── Mutations ──────────────────────────────────────────────
    def record_success(
        self,
        semantic_target: str,
        *,
        strategy: Optional[str] = None,
        selectors: Optional[Iterable[str]] = None,
        uia: Optional[Dict[str, Any]] = None,
        ocr_anchor: Optional[str] = None,
        visual_asset_hash: Optional[str] = None,
        domain: Optional[str] = None,
        app: Optional[str] = None,
    ) -> None:
        if not semantic_target:
            return
        try:
            self.store.upsert_target_resolution(
                semantic_target=semantic_target,
                domain=domain, app=app,
                successful_selectors=list(selectors or []),
                successful_uia=dict(uia or {}),
                ocr_anchor=ocr_anchor,
                visual_asset_hash=visual_asset_hash,
                last_successful_strategy=strategy,
                success=True,
            )
        except Exception as e:
            log.debug(f"TargetMemory.record_success falló: {e}")

    def record_failure(
        self,
        semantic_target: str,
        *,
        strategy: Optional[str] = None,
        domain: Optional[str] = None,
        app: Optional[str] = None,
    ) -> None:
        if not semantic_target:
            return
        try:
            self.store.upsert_target_resolution(
                semantic_target=semantic_target,
                domain=domain, app=app,
                last_successful_strategy=None,
                success=False,
            )
        except Exception as e:
            log.debug(f"TargetMemory.record_failure falló: {e}")

    # ── Strategy preference ───────────────────────────────────
    def prefer_strategies(
        self,
        semantic_target: str,
        base_strategies: Sequence[str],
        *,
        domain: Optional[str] = None,
        app: Optional[str] = None,
    ) -> List[str]:
        """Si la memoria del target tiene una estrategia ganadora, la
        movemos al frente (manteniendo el resto en su orden)."""
        ordered = list(base_strategies)
        entry = self.lookup(semantic_target, domain=domain, app=app)
        if entry is None or not entry.last_successful_strategy:
            return ordered
        winner = entry.last_successful_strategy
        if winner in ordered:
            ordered.remove(winner)
            return [winner] + ordered
        return ordered

    # ── Inspection ─────────────────────────────────────────────
    def selectors_for(
        self, semantic_target: str,
        *, domain: Optional[str] = None, app: Optional[str] = None,
    ) -> List[str]:
        entry = self.lookup(semantic_target, domain=domain, app=app)
        return list(entry.successful_selectors) if entry else []

    def uia_blob_for(
        self, semantic_target: str,
        *, domain: Optional[str] = None, app: Optional[str] = None,
    ) -> Dict[str, Any]:
        entry = self.lookup(semantic_target, domain=domain, app=app)
        return dict(entry.successful_uia) if entry else {}


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────

_target_memory: Optional[TargetMemory] = None


def get_target_memory() -> TargetMemory:
    global _target_memory
    if _target_memory is None:
        _target_memory = TargetMemory()
    return _target_memory


def reset_target_memory_for_tests() -> None:
    global _target_memory
    _target_memory = None


__all__ = [
    "TargetMemory",
    "TargetMemoryEntry",
    "get_target_memory",
    "reset_target_memory_for_tests",
]
