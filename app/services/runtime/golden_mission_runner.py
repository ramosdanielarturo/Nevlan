"""FASE 0.3 — Golden mission runner mínimo.

Ejecuta la misión golden canónica (Search→Chrome→YouTube→scroll) y produce
un reporte JSON con ``coords_used``, ``weakest_step``, ``status``,
``duration_ms`` y flags runtime activos.

Modos:
  * ``dry-run`` — replay ICE/SEP/truth + bridge completo, sin I/O SO (gate CI)
  * ``stub`` — executor mínimo (debug; no válido para ``make fase0-gate``)
  * ``gate`` — sólo truth gate + perfil (sin ejecutor)
  * ``live`` — ejecución real (requiere escritorio + Chrome)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.contracts.mission import Mission
from app.core.config import Settings, get_settings, invalidate_settings_cache
from app.core.paths import VAR_DIR
from app.services.missions.mission_truth_gate import gate_decision
from app.services.missions.smart_runner_bridge import RunnerDecision, run_mission_smart
from app.services.runtime.execution_run_audit import record_execution_run_audit
from app.services.runtime.nevlan_runtime_convergence import (
    capture_runtime_flag_snapshot,
    convergence_profile_label,
    detect_profile_drift,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)

_GOLDEN_OUTPUT_DIR = VAR_DIR / "runtime" / "golden_runner"


@dataclass
class GoldenMissionRunReport:
    """Reporte exportable FASE 0 gate."""

    status: str = ""
    duration_ms: int = 0
    coords_used: bool = False
    weakest_step: Optional[Dict[str, Any]] = None
    executive_source: str = ""
    legacy_graph_ignored: bool = False
    compiled_graph_executed: bool = False
    blocked: bool = False
    block_reason: str = ""
    mode: str = "stub"
    mission_id: str = ""
    run_id: str = ""
    runtime_flags: Dict[str, bool] = field(default_factory=dict)
    profile_label: str = ""
    profile_drift: List[str] = field(default_factory=list)
    step_strategies: List[Dict[str, Any]] = field(default_factory=list)
    gate_executable: bool = False
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "duration_ms": self.duration_ms,
            "coords_used": self.coords_used,
            "weakest_step": self.weakest_step,
            "executive_source": self.executive_source,
            "legacy_graph_ignored": self.legacy_graph_ignored,
            "compiled_graph_executed": self.compiled_graph_executed,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "mode": self.mode,
            "mission_id": self.mission_id,
            "run_id": self.run_id,
            "runtime_flags": dict(self.runtime_flags),
            "profile_label": self.profile_label,
            "profile_drift": list(self.profile_drift),
            "step_strategies": list(self.step_strategies),
            "gate_executable": self.gate_executable,
            "recorded_at": self.recorded_at,
        }


def _apply_convergence_settings(*, prod: bool = False) -> Settings:
    """Aplica perfil convergencia vía env + singleton cacheado."""
    import os

    if prod:
        os.environ["NEVLAN_FORCE_CONVERGENCE_PROFILE"] = "1"
    else:
        os.environ.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
    invalidate_settings_cache()
    return get_settings()


def _dry_run_smart_executor(monkeypatch_target: Any) -> None:
    """Executor sin I/O: registra preferred_strategy tras pipeline completo."""

    def fake_run(self_, steps, **_kw):
        from app.services.missions.smart_executor import MissionResult, StepOutcome
        from app.services.runtime.nevlan_runtime_convergence import (
            is_forbidden_primary_strategy,
        )

        res = MissionResult(
            run_id=f"golden-dry-run-{int(time.time())}",
            status="success",
        )
        for s in steps:
            pref = str((s.params or {}).get("preferred_strategy") or f"{s.kind}:dry_run")
            if is_forbidden_primary_strategy(pref):
                res.status = "failed"
            res.outcomes.append(
                StepOutcome(
                    step_id=s.id,
                    kind=s.kind,
                    status="success" if res.status == "success" else "failed",
                    strategy_used=pref,
                    duration_ms=50,
                )
            )
        return res

    monkeypatch_target.run_mission = fake_run


def _stub_smart_executor(monkeypatch_target: Any) -> None:
    """Stub SmartMissionExecutor.run_mission para CI sin SO."""

    def fake_run(self_, steps, **_kw):
        from app.services.missions.smart_executor import MissionResult, StepOutcome

        res = MissionResult(run_id=f"golden-stub-{int(time.time())}", status="success")
        for s in steps:
            pref = (s.params or {}).get("preferred_strategy") or f"{s.kind}:stub"
            res.outcomes.append(
                StepOutcome(
                    step_id=s.id,
                    kind=s.kind,
                    status="success",
                    strategy_used=str(pref),
                    duration_ms=42,
                )
            )
        return res

    monkeypatch_target.run_mission = fake_run


def run_golden_mission(
    *,
    mode: str = "stub",
    prod_profile: bool = True,
    export_path: Optional[Path] = None,
    stub_executor: Optional[Callable[[], None]] = None,
) -> GoldenMissionRunReport:
    """Ejecuta misión golden y devuelve reporte FASE 0.

    Args:
        mode: ``dry-run`` | ``stub`` | ``gate`` | ``live``
        prod_profile: aplica perfil convergencia producción
        export_path: ruta JSON opcional
        stub_executor: callback para instalar stub (tests)
    """
    started = time.time()
    _apply_convergence_settings(prod=prod_profile)
    settings = get_settings()
    flags = capture_runtime_flag_snapshot(settings)

    mission = build_golden_search_chrome_youtube_mission(name="fase0-golden")
    gate = gate_decision(mission)

    report = GoldenMissionRunReport(
        mode=mode,
        mission_id=str(getattr(mission, "id", "") or ""),
        runtime_flags=flags,
        profile_label=convergence_profile_label(flags),
        profile_drift=detect_profile_drift(flags) if prod_profile else [],
        gate_executable=bool(gate.is_executable),
    )

    if mode == "gate":
        report.status = "gate_ok" if gate.is_executable else "gate_blocked"
        report.blocked = not gate.is_executable
        report.block_reason = ";".join(gate.blockers[:3]) if gate.blockers else ""
        report.executive_source = gate.source
        report.duration_ms = int((time.time() - started) * 1000)
        if export_path:
            _write_report(report, export_path)
        return report

    decision: Optional[RunnerDecision] = None

    if mode in ("stub", "dry-run"):
        from app.services.missions.smart_executor import SmartMissionExecutor

        if stub_executor is not None:
            stub_executor()
        elif mode == "dry-run":
            from app.services.missions.execution_truth_engine import compute_execution_truth

            et = compute_execution_truth(mission)
            if not et.confirmed:
                report.status = "dry_run_truth_failed"
                report.blocked = True
                report.block_reason = ";".join(et.reasons[:3]) if et.reasons else "truth"
                report.duration_ms = int((time.time() - started) * 1000)
                if export_path:
                    _write_report(report, export_path)
                return report
            _dry_run_smart_executor(SmartMissionExecutor)
        else:
            _stub_smart_executor(SmartMissionExecutor)

        decision = run_mission_smart(
            mission,
            allow_legacy_fallback=False,
            use_auto_learning=False,
            expert_mode=True,
        )
    elif mode == "live":
        decision = run_mission_smart(
            mission,
            allow_legacy_fallback=False,
            use_auto_learning=False,
            expert_mode=True,
        )
    else:
        raise ValueError(f"mode desconocido: {mode!r}")

    weakest: Optional[Dict[str, Any]] = None
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
            weakest = dict(beta.weakest_steps[0])
    except Exception:
        pass

    audit = record_execution_run_audit(
        mission=mission,
        decision=decision,
        settings=settings,
        weakest_step=weakest,
    )

    report.status = audit.status
    report.duration_ms = audit.duration_ms or int((time.time() - started) * 1000)
    report.coords_used = audit.coords_used
    report.weakest_step = audit.weakest_step
    report.executive_source = audit.executive_source
    report.legacy_graph_ignored = audit.legacy_graph_ignored
    report.compiled_graph_executed = bool(
        getattr(decision, "source", "") == "compiled_execution_graph"
        and not getattr(decision, "blocked", False)
    )
    report.blocked = audit.blocked
    report.block_reason = audit.block_reason
    report.run_id = audit.run_id
    report.step_strategies = [s.model_dump(mode="json") for s in audit.step_strategies]

    if export_path:
        _write_report(report, export_path)
    return report


def _write_report(report: GoldenMissionRunReport, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def default_export_path() -> Path:
    ts = int(time.time())
    return _GOLDEN_OUTPUT_DIR / f"golden_run_{ts}.json"


def latest_export_path() -> Path:
    return _GOLDEN_OUTPUT_DIR / "golden_run_latest.json"


__all__ = [
    "GoldenMissionRunReport",
    "default_export_path",
    "latest_export_path",
    "run_golden_mission",
]
