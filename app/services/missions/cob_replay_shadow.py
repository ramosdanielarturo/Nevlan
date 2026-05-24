"""COB Replay Shadow — simula ejecución por intención sin runtime real.

Construye ReplayPlan por COB y lo contrasta con SEP y grafo legacy (solo diagnóstico).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import (
    CanonicalOperationalBlock,
    CobReplayPlanStep,
    Mission,
)
from app.core.logger import log
from app.services.missions.canonical_operational_blocks import (
    COB_TO_SEP_TYPES,
    execution_truth_confirmed,
)


def _step(
    order: int,
    phase_id: str,
    label: str,
    primitive: str,
    *,
    params_keys: Optional[List[str]] = None,
    validation_hints: Optional[List[str]] = None,
) -> CobReplayPlanStep:
    return CobReplayPlanStep(
        order=order,
        phase_id=phase_id,
        label=label,
        primitive=primitive,
        params_keys=params_keys or [],
        validation_hints=validation_hints or [],
    )


def build_cob_replay_plan(cob: CanonicalOperationalBlock) -> List[CobReplayPlanStep]:
    """Deriva pasos operacionales finos que el COB usaría en un executor por intención."""
    action = cob.canonical_action or ""

    if action == "search_content_and_browse_results":
        return [
            _step(
                1,
                "focus_search_field",
                "Focus search field",
                "focus_target",
                params_keys=[],
                validation_hints=["anchor_search_role_or_semantic_field"],
            ),
            _step(
                2,
                "inject_semantic_query",
                "Inject semantic query",
                "inject_text",
                params_keys=["query_preview"],
                validation_hints=["non_empty_query"],
            ),
            _step(
                3,
                "submit_search",
                "Submit search intent",
                "confirm_or_submit",
                validation_hints=["enter_or_submit_control"],
            ),
            _step(
                4,
                "validate_results_loaded",
                "Validate results loaded",
                "validate_state",
                validation_hints=["results_surface_visible_or_dom_signal"],
            ),
            _step(
                5,
                "allow_browsing_state",
                "Allow browsing state (scroll / explore)",
                "permit_exploration",
                validation_hints=["optional_scroll_until_stable"],
            ),
        ]

    if action == "open_new_tab":
        return [
            _step(
                1,
                "invoke_new_tab_hotkey",
                "Abrir nueva pestaña (seguro Ctrl+T)",
                "hotkey_invoke",
                validation_hints=["browser_keyboard_shortcut_new_tab"],
            ),
            _step(
                2,
                "validate_tab_surface_ready",
                "Validar navegador responde (UI estable)",
                "validate_state",
                validation_hints=["foreground_browser_or_similar"],
            ),
        ]

    if action == "navigate_to_site":
        return [
            _step(
                1,
                "focus_address_or_omnibox",
                "Focus address bar / omnibox",
                "focus_target",
                validation_hints=["hotkey_or_semantic_address_control"],
            ),
            _step(
                2,
                "inject_navigation_text",
                "Inject URL or navigation text",
                "inject_text",
                params_keys=["url_preview", "navigation_url_hint"],
                validation_hints=["url_or_search_text_present"],
            ),
            _step(
                3,
                "confirm_navigation",
                "Confirm navigation",
                "confirm_or_submit",
                validation_hints=["navigation_finished_signal"],
            ),
            _step(
                4,
                "validate_destination_coherent",
                "Validate destination coherent with intent",
                "validate_state",
                validation_hints=["document_loaded_or_title_domain_hint"],
            ),
        ]

    if action == "open_application":
        return [
            _step(
                1,
                "open_system_launch_surface",
                "Open system launch surface",
                "invoke_launch_surface",
                validation_hints=["launcher_visible"],
            ),
            _step(
                2,
                "inject_launch_query",
                "Inject launch query",
                "inject_text",
                params_keys=["query_preview"],
                validation_hints=["target_app_hint_present"],
            ),
            _step(
                3,
                "confirm_launch_selection",
                "Confirm launch selection",
                "activate_choice",
                validation_hints=["selection_matches_intent"],
            ),
            _step(
                4,
                "validate_app_foreground",
                "Validate application foreground / ready",
                "validate_state",
                validation_hints=["process_or_window_context_stable"],
            ),
        ]

    if action == "select_visible_entity":
        return [
            _step(
                1,
                "resolve_visible_choice",
                "Resolve visible entity",
                "focus_target",
                params_keys=["label_hint"],
                validation_hints=["label_or_role_anchor"],
            ),
            _step(
                2,
                "activate_visible_choice",
                "Activate visible choice",
                "activate_choice",
                validation_hints=["keyboard_or_click_confirm"],
            ),
            _step(
                3,
                "validate_selection_applied",
                "Validate selection applied",
                "validate_state",
                validation_hints=["downstream_ui_changed"],
            ),
        ]

    if action == "fill_and_submit_form":
        return [
            _step(
                1,
                "enumerate_form_fields",
                "Enumerate form fields by role",
                "discover_targets",
                validation_hints=["fields_touched_or_semantic_roles"],
            ),
            _step(
                2,
                "inject_field_values",
                "Inject field values",
                "inject_text",
                validation_hints=["values_match_recorded_intent"],
            ),
            _step(
                3,
                "submit_or_commit_form",
                "Submit or implicit commit",
                "confirm_or_submit",
                validation_hints=["submit_control_or_enter_context"],
            ),
            _step(
                4,
                "validate_post_submit_state",
                "Validate post-submit state",
                "validate_state",
                validation_hints=["success_signal_or_next_surface"],
            ),
        ]

    return [
        _step(1, "resolve_goal", "Resolve operational goal", "discover_targets"),
        _step(2, "execute_generic_intent", "Execute generic intent", "generic_block"),
        _step(3, "validate_outcome", "Validate outcome", "validate_state"),
    ]


def assess_operational_readiness(
    cob: CanonicalOperationalBlock,
    steps: List[CobReplayPlanStep],
) -> Tuple[float, List[str]]:
    """¿Hay suficiente información operacional o solo un resumen bonito?"""
    gaps: List[str] = []
    params = cob.params or {}
    truth = cob.execution_truth_snapshot or {}

    def _has_any(keys: List[str]) -> bool:
        return any(params.get(k) for k in keys if params.get(k) not in (None, "", []))

    for st in steps:
        for pk in st.params_keys:
            if pk not in params or params.get(pk) in (None, "", []):
                gaps.append(f"missing_param:{pk}:{st.phase_id}")

    if cob.canonical_action == "search_content_and_browse_results":
        if not _has_any(["query_preview"]):
            gaps.append("missing_semantic_query_for_search")

    if cob.canonical_action == "navigate_to_site":
        if not _has_any(["url_preview", "navigation_url_hint", "typed_url"]):
            gaps.append("missing_navigation_target_text")

    if cob.canonical_action == "open_application":
        if not _has_any(["query_preview"]):
            gaps.append("missing_launch_query_preview")

    if cob.canonical_action == "select_visible_entity":
        if not _has_any(["label_hint"]) and not truth.get("trusted_pre_action_identity"):
            gaps.append("weak_visible_entity_anchor")

    if truth and not execution_truth_confirmed(truth):
        gaps.append("execution_truth_not_confirmed")

    base = 0.35 + 0.35 * float(cob.confidence or 0.0) + 0.2 * float(cob.stability_score or 0.0)
    penalty = min(0.55, 0.07 * len(set(gaps)))
    readiness = max(0.0, min(1.0, base - penalty))
    if not gaps and readiness < 0.72:
        readiness = min(1.0, readiness + 0.08)
    return readiness, sorted(set(gaps))


def compare_cob_replay_vs_sep_and_legacy(
    mission: Mission,
    plan_summaries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Contrasta granularidad intent-driven vs SEP y grafo compilado."""
    micro_total = sum(len(p.get("steps") or []) for p in plan_summaries)
    cob_n = len(plan_summaries)

    sep = mission.semantic_execution_plan or {}
    sep_steps = [s for s in (sep.get("steps") or []) if isinstance(s, dict)]
    sep_types = [str(s.get("type") or "").lower() for s in sep_steps]
    sep_n = len(sep_types)

    compiled = mission.compiled_execution_graph or []
    compiled_n = len(compiled)
    raw_n = len(mission.raw_trace or [])

    sep_set = set(sep_types)
    aligned = 0
    for p in plan_summaries:
        ca = str(p.get("canonical_action") or "")
        if COB_TO_SEP_TYPES.get(ca, set()) & sep_set:
            aligned += 1

    readiness_vals = [float(p.get("operational_readiness") or 0.0) for p in plan_summaries]
    avg_readiness = round(sum(readiness_vals) / max(len(readiness_vals), 1), 4) if readiness_vals else 0.0

    intent_vs_legacy = (
        "cob_micro_lt_compiled"
        if micro_total and compiled_n and micro_total < compiled_n
        else "mixed_or_unknown"
    )

    return {
        "cob_count": cob_n,
        "cob_replay_micro_steps_total": micro_total,
        "micro_steps_per_cob_avg": round(micro_total / max(cob_n, 1), 4) if cob_n else 0.0,
        "sep_step_count": sep_n,
        "compiled_graph_step_count": compiled_n,
        "interpreted_step_count": len(mission.interpreted_steps or []),
        "raw_event_count": raw_n,
        "sep_alignment_count": aligned,
        "sep_alignment_rate": round(aligned / max(cob_n, 1), 4) if cob_n else 0.0,
        "sep_to_micro_ratio": round(sep_n / max(micro_total, 1), 4) if micro_total else None,
        "compiled_to_micro_ratio": round(compiled_n / max(micro_total, 1), 4) if micro_total else None,
        "average_operational_readiness": avg_readiness,
        "intent_driven_vs_event_legacy_hint": intent_vs_legacy,
    }


