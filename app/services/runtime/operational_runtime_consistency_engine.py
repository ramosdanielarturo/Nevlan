"""Operational Runtime Consistency Engine (ORCE).

Garantiza que la ejecución real permanezca alineada con Goal, Contract, Human View
y Machine Execution Layer; detecta degradaciones, fallbacks ocultos y divergencias.

Flujo: 4LOM Machine → OperationalExecutionExpectation → runtime →
OperationalRuntimeObservation → reconciliación → reporte → acceptance / learning.
"""
from __future__ import annotations

import json
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel

from app.contracts.mission import (
    FourLayerOperationalModel,
    MachineExecutionStep,
    Mission,
    OperationalExecutionExpectation,
    OperationalRuntimeConsistencyReport,
    OperationalRuntimeObservation,
)
from app.core.logger import log
from app.core.paths import VAR_DIR

_ORCE_JSONL = VAR_DIR / "runtime" / "orce_runtime_audit.jsonl"

# Marcadores genéricos (sin apps concretas).
_COORD_MARKERS = (
    "relative_coords",
    "absolute_coords",
    "use_coords",
    "vision_coords",
    ":coords",
    "coords_emergency",
    "coords_first",
)
_SMART_ROUTE_MARKERS = ("smart_route", "smart_route_primary")
_FORBIDDEN_CANONICAL = (
    "coords_first",
    "coords_emergency",
    "use_coords",
    "relative_coords",
    "absolute_coords",
    "vision_coords",
    "smart_route_primary",
    "legacy_blind_retry",
)
_REPAIR_MARKERS = ("repair", "self_heal", "recovery", "relocation", "arl_")

# Rutas UOL esperadas por tipo semántico (genérico).
_EXPECTED_UOL_PATH: Dict[str, List[str]] = {
    "search_content": ["focus_input", "inject_text", "submit_input"],
    "fill_field": ["locate_field", "focus_field", "inject_text", "validate_field_value"],
    "fill_form": ["locate_field", "focus_field", "inject_text", "validate_field_value"],
    "submit_form": ["locate_submit_control", "activate_submit", "validate_outcome"],
    "switch_context": [
        "locate_target_context",
        "activate_context",
        "validate_context_switch",
    ],
    "confirm_dialog": [
        "locate_dialog",
        "activate_choice",
        "validate_dialog_dismissed",
    ],
    "submit_input": ["submit_input"],
    "open_site": ["navigate", "validate_surface"],
    "open_url": ["navigate", "validate_surface"],
    "open_new_tab": ["open_tab"],
    "open_tab": ["open_tab"],
    "open_app": ["launch_app", "attach_session"],
    "open_application": ["launch_app", "attach_session"],
    "select_profile": ["resolve_identity", "select_entity"],
    "select_entity_from_collection": ["resolve_collection", "select_entity"],
    "navigate": ["navigate", "validate_surface"],
}

_SURFACE_BY_SEMANTIC: Dict[str, List[str]] = {
    "search_content": ["input_surface", "results_surface"],
    "fill_form": ["form_input_surface", "input_surface"],
    "fill_field": ["form_input_surface", "input_surface"],
    "submit_form": ["form_input_surface", "input_surface"],
    "switch_context": ["application_surface", "operational_surface"],
    "confirm_dialog": ["dialog_surface", "operational_surface"],
    "open_site": ["browser_surface", "document_surface"],
    "open_app": ["application_surface"],
    "select_profile": ["profile_surface"],
}


class OrceConsistencyStatus(str, Enum):
    ALIGNED = "ALIGNED"
    DIVERGED = "DIVERGED"
    DEGRADED = "DEGRADED"
    NO_DATA = "NO_DATA"


class OrceRuntimeAcceptance(str, Enum):
    ACCEPTED = "ACCEPTED"
    NEEDS_HARDENING = "NEEDS_HARDENING"
    REJECTED = "REJECTED"
    NO_DATA = "NO_DATA"


def _strategy_uses_coords(strategy: str) -> bool:
    sl = (strategy or "").lower()
    return any(m in sl for m in _COORD_MARKERS)


def _strategy_uses_smart_route(strategy: str) -> bool:
    sl = (strategy or "").lower()
    return any(m in sl for m in _SMART_ROUTE_MARKERS)


def _strategy_is_forbidden(strategy: str, forbidden: Sequence[str]) -> bool:
    sl = (strategy or "").lower()
    for f in forbidden:
        if f.lower() in sl:
            return True
    return _strategy_uses_coords(strategy) and any(
        x in sl for x in ("coords_first", "coords_emergency", "use_coords")
    )


