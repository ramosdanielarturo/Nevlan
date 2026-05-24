"""Tests — Operational Truth Preservation (OTP)."""

from __future__ import annotations

import uuid

import pytest

from app.contracts.mission import (
    CanonicalOperationalIntent,
    Mission,
    OperationalFreezeEntityCandidate,
    OperationalTruthDisposition,
    OperationalTruthStage,
)
from app.services.missions.freeze_first_execution_core import attach_coi_to_mission
from app.services.missions.mission_review_summary import (
    build_mission_review_summary,
    render_expert_view,
)
from app.services.missions.operational_truth_preservation import (
    apply_otp_after_promotion,
    audit_operational_truth_chain,
    collect_canonical_cois,
    reconcile_canonical_truths_on_sep_steps,
    truth_origin_id_for_coi,
)
from app.services.missions.semantic_execution_plan import SemanticExecutionPlan, SemanticPlanStep
from app.services.missions.semantic_intent_promotion_engine import apply_promotion_to_mission


def _coi_profile(name: str = "Daniel Chrome", event_id: str = "ev-pcof-1") -> CanonicalOperationalIntent:
    return CanonicalOperationalIntent(
        intent_id=str(uuid.uuid4()),
        source_event_id=event_id,
        entity=OperationalFreezeEntityCandidate(
            display_name=name,
            normalized_name="daniel_chrome",
            confidence=0.95,
            evidence_sources=["uia", "collection"],
        ),
        freeze_confidence=0.95,
        pre_transition_confirmed=True,
        canonical_truth=True,
        truth_strength=0.95,
    )


def test_truth_origin_id_stable():
    coi = _coi_profile()
    assert truth_origin_id_for_coi(coi).startswith("coi:")


def test_register_coi_on_attach():
    mission = Mission(id=str(uuid.uuid4()), name="otp-ffec")
    coi = _coi_profile()
    attach_coi_to_mission(mission, coi)
    assert mission.operational_truth_chain is not None
    assert any(
        n.stage == OperationalTruthStage.COI and n.canonical_truth
        for n in mission.operational_truth_chain.nodes
    )


def test_reconcile_backfills_missing_profile_step():
    coi = _coi_profile()
    mission = Mission(
        id=str(uuid.uuid4()),
        name="otp-backfill",
        canonical_operational_intents=[coi],
        semantic_execution_plan=SemanticExecutionPlan(
            steps=[
                SemanticPlanStep(
                    id="s1",
                    type="open_app",
                    params={"name": "chrome"},
                    preferred_strategy="open_app:windows_search",
                    fallback_strategies=[],
                    confidence=0.95,
                    human_label="Abrir Chrome",
                ),
                SemanticPlanStep(
                    id="s3",
                    type="open_new_tab",
                    params={},
                    preferred_strategy="open_tab:uol_hotkey",
                    fallback_strategies=[],
                    confidence=0.9,
                    human_label="Abrir nueva pestaña",
                ),
            ],
            source="semantic_intent_promotion_engine",
        ).to_dict(),
    )
    reconciled = reconcile_canonical_truths_on_sep_steps(mission, mission.semantic_execution_plan["steps"])
    kinds = [getattr(s, "type", None) for s in reconciled]
    assert "select_entity_from_collection" in kinds
    sel = next(s for s in reconciled if getattr(s, "type", "") == "select_entity_from_collection")
    assert "Daniel Chrome" in (getattr(sel, "human_label", "") or "")
    assert (getattr(sel, "params", None) or {}).get("canonical_truth") is True


def test_otp_detects_degraded_smart_route_label():
    coi = _coi_profile()
    mission = Mission(
        id=str(uuid.uuid4()),
        name="otp-degraded",
        canonical_operational_intents=[coi],
        semantic_execution_plan={
            "intent_status": "READY",
            "steps": [
                {
                    "id": "s1",
                    "type": "open_app",
                    "params": {"name": "chrome"},
                    "human_label": "Abrir Chrome",
                },
                {
                    "id": "s2",
                    "type": "select_entity_from_collection",
                    "params": {
                        "collection_entity": {"display_name": "Daniel Chrome"},
                        "canonical_truth": True,
                        "source_event_id": coi.source_event_id,
                        "preferred_strategy": "search_content:smart_route",
                    },
                    "human_label": "Elemento identificado automáticamente",
                    "preferred_strategy": "search_content:smart_route",
                },
            ],
        },
    )
    chain = audit_operational_truth_chain(mission)
    assert chain.loss_by_stage or chain.degraded or any(
        n.disposition == OperationalTruthDisposition.DEGRADED for n in chain.nodes
    )


