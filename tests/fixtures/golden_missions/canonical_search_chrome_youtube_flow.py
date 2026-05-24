"""Golden: Search → Chrome → perfil → nueva pestaña → YouTube → búsqueda → scroll.

La geometría de eventos es la misma que ``_canonical_chrome_youtube_trace``
en ``test_sipe_telemetry_persistence_catalog``. Se inyecta evidencia
``capture_contract`` / ``target_identity_isolation`` en el click
Search→Chrome para anclar EXECUTION_TRUTH sin depender de ``click_fallback``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import List

from app.contracts.mission import Mission, MissionStatus, RawEvent
from app.services.missions.intent_collapse_engine import apply_collapse_to_mission
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    attach_intent_status_to_plan,
    attach_semantic_plan_to_mission,
)
from app.services.missions.semantic_intent_promotion_engine import apply_promotion_to_mission


def _load_canonical_trace_builder():
    catalog = (
        Path(__file__).resolve().parent.parent.parent
        / "unit"
        / "test_sipe_telemetry_persistence_catalog.py"
    )
    spec = importlib.util.spec_from_file_location("_sipe_tel_catalog_mod", catalog)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "_canonical_chrome_youtube_trace")


# Índice estable: 0=focus search, 1=type chrome, 2=click resultado Chrome.
SEARCH_TO_CHROME_CLICK_INDEX = 2


def inject_search_to_chrome_execution_truth(
    events: List[RawEvent],
    *,
    index: int = SEARCH_TO_CHROME_CLICK_INDEX,
) -> None:
    """Marca el click Search→Chrome como evidencia fuerte para EXECUTION_TRUTH."""
    ev = events[index]
    meta = ev.metadata if isinstance(ev.metadata, dict) else {}
    meta["capture_contract"] = {
        "trusted_pre_action_identity": True,
        "sufficient_for_execution": True,
        "capture_complete": True,
        "contamination": False,
    }
    meta["target_identity_isolation"] = {
        "trusted_pre_action_identity": True,
        "identity_preserved_after_transition": True,
        "successful_state_transition": True,
        "contamination": False,
    }
    ev.metadata = meta


_FASE0_ENTITY_FIRST_STRATEGIES = {
    "open_site": "open_url:hotkey_ctrl_l",
    "open_url": "open_url:hotkey_ctrl_l",
    "search_site": "search_content:uol_surface",
    "search_youtube": "search_content:uol_surface",
    "search_content": "search_content:uol_surface",
}


def apply_fase0_entity_first_strategies(mission: Mission) -> None:
    """FASE 0 golden: preferred_strategy entity-first, sin smart_route primaria."""
    sep_blob = dict(mission.semantic_execution_plan or {})
    steps_in = sep_blob.get("steps") or []
    changed = False
    new_steps: List[dict] = []
    for s in steps_in:
        sd = dict(s)
        step_type = str(sd.get("type") or "")
        pref = str(sd.get("preferred_strategy") or "")
        replacement = _FASE0_ENTITY_FIRST_STRATEGIES.get(step_type)
        if replacement and ("smart_route" in pref or not pref.strip()):
            sd["preferred_strategy"] = replacement
            changed = True
        new_steps.append(sd)
    if not changed:
        return
    sep_blob["steps"] = new_steps
    mission.semantic_execution_plan = sep_blob
    mission.legacy_compiled_graph_purpose = "legacy_debug_only"


def build_golden_search_chrome_youtube_mission(
    *,
    name: str = "golden-canonical-search-chrome-youtube",
) -> Mission:
    trace_fn = _load_canonical_trace_builder()
    events = trace_fn()
    inject_search_to_chrome_execution_truth(events)
    m = Mission(name=name, raw_trace=events)
    apply_collapse_to_mission(m)
    attach_semantic_plan_to_mission(m, force=True)
    m.status = MissionStatus.EXECUTABLE
    apply_promotion_to_mission(m)
    apply_fase0_entity_first_strategies(m)
    return m


def apply_ambiguous_profile_select_step(mission: Mission, *, label: str = "perfil") -> None:
    """Fuerza perfil genérico en ``select_profile`` (bloqueador perfil, truth intacta)."""
    sep_blob = dict(mission.semantic_execution_plan or {})
    steps_in = sep_blob.get("steps") or []
    new_steps: List[dict] = []
    for s in steps_in:
        sd = dict(s)
        if sd.get("type") == "select_profile":
            params = dict(sd.get("params") or {})
            params["profile_name"] = label
            sd["params"] = params
        new_steps.append(sd)
    sep_blob["steps"] = new_steps
    mission.semantic_execution_plan = sep_blob
    plan = SemanticExecutionPlan.from_dict(sep_blob)
    attach_intent_status_to_plan(plan, mission=mission)
    mission.semantic_execution_plan = plan.to_dict()


__all__ = [
    "SEARCH_TO_CHROME_CLICK_INDEX",
    "apply_ambiguous_profile_select_step",
    "apply_fase0_entity_first_strategies",
    "build_golden_search_chrome_youtube_mission",
    "inject_search_to_chrome_execution_truth",
]
