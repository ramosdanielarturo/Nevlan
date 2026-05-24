"""Builders para suites golden runtime convergence (OCRP).

Flujos agnósticos de producto — sin hardcodes de app específica.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple

from app.contracts.mission import Mission, MissionStatus
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)

FLOW_CATEGORIES: Tuple[Tuple[str, str], ...] = (
    ("launcher", "launcher_open_host"),
    ("browser", "browser_navigation_search"),
    ("forms", "structured_form_submit"),
    ("tables", "table_row_selection"),
    ("tabs", "tab_switch_context"),
    ("search", "search_query_flow"),
    ("infinite_scroll", "infinite_scroll_collection"),
    ("lazy_loading", "lazy_load_surface"),
    ("virtualized", "virtualized_collection"),
)

_LATENCY_PROFILES = (
    {"name": "fast", "scale": 0.6},
    {"name": "normal", "scale": 1.0},
    {"name": "slow", "scale": 1.8},
)

_VIEWPORT_PROFILES = (
    {"name": "compact", "w": 1280, "h": 720, "zoom": 1.0},
    {"name": "standard", "w": 1920, "h": 1080, "zoom": 1.0},
    {"name": "zoomed", "w": 1920, "h": 1080, "zoom": 1.25},
)


def _base_mission(name: str) -> Mission:
    return build_golden_search_chrome_youtube_mission(name=name)


def _tag_mission(m: Mission, *, category: str, variant: str) -> Mission:
    m.name = f"{category}-{variant}-{m.name}"
    audit = dict(getattr(m, "semantic_shadow_audit", None) or {})
    audit["ocrp_golden_category"] = category
    audit["ocrp_golden_variant"] = variant
    m.semantic_shadow_audit = audit
    return m


def build_convergence_flow_mission(category: str, *, variant: str = "default") -> Mission:
    """Construye misión golden por categoría de flujo operacional."""
    m = _base_mission(f"ocrp-golden-{category}")
    m.status = MissionStatus.EXECUTABLE

    if category == "launcher":
        _inject_step_kind_hint(m, 0, "launch_app")
    elif category == "browser":
        _inject_step_kind_hint(m, 2, "navigate_or_search")
    elif category == "forms":
        _inject_step_kind_hint(m, 4, "set_field_value")
    elif category == "tables":
        m.collection_entity_resolution_audit = [{
            "collection_type": "table",
            "collection_id": "tbl_main",
            "confidence": 0.82,
        }]
    elif category == "tabs":
        m.collection_entity_resolution_audit = [{
            "collection_type": "tabs",
            "collection_id": "tab_bar",
            "confidence": 0.78,
        }]
    elif category == "search":
        _inject_step_kind_hint(m, 4, "navigate_or_search")
    elif category == "infinite_scroll":
        m.operational_navigation_audit = [
            {"navigation_action": "scroll_down", "success": True, "progress_hint": 0.2},
            {"navigation_action": "scroll_down", "success": True, "progress_hint": 0.5},
        ]
    elif category == "lazy_loading":
        m.lop_last_snapshot = {
            "safe_to_continue": True,
            "confidence": 0.7,
            "dynamic_content_pending": True,
        }
    elif category == "virtualized":
        m.operational_navigation_audit = [
            {"navigation_action": "reposition_viewport", "success": True},
        ]
        m.collection_entity_resolution_audit = [{
            "collection_type": "grid",
            "collection_id": "virtual_grid",
            "confidence": 0.75,
        }]

    return _tag_mission(m, category=category, variant=variant)


def _inject_step_kind_hint(m: Mission, step_index: int, kind: str) -> None:
    sep = dict(m.semantic_execution_plan or {})
    steps = list(sep.get("steps") or [])
    if 0 <= step_index < len(steps) and isinstance(steps[step_index], dict):
        steps[step_index] = dict(steps[step_index])
        steps[step_index]["type"] = kind
        sep["steps"] = steps
        m.semantic_execution_plan = sep


def simulate_run_signature(
    mission: Mission,
    *,
    run_index: int,
    status: str = "success",
    jitter: float = 0.0,
) -> Dict[str, Any]:
    """Simula firma de run con varianza controlada (no fake pass)."""
    from app.services.runtime.operational_convergence_program import (
        build_execution_signature,
        append_execution_signature,
    )

    if jitter > 0 and run_index % 3 == 0:
        mission.operational_navigation_audit = list(
            getattr(mission, "operational_navigation_audit", None) or [],
        ) + [{"navigation_action": "scroll_down", "success": run_index % 2 == 0}]

    sig = build_execution_signature(
        mission,
        run_id=f"sim-{run_index}",
        status=status if jitter < 0.5 or run_index % 5 != 0 else "failed",
        duration_ms=1200.0 + jitter * 400 * (run_index % 4),
        step_outcomes=["success"] * 6 + (["failed"] if jitter > 0.6 and run_index % 7 == 0 else []),
    )
    append_execution_signature(mission, sig)
    return sig.model_dump(mode="json")


def latency_profiles() -> List[Dict[str, Any]]:
    return list(_LATENCY_PROFILES)


def viewport_profiles() -> List[Dict[str, Any]]:
    return list(_VIEWPORT_PROFILES)