def _normalize_strategy_to_path_token(strategy: str) -> str:
    s = (strategy or "").strip().lower()
    if not s:
        return "unknown"
    if _strategy_uses_coords(s):
        return "coords_click"
    if _strategy_uses_smart_route(s):
        return "smart_route"
    if "retry" in s or "arl" in s:
        return "retry"
    if "repair" in s or "self_heal" in s:
        return "repair"
    if ":uol_" in s or "uol_" in s:
        part = s.split(":", 1)[-1] if ":" in s else s
        part = part.replace("uol_", "")
        return _UOL_STRATEGY_ALIASES.get(part, part.split(":")[0] or "uol_native")
    if ":" in s:
        tail = s.split(":", 1)[-1].split(":")[0]
        return _UOL_STRATEGY_ALIASES.get(tail, tail)
    return s


_UOL_STRATEGY_ALIASES: Dict[str, str] = {
    "inject": "inject_text",
    "inject_text": "inject_text",
    "focus": "focus_input",
    "focus_input": "focus_input",
    "submit": "submit_input",
    "submit_input": "submit_input",
    "generic": "execute_semantic_step",
    "hotkey": "open_tab",
    "hotkey_ctrl_t": "open_tab",
    "uol_hotkey": "open_tab",
    "uol_fields": "inject_text",
    "uol_field": "inject_text",
    "locate_field": "locate_field",
    "focus_field": "focus_field",
    "validate_field_value": "validate_field_value",
    "navigate": "navigate",
    "validate_surface": "validate_surface",
    "launch_app": "launch_app",
    "attach_session": "attach_session",
}


_SEARCH_CONTENT_CORE_SUBSTEPS: frozenset = frozenset({
    "focus_input",
    "inject_text",
    "submit_input",
})

_FILL_FORM_CORE_SUBSTEPS: frozenset = frozenset({
    "locate_field",
    "focus_field",
    "inject_text",
    "validate_field_value",
})

_SWITCH_CONTEXT_CORE_SUBSTEPS: frozenset = frozenset({
    "locate_target_context",
    "activate_context",
    "validate_context_switch",
})

_CONFIRM_DIALOG_CORE_SUBSTEPS: frozenset = frozenset({
    "locate_dialog",
    "activate_choice",
    "validate_dialog_dismissed",
})


def confirm_dialog_substeps_valid(
    substeps: Sequence[str],
    *,
    validation_passed: bool,
) -> bool:
    if not validation_passed:
        return False
    if not substeps:
        return False
    return _CONFIRM_DIALOG_CORE_SUBSTEPS.issubset(set(substeps))


def switch_context_substeps_valid(
    substeps: Sequence[str],
    *,
    validation_passed: bool,
) -> bool:
    if not validation_passed:
        return False
    if not substeps:
        return False
    return _SWITCH_CONTEXT_CORE_SUBSTEPS.issubset(set(substeps))


_SUBMIT_FORM_CORE_SUBSTEPS = frozenset({
    "locate_submit_control",
    "activate_submit",
    "validate_outcome",
})


def submit_form_substeps_valid(
    substeps: Sequence[str],
    *,
    validation_passed: bool,
) -> bool:
    if not validation_passed:
        return False
    if not substeps:
        return False
    return _SUBMIT_FORM_CORE_SUBSTEPS.issubset(set(substeps))


def fill_form_substeps_valid(
    substeps: Sequence[str],
    *,
    validation_passed: bool,
) -> bool:
    if not validation_passed:
        return False
    if not substeps:
        return False
    return _FILL_FORM_CORE_SUBSTEPS.issubset(set(substeps))


def search_content_substeps_valid(
    substeps: Sequence[str],
    *,
    validation_passed: bool,
) -> bool:
    """True si ``search_content:uol_surface`` reportó sub-steps reales y validación OK."""
    if not validation_passed:
        return False
    if not substeps:
        return False
    return _SEARCH_CONTENT_CORE_SUBSTEPS.issubset(set(substeps))


def _build_actual_runtime_path(d: Dict[str, Any], strategies: Sequence[str]) -> List[str]:
    substeps = d.get("runtime_resolution_substeps")
    if isinstance(substeps, list) and substeps:
        return [str(s) for s in substeps if str(s).strip()]
    path: List[str] = []
    for st in strategies:
        tok = _normalize_strategy_to_path_token(st)
        if tok and (not path or path[-1] != tok):
            path.append(tok)
    return path


def _search_content_path_aligned(
    expected: Sequence[str],
    actual: Sequence[str],
    *,
    validation_passed: bool,
) -> bool:
    if search_content_substeps_valid(actual, validation_passed=validation_passed):
        exp_core = set(expected)
        return exp_core.issubset(set(actual))
    return _runtime_paths_compatible(expected, actual)


def _runtime_paths_compatible(
    expected: Sequence[str],
    actual: Sequence[str],
) -> bool:
    if not expected:
        return True
    if not actual:
        return False
    exp_set = set(expected)
    neutral = {"execute_semantic_step", "unknown", "uol_native", "retry"}
    for tok in actual:
        mapped = _UOL_STRATEGY_ALIASES.get(tok, tok)
        if mapped in neutral:
            continue
        if mapped not in exp_set and tok not in exp_set:
            return False
    return True


