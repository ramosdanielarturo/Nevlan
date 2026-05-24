"""Goal-Oriented Operational Runtime (GOOR) — Fase 1 sombra.

Piensa en *objetivos operacionales* y transiciones válidas declaradas — no ejecuta
SmartExecutor ni replay procedural; sólo observa, rankea y audita dentro del modelo
existente ORG/OCR/TEL/OMPC/SLL.
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from app.contracts.goal_oriented_runtime import (
    OperationalDecisionContext,
    OperationalGoalExecutionSnapshot,
    OperationalGoalProgress,
    OperationalGoalRecovery,
    OperationalGoalState,
    OperationalGoalTransition,
)
from app.contracts.mission import Mission
from app.contracts.operational_runtime_graph import OperationalRuntimeGoal, OperationalRuntimeGraph
from app.core.logger import log
from app.services.runtime import operational_continuity_runtime as ocr
from app.services.runtime import operational_runtime_graph as orgmod
from app.services.runtime.operational_memory import (
    build_canonical_sequence_from_mission,
    ompc_hints_for_mission_preview,
)


def _settings_goor_enabled(settings_obj: Optional[Any]) -> bool:
    if settings_obj is None:
        try:
            from app.core.config import get_settings

            settings_obj = get_settings()
        except Exception:
            return False
    return bool(getattr(settings_obj, "GOOR_SHADOW_ENABLED", True))


def _truth_agg_ambiguity(truth_blocks: Sequence[Any]) -> Tuple[float, float]:
    vals: List[float] = []
    confs: List[float] = []
    for tb in truth_blocks:
        try:
            vals.append(float(getattr(tb, "ambiguity_score", 0.5)))
        except Exception:
            vals.append(0.5)
        try:
            confs.append(float(getattr(tb, "operational_confidence", 0.55)))
        except Exception:
            confs.append(0.55)
    if not vals:
        return 0.5, 0.55
    return round(sum(vals) / len(vals), 4), round(sum(confs) / len(confs), 4)


def _sep_step_kind_sequence(mission: Mission) -> List[str]:
    blob = getattr(mission, "semantic_execution_plan", None) or {}
    if not isinstance(blob, dict):
        return []
    return [str(s.get("type") or "").strip() for s in blob.get("steps") or [] if isinstance(s, dict)]


def _arl_recovery_pressure(mission: Mission) -> float:
    aud = getattr(mission, "adaptive_runtime_audit", None) or {}
    ev = list(aud.get("events") or []) if isinstance(aud, dict) else []
    if not ev:
        return 0.0
    rec = 0
    bad = 0
    for e in ev[-80:]:
        if not isinstance(e, dict):
            continue
        d = e.get("decision") or {}
        if isinstance(d, dict):
            if str(d.get("decision") or "").upper().find("FALLBACK") >= 0:
                bad += 1
                rec += 1
            elif str(e.get("type") or "") in {"recovery", "relocation", "continuation_loss"}:
                rec += 1
    frac = round(rec / max(1, len(ev)), 4)
    if bad >= 6:
        frac = min(1.0, frac + 0.12)
    return frac


def _validator_drift_from_truths(truth_blocks: Sequence[Any]) -> float:
    if len(truth_blocks) < 2:
        return 0.0
    prev: Optional[Set[str]] = None
    drift_accum = 0.0
    for tb in truth_blocks:
        ve = getattr(tb, "validator_expectations", None) or []
        cur = {str(v) for v in ve[:12]}
        if prev is None:
            prev = cur
            continue
        overlap = len(cur & prev) / max(1, len(cur | prev))
        drift_accum += 1.0 - overlap
        prev = cur
    return round(drift_accum / max(1, len(truth_blocks) - 1), 4)


def _execution_truth_echo(mission: Mission) -> Dict[str, Any]:
    out: Dict[str, Any] = {"confirmed": False, "signals": []}
    try:
        from app.services.missions.execution_truth_engine import is_execution_truth_confirmed

        out["confirmed"] = bool(is_execution_truth_confirmed(mission))
    except Exception:
        out["confirmed"] = False
    vb = getattr(mission, "validation_rules", None) or {}
    if isinstance(vb, dict) and vb:
        out["signals"].append(f"validation_rules_keys:{len(vb)}")
    return out


def _sll_echo_for_capabilities(capabilities: List[str]) -> Dict[str, Any]:
    caps = [str(c) for c in capabilities[:12] if c]
    ranked: Dict[str, List[str]] = {}
    try:
        from app.services.runtime.strategy_learning_layer import StrategyContext, rank_strategies_with_learning

        ctx = StrategyContext(step_kind=str(caps[0] if caps else "*"))
        for cap in caps or ["generic_route"]:
            r, _expl = rank_strategies_with_learning(
                [cap, f"{cap}:shadow_hint"],
                ctx,
                learning=None,
            )
            ranked[cap] = r[:4]
        return {"capabilities": caps, "ranked_hints": ranked, "sll_active": True}
    except Exception as exc:
        return {"capabilities": caps, "error": str(exc), "sll_active": False}


def build_operational_goal_states(graph: OperationalRuntimeGraph) -> List[OperationalGoalState]:
    rows: List[OperationalGoalState] = []
    for n in graph.nodes:
        rows.append(
            OperationalGoalState(
                state_key=n.operational_state,
                sourced_from_truth_type=str(n.truth_type or ""),
                sourced_from_truth_id=str(n.truth_block_id or ""),
                validators_expected=list(n.expected_validators or [])[:16],
            ),
        )
    return rows


def build_operational_decision_context(
    mission: Mission,
    graph: OperationalRuntimeGraph,
    *,
    ocr_score: Optional[float] = None,
    current_state_hint: Optional[str] = None,
) -> OperationalDecisionContext:
    amb_agg, _conf_agg = _truth_agg_ambiguity(list(mission.truth_blocks or []))
    cont = ocr_score
    if cont is None:
        cont, _ = ocr.evaluate_operational_continuity(mission, graph)

    inferred = ""
    seq = [n.operational_state for n in graph.nodes]
    if current_state_hint:
        inferred = current_state_hint
    elif seq:
        inferred = seq[-1]

    om: Optional[Dict[str, Any]] = ompc_hints_for_mission_preview(mission)
    if om is None:
        cq = build_canonical_sequence_from_mission(mission, step_kinds_executed=[])
        if cq:
            om = ompc_hints_for_mission_preview(mission)

    org_m = dict(graph.metrics or {})

    caps: List[str] = []
    if inferred:
        for t in graph.transitions:
            if t.from_operational_state == inferred:
                caps.append(str(t.transition_id))

    sep_kinds = _sep_step_kind_sequence(mission)
    sll = _sll_echo_for_capabilities(sep_kinds[:8] + caps[:4])

    ompc_blob: Dict[str, Any]
    ompc_blob = om if isinstance(om, dict) else {}

    raw_lop = getattr(mission, "lop_last_snapshot", None)
    lop_echo: Dict[str, Any] = {}
    if isinstance(raw_lop, dict) and raw_lop:
        lop_echo = {
            "surface_type": raw_lop.get("surface_type"),
            "operational_state_guess": raw_lop.get("operational_state_guess"),
            "recommended_runtime_action": raw_lop.get("recommended_runtime_action"),
            "safe_to_continue": raw_lop.get("safe_to_continue"),
            "confidence": raw_lop.get("confidence"),
            "matched_goal_ids": raw_lop.get("matched_goal_ids"),
            "modal_detected": raw_lop.get("modal_detected"),
        }

    try:
        from app.services.runtime.universal_operational_element_identity import (
            goor_element_type_echo,
        )

        _uoei_echo = goor_element_type_echo(mission)
    except Exception:
        _uoei_echo = {}

    return OperationalDecisionContext(
        execution_truth_echo={
            **_execution_truth_echo(mission),
            "uoei_element_types": _uoei_echo.get("element_types", []),
            "uoei_mean_stability": _uoei_echo.get("mean_stability", 0.0),
        },
        ocr_continuity_score=float(cont or 0.0),
        org_metric_echo=dict(org_m),
        ompc_hints=ompc_blob,
        sll_echo=sll,
        arl_recovery_pressure=_arl_recovery_pressure(mission),
        validator_history_drift=_validator_drift_from_truths(list(mission.truth_blocks or [])),
        ambiguity_score_aggregate=amb_agg,
        replay_pressure=float(org_m.get("replay_dependency_score", 0.0) or 0.0),
        inferred_current_operational_state=inferred,
        live_operational_perception_echo=lop_echo,
        lop_live_snapshot=dict(raw_lop) if isinstance(raw_lop, dict) and raw_lop else {},
    )


def _goal_truth_blocks(mission: Mission, goal: OperationalRuntimeGoal) -> List[Any]:
    tid = list(goal.originating_truth_ids or [])[:48]
    if not tid:
        return list(mission.truth_blocks or [])
    out = []
    for tb in mission.truth_blocks or []:
        if getattr(tb, "truth_id", None) and str(tb.truth_id) in set(tid):
            out.append(tb)
    return out or list(mission.truth_blocks or [])


def _validator_alignment(nodes_subset: Sequence[Any], mission: Mission) -> float:
    if not nodes_subset:
        return 0.45
    ve_counts = 0
    for n in nodes_subset:
        vv = getattr(n, "expected_validators", None) or []
        ve_counts += len(vv)
    raw = ve_counts / max(8.0, float(len(nodes_subset)) * 3.5)
    return round(max(0.0, min(1.0, raw)), 4)


def evaluate_goal_progress(
    mission: Mission,
    graph: OperationalRuntimeGraph,
    *,
    goal_id: Optional[str] = None,
    ocr_score: Optional[float] = None,
) -> OperationalGoalProgress:
    """Calcula vectores GOOR de progreso hacia un goal declarativo (sombra)."""

    goals = graph.goals
    goal = next((g for g in goals if g.goal_id == goal_id), None) if goals and goal_id else None
    if not goal:
        goal = goals[0] if goals else None

    if goal is None:
        return OperationalGoalProgress(
            goal_id="",
            goal_headline="",
            ambiguity=1.0,
            recovery_pressure=1.0,
        )

    cont = ocr_score
    if cont is None:
        cont, _ = ocr.evaluate_operational_continuity(mission, graph)

    sim = ocr.execute_operational_goal_shadow(mission, graph, goal_id=goal.goal_id)
    tbs = _goal_truth_blocks(mission, goal)
    amb_agg, conf_agg = _truth_agg_ambiguity(tbs)

    nodes_for_goal: List[Any] = []
    idset = set(goal.originating_truth_ids or []) if goal.originating_truth_ids else None
    for n in graph.nodes:
        if idset:
            if n.truth_block_id in idset:
                nodes_for_goal.append(n)
        else:
            nodes_for_goal.append(n)

    val_align = _validator_alignment(nodes_for_goal if nodes_for_goal else graph.nodes, mission)
    arl_p = _arl_recovery_pressure(mission)
    continuity = round(float(cont or 0.0), 4)

    rec_p = round(
        0.52 * arl_p + 0.28 * amb_agg + 0.20 * float(graph.metrics.get("replay_dependency_score") or 0.0),
        4,
    )

    prog = float(sim.progress_ratio or 0.0) if sim else 0.0
    op_conf = round(
        0.42 * prog
        + 0.24 * continuity
        + 0.18 * float(conf_agg)
        + 0.10 * float(val_align)
        + (0.06 if _execution_truth_echo(mission)["confirmed"] else 0.0),
        4,
    )
    inferred_current = []
    if graph.nodes:
        inferred_current.append(graph.nodes[-1].operational_state)

    return OperationalGoalProgress(
        goal_id=goal.goal_id,
        goal_headline=goal.headline,
        progress_ratio=round(min(1.0, prog), 4),
        continuity_strength=continuity,
        ambiguity=amb_agg,
        recovery_pressure=min(1.0, rec_p),
        validator_alignment=val_align,
        operational_confidence=op_conf,
        desired_terminal_states=list(sim.desired_terminal_states if sim else []),
        inferred_current_states=inferred_current[:4],
    )


def _surface_variant_label(classes: Sequence[str]) -> str:
    if not classes:
        return "neutral_surface_transition"
    return "_or_".join(sorted({str(x) for x in classes}))[:220]


def _ompc_probability_for_destination(
    dest_state_substr: str,
    *,
    ompc_hints: Dict[str, Any],
) -> float:
    preds = ompc_hints.get("predicted_next") if isinstance(ompc_hints, dict) else None
    if not isinstance(preds, list):
        return 0.0
    needles = dest_state_substr.lower().strip().replace("_", "")
    bonus = 0.0
    for p in preds:
        if not isinstance(p, dict):
            continue
        tok = str(p.get("next_token") or "").lower().replace("_", "")
        prob = float(p.get("probability") or 0.0)
        if needles and needles in tok:
            bonus += prob
        elif tok.startswith("sep:"):
            bonus += prob * 0.25
    return round(min(1.0, bonus), 4)


def _transition_distance_layers(
    graph: OperationalRuntimeGraph,
    *,
    terminals: Set[str],
) -> Dict[str, int]:
    dist: Dict[str, int] = {t: 0 for t in terminals}
    q = deque(terminals)
    while q:
        cur = q.popleft()
        d0 = dist[cur]
        for tr in graph.transitions:
            if tr.to_operational_state != cur:
                continue
            prev = tr.from_operational_state
            if prev not in dist:
                dist[prev] = d0 + 1
                q.append(prev)
    return dist


def find_candidate_goal_transitions(
    mission: Mission,
    graph: OperationalRuntimeGraph,
    *,
    goal_id: Optional[str] = None,
    from_state: Optional[str] = None,
    limit: int = 24,
) -> List[OperationalGoalTransition]:
    """Transiciones válidas declaradas multi-camino orientadas al goal."""

    goals = graph.goals
    goal = next((g for g in goals if g.goal_id == goal_id), None) if goals and goal_id else None
    if not goal:
        goal = goals[0] if goals else None

    sim = (
        ocr.execute_operational_goal_shadow(mission, graph, goal_id=goal.goal_id)
        if goal
        else ocr.execute_operational_goal_shadow(mission, graph)
    )
    terminals = set(sim.desired_terminal_states if sim and sim.desired_terminal_states else [])

    inferred = from_state or (graph.nodes[-1].operational_state if graph.nodes else "")
    if not inferred:
        return []

    dist_rev = _transition_distance_layers(graph, terminals=terminals)

    ompc_hints: Dict[str, Any] = {}
    om = ompc_hints_for_mission_preview(mission)
    if isinstance(om, dict):
        ompc_hints = om

    base = ocr.find_valid_operational_transition(
        graph, inferred, toward_state=list(terminals)[0] if terminals else None
    )
    cand: List[OperationalGoalTransition] = []
    sep_kinds = _sep_step_kind_sequence(mission)
    dup_guard: Set[str] = set()

    for att in base[:limit]:
        key = (att.transition_id or att.from_operational_state + "→" + att.to_operational_state)
        if key in dup_guard:
            continue
        dup_guard.add(str(key))

        org_match = None
        for t in graph.transitions:
            if t.transition_id == att.transition_id or (
                t.from_operational_state == att.from_operational_state
                and t.to_operational_state == att.to_operational_state
            ):
                org_match = t
                break

        legitimate = list(org_match.legitimate_surface_classes or []) if org_match else []

        hops = dist_rev.get(att.to_operational_state)
        if hops is None and terminals:
            hops = None

        ompc_prob = _ompc_probability_for_destination(att.to_operational_state, ompc_hints=ompc_hints)
        tier_dom = ompc_hints.get("matched", False)

        legitimacy = round(float(att.legitimacy_score), 4)
        proximity = round(1.0 / float(1 + (hops or 12)), 4) if hops is not None else 0.28
        semantic_surface_diversity = min(1.0, len(legitimate) / 5.5)

        tier_boost = (0.12 if tier_dom else 0.05) + 0.12 * ompc_prob

        preliminary = round(
            0.42 * legitimacy + 0.34 * proximity + 0.14 * semantic_surface_diversity + tier_boost,
            4,
        )

        cand.append(
            OperationalGoalTransition(
                transition_id=str(att.transition_id or ""),
                variant_label=_surface_variant_label(legitimate),
                from_operational_state=att.from_operational_state,
                to_operational_state=att.to_operational_state,
                promising_score=preliminary,
                distance_to_goal_terminals=hops,
                legitimacy_from_org=legitimacy,
                ompc_alignment=ompc_prob,
                sll_rank_prior=round(min(1.0, len({k for k in sep_kinds if k}) / 8.0), 4),
                legitimate_surface_classes=legitimate,
                expected_validator_keys=list(org_match.expected_validator_keys or []) if org_match else [],
                rationale=str(att.rationale or ((org_match.description if org_match else "") or "")),
                declares_dynamic_path=len(set(legitimate)) != 1,
            ),
        )

    return cand


def rank_goal_transitions(
    candidates: Sequence[OperationalGoalTransition],
    *,
    ctx: OperationalDecisionContext,
) -> List[OperationalGoalTransition]:
    """Orden estable: score GOOR penalizado por drift ARL / validadores declarados."""

    drift_pen_avg = ctx.validator_history_drift * 0.18 + ctx.arl_recovery_pressure * 0.12

    def _final(ct: OperationalGoalTransition) -> float:
        raw = (
            ct.promising_score
            + 0.10 * ct.ompc_alignment
            + 0.08 * ct.sll_rank_prior
            + 0.06 * ct.legitimacy_from_org
            - drift_pen_avg
        )
        return round(max(0.0, min(1.0, raw)), 6)

    def _tie(ct: OperationalGoalTransition) -> Tuple[float, float, float, float, float, float]:
        d = ct.distance_to_goal_terminals if ct.distance_to_goal_terminals is not None else 999
        return (
            -_final(ct),
            float(d),
            -float(ct.ompc_alignment),
            -float(ct.promising_score),
            len(ct.legitimate_surface_classes),
            float(abs(hash(ct.transition_id))),
        )

    return sorted(list(candidates), key=_tie)


def attempt_goal_transition_shadow(
    *,
    mission: Mission,
    graph: OperationalRuntimeGraph,
    from_state: str,
    to_state: str,
    goal_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Evalúa un salto declarativo sin ejecutar runtime real ni mutar la misión."""

    baseline = evaluate_goal_progress(mission, graph, goal_id=goal_id)
    att = ocr.attempt_operational_transition(graph, from_state, to_state)

    after_score = float(baseline.progress_ratio or 0.0)
    if att.declared_in_org and att.would_validate_shadow:
        after_score = min(1.0, after_score + 0.05)
    elif att.declared_in_org:
        after_score = min(1.0, after_score + 0.02)

    drift_after = OperationalGoalProgress(
        **{
            **baseline.model_dump(),
            "progress_ratio": round(min(1.0, max(0.0, after_score)), 4),
        }
    )

    return {
        "ok": bool(att.declared_in_org),
        "from_operational_state": from_state,
        "to_operational_state": to_state,
        "ocr_attempt_shadow": att.model_dump(mode="json"),
        "progress_before": baseline.model_dump(mode="json"),
        "progress_after_simulated": drift_after.model_dump(mode="json"),
        "note": "Sólo simulación declarativa sobre ORG/OCR; no muta SEP, SmartExecutor ni replay.",
    }


