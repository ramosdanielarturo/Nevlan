"""Tests universales Truth Extraction Layer (TEL). Sin apps específicas en expectativas duras."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.contracts.mission import (
    CanonicalOperationalBlock,
    CanonicalOperationalBlockStatus,
    EventType,
    KeyboardAction,
    Mission,
    MissionStatus,
    MouseAction,
    RawEvent,
    SemanticHypothesis,
    SemanticHypothesisStatus,
    SemanticSession,
    SemanticSessionStatus,
    WindowContext,
)
from app.services.missions.semantic_session_engine import SESSION_FORM_FILL
from app.services.missions.truth_extraction_layer import (
    COB_CANONICAL_TO_TRUTH_TYPE,
    build_tel_audit,
    extract_truth_blocks,
)


def _ts(offset: float = 0.0) -> datetime:
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    return datetime.fromtimestamp(base.timestamp() + offset, tz=timezone.utc)


def _ev(
    eid: str,
    *,
    delta_s: float = 0,
    evt: EventType = EventType.MOUSE_CLICK,
    meta=None,
    proc_name: str = "host.exe",
) -> RawEvent:
    return RawEvent(
        id=eid,
        event_type=evt,
        timestamp=_ts(delta_s),
        mouse_action=MouseAction(x=10, y=10),
        window_context=WindowContext(process_name=proc_name, title="Generic Host"),
        metadata=dict(meta or {}),
    )


def _cob_launcher_open_app(eids, hyps_ids, session_id="s-launch"):
    return CanonicalOperationalBlock(
        session_id=session_id,
        operational_intent_id="oi1",
        block_type="launcher_session",
        canonical_action="open_application",
        params={"query_preview": "GenericAppLauncherQuery"},
        confidence=0.82,
        stability_score=0.76,
        source_event_ids=eids,
        source_hypothesis_ids=hyps_ids,
        absorbed_steps=len(eids),
        status=CanonicalOperationalBlockStatus.ACCEPTED,
        ambiguity={"session_ambiguity": {"hypothesis_type_count": 2}, "promotion_gates": {"overall": 0.71}},
        completion_signals=["launcher_pick"],
    )


def test_launcher_micro_events_compress_to_single_open_application():
    e1, e2, e3 = "e1", "e2", "e3"
    mission = Mission(
        name="tel_generic_launcher",
        status=MissionStatus.DRAFT,
        raw_trace=[_ev(e1, delta_s=0), _ev(e2, delta_s=1.1), _ev(e3, delta_s=2.8)],
        tel_shadow_mode=True,
        cob_shadow_mode=True,
        canonical_operational_blocks=[
            _cob_launcher_open_app([e1, e2, e3], []),
        ],
    )
    blocks = extract_truth_blocks(mission)
    assert len(blocks) >= 1
    assert blocks[0].truth_type == "open_application"
    audit = build_tel_audit(mission, blocks)
    assert audit["truth_block_count"] == len(blocks)
    assert audit["compression_raw_events_per_truth_block"] >= 2


def test_search_then_scroll_splits_search_and_browse():
    sid = "sess-search-a"
    e1 = "sa1"
    mission = Mission(
        name="tel_search_flow",
        status=MissionStatus.DRAFT,
        raw_trace=[
            _ev(e1, delta_s=0, proc_name="browser.exe"),
        ],
        semantic_sessions=[
            SemanticSession(
                id=sid,
                session_type="search_session",
                status=SemanticSessionStatus.COMPLETED,
                source_event_ids=[e1],
                context={"had_scroll": True, "scroll_count": 2},
                confidence=0.8,
                completion_signals=["search_submit"],
            ),
        ],
        canonical_operational_blocks=[
            CanonicalOperationalBlock(
                session_id=sid,
                operational_intent_id="ois",
                block_type="search_session",
                canonical_action="search_content_and_browse_results",
                params={"query_preview": "q=find important row"},
                confidence=0.86,
                stability_score=0.79,
                source_event_ids=[e1],
                absorbed_steps=14,
                completion_signals=["search_submit", "scroll_continuation"],
                ambiguity={
                    "session_ambiguity": {"hypothesis_type_count": 3},
                    "promotion_gates": {"overall": 0.74},
                },
            ),
        ],
        tel_shadow_mode=True,
    )
    blocks = extract_truth_blocks(mission)
    types = [b.truth_type for b in blocks]
    assert "search_content" in types
    assert "browse_results" in types
    intents_joined = " ".join(b.human_intent.lower() for b in blocks)
    for leak in ("uia_text_match", "smart_route", "ctrl+l", ":dom"):
        assert leak not in intents_joined


def test_standalone_hotkey_hypothesis_surfaces_open_tab_operational_truth():
    h1_id = "hyp-tab-a"
    e_tab = "k1"
    mission = Mission(
        name="tel_open_tab_hyp",
        status=MissionStatus.DRAFT,
        raw_trace=[
            _ev(e_tab, delta_s=5, evt=EventType.KEYBOARD_HOTKEY),
        ],
        canonical_operational_blocks=[
            CanonicalOperationalBlock(
                session_id="other",
                block_type="navigation_session",
                canonical_action="navigate_to_site",
                params={"url_preview": "https://partner-app.example/info"},
                confidence=0.8,
                source_event_ids=["n1"],  # disjunta del hotkey hipotético
                absorbed_steps=3,
                stability_score=0.7,
                ambiguity={"session_ambiguity": {}, "promotion_gates": {"overall": 0.8}},
            ),
        ],
        intent_timeline=[
            SemanticHypothesis(
                id=h1_id,
                event_id=e_tab,
                candidate_intent_type="open_new_tab",
                source_event_ids=[e_tab],
                confidence=0.9,
                status=SemanticHypothesisStatus.PROPOSED,
                carriers=["hotkey"],
            ),
        ],
        tel_shadow_mode=True,
    )
    blocks = extract_truth_blocks(mission)
    assert any(b.truth_type == "open_tab" for b in blocks)


def test_form_fill_splits_submit_when_completion_signal_present():
    e0, esubmit = "f0", "f99"
    mission = Mission(
        name="tel_form",
        status=MissionStatus.DRAFT,
        raw_trace=[
            _ev(e0),
            RawEvent(
                id=esubmit,
                event_type=EventType.KEYBOARD_KEY_PRESS,
                timestamp=_ts(120),
                window_context=WindowContext(process_name="suite.exe"),
                keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            ),
        ],
        semantic_sessions=[
            SemanticSession(
                id="sess-f",
                session_type=SESSION_FORM_FILL,
                status=SemanticSessionStatus.COMPLETED,
                source_event_ids=[e0, esubmit],
                completion_signals=["form_submit"],
            ),
        ],
        canonical_operational_blocks=[
            CanonicalOperationalBlock(
                session_id="sess-f",
                block_type="form_fill_session",
                canonical_action="fill_and_submit_form",
                params={"form_fields_count": 2},
                confidence=0.77,
                source_event_ids=[e0, esubmit],
                absorbed_steps=5,
                completion_signals=["form_submit"],
                stability_score=0.64,
                ambiguity={"session_ambiguity": {"hypothesis_type_count": 1}, "promotion_gates": {"overall": 0.76}},
            ),
        ],
        tel_shadow_mode=True,
    )
    blocks = extract_truth_blocks(mission)
    assert set(COB_CANONICAL_TO_TRUTH_TYPE.values()).issuperset(
        {"fill_form"}
    )
    tt = sorted({b.truth_type for b in blocks})
    assert "fill_form" in tt
    assert "submit_form" in tt


def test_select_entity_truth_contains_no_smart_route_vocab():
    eid = "sel1"
    mission = Mission(
        name="tel_select",
        status=MissionStatus.DRAFT,
        raw_trace=[_ev(eid)],
        canonical_operational_blocks=[
            CanonicalOperationalBlock(
                session_id="sel-s",
                block_type="selection_session",
                canonical_action="select_visible_entity",
                params={"label_hint": "Account #4421"},
                confidence=0.79,
                source_event_ids=[eid],
                absorbed_steps=1,
                stability_score=0.72,
                ambiguity={"session_ambiguity": {}, "promotion_gates": {"overall": 0.78}},
            ),
        ],
        tel_shadow_mode=True,
    )
    blocks = extract_truth_blocks(mission)
    assert blocks[0].truth_type == "select_entity"
    combined = blocks[0].human_intent + str(blocks[0].replay_anchor_candidates)
    assert "smart_route" not in combined.lower()


def test_tel_reduces_step_count_vs_artificial_sep():
    ids = [f"w{i}" for i in range(9)]
    mission = Mission(
        name="tel_compress",
        status=MissionStatus.DRAFT,
        raw_trace=[_ev(x, delta_s=i * 0.2) for i, x in enumerate(ids)],
        semantic_execution_plan={
            "steps": [{"type": f"stub_step_{j}"} for j in range(8)],
            "source": "test",
        },
        canonical_operational_blocks=[
            _cob_launcher_open_app(ids, []),
        ],
        tel_shadow_mode=True,
    )
    blocks = extract_truth_blocks(mission)
    audit = build_tel_audit(mission, blocks)
    sep_n = audit["sep_step_count"]
    tel_n = audit["truth_block_count"]
    ratio = audit["compression_sep_steps_per_truth_block"]
    assert sep_n >= tel_n
    assert ratio is not None
    assert ratio == round(sep_n / max(tel_n, 1), 4)
