"""Estabilidad OSUE wait post-submit para search_content."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import OperationalStateType
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime.entity_runtime_resolution import SEARCH_RESULTS_NOT_CONFIRMED
from app.services.runtime.operational_state_understanding_engine import (
    OsueWaitResult,
    SearchOsueWaitProfile,
    is_search_submit_step,
    resolve_search_osue_wait_profile,
    run_osue_postcondition_wait,
    search_wait_should_block_recovery,
    wait_for_search_results_after_submit,
)


def _search_step(**params) -> MissionStep:
    base = {
        "site": "youtube",
        "query": "Devuélveme el amor de Luis Miguel",
        "submit": True,
        "uol_action": "search_content",
    }
    base.update(params)
    return MissionStep(id="p04", kind="search_site", params=base)


def _snap(state_type: str, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        operational_state_type=SimpleNamespace(value=state_type),
        loading_state=kwargs.get("loading_state", "idle"),
        state_stability_score=kwargs.get("stability", 0.85),
        ambiguity_score=kwargs.get("ambiguity", 0.1),
        runtime_blockers=list(kwargs.get("blockers") or []),
    )


def _validate_results(_step: MissionStep, state: StateSnapshot) -> bool:
    url = (state.browser_url or "").lower()
    return "search_query=" in url or "/results" in url


@pytest.fixture
def fast_profile() -> SearchOsueWaitProfile:
    return SearchOsueWaitProfile(
        min_settle_ms=50,
        max_wait_ms=1200,
        poll_ms_initial=40,
        poll_ms_max=80,
        stagnant_polls_for_failure=20,
    )


def test_is_search_submit_step_true_for_search_site():
    assert is_search_submit_step(_search_step()) is True


def test_results_loading_then_available_success(fast_profile):
    step = _search_step()
    seq = [
        OperationalStateType.RESULTS_LOADING_STATE.value,
        OperationalStateType.NAVIGATION_TRANSITION_STATE.value,
        OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    ]
    calls = {"i": 0}

    def factory():
        st = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return _snap(st, loading_state="active")

    result = run_osue_postcondition_wait(
        target_states=[OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        snapshot_factory=factory,
        timeout_ms=fast_profile.max_wait_ms,
        wait_profile=fast_profile,
    )
    assert result.matched is True
    assert result.reason == "RESULTS_CONFIRMED"
    assert OperationalStateType.RESULTS_LOADING_STATE.value in result.transitional_states_seen


def test_navigation_transition_then_available_success(fast_profile):
    step = _search_step()
    seq = [
        OperationalStateType.NAVIGATION_TRANSITION_STATE.value,
        OperationalStateType.RESULTS_LOADING_STATE.value,
        OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    ]
    calls = {"i": 0}

    def factory():
        st = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return _snap(st, loading_state="active")

    result = run_osue_postcondition_wait(
        target_states=[OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        snapshot_factory=factory,
        timeout_ms=fast_profile.max_wait_ms,
        wait_profile=fast_profile,
    )
    assert result.matched is True
    assert result.reason == "RESULTS_CONFIRMED"


def test_early_no_results_during_transient_window_keeps_waiting(fast_profile):
    step = _search_step()
    seq = [
        OperationalStateType.SEARCH_INPUT_READY_STATE.value,
        OperationalStateType.RESULTS_LOADING_STATE.value,
        OperationalStateType.EMPTY_RESULTS_STATE.value,
        OperationalStateType.RESULTS_LOADING_STATE.value,
        OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    ]
    calls = {"i": 0}

    def factory():
        st = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return _snap(st, loading_state="active")

    result = run_osue_postcondition_wait(
        target_states=[OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        snapshot_factory=factory,
        timeout_ms=fast_profile.max_wait_ms,
        wait_profile=fast_profile,
        blocked_states=[OperationalStateType.EMPTY_RESULTS_STATE.value],
    )
    assert result.matched is True
    assert result.osue_wait_started is True


def test_real_timeout_produces_results_timeout(fast_profile):
    step = _search_step()

    def factory():
        return _snap(OperationalStateType.SEARCH_INPUT_READY_STATE.value)

    result = run_osue_postcondition_wait(
        target_states=[OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        snapshot_factory=factory,
        timeout_ms=200,
        wait_profile=SearchOsueWaitProfile(
            min_settle_ms=30,
            max_wait_ms=200,
            poll_ms_initial=30,
            poll_ms_max=40,
        ),
    )
    assert result.matched is False
    assert result.timed_out is True
    assert result.reason == "RESULTS_TIMEOUT"


def test_blocked_modal_needs_human(fast_profile):
    result = run_osue_postcondition_wait(
        target_states=[OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        snapshot_factory=lambda: _snap(
            OperationalStateType.MODAL_INTERRUPTION_STATE.value,
        ),
        timeout_ms=fast_profile.max_wait_ms,
        wait_profile=fast_profile,
    )
    assert result.needs_human is True
    assert result.reason == "BLOCKED_STATE"


def test_wait_for_search_results_url_validator_success(fast_profile):
    step = _search_step()
    states = [
        StateSnapshot(browser_url="https://www.youtube.com/"),
        StateSnapshot(browser_url="https://www.youtube.com/results?search_query=demo"),
    ]
    calls = {"i": 0}

    def detect():
        st = states[min(calls["i"], len(states) - 1)]
        calls["i"] += 1
        return st

    ok, meta = wait_for_search_results_after_submit(
        step,
        detect_fn=detect,
        validate_fn=_validate_results,
        profile=SearchOsueWaitProfile(
            min_settle_ms=20,
            max_wait_ms=800,
            poll_ms_initial=30,
            poll_ms_max=50,
        ),
    )
    assert ok is True
    assert meta.reason == "RESULTS_CONFIRMED"
    assert meta.osue_wait_started is True


def test_three_synthetic_runs_variable_loading_all_success():
    profiles = [
        [OperationalStateType.RESULTS_LOADING_STATE.value] * 2
        + [OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        [OperationalStateType.NAVIGATION_TRANSITION_STATE.value]
        + [OperationalStateType.RESULTS_LOADING_STATE.value] * 3
        + [OperationalStateType.RESULTS_AVAILABLE_STATE.value],
        [OperationalStateType.LOADING_STATE.value]
        + [OperationalStateType.RESULTS_AVAILABLE_STATE.value],
    ]
    for seq in profiles:
        calls = {"i": 0}

        def factory(seq=seq, calls=calls):
            st = seq[min(calls["i"], len(seq) - 1)]
            calls["i"] += 1
            return _snap(st, loading_state="active")

        result = run_osue_postcondition_wait(
            target_states=[OperationalStateType.RESULTS_AVAILABLE_STATE.value],
            snapshot_factory=factory,
            timeout_ms=1500,
            wait_profile=SearchOsueWaitProfile(
                min_settle_ms=30,
                max_wait_ms=1500,
                poll_ms_initial=25,
                poll_ms_max=60,
            ),
        )
        assert result.matched is True
        assert result.reason == "RESULTS_CONFIRMED"


def test_search_wait_blocks_recovery_on_transitional_timeout():
    wr = OsueWaitResult(
        timed_out=True,
        reason="RESULTS_TIMEOUT",
        transitional_states_seen=[
            OperationalStateType.RESULTS_LOADING_STATE.value,
        ],
    )
    assert search_wait_should_block_recovery(wr) is True


def test_smart_executor_maps_timeout_to_search_results_not_confirmed():
    from app.services.missions.smart_executor import SmartMissionExecutor

    exe = SmartMissionExecutor(mission_ref=MagicMock())
    exe._last_osue_wait = OsueWaitResult(timed_out=True, reason="RESULTS_TIMEOUT")
    assert exe._osue_postcondition_error() == SEARCH_RESULTS_NOT_CONFIRMED


def test_uol_handler_wait_no_coords_no_smart_route():
    import app.services.missions.uol_action_handlers as mod

    src = open(mod.__file__, encoding="utf-8").read().lower()
    start = src.index("def _validate_search_outcome_after_action")
    end = src.index("def _search_content_substeps_complete")
    block = src[start:end]
    assert "relative_coords" not in block
    assert "smart_route" not in block
    assert "time.sleep(0.45)" not in block


def test_resolve_search_osue_wait_profile_uses_deadline():
    prof = resolve_search_osue_wait_profile(_search_step(), deadline_ms=9000)
    assert prof.max_wait_ms == 9000
    assert prof.min_settle_ms >= 400