def expected_uol_path_for_semantic_type(semantic_type: str) -> List[str]:
    t = str(semantic_type or "").lower().strip()
    if t in _EXPECTED_UOL_PATH:
        return list(_EXPECTED_UOL_PATH[t])
    if "search" in t:
        return list(_EXPECTED_UOL_PATH["search_content"])
    if "submit" in t:
        return list(_EXPECTED_UOL_PATH["submit_input"])
    if "open_site" in t or "open_url" in t or "navigate" in t:
        return list(_EXPECTED_UOL_PATH["open_site"])
    if "open_app" in t:
        return list(_EXPECTED_UOL_PATH["open_app"])
    if "new_tab" in t or t == "open_tab":
        return list(_EXPECTED_UOL_PATH["open_new_tab"])
    if "fill" in t:
        return list(_EXPECTED_UOL_PATH["fill_form"])
    return ["execute_semantic_step"]


def _canonical_truth_from_params(params: Dict[str, Any]) -> bool:
    if params.get("uol_canonical_truth") or params.get("canonical_truth"):
        return True
    coi = params.get("canonical_operational_intent")
    if isinstance(coi, dict) and coi.get("canonical_truth"):
        return True
    return False


def build_runtime_expectation_for_machine_step(
    ms: MachineExecutionStep,
    *,
    contract_intent_status: str = "",
) -> OperationalExecutionExpectation:
    """Deriva expectativa desde Machine Layer (sin hardcode de apps)."""
    pars = dict(ms.params or {})
    canonical = _canonical_truth_from_params(pars)
    confidence = float(ms.confidence or 0.0)
    preferred = str(ms.preferred_strategy or "").strip()
    fallbacks = [str(f).strip() for f in (ms.fallback_strategies or []) if str(f).strip()]
    preferred_list = [preferred] if preferred else []
    preferred_list.extend(f for f in fallbacks if f not in preferred_list)

    forbidden: List[str] = []
    if canonical and confidence >= 0.85:
        forbidden = list(_FORBIDDEN_CANONICAL)
    elif canonical:
        forbidden = [x for x in _FORBIDDEN_CANONICAL if "coords" in x or "smart_route" in x]

    fallback_budget = 0 if (canonical and confidence >= 0.85) else 1
    if contract_intent_status not in ("READY", "EXECUTABLE") and not canonical:
        fallback_budget = 2

    max_retries = 1 if canonical else 2
    sem = str(ms.semantic_type or "")
    return OperationalExecutionExpectation(
        expected_runtime_path=expected_uol_path_for_semantic_type(sem),
        preferred_strategies=preferred_list,
        forbidden_strategies=forbidden,
        fallback_budget=fallback_budget,
        max_retries=max_retries,
        expected_surface_types=list(_SURFACE_BY_SEMANTIC.get(sem, ["operational_surface"])),
        expected_transition_chain=expected_uol_path_for_semantic_type(sem)[:2],
        expected_validation_behavior="must_pass_on_success",
        expected_runtime_latency_ms=4000,
        canonical_truth_required=canonical,
        continuity_required=True,
    )


def runtime_allowed_degradations_for_step(
    ms: MachineExecutionStep,
    expectation: OperationalExecutionExpectation,
) -> List[str]:
    if expectation.canonical_truth_required and ms.confidence >= 0.85:
        return ["uia_to_dom_fallback"]
    if expectation.canonical_truth_required:
        return ["uia_to_dom_fallback", "single_retry"]
    return ["uia_to_dom_fallback", "single_retry", "strategy_fallback"]


def attach_runtime_expectations_to_model(model: FourLayerOperationalModel) -> None:
    status = str(model.contract.intent_status or "")
    for ms in model.machine_execution.steps:
        if ms.disabled:
            continue
        exp = build_runtime_expectation_for_machine_step(
            ms,
            contract_intent_status=status,
        )
        ms.runtime_expectation = exp
        ms.runtime_allowed_degradations = runtime_allowed_degradations_for_step(ms, exp)

    try:
        from app.services.runtime.operational_state_understanding_engine import (
            attach_osue_states_to_model,
        )

        attach_osue_states_to_model(model)
    except Exception as exc:
        log.debug("[ORCE] attach_osue_states: %s", exc)


