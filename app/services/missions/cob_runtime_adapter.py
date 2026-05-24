"""COB Runtime Adapter — shadow-safe: traduce ReplayPlan a acciones del executor actual.

NO ejecuta input real, NO sustituye SmartExecutor. Solo produce un informe auditable.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from app.contracts.mission import (
    ActionStrategy,
    CanonicalOperationalBlock,
    CobReplayPlanStep,
    Mission,
)
from app.core.logger import log
from app.services.missions.cob_replay_shadow import build_cob_replay_plan
from app.services.missions.uic_capability_registry import (
    get_capability,
    validate_step_against_registry,
)

# Relación heurística: capability catalógica del SEP ↔ capabilities emitidas por el adapter COB.
_SEP_CAP_RELATED_ADAPTER_CAPS: Dict[str, FrozenSet[str]] = {
    "open_url": frozenset({
        "focus_browser_address_bar",
        "type_field_value",
        "submit_field",
        "validate_outcome",
        "open_url",
    }),
    "open_site": frozenset({
        "focus_browser_address_bar",
        "type_field_value",
        "validate_outcome",
        "open_site",
    }),
    "open_new_tab": frozenset({
        "open_new_tab",
        "wait_until",
    }),
    "open_app": frozenset({
        "open_app",
        "type_field_value",
        "select_visible_option",
        "wait_until",
        "focus_search_field",
    }),
    "search_site": frozenset({
        "focus_search_field",
        "type_field_value",
        "submit_field",
        "validate_outcome",
        "scroll_results",
        "search_site",
        "search_web",
    }),
    "search_content": frozenset({
        "focus_search_field",
        "type_field_value",
        "submit_field",
        "validate_outcome",
        "scroll_results",
        "search_content",
    }),
    "scroll_results": frozenset({"scroll_results", "scroll_page"}),
}
from app.services.missions.uic_contract_catalog import UIC_CONTRACT_ROWS

# Tokens que indican fallback basado en coordenadas (prohibido en shadow-safe).
_COORD_MARKERS: Tuple[str, ...] = (
    "coord",
    "coordinate",
    "absolute_xy",
    "pixels",
    "coords_absolute",
)


LIMITATIONS: Tuple[str, ...] = (
    "No se envían eventos Win32 ni pynput; shadow_only.",
    "SmartExecutor y bridges existentes no se invocan desde este módulo.",
    "Algunos primitives (p.ej. discover_targets) no tienen capacidad UIC cerrada.",
    "La cobertura SEP es heurística (capabilities catalógicas vs pasos SEP).",
)


def partition_coord_safe_fallbacks(
    strategies: Tuple[str, ...],
) -> Tuple[List[str], List[str]]:
    """Separa fallbacks seguros vs rechazados por riesgo de coords ciegas."""
    safe: List[str] = []
    unsafe: List[str] = []
    for s in strategies:
        sl = s.lower()
        if any(m in sl for m in _COORD_MARKERS):
            unsafe.append(s)
        else:
            safe.append(s)
    return safe, unsafe


def _norm_params(cob: CanonicalOperationalBlock) -> Dict[str, Any]:
    p = dict(cob.params or {})
    out = dict(p)
    qp = str(p.get("query_preview") or "").strip()
    if qp and not str(out.get("query") or "").strip():
        out["query"] = qp
    up = str(p.get("url_preview") or "").strip()
    if up and not str(out.get("url") or "").strip():
        out["url"] = up
    nh = str(p.get("navigation_url_hint") or "").strip()
    if nh and not out.get("url"):
        out["url"] = nh
    if qp and not str(out.get("app") or "").strip():
        out["app"] = qp
    lh = str(p.get("label_hint") or "").strip()
    if lh and not out.get("profile_name"):
        out["profile_name"] = lh
    return out


def _validator_token(cap: Optional[Any]) -> str:
    if cap is None:
        return "none"
    fn = getattr(cap.outcome_validator, "__name__", "unknown")
    return str(fn)


def _routing_capability_id(cob_action: str, phase_id: str) -> Optional[str]:
    key = (cob_action or "", phase_id or "")
    table: Dict[Tuple[str, str], str] = {
        ("open_new_tab", "invoke_new_tab_hotkey"): "open_new_tab",
        ("open_new_tab", "validate_tab_surface_ready"): "wait_until",
        ("navigate_to_site", "focus_address_or_omnibox"): "focus_browser_address_bar",
        ("navigate_to_site", "inject_navigation_text"): "type_field_value",
        ("navigate_to_site", "confirm_navigation"): "submit_field",
        ("navigate_to_site", "validate_destination_coherent"): "validate_outcome",
        ("search_content_and_browse_results", "focus_search_field"): "focus_search_field",
        ("search_content_and_browse_results", "inject_semantic_query"): "type_field_value",
        ("search_content_and_browse_results", "submit_search"): "submit_field",
        ("search_content_and_browse_results", "validate_results_loaded"): "validate_outcome",
        ("search_content_and_browse_results", "allow_browsing_state"): "scroll_results",
        ("open_application", "open_system_launch_surface"): "open_app",
        ("open_application", "inject_launch_query"): "type_field_value",
        ("open_application", "confirm_launch_selection"): "select_visible_option",
        ("open_application", "validate_app_foreground"): "wait_until",
        ("select_visible_entity", "resolve_visible_choice"): "select_visible_option",
        ("select_visible_entity", "activate_visible_choice"): "select_visible_option",
        ("select_visible_entity", "validate_selection_applied"): "validate_outcome",
        ("fill_and_submit_form", "enumerate_form_fields"): "self_heal_target",
        ("fill_and_submit_form", "inject_field_values"): "type_field_value",
        ("fill_and_submit_form", "submit_or_commit_form"): "submit_field",
        ("fill_and_submit_form", "validate_post_submit_state"): "validate_outcome",
    }
    return table.get(key)


def _executor_action_for(
    cob_action: str,
    cap_id: Optional[str],
    phase_id: str,
) -> str:
    if cap_id == "open_app":
        return ActionStrategy.LAUNCH_APP.value
    if cap_id == "focus_browser_address_bar":
        return ActionStrategy.SEND_HOTKEY.value
    if cap_id == "type_field_value":
        return ActionStrategy.TYPE_TEXT.value
    if cap_id == "submit_field":
        if cob_action == "search_content_and_browse_results" and phase_id == "submit_search":
            return ActionStrategy.SUBMIT_SEARCH.value
        return ActionStrategy.SEND_HOTKEY.value
    if cap_id in {"validate_outcome", "wait_until"}:
        return ActionStrategy.WAIT_FOR_STATE.value
    if cap_id == "scroll_results":
        return ActionStrategy.SCROLL.value
    if cap_id == "open_new_tab":
        return ActionStrategy.OPEN_NEW_TAB.value
    if cap_id == "focus_search_field":
        return ActionStrategy.CLICK.value
    if cap_id == "select_visible_option":
        return ActionStrategy.CLICK.value
    if cap_id == "self_heal_target":
        return "UNSUPPORTED_SHADOW_SCAN"
    return ActionStrategy.WAIT_FOR_STATE.value


def _required_params_for_step(
    cob: CanonicalOperationalBlock,
    step: CobReplayPlanStep,
    cap_id: Optional[str],
    norm: Dict[str, Any],
) -> Dict[str, Any]:
    ca = cob.canonical_action or ""
    pid = step.phase_id or ""

    if cap_id == "type_field_value":
        if ca == "navigate_to_site" and pid == "inject_navigation_text":
            u = str(norm.get("url") or "").strip()
            return {"text": u, "url": u} if u else {}
        if ca == "search_content_and_browse_results":
            q = str(norm.get("query") or "").strip()
            return {"text": q, "query": q} if q else {}
        if ca == "open_application":
            a = str(norm.get("app") or norm.get("query_preview") or "").strip()
            return {"text": a, "app": a} if a else {}
        if ca == "fill_and_submit_form":
            q = str(norm.get("query") or norm.get("text") or "").strip()
            return {"text": q} if q else {}

    if cap_id == "open_app":
        if pid == "open_system_launch_surface":
            return {"method": "windows_search", "intent": "launch_surface"}
        a = str(norm.get("app") or "").strip()
        return {"app": a} if a else {}

    if cap_id == "select_visible_option":
        lbl = str(norm.get("label_hint") or norm.get("profile_name") or "").strip()
        return {"label": lbl, "profile_name": lbl} if lbl else {}

    if cap_id in {"validate_outcome", "wait_until"}:
        return {"expect": "post_condition", "signals": list(cob.completion_signals or [])}

    if cap_id == "scroll_results":
        return {"direction": "vertical", "context": "results_browsing"}

    if cap_id == "focus_browser_address_bar":
        return {"combo": ["ctrl", "l"], "semantic": "focus_address_bar"}

    if cap_id == "submit_field" and ca == "navigate_to_site":
        return {"keys": ["enter"], "semantic": "confirm_navigation"}

    if cap_id == "submit_field" and ca == "search_content_and_browse_results":
        return {"keys": ["enter"], "semantic": "submit_search"}

    return {}


def translate_cob_replay_step(
    cob: CanonicalOperationalBlock,
    step: CobReplayPlanStep,
) -> Dict[str, Any]:
    """Traduce un paso del ReplayPlan COB a una fila executor-compatible (shadow)."""
    norm = _norm_params(cob)
    cap_id = _routing_capability_id(cob.canonical_action or "", step.phase_id or "")
    safety_flags = [
        "shadow_safe_no_execution",
        "no_smart_executor_dispatch",
        "no_blind_coords",
    ]

    unsupported_reason: Optional[str] = None
    readiness = 0.82

    # Primitive sin mapping UIC cerrado
    if step.primitive == "discover_targets" or (
        cob.canonical_action == "fill_and_submit_form" and step.phase_id == "enumerate_form_fields"
    ):
        return {
            "plan_step_order": step.order,
            "phase_id": step.phase_id,
            "label": step.label,
            "primitive": step.primitive,
            "executor_action": "UNSUPPORTED_SHADOW",
            "uic_capability_id": None,
            "required_params": {},
            "strategy": {"primary": None, "safe_fallbacks": []},
            "validator": "none",
            "fallback_policy": {
                "primary": None,
                "safe_fallbacks": [],
                "rejected_coord_fallbacks": [],
            },
            "safety_flags": safety_flags + ["unsupported_primitive"],
            "readiness": 0.0,
            "unsupported_reason": "primitive_requires_runtime_scan_without_uic_closure",
        }

    if cap_id is None:
        return {
            "plan_step_order": step.order,
            "phase_id": step.phase_id,
            "label": step.label,
            "primitive": step.primitive,
            "executor_action": "UNSUPPORTED_SHADOW",
            "uic_capability_id": None,
            "required_params": {},
            "strategy": {"primary": None, "safe_fallbacks": []},
            "validator": "none",
            "fallback_policy": {
                "primary": None,
                "safe_fallbacks": [],
                "rejected_coord_fallbacks": [],
            },
            "safety_flags": safety_flags + ["unmapped_phase"],
            "readiness": 0.0,
            "unsupported_reason": "no_uic_route_for_phase",
        }

    cap = get_capability(cap_id)
    executor_action = _executor_action_for(
        cob.canonical_action or "", cap_id, step.phase_id or "",
    )

    if executor_action == "UNSUPPORTED_SHADOW_SCAN":
        return {
            "plan_step_order": step.order,
            "phase_id": step.phase_id,
            "label": step.label,
            "primitive": step.primitive,
            "executor_action": executor_action,
            "uic_capability_id": cap_id,
            "required_params": {},
            "strategy": {"primary": None, "safe_fallbacks": []},
            "validator": _validator_token(cap),
            "fallback_policy": {
                "primary": None,
                "safe_fallbacks": [],
                "rejected_coord_fallbacks": [],
            },
            "safety_flags": safety_flags + ["unsupported_shadow_scan"],
            "readiness": 0.15,
            "unsupported_reason": "enumerate_form_fields_not_shadow_executable",
        }

    required_params = _required_params_for_step(cob, step, cap_id, norm)

    # Reglas de soporte: params críticos
    if cap_id == "type_field_value":
        if cob.canonical_action == "navigate_to_site" and not str(
            required_params.get("text") or "",
        ).strip():
            unsupported_reason = "MISSING_URL_OR_NAVIGATION_TEXT"
            readiness = 0.0
        elif cob.canonical_action == "search_content_and_browse_results" and not str(
            required_params.get("query") or "",
        ).strip():
            unsupported_reason = "MISSING_QUERY"
            readiness = 0.0
        elif cob.canonical_action == "open_application" and step.phase_id == "inject_launch_query":
            if not str(required_params.get("text") or "").strip():
                unsupported_reason = "MISSING_LAUNCH_QUERY"
                readiness = 0.0

    if cap_id == "select_visible_option" and cob.canonical_action == "open_application":
        if not str(required_params.get("label") or "").strip():
            unsupported_reason = "MISSING_SELECTION_LABEL"
            readiness = 0.55

    # Validador UIC sobre pseudo-paso SEP-shaped
    pseudo_type = cap_id
    pseudo_params = dict(required_params)
    if cap_id == "type_field_value" and "query" in pseudo_params:
        pseudo_params.setdefault("query", pseudo_params.get("query"))
    ve = validate_step_against_registry({
        "type": pseudo_type,
        "params": pseudo_params,
        "needs_user_label": False,
    })
    if ve and readiness > 0:
        unsupported_reason = ve
        readiness = min(readiness, 0.35)

    primary = cap.preferred_strategies[0] if cap and cap.preferred_strategies else None
    safe_fb, unsafe_fb = partition_coord_safe_fallbacks(
        cap.fallback_strategies if cap else (),
    )
    if unsafe_fb:
        safety_flags.append("coord_fallbacks_rejected")
    if cap and cap.allows_coords_emergency:
        safety_flags.append("capability_allows_coords_emergency_but_shadow_blocks_exec")

    strat = {"primary": primary, "safe_fallbacks": safe_fb}
    fallback_policy = {
        "primary": primary,
        "safe_fallbacks": safe_fb,
        "rejected_coord_fallbacks": unsafe_fb,
    }

    if unsupported_reason:
        executor_action = "UNSUPPORTED_SHADOW"
        strat = {"primary": None, "safe_fallbacks": []}
        fallback_policy["safe_fallbacks"] = []
        safety_flags.append("marked_unsupported")

    return {
        "plan_step_order": step.order,
        "phase_id": step.phase_id,
        "label": step.label,
        "primitive": step.primitive,
        "executor_action": executor_action,
        "uic_capability_id": cap_id,
        "required_params": required_params,
        "strategy": strat,
        "validator": _validator_token(cap),
        "fallback_policy": fallback_policy,
        "safety_flags": safety_flags,
        "readiness": round(max(0.0, min(1.0, readiness)), 4),
        "unsupported_reason": unsupported_reason,
    }


def _catalog_capability_for_sep_type(sep_type: str) -> Optional[str]:
    t = str(sep_type or "").strip().lower()
    for row in UIC_CONTRACT_ROWS:
        if row.sep_type.lower() == t:
            return row.capability_id
    return None


def compare_adapter_coverage_vs_sep(
    mission: Mission,
    translated_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Cobertura de capabilities adaptadas vs tipos SEP declarados en la misión."""
    sep = mission.semantic_execution_plan or {}
    sep_steps = [s for s in (sep.get("steps") or []) if isinstance(s, dict)]
    sep_types = [str(s.get("type") or "").lower() for s in sep_steps]

    adapter_caps: Set[str] = set()
    supported_steps = 0
    total_steps = 0
    for row in translated_rows:
        total_steps += 1
        if row.get("unsupported_reason"):
            continue
        if row.get("executor_action") == "UNSUPPORTED_SHADOW":
            continue
        cap = row.get("uic_capability_id")
        if cap:
            adapter_caps.add(str(cap))
        supported_steps += 1

    sep_caps: List[str] = []
    for st in sep_types:
        cid = _catalog_capability_for_sep_type(st)
        if cid:
            sep_caps.append(cid)

    sep_unique = list(dict.fromkeys(sep_caps))
    hit = [c for c in sep_unique if c in adapter_caps]
    overlap_ratio = round(len(hit) / max(len(sep_unique), 1), 4) if sep_unique else None

    fuzzy_hit: List[str] = []
    for sc in sep_unique:
        related = _SEP_CAP_RELATED_ADAPTER_CAPS.get(sc, frozenset())
        if adapter_caps & related:
            fuzzy_hit.append(sc)
    fuzzy_ratio = (
        round(len(fuzzy_hit) / max(len(sep_unique), 1), 4) if sep_unique else None
    )

    return {
        "sep_step_count": len(sep_types),
        "sep_capability_ids_expected": sep_unique,
        "adapter_distinct_capabilities": sorted(adapter_caps),
        "sep_capabilities_matched_by_adapter": hit,
        "sep_adapter_capability_overlap_ratio": overlap_ratio,
        "sep_capabilities_fuzzy_matched": fuzzy_hit,
        "sep_adapter_fuzzy_overlap_ratio": fuzzy_ratio,
        "cob_executable_coverage_ratio": round(supported_steps / max(total_steps, 1), 4)
        if total_steps
        else None,
        "translated_step_count": total_steps,
        "supported_translated_steps": supported_steps,
    }


