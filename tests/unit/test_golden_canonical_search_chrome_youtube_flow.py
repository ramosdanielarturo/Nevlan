"""Contrato golden: flujo canónico Search→Chrome→…→YouTube→scroll + EXECUTION_TRUTH."""
from __future__ import annotations

import pytest

from app.interfaces.desktop.mission_ui_status import (
    compute_mission_ui_execution_state,
    mission_ui_detail_status_line,
)
from app.services.missions.execution_truth_engine import compute_execution_truth
from app.services.missions.mission_review_summary import (
    build_mission_review_summary,
    render_normal_view,
)
from app.services.missions.mission_truth_gate import TruthGateCode, gate_decision
from app.services.missions.semantic_execution_plan import (
    PROFILE_OR_QUERY_REVIEW_FOCUS_BLOCKERS,
    ReadyBlockerCode,
)
from app.services.missions.semantic_review_display import (
    NORMAL_VIEW_SIPE_FORBIDDEN_SUBSTRINGS,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    apply_ambiguous_profile_select_step,
    build_golden_search_chrome_youtube_mission,
)


@pytest.fixture
def golden_canonical_youtube_flow():
    return build_golden_search_chrome_youtube_mission()


class TestGoldenCanonicalYoutubeFlow:

    def test_execution_truth_confirmed_on_canonical_trace(self, golden_canonical_youtube_flow):
        m = golden_canonical_youtube_flow
        gate_decision(m)
        et = compute_execution_truth(m)
        assert et.confirmed, f"execution_truth falló: {et.reasons}"
        audit = getattr(m, "_execution_truth_audit", None) or {}
        nested = audit.get("execution_truth") or {}
        assert nested.get("confirmed") is True

    def test_gate_executable_without_artificial_plan_not_ready(
        self, golden_canonical_youtube_flow,
    ):
        m = golden_canonical_youtube_flow
        dec = gate_decision(m)
        assert dec.is_executable, (
            f"esperaba ejecutable; blockers={dec.blockers!r} "
            f"intent={dec.intent_status!r}"
        )
        assert TruthGateCode.PLAN_NOT_READY not in dec.blockers

    def test_visible_blockers_clean_when_ready(self, golden_canonical_youtube_flow):
        m = golden_canonical_youtube_flow
        gate_decision(m)
        summary = build_mission_review_summary(m, recompute=False)
        assert summary.visible_blockers == []

    def test_mission_review_normal_view_no_legacy_noise(
        self, golden_canonical_youtube_flow,
    ):
        m = golden_canonical_youtube_flow
        gate_decision(m)
        s = build_mission_review_summary(m, recompute=False)
        text = render_normal_view(s).lower()
        for bad in NORMAL_VIEW_SIPE_FORBIDDEN_SUBSTRINGS:
            assert bad.lower() not in text, f"token prohibido {bad!r} en:\n{text}"

    def test_centro_status_line_lists_executable(self, golden_canonical_youtube_flow):
        m = golden_canonical_youtube_flow
        gate_decision(m)
        line = mission_ui_detail_status_line(m)
        assert "lista para ejecutar" in line.lower()

    def test_ui_execution_state_matches_gate(self, golden_canonical_youtube_flow):
        m = golden_canonical_youtube_flow
        dec = gate_decision(m)
        st = compute_mission_ui_execution_state(m)
        assert st.is_executable == dec.is_executable
        assert TruthGateCode.PLAN_NOT_READY not in st.gate_blockers

    def test_semantic_flow_step_coverage(self, golden_canonical_youtube_flow):
        m = golden_canonical_youtube_flow
        sep = m.semantic_execution_plan or {}
        types = {str((s or {}).get("type") or "") for s in (sep.get("steps") or [])}
        assert "open_app" in types
        assert "select_profile" in types
        assert "open_new_tab" in types
        assert "scroll_results" in types
        assert types & {"open_url", "open_site", "search_youtube", "search_site"}


class TestGoldenCanonicalYoutubeFlowAmbiguousProfile:

    def test_truth_confirmed_but_profile_blocker_only(self):
        m = build_golden_search_chrome_youtube_mission(
            name="golden-youtube-ambiguous-profile",
        )
        apply_ambiguous_profile_select_step(m, label="perfil")
        gate_decision(m)

        et = compute_execution_truth(m)
        assert et.confirmed

        summary = build_mission_review_summary(m, recompute=False)
        vb = set(summary.visible_blockers)
        assert vb <= PROFILE_OR_QUERY_REVIEW_FOCUS_BLOCKERS
        assert ReadyBlockerCode.EMPTY_PROFILE_NAME_IN_SELECT_PROFILE in vb

        st = compute_mission_ui_execution_state(m)
        assert not st.is_executable
        line = mission_ui_detail_status_line(m)
        assert "necesita aclaración" in line.lower()

        s = build_mission_review_summary(m, recompute=False)
        text = render_normal_view(s).lower()
        for bad in NORMAL_VIEW_SIPE_FORBIDDEN_SUBSTRINGS:
            assert bad.lower() not in text
