"""Semantic Session Engine — tests (modo sombra)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    Mission,
    MouseAction,
    RawEvent,
    WindowContext,
    mission_from_dict,
)
from app.services.missions.recorder_semantic_shadow import emit_for_raw_event
from app.services.missions.semantic_session_engine import (
    SESSION_FORM_FILL,
    SESSION_NAVIGATION,
    SESSION_SEARCH,
    SESSION_SELECTION,
    SemanticSessionStatus,
    compare_session_vs_sep,
    observe_after_record_step,
)


def _simulate_record(m: Mission, ev: RawEvent) -> None:
    """Replica orden recorder: trace → shadow → sesiones."""
    m.raw_trace.append(ev)
    emit_for_raw_event(m, ev)
    observe_after_record_step(m, ev)


def _chrome_ctx() -> WindowContext:
    return WindowContext(
        hwnd=1,
        title="Browser",
        process_name="chrome.exe",
    )


class TestNavigationSession:
    def test_ctrl_l_url_enter_navigation_session(self):
        m = Mission(name="nav")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        ts = datetime.now(timezone.utc)

        ctrl_l = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["l"], modifiers=["ctrl"]),
            window_context=_chrome_ctx(),
            metadata={"target_identity_isolation": {}},
        )
        _simulate_record(m, ctrl_l)

        type_u = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=ts,
            window_context=_chrome_ctx(),
            metadata={
                "field_session": True,
                "final_text": "https://example.com/",
                "field_label": "Address",
                "web": {"role": "textbox"},
            },
        )
        _simulate_record(m, type_u)

        enter = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=_chrome_ctx(),
            metadata={},
        )
        _simulate_record(m, enter)

        types = [s.session_type for s in m.semantic_sessions]
        assert SESSION_NAVIGATION in types
        intents = [i.intent_type for i in m.operational_intents]
        assert "navigate_to_site" in intents


class TestSearchSession:
    def test_searchbox_query_enter_scroll(self):
        m = Mission(name="search")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        ts = datetime.now(timezone.utc)

        click = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_CLICK,
            timestamp=ts,
            mouse_action=MouseAction(x=100, y=100, button="left"),
            window_context=_chrome_ctx(),
            metadata={
                "uia": {"name": "Search", "control_type": "EditControl"},
                "web": {"role": "searchbox", "placeholder": "Find"},
                "target_identity_isolation": {"trusted_pre_action_identity": True},
            },
        )
        _simulate_record(m, click)

        commit = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=ts,
            window_context=_chrome_ctx(),
            metadata={
                "field_session": True,
                "final_text": "some query",
                "web": {"role": "searchbox"},
            },
        )
        _simulate_record(m, commit)

        ent = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=_chrome_ctx(),
            metadata={},
        )
        _simulate_record(m, ent)

        scroll = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_SCROLL,
            timestamp=ts,
            mouse_action=MouseAction(x=100, y=200, button="scroll"),
            window_context=_chrome_ctx(),
            metadata={"scroll": {"dx": 0, "dy": -120}},
        )
        _simulate_record(m, scroll)

        assert any(s.session_type == SESSION_SEARCH for s in m.semantic_sessions)
        itypes = [i.intent_type for i in m.operational_intents]
        assert "submit_search_query" in itypes
        assert "browse_search_results" in itypes


class TestSelectionSession:
    def test_list_item_then_enter(self):
        m = Mission(name="sel")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        ts = datetime.now(timezone.utc)

        clk = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_CLICK,
            timestamp=ts,
            mouse_action=MouseAction(x=50, y=50),
            window_context=WindowContext(process_name="searchhost.exe"),
            metadata={
                "uia": {
                    "name": "Result Row",
                    "control_type": "ListItemControl",
                },
                "target_identity_isolation": {"trusted_pre_action_identity": True},
            },
        )
        _simulate_record(m, clk)

        ent = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=WindowContext(process_name="searchhost.exe"),
            metadata={},
        )
        _simulate_record(m, ent)

        assert any(s.session_type == SESSION_SELECTION for s in m.semantic_sessions)
        assert any(i.intent_type == "activate_visible_choice" for i in m.operational_intents)


class TestFormFillSession:
    def test_two_fields_tab_submit(self):
        m = Mission(name="form")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        ts = datetime.now(timezone.utc)
        wc = WindowContext(process_name="chrome.exe", title="Form")

        r1 = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=ts,
            window_context=wc,
            metadata={
                "field_session": True,
                "final_text": "alice",
                "web": {"role": "textbox", "accessible_name": "Name"},
            },
        )
        _simulate_record(m, r1)

        tab = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["tab"], modifiers=[]),
            window_context=wc,
            metadata={},
        )
        _simulate_record(m, tab)

        r2 = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=ts,
            window_context=wc,
            metadata={
                "field_session": True,
                "final_text": "bob@mail.test",
                "web": {"role": "textbox", "accessible_name": "Email"},
            },
        )
        _simulate_record(m, r2)

        sub = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=wc,
            metadata={},
        )
        _simulate_record(m, sub)

        assert any(s.session_type == SESSION_FORM_FILL for s in m.semantic_sessions)
        assert any(i.intent_type == "complete_multi_field_form" for i in m.operational_intents)


class TestHardContextChange:
    def test_closes_search_when_explorer_context(self):
        m = Mission(name="ctx")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        ts = datetime.now(timezone.utc)

        click = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_CLICK,
            timestamp=ts,
            mouse_action=MouseAction(x=10, y=10),
            window_context=_chrome_ctx(),
            metadata={
                "web": {"role": "searchbox"},
                "target_identity_isolation": {"trusted_pre_action_identity": True},
            },
        )
        _simulate_record(m, click)

        assert any(
            s.session_type == SESSION_SEARCH and s.status == SemanticSessionStatus.ACTIVE
            for s in m.semantic_sessions
        )

        away = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_CLICK,
            timestamp=ts,
            mouse_action=MouseAction(x=5, y=5),
            window_context=WindowContext(process_name="explorer.exe", title="Documents"),
            metadata={"target_identity_isolation": {"trusted_pre_action_identity": True}},
        )
        _simulate_record(m, away)

        active_search = [
            s for s in m.semantic_sessions
            if s.session_type == SESSION_SEARCH and s.status == SemanticSessionStatus.ACTIVE
        ]
        assert not active_search


class TestCanonicalAgreement:
    def test_canonical_replay_agreement(self):
        from tests.unit.test_sipe_telemetry_persistence_catalog import (
            _canonical_chrome_youtube_trace,
        )
        from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
            inject_search_to_chrome_execution_truth,
        )
        from app.services.missions.intent_collapse_engine import apply_collapse_to_mission
        from app.services.missions.semantic_execution_plan import attach_semantic_plan_to_mission
        from app.contracts.mission import MissionStatus
        from app.services.missions.semantic_intent_promotion_engine import (
            apply_promotion_to_mission,
        )

        events = _canonical_chrome_youtube_trace()
        inject_search_to_chrome_execution_truth(events)

        m = Mission(name="canonical-shadow-sessions", raw_trace=[])
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True

        for ev in events:
            _simulate_record(m, ev)

        apply_collapse_to_mission(m)
        attach_semantic_plan_to_mission(m, force=True)
        m.status = MissionStatus.EXECUTABLE
        apply_promotion_to_mission(m)

        r = compare_session_vs_sep(m)
        assert r["session_agreement_with_sep"] >= 0.30


class TestLegacy:
    def test_old_mission_without_sessions_loads(self):
        d = {
            "id": str(uuid.uuid4()),
            "name": "legacy",
            "version": 1,
            "schema_version": "2.0",
            "status": "draft",
            "raw_trace": [],
        }
        m = mission_from_dict(d)
        assert m.semantic_sessions == []
        assert m.session_shadow_mode is False

    def test_session_shadow_off_skips_observe(self):
        m = Mission(name="x")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = False
        ev = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            keyboard_action=KeyboardAction(keys=["l"], modifiers=["ctrl"]),
            window_context=_chrome_ctx(),
            metadata={},
        )
        m.raw_trace.append(ev)
        emit_for_raw_event(m, ev)
        observe_after_record_step(m, ev)
        assert m.semantic_sessions == []
