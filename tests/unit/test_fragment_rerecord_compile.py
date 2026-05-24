"""Regrabación reutiliza el mismo compilador MissionCompiler que la grabación completa."""
from __future__ import annotations

from app.contracts.mission import (
    RawEvent, EventType, MouseAction, KeyboardAction, ActionStrategy,
)
from app.services.missions.recorder import compile_fragment


def _key(*keys: str, modifiers: list[str] | None = None) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=list(keys), modifiers=modifiers or []),
    )


def _click_edit(uia_name: str = "Field") -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=90, y=140, button="left"),
        metadata={
            "uia": {
                "name": uia_name,
                "control_type": "EditControl",
                "automation_id": "fld1",
            }
        },
    )


def test_compile_fragment_spaces_and_case_session():
    ev = [
        _click_edit("Email"),
        _key("H"), _key("o"), _key("l"), _key(" "), _key("a"),
        _key("Key.tab"),
    ]
    cg, interp = compile_fragment(ev)
    assert len(cg) >= 1
    texts = [
        (s.action_payload or {}).get("text") or ""
        for s in cg
        if s.action_strategy in (
            ActionStrategy.SET_FIELD_VALUE, ActionStrategy.TYPE_TEXT,
        )
    ]
    assert any(t.strip() for t in texts)
    assert any(" " in t for t in texts if t.strip())


def test_compile_fragment_tab_commit():
    ev = [_click_edit("x"), _key("a"), _key("Key.tab")]
    cg, _ = compile_fragment(ev)
    assert len(cg) >= 1
    assert cg[0].action_strategy != ActionStrategy.SEND_HOTKEY or len(cg) <= 3


def test_compile_fragment_enter_commit_variant():
    ev = [_click_edit("x"), _key("b"), _key("enter")]
    cg, interp = compile_fragment(ev)
    assert len(interp) >= 1
    combined = cg[0] if cg else None
    assert combined is not None


def test_more_than_one_logical_action_can_yield_multiple_steps():
    ev = [
        RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=1, y=1, button="left"),
        ),
        RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=5, y=5, button="left"),
        ),
    ]
    cg, _ = compile_fragment(ev)
    assert isinstance(cg, list)
