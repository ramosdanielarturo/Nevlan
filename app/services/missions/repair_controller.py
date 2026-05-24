"""
Nevlan — RepairController (servicio)
===================================
Encapsula políticas de *repair* sin coords ciegas: la ejecución real
sigue viviendo en ``MissionReviewDialog`` + ``WeakStepRepairSession``;
este módulo es el ancla explícita para tests y futuras extensiones.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from app.services.missions.repair_context import (
    RepairBrief,
    infer_repair_brief_from_run,
)


@dataclass
class RepairPolicy:
    """Flags de seguridad para flujos de reparación."""

    allow_relative_coords: bool = False
    require_intent_layer: bool = True


class RepairController:
    """Referencia débil al :class:`RerecordController` para encadenar repairs."""

    infer_brief_from_run = staticmethod(infer_repair_brief_from_run)

    def __init__(
        self,
        *,
        rerecord: Optional[Any] = None,
        policy: Optional[RepairPolicy] = None,
    ) -> None:
        self.rerecord = rerecord
        self.policy = policy or RepairPolicy()

    def attach_rerecord(self, ctrl: Any) -> None:
        self.rerecord = ctrl


__all__ = ["RepairController", "RepairPolicy", "RepairBrief", "infer_repair_brief_from_run"]
