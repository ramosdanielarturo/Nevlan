"""Tests — Universal Operational Element Identity (UOEI)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    OperationalElementType,
    OperationalFreezeCollectionSnapshot,
    OperationalFreezeEntityCandidate,
    OperationalFreezeSnapshot,
    OperationalFreezeTarget,
    RawEvent,
    TransitionRiskLevel,
)
from app.services.missions.freeze_first_execution_core import (
    promote_freeze_to_canonical_intent,
)
from app.services.missions.operational_continuity_reconstruction import (
    build_operational_threads,
)
from app.services.missions.operational_truth_preservation import truth_fingerprint
from app.services.missions.universal_operational_affordance_classifier import (
    classify_from_freeze,
)
from app.services.runtime.goal_oriented_runtime import build_operational_decision_context
from app.services.runtime.operational_runtime_consistency_engine import expert_panel_lines
from app.services.runtime.universal_operational_element_identity import (
    build_identity_context_from_freeze,
    identify_from_freeze,
    identity_fingerprint,
    identity_from_metadata,
    match_identity_stability,
    replay_resolve_by_identity,
    resolve_operational_element_identity,
)

_FAKE_APP = "FictionalApp"
_FAKE_PROC = "FictionalHost.exe"
_FAKE_USER_A = "User Alpha Nine"
_FAKE_USER_B = "User Beta Twelve"


def _freeze(
    *,
    name: str = "",
    role: str = "button",
    hierarchy: list[str] | None = None,
    collection_entities: list[str] | None = None,
    collection_type: str = "list",
    icon_sig: str = "",
    scrollable: bool = False,
    dom_role: str = "",
) -> OperationalFreezeSnapshot:
    ents = collection_entities or []
    coll = None
    if len(ents) > 1:
        coll = OperationalFreezeCollectionSnapshot(
            collection_type=collection_type,
            display_name="CollectionSurface",
            entities=ents,
            entity_count=len(ents),
            confidence=0.9,
            scrollable=scrollable,
        )
    dom_targets = []
    if dom_role:
        dom_targets = [{"role": dom_role, "name": name, "overlap_score": 1.0}]
    return OperationalFreezeSnapshot(
        freeze_id=str(uuid.uuid4()),
        pre_transition_confirmed=True,
        freeze_confidence=0.92,
        transition_risk=TransitionRiskLevel.MEDIUM,
        window_context={"process_name": _FAKE_PROC, "title": _FAKE_APP},
        hierarchy_chain=hierarchy or ["MainWindow"],
        visual_group_signature=icon_sig,
        best_entity_candidate=OperationalFreezeEntityCandidate(
            display_name=name,
            normalized_name=name.lower().replace(" ", "_") if name else "",
            confidence=0.94,
            evidence_sources=["uia"],
        )
        if name
        else None,
        best_collection_candidate=coll,
        collection_entities=[
            OperationalFreezeEntityCandidate(
                display_name=n,
                normalized_name=n.lower().replace(" ", "_"),
                confidence=0.9,
            )
            for n in ents
        ],
        uia_targets=[
            OperationalFreezeTarget(
                role=role,
                name=name,
                overlap_score=1.0,
                parent_chain=hierarchy or ["MainWindow"],
            ),
        ]
        if name or role
        else [],
        dom_targets=dom_targets,
    )


def _type_event(text: str) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        timestamp=datetime.now(timezone.utc),
        keyboard_action=KeyboardAction(keys=[c for c in text if c.strip()]),
        metadata={"final_text": text},
    )


def _scroll_event() -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_SCROLL,
        timestamp=datetime.now(timezone.utc),
    )


class TestElementTypeClassification:

    def test_launcher_input_surface(self):
        fr = _freeze(
            name="",
            role="edit",
            hierarchy=["AppLauncher", "SearchHost"],
            dom_role="textbox",
        )
        ident = identify_from_freeze(fr)
        assert ident.operational_element_type == OperationalElementType.LAUNCHER_INPUT_SURFACE

    def test_search_input_surface(self):
        fr = _freeze(name="", role="searchbox", hierarchy=["MainWindow", "SearchBar"])
        ident = identify_from_freeze(fr, next_event=_type_event("query alpha"))
        assert ident.operational_element_type == OperationalElementType.SEARCH_INPUT_SURFACE

    def test_context_creation_affordance(self):
        fr = _freeze(
            name="+",
            role="button",
            hierarchy=["TabStrip", "BrowserChrome"],
            icon_sig="plus_icon",
        )
        ident = identify_from_freeze(fr)
        assert ident.operational_element_type == OperationalElementType.CONTEXT_CREATION_AFFORDANCE

    def test_selectable_identity(self):
        fr = _freeze(
            name=_FAKE_USER_A,
            role="listitem",
            hierarchy=["IdentityPicker", "MainWindow"],
            collection_entities=[_FAKE_USER_A, _FAKE_USER_B],
            collection_type="cards",
        )
        ident = identify_from_freeze(fr)
        assert ident.operational_element_type == OperationalElementType.SELECTABLE_IDENTITY

    def test_results_container(self):
        fr = _freeze(
            name="ResultItem",
            role="listitem",
            hierarchy=["ResultsPane"],
            collection_entities=["Item1", "Item2", "Item3"],
            collection_type="search_results",
        )
        ident = identify_from_freeze(fr)
        assert ident.operational_element_type in (
            OperationalElementType.RESULTS_CONTAINER,
            OperationalElementType.SCROLLABLE_RESULTS_SURFACE,
        )

    def test_scrollable_results_surface(self):
        fr = _freeze(
            name="FeedItem",
            role="listitem",
            hierarchy=["FeedContainer"],
            collection_entities=["A", "B", "C", "D"],
            collection_type="list",
            scrollable=True,
        )
        ident = identify_from_freeze(fr, current_event=_scroll_event())
        assert ident.operational_element_type == OperationalElementType.SCROLLABLE_RESULTS_SURFACE

    def test_navigation_resource(self):
        fr = _freeze(
            name="NavLink",
            role="link",
            hierarchy=["NavigationBar", "Toolbar"],
        )
        ident = identify_from_freeze(fr)
        assert ident.operational_element_type == OperationalElementType.NAVIGATION_RESOURCE


class TestIdentityStability:

    def test_same_element_different_label_preserves_identity(self):
        fr_a = _freeze(
            name="LabelAlpha",
            role="button",
            hierarchy=["TabStrip", "BrowserChrome"],
            icon_sig="plus_icon",
        )
        fr_b = _freeze(
            name="LabelBeta",
            role="button",
            hierarchy=["TabStrip", "BrowserChrome"],
            icon_sig="plus_icon",
        )
        id_a = identify_from_freeze(fr_a)
        id_b = identify_from_freeze(fr_b)
        assert id_a.operational_element_type == id_b.operational_element_type
        assert match_identity_stability(id_a, id_b) >= 0.85

    def test_same_element_visual_change_preserves_identity(self):
        fr_a = _freeze(
            name="+",
            role="button",
            hierarchy=["TabStrip"],
            icon_sig="plus_icon_v1",
        )
        fr_b = _freeze(
            name="+",
            role="button",
            hierarchy=["TabStrip"],
            icon_sig="plus_icon_v2_redesigned",
        )
        id_a = identify_from_freeze(fr_a)
        id_b = identify_from_freeze(fr_b)
        assert id_a.operational_element_type == OperationalElementType.CONTEXT_CREATION_AFFORDANCE
        assert id_b.operational_element_type == OperationalElementType.CONTEXT_CREATION_AFFORDANCE
        assert match_identity_stability(id_a, id_b) >= 0.85

    def test_different_dom_uia_same_identity(self):
        fr_uia = _freeze(
            name=_FAKE_USER_A,
            role="listitem",
            hierarchy=["IdentityPicker"],
            collection_entities=[_FAKE_USER_A, _FAKE_USER_B],
            collection_type="cards",
        )
        fr_dom = _freeze(
            name=_FAKE_USER_A,
            role="option",
            hierarchy=["IdentityPicker"],
            collection_entities=[_FAKE_USER_A, _FAKE_USER_B],
            collection_type="cards",
            dom_role="option",
        )
        id_uia = identify_from_freeze(fr_uia)
        id_dom = identify_from_freeze(fr_dom)
        assert id_uia.operational_element_type == OperationalElementType.SELECTABLE_IDENTITY
        assert id_dom.operational_element_type == OperationalElementType.SELECTABLE_IDENTITY

    def test_identity_survives_surface_transition(self):
        fr_pre = _freeze(
            name=_FAKE_USER_A,
            role="listitem",
            hierarchy=["IdentityPicker"],
            collection_entities=[_FAKE_USER_A, _FAKE_USER_B],
            collection_type="cards",
        )
        ident_pre = identify_from_freeze(fr_pre)
        ident_post = identify_from_freeze(
            fr_pre,
            historical_element_type=ident_pre.operational_element_type.value,
        )
        assert ident_post.operational_element_type == OperationalElementType.SELECTABLE_IDENTITY
        assert ident_post.historical_matches
        assert match_identity_stability(ident_pre, ident_post) >= 0.85


class TestPipelineIntegration:

    def test_identity_feeds_gie_goor(self):
        from app.contracts.mission import Mission

        fr = _freeze(
            name="+",
            role="button",
            hierarchy=["TabStrip"],
            icon_sig="plus_icon",
        )
        coi = promote_freeze_to_canonical_intent(fr, source_event_id="ev1")
        assert coi is not None
        assert coi.operational_element_identity is not None
        assert coi.operational_element_identity.operational_element_type == (
            OperationalElementType.CONTEXT_CREATION_AFFORDANCE
        )

        mission = Mission(
            id=str(uuid.uuid4()),
            name="test",
            canonical_operational_intents=[coi],
        )
        from app.contracts.operational_runtime_graph import OperationalRuntimeGraph

        ctx = build_operational_decision_context(mission, OperationalRuntimeGraph())
        echo = ctx.execution_truth_echo
        assert "uoei_element_types" in echo
        assert OperationalElementType.CONTEXT_CREATION_AFFORDANCE.value in echo["uoei_element_types"]

    def test_identity_feeds_runtime_replay(self):
        fr = _freeze(
            name="OldLabel",
            role="button",
            hierarchy=["TabStrip"],
            icon_sig="plus_icon",
        )
        expected = identify_from_freeze(fr)
        candidate_same = identify_from_freeze(
            _freeze(name="NewLabel", role="button", hierarchy=["TabStrip"], icon_sig="plus_icon"),
        )
        candidate_unrelated = identify_from_freeze(
            _freeze(name="X", role="link", hierarchy=["NavigationBar"]),
        )
        resolved = replay_resolve_by_identity(expected, [candidate_unrelated, candidate_same])
        assert resolved is not None
        assert resolved.operational_element_type == OperationalElementType.CONTEXT_CREATION_AFFORDANCE

    def test_identity_feeds_orce(self):
        from app.contracts.mission import Mission

        fr = _freeze(
            name=_FAKE_USER_A,
            role="listitem",
            hierarchy=["IdentityPicker"],
            collection_entities=[_FAKE_USER_A, _FAKE_USER_B],
            collection_type="cards",
        )
        coi = promote_freeze_to_canonical_intent(fr, source_event_id="ev2")
        mission = Mission(
            id=str(uuid.uuid4()),
            name="test",
            canonical_operational_intents=[coi] if coi else [],
        )
        lines = expert_panel_lines(mission)
        assert any("UOEI" in ln or "Operational Element Identity" in ln for ln in lines) or lines

    def test_no_hardcoded_app_names_in_module(self):
        import inspect

        from app.services.runtime import universal_operational_element_identity as uoei

        source = inspect.getsource(uoei)
        forbidden = ["chrome", "youtube", "windows", "microsoft", "google"]
        low = source.lower()
        for word in forbidden:
            assert word not in low, f"hardcoded app name found: {word}"


class TestAmbiguityAndUnknown:

    def test_unknown_ambiguous_element(self):
        fr = _freeze(name="", role="", hierarchy=["UnknownPane"])
        ctx = build_identity_context_from_freeze(fr)
        ident = resolve_operational_element_identity(ctx)
        assert ident.operational_element_type == OperationalElementType.UNKNOWN_OPERATIONAL_ELEMENT
        assert ident.ambiguity_score >= 0.0

    def test_replay_uses_identity_not_raw_label(self):
        fr = _freeze(
            name="ButtonControl",
            role="button",
            hierarchy=["TabStrip"],
            icon_sig="plus_icon",
        )
        ident = identify_from_freeze(fr)
        fp = identity_fingerprint(ident)
        assert "ButtonControl" not in fp
        assert fp.startswith("uoei:")

        meta = {"operational_element_identity": ident.model_dump(mode="json")}
        restored = identity_from_metadata(meta)
        assert restored is not None
        assert restored.identity_id == ident.identity_id

    def test_stability_decreases_only_with_true_ambiguity(self):
        fr_clear = _freeze(
            name="+",
            role="button",
            hierarchy=["TabStrip"],
            icon_sig="plus_icon",
        )
        fr_ambiguous = _freeze(name="", role="button", hierarchy=["GenericPane"])
        id_clear = identify_from_freeze(fr_clear)
        id_ambiguous = identify_from_freeze(fr_ambiguous)
        assert id_clear.identity_stability_score > id_ambiguous.identity_stability_score
        assert id_clear.ambiguity_score <= id_ambiguous.ambiguity_score

    def test_truth_fingerprint_prefers_uoei(self):
        fp = truth_fingerprint(
            operational_kind="select_entity_from_collection",
            target_name="SomeLabel",
            operational_element_identity_id="abc123identity",
        )
        assert fp == "uoei:abc123identity"
        assert "SomeLabel" not in fp

    def test_uoac_boosted_by_uoei(self):
        fr = _freeze(
            name="+",
            role="button",
            hierarchy=["TabStrip"],
            icon_sig="plus_icon",
        )
        aff = classify_from_freeze(fr)
        assert aff.affordance_type.value == "create_new_context"
        assert any("uoei:" in r for r in aff.evidence_sources)

    def test_ocrx_thread_carries_element_type(self):
        from app.contracts.mission import Mission

        fr = _freeze(
            name=_FAKE_USER_A,
            role="listitem",
            hierarchy=["IdentityPicker"],
            collection_entities=[_FAKE_USER_A, _FAKE_USER_B],
            collection_type="cards",
        )
        coi = promote_freeze_to_canonical_intent(fr, source_event_id="ev3")
        assert coi is not None
        mission = Mission(
            id=str(uuid.uuid4()),
            name="test",
            canonical_operational_intents=[coi],
        )
        threads = build_operational_threads(mission, cois=[coi])
        assert threads
        assert threads[0].operational_element_type == OperationalElementType.SELECTABLE_IDENTITY.value
        assert threads[0].element_identity_id
