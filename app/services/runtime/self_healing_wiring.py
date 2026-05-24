"""Secuencia explícita de self-healing antes de escalar a reparación humana (telemetría)."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.config import get_settings
from app.core.logger import log
from app.services.runtime.reliability_self_healing import (
    SELF_HEALING_BEFORE_REPAIR_ORDER,
    SelfHealingStep,
)


def ordered_pre_repair_phase_names() -> Tuple[str, ...]:
    """Nombres canónicos antes de pedir repair / re-record de bloque."""

    return tuple(s.value for s in SELF_HEALING_BEFORE_REPAIR_ORDER)


def record_ordered_pre_repair_phases_before_human(
    mission: Any,
    *,
    step_id: Optional[str],
    hint: str = "needs_human",
) -> Dict[str, Any]:
    """Registra en telemetría la cadena ordenada — segura y reversible."""

    if mission is None:
        return {"skipped": True, "reason": "no_mission"}

    phases: Iterable[SelfHealingStep] = SELF_HEALING_BEFORE_REPAIR_ORDER
    trace: List[Dict[str, Any]] = []
    try:
        if not getattr(get_settings(), "RELIABILITY_HARDENING_ENABLED", False):
            return {"skipped": True, "reason": "RELIABILITY_HARDENING_ENABLED=false"}
    except Exception:
        return {"skipped": True, "reason": "settings_unavailable"}

    try:
        from app.services.runtime.runtime_production_telemetry import record_runtime_event

        for i, ph in enumerate(phases, start=1):
            record_runtime_event(
                mission,
                kind="self_healing_phase",
                payload={
                    "phase": ph.value,
                    "order": i,
                    "step_id": step_id,
                    "hint": hint,
                },
            )
            trace.append({"order": i, "phase": ph.value})
    except Exception as exc:
        log.debug("[self_healing_wiring] telemetría omitida: %s", exc)
        return {"ok": False, "error": str(exc), "trace": trace}
    return {"ok": True, "trace": trace}


__all__ = [
    "ordered_pre_repair_phase_names",
    "record_ordered_pre_repair_phases_before_human",
]
