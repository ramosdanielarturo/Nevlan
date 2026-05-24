"""Tests — Universal Operational Language (UOL)."""

from __future__ import annotations

import uuid

import pytest

from app.contracts.mission import (
    CanonicalOperationalIntent,
    CoiTruthOrigin,
    Mission,
    OperationalFreezeEntityCandidate,
    OperationalFreezeSnapshot,
    TransitionRiskLevel,
)
from app.services.missions.freeze_first_execution_core import (
    FFEC_ENTITY_CONFIDENCE_MIN,
    human_ready_label_for_coi,
    promote_freeze_to_canonical_intent,
)
from app.services.missions.mission_review_summary import build_mission_review_summary
from app.services.missions.universal_operational_language import (
    UniversalOperationalActionType,
    build_universal_execution_contract,
    compile_canonical_intent_to_uol,
    compile_semantic_step_to_uol,
    contains_technical_label_noise,
    enrich_mission_step_with_uol,
    human_operational_label_for_review_step,
    human_operational_label_for_uol,
    normalize_display_name,
    resolve_uol_runtime_strategies,
    should_suppress_ask_for_uol,
)
from app.services.missions.execution_contracts import MissionStep


def _coi_profile(name: str = "Daniel Chrome", confidence: float = 0.95) -> CanonicalOperationalIntent:
    freeze = OperationalFreezeSnapshot(
        pre_transition_confirmed=True,
        freeze_confidence=confidence,
        best_entity_candidate=OperationalFreezeEntityCandidate(
            display_name=name,
            normalized_name="daniel chrome",
            entity_type_hint="user_profile",
            confidence=confidence,
        ),
    )
    coi = promote_freeze_to_canonical_intent(freeze, source_event_id="ev-profile")
    assert coi is not None
    return coi


# ── Compilation ─────────────────────────────────────────────────────────────


def test_compile_coi_select_entity_from_collection():
    coi = _coi_profile()
    action = compile_canonical_intent_to_uol(coi)
    assert action.uol_action == UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION
    assert action.canonical_truth is True
    assert action.target_display_name == "Daniel Chrome"
    assert action.surface_type.value == "collection_surface"


def test_compile_open_app():
    step = {"type": "open_app", "params": {"name": "chrome"}}
    action = compile_semantic_step_to_uol(step)
    assert action is not None
    assert action.uol_action == UniversalOperationalActionType.OPEN_APPLICATION
    label = human_operational_label_for_uol(action)
    assert label == "Abrir Chrome"
    assert "smart_route" not in label.lower()


def test_compile_search_content():
    step = {
        "type": "search_content",
        "params": {"query": "Devuélveme el amor Luis Miguel"},
    }
    action = compile_semantic_step_to_uol(step)
    assert action is not None
    assert action.uol_action == UniversalOperationalActionType.SEARCH_CONTENT
    assert action.input_text == "Devuélveme el amor Luis Miguel"
    label = human_operational_label_for_uol(action)
    assert "Devuélveme el amor Luis Miguel" in label
    assert label.startswith('Buscar "')


def test_compile_open_tab():
    step = {"type": "open_new_tab", "params": {}}
    action = compile_semantic_step_to_uol(step)
    assert action.uol_action == UniversalOperationalActionType.OPEN_TAB
    assert human_operational_label_for_uol(action) == "Abrir nueva pestaña"


def test_compile_navigate_to_location():
    step = {"type": "open_url", "params": {"url": "https://www.youtube.com/"}}
    action = compile_semantic_step_to_uol(step)
    assert action.uol_action == UniversalOperationalActionType.NAVIGATE_TO_LOCATION


def test_normalize_display_name_strips_profile_prefix():
    assert normalize_display_name("Abrir el perfil de Daniel Chrome") == "Daniel Chrome"


# ── Execution contract ────────────────────────────────────────────────────────


def test_search_content_execution_contract_steps():
    action = compile_semantic_step_to_uol({
        "type": "search_content",
        "params": {"query": "test"},
    })
    assert action is not None
    ec = build_universal_execution_contract(action)
    assert ec.locate_input_surface is True
    assert ec.inject_text is True
    assert ec.submit is True
    assert ec.validate_results is True
    assert "validar_query_visible" in ec.execution_steps
    assert "query_visible" in ec.validators


def test_select_entity_execution_contract():
    coi = _coi_profile()
    action = compile_canonical_intent_to_uol(coi)
    ec = action.execution_contract
    assert ec.locate_collection is True
    assert ec.select_entity is True
    assert ec.validate_entity_selected is True