def test_acceptance_flow_labels_in_mission_review():
    """Flujo Chrome/YouTube: pasos operacionales sin pérdida en vista normal."""
    coi = _coi_profile("Daniel Chrome")
    mission = Mission(
        id=str(uuid.uuid4()),
        name="chrome-youtube-otp",
        canonical_operational_intents=[coi],
        semantic_execution_plan={
            "intent_status": "READY",
            "ready_provenance": "raw_evidence",
            "source": "semantic_intent_promotion_engine",
            "steps": [
                {
                    "id": "s0",
                    "type": "open_app",
                    "params": {"name": "chrome", "method": "windows_search"},
                    "human_label": "Abrir Chrome",
                },
                {
                    "id": "s1",
                    "type": "select_entity_from_collection",
                    "params": {
                        "collection_entity": {
                            "display_name": "Daniel Chrome",
                            "entity_type": "user_profile",
                        },
                        "canonical_truth": True,
                        "source_event_id": coi.source_event_id,
                    },
                    "human_label": "Elemento identificado automáticamente",
                },
                {
                    "id": "s2",
                    "type": "open_new_tab",
                    "params": {},
                    "human_label": "Abrir nueva pestaña",
                },
                {
                    "id": "s3",
                    "type": "open_site",
                    "params": {"site": "youtube", "url": "https://www.youtube.com"},
                    "human_label": "Abrir YouTube",
                },
                {
                    "id": "s4",
                    "type": "search_site",
                    "params": {"site": "youtube", "query": "Devuélveme el amor Luis Miguel"},
                    "human_label": 'Buscar "Devuélveme el amor Luis Miguel" en Youtube',
                },
                {
                    "id": "s5",
                    "type": "scroll_results",
                    "params": {"direction": "down", "amount": 2},
                    "human_label": "Bajar resultados",
                },
            ],
        },
    )
    apply_otp_after_promotion(mission)
    summary = build_mission_review_summary(mission, recompute=False)
    labels = [s.human_label for s in summary.steps]
    assert labels[0] == "Abrir Chrome"
    assert "Daniel Chrome" in labels[1]
    assert "automáticamente" not in labels[1].lower()
    assert labels[2] == "Abrir nueva pestaña"
    assert "YouTube" in labels[3] or "youtube" in labels[3].lower()
    assert "Devuélveme el amor Luis Miguel" in labels[4]
    assert "Bajar" in labels[5] or "resultados" in labels[5].lower()
    expert = render_expert_view(summary)
    assert "Truth Preservation" in expert
    assert collect_canonical_cois(mission)


def test_apply_promotion_runs_otp_on_pcof_mission():
    from tests.unit.test_pcof_universal_sep_promotion import (
        _mission_with_freeze_and_legacy,
        _profile_picker_freeze,
    )

    entity = "Daniel Chrome D Daniel Arturo Ramos"
    freeze = _profile_picker_freeze(
        entity=entity,
        siblings=[entity, "More actions"],
        absorbed=True,
    )
    m = _mission_with_freeze_and_legacy(freeze)
    from app.services.missions.freeze_first_execution_core import promote_freeze_to_canonical_intent

    coi = promote_freeze_to_canonical_intent(freeze, source_event_id="ev-pcof-1")
    assert coi is not None
    attach_coi_to_mission(m, coi)
    apply_promotion_to_mission(m)
    assert m.operational_truth_chain is not None
    sep = m.semantic_execution_plan or {}
    labels = [
        str(getattr(s, "human_label", None) or (s.get("human_label") if isinstance(s, dict) else "") or "")
        for s in sep.get("steps") or []
    ]
    assert any("Daniel" in lab for lab in labels)
