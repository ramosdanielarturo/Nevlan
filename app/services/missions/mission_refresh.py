"""Recálculo centralizado tras reparaciones o cambios en pasos compilados."""
from __future__ import annotations

from typing import Any, Collection, Dict, Optional, Set

from app.contracts.mission import Mission
from app.core.logger import log


def refresh_mission_after_step_change(
    mission: Mission,
    reason: str,
    affected_step_ids: Optional[Collection[str]] = None,
    *,
    preserve_selected_step_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Reconstruye action_groups y métricas dependientes."""
    aff: Set[str] = set(affected_step_ids or ())
    summary: Dict[str, Any] = {
        "reason": reason,
        "affected_step_ids": sorted(aff),
        "preserve_selected_step_id": preserve_selected_step_id,
    }
    try:
        from app.services.missions.compiler import MissionCompiler

        MissionCompiler.rebuild_action_groups(mission)
        summary["action_groups_count"] = len(getattr(mission, "action_groups", []) or [])
    except Exception as e:
        log.debug(f"refresh_mission_after_step_change rebuild: {e}")
        summary["rebuild_error"] = str(e)[:280]
    try:
        from app.services.missions.step_quality import count_weak_steps

        summary["weak_steps"] = count_weak_steps(
            mission.compiled_execution_graph,
            getattr(mission, "interpreted_steps", None),
        )
    except Exception as e:
        summary["weak_steps_error"] = str(e)[:120]
        summary["weak_steps"] = None
    return summary