def observation_from_uol_record(
    record: Any,
    *,
    step_id: str = "",
) -> OperationalRuntimeObservation:
    """Construye observación desde ``UolStepTelemetryRecord`` o dict."""
    if hasattr(record, "model_dump"):
        d = record.model_dump(mode="json")
    elif isinstance(record, dict):
        d = record
    else:
        d = {}

    sid = str(step_id or d.get("step_id") or "")
    strategies = list(d.get("strategies_attempted") or [])
    handler = str(d.get("handler_used") or d.get("primary_strategy") or "")
    if handler and handler not in strategies:
        strategies.append(handler)
    fallback_s = str(d.get("fallback_strategy") or "")
    if fallback_s and fallback_s not in strategies:
        strategies.append(fallback_s)

    path = _build_actual_runtime_path(d, strategies)
    retries = max(0, len(strategies) - 1) if strategies else 0

    repairs: List[str] = []
    fail = str(d.get("failure_reason") or "").lower()
    for m in _REPAIR_MARKERS:
        if m in fail:
            repairs.append(fail[:120] or m)
            break

    surface_chain: List[str] = []
    stt = str(d.get("surface_transition_type") or "").strip()
    if stt:
        surface_chain.append(stt)
    trans_chain: List[str] = []
    if d.get("continuity_bridge_used"):
        trans_chain.append(str(d.get("continuity_bridge_used")))
    if d.get("operational_thread_id"):
        trans_chain.append(f"thread:{d.get('operational_thread_id')}")

    expected_states = list(d.get("expected_state_transitions") or [])
    actual_states = list(d.get("actual_state_transitions") or d.get("runtime_operational_state_chain") or [])

    return OperationalRuntimeObservation(
        step_id=sid,
        actual_runtime_path=path,
        actual_strategies_used=strategies,
        retries_used=retries,
        repairs_triggered=repairs,
        coords_used=bool(d.get("coords_used")),
        smart_route_used=bool(d.get("smart_route_used")),
        fallback_used=bool(d.get("fallback_used") or d.get("legacy_used")),
        validation_failures=0 if d.get("validation_passed") else 1,
        runtime_surface_chain=surface_chain,
        runtime_transition_chain=trans_chain,
        runtime_operational_state_chain=actual_states,
        expected_state_transitions=expected_states,
        actual_state_transitions=actual_states,
        state_mismatch=bool(d.get("state_mismatch")),
        state_wait_timeout=bool(d.get("state_wait_timeout")),
        blocked_state_detected=bool(d.get("blocked_state_detected")),
        osue_wait_reason=str(d.get("osue_wait_reason") or d.get("reason") or ""),
        runtime_latency_ms=int(
            d.get("execution_latency_ms") or d.get("latency_ms") or d.get("elapsed_ms") or 0
        ),
        runtime_thread_id=str(d.get("operational_thread_id") or ""),
    )


