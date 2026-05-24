"""Inferencia de criticidad por paso (omitir paso / advertencias)."""
from __future__ import annotations

from app.contracts.mission import ActionStrategy, CompiledStep


def infer_step_criticality(step: CompiledStep) -> str:
    """Devuelve low | medium | high según acción y validación compilada."""
    try:
        v = getattr(step.validation_strategy, "value", str(step.validation_strategy))
    except Exception:
        v = str(getattr(step, "validation_strategy", "") or "")
    if v and str(v).lower() != "none":
        return "high"
    astrat = getattr(step, "action_strategy", None)
    try:
        a = getattr(astrat, "value", str(astrat or ""))
    except Exception:
        a = str(astrat or "")
    aval = str(a).lower()
    if "wait_for" in aval or astrat == ActionStrategy.WAIT_FOR_STATE:
        return "low"
    if astrat in (ActionStrategy.SCROLL,):
        return "medium"
    if astrat in (
        ActionStrategy.CLICK,
        ActionStrategy.DOUBLE_CLICK,
        ActionStrategy.RIGHT_CLICK,
    ):
        return "medium"
    if astrat in (
        ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        return "high"
    if astrat == ActionStrategy.AGENT_PROMPT:
        return "medium"
    return "medium"