def attach_context_drift_markers(cont_states: Sequence[Any]) -> List[str]:
    """Eco compacto cuando OCR marca invalidación bajo truth declarado."""

    drift: List[str] = []
    for st in list(cont_states)[-24:]:
        try:
            if not bool(getattr(st, "validity_under_declared_truth", True)):
                drift.append(str(getattr(st, "operational_state_label", "") or "").strip())
        except Exception:
            continue
    return [d for d in drift if d]


def evaluate_goal_continuity(
    mission: Mission,
    graph: OperationalRuntimeGraph,
    *,
    progress: Optional[OperationalGoalProgress] = None,
) -> Tuple[float, List[str]]:
    """Persistencia declarativa de objetivos vivos combinando OCR + progreso simulado."""

    cont_raw, cont_states = ocr.evaluate_operational_continuity(mission, graph)
    prog = progress or evaluate_goal_progress(mission, graph)

    notes = [
        f"goals_declared:{len(graph.goals)}",
        f"truth_blocks:{len(mission.truth_blocks or [])}",
    ]

    divergence = attach_context_drift_markers(cont_states)
    cont = float(cont_raw or 0.0)
    validator_term = float(prog.validator_alignment or 0.0)
    progress_term = float(prog.progress_ratio or 0.0)

    continuity = round(cont * (0.55 + 0.25 * validator_term + 0.20 * progress_term), 4)
    continuity = round(max(0.0, min(1.0, continuity)), 4)

    if divergence:
        continuity = round(continuity * (1.0 - min(0.18, len(divergence) / 140.0)), 4)
        notes.append(f"context_drift_markers:{len(divergence)}")

    notes.append(f"validator_alignment_hint:{prog.validator_alignment}")
    return continuity, notes