def reconcile_step(
    expectation: OperationalExecutionExpectation,
    observation: OperationalRuntimeObservation,
    *,
    allowed_degradations: Optional[Sequence[str]] = None,
) -> OperationalRuntimeObservation:
    """Reconcilia expectativa vs observación; muta scores en ``observation``."""
    reasons: List[str] = []
    penalties = 0.0
    allowed = set(allowed_degradations or [])

    expected_path = list(expectation.expected_runtime_path or [])
    actual_path = list(observation.actual_runtime_path or [])

    if expected_path and actual_path:
        validation_ok = observation.validation_failures == 0
        if expected_path == list(_EXPECTED_UOL_PATH.get("search_content", [])):
            path_ok = _search_content_path_aligned(
                expected_path,
                actual_path,
                validation_passed=validation_ok,
            )
        elif expected_path == list(_EXPECTED_UOL_PATH.get("fill_form", [])):
            path_ok = fill_form_substeps_valid(actual_path, validation_passed=validation_ok)
        elif expected_path == list(_EXPECTED_UOL_PATH.get("submit_form", [])):
            path_ok = submit_form_substeps_valid(actual_path, validation_passed=validation_ok)
        elif expected_path == list(_EXPECTED_UOL_PATH.get("switch_context", [])):
            path_ok = switch_context_substeps_valid(actual_path, validation_passed=validation_ok)
        elif expected_path == list(_EXPECTED_UOL_PATH.get("confirm_dialog", [])):
            path_ok = confirm_dialog_substeps_valid(actual_path, validation_passed=validation_ok)
        else:
            path_ok = _runtime_paths_compatible(expected_path, actual_path)
        if not path_ok:
            if not (actual_path == ["retry"] and "single_retry" in allowed):
                reasons.append(
                    f"RUNTIME_PATH_DIVERGENCE: expected={expected_path} actual={actual_path}",
                )
                penalties += 0.25

    for strat in observation.actual_strategies_used:
        if _strategy_is_forbidden(strat, expectation.forbidden_strategies):
            reasons.append(f"FORBIDDEN_RUNTIME_DEGRADATION: {strat}")
            penalties += 0.35

    if observation.coords_used and expectation.canonical_truth_required:
        reasons.append("FORBIDDEN_RUNTIME_DEGRADATION: coords_used")
        penalties += 0.4

    if observation.smart_route_used and expectation.canonical_truth_required:
        if "uia_to_dom_fallback" not in allowed or observation.fallback_used:
            reasons.append("FORBIDDEN_RUNTIME_DEGRADATION: smart_route_on_canonical")
            penalties += 0.3

    fb_budget = int(expectation.fallback_budget or 0)
    if observation.fallback_used and fb_budget == 0:
        reasons.append("FALLBACK_BUDGET_EXCEEDED")
        penalties += 0.2
    elif observation.fallback_used and observation.retries_used > fb_budget + 1:
        reasons.append("FALLBACK_BUDGET_EXCEEDED")
        penalties += 0.15

    if observation.retries_used > int(expectation.max_retries or 1):
        reasons.append(f"RETRY_EXCESSIVE: {observation.retries_used}")
        penalties += 0.15

    if observation.repairs_triggered:
        hidden = [r for r in observation.repairs_triggered if "repair" in r]
        if hidden:
            reasons.append(f"HIDDEN_REPAIR: {hidden[0][:80]}")
            penalties += 0.2

    if expectation.continuity_required and observation.runtime_transition_chain:
        if not observation.runtime_thread_id and "thread:" not in "".join(
            observation.runtime_transition_chain,
        ):
            pass  # sin thread no implica break si no hay telemetría

    if observation.validation_failures > 0:
        reasons.append("VALIDATION_DIVERGENCE")
        penalties += 0.1

    lat = observation.runtime_latency_ms
    if lat > int(expectation.expected_runtime_latency_ms or 4000) * 2:
        reasons.append(f"LATENCY_DIVERGENCE: {lat}ms")
        penalties += 0.05

    exp_states = list(expectation.expected_state_transitions or [])
    act_states = list(
        observation.actual_state_transitions
        or observation.runtime_operational_state_chain
        or [],
    )
    if exp_states and act_states:
        try:
            from app.services.runtime.operational_state_understanding_engine import (
                orce_compare_state_transitions,
            )

            cmp = orce_compare_state_transitions(exp_states, act_states)
            if not cmp.aligned:
                observation.state_mismatch = True
                reasons.append(f"STATE_TRANSITION_MISMATCH: {cmp.mismatches[0][:80]}")
                penalties += 0.2
        except Exception:
            if act_states[-1] not in exp_states:
                observation.state_mismatch = True
                reasons.append(
                    f"STATE_MISMATCH: expected={exp_states[-1]} actual={act_states[-1]}",
                )
                penalties += 0.15

    if observation.state_wait_timeout:
        reasons.append(f"OSUE_STATE_TIMEOUT: {observation.osue_wait_reason[:80]}")
        penalties += 0.25

    if observation.blocked_state_detected:
        reasons.append(f"OSUE_BLOCKED_STATE: {observation.osue_wait_reason[:80]}")
        penalties += 0.2

    if observation.state_mismatch and not any("STATE" in r for r in reasons):
        reasons.append(f"STATE_MISMATCH: {observation.osue_wait_reason[:80] or 'reported'}")
        penalties += 0.15

    preferred = expectation.preferred_strategies or []
    if preferred and observation.actual_strategies_used:
        used_ok = any(
            p.split(":")[0] in (observation.actual_strategies_used[-1] or "")
            or (observation.actual_strategies_used[-1] or "") in p
            for p in preferred
        )
        if not used_ok and observation.fallback_used:
            reasons.append("STRATEGY_FIDELITY_LOW")
            penalties += 0.1

    score = max(0.0, min(1.0, 1.0 - penalties))
    degradation = "none"
    if penalties >= 0.5:
        degradation = "severe"
    elif penalties >= 0.25:
        degradation = "moderate"
    elif penalties > 0:
        degradation = "mild"

    observation.divergence_detected = bool(reasons)
    observation.divergence_reasons = reasons
    observation.consistency_score = round(score, 4)
    observation.degradation_level = degradation
    return observation


def _load_four_layer(mission: Mission) -> Optional[FourLayerOperationalModel]:
    raw = mission.four_layer_operational_model
    if not isinstance(raw, dict):
        return None
    try:
        return FourLayerOperationalModel.model_validate(raw)
    except Exception:
        return None


