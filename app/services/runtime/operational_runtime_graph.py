"""Operational Runtime Graph (ORG) — Fase sombra.

Construye un grafo operacional declarativo a partir de ``truth_blocks``,
SEP semántico, sesiones COB/ARL y snapshots de ejecución; **no ejecuta**
el SmartExecutor ni el replay legacy.

La misión puede persistir el blob JSON en ``mission.operational_runtime_graph``.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from app.contracts.mission import Mission
from app.contracts.operational_runtime_graph import (
    OperationalRuntimeGraph,
    OperationalRuntimeGoal,
    OperationalRuntimeNode,
    OperationalRuntimeRecoveryEdge,
    OperationalRuntimeState,
    OperationalRuntimeTransition,
)
from app.core.logger import log


_TRUTH_OPERATIONAL_STATES: Dict[str, str] = {
    "open_application": "surface_host_launch_ready",
    "select_entity": "operational_identity_resolved",
    "open_tab": "browsing_tab_surface_ready",
    "navigate_to_location": "navigation_landmark_stable",
    "search_content": "search_query_plane_committed",
    "browse_results": "results_viewport_browsing_active",
    "fill_form": "structured_form_engaged",
    "submit_form": "structured_form_commit_completed",
    "switch_context": "operational_context_rebound",
    "confirm_dialog": "modal_commit_surface_ready",
    "choose_option": "discrete_choice_resolution_ready",
}

_SEP_TYPE_OPERATIONAL_STATES: Dict[str, str] = {
    "open_app": "surface_host_launch_ready",
    "select_visible_option": "operational_identity_resolved",
    "select_profile": "operational_identity_resolved",
    "choose_option": "discrete_choice_resolution_ready",
    "open_new_tab": "browsing_tab_surface_ready",
    "open_url": "navigation_landmark_stable",
    "navigate_to_location": "navigation_landmark_stable",
    "search_content": "search_query_plane_committed",
    "scroll_results": "results_viewport_browsing_active",
    "browse_results": "results_viewport_browsing_active",
    "fill_form": "structured_form_engaged",
    "submit_form": "structured_form_commit_completed",
    "submit_field": "structured_form_commit_completed",
    "switch_context": "operational_context_rebound",
}

_TRANSITION_SURFACE_HINTS: Dict[Tuple[str, str], Tuple[str, List[str]]] = {
    (
        "surface_host_launch_ready",
        "operational_identity_resolved",
    ): ("Identity resolution après surface selection", ["launcher_surface", "chooser_surface"]),
    (
        "operational_identity_resolved",
        "browsing_tab_surface_ready",
    ): ("New navigable browsing surface obtained", ["tab_surface"]),
    ("browsing_tab_surface_ready", "navigation_landmark_stable"): (
        "Landmark navigation committed",
        ["url_surface", "history_surface", "session_restore_surface"],
    ),
    ("navigation_landmark_stable", "search_query_plane_committed"): (
        "Operational search plane engages query",
        ["search_shell_surface"],
    ),
    (
        "search_query_plane_committed",
        "results_viewport_browsing_active",
    ): ("Operational browsing over results engages", ["results_viewport_surface"]),
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _truth_sort_key(tb: Any) -> str:
    cw = getattr(tb, "temporal_signature", None) or {}
    if isinstance(cw, dict):
        return str(cw.get("started_at_iso") or getattr(tb, "truth_id", ""))
    return str(getattr(tb, "truth_id", ""))


def _settings_org_enabled(settings_obj: Optional[Any]) -> bool:
    if settings_obj is None:
        try:
            from app.core.config import get_settings

            settings_obj = get_settings()
        except Exception:
            return False
    return bool(getattr(settings_obj, "ORG_SHADOW_ENABLED", True))


def _validators_for_truth(tb: Any) -> List[str]:
    ve = list(getattr(tb, "validator_expectations", None) or [])
    ts = getattr(tb, "truth_type", "") or ""
    if ve:
        return [str(v) for v in ve][:16]
    if ts in {"search_content", "browse_results"}:
        return ["validator:search_shell_ready", "validator:results_viewport"]
    if ts in {"fill_form", "submit_form"}:
        return ["validator:form_engagement"]
    if ts == "navigate_to_location":
        return ["validator:navigation_landmark"]
    if ts == "open_application":
        return ["validator:launcher_surface_stable"]
    return ["validator:operational_truth_ok"]


def _node_from_truth(tb: Any) -> OperationalRuntimeNode:
    tt = str(getattr(tb, "truth_type", "") or "").strip()
    op_state = _TRUTH_OPERATIONAL_STATES.get(tt) or f"truth_surface_{tt or 'unknown'}"
    ctx = getattr(tb, "context_window", None) or {}
    if not isinstance(ctx, dict):
        ctx = {}
    return OperationalRuntimeNode(
        node_id=_new_id("n"),
        operational_state=op_state,
        truth_block_id=str(getattr(tb, "truth_id", "") or ""),
        truth_type=tt,
        expected_validators=_validators_for_truth(tb),
        acceptable_variants=[str(tt).replace("_", " ")],
        recovery_options=list((getattr(tb, "debug_trace", None) or {}).get("recovery_hints") or [])[:8]
        if isinstance(getattr(tb, "debug_trace", None), dict)
        else [],
        transition_expectations=[],
        execution_truth_snapshot=dict(getattr(tb, "outcome_snapshot", None) or {}),
        ambiguity_snapshot=dict(
            {
                "ambiguity_score": float(getattr(tb, "ambiguity_score", 0.5)),
                "requires_human_confirmation": bool(getattr(tb, "requires_human_confirmation", False)),
            },
        ),
        stability_score=float(getattr(tb, "stability_score", 0.5)),
        replay_anchor_candidates=list(getattr(tb, "replay_anchor_candidates", None) or [])[:12],
        context_requirements={
            **{k: v for k, v in ctx.items() if k != "tel_derived_params"},
            **(
                {"tel_derived_param_keys": list((ctx.get("tel_derived_params") or {}).keys())}
                if isinstance(ctx.get("tel_derived_params"), dict)
                else {}
            ),
        },
        success_signals=list((getattr(tb, "outcome_snapshot", None) or {}).get("success_signals") or [])[:12]
        if isinstance(getattr(tb, "outcome_snapshot", None), dict)
        else [],
        failure_signals=list((getattr(tb, "debug_trace", None) or {}).get("failure_signals") or [])[:8]
        if isinstance(getattr(tb, "debug_trace", None), dict)
        else [],
    )


def _node_from_sep_step(raw: Dict[str, Any], idx: int) -> OperationalRuntimeNode:
    stype = str(raw.get("type") or "").strip()
    op_state = _SEP_TYPE_OPERATIONAL_STATES.get(stype) or f"sep_surface_{stype or 'unknown'}"
    return OperationalRuntimeNode(
        node_id=str(raw.get("id") or _new_id("sep")),
        operational_state=op_state,
        truth_block_id="",
        truth_type="",
        expected_validators=[f"sep_validator:{stype}"][:8],
        acceptable_variants=[stype.replace("_", " ")],
        recovery_options=[],
        transition_expectations=[],
        execution_truth_snapshot={},
        ambiguity_snapshot=dict(raw.get("ambiguity_hints") or {}),
        stability_score=float(raw.get("confidence") or 0.55),
        replay_anchor_candidates=list(raw.get("replay_anchor_candidates") or [])[:12],
        context_requirements={"sep_step_index_hint": idx, "semantic_step_type": stype},
        success_signals=list(raw.get("success_signals_hint") or [])[:8],
        failure_signals=list(raw.get("failure_signals_hint") or [])[:8],
    )


def _goals_from_truths(nodes: Sequence[OperationalRuntimeNode], mission: Mission) -> List[OperationalRuntimeGoal]:
    truth_types = [n.truth_type for n in nodes if n.truth_type]

    absorbed_sessions: List[str] = []
    absorbed_cobs: List[str] = []
    for tb in getattr(mission, "truth_blocks", None) or []:
        absorbed_sessions.extend(list(getattr(tb, "absorbed_sessions", None) or [])[:24])
        if len(absorbed_sessions) > 96:
            break
    try:
        for cob in getattr(mission, "canonical_operational_blocks", None) or []:
            cid = getattr(cob, "id", None)
            if cid:
                absorbed_cobs.append(str(cid))
            if len(absorbed_cobs) > 48:
                break
    except Exception:
        absorbed_cobs = []

    originating_ids = [
        getattr(tb, "truth_id", "")
        for tb in sorted(mission.truth_blocks or [], key=_truth_sort_key)
        if getattr(tb, "truth_id", None)
    ]

    goals: List[OperationalRuntimeGoal] = []
    tt_set = set(truth_types)
    sep_types = []
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    if isinstance(sep, dict):
        for s in sep.get("steps") or []:
            if isinstance(s, dict):
                sep_types.append(str(s.get("type") or ""))
    sep_set = set(sep_types)

    if {"search_content", "browse_results"}.issubset(tt_set | sep_set) or (
        "results_viewport_browsing_active" in {n.operational_state for n in nodes}
        and "search_query_plane_committed" in {n.operational_state for n in nodes}
    ):
        goals.append(
            OperationalRuntimeGoal(
                goal_id=_new_id("goal"),
                headline="Operational content discovery and results navigation",
                rationale=(
                    "La misión establece una intención de buscar contenido operacional "
                    "y continuar dentro del viewport de resultados."
                ),
                originating_truth_ids=originating_ids[:64],
                originating_session_ids=list(dict.fromkeys(absorbed_sessions))[:32],
                originating_cob_ids=list(dict.fromkeys(absorbed_cobs))[:24],
                sep_alignment_hint=(
                    "Alined with semantic search/browse capabilities (non-replay)."
                ),
                ambiguity_notes=(
                    ["multi-host_search_surface"]
                    if getattr(mission, "session_shadow_mode", False)
                    else []
                ),
            ),
        )

    if {"fill_form", "submit_form"}.issubset(tt_set | sep_set):
        goals.append(
            OperationalRuntimeGoal(
                goal_id=_new_id("goal"),
                headline="Structured operational submission",
                rationale="Secuencia de captura formal seguida de confirmación ejecutable.",
                originating_truth_ids=originating_ids[:64],
                originating_session_ids=list(dict.fromkeys(absorbed_sessions))[:24],
                originating_cob_ids=list(dict.fromkeys(absorbed_cobs))[:16],
                sep_alignment_hint="Form capability chain",
                ambiguity_notes=["double_confirm_modal_possible"],
            ),
        )

    # Goal de continuidad host / launcher (primer bloque típico)
    if goals:
        return goals[:3]

    if nodes:
        return [
            OperationalRuntimeGoal(
                goal_id=_new_id("goal"),
                headline="Maintain operational continuity across declared transitions",
                rationale="Cadena inferida desde verdad operacional y/o SEP semántico.",
                originating_truth_ids=originating_ids[:80],
                originating_session_ids=list(dict.fromkeys(absorbed_sessions))[:24],
                originating_cob_ids=list(dict.fromkeys(absorbed_cobs))[:16],
                sep_alignment_hint="Fallback continuity goal",
                ambiguity_notes=[],
            ),
        ]
    return []


def _recovery_edges_from_templates(
    states_seq: Sequence[str],
    arl_audit: Dict[str, Any],
    cob_blocks: Sequence[Any],
) -> List[OperationalRuntimeRecoveryEdge]:
    edges: List[OperationalRuntimeRecoveryEdge] = []
    uniq_sig: Set[str] = set()

    def _push(e: OperationalRuntimeRecoveryEdge) -> None:
        sig = "|".join((e.symptom_state, e.target_state, e.recovery_class, e.description[:48]))
        if sig in uniq_sig:
            return
        uniq_sig.add(sig)
        edges.append(e)

    for prev, curr in zip(states_seq[:-1], states_seq[1:]):
        rid = _new_id("rcv")
        _push(
            OperationalRuntimeRecoveryEdge(
                edge_id=rid,
                symptom_state=f"stall_at:{curr}",
                target_state=prev,
                recovery_class="reanchor_prior_operational_landmark",
                description=f"Operational stall en {curr} → recuperar invariante anterior {prev}.",
                evidence_sources=["truth_transition_template"],
            ),
        )

    for j in range(len(states_seq)):
        st = states_seq[j]
        if st == "results_viewport_browsing_active":
            _push(
                OperationalRuntimeRecoveryEdge(
                    edge_id=_new_id("rcv"),
                    symptom_state="results_viewport_missing_or_stalled",
                    target_state="search_query_plane_committed",
                    recovery_class="reassert_search_plane",
                    description="Sin resultados navegables observables → re-afirmar plano de consulta.",
                    evidence_sources=["org_template"],
                ),
            )
            _push(
                OperationalRuntimeRecoveryEdge(
                    edge_id=_new_id("rcv"),
                    symptom_state="provider_surface_not_loaded",
                    target_state="navigation_landmark_stable",
                    recovery_class="navigation_surface_refresh",
                    description="Fallo contextual en proveedor de búsqueda → re-materializar navegación declarativa.",
                    evidence_sources=["org_template"],
                ),
            )

        if st == "operational_identity_resolved":
            _push(
                OperationalRuntimeRecoveryEdge(
                    edge_id=_new_id("rcv"),
                    symptom_state="identity_surface_not_locked",
                    target_state=st,
                    recovery_class="reselect_operational_identity",
                    description="Re-seleccionar identidad/perfil ante ambigüedad visual.",
                    evidence_sources=["org_template"],
                ),
            )

    # ARL observado
    events = list((arl_audit or {}).get("events") or [])
    for ev in events:
        if not isinstance(ev, dict):
            continue
        dec = ev.get("decision")
        if isinstance(dec, dict):
            dval = str(dec.get("decision") or "")
        else:
            b = ev.get("bundle") or {}
            dval = str((b.get("decision") or {}).get("decision") or "")
        if dval not in {"ADAPT", "REVALIDATE", "FALLBACK_TO_SEP"}:
            continue
        primary = ""
        b2 = ev.get("bundle") or {}
        if isinstance(b2, dict):
            cls = b2.get("classification") or {}
            if isinstance(cls, dict):
                primary = str(cls.get("primary_type") or "")
        action = str(ev.get("canonical_action") or ev.get("type") or "arl_event")
        _push(
            OperationalRuntimeRecoveryEdge(
                edge_id=_new_id("arl_rcv"),
                symptom_state=f"arl_signal:{action}:{primary or dval}",
                target_state=states_seq[-1] if states_seq else "operational_fallback_surface",
                recovery_class="arl_observed_recovery",
                description=f"Recuperación observada ARL ({dval}) para {action}.",
                evidence_sources=["arl", str(ev.get("type") or "unknown")],
            ),
        )

    # COB repair hints ligeros
    for cob in cob_blocks:
        rp = getattr(cob, "repair_strategy", None) or {}
        if isinstance(rp, dict) and rp:
            _push(
                OperationalRuntimeRecoveryEdge(
                    edge_id=_new_id("cob_rcv"),
                    symptom_state=f"cob_signal:{str(getattr(cob, 'canonical_action', '') or 'block')}",
                    target_state=states_seq[-1] if states_seq else "surface_host_launch_ready",
                    recovery_class="cob_repair_escalation",
                    description=str(rp.get("summary") or "COB declaró opciones de repair operacional."),
                    evidence_sources=["cob_shadow", str(getattr(cob, "id", "") or "")],
                ),
            )
    return edges


def build_operational_runtime_graph(mission: Mission) -> OperationalRuntimeGraph:
    """Ensambla ORG desde truth_blocks (+ SEP / sesiones / COB como contexto declarativo)."""

    sources: List[str] = []

    truths = sorted(list(mission.truth_blocks or []), key=_truth_sort_key)
    nodes: List[OperationalRuntimeNode] = []

    if truths:
        sources.append("truth_blocks")
        nodes = [_node_from_truth(tb) for tb in truths]
    elif isinstance(getattr(mission, "semantic_execution_plan", None), dict):
        sources.append("semantic_execution_plan_fallback")
        for i, raw in enumerate((mission.semantic_execution_plan or {}).get("steps") or []):
            if isinstance(raw, dict):
                nodes.append(_node_from_sep_step(raw, i))

    if mission.semantic_sessions and "semantic_sessions" not in "|".join(sources):
        sources.append("semantic_sessions")

    transitions: List[OperationalRuntimeTransition] = []
    for a, b in zip(nodes[:-1], nodes[1:]):
        key = (a.operational_state, b.operational_state)
        desc, classes = _TRANSITION_SURFACE_HINTS.get(
            key,
            (
                f"Operational refinement {a.operational_state} → {b.operational_state}",
                ["semantic_surface_exchange"],
            ),
        )
        transitions.append(
            OperationalRuntimeTransition(
                transition_id=_new_id("t"),
                from_operational_state=a.operational_state,
                to_operational_state=b.operational_state,
                description=desc,
                legitimate_surface_classes=classes + list(dict.fromkeys(a.acceptable_variants))[:4],
                expected_validator_keys=list(dict.fromkeys(a.expected_validators + b.expected_validators))[:24],
            ),
        )

    states_seq = [n.operational_state for n in nodes]
    arl_aud = getattr(mission, "adaptive_runtime_audit", None) or {}
    if not isinstance(arl_aud, dict):
        arl_aud = {}
    cobs = list(getattr(mission, "canonical_operational_blocks", None) or [])[:64]
    if cobs and "canonical_operational_blocks" not in sources:
        sources.append("canonical_operational_blocks")

    recovery_edges = _recovery_edges_from_templates(states_seq, arl_aud, cobs)

    goals = _goals_from_truths(nodes, mission)

    graph = OperationalRuntimeGraph(
        version="1.0",
        nodes=nodes,
        transitions=transitions,
        goals=goals,
        recovery_edges=recovery_edges,
        metrics={},  # set after metrics eval
        predicted_next_states=[],
        shadows_runtime=True,
        build_sources_used=sources,
    )
    sep_count = len((getattr(mission, "semantic_execution_plan", None) or {}).get("steps") or [])
    graph.metrics.update(_compute_resilience_metrics(graph, truths=list(truths), sep_step_count=float(sep_count)))
    if states_seq:
        graph.predicted_next_states = predict_next_operational_state(graph, states_seq[0])
    return graph


def _compute_resilience_metrics(
    graph: OperationalRuntimeGraph,
    *,
    truths: Sequence[Any],
    sep_step_count: float,
) -> Dict[str, float]:
    nn = len(graph.nodes)
    if nn == 0:
        return {
            "replay_dependency_score": 0.0,
            "operational_resilience_score": 0.0,
            "recovery_coverage": 0.0,
            "transition_predictability": 0.0,
            "runtime_compactness": 0.0,
            "execution_continuity": 0.0,
            "layout_independence": 0.0,
            "selector_independence": 0.0,
        }

    anchors = sum(len(n.replay_anchor_candidates) for n in graph.nodes)
    avg_stability = sum(n.stability_score for n in graph.nodes) / float(nn)

    ambi = []
    for tb in truths:
        try:
            ambi.append(float(getattr(tb, "ambiguity_score", 0.5)))
        except Exception:
            ambi.append(0.5)
    avg_amb = sum(ambi) / max(len(ambi), 1) if ambi else 0.5

    readiness_vals = []
    for tb in truths:
        try:
            readiness_vals.append(float(getattr(tb, "execution_readiness", 0.5)))
        except Exception:
            readiness_vals.append(0.5)
    avg_readiness = sum(readiness_vals) / max(len(readiness_vals), 1) if readiness_vals else 0.55

    replay_dependency = min(1.0, anchors / max(5.0 * nn, 1.0))
    layout_independence = max(0.0, min(1.0, 1.0 - avg_amb))
    selector_independence = max(0.0, min(1.0, avg_stability))
    recov = len(graph.recovery_edges)
    trans = len(graph.transitions)
    operational_resilience = min(1.0, recov / max(trans, 1))
    recovery_coverage = min(1.0, recov / max(float(nn), 1.0))
    transition_predictability = 1.0 if trans <= max(nn - 1, 0) else 0.82
    runtime_compactness = float(nn) / max(sep_step_count, 1.0) if sep_step_count else 1.0
    execution_continuity = max(0.0, min(1.0, avg_readiness))

    return {
        "replay_dependency_score": round(replay_dependency, 4),
        "operational_resilience_score": round(operational_resilience, 4),
        "recovery_coverage": round(recovery_coverage, 4),
        "transition_predictability": round(transition_predictability, 4),
        "runtime_compactness": round(min(runtime_compactness, 3.0), 4),
        "execution_continuity": round(execution_continuity, 4),
        "layout_independence": round(layout_independence, 4),
        "selector_independence": round(selector_independence, 4),
    }


def validate_operational_transition(graph: OperationalRuntimeGraph, frm: str, to: str) -> bool:
    for t in graph.transitions:
        if t.from_operational_state == frm and t.to_operational_state == to:
            return True
    return False


def find_recovery_edge(graph: OperationalRuntimeGraph, symptom_substr: str) -> Optional[OperationalRuntimeRecoveryEdge]:
    needle = symptom_substr.lower().strip()
    if not needle:
        return None
    for r in graph.recovery_edges:
        if needle in r.symptom_state.lower() or needle in r.recovery_class.lower():
            return r
    return None


def predict_next_operational_state(
    graph: OperationalRuntimeGraph,
    current_operational_state: str,
) -> List[str]:
    outs: List[str] = []
    for t in graph.transitions:
        if t.from_operational_state == current_operational_state:
            outs.append(t.to_operational_state)
    return list(dict.fromkeys(outs))


def execute_operational_graph_shadow(
    graph: OperationalRuntimeGraph,
    *,
    start_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Simula un recorrido lineal esperado sobre el grafo (sin efectos laterales externos)."""

    if not graph.nodes:
        return {
            "ok": False,
            "reason": "empty_graph",
            "path_states": [],
            "snapshots": [],
        }

    seq_states = [n.operational_state for n in graph.nodes]
    first = start_state or seq_states[0]
    if first not in seq_states:
        first = seq_states[0]

    snapshots: List[OperationalRuntimeState] = []
    path: List[str] = []
    idx_anchor = seq_states.index(first)
    for i in range(idx_anchor, len(seq_states)):
        cur_s = seq_states[i]
        path.append(cur_s)
        snapshots.append(
            OperationalRuntimeState(
                state_id=cur_s,
                node_id=graph.nodes[i].node_id,
                cumulative_progress=round((i + 1) / max(len(seq_states), 1), 4),
                evidence={
                    "expected_validators": graph.nodes[i].expected_validators[:6],
                    "anchors": graph.nodes[i].replay_anchor_candidates[:6],
                },
            ),
        )
        if i + 1 < len(seq_states):
            nxt = seq_states[i + 1]
            if not validate_operational_transition(graph, cur_s, nxt):
                return {
                    "ok": False,
                    "reason": "missing_transition",
                    "gap": (cur_s, nxt),
                    "path_states": path,
                    "snapshots": [s.model_dump() for s in snapshots],
                }

    predicted_tail = predict_next_operational_state(graph, path[-1]) if path else []

    return {
        "ok": True,
        "path_states": path,
        "predicted_optional_branches_from_terminal": predicted_tail,
        "snapshots": [s.model_dump() for s in snapshots],
        "metrics_echo": dict(graph.metrics or {}),
    }


