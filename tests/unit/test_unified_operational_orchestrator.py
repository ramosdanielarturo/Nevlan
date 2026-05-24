"""Unified Operational Orchestration Layer (UOOL) — unit tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.contracts.mission import (
    DominantOperationalStrategy,
    Mission,
    MissionStatus,
    OperationalActionKind,
    OperationalExecutionPhase,
    OperationalRecoveryBudget,
    UnifiedOperationalExecutionContext,
)
from app.interfaces.desktop.magic_ux_status import sanitize_magic_ux_runner_message
from app.services.missions.mission_truth_gate import gate_decision
from app.services.runtime.unified_operational_orchestrator import (
    apply_budget_for_action,
    build_unified_operational_context,
    compute_unified_operational_confidence,
    decide_next_operational_action,
    evaluate_operational_continuity_uool,
    expert_audit_lines,
    merge_cross_layer_memory_signals,
    normal_status_message,
    orchestrate_step_preflight,
    persist_uool_state,
    prepare_uool_for_execution,
    resolve_dominant_operational_strategy,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    apply_ambiguous_profile_select_step,
    build_golden_search_chrome_youtube_mission,
)


@dataclass
class _UOOLSettings:
    UOOL_ENABLED: bool = True
    UOOL_COORDINATE_SMART_EXECUTOR: bool = True
    UOOL_PREFLIGHT_REQUIRED: bool = False
    UOOL_MAX_RECOVERY_ROUNDS: int = 4
    UOOL_MAX_REPAIR_ROUNDS: int = 2
    UOOL_MAX_NAVIGATION_ROUNDS: int = 8
    UOOL_REQUIRE_LIVE_PERCEPTION_ALIGN: bool = True
    UOOL_WAIT_MS: int = 50
    SLL_ENABLED: bool = False
    OMPC_ENABLED: bool = False


def _ctx(**kwargs) -> UnifiedOperationalExecutionContext:
    base = build_unified_operational_context(
        build_golden_search_chrome_youtube_mission(name="uool-ctx"),
        _UOOLSettings(),
        step_index=0,
    )
    return base.model_copy(update=kwargs)


# 1. Dominant strategy resolution
def test_dominant_strategy_entity_first_when_high_confidence():
    ctx = _ctx(
        target_entity={"entity_id": "e1", "confidence": 0.85},
        memory_signals=merge_cross_layer_memory_signals(
            Mission(name="x", status=MissionStatus.EXECUTABLE), _UOOLSettings(),
        ).model_copy(update={"entity_memory_hits": ["e1"]}),
    )
    strat = resolve_dominant_operational_strategy(ctx, Mission(name="x", status=MissionStatus.EXECUTABLE))
    assert strat == DominantOperationalStrategy.ENTITY_FIRST


# 2. Navigation vs repair arbitration
def test_navigation_vs_repair_arbitration_when_continuity_low():
    m = build_golden_search_chrome_youtube_mission(name="uool-nav-repair")
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=2)
    ctx.continuity_confidence = 0.3
    ctx.budget = OperationalRecoveryBudget(recovery_budget=2, recovery_used=2, repair_budget=2, repair_used=0)
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    assert decision.action == OperationalActionKind.REPAIR
    assert decision.dominant_strategy == DominantOperationalStrategy.CONTINUITY_RECOVERY


# 3. Continuity preserved
def test_continuity_preserved_on_golden_mission():
    m = build_golden_search_chrome_youtube_mission(name="uool-cont")
    gate_decision(m)
    score, notes, _ = evaluate_operational_continuity_uool(m, step_index=0)
    assert score >= 0.4
    assert any("step_objective_in_flow" in n for n in notes)


# 4. Entity-first dominance
def test_entity_first_dominance_decision_execute():
    m = Mission(name="ent", status=MissionStatus.EXECUTABLE)
    m.entity_runtime_resolution_audit = [{
        "entity_id": "p1", "display_name": "Perfil A", "confidence": 0.9,
    }]
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0)
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    assert decision.dominant_strategy == DominantOperationalStrategy.ENTITY_FIRST
    assert decision.action == OperationalActionKind.EXECUTE
    assert decision.allow_entity_first is True


# 5. Dynamic wait dominance
def test_dynamic_wait_dominance():
    ctx = _ctx(
        live_perception={"safe_to_continue": True, "dynamic_content_pending": True, "confidence": 0.6},
    )
    ctx.dominant_strategy = resolve_dominant_operational_strategy(
        ctx, Mission(name="w", status=MissionStatus.EXECUTABLE),
    )
    decision = decide_next_operational_action(
        ctx, Mission(name="w", status=MissionStatus.EXECUTABLE), _UOOLSettings(),
    )
    assert decision.dominant_strategy == DominantOperationalStrategy.WAIT_DYNAMIC_CONTENT
    assert decision.action == OperationalActionKind.WAIT


# 6. Recovery budget exhaustion
def test_recovery_budget_exhaustion_asks_human():
    m = Mission(name="budget", status=MissionStatus.EXECUTABLE)
    ctx = _ctx()
    ctx.budget = OperationalRecoveryBudget(
        recovery_budget=1, recovery_used=1,
        repair_budget=1, repair_used=1,
        navigation_budget=1, navigation_used=1,
    )
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    assert decision.action == OperationalActionKind.ASK_HUMAN
    assert "budget_exhausted" in decision.reason_codes


# 7. Navigation storm prevention
def test_navigation_storm_prevention():
    m = Mission(name="navstorm", status=MissionStatus.EXECUTABLE)
    ctx = _ctx(
        target_collection={"collection_id": "c1", "confidence": 0.7},
        continuity_confidence=0.5,
    )
    ctx.budget = OperationalRecoveryBudget(navigation_budget=2, navigation_used=2)
    ctx.dominant_strategy = DominantOperationalStrategy.COLLECTION_NAVIGATION
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    assert decision.action == OperationalActionKind.FALLBACK
    assert "navigation_storm_prevented" in decision.reason_codes
    assert decision.allow_navigation is False


# 8. Cross-layer memory merge
def test_cross_layer_memory_merge():
    m = Mission(name="mem", status=MissionStatus.EXECUTABLE)
    m.adaptive_runtime_audit = {"events": [{"strategy": "uia_strict"}]}
    m.operational_navigation_audit = [{"navigation_action": "scroll_down", "success": True}]
    m.entity_runtime_resolution_audit = [{"entity_id": "e1"}]
    m.collection_entity_resolution_audit = [{"collection_id": "grid1"}]
    sig = merge_cross_layer_memory_signals(m, replace(_UOOLSettings(), SLL_ENABLED=True, OMPC_ENABLED=True))
    assert sig.sll_strategy_hints
    assert sig.navigation_memory_hits
    assert sig.entity_memory_hits
    assert sig.collection_memory_hits
    assert sig.memory_agreement > 0


# 9. Unsafe stop
def test_unsafe_stop():
    m = Mission(name="unsafe", status=MissionStatus.EXECUTABLE)
    step = SimpleNamespace(human_label="Complete payment checkout", kind="click", params={})
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0, step=step)
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    assert decision.action == OperationalActionKind.STOP
    assert decision.phase == OperationalExecutionPhase.UNSAFE


# 10. Ambiguity stop
def test_ambiguity_stop():
    m = build_golden_search_chrome_youtube_mission(name="uool-amb")
    apply_ambiguous_profile_select_step(m, label="perfil")
    gate_decision(m)
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0)
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    assert decision.action == OperationalActionKind.ASK_HUMAN
    assert "ambiguity_stop" in decision.reason_codes


# 11. Live perception mismatch
def test_live_perception_mismatch_triggers_recover():
    m = Mission(name="lop", status=MissionStatus.EXECUTABLE)
    m.lop_last_snapshot = {"safe_to_continue": False, "confidence": 0.2, "live_blockers": ["modal"]}
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0)
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    assert decision.action == OperationalActionKind.RECOVER
    assert "live_perception_mismatch" in decision.reason_codes


# 12. Successful orchestration loop
def test_successful_orchestration_loop_persists_trace():
    m = build_golden_search_chrome_youtube_mission(name="uool-loop")
    gate_decision(m)
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=1)
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    ctx.budget = apply_budget_for_action(ctx.budget, decision.action)
    persist_uool_state(m, ctx, decision)
    assert m.uool_last_context is not None
    assert m.uool_orchestration_report is not None
    assert m.uool_audit_trace
    assert decision.action in (OperationalActionKind.EXECUTE, OperationalActionKind.FALLBACK)


# 13. Legacy mission compatibility
def test_legacy_mission_compatibility_minimal_mission():
    m = Mission(name="legacy-only", status=MissionStatus.EXECUTABLE)
    m.compiled_execution_graph = [object()]
    pf = prepare_uool_for_execution(m, _UOOLSettings())
    assert pf.get("ok") is True or "preflight_action" in pf


# 14. No app hardcodes — strategy resolution uses generic signals only
def test_no_app_hardcodes_in_strategy_resolution():
    m = Mission(name="generic-flow", status=MissionStatus.EXECUTABLE)
    m.entity_runtime_resolution_audit = [{"entity_id": "item", "confidence": 0.8, "display_name": "Item"}]
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0)
    strat = ctx.dominant_strategy.value
    assert "youtube" not in strat.lower()
    assert "chrome" not in strat.lower()


# 15. Stable orchestration under retries
def test_stable_orchestration_under_retries():
    m = build_golden_search_chrome_youtube_mission(name="uool-retry")
    decisions = []
    for i in range(3):
        ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0)
        d = decide_next_operational_action(ctx, m, _UOOLSettings())
        ctx.budget = apply_budget_for_action(ctx.budget, d.action)
        persist_uool_state(m, ctx, d)
        decisions.append(d.action)
    assert decisions[0] == decisions[1] == decisions[2]


# 16. SmartExecutor integration hook
def test_smart_executor_integration_hook(monkeypatch):
    m = build_golden_search_chrome_youtube_mission(name="uool-se")
    gate_decision(m)
    statuses: list = []
    step = SimpleNamespace(id="s0", kind="open_app", human_label="Abrir", params={})
    dec = orchestrate_step_preflight(
        m, _UOOLSettings(), step_index=0, step=step,
        on_status=statuses.append, expert_mode=False,
    )
    assert dec is not None
    assert dec.human_message
    assert "UOOL" not in dec.human_message
    assert "ERL" not in dec.human_message


# 17. LONE + ARL coordination via navigation memory
def test_lone_arl_coordination_navigation_signals():
    m = Mission(name="lone-arl", status=MissionStatus.EXECUTABLE)
    m.operational_navigation_audit = [
        {"navigation_action": "scroll_down", "success": False},
        {"navigation_action": "scroll_down", "success": False},
    ]
    m.adaptive_runtime_audit = {"events": [{"strategy": "arl_relocate"}]}
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0)
    ctx.continuity_confidence = 0.35
    strat = resolve_dominant_operational_strategy(ctx, m)
    assert strat in (
        DominantOperationalStrategy.SEMANTIC_RELOCATION,
        DominantOperationalStrategy.CONTINUITY_RECOVERY,
        DominantOperationalStrategy.COLLECTION_NAVIGATION,
    )


# 18. SLL rerank coherence via memory agreement
def test_sll_rerank_coherence_memory_agreement():
    m = Mission(name="sll", status=MissionStatus.EXECUTABLE)
    m.adaptive_runtime_audit = {"events": [{"strategy": "a"}, {"strategy": "b"}]}
    sig = merge_cross_layer_memory_signals(m, replace(_UOOLSettings(), SLL_ENABLED=True))
    ctx = _ctx(memory_signals=sig)
    conf = compute_unified_operational_confidence(ctx, m)
    assert conf > 0.3
    assert sig.sll_confidence > 0.4


# 19. Mission completes without repair — execute default path
def test_mission_completes_without_repair_execute_path():
    m = build_golden_search_chrome_youtube_mission(name="uool-clean")
    gate_decision(m)
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0)
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    assert decision.action == OperationalActionKind.EXECUTE
    assert decision.action not in (OperationalActionKind.REPAIR, OperationalActionKind.STOP)


# 20. Prevent contradictory actions — nav exhausted blocks navigate
def test_prevent_contradictory_actions():
    m = Mission(name="contradict", status=MissionStatus.EXECUTABLE)
    ctx = _ctx(
        target_collection={"collection_id": "c", "confidence": 0.8},
        continuity_confidence=0.4,
    )
    ctx.budget = OperationalRecoveryBudget(navigation_budget=0, navigation_used=0)
    ctx.dominant_strategy = DominantOperationalStrategy.COLLECTION_NAVIGATION
    d1 = decide_next_operational_action(ctx, m, _UOOLSettings())
    assert d1.action != OperationalActionKind.NAVIGATE or not d1.allow_navigation


def test_uool_disabled_skips_orchestration():
    m = build_golden_search_chrome_youtube_mission(name="uool-off")
    assert orchestrate_step_preflight(m, replace(_UOOLSettings(), UOOL_ENABLED=False), step_index=0, step=None) is None


def test_normal_ux_sanitizes_technical_tokens():
    raw = "UOOL entity_first ARL recovery strategy=uia_strict"
    clean = sanitize_magic_ux_runner_message(raw, expert_mode=False)
    assert "UOOL" not in clean
    assert "ARL" not in clean


def test_expert_audit_lines_populated():
    m = build_golden_search_chrome_youtube_mission(name="uool-expert")
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0)
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    persist_uool_state(m, ctx, decision)
    lines = expert_audit_lines(m)
    assert lines
    assert any("UOOL" in ln for ln in lines)


def test_normal_status_message_human_friendly():
    m = Mission(name="ux", status=MissionStatus.EXECUTABLE)
    ctx = build_unified_operational_context(m, _UOOLSettings(), step_index=0)
    decision = decide_next_operational_action(ctx, m, _UOOLSettings())
    normal = normal_status_message(decision, expert_mode=False)
    expert = normal_status_message(decision, expert_mode=True)
    assert "strategy=" not in normal
    assert "strategy=" in expert
