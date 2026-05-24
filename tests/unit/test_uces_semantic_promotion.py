"""UCES — promoción primaria en compilación (SIPE/TEL/SEP)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.contracts.mission import (
    EventType,
    Mission,
    MouseAction,
    OperationalCollectionType,
    RawEvent,
)
from app.services.missions.intent_collapse_engine import CollapseStatus
from app.services.missions.semantic_execution_plan import (
    ReadyBlockerCode,
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_intent_status_to_plan,
    compute_ready_status,
)
from app.services.missions.semantic_intent_promotion_engine import promote_semantic_intent
from app.services.missions.tel_sep_promoter import (
    build_sep_from_truth_blocks,
    map_truth_block_to_sep_step,
)
from app.services.missions.truth_extraction_layer import extract_truth_blocks
from app.services.missions.semantic_adapter import adapt_mission_to_steps
from app.services.runtime import universal_collection_entity_engine as uces
from app.services.runtime.entity_resolution_layer import is_generic_entity_label


PROFILE_NAMES = [
    "Daniel Chrome D Daniel Arturo Ramos",
    "Guest Profile",
    "Work Account",
]


def _uia_nodes(names: list) -> list:
    return [
        {
            "name": n,
            "control_type": "listitemcontrol",
            "parent_chain": ["ProfilePicker", "Window"],
            "bbox": {"left": 10, "top": 100 + i * 40, "width": 200, "height": 32},
            "children": [],
        }
        for i, n in enumerate(names)
    ]


def _click_event(name: str, *, x: int = 50, y: int = 108) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=x, y=y, button="left"),
        metadata={
            "uia_flat": _uia_nodes(PROFILE_NAMES),
            "target_identity_isolation": {"target_label": name, "trusted_pre_action_identity": True},
        },
    )


def _mission_with_profile_click(profile_name: str = PROFILE_NAMES[0]) -> Mission:
    ev = _click_event(profile_name)
    return Mission(
        id=str(uuid.uuid4()),
        name="uces_promo_test",
        raw_trace=[ev],
    )


def _strong_intent(profile_name: str = PROFILE_NAMES[0]):
    ev = _click_event(profile_name)
    return uces.build_selection_from_capture(
        raw_event=ev,
        capture_bundle={
            "metadata": dict(ev.metadata or {}),
            "execution_truth_confirmed": True,
        },
    )


def _legacy_select_profile_step(profile_name: str = "") -> SemanticPlanStep:
    return SemanticPlanStep(
        id="sp1",
        type="select_profile",
        params={"profile_name": profile_name, "app": "chrome"},
        preferred_strategy="select_profile:uia_text_match",
        fallback_strategies=["select_profile:visible_text_ocr"],
        confidence=0.7,
        human_label="Seleccionar perfil",
        needs_user_label=not bool(profile_name),
    )


@pytest.mark.parametrize("legacy_type", [
    "select_profile",
    "choose_option",
    "click_result",
    "select_tab",
])
def test_legacy_types_promoted_when_uces_strong(legacy_type):
    intent = _strong_intent()
    assert intent.selected
    mission = _mission_with_profile_click()
    step = SemanticPlanStep(
        id="x",
        type=legacy_type,
        params={
            "profile_name": PROFILE_NAMES[0],
            "label": PROFILE_NAMES[0],
            "choice_label": PROFILE_NAMES[0],
        },
        preferred_strategy=f"{legacy_type}:test",
        fallback_strategies=[],
        confidence=0.7,
        human_label="legacy",
        needs_user_label=True,
    )
    promoted = uces.promote_semantic_plan_step(step, mission)
    assert promoted.type == uces.UNIVERSAL_SELECT_KIND
    assert promoted.params.get("operational_collection")
    assert promoted.params.get("collection_entity")
    assert promoted.params.get("fallback_legacy_type") == legacy_type
    assert promoted.needs_user_label is False


def test_table_row_promoted_via_uces():
    nodes = [
        {"name": "Acme Corporation", "control_type": "dataitem", "parent_chain": ["Table", "Grid"]},
        {"name": "Beta Industries", "control_type": "dataitem", "parent_chain": ["Table", "Grid"]},
        {"name": "Gamma Logistics", "control_type": "dataitem", "parent_chain": ["Table", "Grid"]},
    ]
    target = "Beta Industries"
    ev = RawEvent(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=20, y=20, button="left"),
        metadata={
            "uia_flat": nodes,
            "target_identity_isolation": {"target_label": target, "trusted_pre_action_identity": True},
        },
    )
    mission = Mission(id=str(uuid.uuid4()), name="table", raw_trace=[ev])
    step = SemanticPlanStep(
        id="t1",
        type="click_result",
        params={"label": target},
        preferred_strategy="click_result:x",
        fallback_strategies=[],
        confidence=0.65,
        human_label="click",
        needs_user_label=False,
    )
    promoted = uces.promote_semantic_plan_step(step, mission)
    assert promoted.type == uces.UNIVERSAL_SELECT_KIND


def test_cards_collection_promoted():
    names = [
        "Sunrise Apartment Listing",
        "Harbor View Condo",
        "Mountain Cabin Retreat",
        "Downtown Loft Studio",
    ]
    nodes = [
        {"name": n, "control_type": "article", "parent_chain": ["Feed"]}
        for n in names
    ]
    target = names[1]
    ev = RawEvent(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=5, y=5, button="left"),
        metadata={
            "uia_flat": nodes,
            "target_identity_isolation": {"target_label": target, "trusted_pre_action_identity": True},
        },
    )
    mission = Mission(id=str(uuid.uuid4()), name="cards", raw_trace=[ev])
    step = SemanticPlanStep(
        id="c1",
        type="choose_option",
        params={"label": target},
        preferred_strategy="choose_option:list_select",
        fallback_strategies=[],
        confidence=0.66,
        human_label="opt",
        needs_user_label=False,
    )
    promoted = uces.promote_semantic_plan_step(step, mission)
    assert promoted.type == uces.UNIVERSAL_SELECT_KIND
    coll = promoted.params.get("operational_collection") or {}
    assert coll.get("collection_type") in (
        OperationalCollectionType.CARDS.value,
        OperationalCollectionType.LIST.value,
        OperationalCollectionType.UNKNOWN.value,
    )


def test_candidate_count_one_ready():
    intent = _strong_intent()
    params = uces.build_semantic_params_from_intent(intent, legacy_type="select_profile")
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="r1",
                type=uces.UNIVERSAL_SELECT_KIND,
                params=params,
                preferred_strategy=uces.UCES_PRIMARY_STRATEGY,
                fallback_strategies=list(uces.UCES_FALLBACK_STRATEGIES),
                confidence=0.92,
                human_label=uces.human_label_for_uces_step(params),
                needs_user_label=False,
            ),
        ],
        coords_used=False,
        legacy_graph_ignored=True,
    )
    mission = _mission_with_profile_click()
    mission.semantic_execution_plan = plan.to_dict()
    mission.legacy_compiled_graph_purpose = "legacy_debug_only"
    mission._dropped_semantic_events = []
    attach_intent_status_to_plan(plan, mission=mission)
    assert plan.intent_status == CollapseStatus.READY
    assert ReadyBlockerCode.COLLECTION_ENTITY_NEEDS_CLARIFICATION not in (
        plan.not_ready_reasons or []
    )


def test_candidate_count_gt_one_needs_clarification():
    intent = _strong_intent()
    intent.candidate_count = 3
    intent.needs_clarification = True
    intent.selected = False
    params = uces.build_semantic_params_from_intent(intent, legacy_type="select_profile")
    params["candidate_count"] = 3
    params["needs_clarification"] = True
    step = SemanticPlanStep(
        id="amb",
        type=uces.UNIVERSAL_SELECT_KIND,
        params=params,
        preferred_strategy=uces.UCES_PRIMARY_STRATEGY,
        fallback_strategies=[],
        confidence=0.6,
        human_label="?",
        needs_user_label=True,
    )
    blockers = uces.compute_uces_ready_blockers(step)
    assert ReadyBlockerCode.COLLECTION_ENTITY_NEEDS_CLARIFICATION in blockers


def test_generic_entity_not_promoted():
    mission = _mission_with_profile_click("item")
    nodes = _uia_nodes(["item", "item two", "item three"])
    ev = RawEvent(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=10, y=10, button="left"),
        metadata={
            "uia_flat": nodes,
            "target_identity_isolation": {"target_label": "item", "trusted_pre_action_identity": True},
        },
    )
    mission.raw_trace = [ev]
    step = _legacy_select_profile_step("item")
    promoted = uces.promote_semantic_plan_step(step, mission)
    assert promoted.type == "select_profile"


def test_tel_truth_preserves_collection_entity():
    mission = _mission_with_profile_click()
    truths = extract_truth_blocks(mission)
    if not truths:
        pytest.skip("sin COBs en misión mínima")
    sel = [t for t in truths if t.truth_type == "select_entity"]
    if not sel:
        pytest.skip("sin truth select_entity")
    inj = dict((sel[0].context_window or {}).get("tel_derived_params") or {})
    if inj.get("operational_collection") and inj.get("collection_entity"):
        assert inj.get("selection_context") or inj.get("selection_label")


def test_tel_sep_maps_select_entity_from_collection():
    intent = _strong_intent()
    from app.contracts.mission import OperationalTruthBlock, OperationalTruthStatus

    inj = uces.build_semantic_params_from_intent(intent, legacy_type="select_profile")
    tb = OperationalTruthBlock(
        truth_id="tb1",
        truth_type="select_entity",
        canonical_action="select_entity",
        human_intent="pick",
        context_window={"tel_derived_params": inj},
        operational_confidence=0.9,
        truth_status=OperationalTruthStatus.COMMITTED_SHADOW,
    )
    mission = _mission_with_profile_click()
    step = map_truth_block_to_sep_step(tb, mission, last_nav_url={})
    assert step is not None
    assert step.type == uces.UNIVERSAL_SELECT_KIND
    assert step.params.get("collection_entity")


def test_semantic_adapter_preserves_uces_params():
    intent = _strong_intent()
    params = uces.build_semantic_params_from_intent(intent, legacy_type="select_profile")
    adapted = adapt_mission_to_steps([
        {"type": uces.UNIVERSAL_SELECT_KIND, "params": params},
    ])
    assert adapted.ok
    assert adapted.steps[0].kind == uces.UNIVERSAL_SELECT_KIND
    assert adapted.steps[0].params.get("operational_collection")


def test_runtime_prefers_uces_before_legacy(monkeypatch):
    from app.services.runtime import entity_runtime_resolution as err

    calls = []

    def _fake_uces(*a, **k):
        calls.append("uces")
        return uces.EntityInCollectionMatchResult(matched=False, notes=["test"])

    monkeypatch.setattr(uces, "try_collection_entity_first_runtime", _fake_uces)

    from app.services.missions.execution_contracts import MissionStep
    from app.services.missions.state_detector import StateSnapshot

    step = MissionStep(
        id="s",
        kind=uces.UNIVERSAL_SELECT_KIND,
        params=uces.build_semantic_params_from_intent(_strong_intent()),
    )
    err.try_entity_first_runtime(
        step, StateSnapshot(), mission=_mission_with_profile_click(),
    )
    assert calls and calls[0] == "uces"


def test_normal_label_without_legacy_tokens():
    intent = _strong_intent()
    params = uces.build_semantic_params_from_intent(intent, legacy_type="select_profile")
    label = uces.human_label_for_uces_step(params)
    assert "select_profile" not in label.lower()
    assert "uia_text_match" not in label.lower()
    assert "smart_route" not in label.lower()
    assert PROFILE_NAMES[0] in label or "automáticamente" in label.lower()


def test_expert_audit_lines():
    mission = _mission_with_profile_click()
    uces.append_collection_audit(mission, {
        "step_id": "s1",
        "collection_type": "list",
        "entity_name": PROFILE_NAMES[0],
        "confidence": 0.95,
        "strategy": "exact",
    })
    lines = uces.expert_audit_lines(mission)
    assert lines and PROFILE_NAMES[0][:20] in lines[0]


def test_old_mission_without_uces_keeps_legacy():
    step = _legacy_select_profile_step(PROFILE_NAMES[0])
    mission = Mission(id=str(uuid.uuid4()), name="no_trace", raw_trace=[])
    promoted = uces.promote_semantic_plan_step(step, mission)
    assert promoted.type == "select_profile"


def test_model_dump_validate_roundtrip():
    intent = _strong_intent()
    params = uces.build_semantic_params_from_intent(intent, legacy_type="select_profile")
    from app.contracts.mission import OperationalCollection, CollectionEntityReference

    coll = OperationalCollection.model_validate(params["operational_collection"])
    ent = CollectionEntityReference.model_validate(params["collection_entity"])
    assert coll.collection_id
    assert ent.display_name == PROFILE_NAMES[0]
    assert not is_generic_entity_label(ent.display_name)


def test_apply_uces_primary_compilation_on_plan():
    mission = _mission_with_profile_click()
    plan = SemanticExecutionPlan(
        steps=[_legacy_select_profile_step(PROFILE_NAMES[0])],
        coords_used=False,
        legacy_graph_ignored=True,
    )
    n = uces.apply_uces_primary_compilation(plan, mission)
    assert n == 1
    assert plan.steps[0].type == uces.UNIVERSAL_SELECT_KIND


def test_no_chrome_youtube_hardcode_in_promotion():
    src = open(uces.__file__, encoding="utf-8").read().lower()
    assert "youtube.com" not in src or "site_domain" not in src
    assert "chrome profile" not in src.replace("_", " ")
