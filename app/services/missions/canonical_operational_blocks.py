"""Canonical Operational Blocks (COB) — Session Promotion Layer, Fase 1 shadow.

Genera bloques operacionales portables paralelos al SEP; no ejecuta ni reemplaza SIPE/replay.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from app.contracts.mission import (
    CanonicalOperationalBlock,
    CanonicalOperationalBlockStatus,
    Mission,
    OperationalIntent,
    SemanticSession,
    SemanticSessionStatus,
)
from app.core.logger import log

# Alineado con semantic_session_engine (strings estables; sin import circular).
SESSION_LAUNCHER = "launcher_session"
SESSION_NAVIGATION = "navigation_session"
SESSION_SEARCH = "search_session"
SESSION_SELECTION = "selection_session"
SESSION_FORM_FILL = "form_fill_session"

_SESSION_TO_CANONICAL = {
    SESSION_LAUNCHER: "open_application",
    SESSION_NAVIGATION: "navigate_to_site",
    SESSION_SEARCH: "search_content_and_browse_results",
    SESSION_SELECTION: "select_visible_entity",
    SESSION_FORM_FILL: "fill_and_submit_form",
}

COB_TO_SEP_TYPES: Dict[str, Set[str]] = {
    "open_application": {"open_app", "launch_app"},
    "open_new_tab": {"open_new_tab"},
    "navigate_to_site": {"open_url", "open_site", "open_new_tab", "navigate_or_search"},
    "search_content_and_browse_results": {
        "search_youtube",
        "search_site",
        "search_web",
        "search_content",
        "submit_search",
    },
    "select_visible_entity": {"select_profile", "open_search_result", "use_selected_profile", "select_option"},
    "fill_and_submit_form": {"type_text", "set_field_value"},
}


def derive_replay_strategy(
    canonical_action: str,
    session_type: str,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Propone estrategia de replay abstracta (sin ejecutar)."""
    ctx = dict(context or {})
    mode = "semantic"
    components: List[str] = []
    primary = ""

    if canonical_action == "navigate_to_site":
        mode = "hybrid"
        primary = "hotkey_ctrl_l + semantic_url_navigation"
        components = ["hotkey", "semantic"]
    elif canonical_action == "open_application":
        mode = "hybrid"
        primary = "system_launch_shell + semantic_query + confirm_selection"
        components = ["uia_first", "semantic", "visual_anchor"]
    elif canonical_action == "search_content_and_browse_results":
        mode = "hybrid"
        primary = "dom_search_field + semantic_query + scroll_until_results_stable"
        components = ["dom_first", "semantic", "visual_anchor"]
    elif canonical_action == "select_visible_entity":
        mode = "hybrid"
        primary = "semantic_visible_choice + uia_or_dom_activate"
        components = ["semantic", "uia_first", "dom_first"]
    elif canonical_action == "fill_and_submit_form":
        mode = "hybrid"
        primary = "semantic_field_sequence + validate_submit"
        components = ["dom_first", "uia_first", "semantic"]
    elif canonical_action == "open_new_tab":
        mode = "semantic"
        primary = "browser_hotkey_new_tab_then_foreground_validate"
        components = ["hotkey", "semantic"]

    return {
        "mode": mode,
        "primary": primary,
        "components": components,
        "session_type": session_type,
        "context_hints": {k: ctx[k] for k in list(ctx)[:8]},
    }


def derive_repair_strategy(canonical_action: str) -> Dict[str, Any]:
    """Pasos de reparación semánticos (no por índice de keypress)."""
    phases: List[str] = []
    if canonical_action == "navigate_to_site":
        phases = [
            "re_focus_address_or_omnibox",
            "re_validate_url_or_search_text",
            "confirm_navigation_finished",
        ]
    elif canonical_action == "open_new_tab":
        phases = ["re_invoke_new_tab", "confirm_foreground_browser"]
    elif canonical_action == "open_application":
        phases = [
            "re_open_system_launch_surface",
            "re_enter_launch_query",
            "re_confirm_target_application_launched",
        ]
    elif canonical_action == "search_content_and_browse_results":
        phases = [
            "re_confirm_query_semantics",
            "relocate_search_field_by_role_or_anchor",
            "validate_results_loaded",
            "adjust_scroll_until_results_meaningfully_visible",
        ]
    elif canonical_action == "select_visible_entity":
        phases = [
            "re_resolve_visible_choice_label",
            "re_anchor_list_or_menu_container",
            "confirm_selection_applied",
        ]
    elif canonical_action == "fill_and_submit_form":
        phases = [
            "re_identify_field_roles",
            "re_apply_values_with_validation",
            "confirm_submit_or_implicit_commit",
        ]
    else:
        phases = ["re_validate_operational_goal", "re_resolve_primary_anchor"]

    return {
        "granularity": "semantic_block",
        "phases": phases,
        "avoid": ["replay_keypress_index", "replay_raw_coordinates_only"],
    }


