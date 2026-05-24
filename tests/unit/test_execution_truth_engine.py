"""EXECUTION_TRUTH engine — casos Windows Search→Chrome y negativos."""
from __future__ import annotations

from app.contracts.mission import EventType, Mission, RawEvent
from app.services.missions.execution_truth_engine import (
    attach_execution_truth_audit,
    compute_execution_truth,
    is_execution_truth_confirmed,
)
from app.services.missions.mission_truth_gate import gate_decision
from app.services.missions.mission_review_summary import build_mission_review_summary
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_intent_status_to_plan,
)


def _mission_open_tab_clean_raw(cc_extra=None, iso=None):
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="t1",
                type="open_new_tab",
                params={"app": "chrome"},
                preferred_strategy="open_new_tab:hotkey_ctrl_t",
                needs_user_label=True,
            ),
        ],
        source="semantic_intent_promotion_engine",
        coords_used=False,
        legacy_graph_ignored=True,
    )
    m = Mission(name="et-youtube-harness")
    m.legacy_compiled_graph_purpose = "legacy_debug_only"
    m.semantic_execution_plan = plan.to_dict()
    cc = {
        "trusted_pre_action_identity": True,
        "sufficient_for_execution": True,
        "capture_complete": True,
        "contamination": False,
    }
    if cc_extra:
        cc.update(cc_extra)
    meta = {"capture_contract": cc}
    if iso:
        meta["target_identity_isolation"] = iso
    m.raw_trace = [RawEvent(event_type=EventType.MOUSE_CLICK, metadata=meta)]
    return m


class TestWindowsSearchChrome:
    def test_click_fallback_plus_transition_preserves_execution_truth(self):
        iso = {
            "trusted_pre_action_identity": True,
            "identity_preserved_after_transition": True,
            "successful_state_transition": True,
        }
        m = _mission_open_tab_clean_raw(
            cc_extra={
                "target_source": "click_fallback",
                "quality_level": "low",
            },
            iso=iso,
        )
        et = compute_execution_truth(m)
        assert et.confirmed
        assert any(
            "identity_preserved" in r for r in et.reasons
        )
        assert "click_fallback_historical" in et.suppressed_legacy_flags

    def test_pipeline_executable_gate_empty_ui_blockers(self):
        m = _mission_open_tab_clean_raw(
            cc_extra={"target_source": "uia_control"},
        )
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan or {})
        attach_intent_status_to_plan(plan, mission=m)
        m.semantic_execution_plan = plan.to_dict()
        attach_execution_truth_audit(m)
        d = gate_decision(m)
        assert d.is_executable
        summary = build_mission_review_summary(m, recompute=False)
        assert not summary.visible_blockers
        audit = getattr(m, "_execution_truth_audit", {})
        assert audit.get("execution_truth", {}).get("confirmed") is True


class TestNegatives:
    def test_contamination_blocks_truth(self):
        m = _mission_open_tab_clean_raw(
            cc_extra={"contamination": True},
        )
        assert not is_execution_truth_confirmed(m)

    def test_trusted_but_capture_explicitly_incomplete_without_transition(self):
        m = _mission_open_tab_clean_raw(
            cc_extra={
                "capture_complete": False,
                "target_source": "click_fallback",
            },
        )
        assert not compute_execution_truth(m).confirmed
