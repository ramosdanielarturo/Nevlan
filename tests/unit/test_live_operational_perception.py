"""Live Operational Perception (LOP) — Fase 1 sombra."""

from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from app.contracts.mission import Mission, MissionStatus, LiveOperationalRuntimeAction, LiveOperationalSurfaceType
from app.services.runtime import goal_oriented_runtime as goor
from app.services.runtime import operational_runtime_graph as orgmod
from app.services.runtime.live_operational_perception import (
    LivePerceptionInput,
    build_arl_lop_recovery_hints,
    capture_live_operational_snapshot,
    classify_live_surface,
    extract_live_entities,
    lop_expert_panel_lines,
    observe_live_validators,
    refresh_lop_shadow,
)


@pytest.fixture
def _lop_on(monkeypatch):
    monkeypatch.setattr(
        "app.services.runtime.live_operational_perception._settings_lop_enabled",
        lambda *_a, **_k: True,
    )


def _inp_browser_search_results():
    return LivePerceptionInput(
        active_app="GenericBrowser",
        active_window="Browser",
        dom={
            "url": "https://provider.example/search?q=demo",
            "nodes": [
                {"role": "textbox", "input_type": "search", "aria_label": "Search", "tag": "input"},
                {"role": "listitem", "visible_text": "Result A", "tag": "li"},
                {"role": "listitem", "visible_text": "Result B", "tag": "li"},
                {"role": "listitem", "visible_text": "Result C", "tag": "li"},
                {"role": "listitem", "visible_text": "Result D", "tag": "li"},
            ],
        },
        uia_nodes=[
            {
                "role": "Document",
                "name": "page",
                "children": [
                    {"role": "edit", "name": "Search query"},
                    {"role": "listitem", "name": "Item 1"},
                    {"role": "listitem", "name": "Item 2"},
                    {"role": "listitem", "name": "Item 3"},
                    {"role": "listitem", "name": "Item 4"},
                ],
            }
        ],
        runtime_validators={
            "address_bar_visible": True,
            "results_visible": True,
            "search_field_focused": False,
        },
    )


def test_browser_surface_detected(_lop_on):
    inp = LivePerceptionInput(dom={"url": "https://example.test/path"})
    st, _ = classify_live_surface(
        uia_flat=[],
        dom=inp.dom,
        validators={},
        context_hints={},
    )
    assert st == LiveOperationalSurfaceType.BROWSER_SURFACE


def test_search_surface_detected(_lop_on):
    inp = LivePerceptionInput(
        dom={
            "nodes": [
                {"role": "searchbox", "aria_label": "Find", "tag": "input", "input_type": "search"},
            ],
        },
    )
    flat, _ = extract_live_entities(uia_flat=[], dom=inp.dom)
    assert any(e.kind.value == "searchbox" for e in flat)
    st, _ = classify_live_surface(
        uia_flat=[],
        dom=inp.dom,
        validators={"field_focused": True},
        context_hints={},
    )
    assert st == LiveOperationalSurfaceType.SEARCH_SURFACE


def test_results_surface_detected(_lop_on):
    st, ranked = classify_live_surface(
        uia_flat=[],
        dom={
            "nodes": [{"role": "listitem"} for _ in range(6)]
            + [{"role": "textbox", "input_type": "search"}],
        },
        validators={"results_visible": True},
        context_hints={},
    )
    assert st == LiveOperationalSurfaceType.RESULTS_SURFACE
    assert ranked[0].surface_type == LiveOperationalSurfaceType.RESULTS_SURFACE


def test_form_surface_detected(_lop_on):
    st, _ = classify_live_surface(
        uia_flat=[],
        dom={
            "nodes": [
                {"role": "textbox", "input_type": "text"},
                {"role": "textbox", "input_type": "email"},
                {"role": "button", "visible_text": "Submit"},
            ],
        },
        validators={},
        context_hints={},
    )
    assert st == LiveOperationalSurfaceType.FORM_SURFACE