def build_cob_runtime_adapter_report(
    mission: Mission,
    *,
    replay_snapshot: Optional[Dict[str, Any]] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Genera traducción completa COB→executor + cobertura SEP (shadow-safe)."""
    cob_by_id = {c.id: c for c in (mission.canonical_operational_blocks or [])}

    per_cob: List[Dict[str, Any]] = []
    flat_translated: List[Dict[str, Any]] = []

    plans_src: List[Dict[str, Any]] = []
    if replay_snapshot and isinstance(replay_snapshot.get("plans"), list):
        plans_src = replay_snapshot["plans"]
    else:
        for cob in mission.canonical_operational_blocks or []:
            steps = build_cob_replay_plan(cob)
            plans_src.append({
                "cob_id": cob.id,
                "canonical_action": cob.canonical_action,
                "steps": [s.model_dump(mode="json") for s in steps],
            })

    for entry in plans_src:
        cid = str(entry.get("cob_id") or "")
        cob = cob_by_id.get(cid)
        if cob is None:
            continue
        translated: List[Dict[str, Any]] = []
        for sd in entry.get("steps") or []:
            try:
                step = CobReplayPlanStep.model_validate(sd)
            except Exception as e:
                log.debug(f"CobReplayPlanStep validate: {e}")
                continue
            row = translate_cob_replay_step(cob, step)
            row["cob_id"] = cob.id
            row["canonical_action"] = cob.canonical_action
            translated.append(row)
            flat_translated.append(row)

        per_cob.append({
            "cob_id": cob.id,
            "canonical_action": cob.canonical_action,
            "translated_steps": translated,
        })

    coverage = compare_adapter_coverage_vs_sep(mission, flat_translated)

    report = {
        "version": 1,
        "generated_by": "cob_runtime_adapter",
        "shadow_safe": True,
        "limitations": list(LIMITATIONS),
        "per_cob": per_cob,
        "coverage_vs_sep": coverage,
        "flat_translated_steps": flat_translated,
    }
    if persist:
        mission.cob_runtime_adapter_shadow = report
    return report


def rebuild_cob_runtime_adapter_shadow_only(mission: Mission) -> None:
    """Recalcula informe si hay COB; si no, limpia."""
    if not (mission.canonical_operational_blocks or []):
        mission.cob_runtime_adapter_shadow = None
        return
    try:
        rs = mission.cob_replay_shadow if isinstance(mission.cob_replay_shadow, dict) else None
        build_cob_runtime_adapter_report(mission, replay_snapshot=rs, persist=True)
    except Exception as e:
        log.debug(f"rebuild_cob_runtime_adapter_shadow_only: {e}")
