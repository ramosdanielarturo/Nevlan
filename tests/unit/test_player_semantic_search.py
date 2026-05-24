"""Tests para player handler semántico de búsqueda en sites conocidos.

Sprint 2026-05-03: el player debe ejecutar "Buscar en YouTube" como
una intención semántica (paste + Enter + validar URL de resultados),
no como reproducción de microeventos.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import (
    ActionStrategy, EventType, KeyboardAction, Mission, MouseAction,
    RawEvent, WindowContext,
)
from app.services.missions.compiler import MissionCompiler


def _key(ch: str, ts: datetime, seq: int,
         process: str = "chrome.exe") -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=[ch], modifiers=[]),
        window_context=WindowContext(process_name=process),
        timestamp=ts,
        sequence_id=seq,
    )


class TestCompilerEnrichesSubmitSearchWithSite:
    """El compiler debe marcar ``search_site`` en SET_FIELD_VALUE+Enter
    cuando el campo está en YouTube/Google. La descripción humana
    también debe reflejarlo: 'Buscar en YouTube ...'.
    """

    def test_youtube_search_described_as_buscar_en_youtube(self):
        base = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)

        click_field = RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=900, y=120),
            window_context=WindowContext(
                title="YouTube", process_name="chrome.exe",
            ),
            timestamp=base,
            sequence_id=1,
            metadata={
                "uia": {"name": "Buscar", "control_type": "EditControl"},
                "web": {
                    "url": "https://www.youtube.com/",
                    "domain": "youtube.com",
                    "role": "searchbox",
                    "placeholder": "Buscar",
                    "tag": "input",
                    "input_value_at_capture": "",
                    "locators": [
                        {"kind": "role", "role": "searchbox", "name": "Buscar"},
                    ],
                },
            },
        )
        events: List[RawEvent] = [click_field]
        for i, ch in enumerate("hola mundo"):
            events.append(_key(ch, base + timedelta(milliseconds=100 + i * 30),
                               i + 2))
        events.append(RawEvent(
            event_type=EventType.KEYBOARD_KEY_PRESS,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=WindowContext(process_name="chrome.exe"),
            timestamp=base + timedelta(milliseconds=600),
            sequence_id=99,
        ))

        m = Mission(name="yt", raw_trace=events)
        MissionCompiler.compile_mission(m)

        descriptions = [
            (i.description or "")
            for i in m.interpreted_steps
        ]
        joined = " | ".join(descriptions).lower()
        assert "youtube" in joined, (
            f"La descripción debe mencionar YouTube. Pasos: {descriptions}"
        )
        # Y debe haber payload con search_site=youtube en el SET_FIELD_VALUE final:
        for c in m.compiled_execution_graph:
            pl = c.action_payload or {}
            if pl.get("submit_after"):
                assert pl.get("search_site") == "youtube", (
                    f"submit_after sin search_site=youtube: {pl}"
                )
                assert pl.get("search_site_label") == "YouTube"

    def test_google_search_described_as_buscar_en_google(self):
        base = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)

        click_field = RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=400, y=200),
            window_context=WindowContext(
                title="Google", process_name="chrome.exe",
            ),
            timestamp=base,
            sequence_id=1,
            metadata={
                "uia": {"name": "Buscar", "control_type": "EditControl"},
                "web": {
                    "url": "https://www.google.com/",
                    "domain": "google.com",
                    "role": "combobox",
                    "tag": "input",
                    "input_value_at_capture": "",
                    "locators": [
                        {"kind": "role", "role": "combobox", "name": "Buscar"},
                    ],
                },
            },
        )
        events: List[RawEvent] = [click_field]
        for i, ch in enumerate("python"):
            events.append(_key(ch, base + timedelta(milliseconds=100 + i * 30),
                               i + 2))
        events.append(RawEvent(
            event_type=EventType.KEYBOARD_KEY_PRESS,
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
            window_context=WindowContext(process_name="chrome.exe"),
            timestamp=base + timedelta(milliseconds=600),
            sequence_id=99,
        ))

        m = Mission(name="g", raw_trace=events)
        MissionCompiler.compile_mission(m)
        descriptions = [(i.description or "") for i in m.interpreted_steps]
        joined = " | ".join(descriptions).lower()
        assert "google" in joined or "buscar en" in joined


class TestValidateSearchCompletedHelper:
    """Test del helper ``_validate_search_completed`` del player."""

    def _player(self):
        from app.services.missions.player import MissionPlayer
        m = Mission(name="empty", raw_trace=[])
        return MissionPlayer(m)

    def test_no_site_returns_true_without_browser(self):
        """Sin ``search_site`` en payload, no hay nada que validar."""
        p = self._player()
        assert p._validate_search_completed({})

    def test_youtube_results_url_validates(self):
        """page.url contiene /results → True rápidamente."""
        p = self._player()
        fake_page = MagicMock()
        fake_page.url = "https://www.youtube.com/results?search_query=hola"
        with patch(
            "app.skills.tools.web._get_page", return_value=fake_page,
        ):
            ok = p._validate_search_completed(
                {"search_site": "youtube"}, timeout_ms=500,
            )
        assert ok is True

    def test_youtube_no_results_url_eventually_returns_false(self):
        """page.url sigue en /, no contiene /results → False tras timeout."""
        p = self._player()
        fake_page = MagicMock()
        fake_page.url = "https://www.youtube.com/"
        with patch(
            "app.skills.tools.web._get_page", return_value=fake_page,
        ):
            ok = p._validate_search_completed(
                {"search_site": "youtube"}, timeout_ms=400,
            )
        assert ok is False

    def test_google_results_url_validates(self):
        p = self._player()
        fake_page = MagicMock()
        fake_page.url = "https://www.google.com/search?q=python"
        with patch(
            "app.skills.tools.web._get_page", return_value=fake_page,
        ):
            ok = p._validate_search_completed(
                {"search_site": "google"}, timeout_ms=500,
            )
        assert ok is True

    def test_no_browser_returns_false_silently(self):
        """Si no hay browser CDP disponible, devuelve False sin lanzar."""
        p = self._player()
        with patch(
            "app.skills.tools.web._get_page", side_effect=Exception("nope"),
        ):
            ok = p._validate_search_completed(
                {"search_site": "youtube"}, timeout_ms=200,
            )
        assert ok is False
