"""search_content UOL sub-steps ↔ ORCE alignment + OSUE validator bridge."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import MachineExecutionStep, OperationalExecutionExpectation
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.missions.uol_action_handlers import (
    SEARCH_CONTENT_RESOLUTION_SUBSTEPS,
    uol_search_content_surface,
)
from app.services.missions.uol_runtime_telemetry import (
    UolStepTelemetryRecord,
    UolStepTelemetryTracker,
)
from app.services.runtime.operational_runtime_consistency_engine import (
    build_runtime_expectation_for_machine_step,
    observation_from_uol_record,
    reconcile_step,
    search_content_substeps_valid,
)
from app.services.runtime.operational_state_understanding_engine import (
    build_osue_input_from_state_snapshot,
    run_osue_postcondition_wait,
)


_FULL_SUBSTEPS = list(SEARCH_CONTENT_RESOLUTION_SUBSTEPS)


def _search_exp() -> OperationalExecutionExpectation:
    return build_runtime_expectation_for_machine_step(
        MachineExecutionStep(step_id="p03", semantic_type="search_content"),
    )


def test_search_content_substeps_complete_orce_score_high():
    exp = _search_exp()
    obs = observation_from_uol_record(
        {
            "step_id": "p03",
            "handler_used": "search_content:uol_surface",
            "validation_passed": True,
            "execution_latency_ms": 1800,
            "runtime_resolution_substeps": _FULL_SUBSTEPS,
        },
    )
    reconciled = reconcile_step(exp, obs)
    assert not reconciled.divergence_detected
    assert reconciled.consistency_score >= 0.90


def test_search_content_surface_only_still_penalized():
    exp = _search_exp()
    obs = observation_from_uol_record(
        {
            "step_id": "p03",
            "handler_used": "search_content:uol_surface",
            "strategies_attempted": ["search_content:uol_surface"],
            "validation_passed": True,
        },
    )
    assert obs.actual_runtime_path == ["surface"]
    reconciled = reconcile_step(exp, obs)
    assert reconciled.divergence_detected
    assert reconciled.consistency_score < 0.90


def test_search_content_validation_failed_penalized():
    exp = _search_exp()
    obs = observation_from_uol_record(
        {
            "step_id": "p03",
            "handler_used": "search_content:uol_surface",
            "validation_passed": False,
            "runtime_resolution_substeps": _FULL_SUBSTEPS,
        },
    )
    reconciled = reconcile_step(exp, obs)
    assert reconciled.divergence_detected


def test_search_content_substeps_valid_requires_core_steps():
    assert search_content_substeps_valid(_FULL_SUBSTEPS, validation_passed=True)
    assert not search_content_substeps_valid(["surface"], validation_passed=True)
    assert not search_content_substeps_valid(_FULL_SUBSTEPS, validation_passed=False)


def test_search_results_visible_feeds_osue_input():
    state = StateSnapshot(
        browser_url="https://www.youtube.com/results?search_query=luis+miguel",
        active_app="chrome.exe",
    )
    inp = build_osue_input_from_state_snapshot(state)
    assert inp.runtime_validators.get("search_results_visible") is True
    assert inp.runtime_validators.get("results_visible") is True


def test_execution_latency_not_full_step_time():
    mission = MagicMock()
    mission.uol_runtime_audit = []
    step = MissionStep(
        id="p03",
        kind="search_content",
        params={"uol_action": "search_content", "preferred_strategy": "search_content:uol_surface"},
    )
    tracker = UolStepTelemetryTracker(mission=mission, step=step, run_id="r1")
    tracker.record_strategy_attempt(
        "search_content:uol_surface",
        MagicMock(
            ok=True,
            extra={
                "uol_handler": "search_content:uol_surface",
                "runtime_resolution_substeps": _FULL_SUBSTEPS,
                "execution_latency_ms": 2100,
                "validation_status": "passed",
            },
        ),
        validation_passed=True,
    )
    tracker.finalize(success=True, validation_passed=True, strategy_used="search_content:uol_surface")
    assert mission.uol_runtime_audit[-1]["execution_latency_ms"] == 2100
    assert mission.uol_runtime_audit[-1]["latency_ms"] == 2100


def test_handler_emits_runtime_resolution_substeps():
    step = MissionStep(
        id="p03",
        kind="search_content",
        params={"query": "test query", "site": "youtube"},
    )
    state = StateSnapshot(
        browser_url="https://www.youtube.com/results?search_query=test+query",
        active_app="chrome.exe",
    )
    with patch("app.services.missions.uol_action_handlers.ar._search_youtube_dom") as dom:
        dom.return_value = MagicMock(ok=True, message="ok", duration_ms=50)
        with patch("app.services.missions.uol_action_handlers._get_playwright_page", return_value=None):
            res = uol_search_content_surface(step, state)
    assert res.ok
    substeps = (res.extra or {}).get("runtime_resolution_substeps") or []
    assert "focus_input" in substeps
    assert "inject_text" in substeps
    assert "submit_input" in substeps
    assert "validate_results" in substeps
    assert (res.extra or {}).get("execution_latency_ms", 99999) < 5000


def test_no_smart_route_no_coords_in_uol_surface_handler():
    step = MissionStep(id="p03", kind="search_content", params={"query": "x"})
    state = StateSnapshot(active_app="chrome.exe")
    mock_auto = MagicMock()
    mock_auto.GetForegroundControl.return_value = None
    with patch("app.services.missions.uol_action_handlers._get_playwright_page", return_value=None):
        with patch.dict("sys.modules", {"uiautomation": mock_auto}):
            with patch("app.services.missions.uol_action_handlers.ar._search_web_address_bar") as omn:
                omn.return_value = MagicMock(ok=True, message="ok", duration_ms=10)
                with patch("app.services.missions.uol_action_handlers._validate_search_outcome", return_value=True):
                    res = uol_search_content_surface(step, state)
    assert res.ok
    assert "smart_route" not in str(res.strategy)
    assert "coords" not in str(res.extra or {})


def test_osue_wait_matches_results_url():
    state = StateSnapshot(
        browser_url="https://www.youtube.com/results?search_query=demo",
        active_app="chrome.exe",
        uia_visible_names=["a", "b", "c", "d"],
    )

    def factory():
        from app.services.runtime.operational_state_understanding_engine import (
            build_operational_state_snapshot,
        )

        inp = build_osue_input_from_state_snapshot(state)
        return build_operational_state_snapshot(inp)

    result = run_osue_postcondition_wait(
        target_states=["results_available_state"],
        snapshot_factory=factory,
        timeout_ms=2000,
        poll_ms=50,
    )
    assert result.matched
