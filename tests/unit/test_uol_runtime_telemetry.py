"""Tests — UOL Runtime Telemetry & Acceptance Report."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.contracts.mission import Mission
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.action_runner import StrategyResult
from app.services.missions import uol_runtime_telemetry as tel
from app.services.missions.uol_runtime_telemetry import (
    UolAcceptanceStatus,
    UolStepTelemetryRecord,
    append_uol_runtime_record,
    build_uol_runtime_acceptance_report,
    expert_panel_lines,
    start_step_telemetry,
)


@pytest.fixture
def mission() -> Mission:
    return Mission(id=str(uuid.uuid4()), name="tel-test")


def test_append_audit_and_jsonl(mission, tmp_path, monkeypatch):
    monkeypatch.setattr(tel, "_UOL_JSONL", tmp_path / "uol_execution_memory_audit.jsonl")
    rec = UolStepTelemetryRecord(
        step_id="s1",
        uol_action="search_content",
        handler_used="search_content:uol_surface",
        handler_success=True,
        validation_passed=True,
        latency_ms=120,
    )
    append_uol_runtime_record(mission, rec)
    assert len(mission.uol_runtime_audit) == 1
    assert (tmp_path / "uol_execution_memory_audit.jsonl").exists()
    line = json.loads((tmp_path / "uol_execution_memory_audit.jsonl").read_text(encoding="utf-8").strip())
    assert line["step_id"] == "s1"


def test_tracker_uol_native_success(mission):
    step = MissionStep(
        id="s1",
        kind="search_content",
        params={
            "uol_action": "search_content",
            "preferred_strategy": "search_content:uol_surface",
        },
    )
    tr = start_step_telemetry(mission, step, run_id="run-1")
    assert tr.active
    res = StrategyResult(
        ok=True,
        strategy="search_content:uol_surface",
        message="ok",
        extra={
            "uol_handler": "search_content:uol_surface",
            "resolution_path": "dom",
            "validation_status": "passed",
        },
    )
    tr.record_strategy_attempt("search_content:uol_surface", res, validation_passed=True)
    tr.finalize(success=True, validation_passed=True, strategy_used="search_content:uol_surface")
    row = mission.uol_runtime_audit[0]
    assert row["handler_success"] is True
    assert row["fallback_used"] is False
    assert row["legacy_used"] is False


def test_tracker_legacy_fallback(mission):
    step = MissionStep(
        id="s2",
        kind="select_entity_from_collection",
        params={
            "uol_action": "select_entity_from_collection",
            "preferred_strategy": "select_entity_from_collection:uol_entity",
        },
    )
    tr = start_step_telemetry(mission, step)
    tr.record_strategy_attempt(
        "select_entity_from_collection:uol_entity",
        StrategyResult(ok=False, strategy="select_entity_from_collection:uol_entity", message="fail"),
    )
    tr.record_strategy_attempt(
        "select_profile:uia_text_match",
        StrategyResult(ok=True, strategy="select_profile:uia_text_match", message="ok"),
        validation_passed=True,
    )
    tr.finalize(
        success=True,
        validation_passed=True,
        strategy_used="select_profile:uia_text_match",
    )
    row = mission.uol_runtime_audit[0]
    assert row["fallback_used"] is True
    assert row["legacy_used"] is True
    assert row["fallback_strategy"] == "select_profile:uia_text_match"


def test_smart_route_and_coords_detected(mission):
    step = MissionStep(
        id="s3",
        kind="search_content",
        params={"uol_action": "search_content", "preferred_strategy": "search_content:uol_surface"},
    )
    tr = start_step_telemetry(mission, step)
    tr.record_strategy_attempt(
        "search_content:smart_route",
        StrategyResult(ok=False, strategy="search_content:smart_route", message="sr"),
    )
    tr.record_strategy_attempt(
        "search_youtube:relative_coords",
        StrategyResult(ok=False, strategy="search_youtube:relative_coords", message="coords"),
    )
    tr.finalize(success=False, failure_reason="failed")
    row = mission.uol_runtime_audit[0]
    assert row["smart_route_used"] is True
    assert row["coords_used"] is True


def test_canonical_truth_in_record(mission):
    step = MissionStep(
        id="s4",
        kind="select_entity_from_collection",
        params={
            "uol_action": "select_entity_from_collection",
            "uol_canonical_truth": True,
            "canonical_operational_intent": {"canonical_truth": True, "intent_id": "coi-99"},
        },
    )
    tr = start_step_telemetry(mission, step)
    assert tr.record.canonical_truth_used is True
    assert tr.record.coi_intent_id == "coi-99"


def test_acceptance_report_accepted(mission):
    for i in range(6):
        ok = True
        append_uol_runtime_record(
            mission,
            UolStepTelemetryRecord(
                step_id=f"s{i}",
                uol_action="open_application",
                handler_used="open_application:uol_launcher",
                handler_success=ok,
                validation_passed=ok,
                latency_ms=100,
            ),
        )
    report = build_uol_runtime_acceptance_report(mission)
    assert report.total_uol_steps == 6
    assert report.uol_native_success_ratio >= 0.85
    assert report.acceptance_status == UolAcceptanceStatus.ACCEPTED


def test_acceptance_report_needs_hardening_coords(mission):
    append_uol_runtime_record(
        mission,
        UolStepTelemetryRecord(
            step_id="s1",
            uol_action="search_content",
            handler_used="search_content:uol_surface",
            handler_success=True,
            coords_used=True,
            validation_passed=True,
        ),
    )
    report = build_uol_runtime_acceptance_report(mission)
    assert report.acceptance_status == UolAcceptanceStatus.NEEDS_HARDENING
    assert report.coords_usage_count == 1


def test_acceptance_smart_route_on_canonical(mission):
    append_uol_runtime_record(
        mission,
        UolStepTelemetryRecord(
            step_id="s1",
            uol_action="search_content",
            canonical_truth_used=True,
            smart_route_used=True,
            handler_success=False,
            validation_passed=False,
        ),
    )
    report = build_uol_runtime_acceptance_report(mission)
    assert report.acceptance_status == UolAcceptanceStatus.NEEDS_HARDENING


def test_expert_panel_lines(mission):
    append_uol_runtime_record(
        mission,
        UolStepTelemetryRecord(
            step_id="s1",
            uol_action="search_content",
            handler_used="search_content:uol_surface",
            handler_success=True,
            validation_passed=True,
            latency_ms=50,
        ),
    )
    lines = expert_panel_lines(mission)
    assert any("UOL Runtime Acceptance" in ln for ln in lines)
    assert any("UOL-native" in ln for ln in lines)


def test_legacy_mission_without_uol_no_break(mission):
    step = MissionStep(id="x", kind="open_app", params={"name": "chrome"})
    tr = start_step_telemetry(mission, step)
    assert tr.active is False
    tr.finalize(success=True)
    assert mission.uol_runtime_audit == []
    report = build_uol_runtime_acceptance_report(mission)
    assert report.acceptance_status == UolAcceptanceStatus.NO_UOL_DATA


def test_step_without_uol_telemetry_inactive():
    step = MissionStep(id="x", kind="noop", params={})
    assert tel.step_has_uol_telemetry(step) is False
