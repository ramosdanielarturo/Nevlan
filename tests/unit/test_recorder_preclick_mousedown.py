"""FASE 1 — recorder MouseListener: pending mousedown → before_anchor."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.recorder import MouseListener
from app.services.missions.recorder_capture_contract import (
    ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
    CAPTURE_PHASE_PRE_CLICK,
    TARGET_PRECAPTURE_MISSING,
    TargetAnchor,
    attach_precapture_diagnostics,
)


class _StopAfterPrecapture(Exception):
    pass


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


def test_process_click_consumes_pending_and_records_precapture() -> None:
    listener = _listener()
    pre = TargetAnchor(
        hwnd=5, title="Pre", process="app.exe",
        phase=CAPTURE_PHASE_PRE_CLICK, source=ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
    )
    pending = {
        "x": 10, "y": 20, "button": "left", "t_down": 1.0,
        "window_ctx": MagicMock(hwnd=5, title="Pre", process_name="app.exe"),
        "before_anchor": pre,
    }
    listener._capture_uia = lambda x, y: {}  # type: ignore[method-assign]
    attach_calls: list = []

    def _track_attach(meta, **kw):
        attach_calls.append(kw)
        raise _StopAfterPrecapture()

    with patch("app.services.missions.recorder._is_own_hwnd", return_value=False), patch(
        "app.services.missions.recorder._get_window_context_at_point",
        return_value=pending["window_ctx"],
    ), patch(
        "app.services.missions.recorder.attach_precapture_diagnostics",
        side_effect=_track_attach,
    ):
        try:
            listener._process_click(10, 20, "left", None, None, pending)
        except _StopAfterPrecapture:
            pass
    assert len(attach_calls) == 1
    assert attach_calls[0]["before_anchor"] is pre
    assert attach_calls[0]["had_pending"] is True
    assert attach_calls[0]["missing_reason"] == ""


def test_process_click_without_pending_marks_target_precapture_missing() -> None:
    meta: dict = {}
    attach_precapture_diagnostics(
        meta, before_anchor=None, had_pending=False,
        missing_reason=TARGET_PRECAPTURE_MISSING,
    )
    diag = meta["precapture_diagnostics"]
    assert diag["target_precapture_weak"] is True
    assert diag["missing_reason"] == TARGET_PRECAPTURE_MISSING
    assert diag["preclick_captured"] is False
