"""Tests — Operational Memory & Pattern Consolidation (Fase 1)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.execution_contracts import MissionStep
from app.services.missions.smart_executor import MissionResult, StepOutcome
from app.services.runtime import operational_memory as ompc
from app.services.runtime import strategy_learning_layer as sll


def _ompc_cfg(**overrides):
    base = dict(
        OMPC_ENABLED=True,
        OMPC_MIN_PATTERN_OCCURRENCES=2,
        OMPC_CONFIDENCE_THRESHOLD=0.2,
        OMPC_MAX_PATTERN_MEMORY=5000,
        OMPC_TEMPORAL_DECAY_ENABLED=False,
    )
    base.update(overrides)
    return MagicMock(**base)


@pytest.fixture
def ompc_isolated(tmp_path):
    ompc.reset_operational_memory_store_for_tests(tmp_path / "ompc_test.sqlite")
    ompc.reset_ompc_session_metrics_for_tests()
    yield tmp_path / "ompc_test.sqlite"
    ompc.reset_operational_memory_store_for_tests(tmp_path / "ompc_teardown.sqlite")
    ompc.reset_ompc_session_metrics_for_tests()


def test_recurring_workflow_increases_confidence(ompc_isolated):
    seq = ["sep:open_application", "sep:navigation_session", "sep:search_session"]
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            with patch.object(ompc, "_execution_truth_ok", return_value=True):
                p2 = None
                for i in range(5):
                    p2 = ompc.consolidate_operational_pattern(
                        canonical_sequence=seq,
                        workflow_type="nav_search",
                        mission=None,
                        ended_success=True,
                        duration_ms=100.0 + i,
                    )
                assert p2 is not None
                assert p2.recurrence_count == 5
                assert p2.confidence > 0.45


def test_stable_workflow_raises_stability_score(ompc_isolated):
    seq = ["sep:a", "sep:b", "sep:c"]
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            with patch.object(ompc, "_execution_truth_ok", return_value=True):
                for _ in range(6):
                    ompc.consolidate_operational_pattern(
                        canonical_sequence=seq,
                        ended_success=True,
                        fallbacks=0,
                        arl_recoveries=0,
                        ambiguity_events=0,
                        mission=None,
                    )
                pid = ompc.derive_pattern_id(ompc.derive_pattern_signature(seq))
                p = ompc.get_operational_memory_store().load_profile(pid)
                assert p is not None
                assert p.stability_score >= 0.82


def test_fragile_workflow_raises_fragility_score(ompc_isolated):
    seq = ["sep:erp_login", "sep:dynamic_dashboard"]
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            with patch.object(ompc, "_execution_truth_ok", return_value=True):
                for _ in range(8):
                    ompc.consolidate_operational_pattern(
                        canonical_sequence=seq,
                        ended_success=True,
                        validator_failures=3,
                        layout_shift_events=3,
                        ambiguity_events=0,
                        mission=None,
                    )
                pid = ompc.derive_pattern_id(ompc.derive_pattern_signature(seq))
                p = ompc.get_operational_memory_store().load_profile(pid)
                assert p is not None
                assert p.fragility_score >= 0.52


def test_frequent_recoveries_build_known_recovery_patterns(ompc_isolated):
    seq = ["sep:windows_search", "sep:chrome_focus"]
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            with patch.object(ompc, "_execution_truth_ok", return_value=True):
                for _ in range(4):
                    ompc.consolidate_operational_pattern(
                        canonical_sequence=seq,
                        ended_success=True,
                        arl_recoveries=1,
                        recovery_detail={"key": "semantic_relocate", "via": "arl"},
                        mission=None,
                    )
                pid = ompc.derive_pattern_id(ompc.derive_pattern_signature(seq))
                p = ompc.get_operational_memory_store().load_profile(pid)
                assert p is not None
                keys = [r.get("key") for r in p.known_recovery_patterns]
                assert "semantic_relocate" in keys
                row = next(r for r in p.known_recovery_patterns if r["key"] == "semantic_relocate")
                assert int(row.get("count", 0)) >= 2


def test_recurring_layout_shifts_add_optimization_hints(ompc_isolated):
    seq = ["sep:modal_heavy_step"]
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            with patch.object(ompc, "_execution_truth_ok", return_value=True):
                for _ in range(6):
                    ompc.consolidate_operational_pattern(
                        canonical_sequence=seq,
                        ended_success=True,
                        layout_shift_events=2,
                        mission=None,
                    )
                pid = ompc.derive_pattern_id(ompc.derive_pattern_signature(seq))
                p = ompc.get_operational_memory_store().load_profile(pid)
                assert p is not None
                assert "expect_layout_shift" in p.optimization_hints


def test_predicted_next_patterns_follow_transitions(ompc_isolated):
    seq = ["sep:open_application", "sep:navigation_session", "sep:search_session"]
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            with patch.object(ompc, "_execution_truth_ok", return_value=True):
                for _ in range(4):
                    ompc.consolidate_operational_pattern(
                        canonical_sequence=seq, ended_success=True, mission=None
                    )
    preds = ompc.predict_next_operational_patterns(seq[:2], limit=4)
    tokens = [p["next_token"] for p in preds]
    assert "sep:search_session" in tokens


def test_persistence_reload(ompc_isolated):
    seq = ["cob:open_app:block_a", "sep:tail_step"]
    db = ompc_isolated
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            with patch.object(ompc, "_execution_truth_ok", return_value=True):
                ompc.consolidate_operational_pattern(
                    canonical_sequence=seq, ended_success=True, mission=None
                )
    sig = ompc.derive_pattern_signature(seq)
    pid = ompc.derive_pattern_id(sig)
    ompc.reset_operational_memory_store_for_tests(ompc_isolated.parent / "other.sqlite")
    store2 = ompc.OperationalMemoryStore(db)
    loaded = store2.load_profile(pid)
    assert loaded is not None
    assert loaded.pattern_signature == sig
    assert loaded.recurrence_count >= 1


def test_high_ambiguity_skips_consolidation(ompc_isolated):
    seq = ["sep:x", "sep:y"]
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            out = ompc.consolidate_operational_pattern(
                canonical_sequence=seq,
                ended_success=True,
                ambiguity_events=5,
                mission=None,
            )
            assert out is None

    mission = MagicMock()
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=True):
            out = ompc.consolidate_operational_pattern(
                canonical_sequence=["sep:ok", "sep:z"],
                ended_success=True,
                mission=mission,
            )
            assert out is None


def test_sll_plus_ompc_substring_boost(tmp_path):
    sll_db = tmp_path / "sll.sqlite"
    ompc_db = tmp_path / "ompc2.sqlite"
    sll.reset_strategy_learning_store_for_tests(sll_db)
    ompc.reset_operational_memory_store_for_tests(ompc_db)
    ompc.reset_ompc_session_metrics_for_tests()
    c = sll.StrategyContext(
        step_kind="search_web",
        domain_or_site="example.com",
        app_or_process="chrome.exe",
    )
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=2,
        SLL_CONFIDENCE_THRESHOLD=0.1,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        for _ in range(6):
            sll.record_observation(
                context=c,
                strategy_name="search_web:uia_chain",
                success=True,
                duration_ms=80.0,
                mission=None,
                require_execution_truth_for_success=False,
            )
            sll.record_observation(
                context=c,
                strategy_name="search_web:dom_deep",
                success=True,
                duration_ms=80.0,
                mission=None,
                require_execution_truth_for_success=False,
            )
        ranked_plain, _ = sll.rank_strategies_with_learning(
            ["search_web:uia_chain", "search_web:dom_deep"],
            c,
            learning=None,
            execution_truth_confirmed=True,
            sep_alignment_score=0.5,
            ompc_substring_boosts=None,
        )
        ranked_boost, expl = sll.rank_strategies_with_learning(
            ["search_web:uia_chain", "search_web:dom_deep"],
            c,
            learning=None,
            execution_truth_confirmed=True,
            sep_alignment_score=0.5,
            ompc_substring_boosts={"dom": 0.04},
        )
        assert ranked_plain != ranked_boost or any(
            "ompc_substring_boost" in " ".join(expl[s].reasons) for s in ranked_boost
        )
        assert ranked_boost[0] == "search_web:dom_deep"


def test_arl_outcomes_feed_operational_memory(ompc_isolated):
    mission = MagicMock()
    mission.canonical_operational_blocks = []
    mission.semantic_sessions = []
    mission.semantic_shadow_audit = None
    mission.adaptive_runtime_audit = {
        "events": [
            {
                "type": "arl_adapt",
                "bundle": {
                    "deviation": {
                        "deviation_types": ["layout_shift"],
                        "deviation_detected": True,
                    },
                    "decision": {"decision": "CONTINUE", "rationale": "relocate"},
                },
            },
        ],
    }
    steps = [
        MissionStep(id="1", kind="open_application", params={}),
        MissionStep(id="2", kind="navigation_session", params={}),
    ]
    result = MissionResult(
        run_id="r1",
        status="success",
        outcomes=[
            StepOutcome("1", "open_application", "success", retries=0),
            StepOutcome("2", "navigation_session", "success", retries=0),
        ],
    )
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            with patch.object(ompc, "_execution_truth_ok", return_value=True):
                ompc.observe_smart_mission_execution(mission, steps, result)
    profs = ompc.get_operational_memory_store().all_profiles()
    assert profs
    p = profs[0]
    assert p.arl_recovery_rate > 0 or "arl_pre_arm" in p.optimization_hints


def test_cob_sequence_signatures_stable(ompc_isolated):
    mission = MagicMock()
    cob = MagicMock()
    cob.canonical_action = "open_application"
    cob.block_type = "launch"
    mission.canonical_operational_blocks = [cob]
    seq = ompc.build_canonical_sequence_from_mission(
        mission, step_kinds_executed=["search_session"]
    )
    s1 = ompc.derive_pattern_signature(seq)
    s2 = ompc.derive_pattern_signature(list(seq))
    assert s1 == s2
    assert len(s1) == 40


def test_runtime_hints_explainable_in_audit(ompc_isolated):
    seq = ["sep:open_application", "sep:web:youtube_search"]
    with patch.object(ompc, "_settings", return_value=_ompc_cfg()):
        with patch.object(ompc, "_ambiguity_blocked", return_value=False):
            with patch.object(ompc, "_execution_truth_ok", return_value=True):
                ompc.consolidate_operational_pattern(
                    canonical_sequence=seq,
                    ended_success=True,
                    mission=None,
                )
    audit = ompc.build_operational_memory_audit(mission=None, top_n=4)
    assert audit["top_patterns"]
    assert any(
        isinstance(p.get("hints"), list) and p["hints"]
        for p in audit["top_patterns"]
    )
    preds = ompc.predict_next_operational_patterns(seq[:1], limit=2)
    for row in preds:
        assert "explain" in row
        assert "probability" in row


def test_ompc_disabled_no_side_effects(ompc_isolated):
    seq = ["sep:only"]
    with patch.object(ompc, "_settings", return_value=_ompc_cfg(OMPC_ENABLED=False)):
        assert (
            ompc.consolidate_operational_pattern(
                canonical_sequence=seq, ended_success=True, mission=None
            )
            is None
        )
        mission = MagicMock()
        mission.canonical_operational_blocks = []
        mission.semantic_sessions = []
        mission.semantic_shadow_audit = None
        mission.adaptive_runtime_audit = {}
        steps = [MissionStep(id="1", kind="only", params={})]
        result = MissionResult(
            run_id="r",
            status="success",
            outcomes=[StepOutcome("1", "only", "success")],
        )
        ompc.observe_smart_mission_execution(mission, steps, result)
        ompc.observe_cob_pilot_chain(
            mission,
            MagicMock(canonical_action="x", block_type="y"),
            ok=True,
            elapsed_ms=10.0,
            fell_back=False,
        )
    assert ompc.get_operational_memory_store().all_profiles() == []


def test_predict_recovery_path_explainable(ompc_isolated):
    p = ompc.OperationalPatternProfile(
        pattern_id="pat_x",
        pattern_signature="sig",
        known_recovery_patterns=[
            {"key": "retry_search", "count": 3, "detail": {"step": 1}},
        ],
    )
    rows = ompc.predict_recovery_path(p, limit=2)
    assert rows and rows[0].get("explain")


def test_classify_stability_monotone(ompc_isolated):
    hi = ompc.classify_pattern_stability(0.95, 0.0, 0.0, 0.0)
    lo = ompc.classify_pattern_stability(0.95, 0.6, 0.5, 0.5)
    assert hi > lo
