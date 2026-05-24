"""Panel de regrabación: líneas interpretadas sin microhistoria de teclas."""

from __future__ import annotations

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions.rerecord_display import build_rerecord_display


def _kp(keys: list[str], modifiers: list[str] | None = None) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=list(keys), modifiers=list(modifiers or [])),
    )


def test_click_human_line_contains_process():
    ev = RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=10, y=20, button="left"),
        window_context=WindowContext(title="", process_name="chrome.exe"),
        metadata={"uia": {"name": "Buscar"}},
    )
    human, _raw = build_rerecord_display([ev], expert=False)
    assert "Chrome" in human
    assert "clic" in human.lower()


def test_ctrl_save_hotkey_human():
    ev = RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=["s"], modifiers=["ctrl"]),
    )
    human, _ = build_rerecord_display([ev], expert=False)
    assert "Guardar" in human
    assert "Ctrl" in human or "Ctrl+S" in human


def test_tab_triple_human():
    evs = [
        _kp(["Key.tab"]),
        _kp(["Key.tab"]),
        _kp(["Key.tab"]),
    ]
    human, _ = build_rerecord_display(evs, expert=False)
    assert "TAB" in human.upper()
    assert "×" in human


def test_expert_includes_raw_block():
    evs = [_kp(["Key.a"]), _kp(["Key.b"])]
    _h, raw = build_rerecord_display(evs, expert=True)
    assert raw.strip()


def test_keyboard_type_text_captured_line():
    ev = RawEvent(
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        metadata={"final_text": "hola mundo"},
    )
    human, _ = build_rerecord_display([ev], expert=False)
    assert "Texto capturado" in human


def test_expert_toggle_off_limits_raw_visibility():
    evs = [_kp(["Key.down"])]
    _h_emptyish, raw_on = build_rerecord_display(evs, expert=True)
    _, raw_off = build_rerecord_display(evs, expert=False)
    assert raw_on.strip()
    assert raw_off.strip() == ""
