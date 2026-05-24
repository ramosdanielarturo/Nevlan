"""
Nevlan — E2E Chrome + YouTube READY (PRD 2026-05-07c §H)
=========================================================

Caso real reportado el 2026-05-07. La grabación humana es:

    1.  Click barra Windows Search ("Escribe para buscar")
    2.  Type "Chrome"
    3.  Click icono Chrome
    4.  Click perfil "Daniel Chrome D Daniel Arturo Ramos"
    5.  Click "Nueva pestaña"
    6.  Click icono YouTube
    7.  Click barra YouTube ("Buscar")
    8.  Type "Devuélveme el amor de Luis Miguel"
    9.  Enter
    10. Scroll down

El plan semántico final esperado (autoridad ejecutiva) es:

    {
      "intent_status": "READY",
      "coords_used": false,
      "legacy_graph_ignored": true,
      "steps": [
        {"type": "open_app",        params={"name": "chrome", ...}},
        {"type": "select_profile",  params={"profile_name":
                                            "Daniel Arturo Ramos", ...}},
        {"type": "open_new_tab",    preferred="open_new_tab:hotkey_ctrl_t"},
        {"type": "open_url",        params={"url": "youtube.com"},
                                    preferred="open_url:hotkey_ctrl_l"},
        {"type": "search_content",  params={"query":
                                            "Devuélveme el amor de Luis Miguel",
                                            "provider": "youtube"},
                                    preferred="search_content:smart_route"},
        {"type": "scroll_results",  params={"direction": "down",
                                            "amount": 1,
                                            "context": "youtube_results"}},
      ],
    }

Y técnicamente:

    semantic_execution_plan.intent_status == "READY"
    semantic_execution_plan.coords_used == False
    semantic_execution_plan.legacy_graph_ignored == True
    semantic_execution_plan.steps[1].params.profile_name ==
        "Daniel Arturo Ramos"
    semantic_execution_plan.steps[4].params.query ==
        "Devuélveme el amor de Luis Miguel"
    legacy_compiled_graph_purpose == "legacy_debug_only"

NUNCA debe existir:

    - profile_name == ""
    - needs_user_label == True en el plan READY
    - query incompleta
    - mouse_scroll dentro del semantic_execution_plan
    - click/key_press legacy dentro del semantic_execution_plan
    - use_coords como estrategia primaria
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from app.contracts.mission import (
    EventType, KeyboardAction, Mission, MouseAction, RawEvent,
    WindowContext,
)
from app.services.missions.intent_collapse_engine import (
    CollapseStatus, apply_collapse_to_mission,
)
from app.services.missions.profile_resolver import reset_alias_store


_BASE = datetime(2026, 5, 7, 22, 30, 0, tzinfo=timezone.utc)


def _ts(ms: int) -> datetime:
    return _BASE + timedelta(milliseconds=ms)


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _activate(ms: int, *, title: str, proc: str, hwnd: int = 1) -> RawEvent:
    return RawEvent(
        event_type=EventType.WINDOW_ACTIVATE,
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        timestamp=_ts(ms),
        sequence_id=ms,
    )


def _click(x: int, y: int, ms: int, *, title: str, proc: str,
           uia: dict = None, web: dict = None,
           extra_meta: dict = None,
           hwnd: int = 1) -> RawEvent:
    meta = {}
    if uia:
        meta["uia"] = uia
    if web:
        meta["web"] = web
    if extra_meta:
        meta.update(extra_meta)
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=x, y=y),
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        timestamp=_ts(ms),
        sequence_id=ms,
        metadata=meta,
    )


def _type_text(text: str, ms: int, *, title: str, proc: str,
               user_typed_text: str = None,
               hwnd: int = 1) -> RawEvent:
    meta = {"final_text": text, "text": text}
    if user_typed_text is not None:
        meta["field_commit"] = {"user_typed_text": user_typed_text}
    return RawEvent(
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        keyboard_action=KeyboardAction(keys=list(text)),
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        timestamp=_ts(ms),
        sequence_id=ms,
        metadata=meta,
    )


def _enter(ms: int, *, title: str, proc: str, hwnd: int = 1) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=["enter"]),
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        timestamp=_ts(ms),
        sequence_id=ms,
    )


def _scroll(ms: int, *, title: str, proc: str, hwnd: int = 1,
            count: int = 1) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_SCROLL,
        mouse_action=MouseAction(x=400, y=400),
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        timestamp=_ts(ms),
        sequence_id=ms,
        metadata={"count": count, "direction": "down"},
    )


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    from app.core.config import settings
    var = tmp_path / "var"
    var.mkdir()
    monkeypatch.setattr(settings, "VAR_PATH", var)
    reset_alias_store()
    yield
    reset_alias_store()


def _build_chrome_youtube_trace() -> List[RawEvent]:
    """Trace EXACTO del PRD §H: 10 acciones humanas."""
    events: List[RawEvent] = []
    # ── 1. Click barra Windows Search ─────────────────────────────
    events.append(_click(
        200, 1050, ms=0,
        hwnd=1, title="Búsqueda", proc="searchhost.exe",
        uia={"name": "Escribe aquí para buscar",
             "control_type": "EditControl",
             "automation_id": "SearchTextBox"},
    ))
    # ── 2. Type "Chrome" en winsearch ─────────────────────────────
    events.append(_type_text(
        "chrome", ms=200,
        hwnd=1, title="Búsqueda", proc="searchhost.exe",
        user_typed_text="Chrome",
    ))
    # ── 3. Click resultado Chrome ─────────────────────────────────
    events.append(_click(
        400, 400, ms=400,
        hwnd=1, title="Búsqueda", proc="searchhost.exe",
        uia={"name": "Google Chrome, Aplicación de escritorio",
             "control_type": "ListItemControl"},
    ))
    # ── 4. Activación de Chrome (profile picker) ──────────────────
    events.append(_activate(
        600, hwnd=10, title="¿Quién eres? - Google Chrome",
        proc="chrome.exe",
    ))
    # 4b. Click perfil "Daniel Chrome D Daniel Arturo Ramos"
    # (El UIA name viene basura — GroupControl — pero el recorder
    # capturó el label humano completo en el ítem del picker.)
    events.append(_click(
        500, 400, ms=900,
        hwnd=10, title="¿Quién eres? - Google Chrome",
        proc="chrome.exe",
        uia={"name": "GroupControl",
             "control_type": "GroupControl"},
        extra_meta={
            "profile_picker_item_text":
                "Daniel Chrome D Daniel Arturo Ramos",
        },
    ))
    # ── 5. Activación nueva pestaña ───────────────────────────────
    events.append(_activate(
        1200, hwnd=11, title="Nueva pestaña - Google Chrome",
        proc="chrome.exe",
    ))
    events.append(_click(
        300, 50, ms=1300,
        hwnd=11, title="Nueva pestaña - Google Chrome",
        proc="chrome.exe",
        uia={"name": "Nueva pestaña", "control_type": "ButtonControl"},
    ))
    # ── 6. Click icono YouTube ────────────────────────────────────
    events.append(_click(
        300, 400, ms=1500,
        hwnd=11, title="Nueva pestaña - Google Chrome",
        proc="chrome.exe",
        uia={"name": "Youtube", "control_type": "ButtonControl"},
        web={"url": "https://www.google.com/", "domain": "google.com",
             "role": "link", "text": "YouTube",
             "href": "https://www.youtube.com/", "tag": "a",
             "locators": [{"kind": "role", "role": "link",
                           "name": "YouTube"}]},
    ))
    # ── 7. Activación de YouTube ──────────────────────────────────
    events.append(_activate(
        1700, hwnd=12, title="YouTube - Google Chrome",
        proc="chrome.exe",
    ))
    # 7b. Click barra de búsqueda YouTube
    events.append(_click(
        900, 120, ms=1800,
        hwnd=12, title="YouTube - Google Chrome", proc="chrome.exe",
        uia={"name": "guide-service",
             "control_type": "EditControl"},
        web={"url": "https://www.youtube.com/",
             "domain": "youtube.com", "role": "searchbox",
             "placeholder": "Buscar", "tag": "input", "type": "search",
             "input_value_at_capture": ""},
    ))
    # ── 8. Type "Devuélveme el amor de Luis Miguel" ──────────────
    # (UIA leyó la versión sin "de" y sin acentos — el buffer del
    # usuario tiene la versión completa.)
    events.append(_type_text(
        "devuelveme el amor luis miguel", ms=2000,
        hwnd=12, title="YouTube - Google Chrome", proc="chrome.exe",
        user_typed_text="Devuélveme el amor de Luis Miguel",
    ))
    # ── 9. Enter ──────────────────────────────────────────────────
    events.append(_enter(
        2200, hwnd=12, title="YouTube - Google Chrome",
        proc="chrome.exe",
    ))
    # ── 10. Scroll down en resultados ─────────────────────────────
    events.append(_scroll(
        2500, hwnd=12,
        title="devuélveme el amor de luis miguel - YouTube - Google Chrome",
        proc="chrome.exe", count=1,
    ))
    return events


# ──────────────────────────────────────────────────────────────────
# E2E: el plan final cumple TODAS las garantías de READY
# ──────────────────────────────────────────────────────────────────


class TestE2EChromeYoutubeReady:
    def _build_and_collapse(self) -> Mission:
        m = Mission(name="e2e_chrome_youtube",
                    raw_trace=_build_chrome_youtube_trace())
        apply_collapse_to_mission(m)
        return m

    def test_semantic_plan_attached_with_six_steps(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        assert sep, "semantic_execution_plan no se adjuntó"
        kinds = [s.get("type") for s in sep["steps"]]
        # Esperamos los 6 pasos canónicos en el orden del PRD.
        assert kinds == [
            "open_app",
            "select_profile",
            "open_new_tab",
            "open_url",
            "search_content",
            "scroll_results",
        ], kinds

    def test_intent_status_is_ready(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        assert sep["intent_status"] == CollapseStatus.READY, (
            f"intent_status debería ser READY, fue "
            f"{sep['intent_status']!r} con razones "
            f"{sep.get('not_ready_reasons')}"
        )
        assert sep.get("not_ready_reasons") == []

    def test_coords_used_false_and_legacy_ignored(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        assert sep["coords_used"] is False
        assert sep["legacy_graph_ignored"] is True

    def test_compiled_execution_graph_is_legacy_debug_only(self):
        m = self._build_and_collapse()
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"

    def test_select_profile_has_canonical_name(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        sp = sep["steps"][1]
        assert sp["type"] == "select_profile"
        assert sp["params"]["profile_name"] == "Daniel Arturo Ramos"
        assert sp["needs_user_label"] is False

    def test_search_content_youtube_preserves_full_query(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        sy = sep["steps"][4]
        assert sy["type"] == "search_content"
        assert str((sy.get("params") or {}).get("provider") or "").lower() \
            == "youtube"
        assert sy["params"]["query"] == (
            "Devuélveme el amor de Luis Miguel"
        )
        assert sy["needs_user_label"] is False
        assert sy["params"].get("query_fidelity_status", "ok") == "ok"

    def test_open_new_tab_uses_ctrl_t_strategy(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        assert (
            sep["steps"][2]["preferred_strategy"]
            == "open_new_tab:hotkey_ctrl_t"
        )

    def test_open_url_uses_ctrl_l_strategy(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        assert (
            sep["steps"][3]["preferred_strategy"]
            == "open_url:hotkey_ctrl_l"
        )
        assert sep["steps"][3]["params"]["url"].lower().startswith(
            "youtube"
        )

    def test_search_content_youtube_uses_smart_route_primary(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        assert (
            sep["steps"][4]["preferred_strategy"]
            == "search_content:smart_route"
        )

    def test_scroll_results_canonical_params(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        sr = sep["steps"][5]
        assert sr["type"] == "scroll_results"
        assert sr["params"]["direction"] == "down"
        assert sr["params"]["context"] == "youtube_results"
        assert int(sr["params"]["amount"]) >= 1

    def test_no_legacy_step_types_inside_plan(self):
        """Ningún ``click``/``mouse_scroll``/``key_press``/``type_text``
        debe colarse al semantic plan.
        """
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        forbidden = {
            "click", "click_button", "mouse_click", "mouse_scroll",
            "scroll", "key_press", "key_press_enter",
            "type", "type_text", "send_hotkey", "use_coords",
            "coordinate_click", "groupcontrol", "guide-service",
            "guide_service", "video-stream", "video_stream",
        }
        for step in sep["steps"]:
            assert step["type"] not in forbidden, step

    def test_no_use_coords_as_primary_strategy(self):
        m = self._build_and_collapse()
        sep = m.semantic_execution_plan
        for step in sep["steps"]:
            primary = (step.get("preferred_strategy") or "").lower()
            assert "coord" not in primary, primary
            assert "visual" not in primary, primary
            assert "use_coords" not in primary, primary
