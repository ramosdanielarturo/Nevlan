"""Tests — Semantic-first recorder (shadow) + comparador SEP."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    Mission,
    MissionStatus,
    MouseAction,
    RawEvent,
    SemanticHypothesis,
    SemanticHypothesisStatus,
    WindowContext,
    mission_from_dict,
)
from app.services.missions.recorder_semantic_shadow import (
    build_click_hypotheses,
    build_hotkey_hypotheses,
    build_keyboard_type_text_hypotheses,
    emit_for_raw_event,
    mission_shadow_active,
)
from app.services.missions.semantic_shadow_compare import compare_shadow_to_sep


def _win_click(meta: Dict[str, Any], *, proc: str = "searchhost.exe") -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_CLICK,
        timestamp=datetime.now(timezone.utc),
        mouse_action=MouseAction(x=10, y=20, button="left"),
        window_context=WindowContext(
            hwnd=1, title="Search", process_name=proc,
        ),
        metadata=meta,
    )


class TestClickHypotheses:
    def test_windows_search_edit_focus_and_launcher_hint(self):
        ev = _win_click({
            "uia": {
                "name": "Escribe aquí para buscar",
                "automation_id": "SearchTextBox",
                "control_type": "EditControl",
            },
            "target_identity_isolation": {"trusted_pre_action_identity": True},
        })
        hs = build_click_hypotheses(ev)
        types = {h.candidate_intent_type for h in hs}
        assert "focus_input_field" in types
        assert "open_app_candidate" in types

    def test_type_text_launcher_opens_app_candidate(self):
        ev = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=datetime.now(timezone.utc),
            window_context=WindowContext(process_name="searchhost.exe"),
            metadata={
                "field_session": True,
                "final_text": "some-app-query",
                "field_label": "Search",
                "uia": {"automation_id": "SearchTextBox"},
            },
        )
        hs = build_keyboard_type_text_hypotheses(ev)
        types = {h.candidate_intent_type for h in hs}
        assert "type_field_value" in types
        assert "open_app_candidate" in types

    def test_click_named_result_launcher_open_app(self):
        ev = _win_click({
            "uia": {"name": "Example Desktop App"},
            "target_identity_isolation": {"trusted_pre_action_identity": True},
        })
        hs = build_click_hypotheses(ev)
        types = {h.candidate_intent_type for h in hs}
        assert "open_app_candidate" in types


    def test_click_list_item_select_visible_and_launcher(self):
        ev = _win_click({
            "uia": {
                "name": "Some Visible Result",
                "control_type": "ListItemControl",
            },
            "target_identity_isolation": {"trusted_pre_action_identity": True},
        })
        hs = build_click_hypotheses(ev)
        types = {h.candidate_intent_type for h in hs}
        assert "select_visible_option" in types
        assert "open_app_candidate" in types


class TestHotkeys:
    def test_ctrl_t_new_tab(self):
        ev = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=datetime.now(timezone.utc),
            keyboard_action=KeyboardAction(keys=["t"], modifiers=["ctrl"]),
            metadata={"target_identity_isolation": {}},
        )
        hs = build_hotkey_hypotheses(ev)
        assert any(h.candidate_intent_type == "open_new_tab" for h in hs)

    def test_ctrl_l_address_bar(self):
        ev = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=datetime.now(timezone.utc),
            keyboard_action=KeyboardAction(keys=["l"], modifiers=["ctrl"]),
            metadata={},
        )
        hs = build_hotkey_hypotheses(ev)
        assert any(h.candidate_intent_type == "focus_browser_address_bar" for h in hs)


class TestSearchFieldEnter:
    def test_youtube_search_commit_and_enter(self):
        m = Mission(name="t", status=MissionStatus.DRAFT)
        m.semantic_first_mode = "shadow"
        m.semantic_first_version = "1"

        commit = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=datetime.now(timezone.utc),
            window_context=WindowContext(process_name="chrome.exe"),
            metadata={
                "field_session": True,
                "final_text": "query text",
                "field_label": "Buscar",
                "web": {"role": "searchbox", "placeholder": "Buscar"},
            },
        )
        m.raw_trace.append(commit)
        emit_for_raw_event(m, commit)
        n_after_commit = len(m.intent_timeline)

        ent = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=datetime.now(timezone.utc),
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            metadata={},
        )
        m.raw_trace.append(ent)
        emit_for_raw_event(m, ent)

        tail_types = [h.candidate_intent_type for h in m.intent_timeline[n_after_commit:]]
        assert "search_content_candidate" in tail_types
        assert "confirm_semantic_value" in tail_types


class TestShadowCompareCanonical:
    def test_high_agreement_with_aligned_hypotheses(self):
        from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
            build_golden_search_chrome_youtube_mission,
        )

        mission = build_golden_search_chrome_youtube_mission()
        sep_steps: List[Dict[str, Any]] = (
            mission.semantic_execution_plan or {}
        ).get("steps") or []
        assert sep_steps, "canonical mission debe tener SEP"

        aligned: List[SemanticHypothesis] = []
        for st in sep_steps:
            typ = str(st.get("type") or "")
            if typ == "open_app":
                cand = "open_app_candidate"
            elif typ == "select_profile":
                cand = "select_visible_option"
            elif typ == "open_new_tab":
                cand = "open_new_tab"
            elif typ == "open_url":
                cand = "open_url_candidate"
            elif typ in {"search_youtube", "search_site", "search_web"}:
                cand = "search_content_candidate"
            elif typ in {"scroll_results", "scroll_page"}:
                cand = "scroll_content_candidate"
            else:
                cand = "click_button_by_label"
            aligned.append(
                SemanticHypothesis(
                    event_id="syn",
                    candidate_intent_type=cand,
                    confidence=float(st.get("confidence") or 0.8),
                    status=SemanticHypothesisStatus.PROPOSED,
                )
            )
        mission.intent_timeline = aligned
        r = compare_shadow_to_sep(mission)
        assert r["collapse_agreement_rate"] >= 0.55


class TestCompatAndFlags:
    def test_old_mission_json_without_intent_timeline_loads(self):
        d = {
            "id": str(uuid.uuid4()),
            "name": "legacy",
            "version": 1,
            "schema_version": "2.0",
            "status": "draft",
            "raw_trace": [],
        }
        m = mission_from_dict(d)
        assert m.intent_timeline == []

    def test_shadow_disabled_no_timeline_mutation(self, monkeypatch):
        from app.core import config

        monkeypatch.setattr(config.settings, "SEMANTIC_FIRST_SHADOW_ENABLED", False)
        m = Mission(name="x", status=MissionStatus.DRAFT)
        m.semantic_first_mode = "shadow"
        ev = _win_click({"uia": {"name": "x"}, "target_identity_isolation": {"trusted_pre_action_identity": True}})
        m.raw_trace.append(ev)
        # emit respeta flag global
        from app.services.missions import recorder_semantic_shadow as rss

        assert rss.is_shadow_enabled(config.settings) is False
        rss.emit_for_raw_event(m, ev)
        assert len(m.intent_timeline) == 0


def test_mission_shadow_active():
    m = Mission(name="a")
    m.semantic_first_mode = "shadow"
    assert mission_shadow_active(m) is True
