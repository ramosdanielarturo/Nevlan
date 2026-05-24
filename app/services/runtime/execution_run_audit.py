"""FASE 0.5 — Telemetría obligatoria por run (audit JSONL).

Cada ejecución smart_runner escribe una fila en
``var/runtime/execution_run_audit.jsonl`` con la estrategia usada por paso
y el snapshot de flags runtime activos.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.core.logger import log
from app.core.paths import VAR_DIR
from app.services.runtime.nevlan_runtime_convergence import (
    capture_runtime_flag_snapshot,
    convergence_profile_label,
)

_AUDIT_JSONL = VAR_DIR / "runtime" / "execution_run_audit.jsonl"

EXECUTION_RUN_AUDIT_JSONL = _AUDIT_JSONL


class StepStrategyAudit(BaseModel):
    """Estrategia efectiva en un paso de ejecución."""

    model_config = ConfigDict(extra="ignore")

    step_id: str = ""
    kind: str = ""
    status: str = ""
    strategy_used: str = ""
    duration_ms: int = 0


class ExecutionRunAuditRecord(BaseModel):
    """Una fila de auditoría por run completo."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = ""
    mission_id: str = ""
    recorded_at: float = Field(default_factory=time.time)
    status: str = ""
    duration_ms: int = 0
    coords_used: bool = False
    executive_source: str = ""
    legacy_graph_ignored: bool = False
    blocked: bool = False
    block_reason: str = ""
    runtime_flags: Dict[str, bool] = Field(default_factory=dict)
    profile_label: str = ""
    step_strategies: List[StepStrategyAudit] = Field(default_factory=list)
    weakest_step: Optional[Dict[str, Any]] = None


def _extract_step_strategies(mission_result: Any) -> List[StepStrategyAudit]:
    rows: List[StepStrategyAudit] = []
    if mission_result is None:
        return rows
    for o in getattr(mission_result, "outcomes", None) or []:
        try:
            rows.append(
                StepStrategyAudit(
                    step_id=str(getattr(o, "step_id", "") or ""),
                    kind=str(getattr(o, "kind", "") or ""),
                    status=str(getattr(o, "status", "") or ""),
                    strategy_used=str(getattr(o, "strategy_used", "") or ""),
                    duration_ms=int(getattr(o, "duration_ms", 0) or 0),
                )
            )
        except Exception:
            continue
    return rows


def _pick_weakest_step(
    step_strategies: List[StepStrategyAudit],
    *,
    external: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if external:
        return dict(external)
    failed = [s for s in step_strategies if s.status not in ("success", "skipped")]
    if failed:
        s = failed[0]
        return {
            "step_id": s.step_id,
            "kind": s.kind,
            "status": s.status,
            "strategy_used": s.strategy_used,
            "reason": "step_failed",
        }
    if step_strategies:
        slowest = max(step_strategies, key=lambda s: s.duration_ms)
        if slowest.duration_ms > 0:
            return {
                "step_id": slowest.step_id,
                "kind": slowest.kind,
                "status": slowest.status,
                "strategy_used": slowest.strategy_used,
                "duration_ms": slowest.duration_ms,
                "reason": "slowest_step",
            }
    return None


def build_execution_run_audit(
    *,
    mission: Any,
    decision: Any,
    settings: Any,
    weakest_step: Optional[Dict[str, Any]] = None,
) -> ExecutionRunAuditRecord:
    """Construye registro de auditoría desde RunnerDecision + settings."""
    flags = capture_runtime_flag_snapshot(settings)
    step_strategies = _extract_step_strategies(
        getattr(decision, "result", None),
    )
    result_status = None
    if getattr(decision, "result", None) is not None:
        result_status = getattr(decision.result, "status", None)
    if getattr(decision, "blocked", False):
        status = "blocked"
    elif result_status:
        status = str(result_status)
    else:
        status = "unknown"

    return ExecutionRunAuditRecord(
        run_id=str(
            getattr(getattr(decision, "result", None), "run_id", "")
            or getattr(mission, "id", "")
            or "",
        ),
        mission_id=str(getattr(mission, "id", "") or ""),
        status=status,
        duration_ms=int(getattr(decision, "duration_ms", 0) or 0),
        coords_used=bool(getattr(decision, "coords_used", False)),
        executive_source=str(getattr(decision, "source", "") or ""),
        legacy_graph_ignored=bool(getattr(decision, "legacy_graph_ignored", False)),
        blocked=bool(getattr(decision, "blocked", False)),
        block_reason=str(getattr(decision, "reason", "") or ""),
        runtime_flags=flags,
        profile_label=convergence_profile_label(flags),
        step_strategies=step_strategies,
        weakest_step=_pick_weakest_step(step_strategies, external=weakest_step),
    )


def append_execution_run_audit(record: ExecutionRunAuditRecord) -> None:
    """Append-only JSONL — best-effort, nunca bloquea ejecución."""
    try:
        _AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        row = record.model_dump(mode="json")
        with _AUDIT_JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.debug("[ExecutionRunAudit] jsonl append failed: %s", exc)


def record_execution_run_audit(
    *,
    mission: Any,
    decision: Any,
    settings: Any,
    weakest_step: Optional[Dict[str, Any]] = None,
) -> ExecutionRunAuditRecord:
    """Construye y persiste auditoría obligatoria por run."""
    record = build_execution_run_audit(
        mission=mission,
        decision=decision,
        settings=settings,
        weakest_step=weakest_step,
    )
    append_execution_run_audit(record)
    return record


__all__ = [
    "EXECUTION_RUN_AUDIT_JSONL",
    "ExecutionRunAuditRecord",
    "StepStrategyAudit",
    "append_execution_run_audit",
    "build_execution_run_audit",
    "record_execution_run_audit",
]
