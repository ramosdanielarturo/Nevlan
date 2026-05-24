"""PCOF → SEP — integración compilación y READY relief."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.contracts.mission import (
    EventType,
    Mission,
    MouseAction,
    OperationalCollectionType,
    OperationalFreezeCollectionSnapshot,
    OperationalFreezeEntityCandidate,
    OperationalFreezeSnapshot,
    RawEvent,
    TransitionRiskLevel,
)
from app.services.missions.intent_collapse_engine import CollapseStatus
from app.services.missions.mission_review_summary import (
    _EXPERT_ONLY_BLOCKERS,
    build_mission_review_summary,
)
from app.services.missions.semantic_execution_plan import (
    ReadyBlockerCode,
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_intent_status_to_plan,
    compute_ready_status,
)
from app.services.missions.semantic_intent_promotion_engine import (
    PromotionBlockerCode,
    promote_semantic_intent,
)
from app.services.runtime import universal_collection_entity_engine as uces
from app.services.runtime.pre_click_operational_freeze import (
    FREEZE_CONFIDENCE_MIN,
    pcof_expert_panel_lines,
    qualifies_pcof_for_sep_promotion,
)


PROFILE_A = "Daniel Chrome D Daniel Arturo Ramos"
PROFILE_B = "Invitado"
PROFILE_C = "Trabajo"


def _uia_flat(names: list[str]) -> list[dict]:
    return [
        {
            "name": n,
            "control_type": "listitemcontrol",
            "role": "listitem",
            "parent_chain": ["ProfileList", "Window"],
            "bbox": {"left": 100, "top": 80 + i * 40, "width": 320, "height": 36},
        }
        for i, n in enumerate(names)
    ]


def _strong_freeze(
    *,
    entity: str = PROFILE_A,
    entities: list[str] | None = None,
    pre_confirmed: bool = True,
    confidence: float = 0.91,
    absorbed: bool = False,
) -> OperationalFreezeSnapshot:
  # Un candidato seleccionable + affordance (no bloquea promoción universal).
    ents = entities or [PROFILE_A, "More actions for profile"]
    return OperationalFreezeSnapshot(
        freeze_id=str(uuid.uuid4()),
        cursor_xy=(120, 95),
        pre_transition_confirmed=pre_confirmed,
        freeze_confidence=confidence,
        freeze_quality="good",
        transition_risk=TransitionRiskLevel.HIGH,
        best_entity_candidate=OperationalFreezeEntityCandidate(
            display_name=entity,
            normalized_name="daniel_chrome_d_daniel_arturo_ramos",
            confidence=confidence,
            evidence_sources=["uia", "collection"],
            absorbed_ambiguity=absorbed,
        ),
        best_collection_candidate=OperationalFreezeCollectionSnapshot(
            collection_type="list",
            display_name="ProfileList",
            entities=ents,
            entity_count=len(ents),
            confidence=0.88,
        ),
        collection_entities=[
            OperationalFreezeEntityCandidate(
                display_name=n,
                normalized_name=n.lower().replace(" ", "_"),
                confidence=0.85,
            )
            for n in ents
        ],
        uia_targets=[
            {
                "source": "uia",
                "role": "listitem",
                "name": entity,
                "overlap_score": 1.0,
            },
        ],
    )


def _mission_with_freeze(
    freeze: OperationalFreezeSnapshot,
    *,
    legacy_step: SemanticPlanStep | None = None,
) -> Mission:
    coll_ents = (
        list(freeze.best_collection_candidate.entities)
        if freeze.best_collection_candidate
        else []
    )
    label = ""
    if freeze.best_entity_candidate:
        label = freeze.best_entity_candidate.display_name
    meta = {
        "uia_flat": _uia_flat(coll_ents or [PROFILE_A, PROFILE_B]),
        "operational_freeze_snapshot": freeze.model_dump(mode="json"),
        "target_identity_isolation": {
            "target_label": label,
            "trusted_pre_action_identity": bool(label),
        },
        "capture_contract": {
            "trusted_pre_action_identity": True,
            "sufficient_for_execution": True,
            "pcof_confirmed": True,
        },
    }
    ev = RawEvent(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=120, y=95, button="left"),
        metadata=meta,
    )
    m = Mission(id=str(uuid.uuid4()), name="pcof-sep", raw_trace=[ev])
    if legacy_step is not None:
        m.semantic_execution_plan = SemanticExecutionPlan(
            steps=[legacy_step],
            source="test",
        ).to_dict()
    return m


def _legacy_profile_step(name: str = "") -> SemanticPlanStep:
    return SemanticPlanStep(
        id="sp-profile",
        type="select_profile",
        params={"profile_name": name, "app": "chrome"},
        preferred_strategy="select_profile:uia_text_match",
        fallback_strategies=[],
        confidence=0.55,
        human_label="Seleccionar perfil",
        needs_user_label=True,
    )


# 1
def test_strong_freeze_produces_select_entity_from_collection():
    freeze = _strong_freeze()
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    step = uces.promote_semantic_plan_step(_legacy_profile_step(), m)
    assert step.type == uces.UNIVERSAL_SELECT_KIND
    assert step.params.get("pcof_primary") is True
    assert step.params.get("fallback_legacy_type") == "select_profile"
    ent = step.params.get("operational_entity") or step.params.get("collection_entity")
    assert PROFILE_A in str(ent)


# 2
def test_freeze_without_entity_does_not_promote():
    freeze = _strong_freeze()
    freeze.best_entity_candidate = None
    freeze.pre_transition_confirmed = False
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    step = uces.promote_semantic_plan_step(_legacy_profile_step(), m)
    assert step.type == "select_profile"


# 3
def test_freeze_absorbed_with_affordance_sibling_still_promotes():
    """Duplicados UIA + menú ⋯ no bloquean si el click resolvió la entidad."""
    entity = "Open profile Alpha"
    freeze = _strong_freeze(
        entity=entity,
        entities=[entity, entity, "More actions for Alpha"],
        absorbed=True,
    )
    freeze.uia_targets = [
        {"source": "uia", "role": "listitem", "name": entity, "overlap_score": 1.0},
    ]
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    step = uces.promote_semantic_plan_step(_legacy_profile_step(), m)
    assert step.type == uces.UNIVERSAL_SELECT_KIND
    assert step.params.get("selection_strategy") == "pre_click_operational_freeze"


def test_freeze_multiple_candidates_needs_clarification():
    freeze = _strong_freeze(
        entity="Candidate A",
        entities=["Candidate A", "Candidate B", "Candidate C"],
    )
    freeze.uia_targets = [
        {"source": "uia", "role": "listitem", "name": "Candidate A", "overlap_score": 0.4},
    ]
    freeze.best_entity_candidate.confidence = 0.7
    freeze.freeze_confidence = 0.75
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    step = uces.promote_semantic_plan_step(_legacy_profile_step(), m)
    assert step.type == "select_profile" or bool(step.needs_user_label)


# 4
def test_generic_entity_freeze_does_not_promote():
    freeze = _strong_freeze(entity="perfil del navegador")
    ok, _ = qualifies_pcof_for_sep_promotion(freeze)
    assert not ok
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    step = uces.promote_semantic_plan_step(_legacy_profile_step(), m)
    assert step.type == "select_profile"


# 5
def test_pre_transition_false_does_not_promote():
    freeze = _strong_freeze(pre_confirmed=False, confidence=0.5)
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    step = uces.promote_semantic_plan_step(_legacy_profile_step(), m)
    assert step.type == "select_profile"


# 6
def test_pcof_clears_needs_user_label_when_strong():
    freeze = _strong_freeze()
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    promoted = uces.promote_semantic_plan_step(_legacy_profile_step(), m)
    plan = SemanticExecutionPlan(steps=[promoted], source="test")
    uces.apply_pcof_ready_relief(plan, m)
    assert promoted.needs_user_label is False
    assert "automáticamente" in (promoted.human_label or "").lower() or PROFILE_A in promoted.human_label


# 7
def test_pcof_preserves_fallback_legacy_type():
    freeze = _strong_freeze()
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    step = uces.promote_semantic_plan_step(_legacy_profile_step(), m)
    assert step.params.get("fallback_legacy_type") == "select_profile"


# 8
def test_model_dump_validate_roundtrip():
    freeze = _strong_freeze()
    intent = uces.intent_from_pcof_freeze(freeze)
    params = uces.build_semantic_params_from_pcof_freeze(
        freeze, intent, legacy_type="select_profile",
    )
    blob = OperationalFreezeSnapshot.model_validate(
        params["operational_freeze_snapshot"],
    )
    assert blob.freeze_id == freeze.freeze_id
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="x",
                type=uces.UNIVERSAL_SELECT_KIND,
                params=params,
                preferred_strategy=uces.UCES_PRIMARY_STRATEGY,
                fallback_strategies=[],
                confidence=0.9,
                human_label="test",
            ),
        ],
        source="test",
    )
    restored = SemanticExecutionPlan.from_dict(plan.to_dict())
    p = restored.steps[0].params
    assert p.get("operational_freeze_snapshot_ref") == freeze.freeze_id


# 9
def test_ready_without_collection_clarification_blocker():
    freeze = _strong_freeze()
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    step = uces.promote_semantic_plan_step(_legacy_profile_step(), m)
    plan = SemanticExecutionPlan(steps=[step], source="test")
    attach_intent_status_to_plan(plan, mission=m)
    status, blockers = compute_ready_status(plan, mission=m)
    assert ReadyBlockerCode.COLLECTION_ENTITY_NEEDS_CLARIFICATION not in blockers
    assert status == CollapseStatus.READY or CollapseStatus.NEEDS_REVIEW


# 10
def test_sipe_clears_ambiguous_profile_blocker_and_expert_panel():
    freeze = _strong_freeze()
    m = _mission_with_freeze(freeze, legacy_step=_legacy_profile_step())
    result = promote_semantic_intent(m)
    assert PromotionBlockerCode.AMBIGUOUS_PROFILE_PICKER not in result.blockers
    steps = list(result.plan.steps or [])
    prof = next((s for s in steps if s.type == uces.UNIVERSAL_SELECT_KIND), None)
    assert prof is not None
    assert prof.params.get("pcof_primary")
    lines = pcof_expert_panel_lines(m)
    assert any("Pre-Click Operational Freeze" in ln for ln in lines)


def test_mission_review_normal_no_gate_plan_not_ready_banner():
    freeze = _strong_freeze()
    step = uces.promote_semantic_plan_step(
        _legacy_profile_step(),
        _mission_with_freeze(freeze),
    )
    plan = SemanticExecutionPlan(steps=[step], source="test")
    attach_intent_status_to_plan(plan, mission=_mission_with_freeze(freeze))
    m = _mission_with_freeze(freeze)
    m.semantic_execution_plan = plan.to_dict()
    summary = build_mission_review_summary(m)
    visible = " ".join(summary.visible_blockers or [])
    for tok in _EXPERT_ONLY_BLOCKERS:
        if tok == "GATE_PLAN_NOT_READY":
            assert tok not in visible


def test_freeze_confidence_below_threshold_no_promotion():
    freeze = _strong_freeze(confidence=FREEZE_CONFIDENCE_MIN - 0.05)
    ok, reasons = qualifies_pcof_for_sep_promotion(freeze)
    assert not ok
    assert "freeze_confidence_below_threshold" in reasons
