"""FASE 7 — Benchmark real: tests gate de release."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import invalidate_settings_cache

from app.services.runtime.fase7_benchmark_runner import (
    Fase7RunReport,
    run_fase7_benchmark,
    run_fase7_suite,
)
from app.services.runtime.fase7_release_gate import (
    FASE7_ACCEPTANCE_STATUS,
    FASE7_REQUIRED_CONSECUTIVE_RUNS,
    aggregate_fase7_runs,
    evaluate_fase7_run_acceptance,
    fase7_batch_gate_violations,
    fase7_gate_violations,
)
from tests.fixtures.fase7_certified_release import (
    CERTIFIED_LIVE_BASELINE_PATH,
    FASE7_CERTIFIED_REQUIRED_RUNS,
    FASE7_CERTIFIED_TOTAL_RUNS,
)
from tests.fixtures.fase7_missions import FASE7_DEFAULT_SUITE
from tests.fixtures.fase7_missions.notepad_write_save import build_notepad_write_save_mission

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    os.environ.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
    invalidate_settings_cache()
    yield
    os.environ.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
    invalidate_settings_cache()


def _ok_run(**overrides) -> dict:
    base = {
        "run_index": 1,
        "status": "success",
        "coords_used": False,
        "smart_route_count": 0,
        "runtime_timeout": False,
        "needs_human": False,
        "human_retry": False,
        "blocked": False,
        "weakest_step": {
            "step_id": "s1",
            "strategy_used": "open_app:windows_search",
            "duration_ms": 50,
            "reason": "slowest_step",
        },
    }
    base.update(overrides)
    base["acceptance"] = evaluate_fase7_run_acceptance(base)
    return base


def _ok_suite_report(runs: int = 5, **overrides) -> dict:
    run_list = [_ok_run(run_index=i + 1) for i in range(runs)]
    base = {
        "scenario_id": "notepad_write_save",
        "mode": "dry-run",
        "required_runs": runs,
        "runs": run_list,
        "weakest_step": run_list[-1]["weakest_step"],
        "release_status": FASE7_ACCEPTANCE_STATUS,
        "aggregate": {
            "run_count": runs,
            "accepted_runs": runs,
            "consecutive_accepted_runs": runs,
            "coords_used_total": 0,
            "smart_route_count": 0,
            "runtime_timeout_count": 0,
            "needs_human_count": 0,
            "weakest_step": run_list[-1]["weakest_step"],
        },
    }
    base.update(overrides)
    return base


FASE7_NEGATIVE_CONDITIONS = [
    pytest.param({"coords_used": True}, "coords_used_nonzero", id="coords_used"),
    pytest.param({"smart_route_count": 1}, "smart_route_nonzero", id="smart_route"),
    pytest.param({"runtime_timeout": True, "status": "timeout"}, "runtime_timeout_nonzero", id="runtime_timeout"),
    pytest.param({"needs_human": True, "status": "needs_human"}, "needs_human_nonzero", id="needs_human"),
    pytest.param({"human_retry": True}, "needs_human_nonzero", id="human_retry"),
    pytest.param({"status": "failed"}, "run_not_accepted", id="status_failed"),
    pytest.param({"weakest_step": None}, "missing_run_weakest_step", id="missing_weakest"),
]


class TestFase7ReleaseGate:
    def test_ok_suite_no_violations(self):
        report = _ok_suite_report()
        assert fase7_gate_violations(report) == []

    def test_requires_five_consecutive(self):
        runs = [_ok_run(run_index=i + 1) for i in range(4)]
        runs.append(_ok_run(run_index=5, status="failed"))
        report = _ok_suite_report(runs=5)
        report["runs"] = runs
        report["aggregate"] = aggregate_fase7_runs(runs)
        report["release_status"] = "REJECTED"
        v = fase7_gate_violations(report)
        assert any("not_consecutive_accepted" in x for x in v)

    @pytest.mark.parametrize("run_overrides,expected_fragment", FASE7_NEGATIVE_CONDITIONS)
    def test_run_violation_detected(self, run_overrides, expected_fragment):
        run = _ok_run(**run_overrides)
        runs = [_ok_run(run_index=i + 1, **run_overrides) for i in range(5)]
        report = _ok_suite_report(runs=5)
        report["runs"] = runs
        report["aggregate"] = aggregate_fase7_runs(runs)
        report["release_status"] = "REJECTED"
        v = fase7_gate_violations(report)
        assert any(expected_fragment in x for x in v), f"got {v}"

    def test_batch_requires_five_scenarios(self):
        report = _ok_suite_report()
        batch = {
            "scenarios": [report],
            "aggregate": report["aggregate"],
            "weakest_step": report["weakest_step"],
            "release_status": FASE7_ACCEPTANCE_STATUS,
        }
        v = fase7_batch_gate_violations(batch)
        assert any("insufficient_scenarios" in x for x in v)


class TestFase7BenchmarkRunner:
    def test_default_suite_has_five_scenarios(self):
        assert len(FASE7_DEFAULT_SUITE) >= 5

    def test_dry_run_single_scenario_five_of_five(self):
        mission = build_notepad_write_save_mission()
        report = run_fase7_benchmark(mission, mode="dry-run", runs=5)
        blob = report.to_dict()
        assert blob["release_status"] == FASE7_ACCEPTANCE_STATUS
        assert blob["aggregate"]["consecutive_accepted_runs"] == 5
        assert blob["aggregate"]["coords_used_total"] == 0
        assert blob["aggregate"]["smart_route_count"] == 0
        assert blob["weakest_step"]
        assert all(r["weakest_step"] for r in blob["runs"])
        assert fase7_gate_violations(blob) == []

    def test_dry_run_suite_infra(self):
        batch = run_fase7_suite(mode="dry-run", runs=5)
        blob = batch.to_dict()
        assert len(blob["scenarios"]) >= 5
        assert blob["weakest_step"]
        assert blob["aggregate"]["coords_used_total"] == 0
        assert blob["aggregate"]["smart_route_count"] == 0
        for scenario in blob["scenarios"]:
            assert scenario["weakest_step"]
            assert scenario["release_status"] == FASE7_ACCEPTANCE_STATUS


def _load_fase7_script():
    path = ROOT / "scripts" / "run_fase7_benchmark.py"
    spec = importlib.util.spec_from_file_location("run_fase7_benchmark_gate", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestFase7CertifiedBaseline:
    def test_certified_live_baseline_passes_batch_gate(self):
        import json

        assert CERTIFIED_LIVE_BASELINE_PATH.is_file(), "missing certified baseline fixture"
        blob = json.loads(CERTIFIED_LIVE_BASELINE_PATH.read_text(encoding="utf-8"))
        assert blob.get("release_status") == FASE7_ACCEPTANCE_STATUS
        assert len(blob.get("scenarios") or []) >= 5
        agg = blob.get("aggregate") or {}
        assert int(agg.get("accepted_runs") or 0) == FASE7_CERTIFIED_TOTAL_RUNS
        assert int(agg.get("coords_used_total") or 0) == 0
        assert int(agg.get("smart_route_count") or 0) == 0
        assert int(agg.get("needs_human_count") or 0) == 0
        assert fase7_batch_gate_violations(
            blob,
            required_runs=FASE7_CERTIFIED_REQUIRED_RUNS,
        ) == []
        assert blob.get("suite") == "default"


class TestFase7GateClosureCriteria:
    def test_fase7_gate_runner_subprocess_exits_zero(self):
        env = os.environ.copy()
        env.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_fase7_benchmark.py"),
                "--suite",
                "default",
                "--mode",
                "dry-run",
                "--runs",
                str(FASE7_REQUIRED_CONSECUTIVE_RUNS),
                "--report",
                "none",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout

    @pytest.mark.parametrize("run_overrides,expected_fragment", FASE7_NEGATIVE_CONDITIONS[:4])
    def test_gate_script_exits_two_on_violation(self, monkeypatch, run_overrides, expected_fragment):
        mod = _load_fase7_script()
        bad_run = Fase7RunReport(
            run_index=1,
            status="success",
            coords_used=False,
            smart_route_count=0,
            weakest_step={"step_id": "s1", "duration_ms": 1},
        )
        for key, value in run_overrides.items():
            setattr(bad_run, key, value)
        bad_run.acceptance = evaluate_fase7_run_acceptance(bad_run.to_dict())

        from app.services.runtime.fase7_benchmark_runner import Fase7BenchmarkSuiteReport

        suite = Fase7BenchmarkSuiteReport(
            scenario_id="test",
            mode="dry-run",
            required_runs=5,
            runs=[bad_run] * 5,
            release_status="REJECTED",
        )
        suite.aggregate = {
            "consecutive_accepted_runs": 0,
            "coords_used_total": 1 if bad_run.coords_used else 0,
            "smart_route_count": bad_run.smart_route_count,
            "runtime_timeout_count": 1 if bad_run.runtime_timeout else 0,
            "needs_human_count": 1 if (bad_run.needs_human or bad_run.human_retry) else 0,
        }

        class _FakeBatch:
            def to_dict(self):
                return {
                    "scenarios": [suite.to_dict()],
                    "aggregate": suite.aggregate,
                    "weakest_step": bad_run.weakest_step,
                    "release_status": "ACCEPTED",
                }

        monkeypatch.setattr(mod, "run_fase7_suite", lambda **kw: _FakeBatch())
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_fase7_benchmark.py", "--suite", "default", "--mode", "dry-run"],
        )
        code = mod.main()
        assert code in (1, 2)
