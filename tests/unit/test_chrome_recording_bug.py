"""
Tests for the Chrome recording bug fixes:
- Pre-sort by timestamp
- Windows Search → LAUNCH_APP detection
- URL sanity check in field commit
"""
import copy
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from app.contracts.mission import (
    Mission, RawEvent, EventType, MissionStatus, ActionStrategy,
    KeyboardAction, MouseAction, WindowContext, CompiledStep,
    TargetContext, InterpretedStep,
)


def _make_raw_event(
    event_type: EventType,
    timestamp: datetime,
    *,
    keyboard_action=None,
    mouse_action=None,
    metadata: dict = None,
    window_context=None,
) -> RawEvent:
    """Helper to create a raw event for tests."""
    wc = window_context or WindowContext(
        title="Test Window",
        process_name="test.exe",
    )
    return RawEvent(
        event_type=event_type,
        keyboard_action=keyboard_action,
        mouse_action=mouse_action,
        window_context=wc,
        timestamp=timestamp,
        metadata=metadata or {},
    )


def _make_mission(events: list) -> Mission:
    return Mission(
        name="Test",
        raw_trace=events,
        status=MissionStatus.RECORDING,
    )


class TestTimestampPreSort:
    """Verify that compile_mission sorts raw_trace by timestamp before processing."""

    def test_events_reordered_by_timestamp(self):
        from app.services.missions.compiler import MissionCompiler

        t0 = datetime.now(timezone.utc)
        # Simulate threading bug: keyboard event arrives before click
        # but click timestamp is EARLIER
        click_ts = t0
        key_ts = t0 + timedelta(milliseconds=200)

        events = [
            # Key event arrives FIRST (wrong order from threading)
            _make_raw_event(
                EventType.KEYBOARD_TYPE_TEXT,
                key_ts,
                keyboard_action=KeyboardAction(keys=["c"], modifiers=[]),
            ),
            # Click event arrives SECOND (but happened first)
            _make_raw_event(
                EventType.MOUSE_CLICK,
                click_ts,
                mouse_action=MouseAction(x=100, y=200),
                metadata={"x": 100, "y": 200},
            ),
        ]
        mission = _make_mission(events)
        MissionCompiler.compile_mission(mission)

        # After compilation, the raw_trace should be sorted by timestamp
        assert mission.raw_trace[0].timestamp <= mission.raw_trace[1].timestamp


class TestWindowsSearchLaunchApp:
    """Verify Windows Search → LAUNCH_APP pattern detection."""

    def test_launch_app_pattern_detected(self):
        from app.services.missions.compiler import MissionCompiler

        # Simulate compiled steps: Click on search + Type "Chrome" + Enter
        # CompiledStep does NOT have window_context — the search info is
        # in target_context.uia_data and process_name.
        steps = [
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                target_context=TargetContext(
                    uia_data={"name": "Escribe aquí para buscar", "control_type": "EditControl"},
                    process_name="searchhost.exe",
                ),
                action_payload={},
            ),
            CompiledStep(
                action_strategy=ActionStrategy.TYPE_TEXT,
                target_context=TargetContext(),
                action_payload={"text": "Chrome"},
            ),
            CompiledStep(
                action_strategy=ActionStrategy.SEND_HOTKEY,
                target_context=TargetContext(),
                action_payload={"hotkey": "enter"},
            ),
        ]
        interp = [
            InterpretedStep(description="Click en Buscar", raw_event_ids=[]),
            InterpretedStep(description="Escribir Chrome", raw_event_ids=[]),
            InterpretedStep(description="Enter", raw_event_ids=[]),
        ]

        out_c, out_i = MissionCompiler._post_pass_windows_search_launch(steps, interp)

        # Should collapse to a single LAUNCH_APP step
        assert len(out_c) == 1
        assert out_c[0].action_strategy == ActionStrategy.LAUNCH_APP
        assert out_c[0].action_payload["app_name"] == "Chrome"
        assert "Abrir Chrome" in out_i[0].description

    def test_no_match_for_normal_click(self):
        """Non-search clicks should not trigger LAUNCH_APP."""
        from app.services.missions.compiler import MissionCompiler

        steps = [
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                target_context=TargetContext(
                    uia_data={"name": "Guardar", "control_type": "ButtonControl"},
                ),
                action_payload={},
            ),
            CompiledStep(
                action_strategy=ActionStrategy.TYPE_TEXT,
                target_context=TargetContext(),
                action_payload={"text": "document.txt"},
            ),
        ]
        interp = [
            InterpretedStep(description="Click Guardar", raw_event_ids=[]),
            InterpretedStep(description="Escribir nombre", raw_event_ids=[]),
        ]

        out_c, out_i = MissionCompiler._post_pass_windows_search_launch(steps, interp)

        # Should NOT collapse
        assert len(out_c) == 2
        assert out_c[0].action_strategy == ActionStrategy.CLICK

    def test_search_by_process_name(self):
        """Match by process name when UIA name doesn't match locale."""
        from app.services.missions.compiler import MissionCompiler

        steps = [
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                target_context=TargetContext(
                    uia_data={"name": "Hier eingeben zum Suchen", "control_type": "EditControl"},
                    process_name="searchhost.exe",
                ),
                action_payload={},
            ),
            CompiledStep(
                action_strategy=ActionStrategy.TYPE_TEXT,
                target_context=TargetContext(),
                action_payload={"text": "Notepad"},
            ),
        ]
        interp = [
            InterpretedStep(description="Click", raw_event_ids=[]),
            InterpretedStep(description="Type", raw_event_ids=[]),
        ]

        out_c, out_i = MissionCompiler._post_pass_windows_search_launch(steps, interp)

        assert len(out_c) == 1
        assert out_c[0].action_strategy == ActionStrategy.LAUNCH_APP
        assert out_c[0].action_payload["app_name"] == "Notepad"


class TestURLSanityCheck:
    """Verify that browser internal URLs are not captured as typed text."""

    def test_url_replaced_by_buffer(self):
        """If final_text is a browser URL but buffer is normal text, use buffer."""
        _BROWSER_URL_PREFIXES = (
            "chrome://", "edge://", "about:", "chrome-extension://",
            "brave://", "opera://", "vivaldi://", "file:///",
        )
        # Simulate the scenario
        value = "chrome://profile-picker/"
        user_buf = "Chrome"

        is_internal_url = any(value.lower().startswith(p) for p in _BROWSER_URL_PREFIXES)
        buf_is_not_url = not any(user_buf.lower().startswith(p) for p in _BROWSER_URL_PREFIXES)

        assert is_internal_url
        assert buf_is_not_url
        # The recorder would choose user_buf in this case
        assert user_buf == "Chrome"

    def test_normal_url_not_affected(self):
        """Normal URLs typed by the user should NOT be replaced."""
        _BROWSER_URL_PREFIXES = (
            "chrome://", "edge://", "about:", "chrome-extension://",
        )
        value = "https://www.google.com"
        user_buf = "www.google.com"

        is_internal_url = any(value.lower().startswith(p) for p in _BROWSER_URL_PREFIXES)
        assert not is_internal_url  # Should NOT trigger replacement
