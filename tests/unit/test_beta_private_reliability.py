"""Beta privada Nevlan — perfil, informes y wiring mínimos (sin Qt)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.config import Settings
from app.interfaces.desktop.magic_ux_status import sanitize_magic_ux_runner_message
from app.interfaces.desktop.mission_ui_status import mission_ui_detail_status_line
from app.services.missions.mission_truth_gate import gate_decision
from app.services.runtime.beta_private_acceptance_score import compute_beta_private_acceptance_score
from app.services.runtime.beta_private_report_export import write_beta_private_bundle
from app.services.runtime.full_rerecord_guard import (
    mission_corruption_severe_for_full_rerecord,
    should_block_full_rerecord,
)
from app.services.runtime.runtime_latency_budget import (
    BETA_LATENCY_BUDGETS_MS,
    LatencySection,
    RuntimeLatencyCollector,
    build_runtime_latency_report,
    evaluate_beta_latency_budgets,
)
from app.services.runtime.runtime_production_telemetry import record_runtime_event, telemetry_snapshot
from app.services.runtime.self_healing_wiring import ordered_pre_repair_phase_names
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)


def test_beta_private_profile_sets_expected_flags():
    s = Settings(BETA_PRIVATE_RELIABILITY_PROFILE=True)
    assert s.ONE_SHOT_RELIABILITY_ENABLED is True
    assert s.ONE_SHOT_PREFLIGHT_REQUIRED is True
    assert s.RELIABILITY_HARDENING_ENABLED is True
    assert s.RUNTIME_LATENCY_REPORT_ENABLED is True
    assert s.PRODUCTION_TELEMETRY_ENABLED is True
    assert s.MAGIC_UX_SANITIZE_RUNNER_STATUS is True
    assert s.COB_EXECUTION_PILOT_ENABLED is False
    assert s.TEL_SEP_PROMOTION_MODE == "suggest"
    assert s.TEL_SEP_PROMOTION_ENABLED is True
    assert s.ARL_SEP_RECOVERY_MAX_ROUNDS == 1


def test_beta_private_profile_off_defaults_when_false():
    s = Settings(BETA_PRIVATE_RELIABILITY_PROFILE=False)
    assert s.RUNTIME_LATENCY_REPORT_ENABLED is False


def test_self_healing_order_documented_lengths():
    names = ordered_pre_repair_phase_names()
    assert len(names) == 10
    assert names[0] == "semantic_relocation"
    assert names[-1] == "rerecord_block_only"


def test_smoke_report_bundle_sections(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.runtime.beta_private_report_export.PROJECT_ROOT",
        tmp_path,
        raising=False,
    )
    blob = {
        "one_shot_reliability_report": {"stub": True},
        "runtime_latency_report": {"budget_ok": True},
        "mission_health_snapshot": {"mission_health_score": 0.8},
        "telemetry": {"event_count": 0},
        "beta_private_acceptance_snapshot": compute_beta_private_acceptance_score(
            SimpleNamespace(
                mission_health_snapshot={"mission_health_score": 0.9, "is_executable": True},
                one_shot_reliability_report={
                    "one_shot_reliability_score": 0.91,
                    "preflight_outcome": "READY_TO_EXECUTE",
                    "runtime_mode": "SEP_SHADOW",
                },
                runtime_latency_report={"budget_ok": True},
                runtime_production_telemetry=None,
            ),
            mission_result=SimpleNamespace(status="success"),
        ),
    }
    write_beta_private_bundle(summary_json=blob, recommended_fixes=["ok"])
    base = tmp_path / "var" / "reports" / "beta_private"
    ts_dirs = list(base.iterdir())
    assert ts_dirs and (ts_dirs[0] / "summary.json").exists()
    md = (ts_dirs[0] / "summary.md").read_text(encoding="utf-8").lower()
    assert "latency" in md


def test_normal_magic_ux_strip_leak():
    noisy = (
        "smart_route GATE_PLAN_NOT_READY runtime_mode=LIVE execution_truth=yes "
        "LOP GOOR arl_round SLL OMPC strategy_used=uia_text_match "
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    clean = sanitize_magic_ux_runner_message(noisy, expert_mode=False).lower()
    assert "smart_route" not in clean
    assert "gate_" not in clean
    assert "execution_truth" not in clean


def test_expert_preserves_noise():
    raw = "LOP_GATE execution_truth=SLL OMPC GOOR abc"
    assert sanitize_magic_ux_runner_message(raw, expert_mode=True) == raw


def test_self_healing_telemetry_ordered_before_recorded_repair(monkeypatch):
    from app.services.runtime.self_healing_wiring import (
        record_ordered_pre_repair_phases_before_human,
    )

    m = SimpleNamespace(runtime_production_telemetry=None)
    patched = MagicMock()
    patched.RELIABILITY_HARDENING_ENABLED = True
    monkeypatch.setattr(
        "app.services.runtime.self_healing_wiring.get_settings",
        lambda: patched,
    )

    record_ordered_pre_repair_phases_before_human(m, step_id="s1")
    record_runtime_event(m, kind="repair_suggestion_offer", payload={})
    kinds = [e.get("kind") for e in telemetry_snapshot(m).get("events") or []]

    heals = [i for i, k in enumerate(kinds) if k == "self_healing_phase"]
    rep = kinds.index("repair_suggestion_offer")
    assert heals
    assert min(heals) < rep


def test_full_rerecord_guard_blocked_unless_corruption():
    m = build_golden_search_chrome_youtube_mission()
    corrupted, _why = mission_corruption_severe_for_full_rerecord(m)
    assert corrupted is False
    block_full, _msg = should_block_full_rerecord(m, reliability_hardening=True)
    assert block_full is True


def test_latency_budget_preflight_warning():
    by_name = {"one_shot_preflight": BETA_LATENCY_BUDGETS_MS["preflight"] + 900.0}
    ex = evaluate_beta_latency_budgets(by_name=by_name, semantic_step_count=6)
    assert "budget_exceeded:preflight" in ex


def test_build_latency_report_budget_exceeded_list():
    c = RuntimeLatencyCollector()
    c.sections.append(
        LatencySection(
            name="one_shot_preflight",
            elapsed_ms=BETA_LATENCY_BUDGETS_MS["preflight"] + 800.0,
            meta={},
        )
    )
    rep = build_runtime_latency_report(c, semantic_step_count=2)
    assert isinstance(rep.get("budget_exceeded"), list)
    assert len(rep["budget_exceeded"]) >= 1


def test_beta_acceptance_states():
    m_ok = SimpleNamespace(
        mission_health_snapshot={"mission_health_score": 0.93, "is_executable": True},
        one_shot_reliability_report={
            "one_shot_reliability_score": 0.95,
            "preflight_outcome": "READY_TO_EXECUTE",
            "runtime_mode": "SEP_SHADOW",
        },
        runtime_latency_report={"budget_ok": True},
        runtime_production_telemetry=None,
    )
    ok = compute_beta_private_acceptance_score(m_ok, mission_result=SimpleNamespace(status="success"))
    assert ok["state"] in ("READY_FOR_PRIVATE_BETA", "NEEDS_HARDENING")

    m_bad = SimpleNamespace(
        mission_health_snapshot={"mission_health_score": 0.05, "is_executable": False},
        one_shot_reliability_report={"preflight_outcome": "UNSAFE_STOP"},
        runtime_latency_report={"budget_ok": False},
        runtime_production_telemetry=None,
    )
    assert compute_beta_private_acceptance_score(m_bad)["state"] == "UNSAFE_FOR_BETA"


def test_expert_mission_review_keeps_auditable_tokens():
    m = build_golden_search_chrome_youtube_mission()
    gate_decision(m)
    line_exp = mission_ui_detail_status_line(m, expert_mode=True)
    assert "Lista para ejecutar" in line_exp or "gate=[" in line_exp


def test_flags_off_preserved_defaults():
    s = Settings(
        ONE_SHOT_RELIABILITY_ENABLED=False,
        RUNTIME_LATENCY_REPORT_ENABLED=False,
        BETA_PRIVATE_RELIABILITY_PROFILE=False,
    )
    assert s.RUNTIME_LATENCY_REPORT_ENABLED is False
