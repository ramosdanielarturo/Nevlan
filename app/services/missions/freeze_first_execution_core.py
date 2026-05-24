"""Freeze-First Execution Core (FFEC) — Canonical Operational Truth Pipeline.

Ley universal::

    IF operational_freeze_snapshot.pre_transition_confirmed
    AND best_entity_candidate.confidence >= 0.90:
        THEN freeze truth becomes canonical truth

Desde ese momento: no recalcular identidad, no downgrade por after_state,
no ambigüedad legacy, no coords-first, no «ask for clarification» por
reinterpretación post-transición.

``after_state`` **solo** valida outcome (``transition_observed``), nunca
define el target (``AFTER_STATE_VALIDATES_OUTCOME``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import (
    CanonicalOperationalIntent,
    CoiExecutionPolicy,
    CoiResolutionPolicy,
    CoiTruthOrigin,
    CoiValidationPolicy,
    Mission,
    OperationalAffordanceType,
    OperationalFreezeSnapshot,
    RawEvent,
    TransitionRiskLevel,
)
from app.core.logger import log

# Umbral FFEC (producto): confianza mínima de entidad para promoción canónica.
FFEC_ENTITY_CONFIDENCE_MIN: float = 0.90

# Replay priority chain (documented constant; consumidores leen ``execution_policy``).
REPLAY_PRIORITY_CHAIN: Tuple[str, ...] = (
    "canonical_operational_intent",
    "uces_continuity",
    "erl_entity_memory",
    "lone_navigation",
    "uia_runtime",
    "ocr",
    "vision",
    "relative_coords",
    "absolute_coords_emergency",
)


class FreezeFirstExecutionPolicy:
    """Reglas globales cuando existe COI activo en un evento o misión."""

    # Intent layer
    FORBID_RECOMPUTE_INTENT_FROM_AFTER_STATE: bool = True
    MUST_CONSUME_CANONICAL_TRUTH: bool = True

    # Capture contract
    EVALUATE_WITH_PRE_ACTION_TRUTH: bool = True
    AFTER_STATE_NEVER_DEFINES_TARGET: bool = True
    AFTER_STATE_VALIDATES_OUTCOME_ONLY: bool = True

    # Semantic planner
    PREFERRED_STEP_KIND: str = "select_entity_from_collection"
    FORBIDDEN_STEP_KINDS_WHEN_COI: Tuple[str, ...] = (
        "select_profile",
        "click_visual",
        "uia_text_match",
    )

    # Runtime
    COORDS_EMERGENCY_LAST_RESORT: bool = True

    # Mission Review (vista normal)
    HIDE_INTERNAL_MECHANICS: bool = True


def _is_dynamic_surface_unsafe(freeze: OperationalFreezeSnapshot) -> bool:
    if not freeze.dynamic_surface_detected:
        return False
    risk = getattr(freeze.transition_risk, "value", str(freeze.transition_risk))
    return risk in ("high", TransitionRiskLevel.HIGH.value)


def _is_destructive_context(freeze: OperationalFreezeSnapshot) -> bool:
    """Contexto donde un click equivocado tiene alto coste (confirmaciones destructivas)."""
    affordances = [str(a).lower() for a in (freeze.active_affordances or [])]
    markers = ("delete", "remove", "eliminar", "borrar", "uninstall", "format")
    return any(m in " ".join(affordances) for m in markers)


def _is_ambiguity_critical(freeze: OperationalFreezeSnapshot) -> bool:
    from app.services.runtime.pre_click_operational_freeze import (
        actionable_pcof_entity_names,
        pcof_best_dominates_actionables,
    )

    actionable = actionable_pcof_entity_names(freeze)
    if len(actionable) <= 1:
        return False
    return not pcof_best_dominates_actionables(freeze, actionable)


def promotion_gate_reasons(
    freeze: OperationalFreezeSnapshot,
    *,
    affordance: Optional[Any] = None,
    previous_event: Optional[RawEvent] = None,
    next_event: Optional[RawEvent] = None,
) -> Tuple[bool, List[str]]:
    """Evalúa si el freeze puede promoverse a COI (tras UOAC)."""
    from app.services.missions.universal_operational_affordance_classifier import (
        classify_from_freeze,
        should_promote_entity_coi,
    )

    if affordance is None:
        affordance = classify_from_freeze(
            freeze,
            previous_event=previous_event,
            next_event=next_event,
        )

    reasons: List[str] = []
    if not freeze.pre_transition_confirmed:
        return False, ["pre_transition_confirmed_false"]

    if affordance.affordance_type == OperationalAffordanceType.UNKNOWN:
        if float(affordance.confidence or 0) < 0.55:
            return False, ["uoac_unknown_affordance"]

    if should_promote_entity_coi(affordance):
        best = freeze.best_entity_candidate
        if not best or not best.display_name:
            return False, ["no_best_entity_candidate"]

        conf = float(best.confidence or 0.0)
        if conf < FFEC_ENTITY_CONFIDENCE_MIN:
            return False, [f"entity_confidence_below_{FFEC_ENTITY_CONFIDENCE_MIN}"]

        if _is_ambiguity_critical(freeze):
            return False, ["ambiguity_critical"]
    else:
        if float(affordance.confidence or 0) < 0.5:
            return False, ["uoac_low_confidence_non_entity"]
        reasons.append(f"uoac:{affordance.affordance_type.value}")

    if _is_dynamic_surface_unsafe(freeze):
        return False, ["dynamic_surface_unsafe"]

    if _is_destructive_context(freeze):
        return False, ["destructive_context"]

    reasons.extend([
        "pre_transition_confirmed",
        "uoac_classified",
        "gates_passed",
    ])
    if should_promote_entity_coi(affordance):
        reasons.append(f"entity_confidence>={FFEC_ENTITY_CONFIDENCE_MIN}")
    return True, reasons


def promote_freeze_to_canonical_intent(
    freeze: OperationalFreezeSnapshot,
    *,
    source_event_id: str = "",
    transition_expected: Optional[bool] = None,
    previous_event: Optional[RawEvent] = None,
    next_event: Optional[RawEvent] = None,
) -> Optional[CanonicalOperationalIntent]:
    """Promueve freeze a COI tras UOAC (no ``select_entity`` directo por UIA)."""
    from app.services.missions.universal_operational_affordance_classifier import (
        classify_from_freeze,
        should_promote_entity_coi,
    )

    affordance = classify_from_freeze(
        freeze,
        previous_event=previous_event,
        next_event=next_event,
    )
    ok, gate_reasons = promotion_gate_reasons(
        freeze,
        affordance=affordance,
        previous_event=previous_event,
        next_event=next_event,
    )
    if not ok:
        return None

    try:
        from app.services.runtime.universal_operational_element_identity import (
            identify_from_freeze,
        )

        element_identity = identify_from_freeze(
            freeze,
            previous_event=previous_event,
            next_event=next_event,
        )
    except Exception:
        element_identity = None

    best = freeze.best_entity_candidate
    entity_for_coi = best if should_promote_entity_coi(affordance) else None

    if transition_expected is None:
        risk = getattr(freeze.transition_risk, "value", str(freeze.transition_risk))
        transition_expected = risk in ("medium", "high", TransitionRiskLevel.HIGH.value)
        if affordance.transition_expectation:
            transition_expected = affordance.transition_expectation in (
                "medium",
                "high",
            ) or bool(transition_expected)

    truth_strength = float(affordance.confidence or freeze.freeze_confidence)
    if entity_for_coi is not None:
        truth_strength = min(
            1.0,
            max(float(entity_for_coi.confidence or 0), float(freeze.freeze_confidence)),
        )

    return CanonicalOperationalIntent(
        source_event_id=str(source_event_id or ""),
        entity=entity_for_coi,
        collection=freeze.best_collection_candidate if entity_for_coi else None,
        operational_intent_kind=str(affordance.operational_intent_kind or ""),
        operational_affordance=affordance,
        operational_element_identity=element_identity,
        freeze_confidence=float(freeze.freeze_confidence),
        pre_transition_confirmed=True,
        canonical_truth=True,
        truth_origin=CoiTruthOrigin.PRE_CLICK_OPERATIONAL_FREEZE,
        transition_expected=bool(transition_expected),
        transition_observed=False,
        allow_legacy_reinterpretation=False,
        allow_after_state_identity=False,
        truth_strength=truth_strength,
        truth_reasons=list(gate_reasons) + list(affordance.evidence_sources or [])[:6],
        resolution_policy=CoiResolutionPolicy.FREEZE_ENTITY_COLLECTION,
        execution_policy=CoiExecutionPolicy.COI_UCES_ERL_LONE_UIA_OCR_VISION_COORDS_EMERGENCY,
        validation_policy=CoiValidationPolicy.INTENDED_ENTITY_AND_EXPECTED_TRANSITION,
    )


def coi_from_metadata(metadata: Dict[str, Any]) -> Optional[CanonicalOperationalIntent]:
    raw = metadata.get("canonical_operational_intent")
    if not isinstance(raw, dict):
        return None
    try:
        return CanonicalOperationalIntent.model_validate(raw)
    except Exception:
        return None


def apply_coi_to_metadata(
    metadata: Dict[str, Any],
    coi: CanonicalOperationalIntent,
) -> Dict[str, Any]:
    """Persiste COI y refuerza contrato / aislamiento PRE-acción."""
    metadata["canonical_operational_intent"] = coi.model_dump(mode="json")

    iso = dict(metadata.get("target_identity_isolation") or {})
    ent = coi.entity
    if ent and coi.canonical_truth:
        iso["target_label"] = ent.display_name
        iso["expected_label_hint"] = ent.display_name
        iso["trusted_pre_action_identity"] = True
        iso["identity_preserved_after_transition"] = True
        iso["successful_state_transition"] = True
        iso["target_identity_confidence"] = max(
            float(iso.get("target_identity_confidence") or 0.0),
            float(coi.truth_strength),
        )
        iso["trusted_pre_action_reasons"] = list(
            set(list(iso.get("trusted_pre_action_reasons") or []) + [
                "ffec_canonical_operational_intent",
                *list(coi.truth_reasons or [])[:4],
            ]),
        )
        iso["degradation_reason"] = None
        metadata["target_identity_isolation"] = iso

    cc = dict(metadata.get("capture_contract") or {})
    if coi.canonical_truth:
        cc["trusted_pre_action_identity"] = True
        cc["sufficient_for_execution"] = True
        cc["capture_complete"] = True
        cc["canonical_truth"] = True
        cc["ffec_pre_action_truth"] = True
        cc["missing"] = []
        cc["execution_missing"] = []
        cc.setdefault("quality_level", "green")
        cc["target_source"] = cc.get("target_source") or "semantic_target"
    metadata["capture_contract"] = cc
    metadata["pre_action_truth"] = {
        "canonical": True,
        "intent_id": coi.intent_id,
        "entity_display": ent.display_name if ent else "",
        "truth_strength": float(coi.truth_strength),
    }
    return metadata


def attach_coi_to_mission(
    mission: Mission,
    coi: CanonicalOperationalIntent,
) -> None:
    """Añade o actualiza COI en la misión (por ``source_event_id``)."""
    intents = list(mission.canonical_operational_intents or [])
    sid = str(coi.source_event_id or "")
    if sid:
        intents = [i for i in intents if str(i.source_event_id) != sid]
    intents.append(coi)
    try:
        mission.canonical_operational_intents = intents
    except Exception as exc:
        log.debug("[ffec] attach_coi_to_mission: %s", exc)
    if coi.canonical_truth and mission is not None:
        try:
            from app.services.missions.operational_truth_preservation import (
                register_coi_on_mission,
            )

            register_coi_on_mission(mission, coi)
        except Exception as otp_exc:
            log.debug("[ffec] otp register_coi: %s", otp_exc)


def promote_and_attach_from_metadata(
    metadata: Dict[str, Any],
    *,
    source_event_id: str = "",
    mission: Optional[Mission] = None,
) -> Optional[CanonicalOperationalIntent]:
    """Lee freeze del metadata, promueve COI y opcionalmente persiste en misión."""
    fr_raw = metadata.get("operational_freeze_snapshot")
    if not isinstance(fr_raw, dict):
        return None
    try:
        freeze = OperationalFreezeSnapshot.model_validate(fr_raw)
    except Exception:
        return None

    coi = promote_freeze_to_canonical_intent(
        freeze,
        source_event_id=source_event_id,
    )
    if coi is None:
        return None

    if source_event_id and not coi.source_event_id:
        coi = coi.model_copy(update={"source_event_id": source_event_id})

    if coi.operational_affordance is not None:
        from app.services.missions.universal_operational_affordance_classifier import (
            attach_affordance_to_metadata,
        )

        attach_affordance_to_metadata(metadata, coi.operational_affordance)

    apply_coi_to_metadata(metadata, coi)
    if mission is not None:
        attach_coi_to_mission(mission, coi)
    return coi


def find_coi_for_event(
    mission: Any,
    event_id: str,
) -> Optional[CanonicalOperationalIntent]:
    for coi in getattr(mission, "canonical_operational_intents", None) or []:
        if str(getattr(coi, "source_event_id", "")) == str(event_id):
            return coi
    return None


def mission_has_canonical_truth(mission: Any) -> bool:
    for coi in getattr(mission, "canonical_operational_intents", None) or []:
        if getattr(coi, "canonical_truth", False):
            return True
    for ev in getattr(mission, "raw_trace", None) or []:
        meta = dict(getattr(ev, "metadata", None) or {})
        c = coi_from_metadata(meta)
        if c and c.canonical_truth:
            return True
    return False


def apply_ffec_after_raw_event(
    mission: Mission,
    event_id: str,
    metadata: Dict[str, Any],
) -> Optional[CanonicalOperationalIntent]:
    """Hook post-``RawEvent``: enlaza ``source_event_id`` y re-promueve si falta COI."""
    existing = coi_from_metadata(metadata)
    if existing and existing.canonical_truth:
        updated = existing.model_copy(update={"source_event_id": str(event_id)})
        apply_coi_to_metadata(metadata, updated)
        attach_coi_to_mission(mission, updated)
        return updated
    coi = promote_and_attach_from_metadata(
        metadata,
        source_event_id=event_id,
        mission=mission,
    )
    return coi


def patch_capture_contract_after_post_action(
    contract: Dict[str, Any],
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Tras llegar post_state: no invalidar verdad PRE si hay COI."""
    coi = coi_from_metadata(metadata)
    if coi is None or not coi.canonical_truth:
        return contract

    out = dict(contract)
    out["has_post_state"] = True
    out["trusted_pre_action_identity"] = True
    out["sufficient_for_execution"] = True
    out["capture_complete"] = True
    out["canonical_truth"] = True
    out["ffec_pre_action_truth"] = True
    out["identity_preserved_after_transition"] = True
    out["missing"] = []
    out["execution_missing"] = []
    out["after_state_role"] = "outcome_validation_only"
    return out