def test_dialog_surface_detected(_lop_on):
    st, _ = classify_live_surface(
        uia_flat=[
            {"role": "Window", "name": "Confirm", "modal": True},
        ],
        dom={"nodes": [{"role": "dialog"}]},
        validators={"modal_open": True},
        context_hints={},
    )
    assert st == LiveOperationalSurfaceType.DIALOG_SURFACE


def test_modal_blocker_detected(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    snap = capture_live_operational_snapshot(
        m,
        perception_input=LivePerceptionInput(
            runtime_validators={"modal_open": True},
            dom={"nodes": [{"role": "dialog", "aria_label": "Notice"}]},
        ),
    )
    assert snap.modal_detected
    assert any(b.blocker_type == "modal_blocking" for b in snap.live_blockers)


def test_multiple_targets_request_human(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    ents = [
        {"role": "Button", "name": ""},
        {"role": "Button", "name": ""},
        {"role": "Button", "name": ""},
    ]
    snap = capture_live_operational_snapshot(
        m,
        perception_input=LivePerceptionInput(uia_nodes=ents, dom={"nodes": []}),
    )
    assert snap.recommended_runtime_action == LiveOperationalRuntimeAction.REQUEST_HUMAN
    assert snap.safe_to_continue is False


def test_unknown_surface_revalidate_or_wait(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    snap = capture_live_operational_snapshot(m, perception_input=LivePerceptionInput())
    assert snap.surface_type == LiveOperationalSurfaceType.UNKNOWN_SURFACE
    assert snap.recommended_runtime_action in (
        LiveOperationalRuntimeAction.WAIT,
        LiveOperationalRuntimeAction.REVALIDATE,
        LiveOperationalRuntimeAction.REQUEST_HUMAN,
    )


def test_results_visible_goal_matched(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    snap = capture_live_operational_snapshot(m, perception_input=_inp_browser_search_results())
    assert "results_visible" in snap.matched_goal_ids
    assert snap.surface_type in {
        LiveOperationalSurfaceType.RESULTS_SURFACE,
        LiveOperationalSurfaceType.SEARCH_SURFACE,
        LiveOperationalSurfaceType.BROWSER_SURFACE,
    }


def test_form_submitted_goal_matched(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    snap = capture_live_operational_snapshot(
        m,
        perception_input=LivePerceptionInput(
            dom={
                "nodes": [
                    {"role": "textbox", "input_type": "text"},
                    {"role": "textbox", "input_type": "text"},
                    {"role": "button"},
                ],
            },
            runtime_validators={"form_submitted": True},
        ),
    )
    assert "form_submitted" in snap.matched_goal_ids


def test_missing_required_surface_recover(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    snap = capture_live_operational_snapshot(
        m,
        perception_input=LivePerceptionInput(
            context_hints={"expected_surface_types": ["results_surface"]},
            dom={"url": "https://x.test", "nodes": []},
            runtime_validators={},
        ),
    )
    assert snap.recommended_runtime_action == LiveOperationalRuntimeAction.RECOVER


def test_loading_stall_wait(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    snap = capture_live_operational_snapshot(
        m,
        perception_input=LivePerceptionInput(
            dom={"url": "https://x.test"},
            runtime_validators={"network_stalled": True},
        ),
    )
    assert snap.recommended_runtime_action == LiveOperationalRuntimeAction.WAIT


def test_unsafe_destructive_dialog_stop(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    snap = capture_live_operational_snapshot(
        m,
        perception_input=LivePerceptionInput(
            runtime_validators={"destructive_confirm_unknown": True},
        ),
    )
    assert snap.recommended_runtime_action == LiveOperationalRuntimeAction.STOP_UNSAFE


def test_lop_does_not_mutate_sep(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    m.lop_shadow_mode = True
    m.semantic_execution_plan = {"steps": [{"type": "navigate"}, {"type": "search_site"}]}
    m.compiled_execution_graph = []
    before_plan = copy.deepcopy(m.semantic_execution_plan)
    refresh_lop_shadow(m, SimpleNamespace(LOP_SHADOW_ENABLED=True))
    assert m.semantic_execution_plan == before_plan
    assert m.legacy_compiled_graph_purpose == "execution"


def test_lop_shadow_mode_legacy_safe(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    assert getattr(m, "lop_shadow_mode", False) is False
    refresh_lop_shadow(m, SimpleNamespace(LOP_SHADOW_ENABLED=True))
    assert m.lop_last_snapshot is None
    assert isinstance(m.live_operational_perception_audit, dict)
    assert m.live_operational_perception_audit.get("ok") is False


def test_ocr_only_when_insufficient_text(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    calls = {"n": 0}

    def ocr():
        calls["n"] += 1
        return ["fallback text"]

    rich = LivePerceptionInput(
        uia_nodes=[{"role": "Edit", "name": "Enough visible text here to exceed threshold"}],
        dom={"nodes": [{"visible_text": "more text content for budget"}]},
    )
    capture_live_operational_snapshot(m, perception_input=rich, ocr_light=ocr, text_sufficiency_threshold=12)
    assert calls["n"] == 0

    capture_live_operational_snapshot(
        m,
        perception_input=LivePerceptionInput(uia_nodes=[], dom={"nodes": []}),
        ocr_light=ocr,
        text_sufficiency_threshold=500,
    )
    assert calls["n"] == 1


def test_goor_decision_context_consumes_lop(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    m.lop_shadow_mode = True
    m.truth_blocks = []
    snap = capture_live_operational_snapshot(m, perception_input=_inp_browser_search_results())
    m.lop_last_snapshot = snap.model_dump(mode="json")
    g = orgmod.build_operational_runtime_graph(m)
    ctx = goor.build_operational_decision_context(m, g)
    assert ctx.live_operational_perception_echo.get("surface_type") is not None
    assert ctx.live_operational_perception_echo.get("recommended_runtime_action") is not None


def test_arl_consumes_lop_blockers(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    m.lop_shadow_mode = True
    refresh_lop_shadow(
        m,
        SimpleNamespace(LOP_SHADOW_ENABLED=True),
        perception_input=LivePerceptionInput(runtime_validators={"modal_open": True}),
    )
    hints = build_arl_lop_recovery_hints(m)
    assert hints.get("lop_present")
    assert any("modal" in str(b.get("type", "")).lower() for b in hints.get("blockers", []))


def test_mission_review_expert_lop_lines(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)
    m.lop_shadow_mode = True
    refresh_lop_shadow(m, SimpleNamespace(LOP_SHADOW_ENABLED=True), perception_input=_inp_browser_search_results())
    lines = lop_expert_panel_lines(m)
    assert any("surface_type" in ln for ln in lines)
    assert any("live_blockers" in ln or "validators_observed" in ln for ln in lines)


def test_performance_budget_mocked(_lop_on):
    m = Mission(name="m", status=MissionStatus.DRAFT)

    def slow():
        time.sleep(0.06)
        return []

    shot = capture_live_operational_snapshot(
        m,
        perception_input=LivePerceptionInput(dom={"url": "https://slow.test"}),
        ocr_light=slow,
        text_sufficiency_threshold=900,
        max_elapsed_ms=20,
    )
    assert any("slow_path" in (g or "") for g in shot.perception_gaps)


def test_end_to_end_continue_safe(_lop_on):
    m = Mission(name="flow", status=MissionStatus.DRAFT)
    snap = capture_live_operational_snapshot(m, perception_input=_inp_browser_search_results())
    # Sin bloqueos críticos y con señal fuerte de resultados, debe tender a seguir.
    assert snap.recommended_runtime_action == LiveOperationalRuntimeAction.CONTINUE
    assert snap.safe_to_continue is True
    assert snap.surface_type in (
        LiveOperationalSurfaceType.RESULTS_SURFACE,
        LiveOperationalSurfaceType.BROWSER_SURFACE,
    )


def test_observe_live_validators_mapping():
    obs = observe_live_validators({"results_visible": True, "form_submitted": False})
    keys = {o.validator_key for o in obs}
    assert "results_visible" in keys
    assert "form_submitted" in keys