def _hypothesis_type_count(sess: SemanticSession) -> int:
    snap = sess.semantic_ambiguity_snapshot or {}
    types = snap.get("hypothesis_types")
    if isinstance(types, list):
        return len(types)
    return 0


def _merge_truth_from_trace(mission: Mission, sess: SemanticSession) -> Dict[str, Any]:
    """Enriquece execution truth desde metadata del último evento de la sesión."""
    out = dict(sess.execution_truth_snapshot or {})
    want = set(sess.source_event_ids or [])
    for ev in reversed(mission.raw_trace or []):
        if ev.id not in want:
            continue
        meta = dict(ev.metadata or {})
        iso = meta.get("target_identity_isolation") or {}
        cc = meta.get("capture_contract") or {}
        out.setdefault(
            "trusted_pre_action_identity",
            bool(iso.get("trusted_pre_action_identity")),
        )
        out.setdefault("capture_complete", bool(cc.get("capture_complete")))
        out.setdefault(
            "sufficient_for_execution",
            bool(cc.get("sufficient_for_execution")),
        )
        break
    return out


def execution_truth_confirmed(snap: Dict[str, Any]) -> bool:
    if bool(snap.get("trusted_pre_action_identity")):
        return True
    if bool(snap.get("capture_complete")) and bool(snap.get("sufficient_for_execution")):
        return True
    return False


def _promotion_gate_scores(
    mission: Mission,
    sess: SemanticSession,
    merged_truth: Dict[str, Any],
) -> Dict[str, float]:
    """Scores 0–1 por criterio de promoción."""
    hyp_n = _hypothesis_type_count(sess)
    ambiguity_low = max(0.0, 1.0 - min(1.0, hyp_n / 6.0))

    stable = 1.0 if sess.status == SemanticSessionStatus.COMPLETED else 0.35
    if sess.status == SemanticSessionStatus.ABANDONED:
        stable = 0.25

    continuity = min(1.0, (sess.absorbed_events_count or 0) / 5.0)
    if (sess.absorbed_events_count or 0) >= 2:
        continuity = max(continuity, 0.55)

    truth_ok = 1.0 if execution_truth_confirmed(merged_truth) else 0.45

    intents = [i for i in (mission.operational_intents or []) if i.session_id == sess.id]
    intent_persistent = 1.0 if intents else 0.4

    outcome_ok = 1.0 if (sess.completion_signals or intents) else 0.35

    tgt = sess.related_targets or []
    strong_identity = 1.0 if tgt else (0.72 if truth_ok > 0.5 else 0.4)

    overall = (
        0.22 * truth_ok
        + 0.18 * ambiguity_low
        + 0.18 * stable
        + 0.14 * continuity
        + 0.12 * intent_persistent
        + 0.08 * outcome_ok
        + 0.08 * strong_identity
    )
    return {
        "truth_ok": truth_ok,
        "ambiguity_low": ambiguity_low,
        "stable": stable,
        "continuity": continuity,
        "intent_persistent": intent_persistent,
        "outcome_ok": outcome_ok,
        "strong_identity": strong_identity,
        "overall": min(1.0, overall),
    }


def score_cob(cob: CanonicalOperationalBlock) -> Tuple[float, float]:
    """Calcula stability_score y portability_score para un COB."""
    truth_snap = cob.execution_truth_snapshot or {}
    confirmed = execution_truth_confirmed(truth_snap)

    amb = cob.ambiguity or {}
    amb_density = float(amb.get("hypothesis_type_count") or 0)
    amb_penalty = min(1.0, amb_density / 8.0)

    absorbed = max(1, int(cob.absorbed_steps or 1))
    semantic_density = min(1.0, 0.35 + 0.12 * min(absorbed, 8))

    carrier_quality = 0.75
    if cob.canonical_action in (
        "navigate_to_site",
        "search_content_and_browse_results",
        "open_application",
        "open_new_tab",
    ):
        carrier_quality = 0.82

    stability = (
        (0.28 if confirmed else 0.12)
        + 0.22 * max(0.0, 1.0 - amb_penalty)
        + 0.18 * min(1.0, cob.confidence)
        + 0.14 * semantic_density
        + 0.18 * carrier_quality
    )
    stability = max(0.0, min(1.0, stability))

    # Portabilidad: acciones semánticas genéricas + ausencia de hints frágiles.
    base_port = {
        "open_application": 0.78,
        "navigate_to_site": 0.85,
        "search_content_and_browse_results": 0.80,
        "select_visible_entity": 0.72,
        "fill_and_submit_form": 0.68,
    }.get(cob.canonical_action, 0.55)

    params = cob.params or {}
    fragile = 0.0
    if params.get("absolute_coords_only"):
        fragile += 0.25
    portability = max(0.0, min(1.0, base_port - fragile + 0.05 * (1.0 - amb_penalty)))
    return stability, portability


