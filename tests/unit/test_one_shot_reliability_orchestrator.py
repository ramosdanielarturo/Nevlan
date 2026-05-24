"""One-shot reliability orchestrator — preflight, plan source, UI masking helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from app.contracts.mission import Mission, MissionStatus
from app.interfaces.desktop.mission_ui_status import format_product_execution_hint, mission_ui_detail_status_line
from app.services.missions import mission_review_summary as mission_review_summary_mod
from app.services.missions.mission_truth_gate import gate_decision
from app.services.runtime.one_shot_reliability_orchestrator import (
    OneShotPlanSource,
    OneShotPreflightOutcome,
    OneShotRuntimeMode,
    attach_live_perception,
    build_one_shot_audit,
    choose_plan_source,
    choose_recovery_policy,
    compute_one_shot_readiness,
    plan_declares_coords_primary,
    record_runtime_learning,
    run_execution_with_reliability_guards,
    select_runtime_mode,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    apply_ambiguous_profile_select_step,
    build_golden_search_chrome_youtube_mission,
)


@dataclass
class _OSSettings:
    ONE_SHOT_RELIABILITY_ENABLED: bool = True
    ONE_SHOT_PREFLIGHT_REQUIRED: bool = True
    ONE_SHOT_USE_LOP: bool = False
    ONE_SHOT_ALLOW_COB_PILOT: bool = False
    ONE_SHOT_ALLOW_TEL_SEP_PRIMARY: bool = False
    ONE_SHOT_REQUIRE_SAFE_TO_CONTINUE: bool = True
    OMPC_ENABLED: bool = False
    SLL_ENABLED: bool = False


def test_readiness_ready_canonical_golden():
    m = build_golden_search_chrome_youtube_mission()
    gate_decision(m)
    r = compute_one_shot_readiness(m, _OSSettings(), bridge_source="semantic_execution_plan", steps_count=8)
    assert r.outcome in (
        OneShotPreflightOutcome.READY_TO_EXECUTE,
        OneShotPreflightOutcome.READY_WITH_WARNINGS,
    )


def test_ambiguity_blocks_profile_carrier():
    m = build_golden_search_chrome_youtube_mission(name="oss-amb-profile")
    apply_ambiguous_profile_select_step(m, label="perfil")
    gate_decision(m)
    r = compute_one_shot_readiness(m, _OSSettings())
    assert r.outcome == OneShotPreflightOutcome.NEEDS_HUMAN_CLARIFICATION


def test_unsafe_modal_blocks():
    m = build_golden_search_chrome_youtube_mission(name="oss-unsafe")
    sep = dict(m.semantic_execution_plan or {})
    steps = list(sep.get("steps") or [])
    if not steps:
        pytest.fail("golden mission debe tener SEP steps")
    steps[0] = dict(steps[0])
    steps[0]["human_label"] = "Complete payment for subscription"
    sep["steps"] = steps
    m.semantic_execution_plan = sep
    gate_decision(m)
    r = compute_one_shot_readiness(m, _OSSettings())
    assert r.outcome == OneShotPreflightOutcome.UNSAFE_STOP


def test_lop_blocker_stops_when_require_safe():
    m = build_golden_search_chrome_youtube_mission(name="oss-lop")
    gate_decision(m)
    m.lop_last_snapshot = {
        "live_blockers": [{"kind": "modal", "severity": "high"}],
        "safe_to_continue": False,
        "confidence": 0.2,
    }
    chk = compute_one_shot_readiness(m, _OSSettings())
    assert chk.outcome == OneShotPreflightOutcome.UNSAFE_STOP


def test_plan_source_tel_when_promoted_and_allowed(monkeypatch):
    m = build_golden_search_chrome_youtube_mission(name="oss-tel")
    gate_decision(m)
    sep = dict(m.semantic_execution_plan or {})
    sep["source"] = "tel_sep_promoter_primary"
    sep["coords_used"] = False
    m.semantic_execution_plan = sep

    monkeypatch.setattr(
        "app.services.runtime.one_shot_reliability_orchestrator._tel_sep_eligible",
        lambda mission, tel_plan, settings: True,
    )
    monkeypatch.setattr(
        "app.services.runtime.one_shot_reliability_orchestrator.plan_declares_coords_primary",
        lambda sep, steps: False,
    )

    ps = choose_plan_source(m, replace(_OSSettings(), ONE_SHOT_ALLOW_TEL_SEP_PRIMARY=True))
    assert ps == OneShotPlanSource.TEL_SEP_PROMOTED


def test_plan_source_sep_when_tel_disallowed_even_if_promoted_source_tag():
    m = build_golden_search_chrome_youtube_mission(name="oss-sep-prefer")
    sep = dict(m.semantic_execution_plan or {})
    sep["source"] = "tel_sep_promoter_primary"
    m.semantic_execution_plan = sep
    gate_decision(m)
    ps = choose_plan_source(m, _OSSettings())
    assert ps in (OneShotPlanSource.SEP_SIPE_CANONICAL, OneShotPlanSource.COB_ORG_GOOR_CANDIDATE)


def test_legacy_graph_plan_source():
    m = Mission(name="legacy-ish", status=MissionStatus.EXECUTABLE)
    m.semantic_execution_plan = {}
    m.compiled_execution_graph = [object()]
    ps = choose_plan_source(m, _OSSettings())
    assert ps == OneShotPlanSource.LEGACY_GRAPH_DEBUG


def test_recovery_policy_arl_first():
    assert choose_recovery_policy()[0].value == "ARL_FIRST"


def test_normal_ui_gate_codes_are_expert_hidden_registry():
    assert "GATE_PLAN_NOT_READY" in mission_review_summary_mod._EXPERT_ONLY_BLOCKERS


def test_expert_detail_line_shows_gate_tokens():
    m = build_golden_search_chrome_youtube_mission(name="oss-expert-line")
    apply_ambiguous_profile_select_step(m, label="perfil")
    gate_decision(m)
    line = mission_ui_detail_status_line(m, expert_mode=True)
    assert "gate=" in line


def test_format_product_hint_normal_vs_expert():
    m = build_golden_search_chrome_youtube_mission()
    gate_decision(m)
    assert "Lista para ejecutar" in format_product_execution_hint(m, expert_mode=False)
    m.one_shot_reliability_report = {
        "preflight_outcome": "READY_TO_EXECUTE",
        "plan_source": "SEP_SIPE_CANONICAL",
        "runtime_mode": "SEP_SMART_RUNTIME",
        "one_shot_reliability_score": 0.91,
    }
    exp = format_product_execution_hint(m, expert_mode=True)
    assert "SEP_SIPE_CANONICAL" in exp


def test_record_runtime_learning_ompc_delegation_message():
    m = build_golden_search_chrome_youtube_mission()

    @dataclass
    class _Om(_OSSettings):
        OMPC_ENABLED: bool = True

    @dataclass
    class _MR:
        status: str = "success"
        duration_ms: int = 50

    out = record_runtime_learning(m, _Om(), steps=[], mission_result=_MR())
    assert "delegated" in str(out.get("ompc", ""))


def test_runtime_mode_legacy_when_bridge_flag():
    m = build_golden_search_chrome_youtube_mission()
    mode = select_runtime_mode(
        m,
        _OSSettings(),
        plan_source=OneShotPlanSource.SEP_SIPE_CANONICAL,
        cob_pilot_will_run=False,
        bridge_used_legacy=True,
    )
    assert mode == OneShotRuntimeMode.LEGACY_DEBUG_FALLBACK


def test_flags_off_preserve_callbacks():
    m = build_golden_search_chrome_youtube_mission()

    def a():
        return 1

    def b():
        return 2

    off = _OSSettings(ONE_SHOT_RELIABILITY_ENABLED=False)

    sa, sb = run_execution_with_reliability_guards(m, off, on_step_started=a, on_step_done=b)
    assert sa is a and sb is b


def test_no_coords_primary_plan_detection():
    sep = {"coords_used": False, "steps": [{"preferred_strategy": "uia_role", "params": {}}]}
    assert not plan_declares_coords_primary(sep, sep["steps"])
    sep_bad = {"coords_used": True, "steps": []}
    assert plan_declares_coords_primary(sep_bad, [])


def test_corrupted_plan_strict_requests_repair(monkeypatch):
    m = build_golden_search_chrome_youtube_mission(name="oss-corrupt")

    def _boom(_mission):
        class Viol:
            code = "BOOM_STRICT"

        class VR:
            is_valid = False
            violations = [Viol()]

        return VR()

    import app.services.missions.semantic_execution_plan as sep_mod

    monkeypatch.setattr(sep_mod, "validate_semantic_execution_plan_strict", _boom)
    gate_decision(m)
    r = compute_one_shot_readiness(m, _OSSettings())
    assert r.outcome == OneShotPreflightOutcome.REPAIR_REQUIRED


def test_repeated_audit_scores_track_readiness():
    m = build_golden_search_chrome_youtube_mission()
    gate_decision(m)
    r_lo = compute_one_shot_readiness(m, _OSSettings())
    hi = replace(
        r_lo,
        readiness_score=0.95,
        outcome=OneShotPreflightOutcome.READY_TO_EXECUTE,
        warnings=[],
    )
    rep_lo = build_one_shot_audit(
        m,
        _OSSettings(),
        readiness=r_lo,
        plan_source=OneShotPlanSource.SEP_SIPE_CANONICAL,
        runtime_mode=OneShotRuntimeMode.SEP_SMART_RUNTIME,
        bridge_source="semantic_execution_plan",
        cob_pilot_will_run=False,
    )
    rep_hi = build_one_shot_audit(
        m,
        _OSSettings(),
        readiness=hi,
        plan_source=OneShotPlanSource.SEP_SIPE_CANONICAL,
        runtime_mode=OneShotRuntimeMode.SEP_SMART_RUNTIME,
        bridge_source="semantic_execution_plan",
        cob_pilot_will_run=False,
    )
    assert rep_hi["one_shot_reliability_score"] >= rep_lo["one_shot_reliability_score"]


def test_attach_live_perception_skipped_when_disabled():
    m = build_golden_search_chrome_youtube_mission()

    class _NoLop(_OSSettings):
        ONE_SHOT_USE_LOP = False

    assert attach_live_perception(m, _NoLop()).get("skipped") is True


def test_canonical_mission_semantic_step_coverage_one_shot():
    """Escenario «golden» alineado con flujo Search→app→…→scroll (fixture)."""
    m = build_golden_search_chrome_youtube_mission()
    gate_decision(m)
    r = compute_one_shot_readiness(m, _OSSettings(), bridge_source="semantic_execution_plan", steps_count=7)
    assert r.outcome != OneShotPreflightOutcome.UNSAFE_STOP
    sep = m.semantic_execution_plan or {}
    kinds = [str((x or {}).get("type")) for x in (sep.get("steps") or [])]
    assert "open_app" in kinds and "scroll_results" in kinds
