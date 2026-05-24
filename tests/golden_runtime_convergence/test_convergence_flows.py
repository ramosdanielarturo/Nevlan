"""Golden runtime convergence — suites por categoría de flujo."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core.config import Settings, invalidate_settings_cache
from app.services.missions.mission_truth_gate import gate_decision
from app.services.runtime.operational_convergence_program import (
    analyze_multi_run_convergence,
    build_operational_convergence_state,
    compute_execution_repeatability_score,
    prepare_ocrp_for_execution,
)
from tests.golden_runtime_convergence.builders import (
    FLOW_CATEGORIES,
    build_convergence_flow_mission,
    latency_profiles,
    simulate_run_signature,
    viewport_profiles,
)


@dataclass
class _OCRPSettings:
    OCRP_ENABLED: bool = True
    OCRP_STABILIZATION_ENABLED: bool = True
    OCRP_MAX_ARL_RETRIES: int = 1


@pytest.mark.parametrize("category,_label", FLOW_CATEGORIES)
def test_flow_category_builds_convergence_state(category: str, _label: str):
    m = build_convergence_flow_mission(category)
    gate_decision(m)
    conv = build_operational_convergence_state(m, _OCRPSettings())
    assert 0.0 <= conv.convergence_score <= 1.0
    assert conv.strategy_stability >= 0.0


@pytest.mark.parametrize("category,_label", FLOW_CATEGORIES)
def test_flow_category_multi_run_low_jitter(category: str, _label: str):
    m = build_convergence_flow_mission(category)
    gate_decision(m)
    for i in range(10):
        simulate_run_signature(m, run_index=i, jitter=0.05)
    score, rep = compute_execution_repeatability_score(m)
    multi = analyze_multi_run_convergence(m)
    assert multi["buckets"]["10"]["runs"] == 10
    assert score >= 0.35


@pytest.mark.parametrize("lat", latency_profiles())
def test_latency_profile_convergence(lat: dict):
    m = build_convergence_flow_mission("browser", variant=lat["name"])
    gate_decision(m)
    m.runtime_latency_report = {
        "sections": [
            {"name": "smart_executor_run", "duration_ms": 800 * lat["scale"]},
            {"name": "ocrp_preflight", "duration_ms": 120 * lat["scale"]},
        ],
    }
    for i in range(5):
        simulate_run_signature(m, run_index=i, jitter=0.1 * lat["scale"])
    conv = build_operational_convergence_state(m, _OCRPSettings())
    assert conv.timing_consistency >= 0.0


@pytest.mark.parametrize("vp", viewport_profiles())
def test_viewport_profile_stability(vp: dict):
    m = build_convergence_flow_mission("virtualized", variant=vp["name"])
    gate_decision(m)
    audit = dict(getattr(m, "semantic_shadow_audit", None) or {})
    audit["viewport"] = vp
    m.semantic_shadow_audit = audit
    pf = prepare_ocrp_for_execution(m, _OCRPSettings())
    assert pf.get("ok") is True
    assert pf.get("convergence_score") is not None


def test_unstable_flow_detected_with_high_jitter():
    m = build_convergence_flow_mission("infinite_scroll")
    gate_decision(m)
    for i in range(12):
        simulate_run_signature(m, run_index=i, jitter=0.85)
        if i % 2 == 0:
            m.uool_audit_trace = list(getattr(m, "uool_audit_trace", None) or []) + [{
                "strategy": f"s{i % 3}",
                "action": "recover",
            }]
    conv = build_operational_convergence_state(m, _OCRPSettings())
    score, _ = compute_execution_repeatability_score(m)
    assert score < 0.95
    assert conv.convergence_score < 0.95


def test_beta_runtime_profile_enables_ocrp_stack():
    invalidate_settings_cache()
    s = Settings(BETA_PRIVATE_RUNTIME_PROFILE=True)
    assert s.OCRP_ENABLED is True
    assert s.UOOL_ENABLED is True
    assert s.LONE_LIVE_BRIDGE_ENABLED is True
    invalidate_settings_cache()