def detect_goal_stall(
    mission: Mission,
    graph: OperationalRuntimeGraph,
) -> List[str]:
    stalls: List[str] = []

    kinds = _sep_step_kind_sequence(mission)
    if len(kinds) >= 4:
        c = Counter(kinds[-8:])
        for k, n in c.most_common(2):
            if k and n >= 4:
                stalls.append(f"procedural_micro_loop_recent:{k}")
                break

    if float(graph.metrics.get("replay_dependency_score") or 0.0) > 0.78:
        stalls.append("replay_pressure_excessive")

    if _arl_recovery_pressure(mission) >= 0.62:
        stalls.append("arl_recovery_pressure_high")

    if _validator_drift_from_truths(list(mission.truth_blocks or [])) > 0.55:
        stalls.append("validator_drift_pressure")

    re = graph.recovery_edges
    arl_fb = getattr(mission, "adaptive_runtime_audit", None) or {}
    ev_cnt = len((arl_fb.get("events") or []) if isinstance(arl_fb, dict) else []) or 0
    if re and len(re) <= 2 and ev_cnt >= 28:
        stalls.append("recovery_exhaustion_suspected")

    return stalls


def find_goal_recovery(
    mission: Mission,
    graph: OperationalRuntimeGraph,
    *,
    goal_id: Optional[str] = None,
    limit: int = 28,
) -> List[OperationalGoalRecovery]:
    goals = graph.goals
    goal = next((g for g in goals if g.goal_id == goal_id), None) if goals and goal_id else None
    if not goal:
        goal = goals[0] if goals else None

    terminals: Set[str] = set()
    sim = (
        ocr.execute_operational_goal_shadow(mission, graph, goal_id=goal.goal_id)
        if goal
        else ocr.execute_operational_goal_shadow(mission, graph)
    )
    if sim and sim.desired_terminal_states:
        terminals = set(sim.desired_terminal_states)

    recs = ocr.find_operational_recovery(graph, symptom_substr="", limit=72)
    tier_rank = {"semantic": 0, "structural": 1, "procedural": 2}

    scored: List[OperationalGoalRecovery] = []
    semantic_words = (
        "navigate",
        "restore",
        "relocat",
        "rebound",
        "re-query",
        "semantic",
        "context",
        "focus",
        "reloc",
    )

    for r in recs[:limit * 3]:
        toward_goal = (r.target_state in terminals) if terminals else True
        gscore = 0.88 if toward_goal and r.recovery_tier == "semantic" else 0.62
        if not toward_goal:
            gscore *= 0.45
        hay = " ".join(
            [
                str(r.symptom_state or ""),
                str(r.target_state or ""),
                str(r.description or ""),
                str(r.recovery_class or ""),
            ]
        ).lower()
        bonus = 0.05 * sum(1 for w in semantic_words if w in hay)
        gscore = min(1.0, gscore + bonus)
        scored.append(
            OperationalGoalRecovery(
                recovery_edge_id=r.recovery_edge_id,
                goal_id_hint=goal.goal_id if goal else "",
                symptom_state=r.symptom_state,
                target_state=r.target_state,
                recovery_class=r.recovery_class,
                tier=r.recovery_tier,
                description=r.description,
                goal_orientation_score=round(gscore, 4),
                evidence_sources=list(r.evidence_sources or []),
            ),
        )

    scored.sort(key=lambda x: (-x.goal_orientation_score, tier_rank.get(x.tier, 9)))
    return scored[:limit]


