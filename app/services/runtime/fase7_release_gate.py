"""FASE 7 — Gate de release (benchmark real).

Criterio ACCEPTED estricto (no negociable):
  * 5/5 runs consecutivos con status=success
  * coords_used = 0, smart_route = 0
  * runtime_timeout = 0, needs_human = 0
  * sin retry humano
  * weakest_step reportado en cada run y en agregado de suite
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.services.runtime.nevlan_runtime_convergence import is_forbidden_primary_strategy

FASE7_REQUIRED_CONSECUTIVE_RUNS = 5
FASE7_ACCEPTANCE_STATUS = "ACCEPTED"
FASE7_REJECTION_STATUS = "REJECTED"


def count_smart_route_usage(step_strategies: Sequence[Mapping[str, Any]]) -> int:
    return sum(
        1
        for step in step_strategies
        if is_forbidden_primary_strategy(str(step.get("strategy_used") or ""))
    )


def evaluate_fase7_run_acceptance(run: Mapping[str, Any]) -> str:
    """Evalúa un run individual contra criterio ACCEPTED estricto."""
    if run.get("runtime_timeout"):
        return FASE7_REJECTION_STATUS
    if run.get("needs_human") or run.get("human_retry"):
        return FASE7_REJECTION_STATUS
    if run.get("blocked"):
        return FASE7_REJECTION_STATUS
    if run.get("coords_used"):
        return FASE7_REJECTION_STATUS
    if run.get("smart_route_count", 0) > 0:
        return FASE7_REJECTION_STATUS
    if str(run.get("status") or "") != "success":
        return FASE7_REJECTION_STATUS
    return FASE7_ACCEPTANCE_STATUS


def consecutive_accepted_runs(runs: Sequence[Mapping[str, Any]]) -> int:
    """Cuenta ACCEPTED consecutivos desde el inicio (5/5 = listo)."""
    count = 0
    for run in runs:
        if evaluate_fase7_run_acceptance(run) != FASE7_ACCEPTANCE_STATUS:
            break
        count += 1
    return count


def aggregate_fase7_runs(runs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Métricas agregadas de suite para audit release."""
    coords_total = sum(1 for r in runs if r.get("coords_used"))
    smart_route_total = sum(int(r.get("smart_route_count") or 0) for r in runs)
    timeout_total = sum(1 for r in runs if r.get("runtime_timeout"))
    needs_human_total = sum(
        1 for r in runs if r.get("needs_human") or r.get("human_retry")
    )
    accepted = sum(
        1 for r in runs if evaluate_fase7_run_acceptance(r) == FASE7_ACCEPTANCE_STATUS
    )
    slowest: Optional[Dict[str, Any]] = None
    for run in runs:
        ws = run.get("weakest_step")
        if not isinstance(ws, dict):
            continue
        dur = int(ws.get("duration_ms") or 0)
        if slowest is None or dur >= int(slowest.get("duration_ms") or 0):
            slowest = dict(ws)
    return {
        "run_count": len(runs),
        "accepted_runs": accepted,
        "consecutive_accepted_runs": consecutive_accepted_runs(runs),
        "coords_used_total": coords_total,
        "smart_route_count": smart_route_total,
        "runtime_timeout_count": timeout_total,
        "needs_human_count": needs_human_total,
        "weakest_step": slowest,
    }


def fase7_gate_violations(
    report: Mapping[str, Any],
    *,
    required_runs: int = FASE7_REQUIRED_CONSECUTIVE_RUNS,
) -> List[str]:
    """Condiciones que hacen fallar el gate de release FASE 7."""
    violations: List[str] = []
    runs = list(report.get("runs") or [])
    aggregate = dict(report.get("aggregate") or aggregate_fase7_runs(runs))

    if len(runs) < required_runs:
        violations.append(f"insufficient_runs:{len(runs)}<{required_runs}")

    consecutive = int(
        aggregate.get("consecutive_accepted_runs")
        or consecutive_accepted_runs(runs)
    )
    if consecutive < required_runs:
        violations.append(
            f"not_consecutive_accepted:{consecutive}/{required_runs}",
        )

    if int(aggregate.get("coords_used_total") or 0) > 0:
        violations.append("coords_used_nonzero")

    if int(aggregate.get("smart_route_count") or 0) > 0:
        violations.append("smart_route_nonzero")

    if int(aggregate.get("runtime_timeout_count") or 0) > 0:
        violations.append("runtime_timeout_nonzero")

    if int(aggregate.get("needs_human_count") or 0) > 0:
        violations.append("needs_human_nonzero")

    suite_weakest = report.get("weakest_step") or aggregate.get("weakest_step")
    if not suite_weakest:
        violations.append("missing_suite_weakest_step")

    for run in runs:
        idx = run.get("run_index", "?")
        if not run.get("weakest_step"):
            violations.append(f"missing_run_weakest_step:{idx}")
        if evaluate_fase7_run_acceptance(run) != FASE7_ACCEPTANCE_STATUS:
            violations.append(f"run_not_accepted:{idx}:{run.get('status')}")

    if str(report.get("release_status") or "") != FASE7_ACCEPTANCE_STATUS:
        violations.append(f"release_status:{report.get('release_status')!r}")

    return violations


def fase7_batch_gate_violations(
    batch: Mapping[str, Any],
    *,
    required_runs: int = FASE7_REQUIRED_CONSECUTIVE_RUNS,
) -> List[str]:
    """Gate de release para suite completa (≥5 escenarios)."""
    violations: List[str] = []
    scenarios = list(batch.get("scenarios") or [])

    if len(scenarios) < 5:
        violations.append(f"insufficient_scenarios:{len(scenarios)}<5")

    for scenario in scenarios:
        sid = scenario.get("scenario_id", "?")
        scenario_v = fase7_gate_violations(scenario, required_runs=required_runs)
        for v in scenario_v:
            violations.append(f"{sid}:{v}")

    aggregate = dict(batch.get("aggregate") or {})
    if int(aggregate.get("coords_used_total") or 0) > 0:
        violations.append("batch_coords_used_nonzero")
    if int(aggregate.get("smart_route_count") or 0) > 0:
        violations.append("batch_smart_route_nonzero")
    if int(aggregate.get("runtime_timeout_count") or 0) > 0:
        violations.append("batch_runtime_timeout_nonzero")
    if int(aggregate.get("needs_human_count") or 0) > 0:
        violations.append("batch_needs_human_nonzero")
    if not batch.get("weakest_step"):
        violations.append("batch_missing_weakest_step")
    if str(batch.get("release_status") or "") != FASE7_ACCEPTANCE_STATUS:
        violations.append(f"batch_release_status:{batch.get('release_status')!r}")

    return violations


__all__ = [
    "FASE7_ACCEPTANCE_STATUS",
    "FASE7_REQUIRED_CONSECUTIVE_RUNS",
    "FASE7_REJECTION_STATUS",
    "aggregate_fase7_runs",
    "consecutive_accepted_runs",
    "count_smart_route_usage",
    "evaluate_fase7_run_acceptance",
    "fase7_batch_gate_violations",
    "fase7_gate_violations",
]