def merge_cobs(a: CanonicalOperationalBlock, b: CanonicalOperationalBlock) -> CanonicalOperationalBlock:
    """Fusiona dos COB del mismo objetivo operacional (adjacencia lógica)."""
    src_events = list(dict.fromkeys((a.source_event_ids or []) + (b.source_event_ids or [])))
    src_hyps = list(dict.fromkeys((a.source_hypothesis_ids or []) + (b.source_hypothesis_ids or [])))
    src_sess = list(dict.fromkeys((a.source_session_ids or []) + (b.source_session_ids or [])))
    merged = a.model_copy(deep=True)
    merged.source_event_ids = src_events
    merged.source_hypothesis_ids = src_hyps
    merged.source_session_ids = src_sess
    merged.absorbed_steps = int(a.absorbed_steps or 0) + int(b.absorbed_steps or 0)
    merged.completion_signals = list(
        dict.fromkeys((a.completion_signals or []) + (b.completion_signals or [])),
    )
    merged.expected_outcomes = list(
        dict.fromkeys((a.expected_outcomes or []) + (b.expected_outcomes or [])),
    )
    merged.confidence = min(1.0, (float(a.confidence) + float(b.confidence)) / 2.0 + 0.05)
    merged.status = CanonicalOperationalBlockStatus.PROPOSED
    merged.notes = (merged.notes or "") + " | merged_adjacent_block"
    st, pt = score_cob(merged)
    merged.stability_score = st
    merged.portability_score = pt
    return merged


def _primary_intent_for_session(
    mission: Mission,
    sess: SemanticSession,
) -> Tuple[str, Optional[OperationalIntent]]:
    intents = [i for i in (mission.operational_intents or []) if i.session_id == sess.id]
    if not intents:
        return "", None
    preferred = {
        SESSION_LAUNCHER: "launch_via_system_shell",
        SESSION_NAVIGATION: "navigate_to_site",
        SESSION_SEARCH: "submit_search_query",
        SESSION_SELECTION: "activate_visible_choice",
        SESSION_FORM_FILL: "complete_multi_field_form",
    }.get(sess.session_type)
    for i in intents:
        if preferred and i.intent_type == preferred:
            return i.id, i
    return intents[0].id, intents[0]


def promote_session_to_cob(mission: Mission, sess: SemanticSession) -> Optional[CanonicalOperationalBlock]:
    """Promueve una sesión cerrada a COB si ``cob_shadow_mode`` está activo."""
    if not getattr(mission, "cob_shadow_mode", False):
        return None

    canonical = _SESSION_TO_CANONICAL.get(sess.session_type)
    if not canonical:
        return None

    if any(c.session_id == sess.id for c in (mission.canonical_operational_blocks or [])):
        return None

    merged_truth = _merge_truth_from_trace(mission, sess)
    gates = _promotion_gate_scores(mission, sess, merged_truth)

    intent_id, intent_obj = _primary_intent_for_session(mission, sess)
    params: Dict[str, Any] = {}
    if intent_obj and intent_obj.params:
        params.update(intent_obj.params)

    hyp_ids = list(sess.hypothesis_ids or [])
    amb_snapshot = dict(sess.semantic_ambiguity_snapshot or {})
    amb_snapshot["hypothesis_type_count"] = _hypothesis_type_count(sess)

    cob = CanonicalOperationalBlock(
        session_id=sess.id,
        operational_intent_id=intent_id or "",
        block_type=sess.session_type,
        capability_id=intent_obj.capability_id if intent_obj else None,
        canonical_action=canonical,
        params=params,
        confidence=min(1.0, max(sess.confidence, intent_obj.confidence if intent_obj else 0.0)),
        ambiguity={
            "session_ambiguity": amb_snapshot,
            "promotion_gates": gates,
        },
        execution_truth_snapshot=merged_truth,
        semantic_ambiguity_snapshot=dict(sess.semantic_ambiguity_snapshot or {}),
        source_event_ids=list(sess.source_event_ids or []),
        source_hypothesis_ids=hyp_ids,
        source_session_ids=[sess.id],
        absorbed_steps=int(sess.absorbed_events_count or 0),
        expected_outcomes=[sess.inferred_goal] if sess.inferred_goal else [],
        completion_signals=list(sess.completion_signals or []),
        replay_strategy=derive_replay_strategy(canonical, sess.session_type, sess.context),
        repair_strategy=derive_repair_strategy(canonical),
        execution_priority=min(90, 40 + int(20 * gates["overall"])),
        created_by="canonical_operational_blocks",
        notes="shadow_promotion",
    )

    st, pt = score_cob(cob)
    cob.stability_score = st
    cob.portability_score = pt

    if gates["overall"] >= 0.62 and sess.status == SemanticSessionStatus.COMPLETED:
        cob.status = CanonicalOperationalBlockStatus.ACCEPTED
    else:
        cob.status = CanonicalOperationalBlockStatus.PROPOSED

    return cob