def mark_transition_observed(metadata: Dict[str, Any], observed: bool = True) -> None:
    coi = coi_from_metadata(metadata)
    if coi is None:
        return
    updated = coi.model_copy(update={"transition_observed": bool(observed)})
    metadata["canonical_operational_intent"] = updated.model_dump(mode="json")


def human_ready_label_for_coi(coi: CanonicalOperationalIntent) -> str:
    """Copy vista normal Mission Review (UOL human operational rendering)."""
    try:
        from app.services.missions.universal_operational_language import (
            compile_canonical_intent_to_uol,
            human_operational_label_for_uol,
        )

        action = compile_canonical_intent_to_uol(coi)
        return human_operational_label_for_uol(action)
    except Exception:
        ent = coi.entity
        if ent and ent.display_name:
            return "Elemento identificado automáticamente"
        return "Acción lista para ejecutarse"


def recompute_intent_from_after_state_blocked(
    metadata: Dict[str, Any],
) -> bool:
    """``True`` si la capa de intent NO debe reinterpretar desde after_state."""
    if not FreezeFirstExecutionPolicy.FORBID_RECOMPUTE_INTENT_FROM_AFTER_STATE:
        return False
    coi = coi_from_metadata(metadata)
    return bool(coi and coi.canonical_truth and not coi.allow_after_state_identity)


__all__ = [
    "FFEC_ENTITY_CONFIDENCE_MIN",
    "FreezeFirstExecutionPolicy",
    "REPLAY_PRIORITY_CHAIN",
    "apply_coi_to_metadata",
    "apply_ffec_after_raw_event",
    "attach_coi_to_mission",
    "coi_from_metadata",
    "find_coi_for_event",
    "human_ready_label_for_coi",
    "mission_has_canonical_truth",
    "patch_capture_contract_after_post_action",
    "promote_and_attach_from_metadata",
    "promote_freeze_to_canonical_intent",
    "promotion_gate_reasons",
    "recompute_intent_from_after_state_blocked",
    "mark_transition_observed",
]
