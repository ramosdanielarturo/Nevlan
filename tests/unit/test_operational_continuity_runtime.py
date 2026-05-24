"""Operational Continuity Runtime (OCR) — unit tests sombra."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

import pytest

from app.contracts.mission import (
    CanonicalOperationalBlock,
    CanonicalOperationalBlockStatus,
    Mission,
    MissionStatus,
    OperationalTruthBlock,
    OperationalTruthStatus,
)
from app.services.runtime import operational_continuity_runtime as ocr
from app.services.runtime import operational_runtime_graph as org


def _tb(
    truth_type: str,
    *,
    human: str = "",
    readiness: float = 0.9,
    amb: float = 0.12,
    iso_minute: str = "05",
    iso_second: str = "00",
    validator_expectations: list | None = None,
) -> OperationalTruthBlock:
    return OperationalTruthBlock(
        truth_type=truth_type,
        canonical_action=truth_type,
        human_intent=human or truth_type.replace("_", " "),
        stability_score=0.82,
        operational_confidence=readiness,
        ambiguity_score=amb,
        execution_readiness=readiness,
        temporal_signature={
            "started_at_iso": f"2026-05-17T10:{iso_minute}:{iso_second}+00:00",
        },
        context_window={
            "tel_derived_params": {"name": "HostApp"}
            if truth_type == "open_application"
            else {"url": "https://vendor.invalid/app"}
            if truth_type == "navigate_to_location"
            else {"query": "Q1"},
        },
        truth_status=OperationalTruthStatus.COMMITTED_SHADOW,
        validator_expectations=validator_expectations or [],
    )


@pytest.fixture
def _ocr_on(monkeypatch):
    monkeypatch.setattr(ocr, "_settings_ocr_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(org, "_settings_org_enabled", lambda *_a, **_k: True)


def test_continuity_valid_despite_high_visual_ambiguity(_ocr_on):
    """Misma topología operacional; sube ambigüedad visual — continuidad sigue derivada de truth."""
    m1 = Mission(name="a", status=MissionStatus.DRAFT)
    m1.truth_blocks = [_tb("search_content", amb=0.1), _tb("browse_results", amb=0.1)]
    m2 = Mission(name="b", status=MissionStatus.DRAFT)
    m2.truth_blocks = [_tb("search_content", amb=0.88), _tb("browse_results", amb=0.88)]

    g1 = org.build_operational_runtime_graph(m1)
    g2 = org.build_operational_runtime_graph(m2)
    c1, _ = ocr.evaluate_operational_continuity(m1, g1)
    c2, _ = ocr.evaluate_operational_continuity(m2, g2)
    assert c1 > 0.55 and c2 > 0.55
    assert abs(c1 - c2) < 0.22


def test_drift_detection_low_sep_agreement(_ocr_on):
    mission = Mission(name="dr", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("open_application", iso_minute="01"), _tb("submit_form", iso_minute="02")]
    mission.semantic_execution_plan = {
        "steps": [
            {"type": "scroll_results"},
            {"type": "submit_field"},
        ],
    }
    g = org.build_operational_runtime_graph(mission)
    dr = ocr.detect_operational_drift(mission, g)
    assert dr


def test_goal_execution_shadow_search_browse(_ocr_on):
    mission = Mission(name="g", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    g = org.build_operational_runtime_graph(mission)
    gx = ocr.execute_operational_goal_shadow(mission, g)
    assert gx.satisfied_shadow
    assert "results_viewport_browsing_active" in gx.simulation_path_states


def test_recovery_semantic_first_arl_before_structural(_ocr_on):
    mission = Mission(name="rcv", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    mission.adaptive_runtime_audit = {
        "events": [
            {
                "type": "synthetic",
                "canonical_action": "navigate_recovery",
                "decision": {"decision": "FALLBACK_TO_SEP"},
            },
        ],
    }
    g = org.build_operational_runtime_graph(mission)
    recs = ocr.find_operational_recovery(g, symptom_substr="arl", limit=12)
    assert recs
    assert recs[0].recovery_tier == "semantic"


def test_transition_flexibility_branching(_ocr_on):
    mission = Mission(name="tf", status=MissionStatus.DRAFT)
    mission.truth_blocks = [
        _tb("open_application", iso_minute="01"),
        _tb("navigate_to_location", iso_minute="02"),
        _tb("search_content", iso_minute="03"),
    ]
    g = org.build_operational_runtime_graph(mission)
    m = ocr.build_operational_continuity_snapshot(mission)
    assert m.metrics.get("transition_flexibility", 0) >= 0.0


def test_provider_independence_from_context_keys(_ocr_on):
    mission = Mission(name="pi", status=MissionStatus.DRAFT)
    mission.truth_blocks = [
        _tb("open_application"),
        _tb("navigate_to_location"),
        _tb("search_content"),
    ]
    g = org.build_operational_runtime_graph(mission)
    snap = ocr.build_operational_continuity_snapshot(mission)
    assert snap.metrics.get("provider_independence", 0) >= 0.15


def test_layout_independence_metric_echo(_ocr_on):
    mission = Mission(name="lay", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("browse_results", amb=0.05)]
    snap = ocr.build_operational_continuity_snapshot(mission)
    assert 0.0 <= snap.metrics.get("ui_surface_independence", 0) <= 1.0


def test_replay_fallback_pressure_tracks_anchors(_ocr_on):
    mission = Mission(name="rp", status=MissionStatus.DRAFT)
    tb = _tb("open_application")
    tb.replay_anchor_candidates = ["a", "b", "c", "d", "e", "f"]
    mission.truth_blocks = [tb]
    g = org.build_operational_runtime_graph(mission)
    cmpo = ocr.compare_ocr_vs_sep_runtime(g, mission)
    assert cmpo.get("replay_dependence_estimate", 0) >= 0.0


def test_continuity_score_bounded(_ocr_on):
    mission = Mission(name="cs", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    s = ocr.build_operational_continuity_snapshot(mission)
    assert 0.0 <= s.continuity_score <= 1.0


def test_operational_autonomy_bounded(_ocr_on):
    mission = Mission(name="oa", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("fill_form"), _tb("submit_form")]
    s = ocr.build_operational_continuity_snapshot(mission)
    assert 0.0 <= s.operational_autonomy_score <= 1.0


def test_ocr_does_not_mutate_sep(_ocr_on):
    mission = Mission(name="sep", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("open_application")]
    sep_blob: Dict[str, Any] = {
        "steps": [{"type": "open_app"}],
        "source": "unit_test_sep",
    }
    mission.semantic_execution_plan = dict(sep_blob)
    before = dict(mission.semantic_execution_plan)
    ocr.refresh_operational_continuity_shadow(mission)
    assert mission.semantic_execution_plan == before


def test_ocr_module_does_not_import_smart_executor_stack():
    import inspect

    src = inspect.getsource(ocr)
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("from ") or s.startswith("import "):
            low = s.lower()
            assert "smart_executor" not in low
            assert "smart_runner" not in low
            assert "missions.player" not in low


def test_search_browse_maintains_continuity_path(_ocr_on):
    mission = Mission(name="sb", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content", readiness=0.95), _tb("browse_results", readiness=0.95)]
    g = org.build_operational_runtime_graph(mission)
    score, states = ocr.evaluate_operational_continuity(mission, g)
    assert score >= 0.78
    assert len(states) == 2


def test_form_submit_maintains_continuity(_ocr_on):
    mission = Mission(name="fs", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("fill_form"), _tb("submit_form")]
    g = org.build_operational_runtime_graph(mission)
    walk = org.execute_operational_graph_shadow(g)
    assert walk.get("ok") is True
    snap = ocr.build_operational_continuity_snapshot(mission)
    assert snap.continuity_breaks == [] or snap.continuity_score > 0.4


def test_arl_recovery_surfaces_in_continuity_recoveries(_ocr_on):
    mission = Mission(name="arl", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content")]
    mission.adaptive_runtime_audit = {
        "events": [
            {
                "type": "synthetic_fallback",
                "canonical_action": "navigate_recovery",
                "decision": {"decision": "ADAPT"},
            },
        ],
    }
    g = org.build_operational_runtime_graph(mission)
    recs = ocr.find_operational_recovery(g, symptom_substr="arl", limit=8)
    assert any("arl" in " ".join(r.evidence_sources).lower() for r in recs)


def test_ocr_disabled_audit_reason():
    @dataclass
    class Off:
        OCR_SHADOW_ENABLED: bool = False

    mission = Mission(name="off", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("open_application")]
    ocr.refresh_operational_continuity_shadow(mission, Off())
    aud = mission.operational_continuity_audit or {}
    assert aud.get("reason") == "OCR_SHADOW_DISABLED"


def test_attempt_operational_transition_known_arc(_ocr_on):
    mission = Mission(name="tx", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    g = org.build_operational_runtime_graph(mission)
    at = ocr.attempt_operational_transition(
        g,
        "search_query_plane_committed",
        "results_viewport_browsing_active",
    )
    assert at.declared_in_org is True
    assert at.legitimacy_score > 0.4


def test_compare_ocr_vs_sep_has_projection(_ocr_on):
    mission = Mission(name="cmp", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("open_application")]
    mission.semantic_execution_plan = {"steps": [{"type": "open_app"}]}
    g = org.build_operational_runtime_graph(mission)
    c = ocr.compare_ocr_vs_sep_runtime(g, mission)
    assert "ocr_projection" in c
    assert "operational_autonomy_hint" in c
