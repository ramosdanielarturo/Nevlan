"""FASE 7 — Benchmark runner robusto (gate de release).

CLI: ``scripts/run_fase7_benchmark.py`` — ``--mission``, ``--runs 5``,
``--timeout``, ``--report json``.

Modos:
  * ``dry-run`` — infra CI: bridge + executor simulado, sin I/O SO
  * ``live`` — benchmark real en escritorio (5/5 consecutivos)
"""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.contracts.mission import Mission
from app.services.runtime.fase7_release_gate import (
    FASE7_ACCEPTANCE_STATUS,
    FASE7_REJECTION_STATUS,
    FASE7_REQUIRED_CONSECUTIVE_RUNS,
    aggregate_fase7_runs,
    count_smart_route_usage,
    evaluate_fase7_run_acceptance,
)

_FASE7_OUTPUT_DIR = None  # lazy: VAR_DIR / "runtime" / "fase7_benchmark"


def _output_dir():
    global _FASE7_OUTPUT_DIR
    if _FASE7_OUTPUT_DIR is None:
        from app.core.paths import VAR_DIR

        _FASE7_OUTPUT_DIR = VAR_DIR / "runtime" / "fase7_benchmark"
    return _FASE7_OUTPUT_DIR


@dataclass
class Fase7RunReport:
    """Reporte de una corrida individual."""

    run_index: int = 0
    status: str = ""
    duration_ms: int = 0
    coords_used: bool = False
    smart_route_count: int = 0
    runtime_timeout: bool = False
    needs_human: bool = False
    human_retry: bool = False
    blocked: bool = False
    block_reason: str = ""
    acceptance: str = FASE7_REJECTION_STATUS
    run_id: str = ""
    mission_id: str = ""
    scenario_id: str = ""
    weakest_step: Optional[Dict[str, Any]] = None
    step_strategies: List[Dict[str, Any]] = field(default_factory=list)
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_index": self.run_index,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "coords_used": self.coords_used,
            "smart_route_count": self.smart_route_count,
            "runtime_timeout": self.runtime_timeout,
            "needs_human": self.needs_human,
            "human_retry": self.human_retry,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "acceptance": self.acceptance,
            "run_id": self.run_id,
            "mission_id": self.mission_id,
            "scenario_id": self.scenario_id,
            "weakest_step": self.weakest_step,
            "step_strategies": list(self.step_strategies),
            "recorded_at": self.recorded_at,
        }


@dataclass
class Fase7BenchmarkSuiteReport:
    """Reporte agregado de suite / escenario."""

    scenario_id: str = ""
    category: str = ""
    description: str = ""
    mode: str = "dry-run"
    required_runs: int = FASE7_REQUIRED_CONSECUTIVE_RUNS
    runs: List[Fase7RunReport] = field(default_factory=list)
    aggregate: Dict[str, Any] = field(default_factory=dict)
    weakest_step: Optional[Dict[str, Any]] = None
    release_status: str = FASE7_REJECTION_STATUS
    runtime_flags: Dict[str, bool] = field(default_factory=dict)
    profile_label: str = ""
    profile_drift: List[str] = field(default_factory=list)
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "category": self.category,
            "description": self.description,
            "mode": self.mode,
            "required_runs": self.required_runs,
            "runs": [r.to_dict() for r in self.runs],
            "aggregate": dict(self.aggregate),
            "weakest_step": self.weakest_step,
            "release_status": self.release_status,
            "runtime_flags": dict(self.runtime_flags),
            "profile_label": self.profile_label,
            "profile_drift": list(self.profile_drift),
            "recorded_at": self.recorded_at,
        }


@dataclass
class Fase7BenchmarkBatchReport:
    """Varios escenarios en una ejecución (suite default)."""

    suite: str = "default"
    mode: str = "dry-run"
    required_runs: int = FASE7_REQUIRED_CONSECUTIVE_RUNS
    scenarios: List[Fase7BenchmarkSuiteReport] = field(default_factory=list)
    aggregate: Dict[str, Any] = field(default_factory=dict)
    weakest_step: Optional[Dict[str, Any]] = None
    release_status: str = FASE7_REJECTION_STATUS
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite": self.suite,
            "mode": self.mode,
            "required_runs": self.required_runs,
            "scenarios": [s.to_dict() for s in self.scenarios],
            "aggregate": dict(self.aggregate),
            "weakest_step": self.weakest_step,
            "release_status": self.release_status,
            "recorded_at": self.recorded_at,
        }


