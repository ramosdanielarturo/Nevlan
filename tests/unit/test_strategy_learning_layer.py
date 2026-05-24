"""Tests — Strategy Learning Layer (Fase 1)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.execution_learning import LearningLogger
from app.services.missions.execution_contracts import MissionStep
from app.services.runtime import strategy_learning_layer as sll


@pytest.fixture
def tmp_store(tmp_path):
    db = tmp_path / "sll_test.sqlite"
    store = sll.reset_strategy_learning_store_for_tests(db)
    yield store
    sll.reset_strategy_learning_store_for_tests(tmp_path / "sll_reset.sqlite")


def _ctx(kind: str = "open_url") -> sll.StrategyContext:
    return sll.StrategyContext(
        step_kind=kind,
        domain_or_site="youtube.com",
        app_or_process="chrome.exe",
    )


def test_higher_success_rate_ranks_higher(tmp_store):
    c = _ctx()
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=3,
        SLL_CONFIDENCE_THRESHOLD=0.2,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        for _ in range(8):
            sll.record_observation(
                context=c,
                strategy_name="a:slow",
                success=True,
                duration_ms=400.0,
                mission=None,
                require_execution_truth_for_success=False,
            )
        for _ in range(8):
            sll.record_observation(
                context=c,
                strategy_name="b:fast",
                success=False,
                duration_ms=50.0,
                mission=None,
                require_execution_truth_for_success=False,
            )
        ranked, expl = sll.rank_strategies_with_learning(
            ["b:fast", "a:slow"],
            c,
            learning=None,
            execution_truth_confirmed=True,
            sep_alignment_score=0.9,
        )
        assert ranked[0] == "a:slow"
        assert "learned_success_rate" in expl[ranked[0]].reasons[1]


def test_slow_strategy_penalizes_ranking(tmp_store):
    c = _ctx()
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=2,
        SLL_CONFIDENCE_THRESHOLD=0.1,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        for _ in range(10):
            sll.record_observation(
                context=c, strategy_name="fast",
                success=True, duration_ms=40.0, mission=None,
                require_execution_truth_for_success=False,
            )
        for _ in range(10):
            sll.record_observation(
                context=c, strategy_name="slow",
                success=True, duration_ms=9000.0, mission=None,
                require_execution_truth_for_success=False,
            )
        ranked, _ = sll.rank_strategies_with_learning(
            ["slow", "fast"], c, learning=None,
            execution_truth_confirmed=True, sep_alignment_score=0.5,
        )
        assert ranked[0] == "fast"


def test_many_fallbacks_lower_priority(tmp_store):
    c = _ctx()
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=2,
        SLL_CONFIDENCE_THRESHOLD=0.1,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        for _ in range(12):
            sll.record_observation(
                context=c, strategy_name="clean",
                success=True, duration_ms=80.0, mission=None,
                fallback_to_sep=False,
                require_execution_truth_for_success=False,
            )
        for _ in range(12):
            sll.record_observation(
                context=c, strategy_name="noisy",
                success=True, duration_ms=80.0, mission=None,
                fallback_to_sep=True,
                require_execution_truth_for_success=False,
            )
        ranked, _ = sll.rank_strategies_with_learning(
            ["noisy", "clean"], c, learning=None,
            execution_truth_confirmed=True, sep_alignment_score=0.5,
        )
        assert ranked[0] == "clean"


def test_arl_recoveries_boost_score(tmp_store):
    c = _ctx()
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=2,
        SLL_CONFIDENCE_THRESHOLD=0.1,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        for _ in range(10):
            sll.record_observation(
                context=c, strategy_name="plain",
                success=True, duration_ms=100.0, mission=None,
                arl_recovery=False,
                require_execution_truth_for_success=False,
            )
        for _ in range(10):
            sll.record_observation(
                context=c, strategy_name="arl_ok",
                success=True, duration_ms=100.0, mission=None,
                arl_recovery=True,
                require_execution_truth_for_success=False,
            )
        ranked, _ = sll.rank_strategies_with_learning(
            ["plain", "arl_ok"], c, learning=None,
            execution_truth_confirmed=True, sep_alignment_score=0.5,
        )
        assert ranked[0] == "arl_ok"


def test_few_attempts_do_not_dominate(tmp_store):
    c = _ctx()
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=8,
        SLL_CONFIDENCE_THRESHOLD=0.35,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        sll.record_observation(
            context=c, strategy_name="lucky",
            success=True, duration_ms=10.0, mission=None,
            require_execution_truth_for_success=False,
        )
        for _ in range(4):
            sll.record_observation(
                context=c, strategy_name="honest",
                success=False, duration_ms=10.0, mission=None,
                require_execution_truth_for_success=False,
            )
        ranked, expl = sll.rank_strategies_with_learning(
            ["lucky", "honest"], c, learning=None,
            execution_truth_confirmed=True, sep_alignment_score=0.5,
        )
        assert expl["lucky"].metrics.get("attempts", 0) < 8


def test_ambiguous_mission_skips_learning(tmp_store):
    c = _ctx()
    mission = MagicMock()
    mission.semantic_shadow_audit = {"rows": []}
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=1,
        SLL_CONFIDENCE_THRESHOLD=0.0,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        with patch.object(
            sll,
            "_ambiguity_blocked",
            return_value=True,
        ):
            out = sll.record_observation(
                context=c,
                strategy_name="x",
                success=True,
                mission=mission,
                require_execution_truth_for_success=False,
            )
            assert out is None


def test_persistence_reload(tmp_path):
    db = tmp_path / "p.sqlite"
    sll.reset_strategy_learning_store_for_tests(db)
    c = _ctx()
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=1,
        SLL_CONFIDENCE_THRESHOLD=0.0,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        sll.record_observation(
            context=c, strategy_name="persisted",
            success=True, mission=None,
            require_execution_truth_for_success=False,
        )
    sll.reset_strategy_learning_store_for_tests(tmp_path / "other.sqlite")
    store2 = sll.StrategyLearningStore(db)
    key = sll.profile_key_for(c, "persisted")
    loaded = store2.load_profile(key)
    assert loaded is not None
    assert loaded.successes >= 1


def test_ranking_explainable(tmp_store):
    c = _ctx()
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=1,
        SLL_CONFIDENCE_THRESHOLD=0.0,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        _, expl = sll.rank_strategies_with_learning(
            ["s1", "s2"], c, learning=None,
            execution_truth_confirmed=True, sep_alignment_score=0.8,
        )
        lines = sll.explanations_as_human_summary(expl, top_n=2)
        assert any("score=" in ln for ln in lines)


def test_sll_off_delegates_to_learning_logger(tmp_store):
    c = _ctx()
    learn = LearningLogger()
    with patch.object(sll, "_settings", return_value=MagicMock(SLL_ENABLED=False)):
        learn.record_success("r", "open_url", "st", "s2", duration_ms=10)
        learn.record_failure("r", "open_url", "st", "s1", error="x", duration_ms=10)
        ranked, expl = sll.rank_strategies_with_learning(
            ["s1", "s2"], c, learning=learn,
            execution_truth_confirmed=True, sep_alignment_score=0.0,
        )
        assert ranked[0] == "s2"
        assert "SLL desactivado" in expl[ranked[0]].reasons[0]


def test_smart_executor_uses_sll_ranking(monkeypatch, tmp_store):
    monkeypatch.setattr(sll, "_settings", lambda: MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=2,
        SLL_CONFIDENCE_THRESHOLD=0.1,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    ))
    step = MissionStep(
        id="1",
        kind="search_youtube",
        params={"query": "q"},
    )
    before = {"browser_domain": "youtube.com", "active_app": "chrome.exe"}
    c = sll.build_context_from_semantic_step(step, None, before)
    for _ in range(6):
        sll.record_observation(
            context=c, strategy_name="search_youtube:dom_input",
            success=True, duration_ms=50.0, mission=None,
            require_execution_truth_for_success=False,
        )
    for _ in range(6):
        sll.record_observation(
            context=c, strategy_name="search_youtube:uia_searchbox",
            success=False, duration_ms=50.0, mission=None,
            require_execution_truth_for_success=False,
        )
    from app.services.missions.execution_contracts import build_contract
    from app.services.runtime.strategy_learning_layer import (
        rank_strategies_with_learning,
    )

    contract = build_contract(step)
    strat_list = list(contract.all_strategies())
    ranked, _ = rank_strategies_with_learning(
        strat_list, c, learning=LearningLogger(),
        execution_truth_confirmed=True, sep_alignment_score=0.7,
    )
    assert ranked[0] == "search_youtube:dom_input"


def test_cob_pilot_observation_recorded(tmp_path):
    db = tmp_path / "cob.sqlite"
    sll.reset_strategy_learning_store_for_tests(db)
    mission = MagicMock()
    cob = MagicMock()
    cob.id = "cob1"
    cob.canonical_action = "open_application"
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=1,
        SLL_CONFIDENCE_THRESHOLD=0.0,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        sll.record_cob_pilot_observation(
            mission, cob, ok=True, elapsed_ms=120.0, fell_back=False,
        )
    profiles = sll.get_strategy_learning_store().all_profiles()
    assert any(p.strategy_name == "cob_pilot:open_application" for p in profiles)


def test_failed_mission_without_truth_skips_success_updates(tmp_store):
    c = _ctx()
    mission = MagicMock()
    with patch.object(sll, "_settings", return_value=MagicMock(
        SLL_ENABLED=True,
        SLL_MIN_ATTEMPTS=1,
        SLL_CONFIDENCE_THRESHOLD=0.0,
        SLL_MAX_PROFILE_SIZE=5000,
        SLL_DECAY_ENABLED=False,
    )):
        with patch.object(sll, "_execution_truth_ok", return_value=False):
            out = sll.record_observation(
                context=c, strategy_name="x",
                success=True, mission=mission,
                require_execution_truth_for_success=True,
            )
            assert out is None
