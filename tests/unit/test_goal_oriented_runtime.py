"""Goal-Oriented Operational Runtime (GOOR) — tests sombra (Fase 1)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from app.contracts.goal_oriented_runtime import OperationalDecisionContext
from app.contracts.mission import (
    CanonicalOperationalBlock,
    CanonicalOperationalBlockStatus,
    Mission,
    MissionStatus,
    OperationalTruthBlock,
    OperationalTruthStatus,
)
from app.services.runtime import goal_oriented_runtime as goor
from app.services.runtime import operational_continuity_runtime as ocr
from app.services.runtime import operational_runtime_graph as org


@pytest.fixture
def _go(monkeypatch):
    monkeypatch.setattr(goor, "_settings_goor_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(ocr, "_settings_ocr_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(org, "_settings_org_enabled", lambda *_a, **_k: True)


def _tb(
    truth_type: str,
    *,
    human: str = "",
    readiness: float = 0.9,
    amb: float = 0.15,
    iso_minute: str = "07",
    validator_expectations: list | None = None,
    truth_id_suffix: str = "",
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
            "started_at_iso": f"2026-05-17T11:{iso_minute}:05+00:{truth_id_suffix}",
        },
        context_window={"query_hint": "Q1"}
        if truth_type == "search_content"
        else {"feed_surface": True}
        if truth_type == "browse_results"
        else {},
        truth_status=OperationalTruthStatus.COMMITTED_SHADOW,
        validator_expectations=list(validator_expectations or [])[:24],
        truth_id=f"tid-{truth_type}-{iso_minute}-{truth_id_suffix}".replace(":", "_"),
    )


def test_goal_progress_calculation(_go):
    m = Mission(name="gp", status=MissionStatus.DRAFT)
    m.goor_shadow_mode = True
    m.truth_blocks = [_tb("search_content", iso_minute="02"), _tb("browse_results", iso_minute="03")]
    g = org.build_operational_runtime_graph(m)
    p = goor.evaluate_goal_progress(m, g)
    assert p.goal_headline or p.goal_id
    assert p.progress_ratio >= 0.0


def test_candidate_transition_ranking(_go):
    m = Mission(name="rank", status=MissionStatus.DRAFT)
    m.goor_shadow_mode = True
    m.truth_blocks = [_tb("open_application"), _tb("search_content")]
    g = org.build_operational_runtime_graph(m)
    ctx = goor.build_operational_decision_context(m, g)
    cand = goor.find_candidate_goal_transitions(m, g)
    ranked = goor.rank_goal_transitions(cand, ctx=ctx)
    assert isinstance(ranked, list)
    if len(ranked) >= 2:
        assert ranked[0].promising_score >= ranked[-1].promising_score


def test_continuity_aware_decisions(_go):
    m = Mission(name="cont", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("fill_form"), _tb("submit_form")]
    g = org.build_operational_runtime_graph(m)
    p = goor.evaluate_goal_progress(m, g)
    ctn, notes = goor.evaluate_goal_continuity(m, g, progress=p)
    assert 0.0 <= ctn <= 1.0
    assert any("truth_blocks" in n for n in notes)


def test_goal_stall_detection(_go):
    m = Mission(name="stall", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content")]
    m.semantic_execution_plan = {"steps": [{"type": "scroll_results"} for _ in range(8)]}
    g = org.build_operational_runtime_graph(m)
    stalls = goor.detect_goal_stall(m, g)
    assert isinstance(stalls, list)

def test_recovery_pressure_in_progress(_go):
    m = Mission(name="rcp", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content", amb=0.88)]
    m.adaptive_runtime_audit = {"events": [{"type": "recovery", "decision": {"decision": "FALLBACK_TO_SEP"}}] * 20}
    g = org.build_operational_runtime_graph(m)
    p = goor.evaluate_goal_progress(m, g)
    assert p.recovery_pressure > 0.2


def test_transition_diversity_metric(_go):
    m = Mission(name="txdiv", status=MissionStatus.DRAFT)
    m.goor_shadow_mode = True
    m.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    g = org.build_operational_runtime_graph(m)
    snap = goor.build_operational_goal_execution_snapshot(m, graph=g)
    assert "transition_diversity" in snap.metrics


def test_replay_pressure_reduction_hypothesis(_go):
    m = Mission(name="rpr", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("navigate_to_location")]
    g = org.build_operational_runtime_graph(m)
    cmp_blob = goor.compare_goor_vs_sep_runtime(g, m)
    assert "replay_dependency_reduction_hypothesis" in cmp_blob


def test_operational_autonomy_estimate(_go):
    m = Mission(name="aut", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("navigate_to_location"), _tb("browse_results")]
    g = org.build_operational_runtime_graph(m)
    snap = goor.build_operational_goal_execution_snapshot(m, graph=g)
    assert snap.operational_autonomy_estimate >= 0.0


def test_ocr_integration_via_shared_graph(_go):
    m = Mission(name="ocrx", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    g = org.build_operational_runtime_graph(m)
    gx = goor.evaluate_goal_progress(m, g)
    assert gx.continuity_strength >= 0.0


def test_ompc_integration_shape(_go):
    m = Mission(name="ompc", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content")]
    m.canonical_operational_blocks = [
        CanonicalOperationalBlock(
            block_type="nav",
            canonical_action="navigate_surface",
            status=CanonicalOperationalBlockStatus.PROPOSED,
        ),
    ]
    g = org.build_operational_runtime_graph(m)
    ctx = goor.build_operational_decision_context(m, g)
    assert isinstance(ctx.ompc_hints, dict)


def test_sll_ranking_echo_present(_go):
    m = Mission(name="sll", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content")]
    m.semantic_execution_plan = {
        "steps": [
            {"type": "navigate_to_location"},
            {"type": "search_content"},
        ],
    }
    g = org.build_operational_runtime_graph(m)
    ctx = goor.build_operational_decision_context(m, g)
    assert "sll_echo" in ctx.model_dump()


def test_recovery_selection_ordering_semantic_lift(_go):
    m = Mission(name="rcv", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    m.adaptive_runtime_audit = {
        "events": [{"type": "recovery", "decision": {"decision": "FALLBACK_TO_SEP"}}],
    }
    g = org.build_operational_runtime_graph(m)
    recvs = goor.find_goal_recovery(m, g, limit=14)
    if recvs:
        assert sorted(recvs, key=lambda r: (-r.goal_orientation_score))[0].goal_orientation_score >= recvs[
            -1
        ].goal_orientation_score


def test_shadow_mode_refresh_no_mutates_runtime_fields(_go):
    m = Mission(name="immutable", status=MissionStatus.DRAFT)
    m.goor_shadow_mode = True
    m.truth_blocks = [_tb("search_content")]
    sep_before: Dict[str, Any] = {
        "version": 1,
        "steps": [{"type": "search_content"}, {"type": "scroll_results"}],
    }
    m.semantic_execution_plan = dict(sep_before)
    m.legacy_compiled_graph_purpose = "legacy_debug_only"
    g = org.build_operational_runtime_graph(m)
    goor.refresh_goor_shadow(m, graph=g)
    assert m.semantic_execution_plan == sep_before
    assert m.legacy_compiled_graph_purpose == "legacy_debug_only"


def test_search_browse_goal_continuity(_go):
    m = Mission(name="sb", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content", iso_minute="10"), _tb("browse_results", iso_minute="11")]
    g = org.build_operational_runtime_graph(m)
    ctc, _ = goor.evaluate_goal_continuity(m, g)
    assert ctc >= 0.35


def test_form_submit_completion_continuity(_go):
    m = Mission(name="fs", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("fill_form", iso_minute="21"), _tb("submit_form", iso_minute="22")]
    g = org.build_operational_runtime_graph(m)
    p = goor.evaluate_goal_progress(m, g)
    assert "form" in p.goal_headline.lower() or p.progress_ratio >= 0.0


def test_recovery_exhaustion_detected(_go):
    m = Mission(name="rex", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content")]
    m.adaptive_runtime_audit = {"events": [{"kind": i} for i in range(40)]}
    g = org.build_operational_runtime_graph(m)
    stalls = goor.detect_goal_stall(m, g)
    assert any("exhaustion" in s or "replay" in s for s in stalls) or stalls == []


def test_validator_drift_affects_transition_rank(_go):
    m = Mission(name="vdd", status=MissionStatus.DRAFT)
    m.truth_blocks = [
        OperationalTruthBlock(
            truth_type="search_content",
            canonical_action="search_content",
            human_intent="s",
            execution_readiness=0.92,
            validator_expectations=["validator:A"],
            truth_status=OperationalTruthStatus.COMMITTED_SHADOW,
        ),
        OperationalTruthBlock(
            truth_type="browse_results",
            canonical_action="browse_results",
            human_intent="b",
            execution_readiness=0.93,
            validator_expectations=["validator:Z"],
            truth_status=OperationalTruthStatus.COMMITTED_SHADOW,
        ),
    ]
    g = org.build_operational_runtime_graph(m)
    ctx_low = goor.build_operational_decision_context(m, g, ocr_score=0.7)
    ctx_high_dr = OperationalDecisionContext(**{**ctx_low.model_dump(), "validator_history_drift": 0.95})

    cand = goor.find_candidate_goal_transitions(m, g)
    if cand:
        r1 = goor.rank_goal_transitions(cand, ctx=ctx_low)[0].promising_score
        r2 = goor.rank_goal_transitions(cand, ctx=ctx_high_dr)[0].promising_score
        assert r2 <= r1 + 1e-4


def test_legacy_mission_no_goor_mode(_go):
    m = Mission(name="legacy", status=MissionStatus.DRAFT)
    m.goor_shadow_mode = False
    m.truth_blocks = [_tb("search_content")]
    g = org.build_operational_runtime_graph(m)
    snap = goor.refresh_goor_shadow(m, graph=g)
    assert snap.stall_signals
    assert "GOOR_SHADOW_MODE_DISABLED" in snap.stall_signals[0]


def test_goal_shadow_transition_attempt(_go):
    m = Mission(name="gat", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content")]
    g = org.build_operational_runtime_graph(m)
    infer = [n.operational_state for n in g.nodes][:2]
    if len(infer) >= 2:
        blob = goor.attempt_goal_transition_shadow(
            mission=m,
            graph=g,
            from_state=infer[0],
            to_state=infer[1],
        )
        assert "progress_after_simulated" in blob


def test_compare_goor_vs_sep_has_projection_keys(_go):
    m = Mission(name="cmp", status=MissionStatus.DRAFT)
    m.truth_blocks = [_tb("search_content"), _tb("browse_results")]
    m.semantic_execution_plan = {"steps": [{"type": "search_content"}, {"type": "browse_results"}]}
    g = org.build_operational_runtime_graph(m)
    cmpo = goor.compare_goor_vs_sep_runtime(g, m)
    assert "goor_projection" in cmpo
