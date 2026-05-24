"""Sprint Nevlan: firma, estrategia premium, grupos y metadatos interpretados."""
from __future__ import annotations

from app.contracts.mission import (
    Mission, RawEvent, EventType, MouseAction, TargetResolutionStrategy,
    ActionStrategy,
)
from app.services.missions.compiler import MissionCompiler


def _click_rich() -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=320, y=240, button="left"),
        metadata={
            "uia": {
                "name": "Buscar",
                "control_type": "ButtonControl",
                "automation_id": "searchBtn",
                "bbox": {"left": 310, "top": 230, "width": 40, "height": 24},
            },
            "anchor_bbox": {"left": 310, "top": 230, "width": 40, "height": 24},
            "click_offset": {"dx": 4, "dy": 2},
            "rel_win": {"fx": 0.5, "fy": 0.42},
            "snapshot_mid": "C:/dummy/mid.png",
            "target_signature": {"confidence": {"target_quality": "green"}},
        },
        snapshot_ref="C:/dummy/small.png",
    )


def test_coordinate_icon_strategy_when_bundle_complete():
    m = Mission(name="t_intel")
    m.raw_trace = [_click_rich()]
    MissionCompiler.compile_mission(m)
    step = m.compiled_execution_graph[0]
    assert (
        step.target_resolution_strategy == TargetResolutionStrategy.COORDINATE_ICON_VALIDATED_CLICK
    )
    sig = getattr(step.target_context, "signature", None)
    assert isinstance(sig, dict)


def test_interpreted_step_has_quality_and_score():
    m = Mission(name="t_intel2")
    m.raw_trace = [_click_rich()]
    MissionCompiler.compile_mission(m)
    i0 = m.interpreted_steps[0]
    assert getattr(i0, "quality_level", None) in ("green", "yellow", "red")
    pct = getattr(i0, "confidence_score_pct", None)
    assert pct is None or 0 <= float(pct) <= 100


def test_builds_action_groups():
    from app.contracts.mission import RawEvent, EventType, KeyboardAction

    clicks = [_click_rich()]
    clicks.append(
        RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=322, y=248, button="left"),
            metadata={
                "uia": {"name": "OK", "control_type": "ButtonControl"},
                "target_signature": {},
            },
        )
    )
    clicks.append(
        RawEvent(
            event_type=EventType.KEYBOARD_KEY_PRESS,
            keyboard_action=KeyboardAction(keys=["enter"]),
            metadata={},
        )
    )
    m = Mission(name="grp")
    m.raw_trace = clicks
    MissionCompiler.compile_mission(m)
    groups = getattr(m, "action_groups", None)
    assert groups and len(groups) >= 1
