"""Entity runtime budgets — anti-hang para ERL/UCES/LONE/OSUE/benchmark."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import Mission, MissionStatus, OperationalEntity, OperationalEntityType
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.missions.uol_runtime_telemetry import build_uol_runtime_acceptance_report
from app.services.runtime import entity_runtime_resolution as err
from app.services.runtime.live_operational_navigation_engine import run_operational_navigation_loop
from app.services.runtime.operational_state_understanding_engine import (
    OperationalStateSnapshot,
    OperationalStateType,
    run_osue_postcondition_wait,
)


def _entity_step() -> MissionStep:
    ent = OperationalEntity(
        entity_id="e1",
        entity_type=OperationalEntityType.TAB,
        display_name="Escribe aquí para buscar.",
        normalized_name="escribe_aqui_para_buscar",
        confidence=0.9,
    )
    return MissionStep(
        id="lineage:coi:1",
        kind="select_entity_from_collection",
        params={
            "operational_entity": ent.model_dump(mode="json"),
            "collection_entity": {"display_name": "Escribe aquí para buscar."},
            "operational_collection": {"collection_type": "chrome_profile_picker"},
        },
    )


def test_entity_runtime_budget_timeout_returns_failure_reason():
    budget = err.EntityRuntimeBudget(
        deadline_mono=time.monotonic() - 0.01,
        max_attempts=3,
    )
    hook = err.try_entity_first_runtime(
        _entity_step(),
        StateSnapshot(active_app="chrome.exe"),
        budget=budget,
    )
    assert hook.failure_reason == err.ENTITY_RUNTIME_TIMEOUT
    assert hook.timed_out


def test_collect_runtime_candidates_uia_walk_respects_budget():
    budget = err.EntityRuntimeBudget(
        deadline_mono=time.monotonic() + 0.05,
        max_attempts=3,
    )
    state = StateSnapshot(active_app="chrome.exe", uia_visible_names=["A", "B"])

    def slow_walk(*_a, **_k):
        for i in range(1000):
            yield i, MagicMock(Name=f"n{i}"), None
            time.sleep(0.002)

    with patch("uiautomation.GetForegroundControl") as gf:
        root = MagicMock()
        root.Exists.return_value = True
        gf.return_value = root
        with patch("uiautomation.WalkControl", side_effect=slow_walk):
            out = err.collect_runtime_candidates(state, deep=True, budget=budget)
    assert isinstance(out, list)


def test_lone_loop_respects_deadline_and_no_progress():
    ent = OperationalEntity(
        entity_id="e1",
        entity_type=OperationalEntityType.TAB,
        display_name="missing",
        normalized_name="missing",
    )
    budget = err.EntityRuntimeBudget(
        deadline_mono=time.monotonic() + 0.2,
        max_attempts=5,
    )
    calls = {"n": 0}

    def observe(ctx):
        calls["n"] += 1
        ctx["viewport_signature"] = "same"
        return [], []

    def execute(action, ctx):
        return ctx

    result = run_operational_navigation_loop(
        target_entity=ent,
        target_collection=None,
        observe_fn=observe,
        execute_fn=execute,
        max_depth=10,
        deadline_mono=budget.deadline_mono,
        progress_budget=budget,
    )
    assert result.stopped_reason in ("entity_runtime_timeout", "no_state_progress", "no_actions_planned")
    assert calls["n"] >= 1


def test_osue_wait_no_state_progress_exits():
    snap = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.LOADING_STATE,
        state_stability_score=0.5,
        ambiguity_score=0.1,
    )

    def factory():
        return snap

    res = run_osue_postcondition_wait(
        target_states=["results_available_state"],
        snapshot_factory=factory,
        timeout_ms=3000,
        poll_ms=20,
    )
    assert res.timed_out
    assert res.reason == err.NO_STATE_PROGRESS


def test_beta_runner_thread_timeout_does_not_hang():
    def slow():
        time.sleep(5)
        return "done"

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(slow)
        with pytest.raises(FuturesTimeout):
            fut.result(timeout=0.2)


def test_entity_failure_appears_as_weakest_uol_step():
    mission = Mission(id="m1", name="t", status=MissionStatus.EXECUTABLE)
    mission.uol_runtime_audit = [
        {
            "step_id": "lineage:coi:1",
            "uol_action": "select_entity",
            "handler_used": "select_entity_from_collection:uol_entity",
            "handler_success": False,
            "validation_passed": False,
            "failure_reason": err.ENTITY_RUNTIME_TIMEOUT,
            "latency_ms": 12000,
        },
    ]
    report = build_uol_runtime_acceptance_report(mission)
    assert report.weakest_uol_steps
    assert report.weakest_uol_steps[0]["failure_reason"] == err.ENTITY_RUNTIME_TIMEOUT


def test_smart_executor_step_timeout_helper():
    from app.services.missions.smart_executor import SmartMissionExecutor

    ex = SmartMissionExecutor(action_runner=MagicMock(), state_detector=MagicMock())
    step = MissionStep(id="s", kind="select_entity_from_collection")
    deadline = ex._step_deadline_mono(time.time(), step)
    assert deadline > time.monotonic()
