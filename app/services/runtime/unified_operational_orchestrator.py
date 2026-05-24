"""Unified Operational Orchestration Layer (UOOL).

Unifica UCES, ERL, LONE, ARL, LOP, SLL, OMPC, TEL, GOOR, truth y ambigüedad
bajo un único modelo operacional y una sola autoridad decisoria.

No reemplaza SmartExecutor — actúa como brain / objective manager coordinador.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    DominantOperationalStrategy,
    Mission,
    OperationalActionDecision,
    OperationalActionKind,
    OperationalExecutionPhase,
    UnifiedOperationalExecutionContext,
    UnifiedOperationalMemorySignals,
    OperationalRecoveryBudget,
)
from app.core.logger import log

# Mensajes UX vista normal (sin tokens internos).
_NORMAL_STATUS_BY_ACTION: Dict[OperationalActionKind, str] = {
    OperationalActionKind.EXECUTE: "Ejecutando…",
    OperationalActionKind.NAVIGATE: "Reencontrando elemento…",
    OperationalActionKind.WAIT: "Esperando contenido…",
    OperationalActionKind.RECOVER: "Continuando proceso…",
    OperationalActionKind.REPAIR: "Adaptando automáticamente…",
    OperationalActionKind.ASK_HUMAN: "Necesita aclaración…",
    OperationalActionKind.STOP: "No es seguro continuar",
    OperationalActionKind.FALLBACK: "Continuando proceso…",
}

_NORMAL_STATUS_BY_PHASE: Dict[OperationalExecutionPhase, str] = {
    OperationalExecutionPhase.PERCEPTION: "Ejecutando…",
    OperationalExecutionPhase.INTERPRETATION: "Ejecutando…",
    OperationalExecutionPhase.NAVIGATION: "Reencontrando elemento…",
    OperationalExecutionPhase.LOCALIZATION: "Reencontrando elemento…",
    OperationalExecutionPhase.VALIDATION: "Validando contexto…",
    OperationalExecutionPhase.EXECUTION: "Ejecutando…",
    OperationalExecutionPhase.RECOVERY: "Continuando proceso…",
    OperationalExecutionPhase.REPAIR: "Adaptando automáticamente…",
    OperationalExecutionPhase.CONTINUITY: "Continuando proceso…",
    OperationalExecutionPhase.COMPLETED: "Terminado",
    OperationalExecutionPhase.BLOCKED: "Necesita aclaración…",
    OperationalExecutionPhase.UNSAFE: "No es seguro continuar",
}

_UNSAFE_MARKERS = (
    "payment", "pago", "password", "contraseña", "delete permanently",
    "eliminar permanentemente", "wire transfer", "checkout", "2fa", "otp",
)

_AMBIGUITY_BLOCKERS = (
    "SEMANTIC_INTENT_MULTI_TARGET",
    "RB_MULTI_TARGET",
    "entity_resolution:ambiguity",
    "semantic_ambiguity",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sep_blob(mission: Mission) -> Dict[str, Any]:
    sep = getattr(mission, "semantic_execution_plan", None)
    return sep if isinstance(sep, dict) else {}


def _step_dicts(mission: Mission) -> List[Dict[str, Any]]:
    return [s for s in (_sep_blob(mission).get("steps") or []) if isinstance(s, dict)]


def _truth_confirmed(mission: Mission) -> bool:
    try:
        from app.services.missions.execution_truth_engine import is_execution_truth_confirmed

        return bool(is_execution_truth_confirmed(mission))
    except Exception:
        return False


def _ambiguity_requires_human(mission: Mission) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
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
            if agg.requires_human_confirmation:
                reasons.extend(list(agg.ambiguity_reasons or [])[:8])
                return True, reasons
    except Exception as exc:
        log.debug("[UOOL] ambiguity scan: %s", exc)
    for r in _sep_blob(mission).get("not_ready_reasons") or []:
        rs = str(r)
        if any(b in rs for b in _AMBIGUITY_BLOCKERS):
            reasons.append(rs)
            return True, reasons
    return False, reasons


def _unsafe_from_step(step: Any, mission: Mission) -> Tuple[bool, List[str]]:
    hits: List[str] = []
    label = ""
    if step is not None:
        label = str(getattr(step, "human_label", "") or getattr(step, "kind", "") or "")
        params = getattr(step, "params", None) or {}
        if isinstance(params, dict):
            label += " " + str(params.get("human_label") or "")
    for m in _UNSAFE_MARKERS:
        if m in label.lower():
            hits.append(f"unsafe_hint:{m}")
    return bool(hits), hits


def merge_cross_layer_memory_signals(mission: Mission, settings: Any) -> UnifiedOperationalMemorySignals:
    """Fase 7 — unifica SLL, OMPC, navegación, entidad y colección."""
    sig = UnifiedOperationalMemorySignals()
    notes: List[str] = []

    # SLL hints from adaptive audit / strategy learning
    arl = getattr(mission, "adaptive_runtime_audit", None) or {}
    if isinstance(arl, dict):
        for ev in arl.get("events") or []:
            if isinstance(ev, dict) and ev.get("strategy"):
                sig.sll_strategy_hints.append(str(ev["strategy"])[:64])
    sig.sll_confidence = min(1.0, 0.35 + 0.08 * len(sig.sll_strategy_hints))

    # OMPC pattern keys
    ompc_report = getattr(mission, "one_shot_reliability_report", None) or {}
    if isinstance(ompc_report, dict):
        om = ompc_report.get("operational_memory_status") or {}
        if isinstance(om, dict) and om.get("ompc"):
            sig.ompc_pattern_keys.append(str(om["ompc"])[:80])
            sig.ompc_boost = 0.12
            notes.append("ompc_echo")

    # Navigation memory (LONE)
    for row in getattr(mission, "operational_navigation_audit", None) or []:
        if isinstance(row, dict):
            act = str(row.get("navigation_action") or row.get("action") or "")
            if act:
                sig.navigation_memory_hits.append(act[:48])

    # Entity memory (ERL)
    for row in getattr(mission, "entity_runtime_resolution_audit", None) or []:
        if isinstance(row, dict):
            eid = str(row.get("entity_id") or row.get("display_name") or "")
            if eid:
                sig.entity_memory_hits.append(eid[:48])

    # Collection memory (UCES)
    for row in getattr(mission, "collection_entity_resolution_audit", None) or []:
        if isinstance(row, dict):
            cid = str(row.get("collection_id") or row.get("collection_type") or "")
            if cid:
                sig.collection_memory_hits.append(cid[:48])

    if getattr(settings, "SLL_ENABLED", False):
        sig.sll_confidence = min(1.0, sig.sll_confidence + 0.15)
    if getattr(settings, "OMPC_ENABLED", False):
        sig.ompc_boost = min(1.0, sig.ompc_boost + 0.1)

    agreements = [
        min(1.0, len(sig.sll_strategy_hints) / 3.0),
        min(1.0, len(sig.navigation_memory_hits) / 4.0),
        min(1.0, len(sig.entity_memory_hits) / 3.0),
        min(1.0, len(sig.collection_memory_hits) / 2.0),
        sig.ompc_boost,
    ]
    sig.memory_agreement = round(sum(agreements) / max(len(agreements), 1), 4)
    sig.notes = notes[:12]
    return sig


def evaluate_operational_continuity_uool(
    mission: Mission,
    *,
    step_index: int = 0,
) -> Tuple[float, List[str], OperationalExecutionPhase]:
    """Fase 4 — continuidad global más allá de «el target existe»."""
    notes: List[str] = []
    score = 0.55

    # OCR shadow
    try:
        from app.services.runtime import operational_runtime_graph as orgmod
        from app.services.runtime.operational_continuity_runtime import (
            evaluate_operational_continuity,
        )

        graph = orgmod.build_operational_runtime_graph(mission)
        if graph is not None and graph.nodes:
            cont, _states = evaluate_operational_continuity(mission, graph)
            score = float(cont or score)
            notes.append(f"ocr_continuity:{cont}")
    except Exception as exc:
        log.debug("[UOOL] OCR continuity: %s", exc)

    # GOOR goal continuity
    try:
        from app.services.runtime import operational_runtime_graph as orgmod
        from app.services.runtime.goal_oriented_runtime import evaluate_goal_continuity

        graph = orgmod.build_operational_runtime_graph(mission)
        if graph is not None and graph.goals:
            gcont, gnotes = evaluate_goal_continuity(mission, graph)
            score = round(score * 0.55 + float(gcont or 0.0) * 0.45, 4)
            notes.extend(gnotes[:4])
    except Exception as exc:
        log.debug("[UOOL] GOOR continuity: %s", exc)

    # Navigation progress
    nav_audit = getattr(mission, "operational_navigation_audit", None) or []
    if nav_audit:
        recent = nav_audit[-6:]
        successes = sum(
            1 for r in recent
            if isinstance(r, dict) and r.get("success") is not False
        )
        nav_prog = successes / max(len(recent), 1)
        score = round(score * 0.85 + nav_prog * 0.15, 4)
        notes.append(f"navigation_progress:{nav_prog:.2f}")

    # Step alignment — objective still valid within mission flow
    steps = _step_dicts(mission)
    if steps and 0 <= step_index < len(steps):
        score = min(1.0, score + 0.05)
        notes.append("step_objective_in_flow")
    elif step_index >= len(steps) and steps:
        score *= 0.7
        notes.append("step_index_past_plan")

    if _truth_confirmed(mission):
        score = min(1.0, score + 0.08)
        notes.append("execution_truth_confirmed")

    phase = OperationalExecutionPhase.CONTINUITY
    if score < 0.35:
        phase = OperationalExecutionPhase.RECOVERY
    elif score >= 0.75:
        phase = OperationalExecutionPhase.EXECUTION

    return round(max(0.0, min(1.0, score)), 4), notes, phase


def compute_unified_operational_confidence(
    ctx: UnifiedOperationalExecutionContext,
    mission: Mission,
) -> float:
    """Fase 8 — score global compuesto."""
    truth_term = 1.0 if ctx.execution_truth else 0.35
    cont = float(ctx.continuity_confidence or 0.0)
    nav = float(ctx.navigation_confidence or 0.0)
    runtime = float(ctx.runtime_confidence or 0.0)
    mem = float(ctx.memory_signals.memory_agreement or 0.0)

    entity_conf = 0.5
    if ctx.target_entity and isinstance(ctx.target_entity, dict):
        entity_conf = float(ctx.target_entity.get("confidence") or 0.5)

    collection_conf = 0.5
    if ctx.target_collection and isinstance(ctx.target_collection, dict):
        collection_conf = float(ctx.target_collection.get("confidence") or 0.5)

    validator_align = 0.5
    for c in ctx.current_constraints:
        if "validator" in str(c).lower():
            validator_align = 0.75
            break

    live_agree = 0.5
    lp = ctx.live_perception or {}
    if isinstance(lp, dict):
        if lp.get("safe_to_continue") is True:
            live_agree = float(lp.get("confidence") or 0.65)
        elif lp.get("safe_to_continue") is False:
            live_agree = 0.15

    ambiguity_penalty = 0.0
    amb, _ = _ambiguity_requires_human(mission)
    if amb:
        ambiguity_penalty = 0.35

    safety_penalty = 0.0
    if ctx.active_blockers:
        safety_penalty = min(0.5, 0.12 * len(ctx.active_blockers))
    if ctx.current_phase == OperationalExecutionPhase.UNSAFE:
        safety_penalty = max(safety_penalty, 0.6)

    raw = (
        truth_term * 0.18
        + cont * 0.16
        + nav * 0.12
        + runtime * 0.12
        + entity_conf * 0.10
        + collection_conf * 0.08
        + validator_align * 0.08
        + mem * 0.08
        + live_agree * 0.08
    )
    score = max(0.0, min(1.0, raw - ambiguity_penalty - safety_penalty))
    return round(score, 4)


def resolve_dominant_operational_strategy(
    ctx: UnifiedOperationalExecutionContext,
    mission: Mission,
    *,
    step: Any = None,
) -> DominantOperationalStrategy:
    """Fase 3 — evita conflictos entre capas."""
    if ctx.current_phase == OperationalExecutionPhase.UNSAFE:
        return DominantOperationalStrategy.FALLBACK_SAFE

    amb, _ = _ambiguity_requires_human(mission)
    if amb or ctx.current_phase == OperationalExecutionPhase.BLOCKED:
        return DominantOperationalStrategy.FALLBACK_SAFE

    budget = ctx.budget
    if budget.navigation_exhausted and budget.recovery_exhausted:
        return DominantOperationalStrategy.FALLBACK_SAFE

    lp = ctx.live_perception or {}
    if isinstance(lp, dict) and lp.get("dynamic_content_pending"):
        return DominantOperationalStrategy.WAIT_DYNAMIC_CONTENT

    # Entity-first when entity signals dominate
    if ctx.target_entity and isinstance(ctx.target_entity, dict):
        econf = float(ctx.target_entity.get("confidence") or 0.0)
        if econf >= 0.72 and ctx.memory_signals.entity_memory_hits:
            return DominantOperationalStrategy.ENTITY_FIRST
        if econf >= 0.78:
            return DominantOperationalStrategy.ENTITY_FIRST

    # Collection navigation when collection visible but entity weak
    if ctx.target_collection and isinstance(ctx.target_collection, dict):
        cconf = float(ctx.target_collection.get("confidence") or 0.0)
        if cconf >= 0.55 and ctx.continuity_confidence < 0.65:
            return DominantOperationalStrategy.COLLECTION_NAVIGATION

    # Continuity recovery when flow drifting
    if ctx.continuity_confidence < 0.45:
        return DominantOperationalStrategy.CONTINUITY_RECOVERY

    # Memory reuse when OMPC/SLL agree
    if ctx.memory_signals.memory_agreement >= 0.65 and ctx.memory_signals.ompc_boost > 0:
        return DominantOperationalStrategy.OPERATIONAL_MEMORY_REUSE

    # Validator-driven steps
    if step is not None:
        params = getattr(step, "params", None) or {}
        if isinstance(params, dict) and (
            params.get("validator") or params.get("postcondition") or params.get("validation_hint")
        ):
            return DominantOperationalStrategy.VALIDATOR_DRIVEN

    # Semantic relocation when navigation audit shows recent scroll/search
    nav_hits = ctx.memory_signals.navigation_memory_hits
    if nav_hits and any("scroll" in h.lower() or "search" in h.lower() for h in nav_hits[-4:]):
        if ctx.navigation_confidence < 0.6:
            return DominantOperationalStrategy.SEMANTIC_RELOCATION

    if ctx.current_phase in (
        OperationalExecutionPhase.RECOVERY,
        OperationalExecutionPhase.REPAIR,
    ):
        return DominantOperationalStrategy.CONTINUITY_RECOVERY

    return DominantOperationalStrategy.EXECUTE_STEP


def _load_budget_from_mission(mission: Mission, settings: Any) -> OperationalRecoveryBudget:
    raw = getattr(mission, "uool_orchestration_report", None) or {}
    if isinstance(raw, dict) and raw.get("budget"):
        try:
            return OperationalRecoveryBudget.model_validate(raw["budget"])
        except Exception:
            pass
    max_rec = int(getattr(settings, "UOOL_MAX_RECOVERY_ROUNDS", 4) or 4)
    max_rep = int(getattr(settings, "UOOL_MAX_REPAIR_ROUNDS", 2) or 2)
    max_nav = int(getattr(settings, "UOOL_MAX_NAVIGATION_ROUNDS", 8) or 8)
    return OperationalRecoveryBudget(
        recovery_budget=max_rec,
        repair_budget=max_rep,
        navigation_budget=max_nav,
    )


def build_unified_operational_context(
    mission: Mission,
    settings: Any,
    *,
    step_index: int = 0,
    step: Any = None,
    state_snapshot: Any = None,
) -> UnifiedOperationalExecutionContext:
    """Fase 1 — fusiona todas las capas en UOEC."""
    steps = _step_dicts(mission)
    step_id = str(getattr(step, "id", "") or "")
    if not step_id and steps and 0 <= step_index < len(steps):
        step_id = str(steps[step_index].get("id") or "")

    goal = str(getattr(mission, "name", "") or "")
    sep = _sep_blob(mission)
    if steps and 0 <= step_index < len(steps):
        goal = str(steps[step_index].get("human_label") or goal)

    target_entity: Optional[Dict[str, Any]] = None
    erl_rows = getattr(mission, "entity_runtime_resolution_audit", None) or []
    if erl_rows:
        last = erl_rows[-1]
        if isinstance(last, dict):
            target_entity = {
                "entity_id": last.get("entity_id"),
                "display_name": last.get("display_name"),
                "entity_type": last.get("entity_type"),
                "confidence": float(last.get("confidence") or 0.0),
            }

    target_collection: Optional[Dict[str, Any]] = None
    uces_rows = getattr(mission, "collection_entity_resolution_audit", None) or []
    if uces_rows:
        last_c = uces_rows[-1]
        if isinstance(last_c, dict):
            target_collection = {
                "collection_id": last_c.get("collection_id"),
                "collection_type": last_c.get("collection_type"),
                "confidence": float(last_c.get("confidence") or last_c.get("selection_confidence") or 0.0),
            }

    lp_raw = getattr(mission, "lop_last_snapshot", None)
    live_perception = dict(lp_raw) if isinstance(lp_raw, dict) else None

    continuity, cont_notes, cont_phase = evaluate_operational_continuity_uool(
        mission, step_index=step_index,
    )

    memory_signals = merge_cross_layer_memory_signals(mission, settings)

    nav_conf = 0.45
    nav_audit = getattr(mission, "operational_navigation_audit", None) or []
    if nav_audit:
        recent_ok = sum(
            1 for r in nav_audit[-5:]
            if isinstance(r, dict) and r.get("success") is not False
        )
        nav_conf = min(1.0, 0.4 + recent_ok * 0.12)

    runtime_conf = 0.5
    oss = getattr(mission, "one_shot_reliability_report", None) or {}
    if isinstance(oss, dict) and oss.get("one_shot_reliability_score") is not None:
        runtime_conf = float(oss["one_shot_reliability_score"])

    blockers: List[str] = []
    constraints: List[str] = []
    if live_perception and not live_perception.get("safe_to_continue", True):
        blockers.append("lop_not_safe_to_continue")
    amb, amb_reasons = _ambiguity_requires_human(mission)
    if amb:
        blockers.extend(amb_reasons[:4])

    unsafe, unsafe_hits = _unsafe_from_step(step, mission)
    if unsafe:
        blockers.extend(unsafe_hits)

    phase = OperationalExecutionPhase.PERCEPTION
    if step is not None:
        phase = OperationalExecutionPhase.EXECUTION
    if blockers and amb:
        phase = OperationalExecutionPhase.BLOCKED
    if unsafe:
        phase = OperationalExecutionPhase.UNSAFE
    elif continuity < 0.4:
        phase = cont_phase

    ctx = UnifiedOperationalExecutionContext(
        operational_goal=goal[:240],
        target_entity=target_entity,
        target_collection=target_collection,
        active_surface=str(sep.get("intent_status") or ""),
        current_phase=phase,
        runtime_confidence=round(runtime_conf, 4),
        continuity_confidence=continuity,
        navigation_confidence=round(nav_conf, 4),
        execution_truth=_truth_confirmed(mission),
        current_constraints=constraints,
        active_blockers=blockers[:16],
        memory_signals=memory_signals,
        live_perception=live_perception,
        adaptation_state={"continuity_notes": cont_notes[:8]},
        budget=_load_budget_from_mission(mission, settings),
        step_index=step_index,
        step_id=step_id,
    )
    ctx.dominant_strategy = resolve_dominant_operational_strategy(ctx, mission, step=step)
    ctx.unified_confidence = compute_unified_operational_confidence(ctx, mission)
    ctx.orchestration_trace.append({
        "event": "context_built",
        "ts": time.time(),
        "phase": ctx.current_phase.value,
        "strategy": ctx.dominant_strategy.value,
        "confidence": ctx.unified_confidence,
    })
    return ctx


def decide_next_operational_action(
    ctx: UnifiedOperationalExecutionContext,
    mission: Mission,
    settings: Any,
) -> OperationalActionDecision:
    """Fase 2 — única autoridad decisoria global."""
    strategy = resolve_dominant_operational_strategy(ctx, mission)
    ctx.dominant_strategy = strategy
    budget = ctx.budget
    reasons: List[str] = []

    # Unsafe stop (contextual — no confundir LOP mismatch con unsafe humano)
    if ctx.current_phase == OperationalExecutionPhase.UNSAFE:
        return OperationalActionDecision(
            action=OperationalActionKind.STOP,
            dominant_strategy=DominantOperationalStrategy.FALLBACK_SAFE,
            phase=OperationalExecutionPhase.UNSAFE,
            human_message=_NORMAL_STATUS_BY_PHASE[OperationalExecutionPhase.UNSAFE],
            expert_detail=f"unsafe_blockers={ctx.active_blockers}",
            confidence=ctx.unified_confidence,
            block_autonomous_recovery=True,
            allow_entity_first=False,
            allow_navigation=False,
            allow_arl_recovery=False,
            reason_codes=["unsafe_stop"],
        )
    if any("unsafe_hint" in str(b).lower() for b in ctx.active_blockers):
        return OperationalActionDecision(
            action=OperationalActionKind.STOP,
            dominant_strategy=DominantOperationalStrategy.FALLBACK_SAFE,
            phase=OperationalExecutionPhase.UNSAFE,
            human_message=_NORMAL_STATUS_BY_PHASE[OperationalExecutionPhase.UNSAFE],
            expert_detail=f"unsafe_blockers={ctx.active_blockers}",
            confidence=ctx.unified_confidence,
            block_autonomous_recovery=True,
            allow_entity_first=False,
            allow_navigation=False,
            allow_arl_recovery=False,
            reason_codes=["unsafe_stop"],
        )

    # Ambiguity → human
    amb, amb_reasons = _ambiguity_requires_human(mission)
    if amb:
        return OperationalActionDecision(
            action=OperationalActionKind.ASK_HUMAN,
            dominant_strategy=strategy,
            phase=OperationalExecutionPhase.BLOCKED,
            human_message=_NORMAL_STATUS_BY_PHASE[OperationalExecutionPhase.BLOCKED],
            expert_detail=f"ambiguity={amb_reasons}",
            confidence=ctx.unified_confidence,
            block_autonomous_recovery=True,
            allow_entity_first=False,
            reason_codes=["ambiguity_stop"] + amb_reasons[:4],
        )

    # Live perception mismatch
    lp = ctx.live_perception or {}
    if isinstance(lp, dict) and lp.get("safe_to_continue") is False:
        if getattr(settings, "UOOL_REQUIRE_LIVE_PERCEPTION_ALIGN", True):
            if budget.recovery_exhausted:
                return OperationalActionDecision(
                    action=OperationalActionKind.ASK_HUMAN,
                    dominant_strategy=strategy,
                    phase=OperationalExecutionPhase.BLOCKED,
                    human_message="Validando contexto…",
                    expert_detail="live_perception_mismatch_budget_exhausted",
                    confidence=ctx.unified_confidence,
                    block_autonomous_recovery=True,
                    reason_codes=["live_perception_mismatch"],
                )
            reasons.append("live_perception_mismatch")
            return OperationalActionDecision(
                action=OperationalActionKind.RECOVER,
                dominant_strategy=DominantOperationalStrategy.CONTINUITY_RECOVERY,
                phase=OperationalExecutionPhase.RECOVERY,
                human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.RECOVER],
                expert_detail="live_perception_mismatch",
                confidence=ctx.unified_confidence,
                reason_codes=reasons,
            )

    # Budget exhaustion
    if budget.recovery_exhausted and budget.repair_exhausted and budget.navigation_exhausted:
        return OperationalActionDecision(
            action=OperationalActionKind.ASK_HUMAN,
            dominant_strategy=DominantOperationalStrategy.FALLBACK_SAFE,
            phase=OperationalExecutionPhase.BLOCKED,
            human_message=_NORMAL_STATUS_BY_PHASE[OperationalExecutionPhase.BLOCKED],
            expert_detail="recovery_budget_exhausted",
            confidence=ctx.unified_confidence,
            block_autonomous_recovery=True,
            reason_codes=["budget_exhausted"],
        )

    # Navigation storm prevention
    if budget.navigation_exhausted and strategy in (
        DominantOperationalStrategy.COLLECTION_NAVIGATION,
        DominantOperationalStrategy.SEMANTIC_RELOCATION,
    ):
        return OperationalActionDecision(
            action=OperationalActionKind.FALLBACK,
            dominant_strategy=DominantOperationalStrategy.FALLBACK_SAFE,
            phase=OperationalExecutionPhase.REPAIR,
            human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.FALLBACK],
            expert_detail="navigation_budget_exhausted",
            confidence=ctx.unified_confidence,
            allow_navigation=False,
            reason_codes=["navigation_storm_prevented"],
        )

    # Strategy → action mapping (Fase 5 recovery orchestration)
    if strategy == DominantOperationalStrategy.WAIT_DYNAMIC_CONTENT:
        return OperationalActionDecision(
            action=OperationalActionKind.WAIT,
            dominant_strategy=strategy,
            phase=OperationalExecutionPhase.NAVIGATION,
            human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.WAIT],
            expert_detail="wait_dynamic_content",
            confidence=ctx.unified_confidence,
            reason_codes=["wait_dynamic"],
        )

    if strategy == DominantOperationalStrategy.ENTITY_FIRST:
        return OperationalActionDecision(
            action=OperationalActionKind.EXECUTE,
            dominant_strategy=strategy,
            phase=OperationalExecutionPhase.EXECUTION,
            human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.EXECUTE],
            expert_detail="entity_first_dominance",
            confidence=ctx.unified_confidence,
            allow_entity_first=True,
            allow_navigation=not budget.navigation_exhausted,
            reason_codes=["entity_first"],
        )

    if strategy == DominantOperationalStrategy.COLLECTION_NAVIGATION:
        if not budget.navigation_exhausted:
            return OperationalActionDecision(
                action=OperationalActionKind.NAVIGATE,
                dominant_strategy=strategy,
                phase=OperationalExecutionPhase.NAVIGATION,
                human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.NAVIGATE],
                expert_detail="collection_navigation",
                confidence=ctx.unified_confidence,
                allow_navigation=True,
                reason_codes=["collection_nav"],
            )

    if strategy == DominantOperationalStrategy.CONTINUITY_RECOVERY:
        if not budget.recovery_exhausted:
            return OperationalActionDecision(
                action=OperationalActionKind.RECOVER,
                dominant_strategy=strategy,
                phase=OperationalExecutionPhase.RECOVERY,
                human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.RECOVER],
                expert_detail="continuity_recovery",
                confidence=ctx.unified_confidence,
                allow_arl_recovery=True,
                reason_codes=["continuity_recovery"],
            )
        if not budget.repair_exhausted:
            return OperationalActionDecision(
                action=OperationalActionKind.REPAIR,
                dominant_strategy=strategy,
                phase=OperationalExecutionPhase.REPAIR,
                human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.REPAIR],
                expert_detail="continuity_repair_escalation",
                confidence=ctx.unified_confidence,
                reason_codes=["repair_escalation"],
            )

    if strategy == DominantOperationalStrategy.SEMANTIC_RELOCATION:
        if not budget.navigation_exhausted:
            return OperationalActionDecision(
                action=OperationalActionKind.NAVIGATE,
                dominant_strategy=strategy,
                phase=OperationalExecutionPhase.LOCALIZATION,
                human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.NAVIGATE],
                expert_detail="semantic_relocation",
                confidence=ctx.unified_confidence,
                allow_navigation=True,
                reason_codes=["semantic_relocation"],
            )

    if strategy == DominantOperationalStrategy.OPERATIONAL_MEMORY_REUSE:
        return OperationalActionDecision(
            action=OperationalActionKind.EXECUTE,
            dominant_strategy=strategy,
            phase=OperationalExecutionPhase.EXECUTION,
            human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.EXECUTE],
            expert_detail="memory_reuse",
            confidence=ctx.unified_confidence,
            reason_codes=["memory_reuse"],
        )

    if strategy == DominantOperationalStrategy.VALIDATOR_DRIVEN:
        return OperationalActionDecision(
            action=OperationalActionKind.EXECUTE,
            dominant_strategy=strategy,
            phase=OperationalExecutionPhase.VALIDATION,
            human_message=_NORMAL_STATUS_BY_PHASE[OperationalExecutionPhase.VALIDATION],
            expert_detail="validator_driven",
            confidence=ctx.unified_confidence,
            reason_codes=["validator_driven"],
        )

    if strategy == DominantOperationalStrategy.FALLBACK_SAFE:
        return OperationalActionDecision(
            action=OperationalActionKind.FALLBACK,
            dominant_strategy=strategy,
            phase=OperationalExecutionPhase.REPAIR,
            human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.FALLBACK],
            expert_detail="fallback_safe",
            confidence=ctx.unified_confidence,
            reason_codes=["fallback_safe"],
        )

    # Default: execute step via SmartExecutor
    return OperationalActionDecision(
        action=OperationalActionKind.EXECUTE,
        dominant_strategy=DominantOperationalStrategy.EXECUTE_STEP,
        phase=OperationalExecutionPhase.EXECUTION,
        human_message=_NORMAL_STATUS_BY_ACTION[OperationalActionKind.EXECUTE],
        expert_detail="execute_step_default",
        confidence=ctx.unified_confidence,
        allow_entity_first=True,
        allow_navigation=True,
        allow_arl_recovery=True,
        reason_codes=["execute_default"],
    )


def apply_budget_for_action(
    budget: OperationalRecoveryBudget,
    action: OperationalActionKind,
) -> OperationalRecoveryBudget:
    """Incrementa contadores según acción (Fase 6)."""
    b = budget.model_copy(deep=True)
    if action == OperationalActionKind.RECOVER:
        b.recovery_used += 1
    elif action == OperationalActionKind.REPAIR:
        b.repair_used += 1
    elif action == OperationalActionKind.NAVIGATE:
        b.navigation_used += 1
    elif action == OperationalActionKind.WAIT:
        b.navigation_used += 1
    return b


def persist_uool_state(
    mission: Mission,
    ctx: UnifiedOperationalExecutionContext,
    decision: OperationalActionDecision,
) -> None:
    """Persiste contexto y traza en Mission."""
    ctx.updated_at = datetime.now(timezone.utc)
    ctx.orchestration_trace.append({
        "event": "decision",
        "ts": time.time(),
        "action": decision.action.value,
        "strategy": decision.dominant_strategy.value,
        "phase": decision.phase.value,
        "confidence": decision.confidence,
        "reasons": decision.reason_codes[:8],
    })
    try:
        mission.uool_last_context = ctx.model_dump(mode="json")
        mission.uool_orchestration_report = {
            "version": 1,
            "ts": time.time(),
            "current_phase": ctx.current_phase.value,
            "dominant_strategy": ctx.dominant_strategy.value,
            "unified_confidence": ctx.unified_confidence,
            "continuity_confidence": ctx.continuity_confidence,
            "navigation_confidence": ctx.navigation_confidence,
            "execution_truth": ctx.execution_truth,
            "last_action": decision.action.value,
            "budget": ctx.budget.model_dump(mode="json"),
            "active_blockers": ctx.active_blockers[:12],
            "memory_agreement": ctx.memory_signals.memory_agreement,
        }
        trace = list(getattr(mission, "uool_audit_trace", None) or [])
        trace.append({
            "ts": _now_iso(),
            "step_index": ctx.step_index,
            "action": decision.action.value,
            "strategy": decision.dominant_strategy.value,
            "confidence": decision.confidence,
        })
        mission.uool_audit_trace = trace[-64:]
    except Exception as exc:
        log.debug("[UOOL] persist: %s", exc)


def orchestrate_step_preflight(
    mission: Mission,
    settings: Any,
    *,
    step_index: int,
    step: Any,
    state_snapshot: Any = None,
    on_status: Any = None,
    expert_mode: bool = False,
) -> Optional[OperationalActionDecision]:
    """Hook SmartExecutor — consulta UOOL antes de capas autónomas."""
    if not getattr(settings, "UOOL_ENABLED", False):
        return None

    ctx = build_unified_operational_context(
        mission,
        settings,
        step_index=step_index,
        step=step,
        state_snapshot=state_snapshot,
    )
    decision = decide_next_operational_action(ctx, mission, settings)
    try:
        if getattr(settings, "OCRP_ENABLED", False):
            from app.services.runtime.operational_convergence_program import (
                apply_ocrp_to_uool_decision,
                normal_status_for_ocrp,
            )

            decision, conv, hints = apply_ocrp_to_uool_decision(
                decision, mission, settings, ctx,
            )
            ctx.adaptation_state["ocrp_convergence"] = conv.convergence_score
            ctx.adaptation_state["ocrp_hints"] = hints.model_dump(mode="json")
            if not expert_mode:
                decision.human_message = normal_status_for_ocrp(decision.action)
    except Exception as exc:
        log.debug("[UOOL] OCRP apply: %s", exc)
    ctx.budget = apply_budget_for_action(ctx.budget, decision.action)
    persist_uool_state(mission, ctx, decision)

    msg = decision.expert_detail if expert_mode else decision.human_message
    if on_status and msg:
        try:
            on_status(msg)
        except Exception:
            pass

    return decision


def prepare_uool_for_execution(mission: Mission, settings: Any) -> Dict[str, Any]:
    """Preflight al armar misión en smart_runner_bridge."""
    if not getattr(settings, "UOOL_ENABLED", False):
        return {"skipped": True, "reason": "UOOL_ENABLED=false"}

    audit: Dict[str, Any] = {"ok": True}
    try:
        ctx = build_unified_operational_context(mission, settings, step_index=0)
        decision = decide_next_operational_action(ctx, mission, settings)
        persist_uool_state(mission, ctx, decision)
        audit["preflight_action"] = decision.action.value
        audit["preflight_strategy"] = decision.dominant_strategy.value
        audit["unified_confidence"] = ctx.unified_confidence
        if decision.action == OperationalActionKind.STOP:
            audit["ok"] = False
            audit["blocked"] = "unsafe"
        elif decision.action == OperationalActionKind.ASK_HUMAN:
            audit["ok"] = False
            audit["blocked"] = "human_required"
    except Exception as exc:
        audit["ok"] = False
        audit["error"] = str(exc)
        log.debug("[UOOL] prepare: %s", exc)
    return audit


def normal_status_message(
    decision: OperationalActionDecision,
    *,
    expert_mode: bool = False,
) -> str:
    """Fase 11 — copy humano vs experto."""
    if expert_mode:
        return (
            f"[UOOL] {decision.action.value} · "
            f"strategy={decision.dominant_strategy.value} · "
            f"phase={decision.phase.value} · "
            f"conf={decision.confidence:.2f} · "
            f"{decision.expert_detail}"
        )
    return decision.human_message or _NORMAL_STATUS_BY_ACTION.get(
        decision.action, "Ejecutando…",
    )


def expert_audit_lines(mission: Mission) -> List[str]:
    """Líneas para Mission Review experto."""
    lines: List[str] = []
    rep = getattr(mission, "uool_orchestration_report", None) or {}
    if not isinstance(rep, dict) or not rep:
        return lines
    lines.append(
        f"UOOL phase={rep.get('current_phase')} "
        f"strategy={rep.get('dominant_strategy')} "
        f"conf={rep.get('unified_confidence')}",
    )
    if rep.get("continuity_confidence") is not None:
        lines.append(f"  continuity={rep.get('continuity_confidence')}")
    if rep.get("memory_agreement") is not None:
        lines.append(f"  memory_agreement={rep.get('memory_agreement')}")
    blockers = rep.get("active_blockers") or []
    if blockers:
        lines.append(f"  blockers={blockers[:6]}")
    budget = rep.get("budget") or {}
    if budget:
        lines.append(
            f"  budget rec={budget.get('recovery_used')}/{budget.get('recovery_budget')} "
            f"nav={budget.get('navigation_used')}/{budget.get('navigation_budget')}",
        )
    trace = getattr(mission, "uool_audit_trace", None) or []
    if trace:
        last = trace[-1]
        if isinstance(last, dict):
            lines.append(
                f"  last={last.get('action')}@{last.get('step_index')} "
                f"strategy={last.get('strategy')}",
            )
    return lines


__all__ = [
    "apply_budget_for_action",
    "build_unified_operational_context",
    "compute_unified_operational_confidence",
    "decide_next_operational_action",
    "evaluate_operational_continuity_uool",
    "expert_audit_lines",
    "merge_cross_layer_memory_signals",
    "normal_status_message",
    "orchestrate_step_preflight",
    "persist_uool_state",
    "prepare_uool_for_execution",
    "resolve_dominant_operational_strategy",
]
