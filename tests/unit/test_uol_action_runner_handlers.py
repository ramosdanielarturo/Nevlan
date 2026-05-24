"""Tests — UOL-native ActionRunner handlers."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import (
    CanonicalOperationalIntent,
    Mission,
    OperationalFreezeEntityCandidate,
    OperationalFreezeSnapshot,
)
from app.services.missions.action_runner import ActionRunner, StrategyResult
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.freeze_first_execution_core import promote_freeze_to_canonical_intent
from app.services.missions.state_detector import StateSnapshot
from app.services.missions import uol_action_handlers as uol_h
from app.services.missions.universal_operational_language import (
    enrich_mission_step_with_uol,
    resolve_uol_runtime_strategies,
)


def _state(**kw) -> StateSnapshot:
    return StateSnapshot(**{**dict(
        active_app="chrome.exe",
        active_window_title="YouTube",
        browser_url="https://www.youtube.com/",
        browser_domain="youtube.com",
        visible_text="Devuélveme el amor Luis Miguel results",
    ), **kw})


def _coi_mission(name: str = "Daniel Chrome") -> Mission:
    freeze = OperationalFreezeSnapshot(
        pre_transition_confirmed=True,
        freeze_confidence=0.95,
        best_entity_candidate=OperationalFreezeEntityCandidate(
            display_name=name,
            normalized_name="daniel",
            entity_type_hint="user_profile",
            confidence=0.95,
        ),
    )
    coi = promote_freeze_to_canonical_intent(freeze, source_event_id="ev1")
    assert coi is not None
    return Mission(
        id=str(uuid.uuid4()),
        name="uol-test",
        canonical_operational_intents=[coi],
    )


# ── 1. select_entity uses UCES/ERL before legacy ─────────────────────────────


def test_select_entity_uol_delegates_entity_first(monkeypatch):
    calls = []

    class _Hook:
        executed = True
        strategy_result = StrategyResult(ok=True, strategy="uces:test", message="ok")
        strategy_used = "uces:exact"
        blocked_ambiguity = False
        blocked_unsafe = False

    def _fake_try(*a, **k):
        calls.append("entity_first")
        return _Hook()

    monkeypatch.setattr(
        "app.services.runtime.entity_runtime_resolution.try_entity_first_runtime",
        _fake_try,
    )

    step = MissionStep(
        id="s1",
        kind="select_entity_from_collection",
        params={
            "uol_target_display_name": "Daniel Chrome",
            "collection_entity": {"display_name": "Daniel Chrome"},
            "candidate_count": 1,
            "canonical_truth": True,
        },
    )
    res = uol_h.uol_select_entity_from_collection(step, _state(), mission=_coi_mission())
    assert res.ok
    assert calls == ["entity_first"]
    assert res.strategy == "select_entity_from_collection:uol_entity"


def test_select_entity_ambiguous_asks_human():
    step = MissionStep(
        id="s1",
        kind="select_entity_from_collection",
        params={"uol_target_display_name": "X", "candidate_count": 3},
    )
    res = uol_h.uol_select_entity_from_collection(step, _state())
    assert not res.ok
    assert res.extra and res.extra.get("ask_human")


# ── 2. search_content focus/type/submit ──────────────────────────────────────


def test_search_content_uol_dom_path(monkeypatch):
    monkeypatch.setattr(uol_h, "_get_playwright_page", lambda **k: None)
    monkeypatch.setattr(
        uol_h.ar,
        "_search_youtube_dom",
        lambda st, stt: StrategyResult(ok=True, strategy="yt", message="dom"),
    )
    monkeypatch.setattr(uol_h, "_validate_search_outcome", lambda st, stt: True)

    step = MissionStep(
        id="s2",
        kind="search_content",
        params={
            "uol_input_text": "Devuélveme el amor Luis Miguel",
            "query": "Devuélveme el amor Luis Miguel",
            "site": "youtube",
        },
    )
    res = uol_h.uol_search_content_surface(step, _state())
    assert res.ok
    assert res.strategy == "search_content:uol_surface"


def test_search_content_uol_omnibox_fallback(monkeypatch):
    monkeypatch.setattr(uol_h, "_get_playwright_page", lambda **k: None)
    monkeypatch.setattr(
        uol_h.ar,
        "_search_youtube_dom",
        lambda st, stt: StrategyResult(ok=False, strategy="yt", message="no dom"),
    )
    monkeypatch.setattr(
        uol_h.ar,
        "_search_web_address_bar",
        lambda st, stt: StrategyResult(ok=True, strategy="web", message="bar"),
    )

    step = MissionStep(
        id="s2",
        kind="search_content",
        params={"query": "test query"},
    )
    res = uol_h.uol_search_content_surface(step, _state())
    assert res.ok


# ── 3. open_tab Ctrl+T ───────────────────────────────────────────────────────


def test_open_tab_uol_hotkey(monkeypatch):
    monkeypatch.setattr(uol_h, "_hotkey", lambda *k: True)
    monkeypatch.setattr(uol_h.ar, "_open_new_tab_hotkey", lambda st, stt: StrategyResult(ok=True, strategy="hk", message="ok"))
    monkeypatch.setattr(uol_h, "_validate_tab_opened", lambda a, b: True)

    step = MissionStep(id="s3", kind="open_new_tab", params={})
    res = uol_h.uol_open_tab_hotkey(step, _state())
    assert res.ok
    assert res.strategy == "open_tab:uol_hotkey"


# ── 4. navigate Ctrl+L ───────────────────────────────────────────────────────


def test_navigate_uol_address_surface(monkeypatch):
    monkeypatch.setattr(
        uol_h.ar,
        "_open_url_hotkey",
        lambda st, stt: StrategyResult(ok=True, strategy="url", message="nav"),
    )
    monkeypatch.setattr(uol_h, "_validate_location", lambda st, stt: True)

    step = MissionStep(
        id="s4",
        kind="open_url",
        params={"uol_location_hint": "youtube", "url": "https://www.youtube.com/"},
    )
    res = uol_h.uol_navigate_to_location_address(step, _state())
    assert res.ok
    assert res.strategy == "navigate_to_location:uol_address_surface"


# ── 5. browse_results scroll ─────────────────────────────────────────────────


def test_browse_results_scroll(monkeypatch):
    monkeypatch.setattr(
        uol_h.ar,
        "_scroll_results_keyboard",
        lambda st, stt: StrategyResult(ok=True, strategy="kb", message="scroll"),
    )

    step = MissionStep(id="s5", kind="scroll_results", params={"direction": "down"})
    res = uol_h.uol_browse_results_scroll(step, _state())
    assert res.ok
    assert res.strategy == "browse_results:uol_scroll_surface"


# ── 6. no coords primary ─────────────────────────────────────────────────────


def test_filter_coords_primary_moves_to_end():
    strategies = [
        "select_entity_from_collection:uol_entity",
        "select_profile:relative_coords",
        "select_profile:uia_text_match",
    ]
    out = uol_h.filter_coords_primary_strategies(strategies)
    assert out[0] == "select_entity_from_collection:uol_entity"
    assert "relative_coords" in out[-1]


def test_resolve_uol_skips_smart_route_when_canonical():
    step = MissionStep(
        id="s",
        kind="search_content",
        params={
            "uol_action": "search_content",
            "uol_canonical_truth": True,
            "preferred_strategy": "search_content:uol_surface",
        },
    )
    legacy = ["search_content:uol_surface", "search_content:smart_route", "search_youtube:dom_input"]
    out = resolve_uol_runtime_strategies(step, legacy)
    assert out[0] == "search_content:uol_surface"
    assert "smart_route" not in out


# ── 7. ActionRunner dispatches UOL ─────────────────────────────────────────────


def test_action_runner_uol_strategy_registered():
    from app.services.missions.action_runner import supported_strategies

    names = supported_strategies()
    assert "search_content:uol_surface" in names
    assert "open_application:uol_launcher" in names
    assert "navigate_to_location:uol_address_surface" in names


def test_action_runner_uol_with_mission(monkeypatch):
    mission = _coi_mission()

    class _Hook:
        executed = True
        strategy_result = StrategyResult(ok=True, strategy="x", message="ok")
        strategy_used = "uces:x"
        blocked_ambiguity = False
        blocked_unsafe = False

    monkeypatch.setattr(
        "app.services.runtime.entity_runtime_resolution.try_entity_first_runtime",
        lambda *a, **k: _Hook(),
    )

    step = enrich_mission_step_with_uol(
        MissionStep(
            id="s",
            kind="select_entity_from_collection",
            params={"collection_entity": {"display_name": "Daniel Chrome"}, "candidate_count": 1},
        ),
    )
    runner = ActionRunner(mission_ref=mission)
    res = runner.run_strategy("select_entity_from_collection:uol_entity", step, _state())
    assert res.ok


# ── 8. Chrome flow without smart_route primary ───────────────────────────────


def test_chrome_youtube_flow_uol_strategies():
    steps = [
        enrich_mission_step_with_uol(
            MissionStep(id="1", kind="open_app", params={"name": "chrome"}),
        ),
        enrich_mission_step_with_uol(
            MissionStep(
                id="2",
                kind="select_entity_from_collection",
                params={
                    "collection_entity": {"display_name": "Daniel Chrome", "entity_type": "user_profile"},
                    "candidate_count": 1,
                    "canonical_truth": True,
                },
            ),
        ),
        enrich_mission_step_with_uol(MissionStep(id="3", kind="open_new_tab", params={})),
        enrich_mission_step_with_uol(
            MissionStep(id="4", kind="open_url", params={"alias": "youtube", "url": "https://youtube.com"}),
        ),
        enrich_mission_step_with_uol(
            MissionStep(
                id="5",
                kind="search_content",
                params={"query": "Devuélveme el amor Luis Miguel", "site": "youtube"},
            ),
        ),
        enrich_mission_step_with_uol(
            MissionStep(id="6", kind="scroll_results", params={"direction": "down"}),
        ),
    ]
    prefs = [s.params.get("preferred_strategy", "") for s in steps]
    assert prefs[0] == "open_application:uol_launcher"
    assert prefs[2] == "open_tab:uol_hotkey"
    assert prefs[4] == "search_content:uol_surface"
    assert prefs[5] == "browse_results:uol_scroll_surface"
    assert all("smart_route" not in (p or "") for p in prefs)


# ── 9. Mission Review stays clean ─────────────────────────────────────────────


def test_mission_review_stays_human_after_uol_enrich():
    from app.services.missions.mission_review_summary import build_mission_review_summary

    mission = Mission(
        id=str(uuid.uuid4()),
        name="flow",
        semantic_execution_plan={
            "intent_status": "READY",
            "ready_provenance": "raw_evidence",
            "steps": [
                {
                    "id": "s1",
                    "type": "search_content",
                    "params": {"query": "Devuélveme el amor Luis Miguel"},
                    "human_label": "search_content:smart_route",
                },
            ],
        },
    )
    summary = build_mission_review_summary(mission, recompute=False)
    assert summary.steps
    assert "smart_route" not in summary.steps[0].human_label.lower()
    assert "Devuélveme el amor Luis Miguel" in summary.steps[0].human_label
