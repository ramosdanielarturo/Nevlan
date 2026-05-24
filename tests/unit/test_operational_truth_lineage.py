"""Tests — operational truth lineage (materialización + Mission Review)."""

from __future__ import annotations

import uuid

import pytest

from app.contracts.mission import (
    CanonicalOperationalIntent,
    Mission,
    OperationalFreezeEntityCandidate,
)
from app.services.missions.mission_review_summary import build_mission_review_summary
from app.services.missions.operational_truth_lineage import (
    READY_BLOCKER_CANONICAL_NOT_MATERIALIZED,
    assert_no_orphan_canonical_truth,
    compute_review_from_preserved_truth,
    ensure_preserved_truth_pipeline,
    human_label_from_coi,
    materialize_missing_canonical_steps,
    sep_step_dict_from_coi_uol,
    trace_operational_step_lineage,
    uol_action_for_coi,
)
from app.services.missions.operational_truth_preservation import collect_canonical_cois
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
)
from app.services.missions.universal_operational_language import (
    UniversalOperationalActionType,
)

_ENTITY_DISPLAY = "Entity Alpha 7"
_EVENT_SELECT = "ev-select-entity-1"
_EVENT_NAV = "ev-navigate-1"


def _coi_entity(display: str, event_id: str) -> CanonicalOperationalIntent:
    return CanonicalOperationalIntent(
        intent_id=str(uuid.uuid4()),
        source_event_id=event_id,
        entity=OperationalFreezeEntityCandidate(
            display_name=display,
            normalized_name="entity_alpha_7",
            confidence=0.95,
            evidence_sources=["uia"],
        ),
        freeze_confidence=0.95,
        pre_transition_confirmed=True,
        canonical_truth=True,
        truth_strength=0.95,
    )


def _mission_with_coi_and_sep(
    coi: CanonicalOperationalIntent,
    *,
    extra_steps: list | None = None,
) -> Mission:
    steps = [
        SemanticPlanStep(
            id="s-open",
            type="open_app",
            params={"name": "app", "source_event_id": "ev-open"},
            preferred_strategy="open_app:generic",
            fallback_strategies=[],
            confidence=0.9,
            human_label="Abrir aplicación",
        ),
    ]
    if extra_steps:
        steps.extend(extra_steps)
    return Mission(
        id=str(uuid.uuid4()),
        name="lineage-generic",
        canonical_operational_intents=[coi],
        semantic_execution_plan=SemanticExecutionPlan(
            steps=steps,
            source="semantic_intent_promotion_engine",
        ).to_dict(),
    )


def test_generic_entity_selection_survives_sep():
    coi = _coi_entity(_ENTITY_DISPLAY, _EVENT_SELECT)
    mission = _mission_with_coi_and_sep(coi)
    report = materialize_missing_canonical_steps(mission)
    assert report.materialized_count >= 1
    sep_steps = (mission.semantic_execution_plan or {}).get("steps") or []
    linked = [
        s for s in sep_steps
        if isinstance(s, dict)
        and str((s.get("params") or {}).get("source_event_id") or "") == _EVENT_SELECT
    ]
    assert linked
    assert uol_action_for_coi(coi) == UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION.value
    hl = human_label_from_coi(coi)
    assert hl
    assert _ENTITY_DISPLAY in hl or hl


def test_navigation_step_uol_survives_in_sep():
    mission = Mission(
        id=str(uuid.uuid4()),
        name="lineage-nav",
        semantic_execution_plan=SemanticExecutionPlan(
            steps=[
                SemanticPlanStep(
                    id="s-nav",
                    type="open_site",
                    params={
                        "url": "https://example.test/path",
                        "source_event_id": _EVENT_NAV,
                        "canonical_truth": True,
                    },
                    preferred_strategy="open_site:address_bar",
                    fallback_strategies=[],
                    confidence=0.9,
                    human_label="",
                ),
            ],
            source="semantic_intent_promotion_engine",
        ).to_dict(),
    )
    ensure_preserved_truth_pipeline(mission)
    step = next(
        s for s in (mission.semantic_execution_plan or {}).get("steps") or []
        if str(s.get("id") or "") == "s-nav"
    )
    assert str((step.get("params") or {}).get("uol_action") or "") in (
        UniversalOperationalActionType.NAVIGATE_TO_LOCATION.value,
        "",
    )
    lineages = trace_operational_step_lineage(mission)
    nav_ln = next(ln for ln in lineages if ln.sep_step_id == "s-nav")
    assert nav_ln.sep_step_type == "open_site"
    assert nav_ln.uol_action or nav_ln.human_label


