"""Helpers para misiones FASE 7 benchmark (SEP programático + truth anclada)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.contracts.mission import EventType, Mission, MissionStatus, MouseAction, RawEvent, WindowContext
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_intent_status_to_plan,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    apply_fase0_entity_first_strategies,
    inject_search_to_chrome_execution_truth,
)


def _anchor_execution_truth(mission: Mission) -> None:
    """Evidencia mínima para EXECUTION_TRUTH + gate en fixtures FASE 7."""
    ev = RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=100, y=100),
        window_context=WindowContext(hwnd=1, title="fixture", process_name="fixture.exe"),
        metadata={},
    )
    inject_search_to_chrome_execution_truth([ev], index=0)
    mission.raw_trace = [ev]


def _plan_step(
    mission_id: str,
    idx: int,
    step_type: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    preferred_strategy: str,
    human_label: str,
    confidence: float = 0.92,
) -> SemanticPlanStep:
    return SemanticPlanStep(
        id=f"{mission_id}:p{idx:02d}:{step_type}",
        type=step_type,
        params=dict(params or {}),
        preferred_strategy=preferred_strategy,
        fallback_strategies=[],
        confidence=confidence,
        human_label=human_label,
        needs_user_label=False,
        label_prompt="",
        block_id=None,
    )


def build_fase7_semantic_mission(
    *,
    name: str,
    scenario_id: str,
    category: str,
    steps: List[SemanticPlanStep],
    description: str = "",
) -> Mission:
    """Misión EXECUTABLE con SEP + truth anclada para dry-run/live benchmark."""
    mission = Mission(name=name, status=MissionStatus.EXECUTABLE)
    _anchor_execution_truth(mission)
    mission_id = str(getattr(mission, "id", "") or "fase7")

    plan_steps: List[SemanticPlanStep] = []
    for i, step in enumerate(steps):
        if not step.id:
            step.id = f"{mission_id}:p{i:02d}:{step.type}"
        plan_steps.append(step)

    plan = SemanticExecutionPlan(
        steps=plan_steps,
        source="fase7_benchmark_fixture",
        version=2,
        average_confidence=0.9,
        coords_used=False,
        legacy_graph_ignored=True,
    )
    attach_intent_status_to_plan(plan, mission=mission)
    mission.semantic_execution_plan = plan.to_dict()
    mission.legacy_compiled_graph_purpose = "legacy_debug_only"
    apply_fase0_entity_first_strategies(mission)

    sep_blob = dict(mission.semantic_execution_plan or {})
    sep_blob["source"] = "semantic_intent_promotion_engine"
    if sep_blob.get("intent_status") != "READY":
        sep_blob["intent_status"] = "READY"
        sep_blob["not_ready_reasons"] = []
        sep_blob["ready_provenance"] = "fase7_benchmark_fixture"
        mission.semantic_execution_plan = sep_blob

    tags = list(mission.tags or [])
    tags.extend([f"fase7:{scenario_id}", f"fase7_cat:{category}"])
    mission.tags = tags
    if description:
        mission.description = description
    return mission
