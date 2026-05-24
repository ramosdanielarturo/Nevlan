"""Operational State Understanding Engine (OSUE).

Capa que permite a Nevlan comprender el ESTADO operacional real del sistema
en tiempo real — no solo elementos, clicks, affordances o mechanics.

Responde: «¿En qué estado operacional REAL está el sistema ahora?»

Fuentes: UOEI, UOAC, OCRX, PCOF, DOM/UIA, LOP, ORG, ARSS, ORCE.
No ejecuta input; observa, clasifica y valida compatibilidad de estados.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from app.contracts.mission import (
    FourLayerOperationalModel,
    LiveOperationalBlocker,
    LiveOperationalEntityKind,
    LiveOperationalPerceptionSnapshot,
    LiveOperationalSurfaceType,
    MachineExecutionStep,
    Mission,
    OperationalElementIdentity,
    OperationalExecutionExpectation,
    OperationalRuntimeObservation,
    OperationalStateSnapshot,
    OperationalStateType,
)
from app.contracts.operational_runtime_graph import OperationalRuntimeGraph
from app.core.logger import log
from app.core.paths import VAR_DIR

_OSUE_JSONL = VAR_DIR / "runtime" / "osue_state_audit.jsonl"

# Estados bloqueados universalmente para ejecución de pasos interactivos.
_UNIVERSAL_BLOCKED: Set[str] = {
    OperationalStateType.AUTH_REQUIRED_STATE.value,
    OperationalStateType.MODAL_INTERRUPTION_STATE.value,
    OperationalStateType.DESTRUCTIVE_CONFIRMATION_STATE.value,
    OperationalStateType.OVERLAY_BLOCKING_STATE.value,
    OperationalStateType.RUNTIME_ERROR_STATE.value,
    OperationalStateType.LOADING_STATE.value,
    OperationalStateType.NAVIGATION_TRANSITION_STATE.value,
    OperationalStateType.CONTEXT_SWITCH_STATE.value,
    OperationalStateType.ASYNC_REFRESH_STATE.value,
    OperationalStateType.RESULTS_LOADING_STATE.value,
    OperationalStateType.UNKNOWN_OPERATIONAL_STATE.value,
}

# Requeridos por tipo semántico (genérico — sin apps).
_REQUIRED_BY_SEMANTIC: Dict[str, List[str]] = {
    "search_content": [OperationalStateType.SEARCH_INPUT_READY_STATE.value],
    "fill_field": [OperationalStateType.INTERACTION_READY_STATE.value],
    "fill_form": [OperationalStateType.INTERACTION_READY_STATE.value],
    "submit_form": [OperationalStateType.INTERACTION_READY_STATE.value],
    "switch_context": [OperationalStateType.INTERACTION_READY_STATE.value],
    "confirm_dialog": [OperationalStateType.MODAL_INTERRUPTION_STATE.value],
    "submit_input": [OperationalStateType.INTERACTION_READY_STATE.value],
    "open_site": [OperationalStateType.NAVIGATION_READY_STATE.value],
    "open_url": [OperationalStateType.NAVIGATION_READY_STATE.value],
    "open_app": [OperationalStateType.LAUNCHER_READY_STATE.value],
    "open_application": [OperationalStateType.LAUNCHER_READY_STATE.value],
    "navigate": [OperationalStateType.NAVIGATION_READY_STATE.value],
    "select_profile": [OperationalStateType.INTERACTION_READY_STATE.value],
    "select_entity_from_collection": [OperationalStateType.INTERACTION_READY_STATE.value],
}

# Transiciones esperadas post-acción (cadena genérica).
_EXPECTED_TRANSITIONS: Dict[str, List[str]] = {
    "search_content": [
        OperationalStateType.SEARCH_INPUT_READY_STATE.value,
        OperationalStateType.RESULTS_LOADING_STATE.value,
        OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    ],
    "submit_input": [
        OperationalStateType.INTERACTION_READY_STATE.value,
        OperationalStateType.LOADING_STATE.value,
        OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    ],
    "submit_form": [
        OperationalStateType.INTERACTION_READY_STATE.value,
        OperationalStateType.LOADING_STATE.value,
        OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    ],
    "switch_context": [
        OperationalStateType.INTERACTION_READY_STATE.value,
        OperationalStateType.CONTEXT_SWITCH_STATE.value,
        OperationalStateType.MODAL_INTERRUPTION_STATE.value,
    ],
    "confirm_dialog": [
        OperationalStateType.MODAL_INTERRUPTION_STATE.value,
        OperationalStateType.INTERACTION_READY_STATE.value,
    ],
    "open_app": [
        OperationalStateType.LAUNCHER_READY_STATE.value,
        OperationalStateType.NAVIGATION_READY_STATE.value,
    ],
    "open_site": [
        OperationalStateType.NAVIGATION_READY_STATE.value,
        OperationalStateType.LOADING_STATE.value,
        OperationalStateType.INTERACTION_READY_STATE.value,
    ],
}

# Marcadores estructurales de loading (sin textos de producto).
_LOADING_MARKERS = frozenset(
    {
        "loading",
        "spinner",
        "skeleton",
        "progressbar",
        "progress",
        "busy",
        "pending",
        "aria-busy",
    },
)


class OsueReadiness(str, Enum):
    READY = "ready"
    WAIT = "wait"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


_HUMAN_GATE_STATES: Set[str] = {
    OperationalStateType.AUTH_REQUIRED_STATE.value,
    OperationalStateType.MODAL_INTERRUPTION_STATE.value,
    OperationalStateType.DESTRUCTIVE_CONFIRMATION_STATE.value,
    OperationalStateType.OVERLAY_BLOCKING_STATE.value,
}

# Estados transitorios válidos tras submit de búsqueda — no son fallo ni recovery.
SEARCH_TRANSITIONAL_OPERATIONAL_STATES: frozenset = frozenset({
    OperationalStateType.RESULTS_LOADING_STATE.value,
    OperationalStateType.NAVIGATION_TRANSITION_STATE.value,
    OperationalStateType.LOADING_STATE.value,
    OperationalStateType.ASYNC_REFRESH_STATE.value,
    OperationalStateType.SEARCH_INPUT_READY_STATE.value,
})

_POST_TARGET_BY_KIND: Dict[str, str] = {
    "search_web": OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    "search_site": OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    "search_content": OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    "submit_input": OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    "open_url": OperationalStateType.INTERACTION_READY_STATE.value,
    "open_site": OperationalStateType.INTERACTION_READY_STATE.value,
    "open_app": OperationalStateType.LAUNCHER_READY_STATE.value,
    "open_application": OperationalStateType.LAUNCHER_READY_STATE.value,
    "navigate": OperationalStateType.NAVIGATION_READY_STATE.value,
}


@dataclass
class SearchOsueWaitProfile:
    """Perfil wait_until para ``search_content`` con ``submit=true``."""

    min_settle_ms: int = 400
    max_wait_ms: int = 8000
    poll_ms_initial: int = 150
    poll_ms_max: int = 550
    stagnant_polls_for_failure: int = 16
    transitional_states: frozenset = field(default_factory=lambda: SEARCH_TRANSITIONAL_OPERATIONAL_STATES)


@dataclass
class OsueWaitResult:
    """Resultado de espera OSUE en postcondición SmartExecutor."""

    matched: bool = False
    timed_out: bool = False
    needs_human: bool = False
    incompatible: bool = False
    reason: str = ""
    expected_state: str = ""
    actual_state: str = ""
    target_states: List[str] = field(default_factory=list)
    elapsed_ms: int = 0
    polled: int = 0
    stability_score: float = 0.0
    ambiguity_score: float = 0.0
    blockers: List[str] = field(default_factory=list)
    state_lineage: List[str] = field(default_factory=list)
    transitional_states_seen: List[str] = field(default_factory=list)
    osue_wait_started: bool = False
    final_state: str = ""
    wait_duration_ms: int = 0

    def to_audit_dict(self, *, step_id: str = "") -> Dict[str, Any]:
        return {
            "step_id": step_id,
            "matched": self.matched,
            "timed_out": self.timed_out,
            "needs_human": self.needs_human,
            "incompatible": self.incompatible,
            "reason": self.reason,
            "expected_state": self.expected_state,
            "actual_state": self.actual_state,
            "final_state": self.final_state or self.actual_state,
            "target_states": list(self.target_states),
            "elapsed_ms": self.elapsed_ms,
            "wait_duration_ms": self.wait_duration_ms or self.elapsed_ms,
            "polled": self.polled,
            "stability_score": self.stability_score,
            "ambiguity_score": self.ambiguity_score,
            "blockers": list(self.blockers[:8]),
            "state_lineage": list(self.state_lineage[-12:]),
            "transitional_states_seen": list(self.transitional_states_seen),
            "osue_wait_started": bool(self.osue_wait_started),
            "expected_state_transitions": list(self.target_states),
            "actual_state_transitions": list(self.state_lineage[-12:]),
            "state_mismatch": bool(
                not self.matched and not self.timed_out and not self.needs_human,
            ),
            "state_wait_timeout": self.timed_out,
            "blocked_state_detected": self.needs_human,
            "osue_wait_reason": self.reason,
        }


@dataclass
class OperationalStateInput:
    """Cabina de sensores OSUE — inyectable / testeable."""

    lop_snapshot: Optional[LiveOperationalPerceptionSnapshot] = None
    org_graph: Optional[OperationalRuntimeGraph] = None
    machine_step: Optional[MachineExecutionStep] = None
    uia_nodes: List[Dict[str, Any]] = field(default_factory=list)
    dom: Dict[str, Any] = field(default_factory=dict)
    runtime_validators: Dict[str, Any] = field(default_factory=dict)
    context_hints: Dict[str, Any] = field(default_factory=dict)
    element_identities: List[OperationalElementIdentity] = field(default_factory=list)
    continuity_signals: List[str] = field(default_factory=list)
    arss_observations: Dict[str, Any] = field(default_factory=dict)
    orce_divergence: Dict[str, Any] = field(default_factory=dict)
    state_lineage: List[str] = field(default_factory=list)
    active_goal: str = ""
    loading_elapsed_s: float = 0.0


@dataclass
class StepStateCompatibility:
    """Resultado de compatibilidad paso ↔ estado operacional."""

    compatible: bool
    readiness: OsueReadiness
    current_state: str
    required_states: List[str]
    blocked_states: List[str]
    reasons: List[str] = field(default_factory=list)


@dataclass
class StateTransitionComparison:
    """Comparación ORCE: transiciones esperadas vs observadas."""

    aligned: bool
    expected: List[str]
    actual: List[str]
    mismatches: List[str] = field(default_factory=list)
    loading_anomaly: bool = False


def _txt(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def _lower(x: Any) -> str:
    return _txt(x).lower()


def osue_shadow_enabled(settings_obj: Optional[Any] = None) -> bool:
    if settings_obj is None:
        try:
            from app.core.config import get_settings

            settings_obj = get_settings()
        except Exception:
            return False
    return bool(getattr(settings_obj, "OSUE_SHADOW_ENABLED", True))


def osue_enabled(settings_obj: Optional[Any] = None) -> bool:
    if settings_obj is None:
        try:
            from app.core.config import get_settings

            settings_obj = get_settings()
        except Exception:
            return False
    return bool(getattr(settings_obj, "OSUE_ENABLED", False))


def required_states_for_semantic_type(semantic_type: str) -> List[str]:
    t = _lower(semantic_type)
    if t in _REQUIRED_BY_SEMANTIC:
        return list(_REQUIRED_BY_SEMANTIC[t])
    if "search" in t:
        return list(_REQUIRED_BY_SEMANTIC["search_content"])
    if "submit" in t:
        return list(_REQUIRED_BY_SEMANTIC["submit_input"])
    if "open_app" in t or "launch" in t:
        return list(_REQUIRED_BY_SEMANTIC["open_app"])
    if "navigate" in t or "open_url" in t or "open_site" in t:
        return list(_REQUIRED_BY_SEMANTIC["open_site"])
    return [OperationalStateType.INTERACTION_READY_STATE.value]


def expected_transitions_for_semantic_type(semantic_type: str) -> List[str]:
    t = _lower(semantic_type)
    if t in _EXPECTED_TRANSITIONS:
        return list(_EXPECTED_TRANSITIONS[t])
    if "search" in t:
        return list(_EXPECTED_TRANSITIONS["search_content"])
    if "submit" in t:
        return list(_EXPECTED_TRANSITIONS["submit_input"])
    return []


def blocked_states_for_semantic_type(semantic_type: str) -> List[str]:
    base = list(_UNIVERSAL_BLOCKED)
    t = _lower(semantic_type)
    if "search" in t or "submit" in t:
        base.extend(
            [
                OperationalStateType.AUTH_REQUIRED_STATE.value,
                OperationalStateType.MODAL_INTERRUPTION_STATE.value,
            ],
        )
    return list(dict.fromkeys(base))


def _has_loading_signals(
    validators: Dict[str, Any],
    uia_nodes: Sequence[Dict[str, Any]],
    dom: Dict[str, Any],
    lop: Optional[LiveOperationalPerceptionSnapshot],
) -> bool:
    if validators.get("loading") or validators.get("loading_state"):
        return True
    if validators.get("network_stalled"):
        return True
    for n in uia_nodes[:120]:
        role = _lower(n.get("role") or n.get("control_type"))
        name = _lower(n.get("name"))
        if any(m in role or m in name for m in _LOADING_MARKERS):
            return True
        if n.get("busy") or n.get("aria_busy"):
            return True
    nodes = dom.get("nodes") if isinstance(dom.get("nodes"), list) else []
    for dn in nodes[:120]:
        if not isinstance(dn, dict):
            continue
        role = _lower(dn.get("role") or dn.get("tag"))
        cls = _lower(dn.get("class") or dn.get("class_name"))
        if any(m in role or m in cls for m in _LOADING_MARKERS):
            return True
        if dn.get("aria_busy"):
            return True
    if lop:
        for b in lop.live_blockers or []:
            if b.blocker_type == "network_loading_stall":
                return True
    return False


def _count_result_items(
    lop: Optional[LiveOperationalPerceptionSnapshot],
    dom: Dict[str, Any],
) -> int:
    if lop and lop.visible_entities:
        return sum(
            1
            for e in lop.visible_entities
            if e.kind == LiveOperationalEntityKind.RESULT_ITEM
        )
    nodes = dom.get("nodes") if isinstance(dom.get("nodes"), list) else []
    return sum(
        1
        for n in nodes
        if isinstance(n, dict) and _lower(n.get("role")) in {"listitem", "article", "row"}
    )


def _detect_blocker_state(
    blockers: Sequence[LiveOperationalBlocker],
    validators: Dict[str, Any],
    context_hints: Dict[str, Any],
) -> Optional[OperationalStateType]:
    for b in blockers:
        bt = b.blocker_type
        if bt == "auth_required":
            return OperationalStateType.AUTH_REQUIRED_STATE
        if bt in {"modal_blocking", "unknown_modal"}:
            return OperationalStateType.MODAL_INTERRUPTION_STATE
        if bt == "unknown_destructive_confirmation":
            return OperationalStateType.DESTRUCTIVE_CONFIRMATION_STATE
        if bt in {"permission_dialog", "payment_context"}:
            return OperationalStateType.OVERLAY_BLOCKING_STATE
    if validators.get("modal_open") or validators.get("overlay_blocking"):
        return OperationalStateType.OVERLAY_BLOCKING_STATE
    if validators.get("auth_required") or context_hints.get("auth_screen"):
        return OperationalStateType.AUTH_REQUIRED_STATE
    if validators.get("runtime_error") or validators.get("error_visible"):
        return OperationalStateType.RUNTIME_ERROR_STATE
    if context_hints.get("unexpected_app_switch"):
        return OperationalStateType.CONTEXT_SWITCH_STATE
    return None


def infer_operational_state_type(inp: OperationalStateInput) -> Tuple[OperationalStateType, float, List[str]]:
    """Inferir estado operacional desde señales estructurales (sin hardcode de apps)."""

    lop = inp.lop_snapshot
    validators = dict(inp.runtime_validators or {})
    hints = dict(inp.context_hints or {})
    dom = dict(inp.dom or {})
    evidence: List[str] = []

    blockers: List[LiveOperationalBlocker] = list(lop.live_blockers) if lop else []
    blocked = _detect_blocker_state(blockers, validators, hints)
    if blocked:
        conf = 0.82 if blocked != OperationalStateType.UNKNOWN_OPERATIONAL_STATE else 0.4
        evidence.append(f"blocker:{blocked.value}")
        return blocked, conf, evidence

    loading = _has_loading_signals(validators, inp.uia_nodes, dom, lop)
    stall_s = float(validators.get("stall_elapsed_s") or inp.loading_elapsed_s or 0.0)
    surface = lop.surface_type if lop else LiveOperationalSurfaceType.UNKNOWN_SURFACE

    results_visible = bool(validators.get("results_visible") or validators.get("feed_visible"))
    empty_results = bool(validators.get("empty_results") or validators.get("no_results"))
    result_count = _count_result_items(lop, dom)
    search_ready = bool(
        validators.get("search_field_focused")
        or validators.get("field_focused")
        or validators.get("input_visible"),
    )
    scroll_hints = bool(validators.get("scroll_container") or hints.get("infinite_scroll"))

    if loading and stall_s > 12.0 and not results_visible:
        evidence.append(f"infinite_loading:stall_s={stall_s:.1f}")
        return OperationalStateType.LOADING_STATE, 0.78, evidence

    if loading and results_visible:
        evidence.append("async_refresh:loading+results")
        return OperationalStateType.ASYNC_REFRESH_STATE, 0.72, evidence

    if loading and validators.get("navigation_pending"):
        evidence.append("navigation_transition")
        return OperationalStateType.NAVIGATION_TRANSITION_STATE, 0.75, evidence

    if loading and not results_visible:
        if hints.get("post_submit") or validators.get("form_submitted"):
            evidence.append("results_loading:post_submit")
            return OperationalStateType.RESULTS_LOADING_STATE, 0.8, evidence
        evidence.append("loading:active")
        return OperationalStateType.LOADING_STATE, 0.76, evidence

    if results_visible or result_count >= 3:
        if empty_results or result_count == 0:
            if validators.get("search_results_visible"):
                evidence.append("search_results_visible:url_validator")
                return OperationalStateType.RESULTS_AVAILABLE_STATE, 0.86, evidence
            evidence.append("empty_results")
            return OperationalStateType.EMPTY_RESULTS_STATE, 0.74, evidence
        if scroll_hints and result_count >= 8:
            evidence.append("infinite_scroll")
            return OperationalStateType.INFINITE_SCROLL_STATE, 0.7, evidence
        evidence.append(f"results_available:count={result_count}")
        return OperationalStateType.RESULTS_AVAILABLE_STATE, 0.85, evidence

    if surface == LiveOperationalSurfaceType.SEARCH_SURFACE or search_ready:
        evidence.append("search_input_ready")
        return OperationalStateType.SEARCH_INPUT_READY_STATE, 0.82, evidence

    if surface == LiveOperationalSurfaceType.LAUNCHER_SURFACE or hints.get("launcher_like"):
        evidence.append("launcher_ready")
        return OperationalStateType.LAUNCHER_READY_STATE, 0.78, evidence

    if surface == LiveOperationalSurfaceType.BROWSER_SURFACE or validators.get("address_bar_visible"):
        evidence.append("navigation_ready")
        return OperationalStateType.NAVIGATION_READY_STATE, 0.75, evidence

    if surface == LiveOperationalSurfaceType.RESULTS_SURFACE:
        evidence.append("results_ready_surface")
        return OperationalStateType.RESULTS_READY_STATE, 0.72, evidence

    if lop and lop.actionable_entities:
        evidence.append("interaction_ready:actionable_entities")
        return OperationalStateType.INTERACTION_READY_STATE, 0.68, evidence

    if hints.get("context_mismatch") or validators.get("context_mismatch"):
        evidence.append("context_mismatch")
        return OperationalStateType.CONTEXT_SWITCH_STATE, 0.65, evidence

    evidence.append("unknown:insufficient_signals")
    return OperationalStateType.UNKNOWN_OPERATIONAL_STATE, 0.35, evidence


def _compute_ambiguity_score(inp: OperationalStateInput, state: OperationalStateType) -> float:
    score = 0.0
    if inp.lop_snapshot and inp.lop_snapshot.ambiguity_signals:
        score += min(0.35, 0.08 * len(inp.lop_snapshot.ambiguity_signals))
    for ident in inp.element_identities:
        score += min(0.25, float(ident.ambiguity_score or 0.0) * 0.5)
    if state == OperationalStateType.UNKNOWN_OPERATIONAL_STATE:
        score += 0.4
    if inp.lop_snapshot and inp.lop_snapshot.confidence < 0.45:
        score += 0.2
    return round(min(1.0, score), 4)


def _compute_stability_score(
    ambiguity: float,
    confidence: float,
    blockers: Sequence[str],
    loading: bool,
) -> float:
    base = confidence * (1.0 - ambiguity * 0.6)
    if blockers:
        base *= 0.55
    if loading:
        base *= 0.65
    return round(max(0.0, min(1.0, base)), 4)


def _visible_elements_from_identities(
    identities: Sequence[OperationalElementIdentity],
    lop: Optional[LiveOperationalPerceptionSnapshot],
) -> List[str]:
    out: List[str] = []
    for ident in identities[:24]:
        out.append(ident.operational_element_type.value)
        if ident.structural_signature:
            out.append(f"sig:{ident.structural_signature[:48]}")
    if lop:
        for e in (lop.visible_entities or [])[:16]:
            out.append(f"{e.kind.value}:{_txt(e.label_guess)[:40]}")
    return list(dict.fromkeys(out))[:32]


def _historical_from_identities(identities: Sequence[OperationalElementIdentity]) -> List[str]:
    out: List[str] = []
    for ident in identities:
        out.extend(list(ident.historical_matches or [])[:6])
    return list(dict.fromkeys(out))[:24]


def build_operational_state_snapshot(inp: OperationalStateInput) -> OperationalStateSnapshot:
    """Construye instantánea OSUE completa desde fuentes de percepción."""

    state_type, confidence, evidence = infer_operational_state_type(inp)
    lop = inp.lop_snapshot
    validators = dict(inp.runtime_validators or {})
    blockers = [b.blocker_type for b in (lop.live_blockers if lop else [])]
    loading = _has_loading_signals(validators, inp.uia_nodes, inp.dom, lop)
    ambiguity = _compute_ambiguity_score(inp, state_type)
    stability = _compute_stability_score(ambiguity, confidence, blockers, loading)

    surfaces: List[str] = []
    if lop:
        surfaces.append(lop.surface_type.value)
        surfaces.extend(s.surface_type.value for s in (lop.input_surfaces or [])[:4])

    readiness = OsueReadiness.READY.value
    if state_type.value in _UNIVERSAL_BLOCKED:
        readiness = OsueReadiness.BLOCKED.value if blockers else OsueReadiness.WAIT.value
    elif stability >= 0.55 and not blockers:
        readiness = OsueReadiness.READY.value
    elif loading:
        readiness = OsueReadiness.WAIT.value

    lineage = list(inp.state_lineage or [])
    lineage.append(state_type.value)
    lineage = lineage[-24:]

    expected = expected_transitions_for_semantic_type(
        inp.machine_step.semantic_type if inp.machine_step else "",
    )

    return OperationalStateSnapshot(
        operational_state_type=state_type,
        confidence=round(confidence, 4),
        active_goal=_txt(inp.active_goal),
        active_operational_thread=_txt(
            (lop.operational_state_guess if lop else "")
            or (inp.continuity_signals[-1] if inp.continuity_signals else ""),
        ),
        visible_operational_elements=_visible_elements_from_identities(inp.element_identities, lop),
        active_surface_types=list(dict.fromkeys(surfaces))[:8],
        continuity_state="continuous" if inp.continuity_signals else "unknown",
        loading_state="active" if loading else "idle",
        interaction_readiness=readiness,
        navigation_state="ready" if state_type == OperationalStateType.NAVIGATION_READY_STATE else "unknown",
        auth_state="required" if state_type == OperationalStateType.AUTH_REQUIRED_STATE else "none",
        modal_state="open" if state_type in {
            OperationalStateType.MODAL_INTERRUPTION_STATE,
            OperationalStateType.OVERLAY_BLOCKING_STATE,
        } else "closed",
        results_state=state_type.value if "results" in state_type.value else "",
        error_state="present" if state_type == OperationalStateType.RUNTIME_ERROR_STATE else "",
        focus_state="focused" if validators.get("field_focused") else "unknown",
        runtime_blockers=blockers[:12],
        expected_transitions=expected,
        observed_transitions=list(lineage[-6:]),
        state_stability_score=stability,
        ambiguity_score=ambiguity,
        state_lineage=lineage,
        historical_matches=_historical_from_identities(inp.element_identities),
    )


def evaluate_step_compatibility(
    snapshot: OperationalStateSnapshot,
    machine_step: MachineExecutionStep,
) -> StepStateCompatibility:
    """Valida si el estado operacional actual es compatible con un Machine Step."""

    params = dict(machine_step.params or {})
    required = list(params.get("required_operational_states") or [])
    if not required:
        required = required_states_for_semantic_type(machine_step.semantic_type)
    blocked = list(params.get("blocked_operational_states") or [])
    if not blocked:
        blocked = blocked_states_for_semantic_type(machine_step.semantic_type)

    current = snapshot.operational_state_type.value
    reasons: List[str] = []

    if current in blocked:
        reasons.append(f"state_blocked:{current}")
    if required and current not in required and current not in {
        OperationalStateType.INTERACTION_READY_STATE.value,
        OperationalStateType.RESULTS_READY_STATE.value,
    }:
        if not any(r in current for r in required):
            reasons.append(f"required_mismatch:need={required} got={current}")

    if snapshot.state_stability_score < 0.45:
        reasons.append(f"unstable:score={snapshot.state_stability_score:.2f}")
    if snapshot.ambiguity_score > 0.55:
        reasons.append(f"ambiguous:score={snapshot.ambiguity_score:.2f}")

    compatible = len(reasons) == 0
    if compatible:
        readiness = OsueReadiness.READY
    elif current in blocked or snapshot.runtime_blockers:
        readiness = OsueReadiness.BLOCKED
    elif snapshot.loading_state == "active":
        readiness = OsueReadiness.WAIT
    else:
        readiness = OsueReadiness.UNKNOWN

    return StepStateCompatibility(
        compatible=compatible,
        readiness=readiness,
        current_state=current,
        required_states=required,
        blocked_states=blocked,
        reasons=reasons,
    )


def machine_step_allowed(snapshot: OperationalStateSnapshot, machine_step: MachineExecutionStep) -> bool:
    """Regla central: NO ejecutar Machine Step si el estado NO es compatible."""
    if not osue_enabled():
        return True
    return evaluate_step_compatibility(snapshot, machine_step).compatible


def attach_osue_states_to_model(model: FourLayerOperationalModel) -> None:
    """Enriquece Machine Layer con contratos de estado operacional (4LOM)."""

    for ms in model.machine_execution.steps:
        if ms.disabled:
            continue
        sem = str(ms.semantic_type or "")
        required = required_states_for_semantic_type(sem)
        transitions = expected_transitions_for_semantic_type(sem)
        blocked = blocked_states_for_semantic_type(sem)

        pars = dict(ms.params or {})
        pars["required_operational_states"] = required
        pars["expected_state_transitions"] = transitions
        pars["blocked_operational_states"] = blocked
        ms.params = pars

        if ms.runtime_expectation is not None:
            exp = ms.runtime_expectation
            exp.required_operational_states = required
            exp.expected_state_transitions = transitions
            exp.blocked_operational_states = blocked
            if transitions:
                exp.expected_transition_chain = transitions[:]


def resolve_machine_step_for_mission(
    mission: Optional[Mission],
    step_id: str,
) -> Optional[MachineExecutionStep]:
    if not mission or not step_id:
        return None
    try:
        from app.services.missions.four_layer_operational_model import ensure_four_layer_model

        model = ensure_four_layer_model(mission)
        for ms in model.machine_execution.steps:
            if ms.step_id == step_id and not ms.disabled:
                return ms
    except Exception:
        pass
    return None


def resolve_postcondition_target_states(
    machine_step: Optional[MachineExecutionStep],
    step_kind: str = "",
) -> List[str]:
    """Estados operacionales a esperar tras ejecutar un paso (postcondición OSUE)."""

    if machine_step is not None:
        pars = dict(machine_step.params or {})
        trans = list(pars.get("expected_state_transitions") or [])
        exp = machine_step.runtime_expectation
        if not trans and exp is not None:
            trans = list(exp.expected_state_transitions or [])
        if trans:
            terminal = trans[-1]
            if len(trans) >= 2 and trans[0] == terminal:
                return [terminal]
            return [terminal]
        required = list(pars.get("required_operational_states") or [])
        if not required and exp is not None:
            required = list(exp.required_operational_states or [])
        if required:
            return [required[0]]

    kind = _lower(step_kind)
    if kind in _POST_TARGET_BY_KIND:
        return [_POST_TARGET_BY_KIND[kind]]
    if "search" in kind or "submit" in kind:
        return [OperationalStateType.RESULTS_AVAILABLE_STATE.value]
    if "open_app" in kind or "launch" in kind:
        return [OperationalStateType.LAUNCHER_READY_STATE.value]
    if "navigate" in kind or "open_url" in kind or "open_site" in kind:
        return [OperationalStateType.NAVIGATION_READY_STATE.value]
    return [OperationalStateType.INTERACTION_READY_STATE.value]


def build_osue_input_from_state_snapshot(
    state: Any,
    *,
    mission: Optional[Mission] = None,
    step: Optional[Any] = None,
    machine_step: Optional[MachineExecutionStep] = None,
    extra_validators: Optional[Dict[str, Any]] = None,
    state_lineage: Optional[List[str]] = None,
) -> OperationalStateInput:
    """Puente StateSnapshot (SmartExecutor) → OperationalStateInput (OSUE)."""

    validators: Dict[str, Any] = dict(extra_validators or {})
    uia_nodes: List[Dict[str, Any]] = []
    dom: Dict[str, Any] = {"nodes": []}
    context_hints: Dict[str, Any] = {}

    browser_url = _txt(getattr(state, "browser_url", None))
    if browser_url:
        dom["url"] = browser_url
        validators.setdefault("url_matches", True)
        low_url = browser_url.lower()
        if "search_query=" in low_url or "/results" in low_url:
            validators.setdefault("results_visible", True)
            validators.setdefault("search_results_visible", True)

    visible_names = list(getattr(state, "uia_visible_names", None) or [])
    for name in visible_names[:40]:
        low = _lower(name)
        uia_nodes.append({"role": "listitem", "name": name})
        if "search" in low:
            validators.setdefault("search_field_focused", True)
            validators.setdefault("input_visible", True)
        if any(m in low for m in _LOADING_MARKERS):
            validators.setdefault("loading", True)

    if len(visible_names) >= 4:
        validators.setdefault("results_visible", True)

    if getattr(state, "uia_targets_available", False):
        validators.setdefault("input_visible", True)

    if getattr(state, "dom_targets_available", False):
        validators.setdefault("field_focused", True)

    if validators.get("post_submit") or validators.get("form_submitted"):
        context_hints["post_submit"] = True
        validators.setdefault("navigation_pending", True)

    active_app = _txt(getattr(state, "active_app", None))
    if active_app:
        context_hints["active_app"] = active_app

    lop_snap = None
    raw_lop = getattr(mission, "lop_last_snapshot", None) if mission else None
    if isinstance(raw_lop, dict) and raw_lop.get("surface_type"):
        try:
            lop_snap = LiveOperationalPerceptionSnapshot.model_validate(raw_lop)
        except Exception:
            lop_snap = None

    return OperationalStateInput(
        lop_snapshot=lop_snap,
        machine_step=machine_step,
        uia_nodes=uia_nodes,
        dom=dom,
        runtime_validators=validators,
        context_hints=context_hints,
        state_lineage=list(state_lineage or []),
        active_goal=_txt(getattr(mission, "name", "") if mission else ""),
    )


def is_search_submit_step(step: Any) -> bool:
    """True si el paso es búsqueda con envío explícito (post-submit wait)."""
    if step is None:
        return False
    kind = _lower(getattr(step, "kind", "") or "")
    if kind not in {
        "search_site",
        "search_youtube",
        "search_content",
        "search_web",
        "submit_input",
    }:
        return False
    params = dict(getattr(step, "params", None) or {})
    submit = params.get("submit")
    if submit is False:
        return False
    return bool(params.get("query") or params.get("search_query") or params.get("text"))


def resolve_search_osue_wait_profile(
    step: Any,
    *,
    deadline_ms: Optional[int] = None,
) -> SearchOsueWaitProfile:
    """Perfil wait_until para search_content/search_site con submit."""
    params = dict(getattr(step, "params", None) or {})
    max_wait = int(deadline_ms or params.get("postcondition_timeout_ms") or 8000)
    max_wait = max(2000, min(max_wait, 15000))
    min_settle = int(params.get("search_min_settle_ms") or 400)
    return SearchOsueWaitProfile(
        min_settle_ms=min_settle,
        max_wait_ms=max_wait,
        poll_ms_initial=150,
        poll_ms_max=550,
        stagnant_polls_for_failure=16,
    )


def _results_terminal_operational_states() -> Set[str]:
    return {
        OperationalStateType.RESULTS_AVAILABLE_STATE.value,
        OperationalStateType.RESULTS_READY_STATE.value,
        OperationalStateType.INFINITE_SCROLL_STATE.value,
    }


def wait_for_search_results_after_submit(
    step: Any,
    *,
    detect_fn: Callable[[], Any],
    validate_fn: Callable[[Any, Any], bool],
    profile: Optional[SearchOsueWaitProfile] = None,
    snapshot_factory: Optional[Callable[[], OperationalStateSnapshot]] = None,
) -> Tuple[bool, OsueWaitResult]:
    """Espera dinámica post-submit hasta confirmar resultados o timeout real."""

    prof = profile or resolve_search_osue_wait_profile(step)
    targets = [OperationalStateType.RESULTS_AVAILABLE_STATE.value]
    started = time.monotonic()
    transitional_seen: List[str] = []
    lineage: List[str] = []
    poll_ms = prof.poll_ms_initial
    polled = 0
    stagnant = 0
    last_sig = ""
    last_snap: Optional[OperationalStateSnapshot] = None

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def _make_result(**kwargs: Any) -> OsueWaitResult:
        base = dict(
            target_states=list(targets),
            elapsed_ms=_elapsed_ms(),
            wait_duration_ms=_elapsed_ms(),
            polled=polled,
            state_lineage=list(lineage),
            transitional_states_seen=list(transitional_seen),
            osue_wait_started=True,
            final_state=kwargs.pop("actual_state", ""),
        )
        base.update(kwargs)
        if last_snap is not None:
            base.setdefault("stability_score", last_snap.state_stability_score)
            base.setdefault("ambiguity_score", last_snap.ambiguity_score)
            base.setdefault("blockers", list(last_snap.runtime_blockers))
        return OsueWaitResult(**base)

    while _elapsed_ms() < prof.max_wait_ms:
        polled += 1
        state = detect_fn()
        if validate_fn(step, state):
            return True, _make_result(
                matched=True,
                reason="RESULTS_CONFIRMED",
                expected_state=targets[0],
                actual_state=OperationalStateType.RESULTS_AVAILABLE_STATE.value,
            )

        current = ""
        if snapshot_factory is not None:
            last_snap = snapshot_factory()
            current = last_snap.operational_state_type.value
            lineage.append(current)
            if current in _HUMAN_GATE_STATES:
                return False, _make_result(
                    needs_human=True,
                    reason="BLOCKED_STATE",
                    expected_state=targets[0],
                    actual_state=current,
                )
            if current in prof.transitional_states:
                if current not in transitional_seen:
                    transitional_seen.append(current)
                stagnant = 0
            elif current in _results_terminal_operational_states():
                return True, _make_result(
                    matched=True,
                    reason="RESULTS_CONFIRMED",
                    expected_state=targets[0],
                    actual_state=current,
                )
            else:
                sig = f"{current}|{getattr(state, 'browser_url', '')}"
                if sig == last_sig:
                    stagnant += 1
                else:
                    stagnant = 0
                    last_sig = sig

        if _elapsed_ms() >= prof.min_settle_ms:
            if validate_fn(step, state):
                return True, _make_result(
                    matched=True,
                    reason="RESULTS_CONFIRMED",
                    expected_state=targets[0],
                    actual_state=current or OperationalStateType.RESULTS_AVAILABLE_STATE.value,
                )

        time.sleep(max(0.05, poll_ms / 1000.0))
        poll_ms = min(prof.poll_ms_max, int(poll_ms * 1.2))

    actual = last_snap.operational_state_type.value if last_snap else ""
    return False, _make_result(
        timed_out=True,
        reason="RESULTS_TIMEOUT",
        expected_state=targets[0],
        actual_state=actual,
        incompatible=False,
    )


def search_wait_should_block_recovery(wait_result: Optional[OsueWaitResult]) -> bool:
    """True si el recovery no debe ejecutarse por fase transitoria post-submit."""
    if wait_result is None:
        return False
    if wait_result.matched or wait_result.needs_human:
        return False
    if wait_result.transitional_states_seen and not wait_result.timed_out:
        return True
    if wait_result.reason == "RESULTS_TIMEOUT" and wait_result.transitional_states_seen:
        return True
    return False


def run_osue_postcondition_wait(
    *,
    target_states: Sequence[str],
    snapshot_factory: Callable[[], OperationalStateSnapshot],
    timeout_ms: int,
    poll_ms: int = 200,
    blocked_states: Optional[Sequence[str]] = None,
    wait_profile: Optional[SearchOsueWaitProfile] = None,
) -> OsueWaitResult:
    """Espera postcondición por estado operacional — polling OSUE, no sleep fijo primario."""
    from app.services.runtime.entity_runtime_resolution import NO_STATE_PROGRESS

    targets = [str(t) for t in target_states if t]
    if not targets:
        targets = [OperationalStateType.INTERACTION_READY_STATE.value]

    prof = wait_profile
    transitional = set(prof.transitional_states if prof else SEARCH_TRANSITIONAL_OPERATIONAL_STATES)
    min_settle_ms = prof.min_settle_ms if prof else 0
    stagnant_limit = prof.stagnant_polls_for_failure if prof else 5
    blocked_set = set(blocked_states or [])
    conditions = {t: build_wait_condition_for_state(t) for t in targets}
    started = time.monotonic()
    deadline = started + ((prof.max_wait_ms if prof else timeout_ms) / 1000.0)
    polled = 0
    last_snap: Optional[OperationalStateSnapshot] = None
    lineage: List[str] = []
    transitional_seen: List[str] = []
    stagnant = 0
    last_sig = ""
    interval_ms = prof.poll_ms_initial if prof else poll_ms
    max_interval_ms = prof.poll_ms_max if prof else poll_ms

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    while time.monotonic() < deadline:
        polled += 1
        snap = snapshot_factory()
        last_snap = snap
        current = snap.operational_state_type.value
        lineage.append(current)

        if current in transitional:
            if current not in transitional_seen:
                transitional_seen.append(current)
            stagnant = 0
        elif current in _results_terminal_operational_states():
            stagnant = 0
        else:
            sig = f"{current}|{snap.loading_state}"
            if sig == last_sig:
                stagnant += 1
            else:
                stagnant = 0
                last_sig = sig

        if current in _HUMAN_GATE_STATES:
            return OsueWaitResult(
                needs_human=True,
                reason="BLOCKED_STATE" if wait_profile else f"OSUE_BLOCKED:{current}",
                expected_state=targets[-1],
                actual_state=current,
                target_states=list(targets),
                elapsed_ms=_elapsed_ms(),
                wait_duration_ms=_elapsed_ms(),
                polled=polled,
                stability_score=snap.state_stability_score,
                ambiguity_score=snap.ambiguity_score,
                blockers=list(snap.runtime_blockers),
                state_lineage=list(lineage),
                transitional_states_seen=list(transitional_seen),
                osue_wait_started=True,
                final_state=current,
            )

        if (
            _elapsed_ms() >= min_settle_ms
            and stagnant >= stagnant_limit
            and current not in transitional
            and current not in _results_terminal_operational_states()
        ):
            pass

        if (
            current in blocked_set
            and current not in transitional
            and snap.loading_state != "active"
            and not (
                transitional_seen
                and current == OperationalStateType.EMPTY_RESULTS_STATE.value
            )
        ):
            return OsueWaitResult(
                incompatible=True,
                reason="OSUE_STATE_INCOMPATIBLE",
                expected_state=targets[-1],
                actual_state=current,
                target_states=list(targets),
                elapsed_ms=_elapsed_ms(),
                wait_duration_ms=_elapsed_ms(),
                polled=polled,
                stability_score=snap.state_stability_score,
                ambiguity_score=snap.ambiguity_score,
                blockers=list(snap.runtime_blockers),
                state_lineage=list(lineage),
                transitional_states_seen=list(transitional_seen),
                osue_wait_started=True,
                final_state=current,
            )

        for target in targets:
            if conditions[target](snap):
                return OsueWaitResult(
                    matched=True,
                    reason="RESULTS_CONFIRMED" if wait_profile else "OSUE_STATE_REACHED",
                    expected_state=target,
                    actual_state=current,
                    target_states=list(targets),
                    elapsed_ms=_elapsed_ms(),
                    wait_duration_ms=_elapsed_ms(),
                    polled=polled,
                    stability_score=snap.state_stability_score,
                    ambiguity_score=snap.ambiguity_score,
                    blockers=list(snap.runtime_blockers),
                    state_lineage=list(lineage),
                    transitional_states_seen=list(transitional_seen),
                    osue_wait_started=True,
                    final_state=current,
                )

        time.sleep(max(0.05, interval_ms / 1000.0))
        interval_ms = min(max_interval_ms, int(interval_ms * 1.2))

    actual = last_snap.operational_state_type.value if last_snap else ""
    return OsueWaitResult(
        timed_out=True,
        reason="RESULTS_TIMEOUT" if wait_profile else "OSUE_STATE_TIMEOUT",
        expected_state=targets[-1],
        actual_state=actual,
        target_states=list(targets),
        elapsed_ms=_elapsed_ms(),
        wait_duration_ms=_elapsed_ms(),
        polled=polled,
        stability_score=(last_snap.state_stability_score if last_snap else 0.0),
        ambiguity_score=(last_snap.ambiguity_score if last_snap else 0.0),
        blockers=list(last_snap.runtime_blockers) if last_snap else [],
        state_lineage=list(lineage),
        transitional_states_seen=list(transitional_seen),
        osue_wait_started=True,
        final_state=actual,
    )


def persist_osue_wait_on_mission(
    mission: Optional[Mission],
    *,
    step_id: str,
    wait_result: OsueWaitResult,
) -> None:
    if mission is None:
        return
    row = wait_result.to_audit_dict(step_id=step_id)
    audit = list(getattr(mission, "osue_runtime_wait_audit", None) or [])
    audit.append(row)
    mission.osue_runtime_wait_audit = audit[-100:]
    if wait_result.state_lineage:
        lineage = list(getattr(mission, "osue_state_lineage", None) or [])
        lineage.extend(wait_result.state_lineage)
        mission.osue_state_lineage = lineage[-48:]
    mission.osue_last_snapshot = {
        "operational_state_type": wait_result.actual_state,
        "state_stability_score": wait_result.stability_score,
        "ambiguity_score": wait_result.ambiguity_score,
        "runtime_blockers": wait_result.blockers,
        "expected_transitions": wait_result.target_states,
        "observed_transitions": wait_result.state_lineage,
    }


def build_wait_condition_for_state(
    target_state: str,
) -> Callable[[Any], bool]:
    """Condición wait_until basada en estado operacional — NO sleep fijo."""

    target = _lower(target_state)

    def _condition(ctx: Any) -> bool:
        snap = ctx
        if hasattr(ctx, "operational_state_type"):
            current = _lower(getattr(ctx.operational_state_type, "value", ctx.operational_state_type))
        elif isinstance(ctx, dict):
            ost = ctx.get("operational_state_type") or ctx.get("operational_state")
            current = _lower(ost.value if hasattr(ost, "value") else ost)
        else:
            return False
        if current == target:
            return True
        if target == OperationalStateType.RESULTS_AVAILABLE_STATE.value:
            return current in {
                OperationalStateType.RESULTS_AVAILABLE_STATE.value,
                OperationalStateType.RESULTS_READY_STATE.value,
                OperationalStateType.INFINITE_SCROLL_STATE.value,
            }
        if target == OperationalStateType.SEARCH_INPUT_READY_STATE.value:
            return current == OperationalStateType.SEARCH_INPUT_READY_STATE.value
        return False

    _condition.__name__ = f"osue_wait_{target_state}"
    return _condition


def wait_for_operational_state(
    target_state: str,
    *,
    snapshot_factory: Callable[[], OperationalStateSnapshot],
    timeout_ms: int = 12_000,
    poll_ms: int = 200,
) -> Any:
    """Espera dinámica hasta estado operacional — usa wait_manager, NO sleep primario."""

    from app.services.missions.wait_manager import WaitResult, wait_until

    condition = build_wait_condition_for_state(target_state)

    def _factory() -> OperationalStateSnapshot:
        return snapshot_factory()

    result: WaitResult = wait_until(
        condition,
        timeout_ms=timeout_ms,
        poll_ms=poll_ms,
        label=f"osue:{target_state}",
        snapshot_factory=_factory,
    )
    return result


def replay_should_pause(snapshot: OperationalStateSnapshot) -> bool:
    """Replay debe pausar durante loading/transición — no avanzar a ciegas."""

    pause_states = {
        OperationalStateType.LOADING_STATE.value,
        OperationalStateType.RESULTS_LOADING_STATE.value,
        OperationalStateType.NAVIGATION_TRANSITION_STATE.value,
        OperationalStateType.ASYNC_REFRESH_STATE.value,
    }
    if snapshot.operational_state_type.value in pause_states:
        return True
    if snapshot.loading_state == "active" and snapshot.state_stability_score < 0.5:
        return True
    return False


def arss_synthesis_blocked(snapshot: OperationalStateSnapshot) -> Tuple[bool, List[str]]:
    """ARSS NO puede sintetizar mechanics si el estado es ambiguo o inestable."""

    reasons: List[str] = []
    if snapshot.ambiguity_score > 0.5:
        reasons.append(f"ambiguous:{snapshot.ambiguity_score:.2f}")
    if snapshot.state_stability_score < 0.5:
        reasons.append(f"unstable:{snapshot.state_stability_score:.2f}")
    if snapshot.operational_state_type == OperationalStateType.UNKNOWN_OPERATIONAL_STATE:
        reasons.append("unknown_state")
    if snapshot.loading_state == "active":
        reasons.append("loading_active")
    return (len(reasons) > 0, reasons)


def arss_recovery_action_for_snapshot(snapshot: OperationalStateSnapshot) -> str:
    """Acción ARSS cuando OSUE bloquea síntesis: wait, human o revalidate."""

    st = snapshot.operational_state_type.value
    if st in _HUMAN_GATE_STATES or snapshot.auth_state == "required":
        return "request_human"
    if snapshot.loading_state == "active" or st in {
        OperationalStateType.LOADING_STATE.value,
        OperationalStateType.RESULTS_LOADING_STATE.value,
        OperationalStateType.ASYNC_REFRESH_STATE.value,
    }:
        return "wait_for_state_completion"
    if snapshot.state_stability_score < 0.5 or snapshot.ambiguity_score > 0.5:
        return "revalidate_operational_state"
    return "wait_for_state_completion"


def orce_compare_state_transitions(
    expected: Sequence[str],
    actual: Sequence[str],
    *,
    loading_anomaly_threshold_s: float = 12.0,
    loading_elapsed_s: float = 0.0,
) -> StateTransitionComparison:
    """ORCE compara transiciones de estado esperadas vs observadas."""

    exp = [str(x) for x in expected if x]
    act = [str(x) for x in actual if x]
    mismatches: List[str] = []

    loading_anomaly = False
    if loading_elapsed_s > loading_anomaly_threshold_s:
        if OperationalStateType.RESULTS_AVAILABLE_STATE.value in exp:
            if OperationalStateType.RESULTS_AVAILABLE_STATE.value not in act:
                loading_anomaly = True
                mismatches.append("infinite_loading:no_results_available")

    if exp and act:
        final_exp = exp[-1]
        final_act = act[-1]
        if final_exp != final_act and final_act not in exp:
            mismatches.append(f"terminal_mismatch:expected={final_exp} actual={final_act}")

    for i, e in enumerate(exp[: len(act)]):
        if act[i] != e:
            mismatches.append(f"step_{i}:expected={e} actual={act[i]}")

    aligned = len(mismatches) == 0
    return StateTransitionComparison(
        aligned=aligned,
        expected=exp,
        actual=act,
        mismatches=mismatches,
        loading_anomaly=loading_anomaly,
    )


def enrich_observation_with_osue_states(
    observation: OperationalRuntimeObservation,
    state_chain: Sequence[str],
) -> OperationalRuntimeObservation:
    """Añade cadena de estados operacionales a observación ORCE."""

    observation.runtime_operational_state_chain = list(state_chain)
    return observation


def detect_context_mismatch(inp: OperationalStateInput) -> bool:
    """Detecta desalineación de contexto operacional (sin URL/title hardcode)."""

    hints = dict(inp.context_hints or {})
    validators = dict(inp.runtime_validators or {})
    if hints.get("context_mismatch") or validators.get("context_mismatch"):
        return True
    expected = hints.get("expected_surface_types") or []
    lop = inp.lop_snapshot
    if isinstance(expected, list) and expected and lop:
        if lop.surface_type.value not in {str(x) for x in expected}:
            return True
    for b in (lop.live_blockers if lop else []):
        if b.blocker_type in {"missing_required_surface", "unexpected_app_switch", "validator_mismatch"}:
            return True
    return False


def runtime_recovery_from_blocked(snapshot: OperationalStateSnapshot) -> str:
    """Sugerencia de recuperación desde estado bloqueado."""

    st = snapshot.operational_state_type
    if st == OperationalStateType.MODAL_INTERRUPTION_STATE:
        return "dismiss_modal_or_request_human"
    if st == OperationalStateType.AUTH_REQUIRED_STATE:
        return "request_human_auth"
    if st == OperationalStateType.LOADING_STATE:
        return "wait_for_state_completion"
    if st == OperationalStateType.CONTEXT_SWITCH_STATE:
        return "restore_operational_context"
    if st == OperationalStateType.RUNTIME_ERROR_STATE:
        return "retry_or_self_heal"
    if snapshot.loading_state == "active":
        return "wait_for_state_completion"
    return "revalidate_operational_state"


def capture_operational_state_from_lop(
    mission: Mission,
    *,
    perception_input: Optional[Any] = None,
    element_identities: Optional[List[OperationalElementIdentity]] = None,
    machine_step: Optional[MachineExecutionStep] = None,
) -> OperationalStateSnapshot:
    """Captura estado operacional fusionando LOP + sensores."""

    from app.services.runtime.live_operational_perception import (
        LivePerceptionInput,
        capture_live_operational_snapshot,
    )

    lop_snap: Optional[LiveOperationalPerceptionSnapshot] = None
    uia: List[Dict[str, Any]] = []
    dom: Dict[str, Any] = {}
    validators: Dict[str, Any] = {}
    hints: Dict[str, Any] = {}

    if perception_input is not None:
        if isinstance(perception_input, LivePerceptionInput):
            uia = list(perception_input.uia_nodes or [])
            dom = dict(perception_input.dom or {})
            validators = dict(perception_input.runtime_validators or {})
            hints = dict(perception_input.context_hints or {})
        elif isinstance(perception_input, dict):
            uia = list(perception_input.get("uia_nodes") or [])
            dom = dict(perception_input.get("dom") or {})
            validators = dict(perception_input.get("runtime_validators") or {})
            hints = dict(perception_input.get("context_hints") or {})

    lop_snap = capture_live_operational_snapshot(
        mission,
        perception_input=LivePerceptionInput(
            uia_nodes=uia,
            dom=dom,
            runtime_validators=validators,
            context_hints=hints,
        )
        if perception_input is not None
        else None,
    )

    lineage = list(getattr(mission, "osue_state_lineage", None) or [])

    inp = OperationalStateInput(
        lop_snapshot=lop_snap,
        machine_step=machine_step,
        uia_nodes=uia,
        dom=dom,
        runtime_validators=validators,
        context_hints=hints,
        element_identities=list(element_identities or []),
        continuity_signals=[s.kind for s in (lop_snap.continuity_signals or [])[:6]],
        state_lineage=lineage,
        loading_elapsed_s=float(validators.get("stall_elapsed_s") or 0.0),
    )

    if detect_context_mismatch(inp):
        hints["context_mismatch"] = True
        inp.context_hints = hints

    return build_operational_state_snapshot(inp)


def refresh_osue_shadow(
    mission: Mission,
    settings: Optional[Any] = None,
    *,
    perception_input: Optional[Any] = None,
    machine_step: Optional[MachineExecutionStep] = None,
    element_identities: Optional[List[OperationalElementIdentity]] = None,
) -> OperationalStateSnapshot:
    """Persiste snapshot OSUE en Mission (sombra). No muta SEP."""

    if not getattr(mission, "osue_shadow_mode", False):
        snap = OperationalStateSnapshot(
            operational_state_type=OperationalStateType.UNKNOWN_OPERATIONAL_STATE,
            confidence=0.0,
        )
        mission.osue_state_audit = {"ok": False, "reason": "osue_shadow_mode_false"}
        return snap

    if not osue_shadow_enabled(settings):
        snap = OperationalStateSnapshot(
            operational_state_type=OperationalStateType.UNKNOWN_OPERATIONAL_STATE,
            confidence=0.0,
        )
        mission.osue_state_audit = {"ok": False, "reason": "OSUE_SHADOW_DISABLED"}
        mission.osue_last_snapshot = snap.model_dump(mode="json")
        return snap

    try:
        snap = capture_operational_state_from_lop(
            mission,
            perception_input=perception_input,
            element_identities=element_identities,
            machine_step=machine_step,
        )
        lineage = list(getattr(mission, "osue_state_lineage", None) or [])
        lineage.append(snap.operational_state_type.value)
        mission.osue_state_lineage = lineage[-48:]
        mission.osue_last_snapshot = snap.model_dump(mode="json")
        mission.osue_state_audit = {
            "ok": True,
            "state": snap.operational_state_type.value,
            "stability": snap.state_stability_score,
            "ambiguity": snap.ambiguity_score,
            "blockers": snap.runtime_blockers[:6],
            "readiness": snap.interaction_readiness,
        }
        _persist_osue_audit_row(mission, snap)
        return snap
    except Exception as exc:
        log.debug("[OSUE] refresh failed: %s", exc)
        snap = OperationalStateSnapshot(
            operational_state_type=OperationalStateType.UNKNOWN_OPERATIONAL_STATE,
            confidence=0.0,
        )
        mission.osue_state_audit = {"ok": False, "reason": str(exc)[:120]}
        return snap


def _persist_osue_audit_row(mission: Mission, snap: OperationalStateSnapshot) -> None:
    try:
        _OSUE_JSONL.parent.mkdir(parents=True, exist_ok=True)
        row = snap.model_dump(mode="json")
        row["mission_id"] = str(getattr(mission, "id", ""))
        row["recorded_at"] = time.time()
        with _OSUE_JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.debug("[OSUE] jsonl: %s", exc)


def expert_panel_lines(mission: Mission, *, expert_mode: bool = True) -> List[str]:
    """Mission Review — modo experto muestra transiciones/bloqueos; normal sin ruido."""

    if not expert_mode:
        return []

    raw = getattr(mission, "osue_last_snapshot", None)
    audit = getattr(mission, "osue_state_audit", None) or {}
    wait_rows = list(getattr(mission, "osue_runtime_wait_audit", None) or [])
    if not raw and not audit and not wait_rows:
        return []

    lines = ["OSUE — Operational State"]
    if isinstance(raw, dict):
        st = raw.get("operational_state_type", "unknown")
        lines.append(f"  estado: {st} · conf: {float(raw.get('confidence') or 0):.2f}")
        lines.append(
            f"  estabilidad: {float(raw.get('state_stability_score') or 0):.2f} · "
            f"ambigüedad: {float(raw.get('ambiguity_score') or 0):.2f}",
        )
        blockers = raw.get("runtime_blockers") or []
        if blockers:
            lines.append(f"  bloqueos: {', '.join(str(b) for b in blockers[:4])}")
        exp = raw.get("expected_transitions") or []
        obs = raw.get("observed_transitions") or raw.get("state_lineage") or []
        if exp or obs:
            lines.append(f"  EXPECTED: {' → '.join(str(x) for x in exp[:5]) or '—'}")
            lines.append(f"  OBSERVED: {' → '.join(str(x) for x in obs[-5:]) or '—'}")
        if audit.get("loading_anomaly"):
            lines.append("  ⚠ anomalía: loading eterno detectado")
    elif audit:
        lines.append(f"  audit: {audit.get('reason') or audit.get('state', '—')}")

    wait_rows = list(getattr(mission, "osue_runtime_wait_audit", None) or [])
    if wait_rows:
        last_wait = wait_rows[-1]
        lines.append("")
        lines.append("OSUE — Runtime Wait")
        lines.append(
            f"  wait: {'OK' if last_wait.get('matched') else 'FAIL'} · "
            f"reason: {last_wait.get('osue_wait_reason') or last_wait.get('reason') or '—'}",
        )
        lines.append(
            f"  expected: {last_wait.get('expected_state') or '—'} · "
            f"actual: {last_wait.get('actual_state') or '—'}",
        )
        if last_wait.get("state_wait_timeout"):
            lines.append("  ⚠ timeout esperando estado operacional")
        if last_wait.get("blocked_state_detected"):
            lines.append(f"  ⚠ blocker: {', '.join(last_wait.get('blockers') or [])[:80]}")
        lines.append(
            f"  stability: {float(last_wait.get('stability_score') or 0):.2f} · "
            f"polls: {int(last_wait.get('polled') or 0)} · "
            f"{int(last_wait.get('elapsed_ms') or 0)}ms",
        )

    lineage = getattr(mission, "osue_state_lineage", None) or []
    if len(lineage) >= 2:
        cmp = orce_compare_state_transitions(
            expected_transitions_for_semantic_type("search_content"),
            lineage[-4:],
        )
        if not cmp.aligned and cmp.mismatches:
            lines.append(f"  mismatch: {cmp.mismatches[0][:80]}")

    return lines


__all__ = [
    "OperationalStateInput",
    "OsueReadiness",
    "OsueWaitResult",
    "StateTransitionComparison",
    "StepStateCompatibility",
    "arss_recovery_action_for_snapshot",
    "arss_synthesis_blocked",
    "attach_osue_states_to_model",
    "blocked_states_for_semantic_type",
    "build_operational_state_snapshot",
    "build_osue_input_from_state_snapshot",
    "build_wait_condition_for_state",
    "capture_operational_state_from_lop",
    "detect_context_mismatch",
    "enrich_observation_with_osue_states",
    "evaluate_step_compatibility",
    "expected_transitions_for_semantic_type",
    "expert_panel_lines",
    "infer_operational_state_type",
    "machine_step_allowed",
    "orce_compare_state_transitions",
    "osue_enabled",
    "osue_shadow_enabled",
    "persist_osue_wait_on_mission",
    "refresh_osue_shadow",
    "replay_should_pause",
    "required_states_for_semantic_type",
    "resolve_machine_step_for_mission",
    "resolve_postcondition_target_states",
    "resolve_search_osue_wait_profile",
    "run_osue_postcondition_wait",
    "search_wait_should_block_recovery",
    "SearchOsueWaitProfile",
    "is_search_submit_step",
    "wait_for_search_results_after_submit",
    "runtime_recovery_from_blocked",
    "wait_for_operational_state",
]
