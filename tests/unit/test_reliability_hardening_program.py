"""Reliability hardening program — tests unitarios (sin Win32)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.contracts.mission import Mission, MissionStatus
from app.interfaces.desktop.magic_ux_status import (
    MagicUxPhase,
    magic_ux_execution_banner,
    sanitize_magic_ux_runner_message,
)
from app.services.missions.mission_health_score import compute_mission_health_score
from app.services.runtime.reliability_self_healing import (
    ContinuityRetryBudget,
    adaptive_wait_stabilization_ms,
    ambiguity_downgrade_recovery_allowed,
    layout_shift_tolerance_score,
    live_entity_rematch_priority,
    recovery_path_score,
    scroll_continuity_recovery_hint,
    self_healing_steps_before_repair,
    should_attempt_self_healing_before_repair,
    stale_target_invalidation_hint,
    validator_drift_tolerance_multiplier,
)
from app.services.runtime.runtime_latency_budget import RuntimeLatencyCollector, build_runtime_latency_report
from app.services.runtime.runtime_production_telemetry import (
    record_runtime_event,
    summarize_telemetry,
    telemetry_snapshot,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)
from app.services.missions.mission_truth_gate import gate_decision


def test_layout_shift_tolerance_score():
    s = layout_shift_tolerance_score(lop_confidence=0.9, layout_shift_events=2)
    assert 0.0 <= s <= 1.0


def test_language_variation_recovery_ambiguity_downgrade():
    assert ambiguity_downgrade_recovery_allowed(aggregate_ambiguity=0.4)
    assert not ambiguity_downgrade_recovery_allowed(aggregate_ambiguity=0.9)


def test_modal_popup_stale_invalidation():
    assert stale_target_invalidation_hint(last_seen_age_s=10.0, dom_revision_delta=0)
    assert not stale_target_invalidation_hint(last_seen_age_s=1.0, dom_revision_delta=0)


def test_validator_drift_multiplier():
    assert validator_drift_tolerance_multiplier(drift_hints=4) >= 1.0


def test_live_entity_rematch_priority_orders_coords_last():
    ranked = ["vision_coords", "uia_role", "use_coords"]
    assert live_entity_rematch_priority(strategies_ranked=ranked)[0] == "uia_role"


def test_continuity_preserving_retry_cap():
    b = ContinuityRetryBudget(max_retries_per_step=2)
    assert b.can_retry("s1")
    b.register_attempt("s1")
    b.register_attempt("s1")
    assert not b.can_retry("s1")


def test_self_healing_order_before_repair():
    steps = self_healing_steps_before_repair()
    assert len(steps) == 10
    assert steps[0].value == "semantic_relocation"
    assert steps[-1].value == "rerecord_block_only"


def test_self_healing_before_repair_guard_loops():
    assert not should_attempt_self_healing_before_repair(
        arl_rounds_used=2,
        arl_cap=2,
        coords_only_residual=False,
        loops_detected=4,
    )


def test_recovery_path_scoring():
    r = recovery_path_score(
        arl_recoveries=2,
        continuity_preserved=True,
        fallback_pressures=1,
        validator_failures=0,
    )
    assert r > 0.5


def test_scroll_continuity_recovery_hint():
    assert "minor" in scroll_continuity_recovery_hint(
        scroll_delta_expected=3,
        scroll_observed=6,
    )


def test_mission_health_improves_with_truth():
    m = build_golden_search_chrome_youtube_mission(name="health-a")
    gate_decision(m)
    h_good = compute_mission_health_score(m, persist=False)["mission_health_score"]
    weak = Mission(
        name="health-b",
        status=MissionStatus.EXECUTABLE,
        semantic_execution_plan={
            "source": "semantic_intent_promotion_engine",
            "intent_status": "NEEDS_REVIEW",
            "not_ready_reasons": ["EMPTY_APP_IN_OPEN_APP"],
            "coords_used": True,
            "steps": [
                {
                    "type": "open_app",
                    "human_label": "App",
                    "params": {},
                    "preferred_strategy": "use_coords",
                },
            ],
        },
    )
    gate_decision(weak)
    h_weak = compute_mission_health_score(weak, persist=False)["mission_health_score"]
    assert h_good >= h_weak


def test_runtime_latency_budget_rollups():
    c = RuntimeLatencyCollector()
    c.mark_start("lop_refresh")
    c.mark_end("lop_refresh")
    c.mark_start("validators")
    c.mark_end("validators")
    rep = build_runtime_latency_report(c, budget_ms_total=999999.0, perception_budget_ms=999999.0)
    assert rep["budget_ok"]
    assert "rollup" in rep


def test_magic_ux_sanitizes_technical_noise():
    raw = "smart_route failed GATE_PLAN_NOT_READY strategy_used=uia_text_match"
    clean = sanitize_magic_ux_runner_message(raw, expert_mode=False)
    assert "smart_route" not in clean.lower()
    assert "GATE_" not in clean


def test_magic_ux_expert_keeps_technical():
    raw = "runtime_mode SEP retry"
    assert sanitize_magic_ux_runner_message(raw, expert_mode=True) == raw


def test_magic_banner_expert_adds_health(monkeypatch):
    m = build_golden_search_chrome_youtube_mission()
    gate_decision(m)
    m.mission_health_snapshot = {"mission_health_score": 0.88}
    txt = magic_ux_execution_banner(m, expert_mode=True)
    assert "0.88" in txt or "Listo" in txt or "ejecutar" in txt.lower()


def test_telemetry_records_and_summarizes():
    m = SimpleNamespace(runtime_production_telemetry=None)
    record_runtime_event(m, kind="arl_recovery", payload={"step": 1})
    record_runtime_event(m, kind="unsafe_hint", payload={}, severity="warn")
    snap = telemetry_snapshot(m)
    summ = summarize_telemetry(snap)
    assert summ["event_count"] >= 2


def test_recovery_loop_prevention_budget():
    b = ContinuityRetryBudget(max_retries_per_step=3)
    for _ in range(3):
        assert b.can_retry("x")
        b.register_attempt("x")
    assert not b.can_retry("x")
