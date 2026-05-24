"""
Nevlan — RerecordController (servicio)
======================================
Fachada estable para regrabación premium: snapshots locales para undo,
delegación en ``mission_review_confirm.rerecord_step`` desde la UI.

La UI (``MissionReviewDialog``) posee una instancia y empuja snapshots
**antes** de mutar la misión en un guardado exitoso.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.contracts.mission import Mission


class RerecordController:
    """Snapshots pre-mutación para Ctrl+Z después de un guardado OK."""

    def __init__(self, *, max_undo: int = 12) -> None:
        self._snapshots: List[Dict[str, Any]] = []
        self._max_undo = max(1, int(max_undo))

    def offer_undo_anchor(self, pre_mutation_dump: Dict[str, Any]) -> None:
        """Registra estado **antes** de un guardado exitoso (Ctrl+Z lo restaura)."""
        self._snapshots.append(pre_mutation_dump)
        overflow = len(self._snapshots) - self._max_undo
        if overflow > 0:
            del self._snapshots[0:overflow]

    def rollback_latest_into(self, mission: Mission) -> bool:
        """Restaura el último snapshot dentro de ``mission`` (in-place)."""
        if not self._snapshots:
            return False
        blob = self._snapshots.pop()
        restored = Mission.model_validate(blob)
        for fname in Mission.model_fields:
            setattr(mission, fname, getattr(restored, fname))
        return True

    def clear_undo(self) -> None:
        self._snapshots.clear()


__all__ = ["RerecordController"]
