"""Medición acotada de latencia runtime — sin deps Qt."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TypeVar

T = TypeVar("T")


BETA_LATENCY_BUDGETS_MS: Dict[str, float] = {
    "preflight": 1500.0,
    "lop_snapshot": 500.0,
    "step_decision": 300.0,
    "arl_recovery": 1000.0,
    "status_update_normal": 100.0,
}


@dataclass
class LatencySection:
    name: str
    elapsed_ms: float
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeLatencyCollector:
    """Acumula secciones nombradas durante un run."""

    sections: List[LatencySection] = field(default_factory=list)
    _marks: Dict[str, float] = field(default_factory=dict)
    t_wall_start: float = field(default_factory=time.perf_counter)

    def mark_start(self, name: str) -> None:
        self._marks[name] = time.perf_counter()

    def mark_end(self, name: str, **meta: Any) -> float:
        t0 = self._marks.pop(name, None)
        if t0 is None:
            return 0.0
        ms = (time.perf_counter() - t0) * 1000.0
        self.sections.append(LatencySection(name=name, elapsed_ms=ms, meta=dict(meta)))
        return ms

    def span(self, name: str, fn: Callable[[], T], **meta: Any) -> T:
        self.mark_start(name)
        try:
            return fn()
        finally:
            self.mark_end(name, **meta)


def evaluate_beta_latency_budgets(
    *,
    by_name: Dict[str, float],
    semantic_step_count: Optional[int],
) -> List[str]:
    """Etiquetas ``budget_exceeded:*`` cuando se pasan límites beta."""
    exceeded: List[str] = []

    pf = round(by_name.get("one_shot_preflight", 0.0) or 0.0, 3)
    if pf > BETA_LATENCY_BUDGETS_MS["preflight"]:
        exceeded.append("budget_exceeded:preflight")

    lop_ms = round(
        (by_name.get("lop_refresh", 0.0) or 0.0)
        + (by_name.get("lop_shadow_sample", 0.0) or 0.0),
        3,
    )
    if lop_ms > BETA_LATENCY_BUDGETS_MS["lop_snapshot"]:
        exceeded.append("budget_exceeded:lop_snapshot")

    executor_ms = round(by_name.get("smart_executor_run", 0.0) or 0.0, 3)
    n_exec = max(1, int(semantic_step_count or 0))
    if n_exec > 0:
        avg_step_ms = executor_ms / float(n_exec)
        if avg_step_ms > BETA_LATENCY_BUDGETS_MS["step_decision"]:
            exceeded.append("budget_exceeded:step_decision_avg")

    repair_ms = round(
        sum(
            by_name.get(k, 0.0)
            for k in ("recovery_loop", "arl_round", "repair_micro")
        ),
        3,
    )
    if repair_ms > BETA_LATENCY_BUDGETS_MS["arl_recovery"]:
        exceeded.append("budget_exceeded:arl_recovery")

    return exceeded


def build_runtime_latency_report(
    collector: RuntimeLatencyCollector,
    *,
    budget_ms_total: Optional[float] = 180_000.0,
    perception_budget_ms: Optional[float] = 12_000.0,
    semantic_step_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Informe serializable ``runtime_latency_report``."""
    total_ms = (time.perf_counter() - collector.t_wall_start) * 1000.0
    by_name: Dict[str, float] = {}
    for s in collector.sections:
        by_name[s.name] = round(by_name.get(s.name, 0.0) + s.elapsed_ms, 3)
    perception_ms = sum(
        by_name.get(k, 0.0)
        for k in ("lop_refresh", "lop_shadow_sample", "perception_preflight", "execution_truth_prepare")
    )
    validator_ms = sum(by_name.get(k, 0.0) for k in ("validators", "validator_pass") if k in by_name)
    decision_ms = sum(
        by_name.get(k, 0.0)
        for k in ("smart_runner_preflight", "approval_gate", "semantic_plan_build", "one_shot_preflight")
        if k in by_name
    )
    repair_ms = sum(by_name.get(k, 0.0) for k in ("recovery_loop", "arl_round", "repair_micro") if k in by_name)

    budget_ok = True
    degraded_safe = False
    if budget_ms_total is not None and total_ms > budget_ms_total:
        budget_ok = False
        degraded_safe = True
    if perception_budget_ms is not None and perception_ms > perception_budget_ms:
        budget_ok = False
        degraded_safe = True

    budget_exceeded = evaluate_beta_latency_budgets(
        by_name=by_name,
        semantic_step_count=semantic_step_count,
    )
    if budget_exceeded:
        budget_ok = False
        degraded_safe = True

    return {
        "version": 1,
        "wall_clock_total_ms": round(total_ms, 3),
        "sections_ms": {k: round(v, 3) for k, v in sorted(by_name.items())},
        "rollup": {
            "perception_ms": round(perception_ms, 3),
            "validator_ms": round(validator_ms, 3),
            "runtime_decision_ms": round(decision_ms, 3),
            "repair_loop_ms": round(repair_ms, 3),
            "semantic_step_count": semantic_step_count,
            "budget_exceeded_summary": budget_exceeded,
        },
        "budget_ok": budget_ok,
        "degraded_safe": degraded_safe,
        "budget_limits": {
            "total_ms": budget_ms_total,
            "perception_ms": perception_budget_ms,
            "beta_latency_budget_ms": dict(BETA_LATENCY_BUDGETS_MS),
        },
        "budget_exceeded": budget_exceeded,
    }


__all__ = [
    "BETA_LATENCY_BUDGETS_MS",
    "LatencySection",
    "RuntimeLatencyCollector",
    "build_runtime_latency_report",
    "evaluate_beta_latency_budgets",
]
