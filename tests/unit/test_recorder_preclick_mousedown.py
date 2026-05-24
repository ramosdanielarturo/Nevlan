"""FASE 1 — recorder MouseListener: pending mousedown → before_anchor."""
from __future__ import annotations

from unittest.mock import MagicMock

from app.services.missions.recorder import MouseListener
from app.services.missions.recorder_capture_contract import (
    ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
    CAPTURE_PHASE_PRE_CLICK,
    TargetAnchor,
)


def _listener() -> MouseListener:
    return MouseListener(callback=MagicMock(), mission_id="m-test")


def test_pending_click_stores_preclick_anchor_on_mousedown_path() -> None:
    listener = _listener()
    pre = TargetAnchor(
        hwnd=1, title="App", process="a.exe",
        phase=CAPTURE_PHASE_PRE_CLICK, source=ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
    )
    wctx = MagicMock()
    with listener._pending_click_lock:
        listener._pending_click = {
            "x": 10, "y": 20, "button": "left", "t_down": 1.0,
            "window_ctx": wctx, "before_anchor": pre,
        }
    got = listener._pending_click
    assert got is not None
    assert got["before_anchor"].phase == CAPTURE_PHASE_PRE_CLICK


def test_release_path_consumes_pending_click_slot() -> None:
    listener = _listener()
    pre = TargetAnchor(
        hwnd=5, title="Pre", process="app.exe",
        phase=CAPTURE_PHASE_PRE_CLICK, source=ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
    )
    with listener._pending_click_lock:
        listener._pending_click = {
            "x": 10, "y": 20, "button": "left", "t_down": 1.0,
            "window_ctx": MagicMock(), "before_anchor": pre,
        }
    pending_click = None
    with listener._pending_click_lock:
        pending_click = listener._pending_click
        listener._pending_click = None
    assert pending_click is not None
    assert pending_click["before_anchor"] is pre
    assert listener._pending_click is None
