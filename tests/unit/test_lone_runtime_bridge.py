"""LONE Runtime Bridge — percepción LOP viva + ActionRunner."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence, Tuple
from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import (
    LiveOperationalBlocker,
    LiveOperationalEntity,
    LiveOperationalEntityKind,
    LiveOperationalPerceptionSnapshot,
    LiveOperationalRuntimeAction,
    Mission,
    OperationalCollection,
    OperationalCollectionType,
    OperationalEntity,
    OperationalEntityType,
    OperationalNavigationAction,
)
from app.core.config import Settings
from app.services.missions.action_runner import ActionRunner, StrategyResult
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime import live_operational_navigation_engine as lone
from app.services.runtime import lone_runtime_bridge as bridge
from app.services.runtime import universal_collection_entity_engine as uces


def _nodes(names: list, *, role: str = "listitemcontrol", parent: str = "List") -> list:
    return [
        {
            "name": n,
            "control_type": role,
            "parent_chain": [parent, "Window"],
            "children": [],
        }
        for n in names
    ]


def _collection(names: list) -> OperationalCollection:
    nodes = _nodes(names)
    colls = uces.detect_operational_collections(uia_hierarchy=nodes)
    assert colls
    c = colls[0]
    uces.extract_entities_from_collection(c, nodes)
    return c


def _entity(name: str) -> OperationalEntity:
    return OperationalEntity(
        entity_type=OperationalEntityType.RESULT_ITEM,
        display_name=name,
        normalized_name=uces.normalize_entity_name(name),
    )


def _mission(**kw) -> Mission:
    base = {
        "name": "m_lone_bridge",
        "lop_shadow_mode": True,
        "lop_live_feed_enabled": True,
    }
    base.update(kw)
    return Mission(**base)


def _lop_snap(**kw) -> LiveOperationalPerceptionSnapshot:
    defaults = dict(
        active_app="app.exe",
        active_window="Results",
        confidence=0.82,
        safe_to_continue=True,
        recommended_runtime_action=LiveOperationalRuntimeAction.CONTINUE,
    )
    defaults.update(kw)
    return LiveOperationalPerceptionSnapshot(**defaults)


# 1. observe_fn usa LOP snapshot
def test_observe_fn_uses_lop_snapshot():
    names = ["Alpha", "Beta"]
    ents = [
        LiveOperationalEntity(
            kind=LiveOperationalEntityKind.RESULT_ITEM,
            label_guess=n,
            roles=["listitem"],
        )
        for n in names
    ]
    snap = _lop_snap(visible_entities=ents, actionable_entities=ents)
    m = _mission(lop_last_snapshot=snap.model_dump(mode="json"))
    shared: Dict[str, Any] = {}

    with patch.object(bridge, "_refresh_lop_snapshot", return_value=snap):
        observe = bridge.build_lone_observe_fn_from_lop(m, bridge_state=shared)
        colls, uia = observe({})

    assert shared.get("used_lop") is True
    assert len(uia) >= 2
    assert any(n["name"] == "Alpha" for n in uia)
    assert colls or uia  # UCES may or may not form collection from sparse nodes


# 2. execute_fn traduce scroll_down
def test_execute_fn_translates_scroll_down():
    runner = MagicMock(spec=ActionRunner)
    runner.run_strategy.return_value = StrategyResult(
        ok=True, strategy="scroll_page:wheel", message="ok",
    )
    shared: Dict[str, Any] = {}
    execute = bridge.build_lone_execute_fn_from_action_runner(
        action_runner=runner,
        state=StateSnapshot(),
        bridge_state=shared,
    )
    ctx: Dict[str, Any] = {"scroll_y": 0, "scroll_step": 320}
    out = execute(OperationalNavigationAction.SCROLL_DOWN, ctx)

    runner.run_strategy.assert_called_once()
    assert runner.run_strategy.call_args[0][0] == "scroll_page:wheel"
    assert out.get("scroll_y", 0) > 0
    assert shared.get("used_action_runner") is True


# 3. execute_fn traduce wait_dynamic_content
def test_execute_fn_translates_wait_dynamic_content():
    runner = MagicMock(spec=ActionRunner)
    execute = bridge.build_lone_execute_fn_from_action_runner(
        action_runner=runner,
        settings=SimpleNamespace(LONE_ACTION_SETTLE_MS=10),
    )
    ctx: Dict[str, Any] = {}
    out = execute(OperationalNavigationAction.WAIT_DYNAMIC_CONTENT, ctx)
    assert out.get("last_action_ok") is True
    runner.run_strategy.assert_not_called()


# 4. unsafe modal bloquea
def test_unsafe_modal_blocks():
    snap = _lop_snap(
        modal_detected=True,
        safe_to_continue=False,
        live_blockers=[
            LiveOperationalBlocker(blocker_type="modal_blocking", severity="high"),
        ],
    )
    unsafe, reason = bridge.assess_lop_navigation_safety(snap)
    assert unsafe
    assert "modal" in reason or "blocker" in reason


# 5. auth screen bloquea
def test_auth_screen_blocks():
    snap = _lop_snap(
        operational_state_guess="auth_required_login",
        active_window="Sign in - Password",
        live_blockers=[
            LiveOperationalBlocker(blocker_type="auth_required", severity="critical"),
        ],
    )
    unsafe, reason = bridge.assess_lop_navigation_safety(snap)
    assert unsafe
    assert "auth" in reason or "blocker" in reason


# 6. successful loop finds entity after scroll
def test_successful_loop_finds_entity_after_scroll():
    all_names = [f"Item {i:02d}" for i in range(20)]
    target = "Item 08"
    visible = all_names[:6]

    def fake_refresh(mission, settings=None):
        scroll_y = int(getattr(mission, "_scroll", 0))
        start = min(scroll_y // 320, len(all_names) - 6)
        window = all_names[start : start + 6]
        ents = [
            LiveOperationalEntity(
                kind=LiveOperationalEntityKind.RESULT_ITEM,
                label_guess=n,
                roles=["listitem"],
            )
            for n in window
        ]
        return _lop_snap(visible_entities=ents, actionable_entities=ents)

    m = _mission()
    m._scroll = 0  # type: ignore[attr-defined]

    def observe(ctx):
        sy = int(ctx.get("scroll_y") or 0)
        m._scroll = sy  # type: ignore[attr-defined]
        snap = fake_refresh(m)
        uia = bridge.lop_snapshot_to_uia_flat(snap)
        ctx["lop_snapshot"] = snap
        ctx["viewport_signature"] = f"vp-{sy}"
        colls = uces.detect_operational_collections(capture_bundle={"uia_flat": uia})
        if not colls and uia:
            colls = [_collection([n["name"] for n in uia[:6]])]
        return colls, uia

    def sim_scroll(action, ctx):
        step_px = int(ctx.get("scroll_step") or 320)
        sy = int(ctx.get("scroll_y") or 0)
        if action == OperationalNavigationAction.SCROLL_DOWN:
            ctx["scroll_y"] = sy + step_px
        return ctx

    result = lone.run_operational_navigation_loop(
        target_entity=_entity(target),
        target_collection=_collection(all_names[:6]),
        observe_fn=observe,
        execute_fn=sim_scroll,
        max_depth=10,
        context={"scroll_y": 0, "scroll_step": 320},
    )
    assert result.success
    assert result.entity is not None


# 7. no progress prevents infinite loop
def test_no_progress_prevents_infinite_loop():
    names = ["Only A", "Only B"]

    def observe(ctx):
        uia = _nodes(names)
        colls = [_collection(names)]
        ctx["viewport_signature"] = "same"
        return colls, uia

    runner = MagicMock(spec=ActionRunner)
    runner.run_strategy.return_value = StrategyResult(ok=True, strategy="scroll_page:wheel")

    result = lone.run_operational_navigation_loop(
        target_entity=_entity("Missing Z"),
        target_collection=_collection(names),
        observe_fn=observe,
        execute_fn=bridge.build_lone_execute_fn_from_action_runner(
            action_runner=runner,
            settings=SimpleNamespace(LONE_ACTION_SETTLE_MS=5),
        ),
        max_depth=4,
        context={"scroll_y": 0, "no_viewport_progress": 0},
    )
    assert not result.success
    assert result.depth_reached <= 4


# 8. blocker after action stops
def test_blocker_after_action_stops_validation():
    ctx = {
        "lop_snapshot": _lop_snap(
            live_blockers=[LiveOperationalBlocker(blocker_type="auth_required")],
            safe_to_continue=False,
        ),
        "viewport_signature": "a",
    }
    ok, reason = bridge.validate_navigation_after_action(ctx)
    assert not ok
    assert "auth" in reason or "blocker" in reason


# 9. max_depth stops safely
def test_max_depth_stops_safely():
    cfg = bridge.LoneRuntimeBridgeConfig(enabled=True, max_depth=2, settle_ms=5)
    m = _mission()

    with patch.object(bridge, "build_lone_observe_fn_from_lop") as mock_obs:
        with patch.object(bridge, "build_lone_execute_fn_from_action_runner") as mock_exec:
            mock_obs.return_value = lambda ctx: ([], [])
            mock_exec.return_value = lambda a, c: c
            with patch.object(bridge, "_refresh_lop_snapshot", return_value=_lop_snap()):
                result = bridge.run_lone_recovery_with_live_perception(
                    MissionStep(id="s1", kind="click", params={}),
                    StateSnapshot(),
                    m,
                    target_entity=_entity("X"),
                    config=cfg,
                )
    assert not result.success
    assert result.depth_reached <= 2


# 10. LOP unavailable falls back safely
def test_lop_unavailable_falls_back_when_required():
    snap = None
    unsafe, reason = bridge.assess_lop_navigation_safety(
        snap, require_lop_safe=True,
    )
    assert unsafe
    assert reason == "lop_snapshot_missing"


def test_is_lone_live_bridge_unavailable_without_flag():
    m = _mission(lop_live_feed_enabled=True)
    assert not bridge.is_lone_live_bridge_available(
        m, settings=SimpleNamespace(LONE_LIVE_BRIDGE_ENABLED=False),
    )


# 11. action_runner failure records audit
def test_action_runner_failure_records_audit():
    m = _mission()
    runner = MagicMock(spec=ActionRunner)
    runner.run_strategy.return_value = StrategyResult(
        ok=False, strategy="scroll_page:wheel", message="pyautogui no disponible",
    )
    execute = bridge.build_lone_execute_fn_from_action_runner(
        action_runner=runner,
        state=StateSnapshot(),
        settings=SimpleNamespace(LONE_ACTION_SETTLE_MS=5),
    )
    ctx = execute(OperationalNavigationAction.SCROLL_DOWN, {})
    assert ctx.get("last_action_ok") is False

    result = bridge.LoneLiveRecoveryResult(
        success=False,
        actions_executed=["scroll_down"],
        used_action_runner=True,
    )
    bridge.persist_lone_runtime_audit(m, result=result)
    assert m.operational_navigation_audit
    last = m.operational_navigation_audit[-1]
    assert last.get("used_action_runner") is True


# 12. ERL calls LONE live bridge when entity not visible
def test_erl_calls_lone_live_bridge_when_available():
    from app.services.runtime import entity_runtime_resolution as erl

    coll = _collection(["Alpha", "Beta", "Gamma"])
    hidden = _entity("Hidden Target")
    step = MissionStep(
        id="st1",
        kind="click",
        params={
            "operational_collection": coll.model_dump(mode="json"),
            "collection_entity": {"display_name": hidden.display_name},
            "operational_entity": hidden.model_dump(mode="json"),
        },
    )
    state = StateSnapshot()
    mission = _mission()

    with patch.object(bridge, "is_lone_live_bridge_available", return_value=True):
        with patch.object(bridge, "run_lone_recovery_with_live_perception") as mock_live:
            mock_live.return_value = bridge.LoneLiveRecoveryResult(
                success=False, stopped_reason="test",
            )
            with patch.object(
                uces, "try_collection_entity_first_runtime",
            ) as mock_uces:
                mock_uces.return_value = SimpleNamespace(
                    matched=False, entity=None, confidence=0.0, strategy="",
                )
                erl.try_entity_first_runtime(step, state, mission)

    mock_live.assert_called_once()


# 13. no coords primary
def test_translate_no_coords_primary():
    plan = bridge.translate_navigation_action_to_runtime(
        OperationalNavigationAction.SCROLL_DOWN, {},
    )
    assert plan.get("use_coords") is False
    assert plan.get("entity_first") is True
    assert "coord" not in str(plan.get("strategy", "")).lower()


# 14. legacy behavior unchanged when bridge disabled
def test_legacy_behavior_when_bridge_disabled():
    m = _mission(lop_live_feed_enabled=False)
    assert not bridge.is_lone_live_bridge_available(m)
    cfg = bridge.lone_bridge_config_from_settings(
        SimpleNamespace(LONE_LIVE_BRIDGE_ENABLED=False),
    )
    assert cfg.enabled is False


# 15. navigation memory records successful live route
def test_navigation_memory_records_successful_route(tmp_path):
    db = tmp_path / "nav.sqlite"
    store = lone.NavigationMemoryStore(db_path=db)
    store.record_recovery_path(
        "coll_sig",
        "target entity",
        ["scroll_down", "scroll_down"],
        success=True,
    )
    actions, rate = store.best_recovery_sequence("coll_sig", "target entity")
    assert "scroll_down" in actions
    assert rate >= 0.4


def test_lop_snapshot_to_uia_flat_dedupes():
    ent = LiveOperationalEntity(
        kind=LiveOperationalEntityKind.BUTTON,
        label_guess="Next page",
        roles=["button"],
    )
    snap = _lop_snap(visible_entities=[ent, ent], actionable_entities=[ent])
    nodes = bridge.lop_snapshot_to_uia_flat(snap)
    assert len(nodes) == 1
    assert nodes[0]["name"] == "Next page"


def test_paginate_entity_first_from_affordance():
    uia = _nodes(["Next page"], role="button")
    label = bridge._find_affordance_label(uia, bridge._PAGINATE_LABEL_MARKERS)
    assert label == "Next page"
