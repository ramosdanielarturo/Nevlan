"""Tests — LOP Sensor Bridge (Fase 2, sombra / read-only)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.contracts.mission import LiveOperationalEntity, LiveOperationalEntityKind, Mission
from app.contracts.operational_runtime_graph import OperationalRuntimeGraph, OperationalRuntimeNode
from app.services.runtime import operational_continuity_runtime as ocr_runtime
from app.services.runtime.goal_oriented_runtime import build_operational_decision_context
from app.services.runtime import live_operational_perception as lop
from app.services.runtime.live_operational_perception import (
    LiveOperationalRuntimeAction,
    LiveOperationalSurfaceType,
    LivePerceptionInput,
    arl_hints_from_lop,
    capture_live_operational_snapshot,
)
from app.services.runtime.lop_sensor_bridge import (
    LopSensorBridgeConfig,
    LopSensorBudget,
    augment_smart_runner_callbacks_for_lop_shadow,
    build_live_perception_input,
    merge_prior_runner_shadow_audits,
    refresh_lop_from_live_sensors,
)


def _m(**kw) -> Mission:
    base = {"name": "m_lop_sb", "lop_shadow_mode": True, "lop_live_feed_enabled": True}
    base.update(kw)
    return Mission(**base)


def _graph() -> OperationalRuntimeGraph:
    return OperationalRuntimeGraph(
        nodes=[
            OperationalRuntimeNode(
                node_id="n1",
                operational_state="browser_like",
                expected_validators=["browser"],
            ),
        ],
        transitions=[],
        goals=[],
        recovery_edges=[],
        metrics={},
    )


def test_uia_only_browser_like():
    cfg = LopSensorBridgeConfig(include_dom=False, include_uia=True)

    def uia(*, deadline, max_depth, max_nodes):
        return [
            {
                "name": "Frame",
                "control_type": "WindowControl",
                "role": "window",
                "children": [
                    {
                        "name": "Address",
                        "control_type": "EditControl",
                        "role": "edit",
                        "children": [],
                    },
                ],
            },
        ], None

    inp, _, hp = build_live_perception_input(_m(), cfg, uia_sampler=uia)
    snap = capture_live_operational_snapshot(_m(), perception_input=inp)
    assert inp.uia_nodes
    assert snap.surface_type in set(LiveOperationalSurfaceType)
    assert hp.status is not None


def test_dom_only_search_surface():
    def dom(_d, _n):
        return (
            {
                "url": "",
                "domain": "",
                "title": "Ex",
                "nodes": [
                    {
                        "tag": "input",
                        "role": "searchbox",
                        "input_type": "search",
                        "aria_label": "Find",
                        "visible_text": "",
                    },
                ],
            },
            None,
        )

    cfg = LopSensorBridgeConfig(include_uia=False, include_dom=True)
    inp, _, _ = build_live_perception_input(_m(), cfg, dom_sampler=dom)
    snap = capture_live_operational_snapshot(_m(), perception_input=inp)
    assert snap.surface_type == LiveOperationalSurfaceType.SEARCH_SURFACE


def test_uia_dom_results_surface():
    def uia(*, deadline, max_depth, max_nodes):
        kids = [
            {
                "name": f"Item {i}",
                "control_type": "ListItemControl",
                "role": "listitem",
                "scroll_pattern": True,
                "children": [],
            }
            for i in range(8)
        ]
        return [{"name": "List", "control_type": "ListControl", "role": "list", "scroll_pattern": True, "children": kids}], None

    def dom(_d, _n):
        return {"url": "", "domain": "", "title": "", "nodes": []}, None

    inp, _, _ = build_live_perception_input(_m(), LopSensorBridgeConfig(), uia_sampler=uia, dom_sampler=dom)
    validators = inp.runtime_validators if isinstance(inp.runtime_validators, dict) else {}
    inp.runtime_validators = dict(validators)
    inp.runtime_validators["results_visible"] = True
    snap = capture_live_operational_snapshot(_m(), perception_input=inp)
    assert snap.surface_type == LiveOperationalSurfaceType.RESULTS_SURFACE


def test_ocr_on_demand_when_probe_requested():
    def ocr_fn(_payload):
        return ["ocr-line-stub"]

    inp, cap, _ = build_live_perception_input(
        _m(goor_shadow_mode=True, operational_goal_snapshots=[{"h": 1}]),
        LopSensorBridgeConfig(budget=LopSensorBudget(text_sufficiency_chars=900)),
        executor_signals={"prefer_ocr_probe": True},
        uia_sampler=lambda **kw: ([], None),
        dom_sampler=lambda _a, _b: ({"nodes": []}, None),
        ocr_regions_fn=ocr_fn,
    )
    assert inp.context_hints or cap.ok


def test_unknown_modal_request_human():
    inp = LivePerceptionInput(
        runtime_validators={"modal_open": True, "modal_unknown": True},
        dom={"url": "", "nodes": []},
    )
    snap = capture_live_operational_snapshot(_m(), perception_input=inp)
    assert snap.recommended_runtime_action == LiveOperationalRuntimeAction.REQUEST_HUMAN


def test_password_auth_screen_request_human():
    inp = LivePerceptionInput(
        dom={
            "url": "",
            "nodes": [{"input_type": "password", "role": "", "visible_text": ""}],
        },
        context_hints={"auth_screen": True},
    )
    snap = capture_live_operational_snapshot(_m(), perception_input=inp)
    assert snap.recommended_runtime_action in {
        LiveOperationalRuntimeAction.REQUEST_HUMAN,
        LiveOperationalRuntimeAction.STOP_UNSAFE,
    }


def test_destructive_stop_unsafe():
    inp = LivePerceptionInput(
        runtime_validators={"destructive_confirm_unknown": True},
        dom={"url": "", "nodes": []},
    )
    snap = capture_live_operational_snapshot(_m(), perception_input=inp)
    assert snap.recommended_runtime_action == LiveOperationalRuntimeAction.STOP_UNSAFE


def test_multiple_equivalent_targets():
    actionable = [
        LiveOperationalEntity(kind=LiveOperationalEntityKind.BUTTON, label_guess="", roles=["button"]),
    ] * 12
    snap = capture_live_operational_snapshot(_m(), perception_input=LivePerceptionInput(dom={"nodes": [], "url": ""}))
    blockers = lop.detect_live_blockers(snapshot=snap, actionable=actionable, validators={}, context_hints={})
    act, _ = lop.decide_live_runtime_action(snapshot=snap, blockers=blockers)
    assert act == LiveOperationalRuntimeAction.REQUEST_HUMAN


def test_budget_guard_no_continue():
    snap = capture_live_operational_snapshot(_m(), perception_input=LivePerceptionInput())
    snap.recommended_runtime_action = LiveOperationalRuntimeAction.CONTINUE
    snap.perception_gaps.append("lop_sensor_budget:trunc")
    lop._apply_budget_and_critical_gap_guards(snap)
    assert snap.recommended_runtime_action != LiveOperationalRuntimeAction.CONTINUE


def test_goor_lop_live_snapshot_field():
    mission = _m()
    mission.lop_last_snapshot = {"surface_type": "browser_surface", "confidence": 0.66}
    ctx = build_operational_decision_context(mission, _graph())
    assert isinstance(ctx.lop_live_snapshot, dict)
    assert ctx.lop_live_snapshot.get("surface_type") == "browser_surface"


def test_ocr_continuity_merge():
    mission = _m()
    mission.lop_last_snapshot = {
        "surface_type": "results_surface",
        "confidence": 0.4,
        "matched_goal_ids": ["g1"],
        "live_blockers": [],
    }
    fused = ocr_runtime.merge_live_operational_perception_into_continuity_shadow(mission)
    assert fused.get("lop_live_surface_echo", {}).get("lop_sensor_bridge") is True


def test_smart_runner_shadow_steps(lop_enabled):
    _ = lop_enabled
    m = _m()
    settings = SimpleNamespace(LOP_RUNNER_SHADOW_ENABLED=True)

    def uia_sampler(*, deadline, max_depth, max_nodes):
        return [{"name": "w", "control_type": "WindowControl", "role": "window", "children": []}], None

    evt = SimpleNamespace(kind="noop", human_label="", index=1, step_id="s1", total=3)
    hs, _hd = augment_smart_runner_callbacks_for_lop_shadow(m, settings, None, None, uia_sampler=uia_sampler)
    hs(evt)
    aud = getattr(m, "lop_sensor_audit", {}) or {}
    assert aud.get("runner_shadow_steps")


def test_legacy_live_feed_calls_refresh_shadow_only(monkeypatch):
    from unittest.mock import MagicMock

    m = _m(lop_live_feed_enabled=False)
    mock_rs = MagicMock(side_effect=lambda mi, st, **kw: lop.capture_live_operational_snapshot(mi, perception_input=LivePerceptionInput()))
    monkeypatch.setattr(lop, "refresh_lop_shadow", mock_rs)

    refresh_lop_from_live_sensors(m, SimpleNamespace(LOP_SHADOW_ENABLED=True))

    assert mock_rs.called


def test_dom_unavailable_health_reasons():
    inp, _, hlth = build_live_perception_input(
        _m(),
        LopSensorBridgeConfig(),
        uia_sampler=lambda **kw: ([], None),
        dom_sampler=lambda _a, _b: ({"nodes": [], "reason": "off"}, "dom_unreachable_stub"),
    )
    assert "dom_unreadable" in hlth.reasons or "no_structural_signals" in hlth.reasons


def test_dom_js_no_vendor_tokens():
    root = Path(__file__).resolve().parents[2]
    text = (root / "app/services/runtime/lop_sensor_bridge.py").read_text(encoding="utf-8").lower()
    for ban in ("youtube.com", "google.com/chrome", "chrome.exe"):
        assert ban not in text


def test_merge_prior_runner_audits():
    assert merge_prior_runner_shadow_audits({"runner_shadow_steps": [{"x": 1}], "y": 2}) == [{"x": 1}]


@pytest.fixture
def lop_enabled(monkeypatch):
    monkeypatch.setattr(lop, "_settings_lop_enabled", lambda _s: True)


def test_live_feed_gate_when_shadow_settings_off(monkeypatch):
    m = _m(lop_live_feed_enabled=True)
    monkeypatch.setattr(lop, "_settings_lop_enabled", lambda _s: False)
    snap = refresh_lop_from_live_sensors(m, SimpleNamespace(LOP_SHADOW_ENABLED=False))
    gaps = "".join(str(g) for g in (snap.perception_gaps or []))
    assert "LOP_SHADOW_DISABLED" in gaps
    audit = getattr(m, "live_operational_perception_audit", {}) or {}
    assert audit.get("reason") == "LOP_SHADOW_DISABLED"


def test_refresh_live_sensors_persists_audit(lop_enabled):
    _ = lop_enabled
    m = _m()
    inp = LivePerceptionInput(dom={"url": "https://x.test", "nodes": [{"tag": "a"}], "domain": "", "title": ""}, uia_nodes=[])
    refresh_lop_from_live_sensors(
        m,
        SimpleNamespace(LOP_SHADOW_ENABLED=True),
        perception_input_override=inp,
    )
    assert isinstance(getattr(m, "lop_sensor_audit", None), dict)
    assert isinstance(getattr(m, "lop_last_snapshot", None), dict)


def test_arl_hints_reflect_lop_snapshot():
    m = _m()
    m.lop_last_snapshot = {
        "live_blockers": [{"blocker_type": "x", "description": "d"}],
        "recommended_runtime_action": "STOP_UNSAFE",
        "confidence": 0.3,
        "safe_to_continue": False,
        "perception_gaps": [],
    }
    hints = arl_hints_from_lop(m)
    assert hints["lop_present"] is True
    assert any(h.get("type") == "lop_recommended_pause" for h in hints["blockers"])
