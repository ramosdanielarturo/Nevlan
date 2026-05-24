"""Promoción universal PCOF → SEP (sin hardcode por app/perfil)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.contracts.mission import (
    EventType,
    Mission,
    MouseAction,
    OperationalFreezeCollectionSnapshot,
    OperationalFreezeEntityCandidate,
    OperationalFreezeSnapshot,
    RawEvent,
    TransitionRiskLevel,
)
from app.services.missions.intent_collapse_engine import collapse_intent
from app.services.missions.semantic_execution_plan import (
    ReadyBlockerCode,
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_intent_status_to_plan,
    compute_ready_status,
)
from app.services.missions.semantic_intent_promotion_engine import (
    PromotionBlockerCode,
    apply_promotion_to_mission,
)
from app.services.runtime import universal_collection_entity_engine as uces
from app.services.runtime.pre_click_operational_freeze import (
    qualifies_pcof_for_sep_promotion,
)


def _profile_picker_freeze(
    *,
    entity: str,
    siblings: list[str],
    absorbed: bool = False,
) -> OperationalFreezeSnapshot:
    return OperationalFreezeSnapshot(
        freeze_id=str(uuid.uuid4()),
        cursor_xy=(100, 100),
        pre_transition_confirmed=True,
        freeze_confidence=0.95,
        freeze_quality="excellent",
        transition_risk=TransitionRiskLevel.HIGH,
        best_entity_candidate=OperationalFreezeEntityCandidate(
            display_name=entity,
            normalized_name=entity.lower().replace(" ", "_"),
            confidence=1.0,
            evidence_sources=["uia", "collection"],
            absorbed_ambiguity=absorbed,
            bbox={"left": 100, "top": 80, "width": 200, "height": 40},
        ),
        best_collection_candidate=OperationalFreezeCollectionSnapshot(
            collection_type="list",
            display_name="Window",
            entities=siblings,
            entity_count=len(siblings),
            confidence=0.8,
        ),
        uia_targets=[
            {
                "source": "uia",
                "role": "listitem",
                "name": entity,
                "overlap_score": 1.0,
            },
        ],
    )


def _mission_with_freeze_and_legacy(freeze: OperationalFreezeSnapshot) -> Mission:
    ev = RawEvent(
        id="ev-pcof-1",
        timestamp=datetime.now(timezone.utc),
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=100, y=100, button="left"),
        metadata={
            "operational_freeze_snapshot": freeze.model_dump(mode="json"),
        },
    )
    m = Mission(id=str(uuid.uuid4()), name="pcof-universal", raw_trace=[ev])
    m.semantic_execution_plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="open",
                type="open_app",
                params={"name": "browser", "method": "windows_search"},
                preferred_strategy="open_app:windows_search",
                fallback_strategies=[],
                confidence=0.9,
                human_label="Abrir app",
            ),
            SemanticPlanStep(
                id="legacy-sel",
                type="select_profile",
                params={"profile_name": "", "app": "browser"},
                preferred_strategy="select_profile:uia_text_match",
                fallback_strategies=[],
                confidence=0.75,
                human_label="Seleccionar perfil",
                needs_user_label=True,
            ),
        ],
        source="intent_collapse_engine",
    ).to_dict()
    return m


def test_strong_pcof_promotes_to_select_entity_from_collection():
    entity = "Open profile Alpha"
    freeze = _profile_picker_freeze(
        entity=entity,
        siblings=[entity, entity, "More actions for Alpha"],
        absorbed=True,
    )
    ok, _ = qualifies_pcof_for_sep_promotion(freeze)
    assert ok
    m = _mission_with_freeze_and_legacy(freeze)
    result = apply_promotion_to_mission(m)
    step = next(s for s in result.plan.steps if s.type == uces.UNIVERSAL_SELECT_KIND)
    assert step.params.get("selection_strategy") == "pre_click_operational_freeze"
    assert step.params.get("fallback_legacy_type") == "select_profile"
    assert step.params.get("source_event_id") == "ev-pcof-1"
    assert entity in str(step.params.get("collection_entity"))


def test_no_chrome_or_profile_name_dependency():
    entity = "Row item 7"
    freeze = _profile_picker_freeze(
        entity=entity,
        siblings=[entity, "Row item 8"],
        absorbed=False,
    )
    m = _mission_with_freeze_and_legacy(freeze)
    legacy = SemanticPlanStep(
        id="x",
        type="select_item",
        params={},
        preferred_strategy="",
        fallback_strategies=[],
        confidence=0.5,
        human_label="",
    )
    step = uces.promote_semantic_plan_step(legacy, m)
    assert step.type == uces.UNIVERSAL_SELECT_KIND


def test_pcof_clears_empty_profile_blocker():
    entity = "Open profile Alpha"
    freeze = _profile_picker_freeze(
        entity=entity,
        siblings=[entity, "More actions for Alpha"],
        absorbed=True,
    )
    m = _mission_with_freeze_and_legacy(freeze)
    result = apply_promotion_to_mission(m)
    assert PromotionBlockerCode.AMBIGUOUS_PROFILE_PICKER not in result.blockers
    plan = result.plan
    attach_intent_status_to_plan(plan, mission=m)
    _, blockers = compute_ready_status(plan, mission=m)
    assert ReadyBlockerCode.EMPTY_PROFILE_NAME_IN_SELECT_PROFILE not in blockers


def test_weak_pcof_does_not_promote():
    freeze = _profile_picker_freeze(
        entity="perfil del navegador",
        siblings=["perfil del navegador", "otro"],
    )
    ok, reasons = qualifies_pcof_for_sep_promotion(freeze)
    assert not ok
    assert "generic_entity_label" in reasons


def test_multiple_real_candidates_block_promotion():
    freeze = _profile_picker_freeze(
        entity="Candidate A",
        siblings=["Candidate A", "Candidate B"],
    )
    freeze.uia_targets = [
        {"source": "uia", "role": "listitem", "name": "Candidate A", "overlap_score": 0.5},
        {"source": "uia", "role": "listitem", "name": "Candidate B", "overlap_score": 0.5},
    ]
    freeze.best_entity_candidate.confidence = 0.7
    freeze.freeze_confidence = 0.75
    ok, reasons = qualifies_pcof_for_sep_promotion(freeze)
    assert not ok
    assert "multiple_entity_candidates" in reasons


def test_ice_skips_select_profile_when_pcof_resolved():
    from app.services.missions.intent_collapse_engine import (
        _Context,
        _detect_select_profile,
    )

    ctx = _Context(profile_picker_seen=True, strong_pcof_entity_resolved=True)
    assert _detect_select_profile(ctx) is None
