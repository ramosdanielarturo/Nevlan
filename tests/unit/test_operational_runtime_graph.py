"""Operational Runtime Graph (ORG) — Fase sombra."""

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
from app.services.runtime import operational_runtime_graph as org


def _tb(
    truth_type: str,
    *,
    human: str = "",
    readiness: float = 0.9,
    amb: float = 0.12,
    iso_minute: str = "05",
    iso_second: str = "00",
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
    )


def test_build_org_from_truth_blocks():
    mission = Mission(name="tb", status=MissionStatus.DRAFT)
    mission.truth_blocks = [
        _tb("open_application", iso_minute="01", iso_second="01"),
        _tb("select_entity", human="profile row", iso_minute="01", iso_second="02"),
        _tb("open_tab", iso_minute="01", iso_second="03"),
        _tb("navigate_to_location", iso_minute="01", iso_second="04"),
        _tb("search_content", human="search surface", iso_minute="01", iso_second="05"),
        _tb("browse_results", human="scroll viewport", iso_minute="01", iso_second="06"),
    ]
    graph = org.build_operational_runtime_graph(mission)
    assert "truth_blocks" in graph.build_sources_used
    assert len(graph.nodes) == len(mission.truth_blocks)
    sts = [n.operational_state for n in graph.nodes]
    assert sts[0] == "surface_host_launch_ready"
    assert "structured_form_engaged" not in sts


