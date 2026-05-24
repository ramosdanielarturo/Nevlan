"""Micro-validación post-regrabación."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.execution_contracts import MissionStep
from app.services.missions.smart_executor import MissionResult, StepOutcome


@pytest.fixture()
def dummy_plan_steps():
    from app.services.missions.semantic_execution_plan import SemanticPlanStep

    return [
        SemanticPlanStep(
            id="a",
            type="open_site",
            params={},
            preferred_strategy="stub",
        ),
        SemanticPlanStep(
            id="b",
            type="click_element",
            params={},
            preferred_strategy="stub",
        ),
    ]


def test_continue_index_increments_on_success(dummy_plan_steps):
    from app.services.missions.semantic_execution_plan import SemanticExecutionPlan

    plan = SemanticExecutionPlan(steps=dummy_plan_steps)
    stub_steps = [
        MissionStep(id="a", kind="open_site"),
        MissionStep(id="b", kind="click_element"),
    ]

    mr = MissionResult(
        run_id="t",
        status="success",
        outcomes=[
            StepOutcome(
                step_id="b",
                kind="click_element",
                status="success",
                strategy_used="uia:click",
                human_message="ok",
            ),
        ],
    )

    mock_exec = MagicMock()
    mock_exec.run_mission.return_value = mr
    mock_cls = MagicMock(return_value=mock_exec)

    with (
        patch(
            "app.services.missions.repair_step_micro_validate.build_semantic_execution_plan",
            return_value=plan,
        ),
        patch(
            "app.services.missions.repair_step_micro_validate.semantic_plan_to_mission_steps",
            return_value=stub_steps,
        ),
        patch(
            "app.services.missions.repair_step_micro_validate.SmartMissionExecutor",
            new=mock_cls,
        ),
    ):
        from app.services.missions.repair_step_micro_validate import (
            micro_validate_repaired_semantic_step,
        )

        r = micro_validate_repaired_semantic_step(MagicMock(), semantic_step_index=1)

    assert r.ok is True
    assert r.continue_semantic_index == 2
    assert r.total_semantic_steps == 2


def test_blind_coords_success_fails_validation(dummy_plan_steps):
    from app.services.missions.semantic_execution_plan import SemanticExecutionPlan

    plan = SemanticExecutionPlan(steps=dummy_plan_steps)
    stub_steps = [MissionStep(id="a", kind="open_site")]

    mr = MissionResult(
        run_id="t",
        status="success",
        outcomes=[
            StepOutcome(
                step_id="a",
                kind="open_site",
                status="success",
                strategy_used="click_button:relative_coords_fallback",
                human_message="x",
            ),
        ],
    )

    mock_exec = MagicMock()
    mock_exec.run_mission.return_value = mr
    mock_cls = MagicMock(return_value=mock_exec)

    with (
        patch(
            "app.services.missions.repair_step_micro_validate.build_semantic_execution_plan",
            return_value=plan,
        ),
        patch(
            "app.services.missions.repair_step_micro_validate.semantic_plan_to_mission_steps",
            return_value=stub_steps,
        ),
        patch(
            "app.services.missions.repair_step_micro_validate.SmartMissionExecutor",
            new=mock_cls,
        ),
    ):
        from app.services.missions.repair_step_micro_validate import (
            micro_validate_repaired_semantic_step,
        )

        r = micro_validate_repaired_semantic_step(MagicMock(), 0)

    assert r.ok is False
    assert (
        "emergencia" in r.user_summary.lower()
        or "coords" in r.user_summary.lower()
    )