def reconcile_mission(mission: Mission) -> OperationalRuntimeConsistencyReport:
    """Reconciliación completa misión desde ``uol_runtime_audit`` + 4LOM."""
    audit = list(getattr(mission, "uol_runtime_audit", None) or [])
    osue_by_step: Dict[str, Dict[str, Any]] = {}
    for raw_osue in getattr(mission, "osue_runtime_wait_audit", None) or []:
        if isinstance(raw_osue, dict) and raw_osue.get("step_id"):
            osue_by_step[str(raw_osue["step_id"])] = raw_osue

    if not audit and osue_by_step:
        audit = list(osue_by_step.values())

    model = _load_four_layer(mission)
    if model:
        attach_runtime_expectations_to_model(model)
        mission.four_layer_operational_model = model.model_dump(mode="json")

    if not audit:
        return OperationalRuntimeConsistencyReport(
            consistency_status=OrceConsistencyStatus.NO_DATA.value,
            runtime_acceptance=OrceRuntimeAcceptance.NO_DATA.value,
            runtime_repair_suggestions=["ejecutar misión con Smart Executor para telemetría"],
        )

    machine_by_id: Dict[str, MachineExecutionStep] = {}
    if model:
        for ms in model.machine_execution.steps:
            machine_by_id[ms.step_id] = ms

    step_summaries: List[Dict[str, Any]] = []
    observations: List[OperationalRuntimeObservation] = []
    divergences = 0
    forbidden_ct = 0
    hidden_fb = 0
    hidden_repairs = 0
    canon_viol = 0
    continuity_breaks = 0
    degradations: List[str] = []
    scores: List[float] = []

    for row in audit:
        merged = dict(row or {})
        sid = str(merged.get("step_id") or "")
        if sid and sid in osue_by_step:
            merged.update(osue_by_step[sid])
        obs = observation_from_uol_record(merged)
        ms = machine_by_id.get(obs.step_id)
        if ms and ms.disabled:
            continue
        exp = (
            ms.runtime_expectation
            if ms and ms.runtime_expectation
            else build_runtime_expectation_for_machine_step(
                ms
                if ms
                else MachineExecutionStep(
                    step_id=obs.step_id,
                    semantic_type=str((row or {}).get("uol_action") or "unknown"),
                ),
            )
        )
        allowed = (
            list(ms.runtime_allowed_degradations)
            if ms
            else runtime_allowed_degradations_for_step(
                ms or MachineExecutionStep(step_id=obs.step_id, semantic_type=""),
                exp,
            )
        )
        obs = reconcile_step(exp, obs, allowed_degradations=allowed)
        observations.append(obs)

        if obs.divergence_detected:
            divergences += 1
        for r in obs.divergence_reasons:
            if "FORBIDDEN" in r:
                forbidden_ct += 1
            if "FALLBACK" in r:
                hidden_fb += 1
            if "HIDDEN_REPAIR" in r:
                hidden_repairs += 1
            if "coords" in r.lower() or "canonical" in r.lower():
                canon_viol += 1
        if obs.coords_used and exp.canonical_truth_required:
            canon_viol += 1
        if not obs.runtime_thread_id and exp.continuity_required and obs.fallback_used:
            continuity_breaks += 1
        if obs.degradation_level != "none":
            degradations.append(f"{obs.step_id}:{obs.degradation_level}")
        scores.append(obs.consistency_score)

        step_summaries.append(
            {
                "step_id": obs.step_id,
                "expected_path": exp.expected_runtime_path,
                "actual_path": obs.actual_runtime_path,
                "status": (
                    OrceConsistencyStatus.DIVERGED.value
                    if obs.divergence_detected
                    else OrceConsistencyStatus.ALIGNED.value
                ),
                "consistency_score": obs.consistency_score,
                "divergence_reasons": obs.divergence_reasons[:6],
            },
        )

    avg_score = sum(scores) / len(scores) if scores else 0.0
    coords_total = sum(1 for o in observations if o.coords_used)

    if divergences == 0 and coords_total == 0 and forbidden_ct == 0:
        status = OrceConsistencyStatus.ALIGNED.value
    elif forbidden_ct > 0 or canon_viol > 0 or coords_total > 0:
        status = OrceConsistencyStatus.DIVERGED.value
    else:
        status = OrceConsistencyStatus.DEGRADED.value

    acceptance = _compute_runtime_acceptance(
        observations=observations,
        avg_score=avg_score,
        forbidden_ct=forbidden_ct,
        hidden_fb=hidden_fb,
        hidden_repairs=hidden_repairs,
        canon_viol=canon_viol,
        continuity_breaks=continuity_breaks,
    )

    weakest = sorted(
        step_summaries,
        key=lambda s: (s.get("consistency_score", 1.0),),
    )[:5]

    suggestions = _repair_suggestions(observations, acceptance)

    try:
        mission.orce_runtime_audit = [
            o.model_dump(mode="json") for o in observations
        ][-500:]
    except Exception:
        pass

    return OperationalRuntimeConsistencyReport(
        consistency_status=status,
        consistency_score=round(avg_score, 4),
        divergence_count=divergences,
        forbidden_strategy_usage=forbidden_ct,
        hidden_fallbacks=hidden_fb,
        hidden_repairs=hidden_repairs,
        runtime_degradations=degradations[:20],
        canonical_truth_violations=canon_viol,
        continuity_breaks=continuity_breaks,
        weakest_runtime_steps=weakest,
        runtime_acceptance=acceptance.value,
        runtime_repair_suggestions=suggestions,
        step_summaries=step_summaries,
    )