def compare_goor_vs_sep_runtime(
    graph: OperationalRuntimeGraph,
    mission: Mission,
    *,
    goal_id: Optional[str] = None,
) -> Dict[str, Any]:
    baseline = ocr.compare_ocr_vs_sep_runtime(graph, mission)

    prog = evaluate_goal_progress(mission, graph, goal_id=goal_id)
    gx = ocr.execute_operational_goal_shadow(mission, graph, goal_id=goal_id or None)

    sep_len = max(1, len(_sep_step_kind_sequence(mission)))
    shadow_path = len(list(gx.simulation_path_states or [])) if gx else len(graph.nodes)
    shadow_depth = shadow_path if shadow_path else len(graph.nodes)
    procedural_avoidables = max(0, sep_len - min(sep_len, shadow_depth))

    replay_dep = float(baseline.get("replay_dependence_estimate") or 0.0)
    resilience_echo = baseline.get("org_vs_sep_projection", {}) if isinstance(baseline, dict) else {}

    procedural_resilience_estimate = round(
        0.5 * prog.operational_confidence + 0.5 * (1.0 - replay_dep),
        4,
    )

    baseline["goor_projection"] = {
        "goal_headline_echo": gx.headline if gx else "",
        "desired_terminal_echo": gx.desired_terminal_states if gx else [],
        "estimated_procedural_step_delta": procedural_avoidables,
        "operational_goal_progress_ratio": prog.progress_ratio,
        "replay_pressure_sep_echo": resilience_echo,
        "procedural_resilience_estimate": procedural_resilience_estimate,
    }
    baseline["replay_dependency_reduction_hypothesis"] = round(max(0.0, 1.0 - replay_dep), 4)
    return baseline


