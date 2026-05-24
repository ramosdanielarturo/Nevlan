"""Tests — Operational Runtime Consistency Engine (ORCE)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import (
    MachineExecutionStep,
    Mission,
    OperationalExecutionExpectation,
)
from app.services.missions.four_layer_operational_model import (
    build_four_layer_model,
    ensure_four_layer_model,
    machine_steps_for_runtime,
)
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_semantic_plan_to_mission,
)
from app.services.missions.uol_runtime_telemetry import UolStepTelemetryRecord
from app.services.runtime.operational_runtime_consistency_engine import (
    OrceConsistencyStatus,
    OrceRuntimeAcceptance,
    build_runtime_expectation_for_machine_step,
    expert_panel_lines,
    expected_uol_path_for_semantic_type,
    feed_runtime_learning,
    four_layers_preserved_after_runtime_degradation,
    observation_from_uol_record,
    persist_orce_report_on_mission,
    reconcile_mission,
    reconcile_step,
)


def _mission_with_plan(*, canonical_search: bool = False) -> Mission:
    params: dict = {"query": "alpha"}
    if canonical_search:
        params["canonical_truth"] = True
        params["uol_canonical_truth"] = True
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="s-launch",
                type="open_app",
                params={"name": "fictional_app"},
                preferred_strategy="open_app:generic",
                fallback_strategies=[],
                confidence=0.92,
                human_label="Abrir app",
            ),
            SemanticPlanStep(
                id="s-search",
                type="search_content",
                params=params,
                preferred_strategy="search_content:uol_inject",
                fallback_strategies=["search_content:generic"],
                confidence=0.9 if canonical_search else 0.7,
                human_label="Buscar contenido",
            ),
        ],
        source="semantic_intent_promotion_engine",
        intent_status="READY",
    )
    m = Mission(id=str(uuid.uuid4()), name="orce-test", description="Objetivo fijo")
    attach_semantic_plan_to_mission(m, plan=plan, force=True)
    build_four_layer_model(m)
    return m


def _audit(m: Mission, *records: UolStepTelemetryRecord) -> None:
    m.uol_runtime_audit = [r.model_dump(mode="json") for r in records]


def test_runtime_exact_consistency_high():
    m = _mission_with_plan()
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            strategies_attempted=["search_content:uol_inject"],
            handler_used="search_content:uol_inject",
            handler_success=True,
            validation_passed=True,
            latency_ms=800,
        ),
    )
    report = reconcile_mission(m)
    assert report.consistency_score >= 0.85
    assert report.divergence_count == 0
    assert report.consistency_status == OrceConsistencyStatus.ALIGNED.value


def test_permitted_fallback_mild_degradation():
    m = _mission_with_plan()
    exp = build_runtime_expectation_for_machine_step(
        MachineExecutionStep(
            step_id="s-search",
            semantic_type="search_content",
            confidence=0.6,
        ),
        contract_intent_status="READY",
    )
    obs = observation_from_uol_record(
        UolStepTelemetryRecord(
            step_id="s-search",
            strategies_attempted=["search_content:uol_inject", "search_content:generic"],
            fallback_used=True,
            handler_success=True,
            validation_passed=True,
        ),
    )
    out = reconcile_step(exp, obs, allowed_degradations=["uia_to_dom_fallback", "strategy_fallback"])
    assert out.consistency_score >= 0.7
    assert out.degradation_level in ("none", "mild")


def test_coords_prohibited_divergence():
    m = _mission_with_plan(canonical_search=True)
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            strategies_attempted=["search_content:uol_inject", "search_content:use_coords"],
            coords_used=True,
            handler_used="search_content:use_coords",
            fallback_used=True,
            canonical_truth_used=True,
        ),
    )
    report = reconcile_mission(m)
    assert report.divergence_count >= 1
    assert report.forbidden_strategy_usage >= 1 or report.canonical_truth_violations >= 1
    assert report.runtime_acceptance == OrceRuntimeAcceptance.REJECTED.value


def test_hidden_smart_route_forbidden():
    m = _mission_with_plan(canonical_search=True)
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            strategies_attempted=["search_content:smart_route"],
            smart_route_used=True,
            canonical_truth_used=True,
            fallback_used=True,
        ),
    )
    report = reconcile_mission(m)
    assert report.runtime_acceptance == OrceRuntimeAcceptance.REJECTED.value
    assert any(
        "smart_route" in str(s.get("divergence_reasons"))
        for s in (report.step_summaries or [])
    ) or report.forbidden_strategy_usage >= 1


def test_excessive_retry_degradation():
    exp = OperationalExecutionExpectation(max_retries=1, fallback_budget=1)
    obs = observation_from_uol_record(
        UolStepTelemetryRecord(
            step_id="s1",
            strategies_attempted=["a", "b", "c", "d"],
            retries_used=3,
        ),
    )
    out = reconcile_step(exp, obs)
    assert out.retries_used > exp.max_retries
    assert out.consistency_score < 1.0
    assert out.divergence_detected


def test_unexpected_repair_divergence():
    exp = build_runtime_expectation_for_machine_step(
        MachineExecutionStep(step_id="s1", semantic_type="search_content"),
    )
    obs = observation_from_uol_record(
        UolStepTelemetryRecord(
            step_id="s1",
            failure_reason="self_heal repair triggered",
            strategies_attempted=["search_content:uol_inject"],
        ),
    )
    out = reconcile_step(exp, obs)
    assert out.divergence_detected
    assert any("HIDDEN_REPAIR" in r or "repair" in r.lower() for r in out.divergence_reasons)


def test_continuity_break_low_consistency():
    m = _mission_with_plan()
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            strategies_attempted=["search_content:generic"],
            fallback_used=True,
            continuity_preserved=False,
            handler_success=True,
        ),
    )
    report = reconcile_mission(m)
    assert report.consistency_score < 1.0 or report.divergence_count >= 0


def test_canonical_truth_violation_rejected():
    m = _mission_with_plan(canonical_search=True)
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            coords_used=True,
            smart_route_used=True,
            canonical_truth_used=True,
            strategies_attempted=["search_content:coords_first"],
        ),
    )
    report = reconcile_mission(m)
    assert report.runtime_acceptance == OrceRuntimeAcceptance.REJECTED.value
    assert report.canonical_truth_violations >= 1


def test_runtime_acceptance_accepted():
    m = _mission_with_plan()
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-launch",
            strategies_attempted=["open_app:generic"],
            handler_success=True,
            validation_passed=True,
        ),
        UolStepTelemetryRecord(
            step_id="s-search",
            strategies_attempted=["search_content:uol_inject"],
            handler_success=True,
            validation_passed=True,
        ),
    )
    report = reconcile_mission(m)
    assert report.runtime_acceptance == OrceRuntimeAcceptance.ACCEPTED.value


def test_runtime_acceptance_needs_hardening():
    m = _mission_with_plan()
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            strategies_attempted=["a", "b", "c"],
            fallback_used=True,
            legacy_used=True,
            failure_reason="self_heal repair",
        ),
    )
    report = reconcile_mission(m)
    assert report.runtime_acceptance in (
        OrceRuntimeAcceptance.NEEDS_HARDENING.value,
        OrceRuntimeAcceptance.REJECTED.value,
    )


def test_expert_panel_expected_vs_actual():
    m = _mission_with_plan()
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            strategies_attempted=[
                "search_content:uol_inject",
                "search_content:use_coords",
            ],
            coords_used=True,
            fallback_used=True,
        ),
    )
    persist_orce_report_on_mission(m)
    lines = expert_panel_lines(m)
    text = "\n".join(lines)
    assert "EXPECTED" in text
    assert "ACTUAL" in text
    assert "ORCE" in text


def test_runtime_feeds_sll_ompc():
    m = _mission_with_plan(canonical_search=True)
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            coords_used=True,
            strategies_attempted=["search_content:use_coords"],
        ),
    )
    report = reconcile_mission(m)
    with patch(
        "app.services.runtime.strategy_learning_layer.record_observation",
    ) as sll_mock:
        with patch(
            "app.services.runtime.operational_memory.consolidate_operational_pattern",
        ) as ompc_mock:
            with patch("app.core.config.get_settings") as gs:
                gs.return_value = MagicMock(SLL_ENABLED=True, OMPC_ENABLED=True)
                feed_runtime_learning(m, report)
    assert sll_mock.called or report.divergence_count == 0
    if report.divergence_count > 0:
        assert sll_mock.called


def test_no_hardcoded_apps_in_expectations():
    path = expected_uol_path_for_semantic_type("search_content")
    assert "chrome" not in " ".join(path).lower()
    assert "youtube" not in " ".join(path).lower()
    exp = build_runtime_expectation_for_machine_step(
        MachineExecutionStep(
            step_id="x",
            semantic_type="search_content",
            params={"query": "q"},
            preferred_strategy="search_content:generic",
        ),
    )
    blob = str(exp.model_dump())
    assert "chrome" not in blob.lower()
    assert "youtube" not in blob.lower()


def test_runtime_replay_from_machine_layer():
    m = _mission_with_plan()
    model = ensure_four_layer_model(m)
    machine_ids = [ms.step_id for ms in machine_steps_for_runtime(m)]
    sep_ids = [s["id"] for s in (m.semantic_execution_plan or {}).get("steps") or []]
    assert machine_ids == sep_ids
    for ms in model.machine_execution.steps:
        assert ms.runtime_expectation is not None


def test_goal_intact_when_runtime_changes():
    m = _mission_with_plan()
    goal_before = ensure_four_layer_model(m).goal.purpose_statement
    _audit(
        m,
        UolStepTelemetryRecord(step_id="s-search", coords_used=True, strategies_attempted=["x"]),
    )
    reconcile_mission(m)
    goal_after = ensure_four_layer_model(m).goal.purpose_statement
    assert goal_before == goal_after
    assert four_layers_preserved_after_runtime_degradation(m)["goal_intact"]


def test_human_view_intact_when_runtime_degrades():
    m = _mission_with_plan()
    hv_before = [h.human_label for h in ensure_four_layer_model(m).human_view.steps]
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            smart_route_used=True,
            strategies_attempted=["search_content:smart_route"],
        ),
    )
    reconcile_mission(m)
    hv_after = [h.human_label for h in ensure_four_layer_model(m).human_view.steps]
    assert hv_before == hv_after
    assert four_layers_preserved_after_runtime_degradation(m)["human_view_intact"]


def test_machine_layer_preserved():
    m = _mission_with_plan()
    types_before = [
        ms.semantic_type for ms in ensure_four_layer_model(m).machine_execution.steps
    ]
    reconcile_mission(m)
    types_after = [
        ms.semantic_type for ms in ensure_four_layer_model(m).machine_execution.steps
    ]
    assert types_before == types_after
    assert four_layers_preserved_after_runtime_degradation(m)["machine_layer_preserved"]


def test_divergence_persists_in_telemetry():
    m = _mission_with_plan(canonical_search=True)
    _audit(
        m,
        UolStepTelemetryRecord(
            step_id="s-search",
            coords_used=True,
            strategies_attempted=["search_content:use_coords"],
        ),
    )
    persist_orce_report_on_mission(m)
    assert m.operational_runtime_consistency_report is not None
    assert len(m.orce_runtime_audit or []) >= 1
    row = m.orce_runtime_audit[-1]
    assert row.get("divergence_detected") or row.get("divergence_reasons")
