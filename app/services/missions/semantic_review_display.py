"""Resolución texto amigable Mission Review ↔ plan semántico (SIPE).

Sin dependencia de PyQt — importable desde tests y desde
``mission_review`` sin arrastrar widgets.

PRD 2026-05-15: en vista **normal** se priorizan las intenciones humanas
del ``semantic_execution_plan`` (human_label) cuando el SEP proviene de
``semantic_intent_promotion_engine``. En modo experto se conserva la
descripción editable del ``interpreted_steps`` como fuente primaria.

PRD 2026-05-17b — **EXECUTION_TRUTH_CONFIRMED** (:mod:`execution_truth_engine`):
la vista normal suprime ruido legacy cuando la evidencia PRE-acción + contrato
cierra ejecutabilidad real (Search→Chrome, etc.).
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def find_semantic_step_for_compiled(mission: Any, compiled_step_id: str) -> Optional[Dict[str, Any]]:
    """Localiza el step del SEP que corresponde a un CompiledStep.

    Reglas (PRD 2026-05-08d §A.4):
      1. Index del ``compiled_step_id`` en ``compiled_execution_graph``.
      2. ``semantic_execution_plan["steps"][index]`` si existe.
      3. Fallback por prefijo / id embebido entre steps semánticos.
    """
    if not mission or not compiled_step_id:
        return None
    graph = list(getattr(mission, "compiled_execution_graph", None) or [])
    plan = (getattr(mission, "semantic_execution_plan", None) or {})
    sem_steps = list(plan.get("steps") or [])
    if not graph or not sem_steps:
        return None
    idx = next(
        (k for k, cs in enumerate(graph)
         if str(getattr(cs, "id", "")) == str(compiled_step_id)),
        -1,
    )
    if 0 <= idx < len(sem_steps):
        return sem_steps[idx]
    for sp in sem_steps:
        sid = str(sp.get("id") or "")
        if sid and (sid in str(compiled_step_id) or str(compiled_step_id) in sid):
            return sp
    return None


def normal_mode_review_caption_for_compiled(
    mission: Any,
    compiled_step_id: str,
    interpreted_fallback: str,
    *,
    expert_mode: bool,
) -> str:
    """Texto de título de tarjeta Mission Review (fila principal).

    Vista normal + SEP SIPE con ``human_label`` → usamos el semantic.
    Otros casos → ``interpreted_fallback`` (descripción del recorder).
    Modo experto → siempre ``interpreted_fallback``.
    """
    if expert_mode:
        return interpreted_fallback
    plan = (getattr(mission, "semantic_execution_plan", None) or {}) or {}
    if (plan.get("source") or "").strip() != "semantic_intent_promotion_engine":
        return interpreted_fallback
    sem = find_semantic_step_for_compiled(mission, compiled_step_id)
    if not sem:
        return interpreted_fallback
    hl = str(sem.get("human_label") or "").strip()
    return hl if hl else interpreted_fallback


def is_semantic_intent_promotion_plan(mission: Any) -> bool:
    """SEP generado por SIPE (tipos genéricos, ``coords_used`` explícito)."""
    plan = (getattr(mission, "semantic_execution_plan", None) or {}) or {}
    return (plan.get("source") or "").strip() == "semantic_intent_promotion_engine"


def use_semantic_primary_normal_mission_review(mission: Any, *, expert_mode: bool) -> bool:
    """Vista normal debe listar pasos del SEP, no uno por evento compilado."""
    if expert_mode:
        return False
    if not is_semantic_intent_promotion_plan(mission):
        return False
    plan = (getattr(mission, "semantic_execution_plan", None) or {}) or {}
    return bool(plan.get("steps"))


def plan_declares_coords_used(mission: Any) -> Optional[bool]:
    """None si no hay SEP; True/False según ``coords_used`` del blob."""
    plan = (getattr(mission, "semantic_execution_plan", None) or {}) or {}
    if "coords_used" not in plan:
        return None
    return bool(plan.get("coords_used"))


def trusted_pre_action_suppresses_legacy_semantic_noise(mission: Any) -> bool:
    """Delegación en :mod:`execution_truth_engine` (EXECUTION_TRUTH)."""
    from app.services.missions.execution_truth_engine import (
        should_suppress_legacy_capture_noise,
    )

    return should_suppress_legacy_capture_noise(mission)


# Substrings que no deben aparecer en vista normal cuando SIPE manda.
NORMAL_VIEW_SIPE_FORBIDDEN_SUBSTRINGS: tuple = (
    "click_fallback",
    "use_coords",
    "capture_score",
    "fallback_coords",
    "GroupControl",
    "referencia visual",
    "debug_after_state",
    "uia_text_match",
    "target_source",
    "replay strategy",
    "replay_strategy",
    "ctrl+l",
    "smart_route",
    "collection_first",
    "wheel",
    "low confidence",
    "needs clarification",
    "select_profile",
    "ambiguity",
    "confidence %",
)

__all__ = [
    "NORMAL_VIEW_SIPE_FORBIDDEN_SUBSTRINGS",
    "find_semantic_step_for_compiled",
    "is_semantic_intent_promotion_plan",
    "normal_mode_review_caption_for_compiled",
    "plan_declares_coords_used",
    "trusted_pre_action_suppresses_legacy_semantic_noise",
    "use_semantic_primary_normal_mission_review",
]