def _compute_goor_extended_metrics(
    *,
    progresses: Sequence[OperationalGoalProgress],
    ranked_transitions: Sequence[OperationalGoalTransition],
    recoveries: Sequence[OperationalGoalRecovery],
    continuity_rate: float,
    replay_pressure: float,
    decision_std: float,
) -> Dict[str, float]:
    pr = [float(p.progress_ratio or 0.0) for p in progresses if p.progress_ratio is not None]
    completion_strength = round(sum(pr) / max(1, len(pr)), 4) if pr else 0.0

    top_tx = ranked_transitions[: min(12, len(ranked_transitions))]
    tx_quality = (
        round(sum(t.promising_score for t in top_tx) / max(1, len(top_tx)), 4) if top_tx else 0.0
    )

    recv = recoveries[: min(10, len(recoveries))]
    recovery_adapt = round(sum(r.goal_orientation_score for r in recv) / max(1, len(recv)), 4) if recv else 0.0

    to_states = list({t.to_operational_state for t in ranked_transitions[:32]})
    transition_diversity = round(len(to_states) / max(12.0, float(len(ranked_transitions))), 4)
    recovery_diversity = round(
        len({r.recovery_class for r in recoveries[:20]}) / max(6.0, float(len(recoveries))),
        4,
    )

    return {
        "operational_goal_completion_strength": completion_strength,
        "dynamic_transition_quality": tx_quality,
        "recovery_adaptability": recovery_adapt,
        "replay_dependency_reduction": round(max(0.0, min(1.0, 1.0 - replay_pressure)), 4),
        "runtime_goal_autonomy": round(0.6 * completion_strength + 0.4 * (1.0 - replay_pressure), 4),
        "continuity_preservation_rate": round(continuity_rate, 4),
        "transition_diversity": min(1.0, transition_diversity),
        "recovery_diversity": min(1.0, recovery_diversity),
        "operational_decision_confidence": round(decision_std, 4),
    }