def test_goal_content_discovery_derived():
    mission = Mission(name="g", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    gph = org.build_operational_runtime_graph(mission)
    assert gph.goals
    headlines = [g.headline for g in gph.goals]
    assert any("content" in h.lower() for h in headlines)


def test_operational_nodes_valid_fields():
    mission = Mission(name="nf", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("switch_context")]
    node = org.build_operational_runtime_graph(mission).nodes[0]
    assert node.node_id.startswith("n_") or "_" in node.node_id  # prefixed id
    assert node.expected_validators
    assert "ambiguity_score" in (node.ambiguity_snapshot or {})


def test_transitions_not_replay_primitive_strings():
    mission = Mission(name="tnr", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("open_application"), _tb("search_content")]
    gph = org.build_operational_runtime_graph(mission)
    nasty = ("ctrl+l", "keypress_enter", "dom_click_seq", "mouse_scroll_replay")
    for t in gph.transitions:
        blob = (t.description + " " + " ".join(t.legitimate_surface_classes)).lower()
        for n in nasty:
            assert n not in blob


def test_recovery_edges_templates():
    mission = Mission(name="rcv", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    gph = org.build_operational_runtime_graph(mission)
    classes = [e.recovery_class for e in gph.recovery_edges]
    assert "reassert_search_plane" in classes


def test_predict_next_operational_state():
    mission = Mission(name="pred", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("open_application"), _tb("open_tab")]
    gph = org.build_operational_runtime_graph(mission)
    nx = org.predict_next_operational_state(gph, "surface_host_launch_ready")
    assert nx == ["browsing_tab_surface_ready"]


def test_compare_org_vs_sep_runtime():
    mission = Mission(name="cmp", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("open_application"), _tb("search_content")]
    mission.semantic_execution_plan = {
        "steps": [
            {"type": "open_app", "confidence": 0.8},
            {"type": "search_content", "confidence": 0.8},
        ],
    }
    gph = org.build_operational_runtime_graph(mission)
    r = org.compare_org_vs_sep_runtime(gph, mission)
    assert r["org_state_count"] == 2
    assert r["sep_step_count"] == 2
    assert r["paired_agreement_ratio"] == pytest.approx(1.0)


def test_recovery_coverage_score_bounded():
    mission = Mission(name="rcov", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    gph = org.build_operational_runtime_graph(mission)
    assert gph.metrics["recovery_coverage"] <= 1.0
    assert gph.metrics["operational_resilience_score"] <= 1.0


def test_selector_independence_score_bounded():
    mission = Mission(name="sel", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("choose_option", readiness=0.95)]
    m = org.build_operational_runtime_graph(mission).metrics
    assert 0.0 <= m["selector_independence"] <= 1.0


def test_layout_independence_bounded():
    mission = Mission(name="lay", status=MissionStatus.DRAFT)
    t = _tb("browse_results")
    mission.truth_blocks = [t]
    m = org.build_operational_runtime_graph(mission).metrics
    assert 0.0 <= m["layout_independence"] <= 1.0


@pytest.fixture
def _dummy_org_settings(monkeypatch):
    @dataclass
    class S:
        ORG_SHADOW_ENABLED: bool = True

    monkeypatch.setattr(org, "_settings_org_enabled", lambda *_args, **_kw: True)
    yield S()


def test_shadow_refresh_does_not_mutate_sep(_dummy_org_settings):
    mission = Mission(name="sep", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("open_application")]
    sep_blob: Dict[str, Any] = {
        "steps": [{"type": "open_app"}],
        "source": "unit_test_sep",
        "average_confidence": 0.7,
    }
    mission.semantic_execution_plan = dict(sep_blob)

    before = dict(mission.semantic_execution_plan)
    org.refresh_operational_shadow(mission)
    assert mission.semantic_execution_plan == before


def test_legacy_mission_without_truths_maybe_sep_fallback_only():
    mission = Mission(name="leg", status=MissionStatus.DRAFT)
    mission.truth_blocks = []
    mission.semantic_execution_plan = {"steps": [{"type": "open_app", "id": "z", "confidence": 0.5}]}
    gph = org.build_operational_runtime_graph(mission)
    assert "semantic_execution_plan_fallback" in gph.build_sources_used
    assert len(gph.nodes) == 1


def test_search_browse_continuity_metric():
    mission = Mission(name="cont", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content", readiness=1.0), _tb("browse_results", readiness=1.0)]
    mtr = org.build_operational_runtime_graph(mission).metrics
    assert mtr["execution_continuity"] >= 0.9


def test_form_flow_submit_transition_present():
    mission = Mission(name="form", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("fill_form"), _tb("submit_form")]
    gph = org.build_operational_runtime_graph(mission)
    tx = {(t.from_operational_state, t.to_operational_state) for t in gph.transitions}
    assert ("structured_form_engaged", "structured_form_commit_completed") in tx


def test_arl_event_surfaces_recovery_edge():
    mission = Mission(name="arl", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content")]
    mission.adaptive_runtime_audit = {
        "version": 1,
        "events": [
            {
                "type": "synthetic_fallback",
                "canonical_action": "navigate_recovery",
                "decision": {"decision": "FALLBACK_TO_SEP"},
            },
        ],
    }
    recv = org.build_operational_runtime_graph(mission).recovery_edges
    assert any("arl" in (e.evidence_sources or []) for e in recv)


def test_cob_block_adds_recovery_edge():
    cob = CanonicalOperationalBlock(
        id="cb1",
        canonical_action="search_content",
        block_type="navigation_session",
        repair_strategy={"summary": "retry navigation shell"},
        status=CanonicalOperationalBlockStatus.ACCEPTED,
    )
    mission = Mission(name="cob_m", status=MissionStatus.DRAFT)
    mission.truth_blocks = [_tb("search_content")]
    mission.canonical_operational_blocks = [cob]
    edges = org.build_operational_runtime_graph(mission).recovery_edges
    assert any("cob" in e.evidence_sources[0].lower() for e in edges if e.evidence_sources)


def test_empty_mission_audit_disabled_has_reason():
    @dataclass
    class OFF:
        ORG_SHADOW_ENABLED: bool = False

    mission = Mission(name="dis", status=MissionStatus.DRAFT)
    org.refresh_operational_shadow(mission, OFF())
    audit = getattr(mission, "org_audit", {}) or {}
    assert audit.get("reason") == "ORG_SHADOW_DISABLED"
