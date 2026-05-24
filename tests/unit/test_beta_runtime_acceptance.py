"""Tests — Beta Runtime Acceptance (OSUE + ORCE + UOL + ARSS)."""

from __future__ import annotations

import uuid

import pytest

from app.contracts.mission import Mission, MissionStatus
from app.core.config import Settings
from app.services.missions.four_layer_operational_model import build_four_layer_model
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_semantic_plan_to_mission,
)
from app.services.missions.uol_runtime_telemetry import UolStepTelemetryRecord
from app.services.runtime.beta_runtime_acceptance import (
    BetaRuntimeAcceptanceStatus,
    beta_runtime_normal_label,
    compute_beta_runtime_acceptance_report,
    enable_beta_runtime_profile,
    expert_panel_lines,
)
from app.services.runtime.operational_runtime_consistency_engine import persist_orce_report_on_mission


def _mission_with_telemetry(*, canonical: bool = False) -> Mission:
    params: dict = {"query": "demo"}
    if canonical:
        params["canonical_truth"] = True
        params["uol_canonical_truth"] = True
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="s-search",
                type="search_content",
                params=params,
                preferred_strategy="search_content:uol_inject",
                confidence=0.92 if canonical else 0.7,
                human_label="Buscar",
            ),
        ],
        source="test",
        intent_status="READY",
    )
    m = Mission(id=str(uuid.uuid4()), name="beta-acc", status=MissionStatus.EXECUTABLE)
    attach_semantic_plan_to_mission(m, plan=plan, force=True)
    build_four_layer_model(m)
    return m


def _populate_high_telemetry(m: Mission) -> None:
    m.uol_runtime_audit = [
        UolStepTelemetryRecord(
            step_id="s-search",
            handler_used="search_content:uol_inject",
            handler_success=True,
            validation_passed=True,
            latency_ms=600,
            strategies_attempted=["search_content:uol_inject"],
        ).model_dump(mode="json"),
    ]
    m.osue_runtime_wait_audit = [
        {
            "step_id": "s-search",
            "matched": True,
            "state_wait_timeout": False,
            "blocked_state_detected": False,
            "expected_state": "results_available_state",
            "actual_state": "results_available_state",
        },
    ]
    persist_orce_report_on_mission(m)


def test_accepted_with_high_uol_orce_osue():
    m = _mission_with_telemetry()
    _populate_high_telemetry(m)
    report = compute_beta_runtime_acceptance_report(m)
    assert report.status == BetaRuntimeAcceptanceStatus.ACCEPTED
    assert report.uol_native_success_ratio >= 0.85
    assert report.orce_consistency_score >= 0.85
    assert report.osue_state_success_ratio >= 0.85
    assert report.coords_usage_count == 0


def test_needs_hardening_osue_timeout():
    m = _mission_with_telemetry()
    _populate_high_telemetry(m)
    m.osue_runtime_wait_audit = [
        {
            "step_id": "s-search",
            "matched": False,
            "state_wait_timeout": True,
            "osue_wait_reason": "OSUE_STATE_TIMEOUT",
            "expected_state": "results_available_state",
            "actual_state": "loading_state",
        },
    ]
    report = compute_beta_runtime_acceptance_report(m)
    assert report.status == BetaRuntimeAcceptanceStatus.NEEDS_HARDENING
    assert report.runtime_timeout_count >= 1
    assert any("timeout" in r.lower() for r in report.hardening_reasons)


def test_rejected_coords_forbidden():
    m = _mission_with_telemetry(canonical=True)
    _populate_high_telemetry(m)
    m.uol_runtime_audit[0]["coords_used"] = True
    m.uol_runtime_audit[0]["canonical_truth_used"] = True
    report = compute_beta_runtime_acceptance_report(m)
    assert report.status == BetaRuntimeAcceptanceStatus.REJECTED
    assert report.coords_usage_count >= 1


def test_rejected_smart_route_canonical():
    m = _mission_with_telemetry(canonical=True)
    _populate_high_telemetry(m)
    m.uol_runtime_audit[0]["smart_route_used"] = True
    m.uol_runtime_audit[0]["canonical_truth_used"] = True
    m.uol_runtime_audit[0]["handler_success"] = False
    report = compute_beta_runtime_acceptance_report(m)
    assert report.status in {
        BetaRuntimeAcceptanceStatus.REJECTED,
        BetaRuntimeAcceptanceStatus.NEEDS_HARDENING,
    }


def test_rejected_canonical_truth_violation():
    m = _mission_with_telemetry(canonical=True)
    _populate_high_telemetry(m)
    m.uol_runtime_audit[0]["coords_used"] = True
    m.uol_runtime_audit[0]["canonical_truth_used"] = True
    m.uol_runtime_audit[0]["handler_success"] = False
    report = compute_beta_runtime_acceptance_report(m)
    assert report.status == BetaRuntimeAcceptanceStatus.REJECTED
    assert any("canonical" in r or "coords" in r for r in report.hardening_reasons)


def test_weakest_steps_populated():
    m = _mission_with_telemetry()
    _populate_high_telemetry(m)
    m.osue_runtime_wait_audit[0]["matched"] = False
    m.osue_runtime_wait_audit[0]["state_wait_timeout"] = True
    report = compute_beta_runtime_acceptance_report(m)
    assert report.weakest_steps
    assert report.weakest_steps[0].get("step_id") == "s-search"


def test_recommended_next_action_clear():
    m = _mission_with_telemetry()
    _populate_high_telemetry(m)
    m.osue_runtime_wait_audit[0]["state_wait_timeout"] = True
    m.osue_runtime_wait_audit[0]["matched"] = False
    report = compute_beta_runtime_acceptance_report(m)
    assert report.recommended_next_action
    assert len(report.recommended_next_action) > 12


def test_no_data_without_telemetry():
    m = _mission_with_telemetry()
    report = compute_beta_runtime_acceptance_report(m)
    assert report.status == BetaRuntimeAcceptanceStatus.NO_DATA


def test_beta_profile_activates_flags():
    s = Settings(BETA_PRIVATE_RUNTIME_PROFILE=True)
    assert s.OSUE_ENABLED is True
    assert s.OSUE_SHADOW_ENABLED is True
    assert s.PCOF_ENABLED is True
    assert s.RELIABILITY_HARDENING_ENABLED is True
    assert s.MAGIC_UX_SANITIZE_RUNNER_STATUS is True
    assert s.COB_EXECUTION_PILOT_ENABLED is False

    s2 = Settings(BETA_PRIVATE_RUNTIME_PROFILE=False)
    enable_beta_runtime_profile(s2, arss_enabled=True)
    assert s2.ARSS_ENABLED is True


def test_normal_ux_simple_text():
    m = _mission_with_telemetry()
    _populate_high_telemetry(m)
    report = compute_beta_runtime_acceptance_report(m)
    m.beta_runtime_acceptance_report = report.model_dump(mode="json")
    assert beta_runtime_normal_label(m) == "Lista para ejecutar"
    assert expert_panel_lines(m, expert_mode=False) == []

    m.osue_runtime_wait_audit[0]["state_wait_timeout"] = True
    m.osue_runtime_wait_audit[0]["matched"] = False
    report2 = compute_beta_runtime_acceptance_report(m)
    m.beta_runtime_acceptance_report = report2.model_dump(mode="json")
    assert beta_runtime_normal_label(m) == "Necesita fortalecerse"

    lines = expert_panel_lines(m, expert_mode=True)
    assert any("Beta Runtime Acceptance" in ln for ln in lines)