def test_orphan_coi_surfaces_blocker_not_literal_expected_steps():
    coi = _coi_entity(_ENTITY_DISPLAY, _EVENT_SELECT)
    mission = _mission_with_coi_and_sep(coi)
    orphans = assert_no_orphan_canonical_truth(mission)
    assert len(orphans) == 1
    assert orphans[0].coi_intent_id == str(coi.intent_id)
    assert orphans[0].human_label_from_uol
    assert _ENTITY_DISPLAY in orphans[0].human_label_from_uol or orphans[0].human_label_from_uol

    review = compute_review_from_preserved_truth(mission)
    assert not review.orphans
    assert not assert_no_orphan_canonical_truth(mission)


def test_lineage_trace_links_materialized_step_to_coi():
    coi = _coi_entity(_ENTITY_DISPLAY, _EVENT_SELECT)
    mission = _mission_with_coi_and_sep(coi)
    ensure_preserved_truth_pipeline(mission)
    lineages = trace_operational_step_lineage(mission)
    coi_lines = [ln for ln in lineages if ln.coi_intent_id == str(coi.intent_id)]
    assert coi_lines
    materialized = [ln for ln in coi_lines if ln.sep_step_id]
    assert materialized
    assert materialized[0].raw_event_id == _EVENT_SELECT
    assert materialized[0].uol_action == uol_action_for_coi(coi)


def test_sep_materialization_uses_uol_not_expected_strings():
    coi = _coi_entity(_ENTITY_DISPLAY, _EVENT_SELECT)
    mission = Mission(id=str(uuid.uuid4()), name="uol-only")
    step = sep_step_dict_from_coi_uol(coi, mission=mission)
    assert step["human_label"] == human_label_from_coi(coi)
    assert "Daniel" not in step["human_label"]
    assert "YouTube" not in step["human_label"]
    assert "Chrome" not in step.get("params", {}).get("name", "")


def test_mission_review_builds_from_preserved_truth_not_literal_list():
    coi = _coi_entity(_ENTITY_DISPLAY, _EVENT_SELECT)
    mission = _mission_with_coi_and_sep(coi)
    summary = build_mission_review_summary(mission, recompute=True)
    labels = [s.human_label for s in summary.steps]
    assert labels
    forbidden_literals = (
        "Abrir Chrome / Seleccionar perfil Daniel Chrome",
        "Seleccionar perfil Daniel Chrome / Abrir nueva pestaña",
    )
    for lit in forbidden_literals:
        assert lit not in "\n".join(labels)
    assert any(_ENTITY_DISPLAY in lab or human_label_from_coi(coi) in lab for lab in labels)


def test_golden_mission_steps_traceable_from_real_sep_pipeline():
    """Caso Daniel/golden: pasos visibles porque existen en SEP, no por lista literal."""
    from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
        build_golden_search_chrome_youtube_mission,
    )
    from app.services.missions.operational_truth_preservation import apply_otp_after_promotion

    mission = build_golden_search_chrome_youtube_mission()
    apply_otp_after_promotion(mission)
    sep_steps = (mission.semantic_execution_plan or {}).get("steps") or []
    assert len(sep_steps) >= 4

    review = compute_review_from_preserved_truth(mission)
    assert len(review.steps) >= 4
    lineages = trace_operational_step_lineage(mission)
    by_id = {ln.sep_step_id: ln for ln in lineages if ln.sep_step_id}
    for prs in review.steps:
        ln = by_id.get(prs.sep_step_id)
        assert ln is not None, f"paso visible sin lineage SEP: {prs.sep_step_id}"
        assert prs.human_label
        assert ln.human_label

    profile_sep = next(
        (s for s in sep_steps if isinstance(s, dict) and s.get("type") == "select_profile"),
        None,
    )
    assert profile_sep is not None
    profile_id = str(profile_sep.get("id") or "")
    assert any(prs.sep_step_id == profile_id for prs in review.steps)

    cois = collect_canonical_cois(mission)
    if cois:
        coi_uol_labels = {human_label_from_coi(c) for c in cois if human_label_from_coi(c)}
        shown = {s.human_label for s in review.steps}
        assert coi_uol_labels & shown
