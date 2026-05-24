"""Tests — Universal Operational Affordance Classifier (UOAC)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    OperationalAffordanceType,
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
from app.services.missions.mission_review_summary import build_mission_review_summary
from app.services.missions.universal_operational_affordance_classifier import (
    build_context_from_freeze,
    classify_from_freeze,
    classify_from_raw_event,
    classify_operational_affordance,
)
from app.services.missions.universal_operational_language import (
    compile_canonical_intent_to_uol,
    human_operational_label_for_uol,
)

_FAKE_APP = "FictionalApp"
_FAKE_SITE = "example-fictional.test"
_FAKE_USER = "User Alpha Nine"


def _freeze(
    *,
    name: str,
    role: str = "button",
    hierarchy: list[str] | None = None,
    collection_entities: list[str] | None = None,
    collection_type: str = "list",
    icon_sig: str = "",
    surface_proc: str = "fictional.exe",
) -> OperationalFreezeSnapshot:
    ents = collection_entities or []
    coll = None
    if len(ents) > 1:
        coll = OperationalFreezeCollectionSnapshot(
            collection_type=collection_type,
            display_name="IdentityList",
            entities=ents,
            entity_count=len(ents),
            confidence=0.9,
        )
    return OperationalFreezeSnapshot(
        freeze_id=str(uuid.uuid4()),
        pre_transition_confirmed=True,
        freeze_confidence=0.92,
        transition_risk=TransitionRiskLevel.MEDIUM,
        window_context={"process_name": surface_proc, "title": _FAKE_APP},
        hierarchy_chain=hierarchy or ["MainWindow"],
        visual_group_signature=icon_sig,
        best_entity_candidate=OperationalFreezeEntityCandidate(
            display_name=name,
            normalized_name=name.lower().replace(" ", "_"),
            confidence=0.94,
            evidence_sources=["uia"],
        ),
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
        ],
    )


def _type_event(text: str) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        timestamp=datetime.now(timezone.utc),
        keyboard_action=KeyboardAction(keys=[c for c in text if c.strip()]),
        metadata={"final_text": text},
    )


def _enter_event() -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_KEY_PRESS,
        timestamp=datetime.now(timezone.utc),
        keyboard_action=KeyboardAction(keys=["enter"]),
    )


def _scroll_event() -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_SCROLL,
        timestamp=datetime.now(timezone.utc),
    )


class TestStructuralClassification:

    def test_plus_in_tab_bar_create_new_context(self):
        fr = _freeze(
            name="+",
            role="button",
            hierarchy=["TabStrip", "BrowserChrome"],
            icon_sig="plus_icon",
        )
        aff = classify_from_freeze(fr)
        assert aff.affordance_type == OperationalAffordanceType.CREATE_NEW_CONTEXT
        assert aff.uol_action_hint == "open_tab"
        assert "perfil" not in aff.human_label_hint.lower()

    def test_launcher_tile_navigate_or_activate(self):
        fr = _freeze(
            name=_FAKE_APP,
            role="listitem",
            hierarchy=["AppLauncher", "SearchHost"],
            collection_entities=[_FAKE_APP, "Other Tile"],
            surface_proc="SearchHost.exe",
        )
        aff = classify_from_freeze(fr)
        assert aff.affordance_type in (
            OperationalAffordanceType.ACTIVATE_LAUNCHER,
            OperationalAffordanceType.NAVIGATE_TO_RESOURCE,
            OperationalAffordanceType.CHOOSE_OPTION,
            OperationalAffordanceType.SELECT_IDENTITY_CONTEXT,
        )
        assert "YouTube" not in aff.human_label_hint
        assert "Chrome" not in aff.human_label_hint

    def test_identity_card_in_collection(self):
        fr = _freeze(
            name=_FAKE_USER,
            role="listitem",
            hierarchy=["ProfileList", "PickerDialog"],
            collection_entities=[_FAKE_USER, "Guest Slot"],
            collection_type="profile_list",
        )
        aff = classify_from_freeze(fr)
        assert aff.affordance_type == OperationalAffordanceType.SELECT_IDENTITY_CONTEXT
        assert _FAKE_USER in aff.human_label_hint

    def test_input_plus_typing_search_with_query(self):
        fr = _freeze(
            name="Escribe aquí para buscar",
            role="edit",
            hierarchy=["SearchSurface"],
        )
        aff = classify_from_freeze(fr, next_event=_type_event("fictional query"))
        assert aff.affordance_type in (
            OperationalAffordanceType.SEARCH_WITH_QUERY,
            OperationalAffordanceType.FOCUS_INPUT,
        )
        assert aff.should_merge_with_adjacent_events
        assert "Seleccionar Escribe" not in aff.human_label_hint

    def test_enter_merges_with_search(self):
        fr = _freeze(name="Search field", role="edit", hierarchy=["SearchSurface"])
        aff = classify_from_freeze(fr, next_event=_enter_event())
        assert aff.affordance_type in (
            OperationalAffordanceType.SUBMIT_INPUT,
            OperationalAffordanceType.SEARCH_WITH_QUERY,
        )
        assert aff.should_merge_with_adjacent_events or aff.affordance_type == (
            OperationalAffordanceType.SUBMIT_INPUT
        )

    def test_scroll_browse_content(self):
        aff = classify_from_raw_event(_scroll_event())
        assert aff.affordance_type == OperationalAffordanceType.BROWSE_CONTENT

    def test_unlabeled_button_unknown_not_invented(self):
        fr = _freeze(name="", role="button", hierarchy=["Pane"])
        aff = classify_from_freeze(fr)
        assert aff.affordance_type == OperationalAffordanceType.UNKNOWN
        assert not aff.should_materialize_as_step or aff.confidence <= 0.55

    def test_no_product_literal_strings_in_classifier(self):
        import ast
        import app.services.missions.universal_operational_affordance_classifier as mod

        tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
        literals: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value.lower())
        blob = "\n".join(literals)
        for token in ("youtube", "chrome", "daniel"):
            assert token not in blob


class TestFictionalSiteFlow:

    def test_fictional_site_navigation_label(self):
        fr = _freeze(
            name=_FAKE_SITE,
            role="hyperlink",
            hierarchy=["NavigationBar", _FAKE_APP],
        )
        aff = classify_from_freeze(fr)
        assert aff.affordance_type in (
            OperationalAffordanceType.NAVIGATE_TO_RESOURCE,
            OperationalAffordanceType.UNKNOWN,
        )
        assert "YouTube" not in (aff.human_label_hint or "")


class TestFfecUolIntegration:

    def test_input_label_does_not_compile_to_select_entity(self):
        fr = _freeze(
            name="Nueva pestaña",
            role="tabitem",
            hierarchy=["TabStrip", "BrowserChrome"],
        )
        coi = promote_freeze_to_canonical_intent(fr, source_event_id="ev-tab")
        assert coi is not None
        assert coi.operational_affordance is not None
        assert coi.operational_affordance.affordance_type in (
            OperationalAffordanceType.CREATE_NEW_CONTEXT,
            OperationalAffordanceType.UNKNOWN,
        )
        action = compile_canonical_intent_to_uol(coi)
        assert action.uol_action.value != "select_entity_from_collection"
        hl = human_operational_label_for_uol(action)
        assert "Seleccionar Nueva" not in hl

    def test_search_placeholder_not_select_entity(self):
        fr = _freeze(
            name="Escribe aquí para buscar",
            role="edit",
            hierarchy=["SearchBox"],
        )
        coi = promote_freeze_to_canonical_intent(
            fr,
            source_event_id="ev-s",
            next_event=_type_event("alpha beta"),
        )
        assert coi is not None
        action = compile_canonical_intent_to_uol(coi)
        assert action.uol_action.value in (
            "fill_field",
            "search_content",
            "wait_for_dynamic_state",
        )
        hl = human_operational_label_for_uol(action)
        assert "Seleccionar Escribe" not in hl

    def test_identity_still_select_entity_uol(self):
        fr = _freeze(
            name=_FAKE_USER,
            role="listitem",
            hierarchy=["AccountPicker"],
            collection_entities=[_FAKE_USER, "Guest"],
        )
        coi = promote_freeze_to_canonical_intent(fr, source_event_id="ev-id")
        assert coi is not None
        assert coi.entity is not None
        action = compile_canonical_intent_to_uol(coi)
        assert action.uol_action.value == "select_entity_from_collection"


class TestMissionReviewFromAffordance:

    def test_review_uses_uoac_not_raw_uia_for_tab(self):
        from app.contracts.mission import Mission
        from app.services.missions.semantic_execution_plan import (
            SemanticExecutionPlan,
            SemanticPlanStep,
        )

        fr = _freeze(
            name="Nueva pestaña",
            role="tabitem",
            hierarchy=["TabStrip"],
        )
        coi = promote_freeze_to_canonical_intent(fr, source_event_id="ev1")
        assert coi and coi.operational_affordance
        mission = Mission(
            id=str(uuid.uuid4()),
            name="uoac-review",
            canonical_operational_intents=[coi],
            semantic_execution_plan=SemanticExecutionPlan(
                steps=[
                    SemanticPlanStep(
                        id="s1",
                        type="open_new_tab",
                        params={
                            "canonical_truth": True,
                            "canonical_operational_intent": coi.model_dump(mode="json"),
                            "uol_action": coi.operational_affordance.uol_action_hint,
                        },
                        preferred_strategy="open_new_tab:uol",
                        fallback_strategies=[],
                        confidence=0.9,
                        human_label="",
                    ),
                ],
                source="semantic_intent_promotion_engine",
            ).to_dict(),
        )
        summary = build_mission_review_summary(mission, recompute=True)
        labels = " ".join(s.human_label for s in summary.steps)
        assert "Seleccionar Nueva pestaña" not in labels
