"""FASE 5 — Runtime determinista: tests gate."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import Settings, invalidate_settings_cache
from app.services.missions.smart_runner_bridge import run_mission_smart
from app.services.runtime.fase5_deterministic_gate import (
    FASE5_ACCEPTANCE_STATUS,
    FASE5_REQUIRED_REPLAYS,
    FASE5_RUNTIME_ENTRYPOINT,
    apply_fase5_prod_runtime_policy,
    diff_strategy_paths,
    extract_strategy_path,
    fase5_gate_violations,
    is_fase5_deterministic_runtime_active,
)
from app.services.runtime.fase5_replay_runner import run_fase5_golden_replay
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    os.environ.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
    invalidate_settings_cache()
    yield
    os.environ.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
    invalidate_settings_cache()


def _ok_fase5_report(replays: int = 3, **overrides) -> dict:
    path = [{"step_id": "s1", "kind": "open_app", "strategy_used": "open_app:windows_search"}]
    runs = [
        {
            "run_index": i + 1,
            "status": "success",
            "coords_used": False,
            "smart_route_count": 0,
            "player_used": "smart",
            "legacy_fork": False,
            "strategy_path": path,
            "step_strategies": [{"step_id": "s1", "kind": "open_app", "strategy_used": "open_app:windows_search"}],
        }
        for i in range(replays)
    ]
    base = {
        "mode": "dry-run",
        "required_replays": replays,
        "runs": runs,
        "strategy_paths": [path] * replays,
        "strategy_path_diff": [],
        "coords_used": False,
        "smart_route_count": 0,
        "runtime_entrypoint": FASE5_RUNTIME_ENTRYPOINT,
        "forbidden_preferred_count": 0,
        "determinism_status": FASE5_ACCEPTANCE_STATUS,
    }
    base.update(overrides)
    return base


FASE5_NEGATIVE_CONDITIONS = [
    pytest.param({"strategy_path_diff": ["run2_step0:'a'!='b'"]}, "strategy_path_diff", id="path_diff"),
    pytest.param({"coords_used": True}, "coords_used", id="coords_used"),
    pytest.param({"smart_route_count": 1}, "smart_route_nonzero", id="smart_route"),
    pytest.param({"runtime_entrypoint": "legacy_fork"}, "runtime_entrypoint", id="wrong_entrypoint"),
    pytest.param({"forbidden_preferred_count": 1}, "forbidden_preferred", id="forbidden_preferred"),
    pytest.param({"determinism_status": "REJECTED"}, "determinism_status", id="rejected_status"),
]


class TestFase5DeterministicPolicy:
    def test_prod_disables_legacy_fallback(self):
        s = Settings(ENV="prod", SECRET_KEY="prod-secret-key-ok", DEBUG=False)
        allow, _ = apply_fase5_prod_runtime_policy(
            s,
            allow_legacy_fallback=True,
            use_auto_learning=True,
        )
        assert allow is False

    def test_prod_convergence_env_active(self, monkeypatch):
        monkeypatch.setenv("NEVLAN_FORCE_CONVERGENCE_PROFILE", "1")
        s = Settings(ENV="dev", SECRET_KEY="dev-secret-key-ok", DEBUG=True)
        assert is_fase5_deterministic_runtime_active(s)

    def test_bridge_blocks_legacy_in_prod(self, monkeypatch):
        from app.contracts.mission import Mission, MissionStatus

        monkeypatch.setenv("NEVLAN_FORCE_CONVERGENCE_PROFILE", "1")
        invalidate_settings_cache()
        m = Mission(name="legacy-only", status=MissionStatus.EXECUTABLE)
        decision = run_mission_smart(
            m,
            allow_legacy_fallback=True,
            run_legacy_player=lambda _m: None,
        )
        assert decision.blocked
        assert decision.player_used != "legacy"


class TestFase5StrategyPath:
    def test_extract_and_diff_empty_when_equal(self):
        a = [{"step_id": "s1", "kind": "open_app", "strategy_used": "open_app:x"}]
        b = [{"step_id": "s1", "kind": "open_app", "strategy_used": "open_app:x"}]
        assert diff_strategy_paths([a, b, a]) == []

    def test_diff_detects_strategy_change(self):
        a = [{"step_id": "s1", "kind": "open_app", "strategy_used": "open_app:a"}]
        b = [{"step_id": "s1", "kind": "open_app", "strategy_used": "open_app:b"}]
        diffs = diff_strategy_paths([a, b])
        assert diffs
        assert "strategy_used" in diffs[0] or "!" in diffs[0]


class TestFase5GateViolations:
    def test_ok_report_no_violations(self):
        assert fase5_gate_violations(_ok_fase5_report()) == []

    def test_insufficient_replays(self):
        report = _ok_fase5_report(replays=2)
        v = fase5_gate_violations(report, required_replays=3)
        assert any("insufficient_replays" in x for x in v)

    @pytest.mark.parametrize("overrides,expected_fragment", FASE5_NEGATIVE_CONDITIONS)
    def test_negative_conditions(self, overrides, expected_fragment):
        report = _ok_fase5_report()
        report.update(overrides)
        if "coords_used" in overrides and overrides["coords_used"]:
            report["runs"][0]["coords_used"] = True
        v = fase5_gate_violations(report)
        assert any(expected_fragment in x for x in v), v

    def test_legacy_player_run_detected(self):
        report = _ok_fase5_report()
        report["runs"][0]["player_used"] = "legacy"
        v = fase5_gate_violations(report)
        assert any("legacy_player" in x for x in v)


class TestFase5GoldenReplay:
    def test_three_replays_accepted(self):
        report = run_fase5_golden_replay(replays=3, prod_profile=False)
        blob = report.to_dict()
        assert blob["determinism_status"] == FASE5_ACCEPTANCE_STATUS
        assert blob["strategy_path_diff"] == []
        assert blob["coords_used"] is False
        assert blob["smart_route_count"] == 0
        assert len(blob["runs"]) == 3
        assert all(r["strategy_path"] for r in blob["runs"])
        assert fase5_gate_violations(blob) == []

    def test_golden_mission_no_forbidden_preferred(self):
        m = build_golden_search_chrome_youtube_mission()
        from app.services.runtime.fase5_deterministic_gate import (
            count_forbidden_preferred_in_mission,
        )

        assert count_forbidden_preferred_in_mission(m) == 0


def _load_fase5_script():
    path = ROOT / "scripts" / "run_fase5_gate.py"
    spec = importlib.util.spec_from_file_location("run_fase5_gate", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestFase5GateClosure:
    def test_subprocess_exits_zero(self):
        env = os.environ.copy()
        env.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_fase5_gate.py"),
                "--replays",
                str(FASE5_REQUIRED_REPLAYS),
                "--report",
                "none",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
