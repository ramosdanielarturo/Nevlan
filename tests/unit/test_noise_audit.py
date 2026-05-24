"""
Nevlan — Tests del audit_ignored_noise (PRD 2026-05-08c §F)
============================================================

Verifica las heurísticas de ruido humano contra escenarios sintéticos
controlables. Cada heurística se prueba en aislamiento usando mocks
con la misma forma que ``FieldSession`` (acepta ``getattr`` y
``as_dict``).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app.services.missions.noise_audit import (
    IGNORED_NOISE_KINDS,
    _detect_address_bar_clicks,
    _detect_noise_before_bookmark,
    _detect_typed_then_deleted,
    audit_ignored_noise,
)


def _mock_session(
    *,
    field_label: str = "",
    submitted: bool = False,
    final_text: str = "",
    site: str = None,
    events: int = 1,
    event_ids: List[str] = None,
) -> SimpleNamespace:
    """Mock con la API mínima que noise_audit consume (``as_dict()`` +
    ``raw_event_ids``)."""
    payload: Dict[str, Any] = {
        "field_label": field_label,
        "submitted": submitted,
        "final_text": final_text,
        "site": site,
        "events_consumed": events,
    }
    return SimpleNamespace(
        as_dict=lambda: dict(payload),
        raw_event_ids=list(event_ids or []),
    )


# ─── address_bar_click_no_submit ───────────────────────────────────────


def test_accidental_address_bar_click_is_ignored_noise() -> None:
    sessions = [_mock_session(
        field_label="browser_address_bar",
        submitted=False,
        final_text="yt.",
        events=4,
        event_ids=["e1", "e2"],
    )]
    findings = _detect_address_bar_clicks(sessions)
    assert len(findings) == 1
    assert findings[0]["kind"] == "address_bar_click_no_submit"
    assert findings[0]["raw_event_ids"] == ["e1", "e2"]


def test_address_bar_submitted_is_not_noise() -> None:
    sessions = [_mock_session(
        field_label="browser_address_bar",
        submitted=True,
        final_text="https://youtube.com",
    )]
    assert _detect_address_bar_clicks(sessions) == []


def test_non_address_bar_session_is_not_address_bar_noise() -> None:
    sessions = [_mock_session(
        field_label="youtube_search_input",
        submitted=False,
        final_text="canción",
    )]
    assert _detect_address_bar_clicks(sessions) == []


# ─── typed_then_deleted ────────────────────────────────────────────────


def test_typed_then_deleted_noise_does_not_create_step() -> None:
    sessions = [_mock_session(
        field_label="search_field",
        submitted=False,
        final_text="",  # texto borrado
        events=5,
        event_ids=["k1", "k2", "k3"],
    )]
    findings = _detect_typed_then_deleted(sessions)
    assert len(findings) == 1
    assert findings[0]["kind"] == "typed_then_deleted"


def test_typed_then_deleted_with_residual_text_is_not_noise() -> None:
    """Si quedó texto significativo, no es típico ruido."""
    sessions = [_mock_session(
        field_label="search_field",
        submitted=False,
        final_text="hola mundo",  # texto residual significativo
        events=8,
    )]
    assert _detect_typed_then_deleted(sessions) == []


def test_typed_then_deleted_too_few_events_is_not_noise() -> None:
    """Una sesión con 1 evento sin enviar no es típica
    secuencia tipear+borrar."""
    sessions = [_mock_session(
        field_label="search_field",
        submitted=False,
        final_text="",
        events=1,
    )]
    assert _detect_typed_then_deleted(sessions) == []


def test_typed_then_deleted_skips_address_bar() -> None:
    """No queremos doble-flag: address_bar tiene su propia heurística."""
    sessions = [_mock_session(
        field_label="browser_address_bar",
        submitted=False,
        final_text="",
        events=5,
    )]
    assert _detect_typed_then_deleted(sessions) == []


# ─── noise_before_bookmark ────────────────────────────────────────────


def test_noise_before_bookmark_does_not_break_open_url_youtube() -> None:
    """PRD §F: si la misión abrió una URL via favorito y antes hubo
    una sesión rota en barra de direcciones, eso queda como noise
    específico (más informativo que address_bar genérico)."""
    sessions = [_mock_session(
        field_label="browser_address_bar",
        submitted=False,
        final_text="yt",
        events=3,
        event_ids=["e1"],
    )]

    class _M:
        semantic_execution_plan = {
            "steps": [
                {"type": "open_app", "params": {}},
                {
                    "type": "open_url",
                    "params": {"url": "https://youtube.com", "via": "bookmark"},
                },
            ]
        }

    findings = _detect_noise_before_bookmark(sessions, _M())
    assert len(findings) == 1
    assert findings[0]["kind"] == "noise_before_bookmark"


def test_noise_before_bookmark_skipped_if_no_bookmark_step() -> None:
    sessions = [_mock_session(
        field_label="browser_address_bar",
        submitted=False,
        final_text="yt",
        events=3,
    )]

    class _M:
        semantic_execution_plan = {
            "steps": [
                {"type": "open_app", "params": {}},
                {
                    "type": "open_url",
                    "params": {"url": "https://youtube.com", "via": "typed"},
                },
            ]
        }

    assert _detect_noise_before_bookmark(sessions, _M()) == []


# ─── audit_ignored_noise integración ──────────────────────────────────


def test_audit_returns_empty_for_mission_without_raw_trace() -> None:
    class _M:
        raw_trace = None
        semantic_execution_plan = {}

    assert audit_ignored_noise(_M()) == []


def test_audit_swallows_field_session_errors_safely(monkeypatch) -> None:
    """Si build_field_sessions crashea, devolvemos [] silenciosamente."""
    import app.services.missions.noise_audit as na
    from types import SimpleNamespace

    def _exploder(_):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "app.services.missions.field_session_manager.build_field_sessions",
        _exploder,
    )

    class _M:
        raw_trace = [SimpleNamespace()]
        semantic_execution_plan = {}

    assert audit_ignored_noise(_M()) == []


def test_known_noise_kinds_are_documented() -> None:
    assert "address_bar_click_no_submit" in IGNORED_NOISE_KINDS
    assert "typed_then_deleted" in IGNORED_NOISE_KINDS
    assert "noise_before_bookmark" in IGNORED_NOISE_KINDS
