"""Operational Continuity Runtime (OCR) — Fase sombra.

Evalúa continuidad operacional state/goal-driven derivada de ORG, truth_blocks,
TEL, métricas ORG existentes y execution truth — **sin** ejecutar SEP, SmartExecutor
ni replay procedural.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import Mission
from app.contracts.operational_continuity import (
    OperationalContinuitySnapshot,
    OperationalContinuityState,
    OperationalGoalExecution,
    OperationalRecoveryAttempt,
    OperationalTransitionAttempt,
)
from app.contracts.operational_runtime_graph import OperationalRuntimeGoal, OperationalRuntimeGraph

from app.core.logger import log
from app.services.runtime import operational_runtime_graph as orgmod


def _settings_ocr_enabled(settings_obj: Optional[Any]) -> bool:
    if settings_obj is None:
        try:
            from app.core.config import get_settings

            settings_obj = get_settings()
        except Exception:
            return False
    return bool(getattr(settings_obj, "OCR_SHADOW_ENABLED", True))


def _truth_sort_key(tb: Any) -> str:
    cw = getattr(tb, "temporal_signature", None) or {}
    if isinstance(cw, dict):
        return str(cw.get("started_at_iso") or getattr(tb, "truth_id", ""))
    return str(getattr(tb, "truth_id", ""))


def _truth_ok_hint(mission: Mission) -> bool:
    """Señal blanda desde execution_truth (opcional); no fuerza rutas SEP."""
    try:
        from app.services.missions.execution_truth_engine import is_execution_truth_confirmed

        return bool(is_execution_truth_confirmed(mission))
    except Exception:
        return False


def _recovery_tier(recovery_class: str, evidence_sources: Sequence[str]) -> str:
    rc = recovery_class.lower()
    ev = " ".join(str(s).lower() for s in evidence_sources)
    if "arl" in ev or rc == "arl_observed_recovery":
        return "semantic"
    if "cob" in ev or rc == "cob_repair_escalation":
        return "semantic"
    if "procedural_sep" in rc or "replay" in rc or "fallback_to_sep" in ev:
        return "procedural"
    return "structural"


def _goal_terminal_hints(goal: OperationalRuntimeGoal) -> List[str]:
    """Inferencia estable de estados meta solo desde texto ORG/TEL-linked (sin apps DOM)."""

    hay = (goal.headline + " " + goal.rationale + " " + goal.sep_alignment_hint).lower()
    out: List[str] = []

    def _maybe(state: str) -> None:
        if state not in out:
            out.append(state)

    if any(k in hay for k in ("discover", "content", "search", "result", "browse", "viewport")):
        if any(k in hay for k in ("browse", "viewport", "result", "discover", "navigation")):
            _maybe("results_viewport_browsing_active")
        else:
            _maybe("search_query_plane_committed")
    if any(k in hay for k in ("form", "submission", "submit", "structured")):
        _maybe("structured_form_commit_completed")
        _maybe("structured_form_engaged")
    if any(k in hay for k in ("launcher", "open", "host", "application", "surface host")):
        _maybe("surface_host_launch_ready")
    if any(k in hay for k in ("navigate", "landmark", "location", "url")):
        _maybe("navigation_landmark_stable")
    if any(k in hay for k in ("context", "workspace", "switch")):
        _maybe("operational_context_rebound")
    if any(k in hay for k in ("identity", "profile", "select")):
        _maybe("operational_identity_resolved")
    if not out:
        _maybe("operational_context_rebound")

    return out[:8]


def _branching_factor(graph: OperationalRuntimeGraph) -> float:
    if not graph.transitions:
        return 0.0
    out_map: Dict[str, int] = defaultdict(int)
    for t in graph.transitions:
        out_map[t.from_operational_state] += 1
    if not out_map:
        return 0.0
    return round(sum(out_map.values()) / max(len(out_map), 1), 4)


def _provider_independence(nodes: Sequence[Any]) -> float:
    """Derivado de heterogeneidad de `context_window` en truth_blocks reflejado en nodos."""

    keys: set[str] = set()
    for n in nodes:
        ctx = (getattr(n, "context_requirements", None) or {}) if not isinstance(n, dict) else n.get(
            "context_requirements",
            {},
        )
        if not isinstance(ctx, dict):
            continue
        for k in ctx.keys():
            if k.startswith("tel_derived"):
                keys.add(k)
            elif k != "sep_step_index_hint":
                keys.add(str(k))
    diversity = len(keys)
    raw = diversity / max(6.0, float(len(nodes) or 1))
    return round(max(0.0, min(1.0, raw)), 4)


def _anchors_pressure(graph: OperationalRuntimeGraph) -> float:
    nn = len(graph.nodes)
    if nn == 0:
        return 0.0
    anchors = sum(len(n.replay_anchor_candidates or []) for n in graph.nodes)
    return round(min(1.0, anchors / max(5.0 * nn, 1.0)), 4)


def _semantic_recovery_fraction(graph: OperationalRuntimeGraph) -> float:
    if not graph.recovery_edges:
        return 0.0
    sem = sum(
        1
        for r in graph.recovery_edges
        if _recovery_tier(r.recovery_class, r.evidence_sources or []) == "semantic"
    )
    return round(sem / len(graph.recovery_edges), 4)


def _compute_extended_metrics(
    *,
    graph: OperationalRuntimeGraph,
    mission: Mission,
    org_echo: Dict[str, float],
) -> Dict[str, float]:
    nodes = graph.nodes

    continuity_stability = (
        float(org_echo.get("execution_continuity", 0.0)) * 0.55
        + float(org_echo.get("transition_predictability", 0.0)) * 0.25
        + float(org_echo.get("operational_resilience_score", 0.0)) * 0.20
    )
    continuity_stability = round(max(0.0, min(1.0, continuity_stability)), 4)

    replay_fb = round(float(org_echo.get("replay_dependency_score", 0.0)), 4)

    semantic_strength = _semantic_recovery_fraction(graph)

    continuity_repair_coverage = round(float(org_echo.get("recovery_coverage", 0.0)), 4)

    transition_flex = round(_branching_factor(graph) / max(3.0, 1.0), 4)
    transition_flex = min(1.0, transition_flex)

    prov_ind = _provider_independence(nodes)
    ui_surface = round(
        0.5 * float(org_echo.get("layout_independence", 0.0))
        + 0.5 * float(org_echo.get("selector_independence", 0.0)),
        4,
    )

    drift_notes = detect_operational_drift(mission, graph)
    drift_penalty = max(0.0, min(1.0, len(drift_notes) / 12.0))
    operational_drift_resistance = round(1.0 - drift_penalty, 4)

    operational_autonomy = round(
        0.34 * max(0.0, min(1.0, 1.0 - replay_fb))
        + 0.26 * semantic_strength
        + 0.20 * continuity_stability
        + 0.12 * float(org_echo.get("operational_resilience_score", 0.0))
        + 0.08 * prov_ind,
        4,
    )

    return {
        "operational_autonomy_score": operational_autonomy,
        "continuity_stability_score": continuity_stability,
        "replay_fallback_pressure": replay_fb,
        "semantic_recovery_strength": semantic_strength,
        "continuity_repair_coverage": continuity_repair_coverage,
        "transition_flexibility": transition_flex,
        "provider_independence": prov_ind,
        "ui_surface_independence": ui_surface,
        "operational_drift_resistance": operational_drift_resistance,
        "replay_dependence_residual": replay_fb,
    }


def _truth_sort_key(tb: Any) -> str:
    cw = getattr(tb, "temporal_signature", None) or {}
    if isinstance(cw, dict):
        return str(cw.get("started_at_iso") or getattr(tb, "truth_id", ""))
    return str(getattr(tb, "truth_id", ""))


def evaluate_operational_continuity(
    mission: Mission,
    graph: OperationalRuntimeGraph,
) -> Tuple[float, List[OperationalContinuityState]]:
    """Puntúa si la cadena declarativa mantiene continuidad bajo invariantes conocidos."""

    org_m = dict(graph.metrics or {})

    truths = getattr(mission, "truth_blocks", None) or []
    readiness_vals: List[float] = []
    for tb in truths:
        try:
            readiness_vals.append(float(getattr(tb, "execution_readiness", 0.5)))
        except Exception:
            readiness_vals.append(0.5)

    readiness_term = (
        round(sum(readiness_vals) / max(len(readiness_vals), 1), 4) if readiness_vals else org_m.get("execution_continuity", 0.5)
    )

    continuity = (
        readiness_term * 0.42
        + float(org_m.get("execution_continuity", 0.0)) * 0.23
        + float(org_m.get("recovery_coverage", 0.0)) * 0.18
        + float(org_m.get("operational_resilience_score", 0.0)) * 0.12
        + (_truth_ok_hint(mission) and 0.05 or 0.0)
    )
    continuity = round(max(0.0, min(1.0, continuity)), 4)

    states: List[OperationalContinuityState] = []
    for i, node in enumerate(graph.nodes):
        inv = []
        vv = getattr(node, "expected_validators", None) or []
        if vv:
            inv.extend([str(v).split(":")[-1][:32] for v in vv[:6]])
        states.append(
            OperationalContinuityState(
                state_id=f"ccs_{node.node_id}",
                operational_state_label=node.operational_state,
                cumulative_progress_hint=round((i + 1) / max(len(graph.nodes), 1), 4),
                invariant_tags=inv[:8],
                validity_under_declared_truth=bool(vv),
                notes="truth-backed" if node.truth_block_id else "sep_projection",
            ),
        )

    walk = orgmod.execute_operational_graph_shadow(graph)
    if isinstance(walk, dict) and not walk.get("ok") and walk.get("reason") == "missing_transition":
        continuity *= 0.82

    return continuity, states


def detect_operational_drift(mission: Mission, graph: OperationalRuntimeGraph) -> List[str]:
    drift: List[str] = []

    cmp_blob = compare_ocr_vs_sep_runtime(graph, mission)
    pair = cmp_blob.get("paired_agreement_ratio")
    if pair is not None and float(pair) < 0.55:
        drift.append(f"low_org_sep_coarse_agreement:{pair}")

    nc = cmp_blob.get("org_state_count", 0) or 0
    sc = cmp_blob.get("sep_step_count", 0) or 0
    if sc and nc and abs(int(sc) - int(nc)) > max(4, int(0.4 * max(sc, nc))):
        drift.append(f"step_count_topology_divergence:{nc}≠{sc}")

    # Readiness deltas entre truth adyacentes
    truths = sorted(list(mission.truth_blocks or []), key=_truth_sort_key)
    for a, b in zip(truths[:-1], truths[1:]):
        try:
            ra = float(getattr(a, "execution_readiness", 0.55))
            rb = float(getattr(b, "execution_readiness", 0.55))
        except Exception:
            ra = rb = 0.55
        if ra - rb > 0.35:
            drift.append(
                f"readiness_regression_between_truths:"
                f"{getattr(a, 'truth_type', '')}->{getattr(b, 'truth_type', '')}",
            )

    if _anchors_pressure(graph) > 0.72:
        drift.append("high_replay_anchor_surface_pressure")

    return drift


def find_operational_recovery(
    graph: OperationalRuntimeGraph,
    *,
    symptom_substr: str = "",
    limit: int = 24,
) -> List[OperationalRecoveryAttempt]:
    """Lista recoveries ordenados: semantic-first, structural, procedural."""

    needles = symptom_substr.lower().strip()

    tier_rank_order = {"semantic": 0, "structural": 1, "procedural": 2}

    def _attempt(r: Any) -> OperationalRecoveryAttempt:
        tier = _recovery_tier(r.recovery_class, r.evidence_sources or [])
        return OperationalRecoveryAttempt(
            recovery_edge_id=r.edge_id,
            recovery_tier=tier,
            symptom_state=r.symptom_state,
            target_state=r.target_state,
            recovery_class=r.recovery_class,
            description=r.description,
            evidence_sources=list(r.evidence_sources or []),
        )

    cand = list(graph.recovery_edges)

    filtered = []
    if needles:
        for r in cand:
            sym = (r.symptom_state or "").lower()
            desc = (r.description or "").lower()
            cls = (r.recovery_class or "").lower()
            if needles in sym or needles in desc or needles in cls:
                filtered.append(r)
        if not filtered:
            filtered = cand
    else:
        filtered = cand

    ranked = sorted(filtered, key=lambda r: tier_rank_order.get(_recovery_tier(r.recovery_class, r.evidence_sources or []), 1))
    return [_attempt(r) for r in ranked[:limit]]


def find_valid_operational_transition(
    graph: OperationalRuntimeGraph,
    from_state: str,
    *,
    toward_state: Optional[str] = None,
) -> List[OperationalTransitionAttempt]:
    """Transiciones válidas que acercan a un estado operacional destino declarado."""

    valid: List[OperationalTransitionAttempt] = []
    for t in graph.transitions:
        if t.from_operational_state != from_state:
            continue
        key_overlap = len(t.expected_validator_keys or [])
        leg = round(min(1.0, 0.45 + key_overlap / 48.0), 4)

        cand = OperationalTransitionAttempt(
            transition_id=t.transition_id,
            from_operational_state=t.from_operational_state,
            to_operational_state=t.to_operational_state,
            legitimacy_score=leg,
            declared_in_org=True,
            would_validate_shadow=bool(key_overlap),
            rationale=t.description[:220],
        )
        valid.append(cand)

    if toward_state:
        toward = toward_state
        reach = deque([toward])
        dist: Dict[str, int] = {toward: 0}
        while reach:
            cur = reach.popleft()
            d0 = dist[cur]
            if d0 >= 31:
                continue
            for tr in graph.transitions:
                if tr.to_operational_state != cur:
                    continue
                prev = tr.from_operational_state
                if prev not in dist:
                    dist[prev] = d0 + 1
                    reach.append(prev)

        def _tier(node: str) -> Tuple[int, int]:
            hops = dist.get(node, 99)
            direct = 0 if node == toward else 1
            return (direct, hops)

        valid.sort(key=lambda x: (_tier(x.to_operational_state), -x.legitimacy_score))
    else:
        valid.sort(key=lambda x: -x.legitimacy_score)
    return valid


def attempt_operational_transition(
    graph: OperationalRuntimeGraph,
    from_state: str,
    to_state: str,
) -> OperationalTransitionAttempt:
    for t in graph.transitions:
        if t.from_operational_state == from_state and t.to_operational_state == to_state:
            keys = len(t.expected_validator_keys or [])
            leg = round(min(1.0, 0.52 + keys / 40.0), 4)
            return OperationalTransitionAttempt(
                transition_id=t.transition_id,
                from_operational_state=from_state,
                to_operational_state=to_state,
                legitimacy_score=leg,
                declared_in_org=True,
                would_validate_shadow=keys > 0,
                rationale=t.description[:220],
            )
    return OperationalTransitionAttempt(
        transition_id="",
        from_operational_state=from_state,
        to_operational_state=to_state,
        legitimacy_score=0.08,
        declared_in_org=False,
        would_validate_shadow=False,
        rationale="Sin arco declarado ORG/TEL.",
    )


def evaluate_operational_goal(
    mission: Mission,
    graph: OperationalRuntimeGraph,
    *,
    goal_id: Optional[str] = None,
) -> Optional[OperationalGoalExecution]:
    if not graph.goals:
        return None
    goal = next((g for g in graph.goals if g.goal_id == goal_id), None) if goal_id else graph.goals[0]

    terminals = _goal_terminal_hints(goal)
    states_seq = [n.operational_state for n in graph.nodes]

    # BFS al primer estado terminal alcanzable siguiendo arcos declarados desde el estado inicial conocido.
    start = states_seq[0] if states_seq else ""
    if not start:
        return OperationalGoalExecution(
            goal_id=goal.goal_id,
            headline=goal.headline,
            desired_terminal_states=terminals,
            blocked_reason="empty_operational_topology",
        )

    visited: set[str] = set()
    parent: Dict[str, Optional[str]] = {}
    q: deque[str] = deque([start])
    visited.add(start)
    hit: Optional[str] = None

    while q:
        cur = q.popleft()
        if cur in set(terminals):
            hit = cur
            break
        for tr in graph.transitions:
            if tr.from_operational_state != cur:
                continue
            nxt = tr.to_operational_state
            if nxt not in visited:
                visited.add(nxt)
                parent[nxt] = cur
                q.append(nxt)

    path: List[str] = []
    if hit:
        crawl: Optional[str] = hit
        while crawl is not None:
            path.append(crawl)
            crawl = parent.get(crawl)  # type: ignore[arg-type]
        path.reverse()

        satisfied = path[-1] in set(terminals)
        seq_set = set(states_seq)
        progress = round(len(seq_set.intersection(path)) / max(len(seq_set), 1), 4)
        return OperationalGoalExecution(
            goal_id=goal.goal_id,
            headline=goal.headline,
            desired_terminal_states=terminals,
            simulation_path_states=path,
            progress_ratio=progress,
            satisfied_shadow=satisfied,
        )

    return OperationalGoalExecution(
        goal_id=goal.goal_id,
        headline=goal.headline,
        desired_terminal_states=terminals,
        simulation_path_states=[],
        blocked_reason="goal_terminal_not_reachable_via_declared_transitions",
    )


def execute_operational_goal_shadow(
    mission: Mission,
    graph: OperationalRuntimeGraph,
    *,
    goal_id: Optional[str] = None,
) -> OperationalGoalExecution:
    """Sinónimo explícito de simulación goal-only (sombras)."""

    ex = evaluate_operational_goal(mission, graph, goal_id=goal_id)
    return ex or OperationalGoalExecution(goal_id="", headline="", blocked_reason="no_goal_topology")


def compare_ocr_vs_sep_runtime(
    graph: OperationalRuntimeGraph,
    mission: Mission,
) -> Dict[str, Any]:
    baseline = orgmod.compare_org_vs_sep_runtime(graph, mission)
    sep_blob = getattr(mission, "semantic_execution_plan", None) or {}

    procedural_density = baseline.get("replay_linearity_residual", 0.0)
    uniq_types = (
        len({str(s.get("type")) for s in (sep_blob.get("steps") or []) if isinstance(s, dict)})
        if isinstance(sep_blob, dict)
        else 0
    )
    resilience = dict(graph.metrics or {})

    baseline["ocr_projection"] = {
        "procedural_uniformity_hint": procedural_density,
        "sep_unique_capabilities": uniq_types,
        "continuity_pressure_vs_replay_dependency": resilience.get("replay_dependency_score"),
    }

    anch = _anchors_pressure(graph)
    baseline["replay_dependence_estimate"] = round(anch, 4)
    baseline["operational_autonomy_hint"] = round(max(0.0, min(1.0, 1.0 - anch)), 4)
    return baseline


def build_operational_continuity_snapshot(
    mission: Mission,
    *,
    settings: Optional[Any] = None,
) -> OperationalContinuitySnapshot:
    if not _settings_ocr_enabled(settings):
        return OperationalContinuitySnapshot(
            continuity_score=0.0,
            drift_detections=["OCR_SHADOW_DISABLED"],
            build_sources_echo=[],
        )

    graph = orgmod.build_operational_runtime_graph(mission)
    org_echo = dict(graph.metrics or {})

    score, cont_states = evaluate_operational_continuity(mission, graph)
    drift = detect_operational_drift(mission, graph)

    recoveries = find_operational_recovery(graph, symptom_substr="", limit=32)

    goal_ex = execute_operational_goal_shadow(mission, graph)

    states_seq = [n.operational_state for n in graph.nodes]
    inferred_current = states_seq[-1] if states_seq else ""
    active_headline = goal_ex.headline if goal_ex else ""

    pred = orgmod.predict_next_operational_state(graph, inferred_current) if inferred_current else graph.predicted_next_states

    walk = orgmod.execute_operational_graph_shadow(graph)
    breaks: List[str] = []
    if not walk.get("ok"):
        breaks.append(str(walk.get("reason") or "shadow_walk_not_ok"))
        gap = walk.get("gap")
        if gap:
            breaks.append(f"transition_gap:{gap}")

    ext = _compute_extended_metrics(graph=graph, mission=mission, org_echo=org_echo)

    sample_tx: List[OperationalTransitionAttempt] = []
    if inferred_current:
        sample_tx = find_valid_operational_transition(graph, inferred_current)[:6]

    snap = OperationalContinuitySnapshot(
        continuity_score=score,
        continuity_stability_score=ext["continuity_stability_score"],
        inferred_current_operational_state=inferred_current,
        desired_operational_hint=(goal_ex.desired_terminal_states[0] if goal_ex and goal_ex.desired_terminal_states else ""),
        active_goal_headline=active_headline,
        predicted_next_states=pred[:16],
        drift_detections=drift,
        continuity_recoveries=recoveries,
        continuity_breaks=breaks,
        replay_dependence_estimate=ext["replay_fallback_pressure"],
        operational_autonomy_score=ext["operational_autonomy_score"],
        metrics={**org_echo, **ext},
        continuity_states=cont_states,
        goal_execution_shadow=goal_ex,
        transition_attempts_sample=sample_tx,
        ocr_vs_sep=compare_ocr_vs_sep_runtime(graph, mission),
        build_sources_echo=list(graph.build_sources_used or []),
    )
    return snap


def attach_operational_continuity_audit(mission: Mission, audit_blob: Dict[str, Any]) -> None:
    mission.operational_continuity_audit = dict(audit_blob)


def refresh_operational_continuity_shadow(
    mission: Mission,
    settings: Optional[Any] = None,
) -> OperationalContinuitySnapshot:
    """Refresco Mission Review: persiste snapshot + historia acotada, sin mutar SEP."""

    if not _settings_ocr_enabled(settings):
        attach_operational_continuity_audit(
            mission,
            {"ok": False, "reason": "OCR_SHADOW_DISABLED"},
        )
        snap = OperationalContinuitySnapshot(drift_detections=["OCR_SHADOW_DISABLED"])
        mission.continuity_runtime_shadow = snap.model_dump(mode="json")
        return snap

    try:
        snap = build_operational_continuity_snapshot(mission, settings=settings)

        gx = snap.goal_execution_shadow
        history_entry = {
            "generated_at_iso": snap.generated_at_iso,
            "headline": (gx.headline if gx else snap.active_goal_headline),
            "continuity_score": snap.continuity_score,
            "autonomy_score": snap.operational_autonomy_score,
            "replay_pressure": snap.replay_dependence_estimate,
            "progress_ratio": gx.progress_ratio if gx else None,
            "drift_count": len(snap.drift_detections),
        }
        mh = getattr(mission, "operational_goal_history", None) or []
        if not isinstance(mh, list):
            mh = []
        mh.append(history_entry)
        mission.operational_goal_history = mh[-48:]

        mission.continuity_runtime_shadow = snap.model_dump(mode="json")
        attach_operational_continuity_audit(
            mission,
            {
                "ok": True,
                "continuity_score": snap.continuity_score,
                "operational_autonomy_score": snap.operational_autonomy_score,
                "replay_dependence_estimate": snap.replay_dependence_estimate,
                "drifts": snap.drift_detections,
                "recovery_total": len(snap.continuity_recoveries),
                "ocr_vs_sep": snap.ocr_vs_sep,
            },
        )
        log.debug(
            "[OCR] continuity=%s autonomy=%s drift=%s",
            snap.continuity_score,
            snap.operational_autonomy_score,
            len(snap.drift_detections),
        )
        return snap

    except Exception as exc:
        log.debug("[OCR] refresh failed: %s", exc)
        attach_operational_continuity_audit(mission, {"ok": False, "reason": str(exc)})
        snap = OperationalContinuitySnapshot(drift_detections=[f"ocr_fault:{exc}"])
        mission.continuity_runtime_shadow = snap.model_dump(mode="json")
        return snap


def merge_live_operational_perception_into_continuity_shadow(mission: Mission) -> Dict[str, Any]:
    """Fusiona el último eco LOP (Fase 2) en ``continuity_runtime_shadow``."""

    raw = getattr(mission, "lop_last_snapshot", None)
    ctr = getattr(mission, "continuity_runtime_shadow", None)
    fused: Dict[str, Any] = dict(ctr) if isinstance(ctr, dict) else {}
    if not isinstance(raw, dict) or not raw:
        mission.continuity_runtime_shadow = fused
        return fused
    fused.update(
        lop_live_surface_echo={
            "surface_type": raw.get("surface_type"),
            "recommended_runtime_action": raw.get("recommended_runtime_action"),
            "confidence": raw.get("confidence"),
            "modal_detected": raw.get("modal_detected"),
            "matched_goal_tail": (raw.get("matched_goal_ids") or [])[:16],
            "live_blockers_count": len(raw.get("live_blockers") or []),
            "lop_sensor_bridge": True,
        },
    )

    setattr(mission, "continuity_runtime_shadow", fused)
    return fused


__all__ = [
    "attach_operational_continuity_audit",
    "build_operational_continuity_snapshot",
    "compare_ocr_vs_sep_runtime",
    "detect_operational_drift",
    "evaluate_operational_continuity",
    "evaluate_operational_goal",
    "execute_operational_goal_shadow",
    "find_operational_recovery",
    "find_valid_operational_transition",
    "attempt_operational_transition",
    "refresh_operational_continuity_shadow",
    "merge_live_operational_perception_into_continuity_shadow",
]
