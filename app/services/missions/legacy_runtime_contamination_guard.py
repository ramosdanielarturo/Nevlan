"""Legacy Runtime Contamination Guard — muro de aislamiento FFEC.

Cuando existe :class:`~app.contracts.mission.CanonicalOperationalIntent`
con ``canonical_truth=True``, bloquea rutas que recontaminan la identidad
con after_state, coords-first, ambigüedad legacy o downgrade de confianza.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from app.contracts.mission import CanonicalOperationalIntent
from app.services.missions.freeze_first_execution_core import coi_from_metadata


class ContaminationAction(str, Enum):
    REINTERPRET_FROM_VISUAL = "reinterpret_from_visual"
    REOPEN_AMBIGUITY = "reopen_ambiguity"
    COORDS_FIRST = "coords_first"
    AFTER_STATE_TARGET_OVERWRITE = "after_state_target_overwrite"
    LOW_CONFIDENCE_DOWNGRADE = "low_confidence_downgrade"
    VISUAL_ICON_OVERRIDE = "visual_icon_override"
    ASK_FALLBACK = "ask_fallback"
    RECOMPUTE_INTENT_FROM_AFTER_STATE = "recompute_intent_from_after_state"
    LEGACY_STEP_KIND = "legacy_step_kind"


@dataclass
class GuardResult:
    allowed: bool
    action: str = ""
    reason: str = ""
    suppressed: List[str] = field(default_factory=list)


def _coi_active(coi: Optional[CanonicalOperationalIntent]) -> bool:
    return bool(coi and coi.canonical_truth and not coi.allow_legacy_reinterpretation)


def guard_against_legacy_contamination(
    action: ContaminationAction,
    *,
    coi: Optional[CanonicalOperationalIntent] = None,
    metadata: Optional[Dict[str, Any]] = None,
    step_type: str = "",
    confidence: Optional[float] = None,
) -> GuardResult:
    """Bloquea acciones contaminantes si hay COI canónico."""
    if coi is None and metadata:
        coi = coi_from_metadata(metadata)
    if not _coi_active(coi):
        return GuardResult(allowed=True)

    blocked_actions = frozenset({
        ContaminationAction.REINTERPRET_FROM_VISUAL,
        ContaminationAction.REOPEN_AMBIGUITY,
        ContaminationAction.COORDS_FIRST,
        ContaminationAction.AFTER_STATE_TARGET_OVERWRITE,
        ContaminationAction.LOW_CONFIDENCE_DOWNGRADE,
        ContaminationAction.VISUAL_ICON_OVERRIDE,
        ContaminationAction.ASK_FALLBACK,
        ContaminationAction.RECOMPUTE_INTENT_FROM_AFTER_STATE,
    })

    if action in blocked_actions:
        return GuardResult(
            allowed=False,
            action=action.value,
            reason="canonical_operational_intent_active",
            suppressed=[action.value],
        )

    if action == ContaminationAction.LEGACY_STEP_KIND:
        legacy_kinds = frozenset({
            "select_profile",
            "click_visual",
            "uia_text_match",
            "smart_route",
        })
        if (step_type or "").strip().lower() in legacy_kinds:
            return GuardResult(
                allowed=False,
                action=action.value,
                reason=f"legacy_step_kind_blocked:{step_type}",
                suppressed=[step_type],
            )

    if action == ContaminationAction.LOW_CONFIDENCE_DOWNGRADE:
        if confidence is not None and coi is not None:
            floor = float(coi.truth_strength or 0.0)
            if confidence < floor:
                return GuardResult(
                    allowed=False,
                    action=action.value,
                    reason="cannot_downgrade_below_canonical_truth_strength",
                    suppressed=["confidence_downgrade"],
                )

    return GuardResult(allowed=True)


def should_block_coords_first(
    metadata: Dict[str, Any],
) -> bool:
    r = guard_against_legacy_contamination(
        ContaminationAction.COORDS_FIRST,
        metadata=metadata,
    )
    return not r.allowed


def should_block_ask_fallback(metadata: Dict[str, Any]) -> bool:
    r = guard_against_legacy_contamination(
        ContaminationAction.ASK_FALLBACK,
        metadata=metadata,
    )
    return not r.allowed


def filter_legacy_blockers_for_mission_review(
    blockers: List[str],
    mission: Any,
) -> List[str]:
    """Elimina blockers artificiales cuando FFEC ya cerró la verdad."""
    from app.services.missions.freeze_first_execution_core import mission_has_canonical_truth

    if not mission_has_canonical_truth(mission):
        return blockers

    noise = frozenset({
        "ambiguous_profile_picker",
        "AMBIGUOUS_PROFILE_PICKER",
        "empty_profile",
        "profile_unknown",
        "low_confidence",
        "capture_incomplete",
        "needs_clarification",
    })
    return [b for b in blockers if str(b) not in noise and str(b).lower() not in {
        x.lower() for x in noise
    }]


__all__ = [
    "ContaminationAction",
    "GuardResult",
    "filter_legacy_blockers_for_mission_review",
    "guard_against_legacy_contamination",
    "should_block_ask_fallback",
    "should_block_coords_first",
]
