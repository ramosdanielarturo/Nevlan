"""Integración OSUE ↔ SmartExecutor runtime."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import (
    Mission,
    MissionStatus,
    OperationalStateSnapshot,
    OperationalStateType,
)
from app.services.missions.execution_contracts import MissionStep, StepContract
from app.services.missions.four_layer_operational_model import build_four_layer_model
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_semantic_plan_to_mission,
)
from app.services.missions.smart_executor import SmartMissionExecutor, _NeedsHuman
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime.operational_runtime_consistency_engine import reconcile_mission
from app.services.runtime.operational_state_understanding_engine import OsueWaitResult


@pytest.fixture
def _osue_runtime_on(monkeypatch):
    monkeypatch.setattr(
        "app.services.runtime.operational_state_understanding_engine.osue_enabled",
        lambda *_a, **_k: True,
    )


@pytest.fixture
def _osue_runtime_off(monkeypatch):
    monkeypatch.setattr(
        "app.services.runtime.operational_state_understanding_engine.osue_enabled",
        lambda *_a, **_k: False,
    )


def _mission_search() -> Mission:
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="s-search",
                type="search_content",
                params={"query": "demo"},
                preferred_strategy="search_content:uol_inject",
                confidence=0.9,
                human_label="Buscar",
            ),
        ],
        source="test",
        intent_status="READY",
    )
    m = Mission(id=str(uuid.uuid4()), name="osue-exec", status=MissionStatus.EXECUTABLE)
    attach_semantic_plan_to_mission(m, plan=plan, force=True)
    build_four_layer_model(m)
    return m


def _executor(mission: Mission) -> SmartMissionExecutor:
    det = MagicMock()
    det.detect.return_value = StateSnapshot(
        uia_visible_names=["Result A", "Result B", "Result C", "Result D"],
        browser_url="https://provider.example/search?q=demo",
    )
    return SmartMissionExecutor(mission_ref=mission, state_detector=det)


def _always_true_contract(step: MissionStep | None = None) -> StepContract:
    ms = step or MissionStep(id="s-search", kind="search_content", params={})
    return StepContract(
        step=ms,
        precondition=lambda s: True,
        postcondition=lambda s: True,
        preferred_strategy="search_content:generic",
        postcondition_timeout_ms=3000,
    )


def test_postcondition_uses_osue_wait_when_enabled(_osue_runtime_on):
    mission = _mission_search()
    exe = _executor(mission)
    contract = _always_true_contract()
    wait_calls: list = []

    def fake_run_osue_wait(**kwargs):
        wait_calls.append(kwargs)
        return OsueWaitResult(
            matched=True,
            reason="OSUE_STATE_REACHED",
            expected_state=OperationalStateType.RESULTS_AVAILABLE_STATE.value,
            actual_state=OperationalStateType.RESULTS_AVAILABLE_STATE.value,
            target_states=list(kwargs.get("target_states") or []),
        )

    with patch(
        "app.services.missions.smart_executor.SmartMissionExecutor._await_postcondition_osue",
        wraps=exe._await_postcondition_osue,
    ):
        with patch(
            "app.services.runtime.operational_state_understanding_engine.run_osue_postcondition_wait",
            side_effect=fake_run_osue_wait,
        ):
            snap = exe._await_postcondition(contract, 3000, step=MissionStep(id="s-search", kind="search_youtube", params={}))

    assert wait_calls
    assert wait_calls[0]["target_states"]
    assert OperationalStateType.RESULTS_AVAILABLE_STATE.value in wait_calls[0]["target_states"]
    assert snap is not None
    assert exe._last_osue_wait is not None
    assert exe._last_osue_wait.matched is True


def test_osue_disabled_preserves_legacy_polling(_osue_runtime_off):
    mission = _mission_search()
    exe = _executor(mission)
    ms = MissionStep(id="s-search", kind="search_content", params={})
    contract = StepContract(
        step=ms,
        precondition=lambda s: True,
        postcondition=lambda s: False,
        preferred_strategy="search_content:generic",
        postcondition_timeout_ms=500,
    )
    sleep_calls: list = []

    with patch("app.services.missions.smart_executor.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        exe._await_postcondition(contract, 500, step=ms)

    assert any(abs(s - 0.35) < 0.01 for s in sleep_calls)
    assert exe._last_osue_wait is None


def test_loading_waits_for_results_available(_osue_runtime_on):
    from app.services.runtime.operational_state_understanding_engine import (
        OperationalStateInput,
        build_operational_state_snapshot,
        run_osue_postcondition_wait,
    )

    seq = [
        OperationalStateType.RESULTS_LOADING_STATE,
        OperationalStateType.RESULTS_AVAILABLE_STATE,
    ]
    idx = {"i": 0}

    def factory():
        st = seq[min(idx["i"], len(seq) - 1)]
        idx["i"] += 1
        inp = OperationalStateInput(
            runtime_validators={"loading": st == OperationalStateType.RESULTS_LOADING_STATE, "form_submitted": True},
            context_hints={"post_submit": True},
        )
        if st == OperationalStateType.RESULTS_AVAILABLE_STATE:
            inp.runtime_validators = {"results_visible": True}
            inp.dom = {"nodes": [{"role": "listitem"} for _ in range(5)]}
        snap = build_operational_state_snapshot(inp)
        snap.operational_state_type = st
        return snap

    result = run_osue_postcondition_wait(
        target_states=[OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        snapshot_factory=factory,
        timeout_ms=5000,
        poll_ms=10,
    )
    assert result.matched is True
    assert result.actual_state == OperationalStateType.RESULTS_AVAILABLE_STATE.value


def test_modal_blocks_execution(_osue_runtime_on):
    from app.contracts.mission import LiveOperationalBlocker, LiveOperationalPerceptionSnapshot
    from app.services.runtime.operational_state_understanding_engine import (
        OperationalStateInput,
        build_operational_state_snapshot,
        run_osue_postcondition_wait,
    )

    def factory():
        lop = LiveOperationalPerceptionSnapshot(
            modal_detected=True,
            live_blockers=[LiveOperationalBlocker(blocker_type="modal_blocking", severity="high")],
        )
        inp = OperationalStateInput(lop_snapshot=lop, runtime_validators={"modal_open": True})
        snap = build_operational_state_snapshot(inp)
        return snap

    result = run_osue_postcondition_wait(
        target_states=[OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        snapshot_factory=factory,
        timeout_ms=2000,
        poll_ms=10,
    )
    assert result.needs_human is True
    assert result.reason == "OSUE_BLOCKED:modal_interruption_state" or "modal" in result.final_state.lower()


def test_auth_required_requests_human(_osue_runtime_on):
    mission = _mission_search()
    exe = _executor(mission)
    contract = _always_true_contract()

    with patch(
        "app.services.runtime.operational_state_understanding_engine.build_operational_state_snapshot",
    ) as mock_snap:
        mock_snap.return_value = OperationalStateSnapshot(
            operational_state_type=OperationalStateType.AUTH_REQUIRED_STATE,
            auth_state="required",
            runtime_blockers=["auth_required"],
            state_stability_score=0.3,
        )
        with pytest.raises(_NeedsHuman):
            exe._await_postcondition(
                contract,
                2000,
                step=MissionStep(id="s-search", kind="search_content", params={}),
            )

    assert exe._last_osue_wait is not None
    assert exe._last_osue_wait.needs_human is True
    assert mission.osue_runtime_wait_audit


def test_state_mismatch_feeds_orce(_osue_runtime_on):
    mission = _mission_search()
    mission.osue_runtime_wait_audit = [
        {
            "step_id": "s-search",
            "expected_state_transitions": [
                "search_input_ready_state",
                "results_loading_state",
                "results_available_state",
            ],
            "actual_state_transitions": [
                "search_input_ready_state",
                "loading_state",
            ],
            "state_mismatch": True,
            "state_wait_timeout": False,
            "blocked_state_detected": False,
            "osue_wait_reason": "STATE_TRANSITION_MISMATCH",
        },
    ]
    mission.uol_runtime_audit = [
        {
            "step_id": "s-search",
            "strategies_attempted": ["search_content:uol_inject"],
            "handler_used": "search_content:uol_inject",
            "validation_passed": False,
        },
    ]
    report = reconcile_mission(mission)
    assert report.divergence_count >= 1
    all_reasons = [
        r
        for s in (report.step_summaries or [])
        for r in (s.get("divergence_reasons") or [])
    ]
    assert any("STATE" in r.upper() for r in all_reasons)


def test_arss_blocked_if_unstable_state(_osue_runtime_on, monkeypatch):
    from app.services.runtime import adaptive_runtime_strategy_synthesis as arss
    from app.contracts.mission import OperationalRuntimeObservation

    mission = _mission_search()
    mission.osue_last_snapshot = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.UNKNOWN_OPERATIONAL_STATE,
        ambiguity_score=0.9,
        state_stability_score=0.2,
        loading_state="active",
    ).model_dump(mode="json")

    obs = OperationalRuntimeObservation(step_id="s-search", divergence_detected=True)
    attempt = arss.synthesize_variants_from_divergence(mission, step_id="s-search", observation=obs)
    assert attempt.synthesis_strategy == "blocked_unstable_operational_state"
    assert attempt.replay_validation_results.get("blocked") is True


def test_no_fixed_sleep_as_primary_osue_path(_osue_runtime_on):
    mission = _mission_search()
    exe = _executor(mission)
    contract = _always_true_contract()
    sleep_durations: list = []

    with patch(
        "app.services.runtime.operational_state_understanding_engine.run_osue_postcondition_wait",
        return_value=OsueWaitResult(matched=True, reason="OSUE_STATE_REACHED"),
    ):
        with patch("app.services.missions.smart_executor.time.sleep", side_effect=lambda s: sleep_durations.append(s)):
            exe._await_postcondition(contract, 2000, step=MissionStep(id="s-search", kind="search_content", params={}))

    assert not any(abs(s - 0.35) < 0.01 for s in sleep_durations)


def test_machine_required_states_respected(_osue_runtime_on):
    from app.services.runtime.operational_state_understanding_engine import (
        evaluate_step_compatibility,
        resolve_postcondition_target_states,
        resolve_machine_step_for_mission,
    )

    mission = _mission_search()
    ms = resolve_machine_step_for_mission(mission, "s-search")
    assert ms is not None
    targets = resolve_postcondition_target_states(ms, "search_content")
    assert OperationalStateType.SEARCH_INPUT_READY_STATE.value in (
        ms.params.get("required_operational_states") or []
    )
    assert OperationalStateType.RESULTS_AVAILABLE_STATE.value in targets

    ready = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.SEARCH_INPUT_READY_STATE,
        state_stability_score=0.85,
        ambiguity_score=0.1,
    )
    compat = evaluate_step_compatibility(ready, ms)
    assert compat.compatible is True


def test_timeout_produces_osue_state_timeout(_osue_runtime_on):
    from app.services.runtime.operational_state_understanding_engine import (
        OperationalStateInput,
        build_operational_state_snapshot,
        run_osue_postcondition_wait,
    )

    def factory():
        inp = OperationalStateInput(runtime_validators={"loading": True})
        return build_operational_state_snapshot(inp)

    result = run_osue_postcondition_wait(
        target_states=[OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        snapshot_factory=factory,
        timeout_ms=80,
        poll_ms=20,
    )
    assert result.timed_out is True
    assert result.reason == "OSUE_STATE_TIMEOUT"

    mission = _mission_search()
    exe = _executor(mission)
    exe._last_osue_wait = result
    assert exe._osue_postcondition_error() == "OSUE_STATE_TIMEOUT"


def test_expert_panel_shows_osue_wait_result(_osue_runtime_on):
    from app.services.runtime.operational_state_understanding_engine import expert_panel_lines

    mission = _mission_search()
    mission.osue_runtime_wait_audit = [
        {
            "step_id": "s-search",
            "matched": False,
            "state_wait_timeout": True,
            "osue_wait_reason": "OSUE_STATE_TIMEOUT",
            "expected_state": "results_available_state",
            "actual_state": "loading_state",
            "stability_score": 0.42,
            "polled": 5,
            "elapsed_ms": 8000,
        },
    ]
    lines = expert_panel_lines(mission, expert_mode=True)
    assert any("Runtime Wait" in ln for ln in lines)
    assert any("timeout" in ln.lower() or "OSUE_STATE_TIMEOUT" in ln for ln in lines)
