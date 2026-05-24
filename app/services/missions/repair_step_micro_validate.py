"""
Micro-validación tras regrabación de un paso (runtime repair).
===========================================================

Ejecuta **una sola vez** el paso semántico corregido con el mismo
:class:`SmartMissionExecutor` que usará la corrida grande.

- Si ``success``: reanudar en **índice + 1** (la acción ya ocurrió).
- Si ``skipped``: reanudar en el **mismo** índice (siguiente ejecución
  volverá a saltar igual).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

import uuid

from app.core.logger import log
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.semantic_execution_plan import (
    build_semantic_execution_plan,
    semantic_plan_to_mission_steps,
)
from app.services.missions.smart_executor import (
    MissionResult,
    SmartMissionExecutor,
    StepOutcome,
    is_emergency_coordinates_strategy,
)


@dataclass
class RepairMicroValidationResult:
    ok: bool
    """Si True, puede ofrecerse continuar desde ``continue_semantic_index``."""

    continue_semantic_index: int
    """Índice 0-based en el plan semántico para ``semantic_resume_start_index``."""

    user_summary: str
    strategy_used: str = ""
    outcome_status: str = ""
    total_semantic_steps: int = 0
    checks_passed: Dict[str, bool] = field(default_factory=dict)

    expert_detail: str = ""


def _build_checks(status: str, strat: str) -> Dict[str, bool]:
    strat_l = str(strat or "")
    blind = status in ("success", "skipped") and is_emergency_coordinates_strategy(strat_l)
    return {
        "contract_and_runtime": True,
        "target_and_strategy_attempted": status in (
            "success", "skipped", "failed",
        ),
        "no_blind_emergency_click": not blind if status in (
            "success", "skipped",
        ) else False,
        "outcome_measurable": status in ("success", "skipped"),
    }


def _brief_outcome(o: StepOutcome, semantic_idx: int) -> RepairMicroValidationResult:
    strat = str(o.strategy_used or "")
    status = str(o.status or "")
    chk = _build_checks(status, strat)
    base = max(0, int(semantic_idx))

    if status == "skipped":
        return RepairMicroValidationResult(
            ok=chk["no_blind_emergency_click"],
            continue_semantic_index=base,
            user_summary=(
                (o.human_message or "").strip()
                if chk["no_blind_emergency_click"]
                else (
                    "Este paso se omitió usando una estrategia visual o de coordenadas "
                    "de emergencia. Preferí UIA antes de confiar."
                ),
            ).strip(),
            strategy_used=strat,
            outcome_status=status,
            checks_passed=chk,
            expert_detail=f"skipped strategy={strat!r}",
        )

    if status == "success" and chk["no_blind_emergency_click"]:
        return RepairMicroValidationResult(
            ok=True,
            continue_semantic_index=base + 1,
            user_summary=(
                "Verificado: el paso adaptado actuó y la comprobación final encajó."
            ),
            strategy_used=strat,
            outcome_status=status,
            checks_passed=chk,
            expert_detail=f"success strategy={strat!r}",
        )

    if status == "success" and not chk["no_blind_emergency_click"]:
        return RepairMicroValidationResult(
            ok=False,
            continue_semantic_index=base,
            user_summary=(
                "La verificación terminó sólo tras coords o visión de emergencia; "
                "eso equivale a un click sin señal fiable."
            ),
            strategy_used=strat,
            outcome_status=status,
            checks_passed=chk,
            expert_detail=f"blind_success strategy={strat!r}",
        )

    msg = (o.human_message or o.error or "No pasó la comprobación rápida del paso.").strip()
    return RepairMicroValidationResult(
        ok=False,
        continue_semantic_index=base,
        user_summary=msg[:900],
        strategy_used=strat,
        outcome_status=status,
        checks_passed=chk,
        expert_detail=(
            f"{status} strategy={strat!r} err={(o.error or '')[:280]!r}"
        ),
    )


def micro_validate_repaired_semantic_step(
    mission: Any,
    semantic_step_index: int,
    *,
    expert_mode: bool = False,
) -> RepairMicroValidationResult:
    """Comprueba un único paso del ``semantic_execution_plan`` en disco."""
    try:
        plan = build_semantic_execution_plan(mission, use_existing=True)
    except Exception as e:
        log.warning(f"[repair_validate] plan build: {e}")
        return RepairMicroValidationResult(
            ok=False,
            continue_semantic_index=max(0, int(semantic_step_index or 0)),
            user_summary="No se pudo leer el plan semántico para verificar.",
            checks_passed={"contract_and_runtime": False},
            expert_detail=f"plan_build_exc:{type(e).__name__}:{e}"[:380],
        )

    if plan is None or not getattr(plan, "steps", None):
        return RepairMicroValidationResult(
            ok=False,
            continue_semantic_index=max(0, int(semantic_step_index or 0)),
            user_summary=(
                "No hay plan semántico utilizable para comprobar este paso."
            ),
            checks_passed={"contract_and_runtime": False},
            expert_detail="no_plan_steps",
        )

    steps: list[MissionStep] = semantic_plan_to_mission_steps(plan)
    if not steps:
        return RepairMicroValidationResult(
            ok=False,
            continue_semantic_index=0,
            user_summary="El plan semántico quedó vacío al convertir pasos ejecutables.",
            checks_passed={"contract_and_runtime": False},
            expert_detail="empty_steps_after_convert",
        )

    idx = max(0, min(int(semantic_step_index or 0), len(steps) - 1))
    singleton = [steps[idx]]
    rid = (
        "repair-validate-"
        f"{str(getattr(mission, 'id', 'm'))}-"
        f"{uuid.uuid4().hex[:8]}"
    )

    executor = SmartMissionExecutor(run_id=rid, expert_mode=expert_mode)

    try:
        mres: MissionResult = executor.run_mission(singleton)
    except Exception as e:
        log.exception("[repair_validate] executor:")
        return RepairMicroValidationResult(
            ok=False,
            continue_semantic_index=idx,
            user_summary=f"La verificación interna falló: {type(e).__name__}: {e}"[:820],
            checks_passed={"contract_and_runtime": False},
            expert_detail=f"{type(e).__name__}: {e}",
            total_semantic_steps=len(steps),
        )

    outs = list(getattr(mres, "outcomes", []) or [])
    if len(outs) != 1:
        return RepairMicroValidationResult(
            ok=False,
            continue_semantic_index=idx,
            user_summary=(
                "La comprobación no devolvió un resultado claro para el paso. "
                "Revisa Mission Review."
            ),
            outcome_status=getattr(mres, "status", "") or "",
            checks_passed={"contract_and_runtime": False},
            expert_detail=f"outcomes_n={len(outs)} status={getattr(mres, 'status', '')}",
            total_semantic_steps=len(steps),
        )

    br = _brief_outcome(outs[0], idx)
    ms = str(getattr(mres, "status", "") or "")
    if ms in ("stopped_for_human", "failed") and not br.ok:
        lm = str(getattr(mres, "last_message", "") or "").strip()
        if lm:
            br = RepairMicroValidationResult(
                ok=False,
                continue_semantic_index=br.continue_semantic_index,
                user_summary=lm[:900],
                strategy_used=br.strategy_used,
                outcome_status=br.outcome_status or ms,
                checks_passed=br.checks_passed,
                expert_detail=br.expert_detail,
                total_semantic_steps=len(steps),
            )

    br.total_semantic_steps = len(steps)
    return br


__all__ = [
    "RepairMicroValidationResult",
    "micro_validate_repaired_semantic_step",
]