def test_runtime_strategies_prioritize_uol_over_smart_route():
    step = MissionStep(
        id="s1",
        kind="search_content",
        params={
            "uol_action": "search_content",
            "uol_execution_contract": {
                "preferred_runtime_strategies": ["search_content:uol_surface"],
                "fallback_runtime_strategies": ["search_content:dom_input"],
            },
            "uol_canonical_truth": True,
        },
    )
    legacy = [
        "search_content:smart_route",
        "search_site:smart_route",
        "search_content:dom_input",
    ]
    resolved = resolve_uol_runtime_strategies(step, legacy)
    assert resolved[0] == "search_content:uol_surface"
    assert not any("smart_route" in s for s in resolved)


# ── Human rendering ───────────────────────────────────────────────────────────


def test_profile_human_label_not_automatic_element():
    coi = _coi_profile("Daniel Chrome")
    label = human_ready_label_for_coi(coi)
    assert label == "Seleccionar perfil Daniel Chrome"
    assert "automáticamente" not in label.lower()
    assert "uia_text_match" not in label.lower()


def test_ffec_human_label_via_uol():
    coi = _coi_profile()
    action = compile_canonical_intent_to_uol(coi)
    assert human_operational_label_for_uol(action) == "Seleccionar perfil Daniel Chrome"


def test_no_technical_labels_in_uol_rendering():
    coi = _coi_profile()
    label = human_operational_label_for_review_step(
        {"type": "select_profile", "params": {"profile_name": "Daniel"}},
        mission=None,
    )
    # Sin COI en misión, usa compile_semantic_step
    assert label is not None
    assert not contains_technical_label_noise(label)
    assert "smart_route" not in (label or "").lower()

    coi_label = human_operational_label_for_review_step(
        {"type": "select_entity_from_collection", "params": {}, "source_event_id": "ev-profile"},
        mission=Mission(
            id=str(uuid.uuid4()),
            name="test-mission",
            canonical_operational_intents=[coi],
        ),
    )
    assert coi_label == "Seleccionar perfil Daniel Chrome"


# ── Ask elimination ───────────────────────────────────────────────────────────


def test_should_suppress_ask_with_canonical_truth():
    assert should_suppress_ask_for_uol(
        canonical_truth=True,
        entity_confidence=FFEC_ENTITY_CONFIDENCE_MIN,
        candidate_count=1,
        needs_clarification=False,
    )


def test_should_not_suppress_ask_when_ambiguity_critical():
    assert not should_suppress_ask_for_uol(
        canonical_truth=True,
        entity_confidence=0.95,
        ambiguity_critical=True,
    )


def test_mission_review_summary_uses_uol_labels():
    mission = Mission(
        id=str(uuid.uuid4()),
        name="chrome-youtube-flow",
        semantic_execution_plan={
            "intent_status": "READY",
            "ready_provenance": "raw_evidence",
            "steps": [
                {
                    "id": "s1",
                    "type": "open_app",
                    "params": {"name": "chrome"},
                    "human_label": "open_app:smart_route",
                },
                {
                    "id": "s2",
                    "type": "select_entity_from_collection",
                    "params": {
                        "collection_entity": {
                            "display_name": "Daniel Chrome",
                            "entity_type": "user_profile",
                        },
                        "selection_confidence": 0.95,
                        "candidate_count": 1,
                        "canonical_truth": True,
                    },
                    "human_label": "Elemento identificado automáticamente",
                },
                {
                    "id": "s3",
                    "type": "open_new_tab",
                    "params": {},
                    "human_label": "open_new_tab",
                },
                {
                    "id": "s4",
                    "type": "search_content",
                    "params": {"query": "Devuélveme el amor Luis Miguel"},
                    "human_label": "search_content:smart_route",
                },
            ],
        },
        canonical_operational_intents=[_coi_profile()],
    )
    summary = build_mission_review_summary(mission, recompute=False)
    labels = [s.human_label for s in summary.steps]
    assert labels[0] == "Abrir Chrome"
    assert "Daniel Chrome" in labels[1]
    assert "automáticamente" not in labels[1].lower()
    assert labels[2] == "Abrir nueva pestaña"
    assert "Devuélveme el amor Luis Miguel" in labels[3]
    for lab in labels:
        assert "smart_route" not in lab.lower()


# ── Runtime enrichment ────────────────────────────────────────────────────────


def test_enrich_mission_step_attaches_uol_params():
    step = MissionStep(
        id="s1",
        kind="search_content",
        params={"query": "hello"},
        human_label="search_content:smart_route",
    )
    enriched = enrich_mission_step_with_uol(step)
    assert enriched.params.get("uol_action") == "search_content"
    assert enriched.params.get("preferred_strategy")
    assert "smart_route" not in (enriched.human_label or "").lower()
    assert "hello" in (enriched.human_label or "")


def test_uol_to_step_params_roundtrip():
    coi = _coi_profile()
    action = compile_canonical_intent_to_uol(coi)
    params = action.to_step_params()
    assert params["uol_action"] == "select_entity_from_collection"
    assert params["uol_canonical_truth"] is True
    assert params.get("preferred_strategy")