def build_operational_goal_execution_snapshot(
    mission: Mission,
    *,
    graph: Optional[OperationalRuntimeGraph] = None,
    settings: Optional[Any] = None,
) -> OperationalGoalExecutionSnapshot:
    if not _settings_goor_enabled(settings):
        snap = OperationalGoalExecutionSnapshot(schema_version="goor_shadow_disabled")
        snap.metrics = {"shadow_disabled": 1.0}
        snap.stall_signals = ["GOOR_SHADOW_DISABLED"]
        return snap

    g = graph or orgmod.build_operational_runtime_graph(mission)

    cont_vec, continuity_notes = evaluate_goal_continuity(mission, g)
    progresses: List[OperationalGoalProgress] = []
    gid0: Optional[str] = None

    goal_list = g.goals or []
    if goal_list:
        for cg in goal_list[:6]:
            progresses.append(evaluate_goal_progress(mission, g, goal_id=cg.goal_id))
            if gid0 is None:
                gid0 = cg.goal_id
    else:
        progresses.append(evaluate_goal_progress(mission, g))

    mean_conf = round(
        sum(p.operational_confidence for p in progresses) / max(1, len(progresses)),
        4,
    )
    ctx = build_operational_decision_context(mission, g, ocr_score=mean_conf)

    cand_raw = find_candidate_goal_transitions(mission, g, goal_id=gid0)
    ranked = rank_goal_transitions(cand_raw, ctx=ctx)
    chosen = ranked[0] if ranked else None

    stalls = detect_goal_stall(mission, g)
    stalls.extend(
        [
            f"recovery_pressure_peak:{p.goal_id or p.goal_headline}"
            for p in progresses
            if p.recovery_pressure > 0.82
        ],
    )

    recovs = find_goal_recovery(mission, g, goal_id=gid0, limit=36)

    comparison = compare_goor_vs_sep_runtime(g, mission, goal_id=gid0)
    autonomy = round(float(comparison.get("replay_dependency_reduction_hypothesis") or 0.0), 4)
    replay_estimate = ctx.replay_pressure

    metrics = _compute_goor_extended_metrics(
        progresses=list(progresses),
        ranked_transitions=list(ranked),
        recoveries=list(recovs),
        continuity_rate=cont_vec,
        replay_pressure=replay_estimate,
        decision_std=float(mean_conf),
    )

    return OperationalGoalExecutionSnapshot(
        operational_decision_context=ctx,
        goal_states=build_operational_goal_states(g),
        active_goal_progress=progresses[:8],
        ranked_candidate_transitions=ranked[:16],
        chosen_transition=chosen,
        goal_recoveries=recovs[:20],
        stall_signals=stalls[:16],
        continuity_notes=continuity_notes[:24],
        metrics=metrics,
        goor_vs_sep_comparison=comparison,
        replay_dependency_estimate=replay_estimate,
        operational_autonomy_estimate=autonomy,
        build_sources_echo=list(getattr(g, "build_sources_used", None) or []),
    )


