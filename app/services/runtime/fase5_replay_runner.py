"""FASE 5 — Golden replay runner (3× determinismo).

Ejecuta la misión golden 3 veces en dry-run (sin I/O) y verifica que
la secuencia de estrategias por paso sea idéntica.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.logger import log
from app.core.config import get_settings, invalidate_settings_cache
from app.core.paths import VAR_DIR
from app.services.missions.smart_runner_bridge import run_mission_smart
from app.services.runtime.execution_run_audit import record_execution_run_audit
from app.services.runtime.fase5_deterministic_gate import (
    FASE5_ACCEPTANCE_STATUS,
    FASE5_REJECTION_STATUS,
    FASE5_REQUIRED_REPLAYS,
    FASE5_RUNTIME_ENTRYPOINT,
    apply_fase5_prod_runtime_policy,
    count_forbidden_preferred_in_mission,
    count_forbidden_primary_in_path,
    diff_normalized_strategy_paths,
    diff_strategy_paths,
    extract_strategy_path,
)
from app.services.runtime.fase5_strategy_equivalence import (
    enrich_step_strategies_for_equivalence,
    extract_normalized_strategy_path,
)
from app.services.runtime.nevlan_runtime_convergence import (
    capture_runtime_flag_snapshot,
    convergence_profile_label,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)

_FASE5_OUTPUT_DIR = VAR_DIR / "runtime" / "fase5_replay"


@dataclass
class Fase5ReplayRunReport:
    run_index: int = 0
    status: str = ""
    duration_ms: int = 0
    coords_used: bool = False
    smart_route_count: int = 0
    player_used: str = ""
    legacy_fork: bool = False
    run_id: str = ""
    raw_strategy_path: List[Dict[str, str]] = field(default_factory=list)
    normalized_strategy_path: List[Dict[str, str]] = field(default_factory=list)
    strategy_path: List[Dict[str, str]] = field(default_factory=list)
    step_strategies: List[Dict[str, Any]] = field(default_factory=list)
    initial_state_normalized: bool = True
    environment_reset: Dict[str, Any] = field(default_factory=dict)
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_index": self.run_index,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "coords_used": self.coords_used,
            "smart_route_count": self.smart_route_count,
            "player_used": self.player_used,
            "legacy_fork": self.legacy_fork,
            "run_id": self.run_id,
            "raw_strategy_path": list(self.raw_strategy_path),
            "normalized_strategy_path": list(self.normalized_strategy_path),
            "strategy_path": list(self.strategy_path),
            "step_strategies": list(self.step_strategies),
            "initial_state_normalized": self.initial_state_normalized,
            "environment_reset": dict(self.environment_reset),
            "recorded_at": self.recorded_at,
        }


@dataclass
class Fase5ReplayReport:
    mode: str = "dry-run"
    required_replays: int = FASE5_REQUIRED_REPLAYS
    mission_id: str = ""
    runs: List[Fase5ReplayRunReport] = field(default_factory=list)
    strategy_paths: List[List[Dict[str, str]]] = field(default_factory=list)
    normalized_strategy_paths: List[List[Dict[str, str]]] = field(default_factory=list)
    strategy_path_diff: List[str] = field(default_factory=list)
    strategy_path_diff_normalized: List[str] = field(default_factory=list)
    coords_used: bool = False
    smart_route_count: int = 0
    runtime_entrypoint: str = FASE5_RUNTIME_ENTRYPOINT
    forbidden_preferred_count: int = 0
    determinism_status: str = FASE5_REJECTION_STATUS
    runtime_flags: Dict[str, bool] = field(default_factory=dict)
    profile_label: str = ""
    environment_reset_failures: int = 0
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "required_replays": self.required_replays,
            "mission_id": self.mission_id,
            "runs": [r.to_dict() for r in self.runs],
            "strategy_paths": list(self.strategy_paths),
            "normalized_strategy_paths": list(self.normalized_strategy_paths),
            "strategy_path_diff": list(self.strategy_path_diff),
            "strategy_path_diff_normalized": list(self.strategy_path_diff_normalized),
            "coords_used": self.coords_used,
            "smart_route_count": self.smart_route_count,
            "runtime_entrypoint": self.runtime_entrypoint,
            "forbidden_preferred_count": self.forbidden_preferred_count,
            "determinism_status": self.determinism_status,
            "runtime_flags": dict(self.runtime_flags),
            "profile_label": self.profile_label,
            "environment_reset_failures": self.environment_reset_failures,
            "recorded_at": self.recorded_at,
        }


def _apply_convergence_settings(*, prod: bool = True) -> None:
    import os

    if prod:
        os.environ["NEVLAN_FORCE_CONVERGENCE_PROFILE"] = "1"
    else:
        os.environ.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
    invalidate_settings_cache()


def _install_dry_run_executor() -> None:
    from app.services.missions.smart_executor import SmartMissionExecutor
    from app.services.runtime.golden_mission_runner import _dry_run_smart_executor

    _dry_run_smart_executor(SmartMissionExecutor)


def _execute_replay(
    mission: object,
    *,
    mode: str,
    run_index: int,
    settings: Any,
    timeout_sec: int = 0,
) -> Fase5ReplayRunReport:
    from app.services.missions.execution_truth_engine import compute_execution_truth

    started = time.time()
    report = Fase5ReplayRunReport(run_index=run_index)

    if mode == "dry-run":
        et = compute_execution_truth(mission)
        if not et.confirmed:
            report.status = "dry_run_truth_failed"
            report.strategy_path = []
            return report

    allow_legacy, use_auto_learning = apply_fase5_prod_runtime_policy(
        settings,
        allow_legacy_fallback=True,
        use_auto_learning=True,
        force_deterministic_replay=True,
    )

    decision = None
    timed_out = False
    run_timeout = int(timeout_sec or getattr(settings, "BETA_RUN_TIMEOUT_SEC", 600))

    def _invoke_smart_bridge():
        return run_mission_smart(
            mission,
            allow_legacy_fallback=allow_legacy,
            use_auto_learning=use_auto_learning,
            expert_mode=True,
        )

    if mode == "live":
        try:
            from app.services.runtime.fase5_live_environment_reset import (
                normalize_fase5_live_replay_environment,
            )

            reset = normalize_fase5_live_replay_environment(mission=mission)
            report.environment_reset = reset.to_dict()
            report.initial_state_normalized = reset.initial_state_normalized
        except Exception as exc:
            log.debug("[Fase5] environment reset: %s", exc)
            report.initial_state_normalized = False
            report.environment_reset = {
                "initial_state_normalized": False,
                "failure_code": "ENVIRONMENT_NOT_NORMALIZED",
                "reason": str(exc),
            }

        def _run_live_with_com():
            try:
                import uiautomation as auto

                with auto.UIAutomationInitializerInThread():
                    return _invoke_smart_bridge()
            except Exception:
                return _invoke_smart_bridge()

        if run_timeout > 0:
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(_run_live_with_com)
                try:
                    decision = fut.result(timeout=run_timeout)
                except FuturesTimeout:
                    timed_out = True
        else:
            decision = _run_live_with_com()
    else:
        decision = _invoke_smart_bridge()

    if timed_out:
        report.status = "timeout"
        report.duration_ms = run_timeout * 1000
        report.strategy_path = [{
            "step_id": "benchmark:run_timeout",
            "kind": "timeout",
            "strategy_used": "",
        }]
        return report

    audit = record_execution_run_audit(
        mission=mission,
        decision=decision,
        settings=settings,
    )
    step_strategies = [s.model_dump(mode="json") for s in audit.step_strategies]
    step_strategies = enrich_step_strategies_for_equivalence(
        step_strategies,
        mission=mission,
        decision=decision,
    )
    raw_strategy_path = extract_strategy_path(step_strategies)
    normalized_strategy_path = extract_normalized_strategy_path(
        step_strategies,
        run_coords_used=bool(audit.coords_used),
    )

    report.status = audit.status
    report.duration_ms = audit.duration_ms or int((time.time() - started) * 1000)
    report.coords_used = audit.coords_used
    report.smart_route_count = count_forbidden_primary_in_path(step_strategies)
    report.player_used = str(getattr(decision, "player_used", "") or "")
    report.legacy_fork = bool(
        getattr(decision, "used_fallback", False)
        or report.player_used == "legacy"
    )
    report.run_id = audit.run_id
    report.raw_strategy_path = raw_strategy_path
    report.normalized_strategy_path = normalized_strategy_path
    report.strategy_path = raw_strategy_path
    report.step_strategies = step_strategies
    return report


def run_fase5_golden_replay(
    *,
    mode: str = "dry-run",
    replays: int = FASE5_REQUIRED_REPLAYS,
    prod_profile: bool = True,
    export_path: Optional[Path] = None,
    timeout_sec: int = 0,
) -> Fase5ReplayReport:
    """3× replay golden; diff de estrategias debe ser vacío."""
    if mode not in ("dry-run", "live"):
        raise ValueError(f"mode desconocido: {mode!r}")

    _apply_convergence_settings(prod=prod_profile)
    settings = get_settings()
    flags = capture_runtime_flag_snapshot(settings)

    mission = build_golden_search_chrome_youtube_mission(name="fase5-golden-replay")
    forbidden_pref = count_forbidden_preferred_in_mission(mission)

    report = Fase5ReplayReport(
        mode=mode,
        required_replays=max(1, int(replays)),
        mission_id=str(getattr(mission, "id", "") or ""),
        forbidden_preferred_count=forbidden_pref,
        runtime_flags=flags,
        profile_label=convergence_profile_label(flags),
    )

    if mode == "dry-run":
        _install_dry_run_executor()

    raw_paths: List[List[Dict[str, str]]] = []
    normalized_paths: List[List[Dict[str, str]]] = []
    for i in range(report.required_replays):
        run_report = _execute_replay(
            mission,
            mode=mode,
            run_index=i + 1,
            settings=settings,
            timeout_sec=timeout_sec,
        )
        report.runs.append(run_report)
        raw_paths.append(run_report.raw_strategy_path)
        normalized_paths.append(run_report.normalized_strategy_path)
        if not run_report.initial_state_normalized:
            report.environment_reset_failures += 1

    report.strategy_paths = raw_paths
    report.normalized_strategy_paths = normalized_paths
    report.strategy_path_diff = diff_strategy_paths(
        [r.step_strategies for r in report.runs],
    )
    report.strategy_path_diff_normalized = diff_normalized_strategy_paths(
        normalized_paths,
    )
    report.coords_used = any(r.coords_used for r in report.runs)
    report.smart_route_count = sum(r.smart_route_count for r in report.runs)

    accepted = (
        not report.strategy_path_diff_normalized
        and not report.coords_used
        and report.smart_route_count == 0
        and report.environment_reset_failures == 0
        and forbidden_pref == 0
        and all(r.player_used == "smart" for r in report.runs)
        and all(not r.legacy_fork for r in report.runs)
        and len(report.runs) >= report.required_replays
    )
    report.determinism_status = (
        FASE5_ACCEPTANCE_STATUS if accepted else FASE5_REJECTION_STATUS
    )

    if export_path:
        write_fase5_report(report, export_path)
    return report


def write_fase5_report(report: Fase5ReplayReport, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def default_export_path() -> Path:
    return _FASE5_OUTPUT_DIR / f"fase5_replay_{int(time.time())}.json"


def latest_export_path() -> Path:
    return _FASE5_OUTPUT_DIR / "fase5_replay_latest.json"


__all__ = [
    "Fase5ReplayReport",
    "Fase5ReplayRunReport",
    "default_export_path",
    "latest_export_path",
    "run_fase5_golden_replay",
    "write_fase5_report",
]
