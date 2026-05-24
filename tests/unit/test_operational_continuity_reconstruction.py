"""Tests — Operational Continuity Reconstruction (OCRX)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.contracts.mission import (
    CanonicalOperationalIntent,
    EventType,
    Mission,
    MouseAction,
    OperationalFreezeCollectionSnapshot,
    OperationalFreezeEntityCandidate,
    OperationalFreezeSnapshot,
    RawEvent,
    TransitionRiskLevel,
    WindowContext,
)
from app.services.missions.freeze_first_execution_core import (
    attach_coi_to_mission,
    promote_freeze_to_canonical_intent,
)
from app.services.missions.mission_truth_gate import TruthGateCode, gate_decision
from app.services.missions.mission_review_summary import build_mission_review_summary
from app.services.missions.operational_continuity_reconstruction import (
    BRIDGE_PROFILE_PICKER_TO_APPLICATION,
    BRIDGE_SEARCH_LAUNCHER_TO_APPLICATION,
    apply_ocrx_before_ready_status,
    audit_operational_continuity,
    build_operational_threads,
    build_transition_bridges,
    mission_truth_and_continuity_gate_open,
)
from app.services.missions.operational_truth_lineage import (
    assert_no_orphan_canonical_truth,
)
from app.services.missions.operational_truth_preservation import apply_otp_after_promotion
from app.services.missions.semantic_execution_plan import (
    ReadyBlockerCode,
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_intent_status_to_plan,
    compute_ready_status,
)
from app.services.missions.semantic_intent_promotion_engine import apply_promotion_to_mission

_BASE = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
PROFILE = "Daniel Chrome"


def _click(
    x: int,
    y: int,
    *,
    ms: int = 0,
    proc: str = "SearchUI.exe",
    title: str = "Search",
    meta: dict | None = None,
) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_CLICK,
        timestamp=_BASE + timedelta(milliseconds=ms),
        mouse_action=MouseAction(x=x, y=y, button="left"),
        window_context=WindowContext(hwnd=1, title=title, process_name=proc),
        metadata=dict(meta or {}),
    )


def _freeze_profile(*, event_id: str) -> OperationalFreezeSnapshot:
    entity_label = f"Abrir el perfil de {PROFILE}"
    return OperationalFreezeSnapshot(
        freeze_id=str(uuid.uuid4()),
        pre_transition_confirmed=True,
        freeze_confidence=0.95,
        transition_risk=TransitionRiskLevel.HIGH,
        best_entity_candidate=OperationalFreezeEntityCandidate(
            display_name=entity_label,
            normalized_name="daniel_chrome",
            confidence=1.0,
            evidence_sources=["uia", "collection"],
            absorbed_ambiguity=True,
        ),
        best_collection_candidate=OperationalFreezeCollectionSnapshot(
            collection_type="list",
            display_name="profiles",
            entities=[entity_label, entity_label],
            entity_count=2,
            confidence=0.9,
        ),
        uia_targets=[
            {
                "source": "uia",
                "role": "listitem",
                "name": entity_label,
                "overlap_score": 1.0,
            },
        ],
    )


def _mission_searchui_to_chrome() -> Mission:
    """SearchUI → click perfil → chrome.exe (continuidad esperada)."""
    ev_profile = _click(200, 100, ms=500, proc="SearchUI.exe", title="Google Chrome")
    freeze = _freeze_profile(event_id=ev_profile.id)
    ev_profile.metadata["operational_freeze_snapshot"] = freeze.model_dump(mode="json")
    coi = promote_freeze_to_canonical_intent(freeze, source_event_id=ev_profile.id)
    assert coi is not None, "FFEC debe promover COI con freeze fuerte"
    coi = coi.model_copy(
        update={"transition_observed": True, "transition_expected": True},
    )
    ev_profile.metadata["canonical_operational_intent"] = coi.model_dump(mode="json")

    trace = [
        _click(50, 50, ms=0, proc="SearchUI.exe"),
        ev_profile,
        _click(10, 10, ms=1200, proc="chrome.exe", title="Google Chrome"),
    ]
    m = Mission(id=str(uuid.uuid4()), name="ocrx-search-chrome", raw_trace=trace)
    attach_coi_to_mission(m, coi)
    m.semantic_execution_plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="s0",
                type="open_app",
                params={"name": "chrome", "method": "windows_search"},
                preferred_strategy="open_app:windows_search",
                fallback_strategies=[],
                confidence=0.95,
                human_label="Abrir Chrome",
            ),
            SemanticPlanStep(
                id="s1",
                type="open_new_tab",
                params={},
                preferred_strategy="open_new_tab:hotkey_ctrl_t",
                fallback_strategies=[],
                confidence=0.9,
                human_label="Abrir nueva pestaña",
            ),
        ],
        source="semantic_intent_promotion_engine",
    ).to_dict()
    return m


class TestContinuityDetection:

    def test_threads_from_canonical_coi(self):
        m = _mission_searchui_to_chrome()
        threads = build_operational_threads(m)
        assert len(threads) >= 1
        th = threads[0]
        assert th.canonical_truth
        assert th.entity_confidence >= 0.90
        assert th.continuity_preserved
        assert PROFILE in th.human_label

    def test_bridge_search_or_profile_picker(self):
        m = _mission_searchui_to_chrome()
        threads = build_operational_threads(m)
        bridges = build_transition_bridges(m, threads)
        assert bridges
        assert bridges[0].bridge_type in (
            BRIDGE_SEARCH_LAUNCHER_TO_APPLICATION,
            BRIDGE_PROFILE_PICKER_TO_APPLICATION,
        )


class TestMaterializationNotBlockerFilter:

    def test_empty_profile_blocker_cleared_by_materialization(self):
        m = _mission_searchui_to_chrome()
        apply_ocrx_before_ready_status(m)
        assert not assert_no_orphan_canonical_truth(m)
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        attach_intent_status_to_plan(plan, mission=m)
        _, blockers = compute_ready_status(plan, mission=m)
        assert ReadyBlockerCode.EMPTY_PROFILE_NAME_IN_SELECT_PROFILE not in blockers

    def test_no_gate_plan_not_ready_with_strong_coi(self):
        m = _mission_searchui_to_chrome()
        apply_promotion_to_mission(m)
        apply_otp_after_promotion(m)
        dec = gate_decision(m)
        assert TruthGateCode.PLAN_NOT_READY not in dec.blockers
        if dec.is_executable:
            assert dec.intent_status in ("READY", "EXECUTABLE")


class TestSepReconstruction:

    def test_backfills_profile_step(self):
        m = _mission_searchui_to_chrome()
        apply_ocrx_before_ready_status(m)
        sep = m.semantic_execution_plan or {}
        types = [str(s.get("type") or "") for s in sep.get("steps") or []]
        assert "select_entity_from_collection" in types
        labels = [str(s.get("human_label") or "") for s in sep.get("steps") or []]
        assert any(PROFILE in lab for lab in labels)
        assert not any("smart_route" in lab.lower() for lab in labels)

    def test_ready_status_without_profile_blocker(self):
        m = _mission_searchui_to_chrome()
        apply_ocrx_before_ready_status(m)
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        attach_intent_status_to_plan(plan, mission=m)
        status, blockers = compute_ready_status(plan, mission=m)
        assert ReadyBlockerCode.EMPTY_PROFILE_NAME_IN_SELECT_PROFILE not in blockers
        if status == "READY":
            assert blockers == []


class TestMissionReview:

    def test_mission_review_shows_profile_step(self):
        m = _mission_searchui_to_chrome()
        apply_promotion_to_mission(m)
        apply_otp_after_promotion(m)
        summary = build_mission_review_summary(m, recompute=True)
        labels = [s.human_label for s in summary.steps]
        assert any(PROFILE in lab for lab in labels)
        assert "GATE_PLAN_NOT_READY" not in " ".join(summary.visible_blockers)


class TestTelemetryFields:

    def test_uol_record_accepts_continuity_fields(self):
        from app.services.missions.uol_runtime_telemetry import UolStepTelemetryRecord

        rec = UolStepTelemetryRecord(
            step_id="s1",
            continuity_preserved=True,
            continuity_bridge_used=BRIDGE_PROFILE_PICKER_TO_APPLICATION,
            operational_thread_id="thread:abc",
            surface_transition_type="application_handoff",
            continuity_confidence=0.95,
        )
        assert rec.continuity_preserved is True
        assert rec.operational_thread_id == "thread:abc"
