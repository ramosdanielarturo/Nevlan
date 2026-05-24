"""Políticas mínimas para re-record completo vs. sólo bloque (beta privada)."""

from __future__ import annotations

from typing import Any, Tuple


def mission_corruption_severe_for_full_rerecord(mission: Any) -> Tuple[bool, str]:
    """Heurística conservadora — sólo permite re-record completo ante corrupción fuerte."""

    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict) or not sep:
        return True, "missing_semantic_execution_plan_blob"
    steps = sep.get("steps")
    if not isinstance(steps, list) or not steps:
        return True, "empty_semantic_steps"
    st = str(sep.get("intent_status") or "").upper()
    if st == "CORRUPTED":
        return True, "intent_status_CORRUPTED"
    if sep.get("_corruption_fatal") or sep.get("integrity_failed"):
        return True, "integrity_flag_set"
    return False, ""


def should_block_full_rerecord(mission: Any, *, reliability_hardening: bool = False) -> Tuple[bool, str]:
    """Con hardening beta: preferir patch de bloque salvo corrupción severa."""

    if not reliability_hardening:
        return False, ""
    corrupted, why = mission_corruption_severe_for_full_rerecord(mission)
    return (not corrupted, "prefer_partial_rerecord_unless_" + why if why else "")


__all__ = [
    "mission_corruption_severe_for_full_rerecord",
    "should_block_full_rerecord",
]
