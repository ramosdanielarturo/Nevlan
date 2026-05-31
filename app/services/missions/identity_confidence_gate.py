"""
Universal identity confidence gate — mandatory playback path
--------------------------------------------------------------
Decides whether an action step may execute without blind coords/guessing.

If identity confidence is below :data:`IDENTITY_EXECUTE_THRESHOLD`, playback
returns ``NEEDS_REVIEW`` with actionable hints instead of clicking by coords.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.services.missions.execution_contracts import MissionStep
from app.services.missions.target_identity import (
    CONFIDENCE_CONFIRM_THRESHOLD,
    TargetIdentity,
)
from app.services.runtime.nevlan_runtime_convergence import (
    is_forbidden_primary_strategy,
)

__all__ = [
    "IDENTITY_EXECUTE_THRESHOLD",
    "IDENTITY_STRONG_THRESHOLD",
    "IDENTITY_INSUFFICIENT",
    "NEEDS_REVIEW",
    "IdentityGateDecision",
    "evaluate_identity_gate",
    "filter_strategies_identity_first",
    "has_semantic_plan_mandate",
    "score_step_identity",
]

IDENTITY_EXECUTE_THRESHOLD = CONFIDENCE_CONFIRM_THRESHOLD  # 0.65
IDENTITY_STRONG_THRESHOLD = 0.78

IDENTITY_INSUFFICIENT = "IDENTITY_INSUFFICIENT"
NEEDS_REVIEW = "NEEDS_REVIEW"

# Steps that require a resolvable target identity before execution.
IDENTITY_REQUIRED_KINDS = frozenset({
    "click_named_item",
    "type_text",
    "select_profile",
    "select_entity_from_collection",
})

# Routed by dedicated strategies — identity gate defers to post-step verification.
IDENTITY_EXEMPT_KINDS = frozenset({
    "open_app",
    "open_new_tab",
    "open_url",
    "open_site",
    "scroll_page",
    "fill_form",
    "submit_form",
})

_NAMED_TARGET_KEYS = (
    "target_name",
    "name",
    "item_name",
    "anchor_text",
    "label",
    "profile_name",
    "query",
    "text",
)


@dataclass(frozen=True)
class IdentityGateDecision:
    allowed: bool
    confidence: float
    failure_code: str = ""
    human_message: str = ""
    missing: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)


def has_semantic_plan_mandate(step: MissionStep) -> bool:
    """True when SEP/UCP injected a non-coords preferred strategy."""
    pref = str((step.params or {}).get("preferred_strategy") or "").strip()
    return bool(pref) and not is_forbidden_primary_strategy(pref)


def score_step_identity(step: MissionStep) -> tuple[float, List[str], List[str]]:
    """Return ``(confidence, missing_evidence, positive_signals)``."""
    params = step.params or {}
    missing: List[str] = []
    signals: List[str] = []
    score = 0.0

    pref = str(params.get("preferred_strategy") or "").strip()
    if pref and not is_forbidden_primary_strategy(pref):
        score = max(score, 0.85)
        signals.append("semantic_plan_strategy")

    ti_raw = params.get("target_identity")
    if isinstance(ti_raw, TargetIdentity):
        conf = float(ti_raw.confidence or 0.0)
        if conf >= IDENTITY_EXECUTE_THRESHOLD:
            signals.append("target_identity")
        score = max(score, conf)
    elif isinstance(ti_raw, dict):
        conf = float(ti_raw.get("confidence") or 0.0)
        if conf >= IDENTITY_EXECUTE_THRESHOLD:
            signals.append("target_identity")
        score = max(score, conf)

    cc = params.get("capture_contract") or {}
    if isinstance(cc, dict) and (
        cc.get("trusted_pre_action_identity") or cc.get("sufficient_for_execution")
    ):
        score = max(score, 0.82)
        signals.append("capture_contract")

    tii = params.get("target_identity_isolation") or {}
    if isinstance(tii, dict) and tii.get("trusted_pre_action_identity"):
        score = max(score, 0.80)
        signals.append("target_identity_isolation")

    if str(params.get("uol_action") or "").strip():
        score = max(score, 0.80)
        signals.append("uol_contract")

    ent = params.get("operational_entity") or params.get("entity")
    if isinstance(ent, dict) and str(ent.get("label") or ent.get("name") or "").strip():
        score = max(score, IDENTITY_STRONG_THRESHOLD)
        signals.append("operational_entity")

    uia = params.get("uia") or params.get("uia_data") or {}
    if isinstance(uia, dict):
        if str(uia.get("name") or "").strip() or str(uia.get("automation_id") or "").strip():
            score = max(score, 0.75)
            signals.append("uia_anchor")

    if any(str(params.get(k) or "").strip() for k in _NAMED_TARGET_KEYS):
        score = max(score, 0.70)
        signals.append("named_target_param")
    elif step.kind in IDENTITY_REQUIRED_KINDS:
        missing.append("target_text_or_anchor")

    surface = str(params.get("surface") or params.get("surface_kind") or "").strip()
    if surface and step.kind == "click_named_item":
        score = max(score, 0.72)
        signals.append(f"surface:{surface}")

    return score, missing, signals


def evaluate_identity_gate(
    step: MissionStep,
    *,
    entity_resolved: bool = False,
    uol_handled: bool = False,
    skip_kinds: Optional[frozenset] = None,
) -> IdentityGateDecision:
    """Decide if ``step`` may proceed to strategy execution."""
    if uol_handled or entity_resolved:
        return IdentityGateDecision(allowed=True, confidence=1.0, signals=["prior_path"])

    kind = str(step.kind or "")
    exempt = skip_kinds or IDENTITY_EXEMPT_KINDS
    if kind not in IDENTITY_REQUIRED_KINDS or kind in exempt:
        return IdentityGateDecision(allowed=True, confidence=1.0, signals=["exempt_kind"])

    confidence, missing, signals = score_step_identity(step)
    if confidence >= IDENTITY_EXECUTE_THRESHOLD:
        return IdentityGateDecision(
            allowed=True,
            confidence=confidence,
            signals=signals,
        )

    suggestions = _build_suggestions(step, missing)
    label = step.human_label or step.describe()
    human = (
        f"No tengo identidad suficiente para «{label}». "
        "Revisa el objetivo o regraba este paso."
    )
    return IdentityGateDecision(
        allowed=False,
        confidence=confidence,
        failure_code=NEEDS_REVIEW,
        human_message=human,
        missing=missing,
        suggestions=suggestions,
        signals=signals,
    )


def filter_strategies_identity_first(
    strategies: Sequence[str],
    step: MissionStep,
) -> List[str]:
    """Remove coords/emergency strategies when a semantic identity path exists."""
    if not has_semantic_plan_mandate(step):
        safe = [s for s in strategies if not is_forbidden_primary_strategy(s)]
        risky = [s for s in strategies if is_forbidden_primary_strategy(s)]
        return safe + risky

    filtered = [s for s in strategies if not is_forbidden_primary_strategy(s)]
    return filtered


def _build_suggestions(step: MissionStep, missing: List[str]) -> List[str]:
    out: List[str] = []
    if "target_text_or_anchor" in missing:
        out.append("Añade una etiqueta visible o regraba el click con OCR activo.")
    if step.kind == "select_profile":
        out.append("Confirma el nombre exacto del perfil de Chrome.")
    if step.kind == "type_text":
        out.append("Verifica que el campo destino tenga nombre UIA o etiqueta OCR.")
    if not out:
        out.append("Habilita visión OCR o regraba este paso con identidad fuerte.")
    return out
