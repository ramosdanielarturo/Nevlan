"""Operational Convergence & Repeatability Program (OCRP).

Convierte la arquitectura inteligente existente en convergencia operacional
estadística real: anti-jitter, anti-storm, timing adaptativo y repetibilidad.

No crea runtime paralelo — compone señales de UOOL, ARL, LONE, ERL, LOP, etc.
"""
from __future__ import annotations

import hashlib
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    AdaptiveRuntimeTimingModel,
    DominantOperationalStrategy,
    Mission,
    OperationalActionDecision,
    OperationalActionKind,
    OperationalConvergenceState,
    OperationalExecutionSignature,
    OperationalStabilizationHints,
    StepStabilityIndex,
    UnifiedOperationalExecutionContext,
)
from app.core.logger import log

_EXECUTE_ALWAYS_THRESHOLD = 0.95

_NORMAL_OCRP_MESSAGES = {
    "continuing": "Continuando proceso…",
    "relocating": "Reencontrando contexto…",
    "validating": "Validando información…",
}

_CONVERGENCE_DETECTORS = (
    "navigation_oscillation",
    "recovery_storm",
    "strategy_jitter",
    "validator_instability",
    "entity_flip_flop",
    "collection_mismatch_cascade",
    "perception_divergence",
    "repeated_no_progress",
    "unstable_dominant_strategy",
    "over_recovery",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp01(v: float) -> float:
    return round(max(0.0, min(1.0, float(v))), 4)


def _sequence_stability(seq: Sequence[str]) -> float:
    if not seq:
        return 0.55
    if len(seq) == 1:
        return 0.85
    changes = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
    flip_rate = changes / max(len(seq) - 1, 1)
    return _clamp01(1.0 - flip_rate * 0.85)


def _variance_penalty(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    try:
        var = statistics.pvariance(values)
        return _clamp01(min(1.0, var / max(statistics.mean(values) or 1.0, 0.001)))
    except Exception:
        return 0.0


def _uool_trace(mission: Mission) -> List[Dict[str, Any]]:
    return [t for t in (getattr(mission, "uool_audit_trace", None) or []) if isinstance(t, dict)]


def _signatures(mission: Mission) -> List[Dict[str, Any]]:
    return [s for s in (getattr(mission, "ocrp_execution_signatures", None) or []) if isinstance(s, dict)]


def _strategies_from_mission(mission: Mission) -> List[str]:
    out: List[str] = []
    for t in _uool_trace(mission):
        s = str(t.get("strategy") or "")
        if s:
            out.append(s)
    rep = getattr(mission, "uool_orchestration_report", None) or {}
    if isinstance(rep, dict) and rep.get("dominant_strategy"):
        out.append(str(rep["dominant_strategy"]))
    return out


def _navigation_actions(mission: Mission) -> List[str]:
    rows = getattr(mission, "operational_navigation_audit", None) or []
    return [
        str(r.get("navigation_action") or r.get("action") or "")
        for r in rows if isinstance(r, dict) and (r.get("navigation_action") or r.get("action"))
    ]


def _recovery_events(mission: Mission) -> List[str]:
    out: List[str] = []
    arl = getattr(mission, "adaptive_runtime_audit", None) or {}
    if isinstance(arl, dict):
        for ev in arl.get("events") or []:
            if isinstance(ev, dict):
                out.append(str(ev.get("kind") or ev.get("strategy") or "arl_event"))
    for t in _uool_trace(mission):
        if str(t.get("action") or "") in ("recover", "repair", "fallback"):
            out.append(str(t.get("action")))
    return out


def _entity_ids(mission: Mission) -> List[str]:
    rows = getattr(mission, "entity_runtime_resolution_audit", None) or []
    return [str(r.get("entity_id") or r.get("display_name") or "") for r in rows if isinstance(r, dict)]


def _collection_ids(mission: Mission) -> List[str]:
    rows = getattr(mission, "collection_entity_resolution_audit", None) or []
    return [str(r.get("collection_id") or r.get("collection_type") or "") for r in rows if isinstance(r, dict)]


def _timing_samples(mission: Mission) -> List[float]:
    samples: List[float] = []
    lat = getattr(mission, "runtime_latency_report", None) or {}
    if isinstance(lat, dict):
        for sec in lat.get("sections") or []:
            if isinstance(sec, dict) and sec.get("duration_ms") is not None:
                samples.append(float(sec["duration_ms"]))
    for sig in _signatures(mission)[-20:]:
        d = sig.get("duration_ms")
        if d is not None:
            samples.append(float(d))
    return samples


# ── Fase 3: Convergence detectors ───────────────────────────────────────────


def detect_navigation_oscillation(mission: Mission) -> Tuple[bool, List[str]]:
    actions = _navigation_actions(mission)[-12:]
    if len(actions) < 4:
        return False, []
    flags: List[str] = []
    for i in range(2, len(actions)):
        if actions[i] == actions[i - 2] and actions[i] != actions[i - 1]:
            flags.append(f"oscillation:{actions[i - 1]}↔{actions[i]}")
    pairs = Counter(tuple(sorted([actions[i], actions[i + 1]])) for i in range(len(actions) - 1))
    for pair, n in pairs.most_common(2):
        if n >= 3:
            flags.append(f"nav_ping_pong:{pair[0]}/{pair[1]}x{n}")
    return bool(flags), flags[:6]


def detect_recovery_storm(mission: Mission) -> Tuple[bool, List[str]]:
    rec = _recovery_events(mission)[-16:]
    if len(rec) < 5:
        return False, []
    recent = rec[-8:]
    if len(recent) >= 5 and sum(1 for r in recent if r) >= 5:
        return True, [f"recovery_storm:count={len(recent)}"]
    return False, []


def detect_strategy_jitter(mission: Mission) -> Tuple[bool, List[str]]:
    strategies = _strategies_from_mission(mission)[-10:]
    if len(strategies) < 4:
        return False, []
    stability = _sequence_stability(strategies)
    if stability < 0.45:
        return True, [f"strategy_jitter:stability={stability}"]
    return False, []


def detect_validator_instability(mission: Mission) -> Tuple[bool, List[str]]:
    flags: List[str] = []
    for sig in _signatures(mission)[-5:]:
        outcomes = sig.get("validator_outcomes") or []
        if isinstance(outcomes, list) and len(outcomes) >= 2:
            vals = [str(o.get("ok")) for o in outcomes if isinstance(o, dict)]
            if len(set(vals)) > 1:
                flags.append("validator_flip")
    return bool(flags), flags


def detect_entity_flip_flop(mission: Mission) -> Tuple[bool, List[str]]:
    ids = [i for i in _entity_ids(mission)[-8:] if i]
    if len(ids) < 3:
        return False, []
    unique = len(set(ids))
    if unique >= 3 and len(ids) >= 4:
        return True, [f"entity_flip_flop:unique={unique}"]
    return False, []


def detect_collection_mismatch_cascade(mission: Mission) -> Tuple[bool, List[str]]:
    cols = [c for c in _collection_ids(mission)[-6:] if c]
    if len(cols) >= 3 and len(set(cols)) >= 2:
        erl = getattr(mission, "entity_runtime_resolution_audit", None) or []
        fails = sum(1 for r in erl[-6:] if isinstance(r, dict) and not r.get("resolved", True))
        if fails >= 2:
            return True, [f"collection_mismatch_cascade:fails={fails}"]
    return False, []


def detect_perception_divergence(mission: Mission) -> Tuple[bool, List[str]]:
    lp = getattr(mission, "lop_last_snapshot", None) or {}
    if not isinstance(lp, dict):
        return False, []
    conf = float(lp.get("confidence") or 0.0)
    safe = lp.get("safe_to_continue", True)
    uool = getattr(mission, "uool_orchestration_report", None) or {}
    uconf = float(uool.get("unified_confidence") or 0.5) if isinstance(uool, dict) else 0.5
    if abs(conf - uconf) > 0.45:
        return True, [f"perception_divergence:lop={conf:.2f},uool={uconf:.2f}"]
    if safe is False and uconf > 0.7:
        return True, ["perception_divergence:unsafe_vs_high_confidence"]
    return False, []


def detect_repeated_no_progress(mission: Mission) -> Tuple[bool, List[str]]:
    nav = getattr(mission, "operational_navigation_audit", None) or []
    if len(nav) < 4:
        return False, []
    tail = nav[-6:]
    no_prog = sum(
        1 for r in tail
        if isinstance(r, dict) and r.get("success") is False and not r.get("progress_hint")
    )
    if no_prog >= 3:
        return True, [f"repeated_no_progress:{no_prog}"]
    return False, []


def detect_unstable_dominant_strategy(mission: Mission) -> Tuple[bool, List[str]]:
    sigs = _signatures(mission)[-10:]
    if len(sigs) < 3:
        return False, []
    heads = []
    for s in sigs:
        seq = s.get("dominant_strategy_sequence") or []
        if seq:
            heads.append(str(seq[0]))
    if len(heads) >= 3 and len(set(heads)) >= 3:
        return True, [f"unstable_dominant_strategy:heads={heads[-3:]}"]
    return False, []


def detect_over_recovery(mission: Mission) -> Tuple[bool, List[str]]:
    budget_raw = getattr(mission, "uool_orchestration_report", None) or {}
    if not isinstance(budget_raw, dict):
        return False, []
    b = budget_raw.get("budget") or {}
    if not isinstance(b, dict):
        return False, []
    rec_u = int(b.get("recovery_used") or 0)
    rec_b = int(b.get("recovery_budget") or 4)
    if rec_b > 0 and rec_u >= rec_b:
        return True, [f"over_recovery:{rec_u}/{rec_b}"]
    return False, []


def run_convergence_detectors(mission: Mission) -> Tuple[List[str], List[str]]:
    """Ejecuta todos los detectores — retorna (detectors_fired, instability_flags)."""
    fired: List[str] = []
    flags: List[str] = []
    checks = (
        (detect_navigation_oscillation, "navigation_oscillation"),
        (detect_recovery_storm, "recovery_storm"),
        (detect_strategy_jitter, "strategy_jitter"),
        (detect_validator_instability, "validator_instability"),
        (detect_entity_flip_flop, "entity_flip_flop"),
        (detect_collection_mismatch_cascade, "collection_mismatch_cascade"),
        (detect_perception_divergence, "perception_divergence"),
        (detect_repeated_no_progress, "repeated_no_progress"),
        (detect_unstable_dominant_strategy, "unstable_dominant_strategy"),
        (detect_over_recovery, "over_recovery"),
    )
    for fn, name in checks:
        try:
            hit, detail = fn(mission)
            if hit:
                fired.append(name)
                flags.extend(detail)
        except Exception as exc:
            log.debug("[OCRP] detector %s: %s", name, exc)
    return fired, flags[:24]


# ── Fase 1: Convergence model ───────────────────────────────────────────────


def build_operational_convergence_state(
    mission: Mission,
    settings: Any,
    *,
    ctx: Optional[UnifiedOperationalExecutionContext] = None,
) -> OperationalConvergenceState:
    """Construye OperationalConvergenceState desde auditorías existentes."""
    strategies = _strategies_from_mission(mission)
    nav_actions = _navigation_actions(mission)
    recoveries = _recovery_events(mission)
    entities = _entity_ids(mission)
    collections = _collection_ids(mission)
    timings = _timing_samples(mission)

    strategy_stability = _sequence_stability(strategies)
    navigation_stability = _sequence_stability(nav_actions)
    recovery_stability = _clamp01(1.0 - min(1.0, len(recoveries[-8:]) / 8.0 * 0.9))
    entity_stability = _sequence_stability(entities) if entities else 0.6
    collection_stability = _sequence_stability(collections) if collections else 0.6

    runtime_variance = _variance_penalty(timings)
    timing_consistency = _clamp01(1.0 - runtime_variance)

    validator_consistency = 0.65
    sigs = _signatures(mission)
    if sigs:
        ok_rates = []
        for sig in sigs[-5:]:
            outs = sig.get("validator_outcomes") or []
            if isinstance(outs, list) and outs:
                ok = sum(1 for o in outs if isinstance(o, dict) and o.get("ok"))
                ok_rates.append(ok / len(outs))
        if ok_rates:
            validator_consistency = _clamp01(sum(ok_rates) / len(ok_rates))

    perception_agreement = 0.6
    lp = getattr(mission, "lop_last_snapshot", None) or {}
    if isinstance(lp, dict) and lp:
        conf = float(lp.get("confidence") or 0.5)
        perception_agreement = conf if lp.get("safe_to_continue", True) else conf * 0.5

    orchestration_coherence = 0.55
    if ctx is not None:
        orchestration_coherence = _clamp01(float(ctx.unified_confidence or 0.55))
    else:
        uool = getattr(mission, "uool_orchestration_report", None) or {}
        if isinstance(uool, dict) and uool.get("unified_confidence") is not None:
            orchestration_coherence = _clamp01(float(uool["unified_confidence"]))

    retry_convergence = 0.7
    uool_tr = _uool_trace(mission)
    if uool_tr:
        actions = [str(t.get("action") or "") for t in uool_tr]
        retry_convergence = _sequence_stability(actions)

    detectors_fired, instability_flags = run_convergence_detectors(mission)

    # Penalizar por detectores
    penalty = min(0.35, len(detectors_fired) * 0.04)

    convergence_score = _clamp01(
        strategy_stability * 0.14
        + recovery_stability * 0.12
        + navigation_stability * 0.12
        + entity_stability * 0.10
        + collection_stability * 0.08
        + timing_consistency * 0.10
        + validator_consistency * 0.10
        + perception_agreement * 0.08
        + orchestration_coherence * 0.10
        + retry_convergence * 0.06
        - penalty,
    )

    unstable_steps: List[int] = []
    for row in getattr(mission, "ocrp_step_stability", None) or []:
        if isinstance(row, dict) and row.get("fragile"):
            unstable_steps.append(int(row.get("step_index") or 0))

    return OperationalConvergenceState(
        strategy_stability=strategy_stability,
        recovery_stability=recovery_stability,
        navigation_stability=navigation_stability,
        entity_stability=entity_stability,
        collection_stability=collection_stability,
        runtime_variance=runtime_variance,
        validator_consistency=validator_consistency,
        perception_agreement=perception_agreement,
        orchestration_coherence=orchestration_coherence,
        retry_convergence=retry_convergence,
        timing_consistency=timing_consistency,
        convergence_score=convergence_score,
        instability_flags=instability_flags,
        detectors_fired=detectors_fired,
        unstable_step_indices=unstable_steps[:16],
    )


# ── Fase 2: Repeatability score ─────────────────────────────────────────────


def compute_execution_repeatability_score(
    mission: Mission,
    *,
    signatures: Optional[Sequence[Dict[str, Any]]] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Score compuesto de repetibilidad — 0.95+ = execute-always candidate."""
    sigs = list(signatures if signatures is not None else _signatures(mission))
    conv = build_operational_convergence_state(mission, None)

    successful = sum(1 for s in sigs if str(s.get("status") or "") in ("success", "success_with_recovery"))
    total = len(sigs)
    replay_ratio = successful / max(total, 1) if total else 0.55

    recovery_counts = []
    fallback_counts = []
    nav_variances = []
    for s in sigs:
        recovery_counts.append(len(s.get("recovery_path") or []))
        fb = sum(1 for a in (s.get("dominant_strategy_sequence") or []) if "fallback" in str(a))
        fallback_counts.append(fb)
        nav_variances.append(len(set(s.get("navigation_path") or [])))

    recovery_ratio = _clamp01(1.0 - (sum(recovery_counts) / max(total, 1)) / 6.0) if total else 0.6
    fallback_ratio = _clamp01(1.0 - (sum(fallback_counts) / max(total, 1)) / 4.0) if total else 0.65
    nav_variance_penalty = _variance_penalty([float(v) for v in nav_variances]) if nav_variances else 0.0

    arl_variance = _variance_penalty(recovery_counts) if recovery_counts else 0.0
    lone_variance = _variance_penalty(nav_variances) if nav_variances else 0.0

    entity_drift = _clamp01(1.0 - (1.0 - conv.entity_stability) * 1.2)
    perception_drift = _clamp01(conv.perception_agreement)

    score = _clamp01(
        conv.convergence_score * 0.28
        + replay_ratio * 0.22
        + recovery_ratio * 0.12
        + fallback_ratio * 0.08
        + (1.0 - nav_variance_penalty) * 0.08
        + (1.0 - arl_variance) * 0.06
        + (1.0 - lone_variance) * 0.06
        + entity_drift * 0.05
        + conv.validator_consistency * 0.05
        + conv.timing_consistency * 0.05
        + perception_drift * 0.05
    )

    report = {
        "version": 1,
        "ts": time.time(),
        "repeatability_score": score,
        "execute_always_candidate": score >= _EXECUTE_ALWAYS_THRESHOLD,
        "execute_always_threshold": _EXECUTE_ALWAYS_THRESHOLD,
        "run_count": total,
        "successful_replay_ratio": round(replay_ratio, 4),
        "recovery_ratio": round(recovery_ratio, 4),
        "fallback_ratio": round(fallback_ratio, 4),
        "navigation_variance": round(nav_variance_penalty, 4),
        "arl_variance": round(arl_variance, 4),
        "lone_variance": round(lone_variance, 4),
        "perception_drift": round(1.0 - perception_drift, 4),
        "entity_drift": round(1.0 - entity_drift, 4),
        "convergence_score": conv.convergence_score,
        "detectors_fired": conv.detectors_fired,
    }
    return score, report


# ── Fase 4: Stabilization engine ────────────────────────────────────────────


def stabilize_operational_execution(
    mission: Mission,
    settings: Any,
    ctx: Optional[UnifiedOperationalExecutionContext],
    convergence: OperationalConvergenceState,
) -> OperationalStabilizationHints:
    """Congela estrategia, limita switching/recovery/navigation."""
    hints = OperationalStabilizationHints()
    timing = runtime_self_calibration(mission, settings)
    hints.adaptive_wait_ms = timing.wait_ms

    if not getattr(settings, "OCRP_STABILIZATION_ENABLED", True):
        return hints

    # Estrategia dominante reciente — congelar si jitter detectado
    strategies = _strategies_from_mission(mission)
    if strategies and convergence.strategy_stability < 0.55:
        hints.frozen_strategy = strategies[-1]
        hints.max_strategy_switches = 1
        hints.reason_codes.append("strategy_freeze")

    if "recovery_storm" in convergence.detectors_fired or "over_recovery" in convergence.detectors_fired:
        hints.block_recovery = True
        hints.max_arl_retries = 0
        hints.reason_codes.append("recovery_storm_prevented")

    if "navigation_oscillation" in convergence.detectors_fired:
        hints.block_navigation = True
        hints.max_navigation_branching = 1
        hints.reason_codes.append("navigation_oscillation_prevented")

    if convergence.convergence_score < 0.5:
        hints.prefer_continuity = True
        hints.max_arl_retries = min(hints.max_arl_retries, 1)
        hints.reason_codes.append("low_convergence_prefer_continuity")

    max_arl = int(getattr(settings, "OCRP_MAX_ARL_RETRIES", 1) or 1)
    hints.max_arl_retries = min(hints.max_arl_retries, max_arl)

    if ctx is not None and ctx.budget.recovery_exhausted:
        hints.block_recovery = True

    return hints


def apply_stabilization_to_decision(
    decision: OperationalActionDecision,
    hints: OperationalStabilizationHints,
    convergence: OperationalConvergenceState,
) -> OperationalActionDecision:
    """Aplica hints OCRP a decisión UOOL."""
    d = decision.model_copy(deep=True)
    reasons = list(d.reason_codes)

    if hints.frozen_strategy:
        try:
            frozen = DominantOperationalStrategy(hints.frozen_strategy)
            d.dominant_strategy = frozen
            reasons.append("ocrp_strategy_frozen")
        except ValueError:
            pass

    if hints.block_recovery and d.action in (
        OperationalActionKind.RECOVER,
        OperationalActionKind.REPAIR,
    ):
        d.action = OperationalActionKind.FALLBACK
        d.human_message = _NORMAL_OCRP_MESSAGES["continuing"]
        d.expert_detail = (d.expert_detail or "") + ";ocrp_block_recovery"
        d.allow_arl_recovery = False
        reasons.append("ocrp_block_recovery")

    if hints.block_navigation and d.action == OperationalActionKind.NAVIGATE:
        d.action = OperationalActionKind.WAIT
        d.human_message = _NORMAL_OCRP_MESSAGES["relocating"]
        d.allow_navigation = False
        reasons.append("ocrp_block_navigation")

    if hints.max_arl_retries == 0:
        d.allow_arl_recovery = False

    if convergence.convergence_score < 0.4 and d.action == OperationalActionKind.RECOVER:
        d.action = OperationalActionKind.ASK_HUMAN
        d.human_message = _NORMAL_OCRP_MESSAGES["validating"]
        reasons.append("ocrp_low_convergence_human")

    d.reason_codes = reasons[:16]
    return d


# ── Fase 5: Adaptive timing ─────────────────────────────────────────────────


def runtime_self_calibration(
    mission: Mission,
    settings: Any,
) -> AdaptiveRuntimeTimingModel:
    """Self-calibration de waits/thresholds según convergencia histórica."""
    base = AdaptiveRuntimeTimingModel()
    stored = getattr(mission, "ocrp_timing_model", None)
    if isinstance(stored, dict):
        try:
            base = AdaptiveRuntimeTimingModel.model_validate(stored)
        except Exception:
            pass

    timings = _timing_samples(mission)
    conv = build_operational_convergence_state(mission, settings)
    sigs = _signatures(mission)

    wait_ms = base.wait_ms
    if timings:
        med = statistics.median(timings)
        wait_ms = int(max(200, min(5000, med * 0.15)))

    if conv.timing_consistency < 0.5:
        wait_ms = int(wait_ms * 1.25)

    nav_depth = len(_navigation_actions(mission))
    nav_settle = int(max(250, min(2000, 300 + nav_depth * 25)))

    abandon_ms = base.abandon_after_ms
    if "repeated_no_progress" in conv.detectors_fired:
        abandon_ms = int(abandon_ms * 0.75)

    confidence = _clamp01(0.35 + conv.timing_consistency * 0.4 + min(len(sigs), 10) * 0.025)
    source = "calibrated" if sigs else "default"

    model = AdaptiveRuntimeTimingModel(
        wait_ms=wait_ms,
        revalidate_after_ms=int(max(wait_ms, wait_ms * 1.5)),
        navigation_settle_ms=nav_settle,
        abandon_after_ms=abandon_ms,
        post_action_settle_ms=int(max(300, wait_ms * 0.5)),
        confidence=confidence,
        source=source,
    )
    return model


def persist_calibration(mission: Mission, model: AdaptiveRuntimeTimingModel, audit: Dict[str, Any]) -> None:
    try:
        mission.ocrp_timing_model = model.model_dump(mode="json")
        mission.ocrp_calibration_audit = audit
    except Exception as exc:
        log.debug("[OCRP] persist calibration: %s", exc)


# ── Fase 6: Multi-run convergence ───────────────────────────────────────────


def analyze_multi_run_convergence(
    mission: Mission,
    *,
    bucket_sizes: Sequence[int] = (10, 50, 100, 1000),
) -> Dict[str, Any]:
    """Compara ejecuciones acumuladas — variance, unstable steps/strategies."""
    sigs = _signatures(mission)
    total = len(sigs)
    analysis: Dict[str, Any] = {
        "version": 1,
        "ts": time.time(),
        "total_runs": total,
        "buckets": {},
    }

    for n in bucket_sizes:
        bucket = sigs[-n:] if total else []
        if not bucket:
            analysis["buckets"][str(n)] = {"runs": 0, "insufficient_data": True}
            continue

        scores = [float(s.get("repeatability_score") or 0.0) for s in bucket]
        statuses = [str(s.get("status") or "") for s in bucket]
        strat_heads = []
        for s in bucket:
            seq = s.get("dominant_strategy_sequence") or []
            if seq:
                strat_heads.append(str(seq[0]))

        success_rate = sum(1 for st in statuses if st in ("success", "success_with_recovery")) / len(bucket)
        score_var = _variance_penalty(scores) if len(scores) >= 2 else 0.0
        strat_var = 1.0 - _sequence_stability(strat_heads) if strat_heads else 0.0

        unstable_steps: Counter = Counter()
        for s in bucket:
            for i, st in enumerate(s.get("step_outcomes") or []):
                if str(st) not in ("success", "skipped"):
                    unstable_steps[i] += 1

        analysis["buckets"][str(n)] = {
            "runs": len(bucket),
            "success_rate": round(success_rate, 4),
            "score_variance": round(score_var, 4),
            "strategy_variance": round(strat_var, 4),
            "mean_repeatability": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "unstable_step_indices": [k for k, v in unstable_steps.most_common(5) if v >= 2],
            "unstable_strategies": list(set(strat_heads))[:8],
        }

    return analysis


# ── Fase 8: Execution signatures ────────────────────────────────────────────


def _fingerprint(parts: Sequence[str]) -> str:
    blob = "|".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_execution_signature(
    mission: Mission,
    *,
    run_id: str = "",
    status: str = "",
    duration_ms: float = 0.0,
    step_outcomes: Optional[List[str]] = None,
) -> OperationalExecutionSignature:
    """Construye firma de ejecución desde auditorías mission."""
    strategies = _strategies_from_mission(mission)
    nav = _navigation_actions(mission)
    rec = _recovery_events(mission)

    timing: Dict[str, float] = {}
    lat = getattr(mission, "runtime_latency_report", None) or {}
    if isinstance(lat, dict):
        for sec in lat.get("sections") or []:
            if isinstance(sec, dict) and sec.get("name"):
                timing[str(sec["name"])] = float(sec.get("duration_ms") or 0.0)

    fp = _fingerprint(strategies + nav[:8] + rec[:6] + [status])

    rep_score, _ = compute_execution_repeatability_score(mission)

    return OperationalExecutionSignature(
        run_id=run_id or str(getattr(mission, "id", "")),
        mission_id=str(getattr(mission, "id", "")),
        mission_version=int(getattr(mission, "version", 1) or 1),
        status=status,
        dominant_strategy_sequence=strategies[-32:],
        recovery_path=rec[-24:],
        navigation_path=nav[-32:],
        validator_outcomes=[],
        timing_profile=timing,
        step_outcomes=list(step_outcomes or []),
        convergence_fingerprint=fp,
        repeatability_score=rep_score,
        duration_ms=duration_ms,
    )


def append_execution_signature(mission: Mission, signature: OperationalExecutionSignature) -> None:
    try:
        sigs = list(getattr(mission, "ocrp_execution_signatures", None) or [])
        sigs.append(signature.model_dump(mode="json"))
        mission.ocrp_execution_signatures = sigs[-128:]
    except Exception as exc:
        log.debug("[OCRP] append signature: %s", exc)


# ── Fase 9: Step stability index ──────────────────────────────────────────────


def compute_step_stability_index(
    mission: Mission,
    step_index: int,
    *,
    signatures: Optional[Sequence[Dict[str, Any]]] = None,
) -> StepStabilityIndex:
    """Índice de estabilidad por step."""
    sigs = list(signatures if signatures is not None else _signatures(mission))
    step_id = ""
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    steps = sep.get("steps") or [] if isinstance(sep, dict) else []
    if isinstance(steps, list) and 0 <= step_index < len(steps) and isinstance(steps[step_index], dict):
        step_id = str(steps[step_index].get("id") or "")

    outcomes = []
    for s in sigs:
        so = s.get("step_outcomes") or []
        if isinstance(so, list) and step_index < len(so):
            outcomes.append(str(so[step_index]))

    replay_stability = _sequence_stability(outcomes) if outcomes else 0.55

    erl = getattr(mission, "entity_runtime_resolution_audit", None) or []
    entity_stability = 0.6
    for r in erl:
        if isinstance(r, dict) and int(r.get("step_index") or -1) == step_index:
            entity_stability = float(r.get("confidence") or 0.6)

    nav = _navigation_actions(mission)
    navigation_stability = _sequence_stability(nav) if nav else 0.6

    validator_stability = 0.65
    runtime_variance = _variance_penalty([float(s.get("duration_ms") or 0) for s in sigs[-5:]])

    stability_index = _clamp01(
        replay_stability * 0.35
        + entity_stability * 0.25
        + navigation_stability * 0.20
        + validator_stability * 0.12
        + (1.0 - runtime_variance) * 0.08,
    )

    notes: List[str] = []
    fragile = stability_index < 0.45 or (bool(outcomes) and outcomes.count("failed") >= 2)
    if fragile:
        notes.append("fragile_step")

    return StepStabilityIndex(
        step_index=step_index,
        step_id=step_id,
        replay_stability=replay_stability,
        entity_stability=_clamp01(entity_stability),
        navigation_stability=navigation_stability,
        validator_stability=validator_stability,
        runtime_variance=runtime_variance,
        stability_index=stability_index,
        fragile=fragile,
        notes=notes,
    )


def refresh_step_stability_indices(mission: Mission) -> List[StepStabilityIndex]:
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    steps = sep.get("steps") or [] if isinstance(sep, dict) else []
    indices = [compute_step_stability_index(mission, i) for i in range(len(steps))]
    try:
        mission.ocrp_step_stability = [x.model_dump(mode="json") for x in indices]
    except Exception:
        pass
    return indices


# ── Fase 11: UOOL integration entrypoints ───────────────────────────────────


def apply_ocrp_to_uool_decision(
    decision: OperationalActionDecision,
    mission: Mission,
    settings: Any,
    ctx: Optional[UnifiedOperationalExecutionContext] = None,
) -> Tuple[OperationalActionDecision, OperationalConvergenceState, OperationalStabilizationHints]:
    """Integración UOOL — convergence + stabilization hints."""
    if not getattr(settings, "OCRP_ENABLED", False):
        conv = build_operational_convergence_state(mission, settings, ctx=ctx)
        return decision, conv, OperationalStabilizationHints()

    conv = build_operational_convergence_state(mission, settings, ctx=ctx)
    hints = stabilize_operational_execution(mission, settings, ctx, conv)
    stabilized = apply_stabilization_to_decision(decision, hints, conv)

    try:
        mission.ocrp_convergence_state = conv.model_dump(mode="json")
        score, rep = compute_execution_repeatability_score(mission)
        rep["last_decision"] = stabilized.action.value
        mission.ocrp_repeatability_report = rep
    except Exception as exc:
        log.debug("[OCRP] persist convergence: %s", exc)

    return stabilized, conv, hints


def prepare_ocrp_for_execution(mission: Mission, settings: Any) -> Dict[str, Any]:
    """Preflight OCRP al armar misión."""
    if not getattr(settings, "OCRP_ENABLED", False):
        return {"skipped": True, "reason": "OCRP_ENABLED=false"}

    audit: Dict[str, Any] = {"ok": True}
    try:
        conv = build_operational_convergence_state(mission, settings)
        score, rep = compute_execution_repeatability_score(mission)
        timing = runtime_self_calibration(mission, settings)
        multi = analyze_multi_run_convergence(mission)
        refresh_step_stability_indices(mission)

        mission.ocrp_convergence_state = conv.model_dump(mode="json")
        mission.ocrp_repeatability_report = rep
        persist_calibration(mission, timing, {"ts": time.time(), "source": timing.source})

        audit.update({
            "convergence_score": conv.convergence_score,
            "repeatability_score": score,
            "execute_always_candidate": rep.get("execute_always_candidate"),
            "detectors_fired": conv.detectors_fired,
            "multi_run": multi,
        })

        if getattr(settings, "OCRP_BLOCK_UNSTABLE_MISSIONS", False) and score < 0.35:
            audit["ok"] = False
            audit["blocked"] = "unstable_mission"
    except Exception as exc:
        audit["ok"] = False
        audit["error"] = str(exc)
        log.debug("[OCRP] prepare: %s", exc)
    return audit


def record_post_run_convergence(
    mission: Mission,
    settings: Any,
    *,
    run_id: str = "",
    status: str = "",
    duration_ms: float = 0.0,
    step_outcomes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Post-run: firma + multi-run + calibración."""
    if not getattr(settings, "OCRP_ENABLED", False):
        return {"skipped": True}

    sig = build_execution_signature(
        mission,
        run_id=run_id,
        status=status,
        duration_ms=duration_ms,
        step_outcomes=step_outcomes,
    )
    append_execution_signature(mission, sig)
    timing = runtime_self_calibration(mission, settings)
    persist_calibration(mission, timing, {"ts": time.time(), "post_run": True})
    score, rep = compute_execution_repeatability_score(mission)
    mission.ocrp_repeatability_report = rep
    multi = analyze_multi_run_convergence(mission)
    refresh_step_stability_indices(mission)
    return {
        "repeatability_score": score,
        "signature_fingerprint": sig.convergence_fingerprint,
        "multi_run": multi,
    }


def ocrp_adaptive_wait_ms(mission: Mission, settings: Any) -> int:
    """Wait adaptativo para UOOL (reemplaza sleep fijo cuando OCRP activo)."""
    if not getattr(settings, "OCRP_ENABLED", False):
        return int(getattr(settings, "UOOL_WAIT_MS", 800) or 800)
    raw = getattr(mission, "ocrp_timing_model", None)
    if isinstance(raw, dict) and raw.get("wait_ms") is not None:
        return int(raw["wait_ms"])
    return runtime_self_calibration(mission, settings).wait_ms


def expert_audit_lines(mission: Mission) -> List[str]:
    lines: List[str] = []
    rep = getattr(mission, "ocrp_repeatability_report", None) or {}
    conv = getattr(mission, "ocrp_convergence_state", None) or {}
    if isinstance(rep, dict) and rep.get("repeatability_score") is not None:
        lines.append(
            f"OCRP repeatability={rep.get('repeatability_score')} "
            f"execute_always={rep.get('execute_always_candidate')}",
        )
    if isinstance(conv, dict) and conv.get("convergence_score") is not None:
        lines.append(
            f"  convergence={conv.get('convergence_score')} "
            f"variance={conv.get('runtime_variance')} "
            f"strategy_stability={conv.get('strategy_stability')}",
        )
    if isinstance(conv, dict) and conv.get("detectors_fired"):
        lines.append(f"  detectors={conv.get('detectors_fired')[:6]}")
    fragile = [
        r for r in (getattr(mission, "ocrp_step_stability", None) or [])
        if isinstance(r, dict) and r.get("fragile")
    ]
    if fragile:
        lines.append(f"  fragile_steps={[r.get('step_index') for r in fragile[:5]]}")
    storms = []
    if isinstance(conv, dict):
        for d in conv.get("detectors_fired") or []:
            if d in ("recovery_storm", "navigation_oscillation", "over_recovery"):
                storms.append(d)
    if storms:
        lines.append(f"  storms_avoided={storms}")
    return lines


def normal_status_for_ocrp(action: OperationalActionKind) -> str:
    if action in (OperationalActionKind.RECOVER, OperationalActionKind.FALLBACK, OperationalActionKind.REPAIR):
        return _NORMAL_OCRP_MESSAGES["continuing"]
    if action in (OperationalActionKind.NAVIGATE, OperationalActionKind.WAIT):
        return _NORMAL_OCRP_MESSAGES["relocating"]
    if action == OperationalActionKind.EXECUTE:
        return _NORMAL_OCRP_MESSAGES["validating"]
    return _NORMAL_OCRP_MESSAGES["continuing"]


__all__ = [
    "AdaptiveRuntimeTimingModel",
    "OperationalConvergenceState",
    "OperationalExecutionSignature",
    "OperationalStabilizationHints",
    "StepStabilityIndex",
    "analyze_multi_run_convergence",
    "apply_ocrp_to_uool_decision",
    "apply_stabilization_to_decision",
    "append_execution_signature",
    "build_execution_signature",
    "build_operational_convergence_state",
    "compute_execution_repeatability_score",
    "compute_step_stability_index",
    "detect_navigation_oscillation",
    "detect_recovery_storm",
    "detect_strategy_jitter",
    "expert_audit_lines",
    "normal_status_for_ocrp",
    "ocrp_adaptive_wait_ms",
    "prepare_ocrp_for_execution",
    "record_post_run_convergence",
    "refresh_step_stability_indices",
    "run_convergence_detectors",
    "runtime_self_calibration",
    "stabilize_operational_execution",
]
