"""Consistencia UI desktop: estado ejecutable centralizado (mission_ui_status)."""
from __future__ import annotations

from app.contracts.mission import EventType, Mission, MissionStatus, RawEvent
from app.interfaces.desktop.mission_ui_status import (
    compute_mission_ui_execution_state,
    mission_ui_detail_status_line,
    mission_ui_list_icon,
    mission_ui_list_tooltip_suffix,
    mission_ui_should_offer_execute_and_test,
    mission_ui_shows_in_attention_filter,
)
from app.services.missions.mission_truth_gate import TruthGateCode, ui_blockers
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
)


def _trusted_raw() -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        metadata={
            "capture_contract": {
                "sufficient_for_execution": True,
                "trusted_pre_action_identity": True,
                "capture_complete": True,
                "contamination": False,
            },
        },
    )


def _executable_open_tab_mission() -> Mission:
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="t1",
                type="open_new_tab",
                params={"app": "chrome"},
                preferred_strategy="open_new_tab:hotkey_ctrl_t",
                needs_user_label=True,
                confidence=0.2,
            ),
        ],
        source="semantic_intent_promotion_engine",
        coords_used=False,
        legacy_graph_ignored=True,
    )
    m = Mission(name="ui-consistency")
    m.legacy_compiled_graph_purpose = "legacy_debug_only"
    m.semantic_execution_plan = plan.to_dict()
    m.raw_trace = [_trusted_raw()]
    # Persistencia vieja: no debe engañar a la UI.
    m.status = MissionStatus.NEEDS_REVIEW
    return m


def _blocked_profile_mission() -> Mission:
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="p1",
                type="select_profile",
                params={
                    "profile_name": "perfil del navegador",
                    "app": "chrome",
                },
                preferred_strategy="select_profile:uia_text_match",
                needs_user_label=True,
            ),
        ],
        source="semantic_intent_promotion_engine",
        coords_used=False,
        legacy_graph_ignored=True,
    )
    m = Mission(name="blocked-prof")
    m.legacy_compiled_graph_purpose = "legacy_debug_only"
    m.semantic_execution_plan = plan.to_dict()
    m.raw_trace = []
    m.status = MissionStatus.EXECUTABLE
    return m


class TestCentralizedUiState:
    def test_recording_mission_shows_workflow_not_sep(self):
        m = _executable_open_tab_mission()
        m.status = MissionStatus.RECORDING
        assert "Grabando" in mission_ui_detail_status_line(m)
        assert mission_ui_should_offer_execute_and_test(m) is False

    def test_trusted_executable_ignores_stale_needs_review_status(self):
        m = _executable_open_tab_mission()
        st = compute_mission_ui_execution_state(m)
        assert st.is_executable
        assert not st.visible_blockers
        assert TruthGateCode.PLAN_NOT_READY not in st.gate_blockers
        line = mission_ui_detail_status_line(m)
        assert "Lista para ejecutar" in line
        assert "Requiere revisión" not in line
        assert mission_ui_list_icon(m) == "✅"
        assert "ejecutable" in mission_ui_list_tooltip_suffix(m)
        assert mission_ui_should_offer_execute_and_test(m) is True
        assert mission_ui_shows_in_attention_filter(m) is False

    def test_real_blockers_need_attention_even_if_status_executable(self):
        m = _blocked_profile_mission()
        st = compute_mission_ui_execution_state(m)
        assert not st.is_executable
        assert st.visible_blockers
        assert mission_ui_should_offer_execute_and_test(m) is False
        assert mission_ui_shows_in_attention_filter(m) is True
        assert mission_ui_list_icon(m) == "📝"


class TestBannerAndGateCodes:
    def test_ui_blockers_empty_when_executable_despite_stale_status(self):
        m = _executable_open_tab_mission()
        assert ui_blockers(m) == []
