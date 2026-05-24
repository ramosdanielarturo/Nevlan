"""Tests — Operational State Understanding Engine (OSUE)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import (
    MachineExecutionStep,
    Mission,
    MissionStatus,
    OperationalElementIdentity,
    OperationalElementType,
    OperationalRuntimeObservation,
    OperationalStateSnapshot,
    OperationalStateType,
)
from app.services.missions.four_layer_operational_model import build_four_layer_model
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_semantic_plan_to_mission,
)
from app.services.runtime.live_operational_perception import LivePerceptionInput
from app.services.runtime.operational_runtime_consistency_engine import (
    attach_runtime_expectations_to_model,
)
from app.services.runtime.operational_state_understanding_engine import (
    OperationalStateInput,
    OsueReadiness,
    arss_synthesis_blocked,
    attach_osue_states_to_model,
    build_operational_state_snapshot,
    build_wait_condition_for_state,
    capture_operational_state_from_lop,
    detect_context_mismatch,
    enrich_observation_with_osue_states,
    evaluate_step_compatibility,
    expert_panel_lines,
    infer_operational_state_type,
    machine_step_allowed,
    orce_compare_state_transitions,
    replay_should_pause,
    required_states_for_semantic_type,
    runtime_recovery_from_blocked,
    wait_for_operational_state,
)
from app.services.runtime import adaptive_runtime_strategy_synthesis as arss


@pytest.fixture
def _osue_on(monkeypatch):
    monkeypatch.setattr(
        "app.services.runtime.operational_state_understanding_engine.osue_shadow_enabled",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "app.services.runtime.operational_state_understanding_engine.osue_enabled",
        lambda *_a, **_k: True,
    )


def _search_input_inp(**overrides):
    base = OperationalStateInput(
        dom={
            "nodes": [
                {"role": "textbox", "input_type": "search", "aria_label": "Find", "tag": "input"},
            ],
        },
        runtime_validators={"field_focused": True, "input_visible": True},
        context_hints={},
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _mission_with_search_step() -> Mission:
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="s-search",
                type="search_content",
                params={"query": "demo"},
                preferred_strategy="search_content:uol_inject",
                confidence=0.9,
                human_label="Buscar",
            ),
        ],
        source="test",
        intent_status="READY",
    )
    m = Mission(id=str(uuid.uuid4()), name="osue-test", status=MissionStatus.DRAFT)
    attach_semantic_plan_to_mission(m, plan=plan, force=True)
    build_four_layer_model(m)
    return m


# 1. Search input detected as ready state.
def test_search_input_ready_state(_osue_on):
    st, conf, ev = infer_operational_state_type(_search_input_inp())
    assert st == OperationalStateType.SEARCH_INPUT_READY_STATE
    assert conf >= 0.7
    assert any("search" in e for e in ev)


# 2. Submit produces loading → results transition.
def test_submit_loading_to_results_transition(_osue_on):
    loading_inp = OperationalStateInput(
        runtime_validators={"loading": True, "form_submitted": True},
        context_hints={"post_submit": True},
    )
    st_load, _, _ = infer_operational_state_type(loading_inp)
    assert st_load == OperationalStateType.RESULTS_LOADING_STATE

    results_inp = OperationalStateInput(
        runtime_validators={"results_visible": True},
        dom={"nodes": [{"role": "listitem"} for _ in range(5)]},
    )
    st_res, _, _ = infer_operational_state_type(results_inp)
    assert st_res == OperationalStateType.RESULTS_AVAILABLE_STATE

    cmp = orce_compare_state_transitions(
        [
            OperationalStateType.SEARCH_INPUT_READY_STATE.value,
            OperationalStateType.RESULTS_LOADING_STATE.value,
            OperationalStateType.RESULTS_AVAILABLE_STATE.value,
        ],
        [
            OperationalStateType.SEARCH_INPUT_READY_STATE.value,
            OperationalStateType.RESULTS_LOADING_STATE.value,
            OperationalStateType.RESULTS_AVAILABLE_STATE.value,
        ],
    )
    assert cmp.aligned


# 3. Infinite loading detected.
def test_infinite_loading_detected(_osue_on):
    inp = OperationalStateInput(
        runtime_validators={"loading": True, "stall_elapsed_s": 15.0},
        loading_elapsed_s=15.0,
    )
    st, _, ev = infer_operational_state_type(inp)
    assert st == OperationalStateType.LOADING_STATE
    cmp = orce_compare_state_transitions(
        [
            OperationalStateType.SEARCH_INPUT_READY_STATE.value,
            OperationalStateType.RESULTS_LOADING_STATE.value,
            OperationalStateType.RESULTS_AVAILABLE_STATE.value,
        ],
        [OperationalStateType.SEARCH_INPUT_READY_STATE.value, OperationalStateType.LOADING_STATE.value],
        loading_elapsed_s=15.0,
    )
    assert cmp.loading_anomaly
    assert cmp.mismatches


# 4. Modal interruption detected.
def test_modal_interruption_detected(_osue_on):
    from app.contracts.mission import LiveOperationalBlocker, LiveOperationalPerceptionSnapshot

    lop = LiveOperationalPerceptionSnapshot(
        modal_detected=True,
        live_blockers=[
            LiveOperationalBlocker(blocker_type="modal_blocking", severity="high"),
        ],
    )
    inp = OperationalStateInput(
        lop_snapshot=lop,
        runtime_validators={"modal_open": True},
    )
    st, _, _ = infer_operational_state_type(inp)
    assert st == OperationalStateType.MODAL_INTERRUPTION_STATE


# 5. Auth required detected.
def test_auth_required_detected(_osue_on):
    from app.contracts.mission import LiveOperationalBlocker, LiveOperationalPerceptionSnapshot

    lop = LiveOperationalPerceptionSnapshot(
        live_blockers=[LiveOperationalBlocker(blocker_type="auth_required", severity="critical")],
    )
    inp = OperationalStateInput(lop_snapshot=lop, context_hints={"auth_screen": True})
    st, _, _ = infer_operational_state_type(inp)
    assert st == OperationalStateType.AUTH_REQUIRED_STATE


# 6. Overlay blocking interaction detected.
def test_overlay_blocking_detected(_osue_on):
    inp = OperationalStateInput(runtime_validators={"overlay_blocking": True, "modal_open": True})
    st, _, _ = infer_operational_state_type(inp)
    assert st == OperationalStateType.OVERLAY_BLOCKING_STATE


# 7. Results available detected.
def test_results_available_detected(_osue_on):
    inp = OperationalStateInput(
        runtime_validators={"results_visible": True},
        dom={"nodes": [{"role": "listitem"} for _ in range(6)]},
    )
    st, conf, _ = infer_operational_state_type(inp)
    assert st == OperationalStateType.RESULTS_AVAILABLE_STATE
    assert conf >= 0.8


# 8. Empty results detected.
def test_empty_results_detected(_osue_on):
    inp = OperationalStateInput(
        runtime_validators={"results_visible": True, "empty_results": True},
        dom={"nodes": []},
    )
    st, _, _ = infer_operational_state_type(inp)
    assert st == OperationalStateType.EMPTY_RESULTS_STATE


# 9. Async refresh transition detected.
def test_async_refresh_transition(_osue_on):
    inp = OperationalStateInput(
        runtime_validators={"loading": True, "results_visible": True},
    )
    st, _, ev = infer_operational_state_type(inp)
    assert st == OperationalStateType.ASYNC_REFRESH_STATE
    assert any("async" in e for e in ev)


# 10. Context mismatch detected.
def test_context_mismatch_detected(_osue_on):
    from app.contracts.mission import LiveOperationalPerceptionSnapshot, LiveOperationalSurfaceType

    lop = LiveOperationalPerceptionSnapshot(surface_type=LiveOperationalSurfaceType.FORM_SURFACE)
    inp = OperationalStateInput(
        lop_snapshot=lop,
        context_hints={"expected_surface_types": ["search_surface", "results_surface"]},
    )
    assert detect_context_mismatch(inp) is True
    inp2 = OperationalStateInput(context_hints={"context_mismatch": True})
    st, _, _ = infer_operational_state_type(inp2)
    assert st == OperationalStateType.CONTEXT_SWITCH_STATE


# 11. Runtime waits on state not sleep.
def test_runtime_waits_on_state_not_sleep(_osue_on):
    calls: list = []

    def factory():
        calls.append("poll")
        return OperationalStateSnapshot(
            operational_state_type=OperationalStateType.RESULTS_AVAILABLE_STATE,
        )

    with patch("app.services.missions.wait_manager.wait_until") as mock_wait:
        mock_wait.return_value = SimpleNamespace(matched=True, elapsed_ms=400, polled=2, timed_out=False)
        result = wait_for_operational_state(
            OperationalStateType.RESULTS_AVAILABLE_STATE.value,
            snapshot_factory=factory,
            timeout_ms=5000,
        )
        mock_wait.assert_called_once()
        assert result.matched is True

    cond = build_wait_condition_for_state(OperationalStateType.RESULTS_AVAILABLE_STATE.value)
    snap = OperationalStateSnapshot(operational_state_type=OperationalStateType.RESULTS_AVAILABLE_STATE)
    assert cond(snap) is True


# 12. ARSS blocked during unstable state.
def test_arss_blocked_during_unstable_state(_osue_on, monkeypatch):
    unstable = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.UNKNOWN_OPERATIONAL_STATE,
        ambiguity_score=0.8,
        state_stability_score=0.3,
        loading_state="active",
    )
    blocked, reasons = arss_synthesis_blocked(unstable)
    assert blocked is True
    assert reasons

    m = _mission_with_search_step()
    m.osue_last_snapshot = unstable.model_dump(mode="json")
    obs = OperationalRuntimeObservation(step_id="s-search", divergence_detected=True)
    attempt = arss.synthesize_variants_from_divergence(m, step_id="s-search", observation=obs)
    assert attempt.synthesis_strategy == "blocked_unstable_operational_state"


# 13. ORCE compares expected vs actual states.
def test_orce_compares_expected_vs_actual_states(_osue_on):
    cmp_ok = orce_compare_state_transitions(
        ["search_input_ready_state", "results_loading_state", "results_available_state"],
        ["search_input_ready_state", "results_loading_state", "results_available_state"],
    )
    assert cmp_ok.aligned

    cmp_bad = orce_compare_state_transitions(
        ["search_input_ready_state", "results_available_state"],
        ["search_input_ready_state", "loading_state"],
    )
    assert cmp_bad.aligned is False
    assert cmp_bad.mismatches


# 14. Replay pauses during loading.
def test_replay_pauses_during_loading(_osue_on):
    loading_snap = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.RESULTS_LOADING_STATE,
        loading_state="active",
        state_stability_score=0.4,
    )
    assert replay_should_pause(loading_snap) is True

    ready_snap = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.SEARCH_INPUT_READY_STATE,
        loading_state="idle",
        state_stability_score=0.8,
    )
    assert replay_should_pause(ready_snap) is False


# 15. UOEI feeds state understanding.
def test_uoei_feeds_state_understanding(_osue_on):
    ident = OperationalElementIdentity(
        operational_element_type=OperationalElementType.SEARCH_INPUT_SURFACE,
        confidence=0.88,
        structural_signature="searchbox:role=edit",
        historical_matches=["stable_search_input_v3"],
        ambiguity_score=0.1,
    )
    snap = build_operational_state_snapshot(
        OperationalStateInput(
            element_identities=[ident],
            runtime_validators={"field_focused": True},
            dom={"nodes": [{"role": "searchbox", "input_type": "search"}]},
        ),
    )
    assert any(
        "search_input_surface" in e.lower() or "search" in e.lower()
        for e in snap.visible_operational_elements
    )
    assert "stable_search_input_v3" in snap.historical_matches


# 16. No hardcoded app/site names.
def test_no_hardcoded_app_site_names(_osue_on):
    import inspect

    from app.services.runtime import operational_state_understanding_engine as osue_mod

    src = inspect.getsource(osue_mod)
    forbidden = ["youtube", "chrome", "google", "facebook", "twitter", "netflix"]
    lower = src.lower()
    for name in forbidden:
        assert name not in lower, f"hardcoded app name found: {name}"


# 17. State stability score decreases with ambiguity.
def test_stability_decreases_with_ambiguity(_osue_on):
    from app.contracts.mission import LiveOperationalSignal, LiveOperationalPerceptionSnapshot

    low_amb = build_operational_state_snapshot(_search_input_inp())
    high_amb = build_operational_state_snapshot(
        OperationalStateInput(
            dom={"nodes": [{"role": "textbox", "input_type": "search"}]},
            runtime_validators={"field_focused": True},
            lop_snapshot=LiveOperationalPerceptionSnapshot(
                ambiguity_signals=[
                    LiveOperationalSignal(kind="multi_target", source="uia", weight=0.5, payload={}),
                ]
                * 4,
                confidence=0.3,
            ),
            element_identities=[
                OperationalElementIdentity(ambiguity_score=0.9, confidence=0.4),
            ],
        ),
    )
    assert high_amb.ambiguity_score > low_amb.ambiguity_score
    assert high_amb.state_stability_score < low_amb.state_stability_score


# 18. Runtime recovery triggered from blocked state.
def test_runtime_recovery_from_blocked_state(_osue_on):
    modal_snap = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.MODAL_INTERRUPTION_STATE,
    )
    assert "modal" in runtime_recovery_from_blocked(modal_snap).lower()

    auth_snap = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.AUTH_REQUIRED_STATE,
    )
    assert "human" in runtime_recovery_from_blocked(auth_snap).lower()

    load_snap = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.LOADING_STATE,
        loading_state="active",
    )
    assert runtime_recovery_from_blocked(load_snap) == "wait_for_state_completion"


# 19. Results_loading → results_available recognized universally.
def test_results_loading_to_available_universal(_osue_on):
    chain = [
        OperationalStateType.RESULTS_LOADING_STATE.value,
        OperationalStateType.RESULTS_AVAILABLE_STATE.value,
    ]
    cmp = orce_compare_state_transitions(
        [
            OperationalStateType.SEARCH_INPUT_READY_STATE.value,
            OperationalStateType.RESULTS_LOADING_STATE.value,
            OperationalStateType.RESULTS_AVAILABLE_STATE.value,
        ],
        [OperationalStateType.SEARCH_INPUT_READY_STATE.value] + chain,
    )
    assert cmp.aligned

    cond = build_wait_condition_for_state(OperationalStateType.RESULTS_AVAILABLE_STATE.value)
    assert cond(
        OperationalStateSnapshot(operational_state_type=OperationalStateType.RESULTS_AVAILABLE_STATE),
    )


# 20. Machine Layer uses required_operational_states.
def test_machine_layer_required_operational_states(_osue_on):
    m = _mission_with_search_step()
    from app.services.missions.four_layer_operational_model import ensure_four_layer_model

    model = ensure_four_layer_model(m)
    attach_runtime_expectations_to_model(model)

    ms = model.machine_execution.steps[0]
    assert "required_operational_states" in ms.params
    assert OperationalStateType.SEARCH_INPUT_READY_STATE.value in ms.params["required_operational_states"]
    assert ms.runtime_expectation is not None
    assert ms.runtime_expectation.required_operational_states
    assert ms.runtime_expectation.blocked_operational_states

    attach_osue_states_to_model(model)
    compat = evaluate_step_compatibility(
        OperationalStateSnapshot(
            operational_state_type=OperationalStateType.SEARCH_INPUT_READY_STATE,
            state_stability_score=0.85,
            ambiguity_score=0.1,
        ),
        ms,
    )
    assert compat.compatible is True
    assert compat.readiness == OsueReadiness.READY

    blocked_compat = evaluate_step_compatibility(
        OperationalStateSnapshot(
            operational_state_type=OperationalStateType.MODAL_INTERRUPTION_STATE,
            runtime_blockers=["modal_blocking"],
        ),
        ms,
    )
    assert blocked_compat.compatible is False
    assert machine_step_allowed(
        OperationalStateSnapshot(operational_state_type=OperationalStateType.MODAL_INTERRUPTION_STATE),
        ms,
    ) is False


def test_refresh_osue_shadow_persists(_osue_on):
    m = _mission_with_search_step()
    m.osue_shadow_mode = True
    snap = capture_operational_state_from_lop(
        m,
        perception_input=LivePerceptionInput(
            dom={"nodes": [{"role": "searchbox", "input_type": "search"}]},
            runtime_validators={"field_focused": True},
        ),
    )
    assert snap.operational_state_type == OperationalStateType.SEARCH_INPUT_READY_STATE

    from app.services.runtime.operational_state_understanding_engine import refresh_osue_shadow

    out = refresh_osue_shadow(m, SimpleNamespace(OSUE_SHADOW_ENABLED=True))
    assert m.osue_last_snapshot is not None
    assert m.osue_state_audit.get("ok") is True


def test_expert_panel_lines(_osue_on):
    m = _mission_with_search_step()
    m.osue_last_snapshot = OperationalStateSnapshot(
        operational_state_type=OperationalStateType.RESULTS_LOADING_STATE,
        state_stability_score=0.5,
        ambiguity_score=0.3,
        runtime_blockers=["network_loading_stall"],
        expected_transitions=["search_input_ready_state", "results_available_state"],
        observed_transitions=["search_input_ready_state", "results_loading_state"],
    ).model_dump(mode="json")
    lines = expert_panel_lines(m, expert_mode=True)
    assert any("OSUE" in ln for ln in lines)
    assert expert_panel_lines(m, expert_mode=False) == []


def test_enrich_observation_with_osue_states(_osue_on):
    obs = OperationalRuntimeObservation(step_id="s1")
    enriched = enrich_observation_with_osue_states(
        obs,
        ["search_input_ready_state", "results_loading_state"],
    )
    assert enriched.runtime_operational_state_chain == [
        "search_input_ready_state",
        "results_loading_state",
    ]