def simulate_mission_cob_replay_shadow(
    mission: Mission,
    *,
    persist: bool = True,
) -> Dict[str, Any]:
    """Simula toda la cadena COB → ReplayPlan y benchmark vs SEP/legacy."""
    cobs = mission.canonical_operational_blocks or []
    plans_out: List[Dict[str, Any]] = []

    for cob in cobs:
        try:
            steps = build_cob_replay_plan(cob)
            readiness, gaps = assess_operational_readiness(cob, steps)
            plans_out.append({
                "cob_id": cob.id,
                "canonical_action": cob.canonical_action,
                "session_id": cob.session_id,
                "operational_readiness": round(readiness, 4),
                "gaps": gaps,
                "steps": [s.model_dump(mode="json") for s in steps],
            })
        except Exception as e:
            log.debug(f"cob_replay_shadow plan build: {e}")
            plans_out.append({
                "cob_id": getattr(cob, "id", ""),
                "canonical_action": getattr(cob, "canonical_action", ""),
                "session_id": getattr(cob, "session_id", ""),
                "operational_readiness": 0.0,
                "gaps": ["plan_build_failed"],
                "steps": [],
                "error": str(e),
            })

    comparison = compare_cob_replay_vs_sep_and_legacy(mission, plans_out)

    snapshot = {
        "version": 1,
        "generated_by": "cob_replay_shadow",
        "shadow_only": True,
        "plans": plans_out,
        "comparison": comparison,
    }
    if persist:
        mission.cob_replay_shadow = snapshot
    return snapshot


def rebuild_cob_replay_shadow_only(mission: Mission) -> None:
    """Recalcula snapshot si hay COB; si no, limpia."""
    if not (mission.canonical_operational_blocks or []):
        mission.cob_replay_shadow = None
        return
    try:
        simulate_mission_cob_replay_shadow(mission, persist=True)
    except Exception as e:
        log.debug(f"rebuild_cob_replay_shadow_only: {e}")