def compare_org_vs_sep_runtime(
    graph: OperationalRuntimeGraph,
    mission: Mission,
) -> Dict[str, Any]:
    """Contrasta la secuencia ORG coarse vs tipos SEP (ambos son declarativos, no raw replay)."""

    sep_blob = getattr(mission, "semantic_execution_plan", None)
    sep_types = []
    if isinstance(sep_blob, dict):
        for s in sep_blob.get("steps") or []:
            if isinstance(s, dict):
                sep_types.append(str(s.get("type") or ""))

    mapped_sep_states = [_SEP_TYPE_OPERATIONAL_STATES.get(t, f"sep::{t}") for t in sep_types]
    org_states = [n.operational_state for n in graph.nodes]

    agreement = []
    upto = min(len(org_states), len(mapped_sep_states))
    for i in range(upto):
        agreement.append(org_states[i] == mapped_sep_states[i])

    return {
        "org_state_count": len(org_states),
        "sep_step_count": len(sep_types),
        "paired_agreement_ratio": round(sum(bool(x) for x in agreement) / max(len(agreement), 1), 4)
        if agreement
        else None,
        "org_states": org_states[:64],
        "sep_coarse_buckets": mapped_sep_states[:64],
        "replay_linearity_residual": round(len(set(sep_types)) / max(len(sep_types), 1), 4)
        if sep_types
        else 0.0,
    }


