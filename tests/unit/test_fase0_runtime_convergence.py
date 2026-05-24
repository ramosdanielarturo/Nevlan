"""FASE 0 — Convergencia brutal: tests unitarios gate endurecido."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import Settings, invalidate_settings_cache
from app.services.missions.mission_truth_gate import gate_decision
from app.services.missions.smart_runner_bridge import run_mission_smart
from app.services.runtime.golden_mission_runner import (
    GoldenMissionRunReport,
    run_golden_mission,
)
from app.services.runtime.nevlan_runtime_convergence import (
    ARSS_DISABLED_IN_PROD_RATIONALE,
    PRODUCTION_CONVERGENCE_CANONICAL,
    capture_runtime_flag_snapshot,
    convergence_profile_label,
    detect_profile_drift,
    enforce_single_executive_source,
    fase0_gate_violations,
    is_forbidden_primary_strategy,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)

ROOT = Path(__file__).resolve().parents[2]

# Matriz de condiciones negativas del gate (criterio cierre FASE 0).
GATE_NEGATIVE_CONDITIONS = [
    pytest.param(
        {"profile_drift": ["ARSS_ENABLED: got=True want=False"]},
        "profile_drift",
        id="profile_drift",
    ),
    pytest.param(
        {"executive_source": "compiled_execution_graph"},
        "executive_source",
        id="executive_source",
    ),
    pytest.param(
        {"compiled_graph_executed": True},
        "compiled_graph_executed_with_sep",
        id="compiled_graph_executed",
    ),
    pytest.param({"coords_used": True}, "coords_used", id="coords_used"),
    pytest.param({"step_strategies": []}, "missing_step_strategies", id="missing_step_strategies"),
    pytest.param(
        {"step_strategies": [{"step_id": "s", "strategy_used": "open_site:smart_route"}]},
        "forbidden_primary",
        id="forbidden_primary_smart_route",
    ),
    pytest.param({"mode": "stub"}, "non_representative_mode", id="non_representative_mode_stub"),
]


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    os.environ.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
    invalidate_settings_cache()
    yield
    os.environ.pop("NEVLAN_FORCE_CONVERGENCE_PROFILE", None)
    invalidate_settings_cache()


def _ok_report(**overrides):
    base = {
        "status": "success",
        "coords_used": False,
        "executive_source": "semantic_execution_plan",
        "legacy_graph_ignored": True,
        "compiled_graph_executed": False,
        "profile_drift": [],
        "mode": "dry-run",
        "run_id": "golden-dry-run-1",
        "step_strategies": [
            {"step_id": "s1", "strategy_used": "open_app:windows_search"},
        ],
    }
    base.update(overrides)
    return base


def _good_gate_report(**overrides) -> GoldenMissionRunReport:
    report = GoldenMissionRunReport(
        status="success",
        mode="dry-run",
        run_id="gate-negative-run",
        executive_source="semantic_execution_plan",
        legacy_graph_ignored=True,
        compiled_graph_executed=False,
        coords_used=False,
        step_strategies=[{"step_id": "s1", "strategy_used": "open_app:windows_search"}],
        gate_executable=True,
        profile_drift=[],
    )
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


def _load_gate_script_module():
    path = ROOT / "scripts" / "run_golden_mission.py"
    spec = importlib.util.spec_from_file_location("run_golden_mission_gate", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _invoke_gate_script(
    monkeypatch,
    tmp_path: Path,
    report: GoldenMissionRunReport,
    audit_path: Path,
) -> int:
    mod = _load_gate_script_module()
    monkeypatch.setattr(mod, "run_golden_mission", lambda **_kw: report)
    monkeypatch.setattr(mod, "EXECUTION_RUN_AUDIT_JSONL", audit_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_golden_mission.py",
            "--mode",
            "dry-run",
            "--export",
            str(tmp_path / "out.json"),
        ],
    )
    return int(mod.main())


class TestProductionConvergenceProfile:
    def test_prod_applies_canonical_flags(self):
        s = Settings(ENV="prod", SECRET_KEY="prod-secret-key-ok", DEBUG=False)
        snap = capture_runtime_flag_snapshot(s)
        assert snap["BETA_PRIVATE_RUNTIME_PROFILE"] is True
        assert snap["PCOF_ENABLED"] is True
        assert snap["OSUE_ENABLED"] is True
        assert snap["ARSS_ENABLED"] is False
        assert detect_profile_drift(snap) == []

    def test_prod_ignores_constructor_contradictions(self):
        s = Settings(
            ENV="prod",
            SECRET_KEY="prod-secret-key-ok",
            DEBUG=False,
            ARSS_ENABLED=True,
            BETA_PRIVATE_RUNTIME_PROFILE=False,
            PCOF_ENABLED=False,
            OSUE_ENABLED=False,
        )
        assert s.ARSS_ENABLED is False
        assert s.PCOF_ENABLED is True
        assert s.OSUE_ENABLED is True
        assert s.BETA_PRIVATE_RUNTIME_PROFILE is True

    def test_prod_ignores_os_environ_contradictions(self, monkeypatch):
        monkeypatch.setenv("ENV", "prod")
        monkeypatch.setenv("ARSS_ENABLED", "true")
        monkeypatch.setenv("PCOF_ENABLED", "false")
        monkeypatch.setenv("BETA_PRIVATE_RUNTIME_PROFILE", "false")
        monkeypatch.setenv("OSUE_ENABLED", "false")
        invalidate_settings_cache()
        s = Settings(ENV="prod", SECRET_KEY="prod-secret-key-ok", DEBUG=False)
        assert s.ARSS_ENABLED is False
        assert s.PCOF_ENABLED is True
        assert s.BETA_PRIVATE_RUNTIME_PROFILE is True

    def test_arss_disabled_rationale_documented(self):
        assert "FASE 0" in ARSS_DISABLED_IN_PROD_RATIONALE
        assert PRODUCTION_CONVERGENCE_CANONICAL["ARSS_ENABLED"] is False

    def test_profile_label_converged(self):
        assert convergence_profile_label(dict(PRODUCTION_CONVERGENCE_CANONICAL)) == "beta_runtime_converged"


class TestSingleExecutiveSource:
    def test_enforce_marks_legacy_debug_only(self):
        m = build_golden_search_chrome_youtube_mission()
        m.legacy_compiled_graph_purpose = "execution"
        assert enforce_single_executive_source(m) == "semantic_execution_plan"
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"

    def test_gate_uses_semantic_plan(self):
        m = build_golden_search_chrome_youtube_mission()
        dec = gate_decision(m)
        assert dec.is_executable
        assert dec.source == "semantic_execution_plan"

    def test_sep_present_triggers_ceg_guard(self):
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )

        m = build_golden_search_chrome_youtube_mission()
        assert has_valid_semantic_execution_plan(m)

    def test_ceg_blocked_when_plan_empty_but_sep_on_mission(self, monkeypatch):
        import app.services.missions.smart_runner_bridge as bridge
        from app.services.missions.semantic_execution_plan import SemanticExecutionPlan

        empty = SemanticExecutionPlan(steps=[])
        m = build_golden_search_chrome_youtube_mission()
        sep = dict(m.semantic_execution_plan or {})
        sep["source"] = "intent_collapse_engine"
        m.semantic_execution_plan = sep

        ceg_calls = []

        def _track_ceg(*_a, **_k):
            ceg_calls.append(1)
            raise AssertionError("CEG no debe ejecutar")

        monkeypatch.setattr(
            bridge,
            "apply_promotion_to_mission",
            lambda _m: type("R", (), {"plan": empty, "promotions": []})(),
        )
        monkeypatch.setattr(
            bridge,
            "build_semantic_execution_plan",
            lambda *_a, **_k: empty,
        )
        monkeypatch.setattr(
            "app.services.missions.tel_sep_promoter.run_tel_sep_hybrid_promotion",
            lambda *_a, **_k: None,
        )
        # El bridge recarga plan desde blob SEP tras tel_sep; simular plan
        # in-memory vacío pese a blob válido (escenario que dispara el guard).
        monkeypatch.setattr(
            bridge.SemanticExecutionPlan,
            "from_dict",
            classmethod(lambda cls, _d: empty),
        )
        monkeypatch.setattr(bridge, "adapt_mission_to_steps", _track_ceg)

        decision = run_mission_smart(
            m,
            allow_legacy_fallback=False,
            use_auto_learning=False,
        )
        assert decision.blocked
        assert decision.reason == "sep_present_legacy_graph_forbidden"
        assert not ceg_calls


class TestFase0GateViolations:
    def test_ok_report_no_violations(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text('{"run_id":"golden-dry-run-1"}\n', encoding="utf-8")
        assert fase0_gate_violations(_ok_report(), audit_jsonl=audit, run_id="golden-dry-run-1") == []

    def test_fails_on_profile_drift(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text("{}\n", encoding="utf-8")
        v = fase0_gate_violations(
            _ok_report(profile_drift=["ARSS_ENABLED: got=True want=False"]),
            audit_jsonl=audit,
            run_id="golden-dry-run-1",
        )
        assert any(x.startswith("profile_drift") for x in v)

    def test_fails_on_wrong_executive_source(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text("{}\n", encoding="utf-8")
        v = fase0_gate_violations(
            _ok_report(executive_source="compiled_execution_graph"),
            audit_jsonl=audit,
            run_id="golden-dry-run-1",
        )
        assert any("executive_source" in x for x in v)

    def test_fails_on_compiled_graph_executed(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text("{}\n", encoding="utf-8")
        v = fase0_gate_violations(
            _ok_report(compiled_graph_executed=True),
            audit_jsonl=audit,
            run_id="golden-dry-run-1",
        )
        assert "compiled_graph_executed_with_sep" in v

    def test_fails_on_coords_used(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text("{}\n", encoding="utf-8")
        v = fase0_gate_violations(_ok_report(coords_used=True), audit_jsonl=audit, run_id="x")
        assert "coords_used" in v

    def test_fails_on_missing_step_strategies(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text("{}\n", encoding="utf-8")
        v = fase0_gate_violations(_ok_report(step_strategies=[]), audit_jsonl=audit, run_id="x")
        assert "missing_step_strategies" in v

    def test_fails_on_smart_route_primary(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text("{}\n", encoding="utf-8")
        assert is_forbidden_primary_strategy("open_site:smart_route")
        v = fase0_gate_violations(
            _ok_report(step_strategies=[{"step_id": "s", "strategy_used": "open_site:smart_route"}]),
            audit_jsonl=audit,
            run_id="x",
        )
        assert any("forbidden_primary" in x for x in v)

    def test_fails_on_stub_only_mode(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text("{}\n", encoding="utf-8")
        v = fase0_gate_violations(_ok_report(mode="stub"), audit_jsonl=audit, run_id="x")
        assert any("non_representative_mode" in x for x in v)

    def test_fails_on_missing_audit_jsonl(self, tmp_path):
        v = fase0_gate_violations(_ok_report(), audit_jsonl=tmp_path / "missing.jsonl", run_id="x")
        assert "missing_execution_run_audit_jsonl" in v

    def test_fails_on_audit_jsonl_missing_run_id(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text('{"run_id":"other-run"}\n', encoding="utf-8")
        v = fase0_gate_violations(
            _ok_report(run_id="golden-dry-run-1"),
            audit_jsonl=audit,
            run_id="golden-dry-run-1",
        )
        assert "audit_jsonl_missing_run_id" in v


class TestGoldenMissionRunner:
    def test_dry_run_passes_gate_checks(self, tmp_path, monkeypatch):
        audit = tmp_path / "execution_run_audit.jsonl"
        monkeypatch.setattr("app.services.runtime.execution_run_audit._AUDIT_JSONL", audit)
        monkeypatch.setattr(
            "app.services.runtime.execution_run_audit.EXECUTION_RUN_AUDIT_JSONL",
            audit,
        )

        report = run_golden_mission(mode="dry-run", export_path=tmp_path / "report.json")
        blob = report.to_dict()

        assert blob["executive_source"] == "semantic_execution_plan"
        assert blob["legacy_graph_ignored"] is True
        assert blob["profile_drift"] == []
        assert blob["mode"] == "dry-run"
        assert not any(
            "smart_route" in str(s.get("strategy_used", ""))
            for s in blob["step_strategies"]
        )

        violations = fase0_gate_violations(
            blob,
            audit_jsonl=audit,
            run_id=str(report.run_id),
        )
        assert violations == [], f"unexpected violations: {violations}"

    def test_audit_jsonl_written_on_dry_run(self, tmp_path, monkeypatch):
        audit = tmp_path / "execution_run_audit.jsonl"
        monkeypatch.setattr("app.services.runtime.execution_run_audit._AUDIT_JSONL", audit)
        report = run_golden_mission(mode="dry-run")
        assert audit.is_file()
        assert report.run_id in audit.read_text(encoding="utf-8")


class TestFase0GateClosureCriteria:
    """Criterio cierre FASE 0: runner dry-run exit 0; negativos exit 2 por condición."""

    def test_fase0_gate_runner_subprocess_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_golden_mission.py"), "--mode", "dry-run"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout

    @pytest.mark.parametrize("overrides,expected_fragment", GATE_NEGATIVE_CONDITIONS)
    def test_gate_script_exits_two_on_violation(
        self,
        tmp_path,
        monkeypatch,
        overrides,
        expected_fragment,
    ):
        audit = tmp_path / "audit.jsonl"
        audit.write_text('{"run_id":"gate-negative-run"}\n', encoding="utf-8")
        report = _good_gate_report(**overrides)
        code = _invoke_gate_script(monkeypatch, tmp_path, report, audit)
        assert code == 2, expected_fragment

    def test_gate_script_exits_two_on_missing_audit_jsonl(self, tmp_path, monkeypatch):
        report = _good_gate_report()
        code = _invoke_gate_script(
            monkeypatch,
            tmp_path,
            report,
            tmp_path / "missing.jsonl",
        )
        assert code == 2

    def test_gate_script_exits_two_on_audit_missing_run_id(self, tmp_path, monkeypatch):
        audit = tmp_path / "audit.jsonl"
        audit.write_text('{"run_id":"other-run"}\n', encoding="utf-8")
        report = _good_gate_report(run_id="gate-negative-run")
        code = _invoke_gate_script(monkeypatch, tmp_path, report, audit)
        assert code == 2

    def test_all_gate_conditions_covered_by_negative_tests(self):
        covered = {
            "profile_drift",
            "executive_source",
            "compiled_graph_executed_with_sep",
            "coords_used",
            "missing_step_strategies",
            "forbidden_primary",
            "non_representative_mode",
            "missing_execution_run_audit_jsonl",
            "audit_jsonl_missing_run_id",
        }
        assert len(GATE_NEGATIVE_CONDITIONS) == 7
        assert len(covered) == 9
