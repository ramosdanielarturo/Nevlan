"""COB Replay Shadow — planes simulados sin runtime real."""
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
from app.services.missions.cob_replay_shadow import (
    assess_operational_readiness,
    build_cob_replay_plan,
    compare_cob_replay_vs_sep_and_legacy,
    simulate_mission_cob_replay_shadow,
)
from app.services.missions.recorder_semantic_shadow import emit_for_raw_event
from app.services.missions.semantic_session_engine import observe_after_record_step


def _chrome_ctx() -> WindowContext:
    return WindowContext(hwnd=1, title="Browser", process_name="chrome.exe")


def _record(m: Mission, ev: RawEvent) -> None:
    m.raw_trace.append(ev)
    emit_for_raw_event(m, ev)
    observe_after_record_step(m, ev)


class TestSearchReplayPlan:
    def test_search_produces_five_intent_steps(self):
        cob = CanonicalOperationalBlock(
            canonical_action="search_content_and_browse_results",
            block_type="search_session",
            session_id="s1",
            params={"query_preview": "cats"},
            execution_truth_snapshot={
                "trusted_pre_action_identity": True,
                "capture_complete": True,
                "sufficient_for_execution": True,
            },
        )
        steps = build_cob_replay_plan(cob)
        assert len(steps) == 5
        ids = [s.phase_id for s in steps]
        assert ids[0] == "focus_search_field"
        assert ids[1] == "inject_semantic_query"
        assert ids[2] == "submit_search"
        assert ids[3] == "validate_results_loaded"
        assert ids[4] == "allow_browsing_state"

    def test_readiness_drops_without_query(self):
        cob = CanonicalOperationalBlock(
            canonical_action="search_content_and_browse_results",
            block_type="search_session",
            session_id="s2",
            params={},
            execution_truth_snapshot={},
        )
        steps = build_cob_replay_plan(cob)
        readiness, gaps = assess_operational_readiness(cob, steps)
        assert readiness < 0.85
        assert any("query" in g for g in gaps)


class TestSimulateMission:
    def test_simulate_persists_and_compares(self):
        m = Mission(name="x")
        m.semantic_execution_plan = {
            "steps": [
                {"type": "open_app"},
                {"type": "search_site"},
            ],
        }
        m.compiled_execution_graph = [{}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}]
        for _ in range(24):
            m.raw_trace.append(
                RawEvent(
                    id=str(uuid.uuid4()),
                    event_type=EventType.MOUSE_MOVE,
                    timestamp=datetime.now(timezone.utc),
                    mouse_action=MouseAction(x=0, y=0, button="left"),
                ),
            )
        m.canonical_operational_blocks = [
            CanonicalOperationalBlock(
                canonical_action="open_application",
                block_type="launcher_session",
                session_id="a",
                params={"query_preview": "app"},
                absorbed_steps=5,
            ),
            CanonicalOperationalBlock(
                canonical_action="search_content_and_browse_results",
                block_type="search_session",
                session_id="b",
                params={"query_preview": "q"},
                absorbed_steps=8,
            ),
        ]
        snap = simulate_mission_cob_replay_shadow(m, persist=True)
        assert m.cob_replay_shadow is not None
        assert snap["comparison"]["cob_replay_micro_steps_total"] >= 9
        assert snap["comparison"]["sep_step_count"] == 2
        hint = snap["comparison"]["intent_driven_vs_event_legacy_hint"]
        assert hint == "cob_micro_lt_compiled"


class TestEndToEndShadowRecording:
    def test_search_session_cob_has_replay_shadow(self):
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
                "web": {"role": "searchbox"},
                "target_identity_isolation": {"trusted_pre_action_identity": True},
                "capture_contract": {"capture_complete": True, "sufficient_for_execution": True},
            },
        )
        _record(m, click)

        commit = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=ts,
            window_context=_chrome_ctx(),
            metadata={
                "field_session": True,
                "final_text": "nevlan",
                "web": {"role": "searchbox"},
            },
        )
        _record(m, commit)

        enter = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=ts,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=_chrome_ctx(),
            metadata={},
        )
        _record(m, enter)

        scroll = RawEvent(
            id=str(uuid.uuid4()),
            event_type=EventType.MOUSE_SCROLL,
            timestamp=ts,
            mouse_action=MouseAction(x=50, y=50, button="scroll"),
            window_context=_chrome_ctx(),
            metadata={"scroll": {"dx": 0, "dy": -120}},
        )
        _record(m, scroll)

        search_cobs = [c for c in m.canonical_operational_blocks if c.canonical_action == "search_content_and_browse_results"]
        assert len(search_cobs) == 1
        snap = simulate_mission_cob_replay_shadow(m, persist=True)
        plans = snap.get("plans") or []
        search_plan = next(p for p in plans if p.get("canonical_action") == "search_content_and_browse_results")
        assert len(search_plan.get("steps") or []) == 5


class TestLegacyMissionJson:
    def test_loads_without_cob_replay_shadow_key(self):
        m = mission_from_dict({"name": "old", "raw_trace": [], "interpreted_steps": [], "compiled_execution_graph": []})
        assert m.cob_replay_shadow is None


class TestCompareHelper:
    def test_compare_cob_replay_vs_sep_and_legacy(self):
        m = Mission(name="c")
        m.semantic_execution_plan = {"steps": [{"type": "open_url"}]}
        summaries = [
            {"canonical_action": "navigate_to_site", "steps": [{"x": 1}, {"x": 2}], "operational_readiness": 0.8},
        ]
        r = compare_cob_replay_vs_sep_and_legacy(m, summaries)
        assert r["sep_alignment_count"] == 1
        assert r["cob_replay_micro_steps_total"] == 2