def attach_org_audit(mission: Mission, audit_blob: Dict[str, Any]) -> None:
    mission.org_audit = dict(audit_blob)


def refresh_operational_shadow(
    mission: Mission,
    settings: Optional[Any] = None,
) -> OperationalRuntimeGraph:
    """Conveniencia Mission Review — persiste ORG en la misión y adjunta auditoría."""

    blank = OperationalRuntimeGraph(
        version="1.0",
        shadows_runtime=True,
        build_sources_used=[],
        metrics={
            "replay_dependency_score": 0.0,
            "operational_resilience_score": 0.0,
        },
    )

    if not _settings_org_enabled(settings):
        attach_org_audit(
            mission,
            {
                "ok": False,
                "reason": "ORG_SHADOW_DISABLED",
                "comparison": {},
                "shadow_run": {},
            },
        )
        return blank

    try:
        graph = build_operational_runtime_graph(mission)
        cmp_res = compare_org_vs_sep_runtime(graph, mission)
        shadow = execute_operational_graph_shadow(graph)
        resilience = dict(graph.metrics or {})
        resilience["replay_independence_estimate"] = round(
            max(0.0, min(1.0, 1.0 - resilience.get("replay_dependency_score", 0.0))),
            4,
        )

        audit: Dict[str, Any] = {
            "ok": True,
            "version": graph.version,
            "sources": graph.build_sources_used,
            "comparison": cmp_res,
            "shadow_run": shadow,
            "resilience_scores": resilience,
            "recovery_edge_total": len(graph.recovery_edges),
            "predicted_tail": shadow.get("predicted_optional_branches_from_terminal"),
        }
        attach_org_audit(mission, audit)
        if graph.nodes:
            mission.operational_runtime_graph = graph.model_dump(mode="json")
            mission.org_shadow_mode = True
        else:
            mission.operational_runtime_graph = None
            mission.org_shadow_mode = False

        log.debug(
            "[ORG] shadow refresh nodes=%s transitions=%s goals=%s",
            len(graph.nodes),
            len(graph.transitions),
            len(graph.goals),
        )
        return graph if graph.nodes else blank
    except Exception as exc:  # pragma: no cover - defensive UX
        log.debug(f"[ORG] refresh failed: {exc}")
        attach_org_audit(
            mission,
            {"ok": False, "reason": str(exc)},
        )
        return blank


__all__ = [
    "attach_org_audit",
    "build_operational_runtime_graph",
    "compare_org_vs_sep_runtime",
    "execute_operational_graph_shadow",
    "find_recovery_edge",
    "predict_next_operational_state",
    "refresh_operational_shadow",
    "validate_operational_transition",
]
