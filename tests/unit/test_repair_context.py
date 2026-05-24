"""repair_context — brief derivado desde ExecutionRun."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.missions.execution_lifecycle import (
    STEP_FAILED,
    ExecutionRun,
    ExecutionStatus,
    StepExecutionEntry,
)


def test_infer_repair_brief_from_failed_trace():
    from app.services.missions.semantic_execution_plan import (
        SemanticExecutionPlan,
        SemanticPlanStep,
    )

    c0 = MagicMock()
    c0.id = "a"
    c1 = MagicMock()
    c1.id = "b"
    mission = MagicMock()
    mission.compiled_execution_graph = [c0, c1]

    fake_plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(id="a", type="open_site"),
            SemanticPlanStep(id="b", type="click_element"),
        ],
    )
    run = ExecutionRun(mission_id="mid")
    run.status = ExecutionStatus.FAILED
    run.trace.entries.append(
        StepExecutionEntry(
            step_id="b",
            step_type="click_element",
            status=STEP_FAILED,
            error="not found",
        )
    )

    with patch(
        "app.services.missions.repair_context.build_semantic_execution_plan",
        return_value=fake_plan,
    ):
        from app.services.missions.repair_context import infer_repair_brief_from_run

        rb = infer_repair_brief_from_run(run, mission)

    assert rb is not None
    assert rb.step_semantic_id == "b"
    assert rb.semantic_plan_index == 1
    assert rb.compiled_step_index == 1
    assert rb.resume_semantic_start_index == 1
