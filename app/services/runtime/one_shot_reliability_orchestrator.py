"""One-Shot Reliability Closure — orquestación única sobre runtime existente.

Sin nueva arquitectura paralela: compone TEL/SEP, COB, ORG/GOOR, LOP, truth,
ambigüedad, ARL/SLL/OMPC ya presentes. Todo queda detrás de flags en Settings.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import Mission
from app.core.logger import log


class OneShotPreflightOutcome(str, Enum):
    READY_TO_EXECUTE = "READY_TO_EXECUTE"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    NEEDS_HUMAN_CLARIFICATION = "NEEDS_HUMAN_CLARIFICATION"
    UNSAFE_STOP = "UNSAFE_STOP"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


class OneShotPlanSource(str, Enum):
    """Fuente primaria lógica para auditoría (coords nunca primarios)."""

    TEL_SEP_PROMOTED = "TEL_SEP_PROMOTED"
    COB_ORG_GOOR_CANDIDATE = "COB_ORG_GOOR_CANDIDATE"
    SEP_SIPE_CANONICAL = "SEP_SIPE_CANONICAL"
    LEGACY_GRAPH_DEBUG = "LEGACY_GRAPH_DEBUG"


class OneShotRuntimeMode(str, Enum):
    TEL_SEP_RUNTIME = "TEL_SEP_RUNTIME"
    COB_PILOT_PREFIX = "COB_PILOT_PREFIX"
    GOOR_SHADOW_GUIDED = "GOOR_SHADOW_GUIDED"
    SEP_SMART_RUNTIME = "SEP_SMART_RUNTIME"
    LEGACY_DEBUG_FALLBACK = "LEGACY_DEBUG_FALLBACK"


class OneShotRecoveryPolicy(str, Enum):
    """Orden conceptual documentado en PRD (ARL → GOOR → SEP → micro repair → re-record bloque)."""

    ARL_FIRST = "ARL_FIRST"
    GOOR_ALT_TRANSITION = "GOOR_ALT_TRANSITION"
    SEP_FALLBACK = "SEP_FALLBACK"
    MICRO_REPAIR = "MICRO_REPAIR"
    RERECORD_OPERATIONAL_BLOCK = "RERECORD_OPERATIONAL_BLOCK"


_RECOVERY_ORDER: Tuple[OneShotRecoveryPolicy, ...] = (
    OneShotRecoveryPolicy.ARL_FIRST,
    OneShotRecoveryPolicy.GOOR_ALT_TRANSITION,
    OneShotRecoveryPolicy.SEP_FALLBACK,
    OneShotRecoveryPolicy.MICRO_REPAIR,
    OneShotRecoveryPolicy.RERECORD_OPERATIONAL_BLOCK,
)

_UNSAFE_MODAL_MARKERS = (
    "payment",
    "pago",
    "credit card",
    "tarjeta",
    "checkout",
    "password",
    "contraseña",
    "sign in",
    "iniciar sesión",
    "authenticate",
    "2fa",
    "otp",
)

_UNSAFE_CONTEXT_MARKERS = (
    "delete permanently",
    "eliminar permanentemente",
    "factory reset",
    "borrar todo",
    "irreversible",
)


@dataclass
class OneShotReadinessResult:
    outcome: OneShotPreflightOutcome
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    blocker_codes: List[str] = field(default_factory=list)
    readiness_score: float = 0.0


def choose_recovery_policy(*, failure_kind: str = "") -> Tuple[OneShotRecoveryPolicy, ...]:
    """Política de reparación por etapas (sin ejecutar — contrato para UI/docs)."""
    _ = failure_kind
    return _RECOVERY_ORDER


def prepare_mission_for_reliable_execution(mission: Mission, settings: Any) -> Dict[str, Any]:
    """Refresca artefactos SAFE ya existentes (gate SEP, execution_truth audit)."""
    audit: Dict[str, Any] = {"gate_refreshed": False, "execution_truth": None}
    try:
        from app.services.missions.mission_truth_gate import gate_decision

        gate_decision(mission)
        audit["gate_refreshed"] = True
    except Exception as exc:
        audit["gate_error"] = str(exc)
        log.debug("[ONE_SHOT] gate_decision en prepare: %s", exc)
    try:
        from app.services.missions.execution_truth_engine import attach_execution_truth_audit

        audit["execution_truth"] = attach_execution_truth_audit(mission)
    except Exception as exc:
        audit["execution_truth_error"] = str(exc)
        log.debug("[ONE_SHOT] execution_truth en prepare: %s", exc)
    return audit


def attach_live_perception(mission: Mission, settings: Any) -> Dict[str, Any]:
    """Captura LOP sombra cuando ONE_SHOT_USE_LOP y la misión lo permite."""
    if not getattr(settings, "ONE_SHOT_USE_LOP", True):
        return {"skipped": True, "reason": "ONE_SHOT_USE_LOP=false"}
    try:
        from app.services.runtime.live_operational_perception import refresh_lop_shadow

        snap = refresh_lop_shadow(mission, settings)
        return {
            "ok": True,
            "confidence": float(getattr(snap, "confidence", 0.0) or 0.0),
            "safe_to_continue": bool(getattr(snap, "safe_to_continue", True)),
            "blockers": list(getattr(snap, "live_blockers", None) or []),
        }
    except Exception as exc:
        log.debug("[ONE_SHOT] LOP refresh: %s", exc)
        return {"ok": False, "reason": str(exc)}


def run_preflight_truth_check(mission: Mission) -> Dict[str, Any]:
    from app.services.missions.execution_truth_engine import compute_execution_truth

    et = compute_execution_truth(mission)
    return {
        "confirmed": bool(et.confirmed),
        "reasons": list(et.reasons),
        "where": str(et.where or ""),
    }


def run_preflight_lop_check(mission: Mission, settings: Any) -> Dict[str, Any]:
    raw = getattr(mission, "lop_last_snapshot", None) or {}
    use_lop = getattr(settings, "ONE_SHOT_USE_LOP", True)
    if not isinstance(raw, dict) or not raw:
        if not use_lop:
            return {"skipped": True}
        return {"present": False}
    blockers = list(raw.get("live_blockers") or [])
    safe = bool(raw.get("safe_to_continue", True))
    conf = float(raw.get("confidence") or 0.0)
    out = {
        "present": True,
        "safe_to_continue": safe,
        "confidence": conf,
        "blocker_count": len(blockers),
        "blockers_head": blockers[:8],
    }
    req = bool(getattr(settings, "ONE_SHOT_REQUIRE_SAFE_TO_CONTINUE", True))
    if req and not safe:
        out["hard_stop"] = True
        out["reason"] = "lop_unsafe_to_continue"
    return out


def _sep_blob(mission: Mission) -> Dict[str, Any]:
    sep = getattr(mission, "semantic_execution_plan", None)
    return sep if isinstance(sep, dict) else {}


def _plan_steps(mission: Mission) -> List[Dict[str, Any]]:
    steps = _sep_blob(mission).get("steps") or []
    return [s for s in steps if isinstance(s, dict)]


def _tel_candidate_plan(mission: Mission) -> Optional[Any]:
    raw = getattr(mission, "tel_semantic_execution_plan_candidate", None)
    if not isinstance(raw, dict) or not raw.get("steps"):
        return None
    try:
        from app.services.missions.semantic_execution_plan import SemanticExecutionPlan

        return SemanticExecutionPlan.from_dict(raw)
    except Exception:
        return None


def choose_plan_source(mission: Mission, settings: Any) -> OneShotPlanSource:
    """Prioridad PRD — coords nunca fuente primaria (auditoría)."""
    sep = _sep_blob(mission)
    src = str(sep.get("source") or "").strip()
    steps = _plan_steps(mission)
    coords_primary = bool(plan_declares_coords_primary(sep, steps))

    tel_plan = _tel_candidate_plan(mission)
    allow_tel = bool(getattr(settings, "ONE_SHOT_ALLOW_TEL_SEP_PRIMARY", False))

    if (
        src == "tel_sep_promoter_primary"
        and allow_tel
        and not coords_primary
        and _tel_sep_eligible(mission, tel_plan, settings)
    ):
        return OneShotPlanSource.TEL_SEP_PROMOTED

    cob_ok = _cob_dry_run_ok(mission)
    lop_ok = _lop_ok_for_cob_path(mission, settings)
    recovery_ok = _recovery_edges_present(mission)
    if cob_ok and lop_ok and recovery_ok and not coords_primary:
        return OneShotPlanSource.COB_ORG_GOOR_CANDIDATE

    if steps and src in (
        "semantic_intent_promotion_engine",
        "tel_sep_promoter_primary",
        "tel_sep_promoter_candidate",
    ):
        return OneShotPlanSource.SEP_SIPE_CANONICAL

    cgraph = getattr(mission, "compiled_execution_graph", None) or []
    if cgraph and not steps:
        return OneShotPlanSource.LEGACY_GRAPH_DEBUG

    return OneShotPlanSource.SEP_SIPE_CANONICAL


def plan_declares_coords_primary(sep: Dict[str, Any], steps: Sequence[Dict[str, Any]]) -> bool:
    """True si el plan declara coords como ruta primaria — prohibido como fuente primaria."""
    if bool(sep.get("coords_used")):
        return True
    for s in steps:
        ps = str(s.get("preferred_strategy") or "").lower()
        if any(x in ps for x in ("use_coords", "vision_coords", "relative_coords")):
            return True
        params = s.get("params") if isinstance(s.get("params"), dict) else {}
        if isinstance(params, dict):
            for k in ("bbox", "fallback_coords", "coords_xy", "absolute_xy"):
                if params.get(k):
                    return True
    return False


def _tel_sep_eligible(mission: Mission, tel_plan: Any, settings: Any) -> bool:
    plan_obj = tel_plan
    if plan_obj is None:
        try:
            from app.services.missions.semantic_execution_plan import SemanticExecutionPlan

            sep = _sep_blob(mission)
            if sep.get("steps"):
                plan_obj = SemanticExecutionPlan.from_dict(sep)
        except Exception:
            plan_obj = None
    if plan_obj is None:
        return False
    try:
        from app.services.missions.semantic_ambiguity_engine import (
            aggregate_plan_ambiguity,
            evaluate_plan_semantic_ambiguity,
        )
        from app.services.missions.tel_sep_promoter import validate_tel_sep_ready

        amb = aggregate_plan_ambiguity(evaluate_plan_semantic_ambiguity(plan_obj, mission))
        if amb.requires_human_confirmation:
            return False
        vr = validate_tel_sep_ready(mission, plan_obj, settings=settings)
        return bool(vr.ok)
    except Exception:
        return False


def _cob_dry_run_ok(mission: Mission) -> bool:
    dry = getattr(mission, "cob_dry_run_shadow", None)
    if not isinstance(dry, dict):
        return False
    rows = dry.get("rows") or dry.get("details") or []
    if isinstance(rows, list) and rows:
        unsupported = sum(
            1
            for r in rows
            if isinstance(r, dict) and str(r.get("status") or "").lower() in ("unsupported", "error")
        )
        return unsupported == 0
    return bool(dry.get("ok", False))


def _lop_ok_for_cob_path(mission: Mission, settings: Any) -> bool:
    if not getattr(settings, "ONE_SHOT_USE_LOP", True):
        return True
    raw = getattr(mission, "lop_last_snapshot", None)
    if not isinstance(raw, dict) or not raw:
        return True
    if raw.get("perception_gaps"):
        return False
    return bool(raw.get("safe_to_continue", True))


def _recovery_edges_present(mission: Mission) -> bool:
    org = getattr(mission, "operational_runtime_graph", None)
    if isinstance(org, dict):
        edges = org.get("edges") or org.get("transitions") or []
        if isinstance(edges, list) and len(edges) > 0:
            return True
    goor = getattr(mission, "goal_oriented_runtime_audit", None)
    if isinstance(goor, dict) and goor.get("ranked_transitions"):
        return True
    return False


def _unsafe_scan_plan(mission: Mission) -> List[str]:
    hits: List[str] = []
    for s in _plan_steps(mission):
        blob = f"{s.get('human_label','')} {s.get('preferred_strategy','')}".lower()
        for m in _UNSAFE_MODAL_MARKERS:
            if m in blob:
                hits.append(f"unsafe_modal_hint:{m}")
        for m in _UNSAFE_CONTEXT_MARKERS:
            if m in blob:
                hits.append(f"unsafe_context_hint:{m}")
    p = str(getattr(mission, "name", "") or "").lower()
    if "paywall" in p or "billing" in p:
        hits.append("unsafe_mission_name_hint")
    return hits[:12]


def _multi_target_scan(mission: Mission) -> bool:
    try:
        from app.services.missions.semantic_ambiguity_engine import RB_MULTI_TARGET

        sep = _sep_blob(mission)
        for r in sep.get("not_ready_reasons") or []:
            if RB_MULTI_TARGET in str(r):
                return True
        for s in _plan_steps(mission):
            hl = str(s.get("human_label") or "")
            if RB_MULTI_TARGET in hl:
                return True
    except Exception:
        pass
    return False


def _validators_scan(mission: Mission) -> Dict[str, Any]:
    """Presencia de validadores UIC / pasos con validación explícita."""
    steps = _plan_steps(mission)
    n_val_hints = 0
    for s in steps:
        params = s.get("params") if isinstance(s.get("params"), dict) else {}
        if not isinstance(params, dict):
            continue
        if params.get("validator") or params.get("postcondition") or params.get("validation_hint"):
            n_val_hints += 1
    return {"steps_with_validator_hints": n_val_hints, "step_count": len(steps)}


def compute_one_shot_readiness(
    mission: Mission,
    settings: Any,
    *,
    bridge_source: str = "",
    steps_count: int = 0,
) -> OneShotReadinessResult:
    """Preflight real consolidado — READY / WARN / HUMAN / UNSAFE / REPAIR."""
    reasons: List[str] = []
    warnings: List[str] = []
    blockers: List[str] = []

    gd = None
    try:
        from app.services.missions.mission_truth_gate import TruthGateCode, gate_decision

        gd = gate_decision(mission)
        if not gd.is_executable:
            blockers.extend(gd.blockers)
            if TruthGateCode.PLAN_INVALID_STRICT in gd.blockers or TruthGateCode.LAYER_MISMATCH in gd.blockers:
                return OneShotReadinessResult(
                    outcome=OneShotPreflightOutcome.REPAIR_REQUIRED,
                    reasons=["truth_gate_not_executable"],
                    blocker_codes=list(dict.fromkeys(blockers)),
                    readiness_score=0.0,
                )
    except Exception as exc:
        warnings.append(f"gate_exception:{exc}")

    try:
        from app.services.missions.semantic_execution_plan import validate_semantic_execution_plan_strict

        strict = validate_semantic_execution_plan_strict(mission)
        if not strict.is_valid:
            return OneShotReadinessResult(
                outcome=OneShotPreflightOutcome.REPAIR_REQUIRED,
                reasons=["semantic_plan_strict_invalid"],
                warnings=warnings,
                blocker_codes=[v.code for v in strict.violations][:20],
                readiness_score=0.05,
            )
    except Exception as exc:
        warnings.append(f"strict_validation_exception:{exc}")

    amb_block = False
    try:
        from app.services.missions.semantic_ambiguity_engine import (
            aggregate_plan_ambiguity,
            evaluate_plan_semantic_ambiguity,
        )
        from app.services.missions.semantic_execution_plan import SemanticExecutionPlan

        sep = _sep_blob(mission)
        if sep.get("steps"):
            plan = SemanticExecutionPlan.from_dict(sep)
            agg = aggregate_plan_ambiguity(evaluate_plan_semantic_ambiguity(plan, mission))
            amb_block = bool(agg.requires_human_confirmation)
            if amb_block:
                reasons.append("semantic_ambiguity_requires_human")
    except Exception as exc:
        warnings.append(f"ambiguity_exception:{exc}")

    unsafe_hits = _unsafe_scan_plan(mission)
    if unsafe_hits:
        return OneShotReadinessResult(
            outcome=OneShotPreflightOutcome.UNSAFE_STOP,
            reasons=["unsafe_context_detected"],
            warnings=warnings + unsafe_hits[:6],
            blocker_codes=["UNSAFE_CONTEXT"],
            readiness_score=0.0,
        )

    if _multi_target_scan(mission):
        return OneShotReadinessResult(
            outcome=OneShotPreflightOutcome.NEEDS_HUMAN_CLARIFICATION,
            reasons=["multiple_equivalent_targets"],
            warnings=warnings,
            blocker_codes=["SEMANTIC_INTENT_MULTI_TARGET"],
            readiness_score=0.25,
        )

    truth = run_preflight_truth_check(mission)
    if not truth["confirmed"]:
        warnings.append("execution_truth_not_confirmed")

    lop_r = run_preflight_lop_check(mission, settings)
    if lop_r.get("hard_stop"):
        return OneShotReadinessResult(
            outcome=OneShotPreflightOutcome.UNSAFE_STOP,
            reasons=["lop_blocked"],
            warnings=warnings + [str(lop_r.get("reason"))],
            blocker_codes=["LOP_BLOCKERS"],
            readiness_score=0.15,
        )

    valcov = _validators_scan(mission)
    if valcov["step_count"] and valcov["steps_with_validator_hints"] == 0:
        warnings.append("validator_coverage_low")

    if gd is not None and not gd.is_executable:
        return OneShotReadinessResult(
            outcome=OneShotPreflightOutcome.NEEDS_HUMAN_CLARIFICATION,
            reasons=reasons or ["mission_not_executable"],
            warnings=warnings,
            blocker_codes=list(dict.fromkeys(blockers))[:24],
            readiness_score=0.2,
        )

    if amb_block:
        return OneShotReadinessResult(
            outcome=OneShotPreflightOutcome.NEEDS_HUMAN_CLARIFICATION,
            reasons=reasons,
            warnings=warnings,
            blocker_codes=["SEMANTIC_AMBIGUITY"],
            readiness_score=0.35,
        )

    score = 0.72
    if truth["confirmed"]:
        score += 0.15
    if lop_r.get("present") and lop_r.get("safe_to_continue", True):
        score += 0.08
    if bridge_source == "semantic_execution_plan":
        score += 0.03
    score = min(1.0, score)
    if warnings:
        return OneShotReadinessResult(
            outcome=OneShotPreflightOutcome.READY_WITH_WARNINGS,
            reasons=["ready_with_warnings"],
            warnings=warnings,
            readiness_score=score,
        )
    return OneShotReadinessResult(
        outcome=OneShotPreflightOutcome.READY_TO_EXECUTE,
        reasons=["ready"],
        warnings=[],
        readiness_score=score,
    )


def select_runtime_mode(
    mission: Mission,
    settings: Any,
    *,
    plan_source: OneShotPlanSource,
    cob_pilot_will_run: bool,
    bridge_used_legacy: bool,
) -> OneShotRuntimeMode:
    if bridge_used_legacy:
        return OneShotRuntimeMode.LEGACY_DEBUG_FALLBACK
    if cob_pilot_will_run:
        return OneShotRuntimeMode.COB_PILOT_PREFIX
    src = str(_sep_blob(mission).get("source") or "")
    if src == "tel_sep_promoter_primary" and getattr(settings, "ONE_SHOT_ALLOW_TEL_SEP_PRIMARY", False):
        return OneShotRuntimeMode.TEL_SEP_RUNTIME
    if getattr(mission, "goor_shadow_mode", False) and getattr(mission, "goal_oriented_runtime_audit", None):
        return OneShotRuntimeMode.GOOR_SHADOW_GUIDED
    return OneShotRuntimeMode.SEP_SMART_RUNTIME


def run_execution_with_reliability_guards(
    mission: Mission,
    settings: Any,
    *,
    on_step_started: Optional[Callable[..., Any]],
    on_step_done: Optional[Callable[..., Any]],
) -> Tuple[Optional[Callable[..., Any]], Optional[Callable[..., Any]]]:
    """Envuelve callbacks existentes con bucket de auditoría one-shot (sin UI)."""
    if not getattr(settings, "ONE_SHOT_RELIABILITY_ENABLED", False):
        return on_step_started, on_step_done

    guard_log: List[Dict[str, Any]] = []

    def _wrap_started(cb: Optional[Callable[..., Any]], phase: str) -> Optional[Callable[..., Any]]:
        if cb is None:
            return None

        def _inner(*args: Any, **kwargs: Any):
            guard_log.append({"phase": phase, "t": time.time(), "event": "started"})
            try:
                if getattr(settings, "ONE_SHOT_USE_LOP", True):
                    from app.services.runtime.arl_metrics import get_arl_metrics

                    get_arl_metrics()  # warm singleton — side effect harmless
            except Exception:
                pass
            return cb(*args, **kwargs)

        return _inner

    def _wrap_done(cb: Optional[Callable[..., Any]]) -> Optional[Callable[..., Any]]:
        if cb is None:
            return None

        def _inner(*args: Any, **kwargs: Any):
            guard_log.append({"phase": "step", "t": time.time(), "event": "done"})
            return cb(*args, **kwargs)

        return _inner

    wrapped_s = _wrap_started(on_step_started, "transition")
    wrapped_d = _wrap_done(on_step_done)
    try:
        rep = getattr(mission, "one_shot_reliability_report", None)
        if isinstance(rep, dict):
            rep["live_guards"] = guard_log
        else:
            mission.one_shot_reliability_report = {"live_guards": guard_log}
    except Exception:
        pass
    return wrapped_s, wrapped_d


def record_runtime_learning(
    mission: Mission,
    settings: Any,
    *,
    steps: Sequence[Any],
    mission_result: Any,
) -> Dict[str, Any]:
    """Best-effort: refuerza SLL; OMPC lo escribe ya SmartMissionExecutor (evita duplicar)."""
    summary: Dict[str, Any] = {"sll": None, "ompc": None, "steps_observed": len(steps)}
    if getattr(settings, "OMPC_ENABLED", False):
        summary["ompc"] = "delegated_to_smart_executor_observe_smart_mission_execution"
    try:
        from app.services.runtime.strategy_learning_layer import StrategyContext, record_observation

        if getattr(settings, "SLL_ENABLED", False) and mission_result is not None:
            ok = str(getattr(mission_result, "status", "") or "") == "success"
            ctx = StrategyContext(
                capability_id="mission_terminal",
                operational_intent=str(getattr(mission, "name", "") or "")[:120],
            )
            prof = record_observation(
                context=ctx,
                strategy_name="one_shot_mission_run",
                success=ok,
                duration_ms=float(getattr(mission_result, "duration_ms", 0) or 0),
                mission=mission,
                mission_terminal_failed=not ok,
                require_execution_truth_for_success=False,
                notes="one_shot_orchestrator",
            )
            summary["sll"] = prof.key if prof else None
    except Exception as exc:
        summary["sll_error"] = str(exc)
    return summary


def _score_from_parts(
    readiness: float,
    live_conf: float,
    recovery_cov: float,
    val_cov: float,
    repairability: float,
) -> float:
    return float(
        max(
            0.0,
            min(
                1.0,
                0.34 * readiness
                + 0.18 * live_conf
                + 0.16 * recovery_cov
                + 0.14 * val_cov
                + 0.18 * repairability,
            ),
        )
    )


def build_one_shot_audit(
    mission: Mission,
    settings: Any,
    *,
    readiness: OneShotReadinessResult,
    plan_source: OneShotPlanSource,
    runtime_mode: OneShotRuntimeMode,
    bridge_source: str,
    cob_pilot_will_run: bool,
    execution_learning: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Informe canónico ``one_shot_reliability_report``."""
    live_conf = 0.0
    raw_snap = getattr(mission, "lop_last_snapshot", None)
    if isinstance(raw_snap, dict):
        try:
            live_conf = float(raw_snap.get("confidence") or 0.0)
        except Exception:
            live_conf = 0.0

    recovery_cov = 0.25
    if _recovery_edges_present(mission):
        recovery_cov += 0.35
    arl_a = getattr(mission, "adaptive_runtime_audit", None)
    if isinstance(arl_a, dict) and arl_a.get("events"):
        recovery_cov += 0.2
    recovery_cov = min(1.0, recovery_cov)

    val = _validators_scan(mission)
    val_cov = (
        min(1.0, val["steps_with_validator_hints"] / max(1, val["step_count"]))
        if val["step_count"]
        else 0.35
    )

    amb_blockers: List[str] = []
    try:
        from app.services.missions.semantic_ambiguity_engine import (
            aggregate_plan_ambiguity,
            evaluate_plan_semantic_ambiguity,
        )
        from app.services.missions.semantic_execution_plan import SemanticExecutionPlan

        sep = _sep_blob(mission)
        if sep.get("steps"):
            agg = aggregate_plan_ambiguity(
                evaluate_plan_semantic_ambiguity(SemanticExecutionPlan.from_dict(sep), mission),
            )
            amb_blockers = list(agg.ambiguity_reasons or [])[:12]
    except Exception:
        pass

    unsafe_blockers = _unsafe_scan_plan(mission)

    repairability = 0.55
    if isinstance(getattr(mission, "operational_runtime_graph", None), dict):
        repairability += 0.15
    if getattr(mission, "truth_blocks", None):
        repairability += 0.12
    repairability = min(1.0, repairability)

    oss = _score_from_parts(
        readiness.readiness_score,
        live_conf,
        recovery_cov,
        val_cov,
        repairability,
    )

    report = {
        "version": 1,
        "ts": time.time(),
        "readiness_score": readiness.readiness_score,
        "preflight_outcome": readiness.outcome.value,
        "preflight_reasons": list(readiness.reasons),
        "preflight_warnings": list(readiness.warnings),
        "preflight_blocker_codes": list(readiness.blocker_codes),
        "plan_source": plan_source.value,
        "runtime_mode": runtime_mode.value,
        "bridge_semantic_source": bridge_source,
        "cob_pilot_will_run": bool(cob_pilot_will_run),
        "live_perception_confidence": live_conf,
        "recovery_coverage": recovery_cov,
        "unsafe_blockers": unsafe_blockers,
        "ambiguity_blockers": amb_blockers,
        "validator_coverage": val_cov,
        "strategy_learning_status": execution_learning or {},
        "operational_memory_status": execution_learning or {},
        "repairability_score": repairability,
        "one_shot_reliability_score": oss,
        "recovery_policy_order": [p.value for p in _RECOVERY_ORDER],
    }
    return report


__all__ = [
    "OneShotPreflightOutcome",
    "OneShotPlanSource",
    "OneShotRuntimeMode",
    "OneShotRecoveryPolicy",
    "OneShotReadinessResult",
    "prepare_mission_for_reliable_execution",
    "attach_live_perception",
    "run_preflight_truth_check",
    "run_preflight_lop_check",
    "choose_plan_source",
    "choose_recovery_policy",
    "compute_one_shot_readiness",
    "select_runtime_mode",
    "run_execution_with_reliability_guards",
    "record_runtime_learning",
    "build_one_shot_audit",
    "plan_declares_coords_primary",
]
