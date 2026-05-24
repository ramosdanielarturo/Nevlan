"""Canonical Operational Blocks — Fase 1 shadow."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.contracts.mission import (
    CanonicalOperationalBlock,
    EventType,
    KeyboardAction,
    Mission,
    MouseAction,
    RawEvent,
    WindowContext,
    mission_from_dict,
)
from app.services.missions.canonical_operational_blocks import (
    SESSION_FORM_FILL,
    SESSION_LAUNCHER,
    SESSION_NAVIGATION,
    SESSION_SEARCH,
    SESSION_SELECTION,
    compare_cobs_vs_sep,
    derive_repair_strategy,
    derive_replay_strategy,
    merge_cobs,
    promote_session_to_cob,
    rebuild_cob_audit_only,
    score_cob,
)
from app.services.missions.recorder_semantic_shadow import emit_for_raw_event
from app.services.missions.semantic_session_engine import (
    SemanticSessionStatus,
    observe_after_record_step,
)


def _simulate_record(m: Mission, ev: RawEvent) -> None:
    m.raw_trace.append(ev)
    emit_for_raw_event(m, ev)
    observe_after_record_step(m, ev)


def _chrome_ctx() -> WindowContext:
    return WindowContext(
        hwnd=1,
        title="Browser",
        process_name="chrome.exe",
    )


def _shell_ctx() -> WindowContext:
    return WindowContext(
        hwnd=2,
        title="Search",
        process_name="searchhost.exe",
    )


class TestPromoteLauncher:
    def test_launcher_session_to_open_application_cob(self):
        m = Mission(name="l")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        m.cob_shadow_mode = True
        ts = datetime.now(timezone.utc)

        click = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_CLICK,
            timestamp=ts,
            mouse_action=MouseAction(x=10, y=10, button="left"),
            window_context=_shell_ctx(),
            metadata={
                "uia": {"name": "", "control_type": "EditControl", "automation_id": "SearchTextBox"},
                "target_identity_isolation": {"trusted_pre_action_identity": True},
                "capture_contract": {"capture_complete": True, "sufficient_for_execution": True},
            },
        )
        _simulate_record(m, click)

        typ = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=ts,
            window_context=_shell_ctx(),
            metadata={
                "field_session": True,
                "final_text": "Chrome",
                "uia": {"automation_id": "SearchTextBox"},
            },
        )
        _simulate_record(m, typ)

        pick = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_CLICK,
            timestamp=ts,
            mouse_action=MouseAction(x=50, y=120, button="left"),
            window_context=_chrome_ctx(),
            metadata={
                "uia": {"control_type": "ListItemControl", "name": "Chrome"},
                "target_identity_isolation": {"trusted_pre_action_identity": True},
            },
        )
        _simulate_record(m, pick)

        launcher_sess = next(s for s in m.semantic_sessions if s.session_type == SESSION_LAUNCHER)
        assert launcher_sess.status != SemanticSessionStatus.ACTIVE

        assert len(m.canonical_operational_blocks) >= 1
        cob = m.canonical_operational_blocks[0]
        assert cob.canonical_action == "open_application"
        assert cob.block_type == SESSION_LAUNCHER


class TestPromoteNavigation:
    def test_navigation_session_to_navigate_to_site_cob(self):
        m = Mission(name="nav")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        m.cob_shadow_mode = True
        ts = datetime.now(timezone.utc)

        ctrl_l = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["l"], modifiers=["ctrl"]),
            window_context=_chrome_ctx(),
            metadata={
                "target_identity_isolation": {"trusted_pre_action_identity": True},
                "capture_contract": {"capture_complete": True, "sufficient_for_execution": True},
            },
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

        sess = next(s for s in m.semantic_sessions if s.session_type == SESSION_NAVIGATION)
        assert sess.status != SemanticSessionStatus.ACTIVE
        cob = next(c for c in m.canonical_operational_blocks if c.block_type == SESSION_NAVIGATION)
        assert cob.canonical_action == "navigate_to_site"


class TestPromoteSearch:
    def test_search_session_to_search_content_and_browse_results_cob(self):
        m = Mission(name="search")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        m.cob_shadow_mode = True
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
                "capture_contract": {"capture_complete": True, "sufficient_for_execution": True},
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
                "final_text": "cats",
                "web": {"role": "searchbox"},
            },
        )
        _simulate_record(m, commit)

        enter = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=_chrome_ctx(),
            metadata={},
        )
        _simulate_record(m, enter)

        scroll = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_SCROLL,
            timestamp=ts,
            mouse_action=MouseAction(x=50, y=50, button="left"),
            window_context=_chrome_ctx(),
            metadata={"scroll_delta": -120},
        )
        _simulate_record(m, scroll)

        sess = next(s for s in m.semantic_sessions if s.session_type == SESSION_SEARCH)
        assert sess.status != SemanticSessionStatus.ACTIVE
        cob = next(c for c in m.canonical_operational_blocks if c.block_type == SESSION_SEARCH)
        assert cob.canonical_action == "search_content_and_browse_results"


class TestPromoteSelection:
    def test_selection_session_to_select_visible_entity_cob(self):
        m = Mission(name="sel")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        m.cob_shadow_mode = True
        ts = datetime.now(timezone.utc)

        click = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_CLICK,
            timestamp=ts,
            mouse_action=MouseAction(x=30, y=80, button="left"),
            window_context=_chrome_ctx(),
            metadata={
                "uia": {"control_type": "ListItemControl", "name": "Option A"},
                "target_identity_isolation": {"trusted_pre_action_identity": True},
                "capture_contract": {"capture_complete": True, "sufficient_for_execution": True},
            },
        )
        _simulate_record(m, click)

        enter = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=_chrome_ctx(),
            metadata={},
        )
        _simulate_record(m, enter)

        sess = next(s for s in m.semantic_sessions if s.session_type == SESSION_SELECTION)
        assert sess.status != SemanticSessionStatus.ACTIVE
        cob = next(c for c in m.canonical_operational_blocks if c.block_type == SESSION_SELECTION)
        assert cob.canonical_action == "select_visible_entity"


class TestPromoteForm:
    def test_form_fill_session_to_fill_and_submit_form_cob(self):
        m = Mission(name="form")
        m.semantic_first_mode = "shadow"
        m.session_shadow_mode = True
        m.cob_shadow_mode = True
        ts = datetime.now(timezone.utc)

        click = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_CLICK,
            timestamp=ts,
            mouse_action=MouseAction(x=40, y=40, button="left"),
            window_context=_chrome_ctx(),
            metadata={
                "uia": {"control_type": "EditControl", "name": "Email"},
                "web": {"role": "textbox", "placeholder": "you@example.org"},
                "target_identity_isolation": {"trusted_pre_action_identity": True},
                "capture_contract": {"capture_complete": True, "sufficient_for_execution": True},
            },
        )
        _simulate_record(m, click)

        typ = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=ts,
            window_context=_chrome_ctx(),
            metadata={
                "field_session": True,
                "final_text": "hi@example.org",
                "web": {"role": "textbox"},
            },
        )
        _simulate_record(m, typ)

        submit = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=_chrome_ctx(),
            metadata={},
        )
        _simulate_record(m, submit)

        sess = next(s for s in m.semantic_sessions if s.session_type == SESSION_FORM_FILL)
        assert sess.status != SemanticSessionStatus.ACTIVE
        cob = next(c for c in m.canonical_operational_blocks if c.block_type == SESSION_FORM_FILL)
        assert cob.canonical_action == "fill_and_submit_form"


class TestCompareStrategies:
    def test_compare_cobs_vs_sep_compression_and_noise(self):
        m = Mission(name="cmp")
        m.semantic_execution_plan = {
            "steps": [
                {"type": "open_app"},
                {"type": "open_url"},
                {"type": "search_site"},
                {"type": "type_text"},
                {"type": "type_text"},
                {"type": "scroll"},
            ],
        }
        # Simular muchos eventos crudos frente a pocos COB
        for _ in range(48):
            m.raw_trace.append(
                RawEvent(
                    id=str(uuid.uuid4()),
                    event_type=EventType.MOUSE_MOVE,
                    timestamp=datetime.now(timezone.utc),
                    mouse_action=MouseAction(x=1, y=1, button="left"),
                ),
            )
        m.canonical_operational_blocks = [
            CanonicalOperationalBlock(
                canonical_action="open_application",
                block_type=SESSION_LAUNCHER,
                session_id="s1",
                absorbed_steps=15,
            ),
            CanonicalOperationalBlock(
                canonical_action="navigate_to_site",
                block_type=SESSION_NAVIGATION,
                session_id="s2",
                absorbed_steps=12,
            ),
            CanonicalOperationalBlock(
                canonical_action="search_content_and_browse_results",
                block_type=SESSION_SEARCH,
                session_id="s3",
                absorbed_steps=21,
            ),
        ]
        r = compare_cobs_vs_sep(m)
        assert r["compression_ratio_events_per_cob"] >= 10.0
        assert r["agreement_rate"] >= 0.66
        assert r["structural_noise_reduction_cob_vs_raw"] > 0.9

    def test_derive_replay_strategy_hotkey_and_semantic(self):
        rs = derive_replay_strategy("navigate_to_site", SESSION_NAVIGATION, {})
        assert rs["mode"] == "hybrid"
        assert "hotkey" in rs["components"]

    def test_derive_repair_strategy_not_event_granular(self):
        rr = derive_repair_strategy("search_content_and_browse_results")
        phases = rr.get("phases") or []
        joined = " ".join(phases).lower()
        assert "keypress" not in joined and "#17" not in joined
        assert rr.get("granularity") == "semantic_block"


class TestLegacyCompat:
    def test_old_mission_dict_loads_without_cob_keys(self):
        data = {
            "name": "legacy",
            "raw_trace": [],
            "interpreted_steps": [],
            "compiled_execution_graph": [],
        }
        m = mission_from_dict(data)
        assert m.canonical_operational_blocks == []
        assert m.cob_shadow_mode is False
        rebuild_cob_audit_only(m)
        assert m.cob_audit is None

    def test_cob_shadow_mode_off_skips_promotion(self):
        m = Mission(name="off")
        m.cob_shadow_mode = False
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
                "final_text": "https://a.test/",
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

        sess = next(s for s in m.semantic_sessions if s.session_type == SESSION_NAVIGATION)
        assert promote_session_to_cob(m, sess) is None
        assert m.canonical_operational_blocks == []


class TestMergeAndScore:
    def test_merge_cobs_accumulates_absorption(self):
        a = CanonicalOperationalBlock(
            canonical_action="navigate_to_site",
            block_type=SESSION_NAVIGATION,
            session_id="a",
            absorbed_steps=3,
            source_event_ids=["e1"],
        )
        b = CanonicalOperationalBlock(
            canonical_action="navigate_to_site",
            block_type=SESSION_NAVIGATION,
            session_id="b",
            absorbed_steps=5,
            source_event_ids=["e2"],
        )
        merged = merge_cobs(a, b)
        assert merged.absorbed_steps == 8
        assert set(merged.source_event_ids) == {"e1", "e2"}

    def test_score_cob_bounded(self):
        c = CanonicalOperationalBlock(
            canonical_action="navigate_to_site",
            block_type=SESSION_NAVIGATION,
            session_id="z",
            absorbed_steps=4,
            confidence=0.9,
            execution_truth_snapshot={
                "trusted_pre_action_identity": True,
                "capture_complete": True,
                "sufficient_for_execution": True,
            },
        )
        st, pt = score_cob(c)
        assert 0 <= st <= 1 and 0 <= pt <= 1
