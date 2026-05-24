"""UI / status alignment para Trusted Pre-Action Identity (PRD 2026-05-17).

Sin Qt: ``mission_review_summary``, ``truth_gate``, ``semantic_review_display``.
"""
from __future__ import annotations

from app.contracts.mission import EventType, Mission, RawEvent
from app.services.missions.mission_review_summary import (
    BADGE_ZERO_CAPTURE,
    build_mission_review_summary,
    compute_step_badges,
    filter_badges_for_normal_semantic_view,
)
from app.services.missions.mission_truth_gate import (
    TruthGateCode,
    gate_decision,
    is_executable,
)
from app.services.missions.semantic_execution_plan import (
    CollapseStatus,
    ReadyBlockerCode,
    SemanticExecutionPlan,
    SemanticPlanStep,
)
from app.services.missions.semantic_review_display import (
    normal_mode_review_caption_for_compiled,
    trusted_pre_action_suppresses_legacy_semantic_noise,
)


def _trusted_click_event() -> RawEvent:
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


def _base_mission(plan: SemanticExecutionPlan) -> Mission:
    m = Mission(name="trusted-pre-action-ui")
    m.legacy_compiled_graph_purpose = "legacy_debug_only"
    m.semantic_execution_plan = plan.to_dict()
    m.raw_trace = [_trusted_click_event()]
    return m


class TestTrustedPreActionReliefAndSummary:
    def test_relief_clears_spurious_needs_label_open_new_tab(self):
        plan = SemanticExecutionPlan(
            steps=[
                SemanticPlanStep(
                    id="t1",
                    type="open_new_tab",
                    params={"app": "chrome"},
                    preferred_strategy="open_new_tab:hotkey_ctrl_t",
                    human_label="Nueva pestaña",
                    needs_user_label=True,
                    label_prompt="legacy_noise",
                    confidence=0.22,
                ),
            ],
            source="semantic_intent_promotion_engine",
            coords_used=False,
            legacy_graph_ignored=True,
        )
        m = _base_mission(plan)
        blob = dict(m.semantic_execution_plan or {})
        steps = list(blob.get("steps") or [])
        row = dict(steps[0])
        row["execution_metrics"] = {"zero_capture": True}
        steps[0] = row
        blob["steps"] = steps
        m.semantic_execution_plan = blob

        summary = build_mission_review_summary(m)
        assert summary.steps[0].needs_review is False
        assert not summary.visible_blockers
        assert CollapseStatus.READY == (
            m.semantic_execution_plan or {}
        ).get("intent_status")
        badges = summary.steps[0].badges
        joined = " ".join(badges)
        assert BADGE_ZERO_CAPTURE not in badges
        assert "Zero-capture" not in joined

    def test_ambiguous_profile_stays_blocked_despite_trusted_click(self):
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
                    human_label="Elegir perfil",
                    needs_user_label=True,
                ),
            ],
            source="semantic_intent_promotion_engine",
            coords_used=False,
            legacy_graph_ignored=True,
        )
        m = _base_mission(plan)
        summary = build_mission_review_summary(m)
        assert summary.visible_blockers
        assert ReadyBlockerCode.EMPTY_PROFILE_NAME_IN_SELECT_PROFILE in (
            m.semantic_execution_plan or {}
        ).get("not_ready_reasons", [])

    def test_suspect_query_stays_blocked(self):
        plan = SemanticExecutionPlan(
            steps=[
                SemanticPlanStep(
                    id="y1",
                    type="search_youtube",
                    params={
                        "query": "foo",
                        "site": "youtube",
                        "query_fidelity_status": "suspect_garbled",
                    },
                    preferred_strategy="search_youtube:dom_input",
                    human_label="Buscar",
                ),
            ],
            source="semantic_intent_promotion_engine",
            coords_used=False,
            legacy_graph_ignored=True,
        )
        m = _base_mission(plan)
        summary = build_mission_review_summary(m)
        reasons = (m.semantic_execution_plan or {}).get("not_ready_reasons") or []
        assert ReadyBlockerCode.QUERY_FIDELITY_SUSPECT in reasons
        assert summary.visible_blockers


class TestTruthGateExecutable:
    def test_no_gate_plan_not_ready_when_executable(self):
        plan = SemanticExecutionPlan(
            steps=[
                SemanticPlanStep(
                    id="t1",
                    type="open_new_tab",
                    params={"app": "chrome"},
                    preferred_strategy="open_new_tab:hotkey_ctrl_t",
                    needs_user_label=True,
                    confidence=0.4,
                ),
            ],
            source="semantic_intent_promotion_engine",
            coords_used=False,
            legacy_graph_ignored=True,
        )
        m = _base_mission(plan)
        d = gate_decision(m)
        assert d.is_executable
        assert TruthGateCode.PLAN_NOT_READY not in d.blockers


class TestSemanticReviewDisplayHelpers:
    def test_trusted_noise_helper_follows_mission_flag(self):
        plan = SemanticExecutionPlan(
            steps=[
                SemanticPlanStep(
                    id="t1",
                    type="open_new_tab",
                    params={"app": "chrome"},
                    preferred_strategy="open_new_tab:hotkey_ctrl_t",
                ),
            ],
            source="semantic_intent_promotion_engine",
            coords_used=False,
            legacy_graph_ignored=True,
        )
        m_trust = _base_mission(plan)
        assert trusted_pre_action_suppresses_legacy_semantic_noise(m_trust)

        m_plain = Mission(name="plain")
        m_plain.legacy_compiled_graph_purpose = "legacy_debug_only"
        m_plain.semantic_execution_plan = plan.to_dict()
        m_plain.raw_trace = []
        assert not trusted_pre_action_suppresses_legacy_semantic_noise(m_plain)

    def test_expert_caption_keeps_debug_after_state_token(self):
        cap = normal_mode_review_caption_for_compiled(
            Mission(name="x"),
            "compiled-id",
            "click debug_after_state diagnostics",
            expert_mode=True,
        )
        assert "debug_after_state" in cap


class TestAutomationCenterStatusContract:
    def test_is_executable_for_centro_header(self):
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
        m = _base_mission(plan)
        assert is_executable(m)


class TestFilterBadgesTrusted:
    def test_trusted_mission_strips_zero_capture_constants(self):
        raw = [BADGE_ZERO_CAPTURE, "🧠 Semantic"]
        m = Mission(name="b")
        m.raw_trace = [_trusted_click_event()]
        out = filter_badges_for_normal_semantic_view(raw, mission=m)
        assert BADGE_ZERO_CAPTURE not in out
        assert any("Semantic" in x for x in out)