def attach_goor_audit(mission: Mission, audit_blob: Dict[str, Any]) -> None:
    mission.goal_oriented_runtime_audit = dict(audit_blob)


def refresh_goor_shadow(
    mission: Mission,
    settings: Optional[Any] = None,
    *,
    graph: Optional[OperationalRuntimeGraph] = None,
) -> OperationalGoalExecutionSnapshot:
    def _snapshot_history_append(sn: OperationalGoalExecutionSnapshot) -> None:
        mh = getattr(mission, "operational_goal_snapshots", None)
        mh_list = mh if isinstance(mh, list) else []
        mh_list.append(sn.model_dump(mode="json"))
        mission.operational_goal_snapshots = mh_list[-48:]

    if not getattr(mission, "goor_shadow_mode", False):
        blob = {"ok": False, "reason": "goor_shadow_mode_false"}
        attach_goor_audit(mission, blob)
        snap = OperationalGoalExecutionSnapshot(stall_signals=["GOOR_SHADOW_MODE_DISABLED"])
        return snap

    if not _settings_goor_enabled(settings):
        snap = OperationalGoalExecutionSnapshot(stall_signals=["GOOR_SHADOW_DISABLED"])
        attach_goor_audit(mission, {"ok": False, "reason": "GOOR_SHADOW_DISABLED"})
        _snapshot_history_append(snap)
        return snap

    try:
        snap = build_operational_goal_execution_snapshot(mission, graph=graph, settings=settings)
        _snapshot_history_append(snap)

        attach_goor_audit(
            mission,
            {
                "ok": True,
                "chosen_transition_to": getattr(snap.chosen_transition, "to_operational_state", None)
                if snap.chosen_transition
                else None,
                "stall_count": len(snap.stall_signals),
                "metrics": snap.metrics,
                "goor_vs_sep": snap.goor_vs_sep_comparison,
            },
        )
        log.debug(
            "[GOOR] autonomy=%s replay_dep=%s transitions=%s",
            snap.operational_autonomy_estimate,
            snap.replay_dependency_estimate,
            len(snap.ranked_candidate_transitions),
        )
        return snap
    except Exception as exc:
        log.debug("[GOOR] refresh failed: %s", exc)
        attach_goor_audit(mission, {"ok": False, "reason": str(exc)})
        snap = OperationalGoalExecutionSnapshot(stall_signals=[f"goor_fault:{exc}"])
        _snapshot_history_append(snap)
        return snap


__all__ = [
    "attach_context_drift_markers",
    "attach_goor_audit",
    "attempt_goal_transition_shadow",
    "build_operational_decision_context",
    "build_operational_goal_execution_snapshot",
    "build_operational_goal_states",
    "compare_goor_vs_sep_runtime",
    "detect_goal_stall",
    "evaluate_goal_continuity",
    "evaluate_goal_progress",
    "find_candidate_goal_transitions",
    "find_goal_recovery",
    "rank_goal_transitions",
    "refresh_goor_shadow",
]