def _compute_runtime_acceptance(
    *,
    observations: List[OperationalRuntimeObservation],
    avg_score: float,
    forbidden_ct: int,
    hidden_fb: int,
    hidden_repairs: int,
    canon_viol: int,
    continuity_breaks: int,
) -> OrceRuntimeAcceptance:
    if not observations:
        return OrceRuntimeAcceptance.NO_DATA
    coords = sum(1 for o in observations if o.coords_used)
    if forbidden_ct > 0 or canon_viol > 0 or coords > 0:
        return OrceRuntimeAcceptance.REJECTED
    if hidden_repairs > 0 or hidden_fb > 2 or continuity_breaks > 0:
        return OrceRuntimeAcceptance.NEEDS_HARDENING
    serious = any(
        o.divergence_detected
        and any(
            k in " ".join(o.divergence_reasons)
            for k in (
                "FORBIDDEN",
                "FALLBACK_BUDGET",
                "HIDDEN_REPAIR",
                "coords",
            )
        )
        for o in observations
    )
    if avg_score < 0.75 or serious:
        return OrceRuntimeAcceptance.NEEDS_HARDENING
    if any(o.divergence_detected for o in observations) and avg_score < 0.9:
        return OrceRuntimeAcceptance.NEEDS_HARDENING
    return OrceRuntimeAcceptance.ACCEPTED


def _repair_suggestions(
    observations: List[OperationalRuntimeObservation],
    acceptance: OrceRuntimeAcceptance,
) -> List[str]:
    out: List[str] = []
    if acceptance == OrceRuntimeAcceptance.ACCEPTED:
        out.append("runtime consistente con Machine Layer")
        return out
    for o in observations:
        if o.coords_used:
            out.append(f"{o.step_id}: eliminar coords; reforzar estrategia UOL-native")
        if o.smart_route_used:
            out.append(f"{o.step_id}: reducir smart_route; preferir handler canónico")
        for r in o.divergence_reasons[:2]:
            out.append(f"{o.step_id}: {r[:100]}")
    if not out:
        out.append("revisar estrategias preferidas en Machine Layer")
    return out[:12]


def append_orce_observation(mission: Mission, observation: OperationalRuntimeObservation) -> None:
    """Persiste observación reconciliada en ``mission.orce_runtime_audit``."""
    try:
        audit = list(getattr(mission, "orce_runtime_audit", None) or [])
        audit.append(observation.model_dump(mode="json"))
        mission.orce_runtime_audit = audit[-500:]
    except Exception as exc:
        log.debug("[ORCE] mission audit: %s", exc)
    try:
        _ORCE_JSONL.parent.mkdir(parents=True, exist_ok=True)
        row = observation.model_dump(mode="json")
        row["mission_id"] = str(getattr(mission, "id", "") or "")
        row["recorded_at"] = time.time()
        with _ORCE_JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.debug("[ORCE] jsonl: %s", exc)


def persist_orce_report_on_mission(mission: Mission) -> OperationalRuntimeConsistencyReport:
    report = reconcile_mission(mission)
    if mission is not None:
        try:
            mission.operational_runtime_consistency_report = report.model_dump(mode="json")
        except Exception:
            pass
    return report


def feed_runtime_learning(mission: Mission, report: OperationalRuntimeConsistencyReport) -> None:
    """Alimenta SLL y OMPC con divergencias (no solo logging)."""
    try:
        from app.core.config import get_settings

        settings = get_settings()
    except Exception:
        settings = None

    if not report.step_summaries:
        return

    sll_on = settings and getattr(settings, "SLL_ENABLED", False)
    ompc_on = settings and getattr(settings, "OMPC_ENABLED", False)

    if sll_on:
        try:
            from app.services.runtime.strategy_learning_layer import (
                StrategyContext,
                record_observation,
            )

            for summary in report.step_summaries:
                if summary.get("status") != OrceConsistencyStatus.DIVERGED.value:
                    continue
                sid = str(summary.get("step_id") or "")
                ctx = StrategyContext(
                    capability_id=sid,
                    step_kind=sid,
                    operational_intent="orce_divergence",
                )
                for strat in summary.get("actual_path") or []:
                    record_observation(
                        context=ctx,
                        strategy_name=str(strat),
                        success=False,
                        duration_ms=0.0,
                        mission=mission,
                        validator_passed=False,
                        fallback_to_sep=True,
                        notes="orce_runtime_divergence",
                        require_execution_truth_for_success=False,
                    )
        except Exception as exc:
            log.debug("[ORCE] SLL feed: %s", exc)

    if ompc_on and report.divergence_count > 0:
        try:
            from app.services.runtime.operational_memory import consolidate_operational_pattern

            seq = [
                f"sep:{s.get('step_id')}"
                for s in (report.step_summaries or [])
                if s.get("step_id")
            ]
            if seq:
                consolidate_operational_pattern(
                    canonical_sequence=seq,
                    workflow_type="orce/runtime_consistency",
                    mission=mission,
                    ended_success=report.runtime_acceptance
                    == OrceRuntimeAcceptance.ACCEPTED.value,
                    duration_ms=0.0,
                    fallbacks=report.hidden_fallbacks,
                    arl_recoveries=report.hidden_repairs,
                    validator_failures=report.canonical_truth_violations,
                    notes="orce_reconciliation",
                    mission_terminal_failed=report.runtime_acceptance
                    == OrceRuntimeAcceptance.REJECTED.value,
                )
        except Exception as exc:
            log.debug("[ORCE] OMPC feed: %s", exc)