def append_promoted_cob_for_session(mission: Mission, sess: SemanticSession) -> None:
    """Hook tras cerrar sesión: añade COB y refresca auditoría."""
    cob = promote_session_to_cob(mission, sess)
    if cob is None:
        return
    mission.canonical_operational_blocks.append(cob)
    try:
        mission.cob_audit = build_cob_audit(mission)
    except Exception as e:
        log.debug(f"build_cob_audit: {e}")


def compare_cobs_vs_sep(mission: Mission) -> Dict[str, Any]:
    """Compara COB frente al SEP: acuerdo, compresión, ruido estructural."""
    cobs = mission.canonical_operational_blocks or []
    sep = mission.semantic_execution_plan or {}
    steps = [s for s in (sep.get("steps") or []) if isinstance(s, dict)]
    sep_types = [str(s.get("type") or "").lower() for s in steps]

    raw_n = len(mission.raw_trace or [])
    cob_n = len(cobs)
    sep_n = len(sep_types)

    compression_vs_raw = round(raw_n / max(cob_n, 1), 4) if cob_n else 0.0
    compression_sep_vs_cob = round(sep_n / max(cob_n, 1), 4) if cob_n and sep_n else None

    sep_set = set(sep_types)
    matched = 0
    for cob in cobs:
        bucket = COB_TO_SEP_TYPES.get(cob.canonical_action, set())
        if bucket & sep_set:
            matched += 1

    agreement = round(matched / max(cob_n, 1), 4) if cob_n else 0.0

    noise_reduction = round(max(0.0, 1.0 - (sep_n / max(raw_n, 1))), 4) if raw_n else 0.0
    cob_noise_reduction = round(max(0.0, 1.0 - (cob_n / max(raw_n, 1))), 4) if raw_n else 0.0

    replay_simplification = round(max(cob_n, 1) / max(sep_n, 1), 4) if sep_n else None
    repair_simplification = round(max(cob_n, 1) / max(raw_n / 8.0, 1.0), 4) if raw_n else None

    return {
        "cob_count": cob_n,
        "sep_step_count": sep_n,
        "raw_event_count": raw_n,
        "agreement_rate": agreement,
        "matched_cobs_to_sep": matched,
        "compression_ratio_events_per_cob": compression_vs_raw,
        "compression_sep_steps_per_cob": compression_sep_vs_cob,
        "noise_reduction_vs_sep_density": noise_reduction,
        "structural_noise_reduction_cob_vs_raw": cob_noise_reduction,
        "replay_simplification_cob_vs_sep_steps": replay_simplification,
        "repair_simplification_semantic_vs_event_granularity": repair_simplification,
    }


def build_cob_audit(mission: Mission) -> Dict[str, Any]:
    """Resumen auditable para Mission Review experto."""
    cobs = mission.canonical_operational_blocks or []
    cmp_res = compare_cobs_vs_sep(mission)
    summaries = []
    for c in cobs:
        summaries.append({
            "canonical_action": c.canonical_action,
            "block_type": c.block_type,
            "confidence": c.confidence,
            "status": c.status.value,
            "stability_score": c.stability_score,
            "portability_score": c.portability_score,
            "absorbed_steps": c.absorbed_steps,
            "replay_mode": (c.replay_strategy or {}).get("mode"),
            "repair_phases_n": len((c.repair_strategy or {}).get("phases") or []),
        })
    return {
        "cob_shadow_mode": getattr(mission, "cob_shadow_mode", False),
        "cob_count": len(cobs),
        "blocks": summaries,
        "compare_vs_sep": cmp_res,
    }


def rebuild_cob_audit_only(mission: Mission) -> None:
    """Recalcula cob_audit sin mutar COB (p.ej. misión cargada desde disco)."""
    if not getattr(mission, "cob_shadow_mode", False) and not (mission.canonical_operational_blocks or []):
        mission.cob_audit = None
        return
    mission.cob_audit = build_cob_audit(mission)
