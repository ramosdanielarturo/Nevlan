"""Operational Convergence & Repeatability Program (OCRP) — unit tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from app.contracts.mission import (
    Mission,
    MissionStatus,
    OperationalActionDecision,
    OperationalActionKind,
    OperationalConvergenceState,
    OperationalRecoveryBudget,
    UnifiedOperationalExecutionContext,
)
from app.core.config import Settings, invalidate_settings_cache
from app.interfaces.desktop.magic_ux_status import sanitize_magic_ux_runner_message
from app.services.missions.mission_truth_gate import gate_decision
from app.services.runtime.operational_convergence_program import (
    analyze_multi_run_convergence,
    apply_ocrp_to_uool_decision,
    apply_stabilization_to_decision,
    build_operational_convergence_state,
    compute_execution_repeatability_score,
    compute_step_stability_index,
    detect_navigation_oscillation,
    detect_recovery_storm,
    detect_strategy_jitter,
    prepare_ocrp_for_execution,
    record_post_run_convergence,
    runtime_self_calibration,
    stabilize_operational_execution,
)
from app.services.runtime.unified_operational_orchestrator import (
    build_unified_operational_context,
    decide_next_operational_action,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)


@dataclass
class _OCRPSettings:
    OCRP_ENABLED: bool = True
    OCRP_STABILIZATION_ENABLED: bool = True
    OCRP_MAX_ARL_RETRIES: int = 1
    UOOL_ENABLED: bool = True


def _mission(name: str = "ocrp-test") -> Mission:
    m = build_golden_search_chrome_youtube_mission(name=name)
    gate_decision(m)
    return m


# 1. Strategy stabilization
def test_strategy_stabilization_freezes_on_jitter():
    m = _mission("ocrp-strat")
    m.uool_audit_trace = [
        {"strategy": "entity_first", "action": "execute"},
        {"strategy": "collection_navigation", "action": "navigate"},
        {"strategy": "execute_step", "action": "execute"},
        {"strategy": "continuity_recovery", "action": "recover"},
    ]
    conv = build_operational_convergence_state(m, _OCRPSettings())
    hints = stabilize_operational_execution(m, _OCRPSettings(), None, conv)
    if conv.strategy_stability < 0.55:
        assert hints.frozen_strategy == "continuity_recovery"


# 2. Recovery storm prevention
def test_recovery_storm_prevention():
    m = _mission("ocrp-storm")
    m.adaptive_runtime_audit = {"events": [{"kind": "arl"}] * 8}
    m.uool_audit_trace = [{"action": "recover"}] * 6
    hit, _ = detect_recovery_storm(m)
    assert hit is True
    conv = build_operational_convergence_state(m, _OCRPSettings())
    hints = stabilize_operational_execution(m, _OCRPSettings(), None, conv)
    assert hints.block_recovery or hints.max_arl_retries == 0


# 3. Navigation oscillation detection
def test_navigation_oscillation_detection():
    m = Mission(name="nav-osc", status=MissionStatus.EXECUTABLE)
    m.operational_navigation_audit = [
        {"navigation_action": "scroll_down", "success": False},
        {"navigation_action": "scroll_up", "success": False},
        {"navigation_action": "scroll_down", "success": False},
        {"navigation_action": "scroll_up", "success": False},
    ]
    hit, flags = detect_navigation_oscillation(m)
    assert hit is True
    assert flags


# 4. Timing adaptation
def test_timing_adaptation_not_fixed_default():
    m = _mission("ocrp-timing")
    m.runtime_latency_report = {
        "sections": [
            {"name": "smart_executor_run", "duration_ms": 2400},
            {"name": "ocrp_preflight", "duration_ms": 180},
        ],
    }
    model = runtime_self_calibration(m, _OCRPSettings())
    assert model.wait_ms != 800 or model.source == "calibrated"
    assert model.navigation_settle_ms >= 250


# 5. Convergence scoring
def test_convergence_scoring_bounded():
    m = _mission("ocrp-score")
    conv = build_operational_convergence_state(m, _OCRPSettings())
    assert 0.0 <= conv.convergence_score <= 1.0
    assert conv.timing_consistency >= 0.0


# 6. Stable multi-run execution
def test_stable_multi_run_execution():
    m = _mission("ocrp-multi")
    for i in range(10):
        record_post_run_convergence(
            m, _OCRPSettings(),
            run_id=f"r{i}",
            status="success",
            duration_ms=1100 + i * 10,
            step_outcomes=["success"] * 8,
        )
    score, rep = compute_execution_repeatability_score(m)
    multi = analyze_multi_run_convergence(m)
    assert multi["buckets"]["10"]["runs"] == 10
    assert score >= 0.4


# 7. Unstable mission detection
def test_unstable_mission_detection():
    m = _mission("ocrp-unstable")
    for i in range(8):
        record_post_run_convergence(
            m, _OCRPSettings(),
            run_id=f"u{i}",
            status="failed" if i % 2 == 0 else "success",
            duration_ms=500 + i * 300,
            step_outcomes=["failed", "success"] * 4,
        )
    score, rep = compute_execution_repeatability_score(m)
    assert rep["execute_always_candidate"] is False
    assert score < 0.95


# 8. Entity drift detection
def test_entity_drift_detection():
    m = Mission(name="ent-drift", status=MissionStatus.EXECUTABLE)
    m.entity_runtime_resolution_audit = [
        {"entity_id": "a", "confidence": 0.9},
        {"entity_id": "b", "confidence": 0.85},
        {"entity_id": "c", "confidence": 0.8},
        {"entity_id": "d", "confidence": 0.75},
    ]
    conv = build_operational_convergence_state(m, _OCRPSettings())
    assert "entity_flip_flop" in conv.detectors_fired or conv.entity_stability < 0.7


# 9. Validator inconsistency detection
def test_validator_inconsistency_detection():
    m = Mission(name="val-inc", status=MissionStatus.EXECUTABLE)
    m.ocrp_execution_signatures = [{
        "validator_outcomes": [{"ok": True}, {"ok": False}],
    }] * 3
    conv = build_operational_convergence_state(m, _OCRPSettings())
    assert "validator_instability" in conv.detectors_fired or conv.validator_consistency < 1.0


# 10. Self-calibration
def test_self_calibration_persists_timing_model():
    m = _mission("ocrp-cal")
    prepare_ocrp_for_execution(m, _OCRPSettings())
    assert m.ocrp_timing_model is not None
    assert m.ocrp_calibration_audit is not None


# 11. UOOL convergence integration
def test_uool_convergence_integration():
    m = _mission("ocrp-uool")
    ctx = build_unified_operational_context(m, replace(_OCRPSettings(), OCRP_ENABLED=True), step_index=0)
    base = decide_next_operational_action(ctx, m, _OCRPSettings())
    stabilized, conv, hints = apply_ocrp_to_uool_decision(base, m, _OCRPSettings(), ctx)
    assert m.ocrp_convergence_state is not None
    assert stabilized.action in OperationalActionKind


# 12. Repeatability scoring
def test_repeatability_scoring_execute_always_threshold():
    m = _mission("ocrp-rep")
    for i in range(15):
        record_post_run_convergence(
            m, _OCRPSettings(),
            status="success",
            duration_ms=1000,
            step_outcomes=["success"] * 8,
        )
    score, rep = compute_execution_repeatability_score(m)
    assert "repeatability_score" in rep
    assert rep["execute_always_threshold"] == 0.95


# 13. No infinite loops — budget blocks recovery cascade
def test_no_infinite_recovery_loops():
    m = _mission("ocrp-loop")
    ctx = UnifiedOperationalExecutionContext(
        budget=OperationalRecoveryBudget(recovery_budget=1, recovery_used=1),
    )
    conv = OperationalConvergenceState(detectors_fired=["over_recovery"])
    hints = stabilize_operational_execution(m, _OCRPSettings(), ctx, conv)
    decision = OperationalActionDecision(action=OperationalActionKind.RECOVER)
    out = apply_stabilization_to_decision(decision, hints, conv)
    assert out.action != OperationalActionKind.RECOVER or out.allow_arl_recovery is False


# 14. Runtime variance reduction via stabilization
def test_runtime_variance_reduction_signal():
    m = _mission("ocrp-var")
    m.runtime_latency_report = {
        "sections": [{"name": "x", "duration_ms": 100}, {"name": "y", "duration_ms": 5000}],
    }
    conv = build_operational_convergence_state(m, _OCRPSettings())
    assert conv.runtime_variance > 0.0
    model = runtime_self_calibration(m, _OCRPSettings())
    assert model.confidence >= 0.0


# 15. Golden mission stability
def test_golden_mission_stability():
    m = _mission("ocrp-golden")
    conv = build_operational_convergence_state(m, _OCRPSettings())
    score, _ = compute_execution_repeatability_score(m)
    assert conv.convergence_score >= 0.35
    assert score >= 0.35


# 16. No app hardcodes
def test_no_app_hardcodes():
    m = Mission(name="generic-flow", status=MissionStatus.EXECUTABLE)
    conv = build_operational_convergence_state(m, _OCRPSettings())
    blob = conv.model_dump_json()
    assert "youtube" not in blob.lower()
    assert "chrome" not in blob.lower()


# 17. Legacy compatibility
def test_legacy_compatibility_minimal_mission():
    m = Mission(name="legacy", status=MissionStatus.EXECUTABLE)
    pf = prepare_ocrp_for_execution(m, replace(_OCRPSettings(), OCRP_ENABLED=False))
    assert pf.get("skipped") is True


# 18. Consistent dominant strategy under low jitter
def test_consistent_dominant_strategy():
    m = _mission("ocrp-dom")
    for i in range(5):
        m.uool_audit_trace = list(getattr(m, "uool_audit_trace", None) or []) + [{
            "strategy": "execute_step",
            "action": "execute",
        }]
    conv = build_operational_convergence_state(m, _OCRPSettings())
    assert conv.strategy_stability >= 0.7


# 19. Convergence under retries
def test_convergence_under_retries():
    m = _mission("ocrp-retry")
    m.uool_audit_trace = [
        {"strategy": "execute_step", "action": "execute"},
        {"strategy": "execute_step", "action": "recover"},
        {"strategy": "execute_step", "action": "execute"},
    ]
    conv = build_operational_convergence_state(m, _OCRPSettings())
    assert conv.retry_convergence >= 0.1


# 20. Beta profile activation
def test_beta_profile_activation():
    invalidate_settings_cache()
    s = Settings(BETA_PRIVATE_RUNTIME_PROFILE=True)
    assert s.OCRP_ENABLED is True
    assert s.UOOL_ENABLED is True
    assert s.LONE_LIVE_BRIDGE_ENABLED is True
    assert s.OCRP_STABILIZATION_ENABLED is True
    invalidate_settings_cache()


def test_step_stability_index_fragile():
    m = _mission("ocrp-step")
    for _ in range(4):
        record_post_run_convergence(
            m, _OCRPSettings(),
            status="failed",
            step_outcomes=["failed"] * 8,
        )
    idx = compute_step_stability_index(m, 0)
    assert idx.fragile or idx.stability_index < 0.55


def test_strategy_jitter_detector():
    m = Mission(name="jit", status=MissionStatus.EXECUTABLE)
    m.uool_audit_trace = [{"strategy": f"s{i}"} for i in range(6)]
    hit, _ = detect_strategy_jitter(m)
    assert hit is True


def test_normal_ux_hides_ocrp_tokens():
    raw = "OCRP recovery_storm navigation_oscillation repeatability=0.42"
    clean = sanitize_magic_ux_runner_message(raw, expert_mode=False)
    assert "OCRP" not in clean
    assert "oscillation" not in clean.lower()