def observe_mission_after_run(mission: Mission) -> OperationalRuntimeConsistencyReport:
    """Hook post-ejecución: reconciliar, persistir, aprender."""
    report = persist_orce_report_on_mission(mission)
    feed_runtime_learning(mission, report)
    return report


def expert_panel_lines(mission: Mission) -> List[str]:
    """Panel Mission Review experto: Expected vs Actual."""
    raw = getattr(mission, "operational_runtime_consistency_report", None)
    if isinstance(raw, dict) and raw.get("step_summaries"):
        try:
            report = OperationalRuntimeConsistencyReport.model_validate(raw)
        except Exception:
            report = reconcile_mission(mission)
    else:
        report = reconcile_mission(mission)

    if report.consistency_status == OrceConsistencyStatus.NO_DATA.value:
        lines = [
            "ORCE: sin telemetría runtime (ejecuta la misión con Smart Executor).",
        ]
    else:
        lines = [
            "ORCE — Runtime Consistency",
            f"  estado: {report.consistency_status} · acceptance: {report.runtime_acceptance}",
            f"  score: {report.consistency_score:.2f} · divergencias: {report.divergence_count}",
            f"  forbidden: {report.forbidden_strategy_usage} · "
            f"fallbacks ocultos: {report.hidden_fallbacks} · repairs: {report.hidden_repairs}",
        ]
        for s in (report.step_summaries or [])[:4]:
            exp = " → ".join(s.get("expected_path") or [])[:60]
            act = " → ".join(s.get("actual_path") or [])[:60]
            st = s.get("status", "")
            lines.append(f"  [{s.get('step_id')}] {st}")
            lines.append(f"    EXPECTED: {exp or '—'}")
            lines.append(f"    ACTUAL:   {act or '—'}")
            reasons = s.get("divergence_reasons") or []
            if reasons:
                lines.append(f"    · {reasons[0][:90]}")
        if report.runtime_repair_suggestions:
            lines.append(f"  sugerencia: {report.runtime_repair_suggestions[0][:100]}")

    try:
        from app.services.runtime.universal_operational_element_identity import (
            uoei_expert_panel_lines,
        )

        uoei_lines = uoei_expert_panel_lines(mission)
        if uoei_lines:
            lines.append("")
            lines.extend(uoei_lines[:6])
    except Exception:
        pass

    try:
        from app.services.runtime.adaptive_runtime_strategy_synthesis import (
            arss_expert_panel_lines,
        )

        arss_lines = arss_expert_panel_lines(mission)
        if arss_lines:
            lines.append("")
            lines.extend(arss_lines[:10])
    except Exception:
        pass

    try:
        from app.services.runtime.operational_state_understanding_engine import (
            expert_panel_lines as osue_expert_panel_lines,
        )

        osue_lines = osue_expert_panel_lines(mission, expert_mode=True)
        if osue_lines:
            lines.append("")
            lines.extend(osue_lines[:12])
    except Exception:
        pass

    try:
        from app.services.runtime.beta_runtime_acceptance import (
            expert_panel_lines as beta_expert_panel_lines,
        )

        beta_lines = beta_expert_panel_lines(mission, expert_mode=True)
        if beta_lines:
            lines.append("")
            lines.extend(beta_lines[:10])
    except Exception:
        pass

    return lines


def four_layers_preserved_after_runtime_degradation(mission: Mission) -> Dict[str, bool]:
    """Goal/Human intactos; Machine preservado (para tests)."""
    model = _load_four_layer(mission)
    goal_ok = bool(model and model.goal.purpose_statement)
    human_ok = bool(model and model.human_view.steps)
    machine_ok = bool(model and model.machine_execution.steps)
    return {
        "goal_intact": goal_ok,
        "human_view_intact": human_ok,
        "machine_layer_preserved": machine_ok,
    }


__all__ = [
    "OrceConsistencyStatus",
    "OrceRuntimeAcceptance",
    "append_orce_observation",
    "attach_runtime_expectations_to_model",
    "build_runtime_expectation_for_machine_step",
    "expert_panel_lines",
    "expected_uol_path_for_semantic_type",
    "feed_runtime_learning",
    "four_layers_preserved_after_runtime_degradation",
    "observe_mission_after_run",
    "observation_from_uol_record",
    "persist_orce_report_on_mission",
    "reconcile_mission",
    "reconcile_step",
    "runtime_allowed_degradations_for_step",
    "search_content_substeps_valid",
    "fill_form_substeps_valid",
    "submit_form_substeps_valid",
    "switch_context_substeps_valid",
    "confirm_dialog_substeps_valid",
]
