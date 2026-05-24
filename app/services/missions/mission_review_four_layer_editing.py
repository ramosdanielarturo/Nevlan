"""Mission Review — edición real vía Four-Layer Operational Model (4LOM).

Orquesta las APIs existentes de ``four_layer_operational_model`` para la UI
y tests. No introduce arquitectura nueva.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import Mission
from app.core.logger import log
from app.services.missions.four_layer_operational_model import (
    apply_goal_purpose_edit,
    apply_human_view_label_edit,
    apply_machine_layer_reorder,
    apply_machine_step_edit,
    apply_step_disabled,
    editable_param_keys_for_step,
    ensure_four_layer_model,
    expert_lineage_mappings,
    persist_four_layer_mission,
    validate_four_layer_invariants,
    validate_reorder_dependencies,
)


class FourLayerEditError(ValueError):
    """Edición rechazada (reorden inválido, paso desconocido, etc.)."""


def rename_step_label(mission: Mission, machine_step_id: str, new_label: str) -> None:
    """Solo Human View — no toca machine type/params ni Goal."""
    apply_human_view_label_edit(mission, machine_step_id, human_label=new_label)


def edit_step_params(
    mission: Mission,
    machine_step_id: str,
    params_patch: Dict[str, Any],
) -> None:
    """Machine Layer + SEP; Goal intacto."""
    if not params_patch:
        return
    goal_before = ensure_four_layer_model(mission).goal.purpose_statement
    apply_machine_step_edit(
        mission,
        machine_step_id,
        params_patch=params_patch,
        refresh_contract=True,
    )
    goal_after = ensure_four_layer_model(mission).goal.purpose_statement
    if goal_before != goal_after:
        apply_goal_purpose_edit(mission, goal_before, source="integrity_restore")


def edit_step_strategy(
    mission: Mission,
    machine_step_id: str,
    preferred_strategy: str,
    *,
    fallback_strategies: Optional[List[str]] = None,
) -> None:
    apply_machine_step_edit(
        mission,
        machine_step_id,
        preferred_strategy=preferred_strategy,
        fallback_strategies=fallback_strategies,
        refresh_contract=False,
    )


def reorder_steps(mission: Mission, ordered_machine_step_ids: List[str]) -> None:
    """Reorden Human + Machine con validación de dependencias."""
    model = ensure_four_layer_model(mission)
    ok, reason = validate_reorder_dependencies(
        model.machine_execution.steps,
        ordered_machine_step_ids,
    )
    if not ok:
        raise FourLayerEditError(reason)
    apply_machine_layer_reorder(mission, ordered_machine_step_ids)


def _try_reorder(mission: Mission, ids: List[str]) -> None:
    model = ensure_four_layer_model(mission)
    ok, reason = validate_reorder_dependencies(
        model.machine_execution.steps,
        ids,
    )
    if not ok:
        raise FourLayerEditError(reason)
    apply_machine_layer_reorder(mission, ids)


def move_step_up(mission: Mission, machine_step_id: str) -> None:
    model = ensure_four_layer_model(mission)
    ids = [ms.step_id for ms in model.machine_execution.steps]
    if machine_step_id not in ids:
        raise FourLayerEditError(f"unknown step: {machine_step_id}")
    idx = ids.index(machine_step_id)
    for j in range(idx - 1, -1, -1):
        trial = list(ids)
        trial[idx], trial[j] = trial[j], trial[idx]
        try:
            _try_reorder(mission, trial)
            return
        except FourLayerEditError:
            continue
    raise FourLayerEditError("REORDER_NO_VALID_MOVE_UP")


def move_step_down(mission: Mission, machine_step_id: str) -> None:
    model = ensure_four_layer_model(mission)
    ids = [ms.step_id for ms in model.machine_execution.steps]
    if machine_step_id not in ids:
        raise FourLayerEditError(f"unknown step: {machine_step_id}")
    idx = ids.index(machine_step_id)
    for j in range(idx + 1, len(ids)):
        trial = list(ids)
        trial[idx], trial[j] = trial[j], trial[idx]
        try:
            _try_reorder(mission, trial)
            return
        except FourLayerEditError:
            continue
    raise FourLayerEditError("REORDER_NO_VALID_MOVE_DOWN")


def set_step_disabled(
    mission: Mission,
    machine_step_id: str,
    disabled: bool = True,
) -> None:
    apply_step_disabled(mission, machine_step_id, disabled=disabled)


def save_mission_edits(mission: Mission) -> List[str]:
    """Persiste 4LOM + SEP; devuelve invariantes pendientes."""
    return persist_four_layer_mission(mission)


def get_step_display_context(
    mission: Mission,
    machine_step_id: str,
) -> Dict[str, Any]:
    """Contexto para diálogos de edición en UI."""
    model = ensure_four_layer_model(mission)
    ms = next((m for m in model.machine_execution.steps if m.step_id == machine_step_id), None)
    hv = next((h for h in model.human_view.steps if h.machine_step_id == machine_step_id), None)
    if ms is None or hv is None:
        raise FourLayerEditError(f"unknown step: {machine_step_id}")
    sep_step = next(
        (
            s
            for s in (mission.semantic_execution_plan or {}).get("steps") or []
            if str(s.get("id") or "") == machine_step_id
        ),
        {},
    )
    return {
        "machine_step_id": machine_step_id,
        "human_label": hv.human_label,
        "semantic_type": ms.semantic_type,
        "params": dict(ms.params),
        "preferred_strategy": ms.preferred_strategy,
        "fallback_strategies": list(ms.fallback_strategies),
        "disabled": bool(ms.disabled or hv.disabled),
        "editable_keys": editable_param_keys_for_step(mission, machine_step_id),
        "sep_intent_fragment": {
            "human_label": sep_step.get("human_label"),
            "type": sep_step.get("type"),
        },
    }


def get_expert_mapping_lines(mission: Mission) -> List[str]:
    return expert_lineage_mappings(mission)


def raw_trace_unchanged(mission: Mission, prior_trace_len: int) -> bool:
    return len(mission.raw_trace or []) == prior_trace_len


__all__ = [
    "FourLayerEditError",
    "edit_step_params",
    "edit_step_strategy",
    "get_expert_mapping_lines",
    "get_step_display_context",
    "move_step_down",
    "move_step_up",
    "raw_trace_unchanged",
    "rename_step_label",
    "reorder_steps",
    "save_mission_edits",
    "set_step_disabled",
]
