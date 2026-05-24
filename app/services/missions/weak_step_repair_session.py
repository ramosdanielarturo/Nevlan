"""Máquina de estados ligera para cola «Reparar débiles» — progreso N de M sin contadores mágicos."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class WeakRepairState(str, Enum):
    IDLE = "idle"
    SCANNING_WEAK_STEPS = "scanning_weak_steps"
    SHOWING_QUEUE = "showing_queue"
    REPAIRING_STEP = "repairing_step"
    WAITING_FOR_USER_ACTION = "waiting_for_user_action"
    VALIDATING_REPAIR = "validating_repair"
    REPAIR_SUCCESS = "repair_success"
    REPAIR_FAILED = "repair_failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class WeakStepRepairSession:
    """La cola concreta de índices la mantiene el diálogo; aquí sólo estado y contadores."""

    total_steps: int
    repaired: int = 0
    failed_attempts: int = 0
    current_state: WeakRepairState = WeakRepairState.SCANNING_WEAK_STEPS
    cancelled: bool = False
    hint: Optional[str] = None

    def begin_queue_after_dialog(self) -> None:
        if not self.cancelled:
            self.current_state = WeakRepairState.SHOWING_QUEUE

    def entering_pick(self, step_idx: int) -> None:
        """Antes de abrir una captura 1 clic para un paso."""
        _ = step_idx  # útil para depuración / UI futura
        self.current_state = WeakRepairState.WAITING_FOR_USER_ACTION

    def mark_success(self) -> None:
        self.repaired += 1
        self.current_state = WeakRepairState.REPAIR_SUCCESS

    def mark_failed(self, message: str = "") -> None:
        self.failed_attempts += 1
        self.hint = (message or None)[:500]
        self.current_state = WeakRepairState.REPAIR_FAILED

    def cancel(self) -> None:
        self.cancelled = True
        self.current_state = WeakRepairState.CANCELLED

    def mark_completed_queue(self) -> None:
        self.current_state = WeakRepairState.COMPLETED

    def progress_label_es(self) -> str:
        t = max(0, self.total_steps)
        if t <= 0:
            return "0 pasos en cola"
        return f"{self.repaired} de {t} pasos reparados"