def _apply_convergence_settings(*, prod: bool = True):
    import os

    from app.core.config import get_settings, invalidate_settings_cache

    if prod:
        os.environ["NEVLAN_FORCE_CONVERGENCE_PROFILE"] = "1"
    else:
        os.environ.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
    invalidate_settings_cache()
    return get_settings()


def _dry_run_smart_executor(target: Any) -> None:
    from app.services.runtime.nevlan_runtime_convergence import (
        is_forbidden_primary_strategy,
    )

    def fake_run(self_, steps, **_kw):
        from app.services.missions.smart_executor import MissionResult, StepOutcome

        run_id = f"fase7-dry-{uuid.uuid4().hex[:8]}"
        res = MissionResult(run_id=run_id, status="success")
        for i, s in enumerate(steps):
            pref = str((s.params or {}).get("preferred_strategy") or f"{s.kind}:dry_run")
            if is_forbidden_primary_strategy(pref):
                res.status = "failed"
            dur = 40 + (i * 15)
            res.outcomes.append(
                StepOutcome(
                    step_id=s.id,
                    kind=s.kind,
                    status="success" if res.status == "success" else "failed",
                    strategy_used=pref,
                    duration_ms=dur,
                )
            )
        return res

    target.run_mission = fake_run


def _pick_weakest_from_steps(steps: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not steps:
        return None
    failed = [s for s in steps if str(s.get("status") or "") not in ("success", "skipped")]
    if failed:
        s = failed[0]
        return {
            "step_id": s.get("step_id"),
            "kind": s.get("kind"),
            "strategy_used": s.get("strategy_used"),
            "duration_ms": s.get("duration_ms"),
            "reason": "failed_step",
        }
    slowest = max(steps, key=lambda s: int(s.get("duration_ms") or 0))
    return {
        "step_id": slowest.get("step_id"),
        "kind": slowest.get("kind"),
        "strategy_used": slowest.get("strategy_used"),
        "duration_ms": slowest.get("duration_ms"),
        "reason": "slowest_step",
    }


def _compute_weakest_step(
    mission: Mission,
    decision: Optional[Any],
    step_strategies: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    try:
        from app.services.runtime.beta_runtime_acceptance import (
            compute_beta_runtime_acceptance_report,
        )

        beta = compute_beta_runtime_acceptance_report(
            mission,
            run_id=getattr(getattr(decision, "result", None), "run_id", "") or "",
            mission_result=getattr(decision, "result", None),
        )
        if beta.weakest_steps:
            return dict(beta.weakest_steps[0])
    except Exception:
        pass
    return _pick_weakest_from_steps(step_strategies)


def _needs_human_from_decision(decision: Optional[Any]) -> bool:
    if decision is None:
        return False
    if getattr(decision, "blocked", False):
        return True
    result = getattr(decision, "result", None)
    status = str(getattr(result, "status", "") or "")
    return status in ("stopped_for_human", "needs_human", "failed")


def _human_retry_from_decision(decision: Optional[Any]) -> bool:
    result = getattr(decision, "result", None)
    for outcome in getattr(result, "outcomes", None) or []:
        if str(getattr(outcome, "status", "") or "") in ("needs_human", "retry"):
            return True
    return False


def _execute_single_run(
    mission: Mission,
    *,
    mode: str,
    run_index: int,
    scenario_id: str,
    timeout_sec: int,
    settings: Any,
) -> Fase7RunReport:
    from app.services.missions.execution_truth_engine import compute_execution_truth
    from app.services.missions.smart_runner_bridge import run_mission_smart
    from app.services.runtime.execution_run_audit import record_execution_run_audit

    started = time.time()
    report = Fase7RunReport(
        run_index=run_index,
        mission_id=str(getattr(mission, "id", "") or ""),
        scenario_id=scenario_id,
    )

    decision: Optional[Any] = None
    timed_out = False

    if mode == "dry-run":
        from app.services.missions.smart_executor import SmartMissionExecutor

        et = compute_execution_truth(mission)
        if not et.confirmed:
            report.status = "dry_run_truth_failed"
            report.blocked = True
            report.block_reason = ";".join(et.reasons[:3]) if et.reasons else "truth"
            report.duration_ms = int((time.time() - started) * 1000)
            report.weakest_step = {
                "step_id": "truth_gate",
                "reason": "execution_truth_failed",
                "strategy_used": "",
                "duration_ms": report.duration_ms,
            }
            report.acceptance = evaluate_fase7_run_acceptance(report.to_dict())
            return report

        _dry_run_smart_executor(SmartMissionExecutor)
        decision = run_mission_smart(
            mission,
            allow_legacy_fallback=False,
            use_auto_learning=False,
            expert_mode=True,
        )
    elif mode == "live":
        run_timeout = max(1, int(timeout_sec or getattr(settings, "BETA_RUN_TIMEOUT_SEC", 600)))
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(
                run_mission_smart,
                mission,
                allow_legacy_fallback=False,
                use_auto_learning=False,
                expert_mode=True,
            )
            try:
                decision = fut.result(timeout=run_timeout)
            except FuturesTimeout:
                timed_out = True
    else:
        raise ValueError(f"mode desconocido: {mode!r}")

    if timed_out:
        report.status = "timeout"
        report.runtime_timeout = True
        report.duration_ms = int(timeout_sec * 1000)
        report.weakest_step = {
            "step_id": "benchmark:run_timeout",
            "reason": "runtime_timeout",
            "strategy_used": "",
            "duration_ms": report.duration_ms,
        }
        report.acceptance = evaluate_fase7_run_acceptance(report.to_dict())
        return report

    step_strategies: List[Dict[str, Any]] = []
    weakest: Optional[Dict[str, Any]] = None
    audit = None

    if decision is not None:
        audit = record_execution_run_audit(
            mission=mission,
            decision=decision,
            settings=settings,
            weakest_step=None,
        )
        step_strategies = [s.model_dump(mode="json") for s in audit.step_strategies]
        weakest = audit.weakest_step or _compute_weakest_step(
            mission,
            decision,
            step_strategies,
        )

        report.status = audit.status
        report.duration_ms = audit.duration_ms or int((time.time() - started) * 1000)
        report.coords_used = audit.coords_used
        report.blocked = audit.blocked
        report.block_reason = audit.block_reason
        report.run_id = audit.run_id
        report.step_strategies = step_strategies
        report.smart_route_count = count_smart_route_usage(step_strategies)
        report.needs_human = _needs_human_from_decision(decision)
        report.human_retry = _human_retry_from_decision(decision)
    else:
        report.status = "failed"
        report.duration_ms = int((time.time() - started) * 1000)

    report.weakest_step = weakest or {
        "step_id": "unknown",
        "reason": "synthetic_fallback",
        "strategy_used": "",
        "duration_ms": report.duration_ms,
    }
    report.acceptance = evaluate_fase7_run_acceptance(report.to_dict())
    return report


def _finalize_suite_report(suite: Fase7BenchmarkSuiteReport) -> Fase7BenchmarkSuiteReport:
    run_dicts = [r.to_dict() for r in suite.runs]
    suite.aggregate = aggregate_fase7_runs(run_dicts)
    suite.weakest_step = suite.aggregate.get("weakest_step")
    consecutive = int(suite.aggregate.get("consecutive_accepted_runs") or 0)
    if consecutive >= suite.required_runs:
        suite.release_status = FASE7_ACCEPTANCE_STATUS
    else:
        suite.release_status = FASE7_REJECTION_STATUS
    return suite


def run_fase7_benchmark(
    mission: Mission,
    *,
    scenario_id: str = "",
    category: str = "",
    description: str = "",
    mode: str = "dry-run",
    runs: int = FASE7_REQUIRED_CONSECUTIVE_RUNS,
    timeout_sec: int = 0,
    prod_profile: bool = True,
    stop_on_first_rejection: bool = True,
) -> Fase7BenchmarkSuiteReport:
    """Ejecuta N corridas consecutivas de un escenario."""
    settings = _apply_convergence_settings(prod=prod_profile)
    from app.services.runtime.nevlan_runtime_convergence import (
        capture_runtime_flag_snapshot,
        convergence_profile_label,
        detect_profile_drift,
    )
    from app.services.missions.mission_truth_gate import gate_decision

    flags = capture_runtime_flag_snapshot(settings)

    meta = dict(getattr(mission, "metadata", None) or {})
    f7 = {}
    tags = list(getattr(mission, "tags", None) or [])
    for tag in tags:
        if str(tag).startswith("fase7:") and not str(tag).startswith("fase7_cat:"):
            f7["id"] = str(tag).split(":", 1)[1]
        if str(tag).startswith("fase7_cat:"):
            f7["category"] = str(tag).split(":", 1)[1]
    scenario_id = scenario_id or str(f7.get("id") or mission.name or "fase7")
    category = category or str(f7.get("category") or "")
    description = description or str(getattr(mission, "description", "") or "")

    suite = Fase7BenchmarkSuiteReport(
        scenario_id=scenario_id,
        category=category,
        description=description,
        mode=mode,
        required_runs=max(1, int(runs)),
        runtime_flags=flags,
        profile_label=convergence_profile_label(flags),
        profile_drift=detect_profile_drift(flags) if prod_profile else [],
    )

    gate = gate_decision(mission)
    if not gate.is_executable and mode == "live":
        suite.runs.append(
            Fase7RunReport(
                run_index=1,
                status="gate_blocked",
                blocked=True,
                block_reason=";".join(gate.blockers[:3]) if gate.blockers else "gate",
                mission_id=str(getattr(mission, "id", "") or ""),
                scenario_id=scenario_id,
                weakest_step={
                    "step_id": "truth_gate",
                    "reason": "gate_blocked",
                    "strategy_used": "",
                    "duration_ms": 0,
                },
            )
        )
        return _finalize_suite_report(suite)

    effective_timeout = int(timeout_sec or getattr(settings, "BETA_RUN_TIMEOUT_SEC", 600))

    for i in range(suite.required_runs):
        run_report = _execute_single_run(
            mission,
            mode=mode,
            run_index=i + 1,
            scenario_id=scenario_id,
            timeout_sec=effective_timeout,
            settings=settings,
        )
        suite.runs.append(run_report)
        if stop_on_first_rejection and run_report.acceptance != FASE7_ACCEPTANCE_STATUS:
            break

    return _finalize_suite_report(suite)


def run_fase7_suite(
    scenarios: Optional[Sequence[Any]] = None,
    *,
    mode: str = "dry-run",
    runs: int = FASE7_REQUIRED_CONSECUTIVE_RUNS,
    timeout_sec: int = 0,
    prod_profile: bool = True,
    suite_name: str = "default",
) -> Fase7BenchmarkBatchReport:
    """Ejecuta la suite mínima de producto (≥5 escenarios)."""
    from tests.fixtures.fase7_missions import FASE7_DEFAULT_SUITE

    specs = list(scenarios or FASE7_DEFAULT_SUITE)
    batch = Fase7BenchmarkBatchReport(
        suite=suite_name,
        mode=mode,
        required_runs=max(1, int(runs)),
    )

    all_runs: List[Dict[str, Any]] = []
    for spec in specs:
        mission = spec.builder()
        suite_report = run_fase7_benchmark(
            mission,
            scenario_id=spec.scenario_id,
            category=spec.category,
            description=spec.description,
            mode=mode,
            runs=runs,
            timeout_sec=timeout_sec,
            prod_profile=prod_profile,
        )
        batch.scenarios.append(suite_report)
        all_runs.extend(r.to_dict() for r in suite_report.runs)

    batch.aggregate = aggregate_fase7_runs(all_runs)
    batch.weakest_step = batch.aggregate.get("weakest_step")
    all_accepted = all(
        s.release_status == FASE7_ACCEPTANCE_STATUS for s in batch.scenarios
    )
    batch.release_status = (
        FASE7_ACCEPTANCE_STATUS if all_accepted else FASE7_REJECTION_STATUS
    )
    return batch


def write_fase7_report(report: Any, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict() if hasattr(report, "to_dict") else dict(report)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def default_export_path(*, prefix: str = "fase7") -> Path:
    return _output_dir() / f"{prefix}_run_{int(time.time())}.json"


def latest_export_path(*, prefix: str = "fase7") -> Path:
    return _output_dir() / f"{prefix}_run_latest.json"


__all__ = [
    "Fase7BenchmarkBatchReport",
    "Fase7BenchmarkSuiteReport",
    "Fase7RunReport",
    "default_export_path",
    "latest_export_path",
    "run_fase7_benchmark",
    "run_fase7_suite",
    "write_fase7_report",
]
