"""Tests — Freeze-First Execution Core (FFEC)."""

from __future__ import annotations

import uuid

import pytest

from app.contracts.mission import (
    Mission,
    OperationalFreezeCollectionSnapshot,
    OperationalFreezeEntityCandidate,
    OperationalFreezeSnapshot,
    TransitionRiskLevel,
)
from app.services.missions.freeze_first_execution_core import (
    FFEC_ENTITY_CONFIDENCE_MIN,
    apply_coi_to_metadata,
    coi_from_metadata,
    mission_has_canonical_truth,
    patch_capture_contract_after_post_action,
    promote_freeze_to_canonical_intent,
    promotion_gate_reasons,
)
from app.services.missions.legacy_runtime_contamination_guard import (
    ContaminationAction,
    guard_against_legacy_contamination,
)


def _freeze(
    *,
    confirmed: bool = True,
    confidence: float = 0.92,
    dynamic: bool = False,
    entities: list[str] | None = None,
) -> OperationalFreezeSnapshot:
    ent_name = "Daniel Chrome D Daniel Arturo Ramos"
    ents = entities or [ent_name]
    return OperationalFreezeSnapshot(
        freeze_id=str(uuid.uuid4()),
        pre_transition_confirmed=confirmed,
        freeze_confidence=confidence,
        dynamic_surface_detected=dynamic,
        transition_risk=TransitionRiskLevel.HIGH,
        best_entity_candidate=OperationalFreezeEntityCandidate(
            display_name=ent_name,
            normalized_name="daniel",
            confidence=confidence,
            evidence_sources=["uia", "collection"],
        ),
        best_collection_candidate=OperationalFreezeCollectionSnapshot(
            collection_type="list",
            display_name="profiles",
            entities=ents,
            entity_count=len(ents),
            confidence=0.88,
        ),
    )


def test_promotion_gate_requires_confidence():
    fr = _freeze(confidence=0.5)
    ok, reasons = promotion_gate_reasons(fr)
    assert not ok
    assert any("entity_confidence" in r for r in reasons)


def test_promote_freeze_to_coi_success():
    fr = _freeze(confidence=FFEC_ENTITY_CONFIDENCE_MIN + 0.01)
    coi = promote_freeze_to_canonical_intent(fr, source_event_id="ev-1")
    assert coi is not None
    assert coi.canonical_truth is True
    assert coi.allow_legacy_reinterpretation is False
    assert coi.allow_after_state_identity is False
    assert coi.entity is not None
    assert coi.entity.display_name.startswith("Daniel")


def test_promotion_blocked_on_dynamic_surface_unsafe():
    fr = _freeze(dynamic=True)
    ok, _ = promotion_gate_reasons(fr)
    assert not ok


def test_apply_coi_makes_capture_complete_without_post():
    meta: dict = {"capture_contract": {"capture_complete": False, "missing": ["post_state"]}}
    coi = promote_freeze_to_canonical_intent(_freeze())
    assert coi is not None
    apply_coi_to_metadata(meta, coi)
    cc = meta["capture_contract"]
    assert cc["capture_complete"] is True
    assert cc["sufficient_for_execution"] is True
    assert cc["canonical_truth"] is True
    assert coi_from_metadata(meta) is not None


def test_patch_capture_contract_preserves_pre_truth_after_post():
    coi = promote_freeze_to_canonical_intent(_freeze())
    assert coi is not None
    meta = {}
    apply_coi_to_metadata(meta, coi)
    contract = {"capture_complete": False, "missing": ["post_state", "uia"]}
    patched = patch_capture_contract_after_post_action(contract, meta)
    assert patched["capture_complete"] is True
    assert patched["has_post_state"] is True
    assert patched.get("after_state_role") == "outcome_validation_only"


def test_legacy_guard_blocks_coords_and_ask():
    coi = promote_freeze_to_canonical_intent(_freeze())
    assert coi is not None
    r = guard_against_legacy_contamination(
        ContaminationAction.COORDS_FIRST,
        coi=coi,
    )
    assert not r.allowed
    r2 = guard_against_legacy_contamination(
        ContaminationAction.ASK_FALLBACK,
        coi=coi,
    )
    assert not r2.allowed


def test_mission_has_canonical_truth_from_intents():
    coi = promote_freeze_to_canonical_intent(_freeze(), source_event_id="e1")
    assert coi is not None
    m = Mission(name="ffec-test")
    m.canonical_operational_intents = [coi]
    assert mission_has_canonical_truth(m) is True
