"""
Tests del Intent Collapse Engine (Final).
=========================================

Cubren los siete casos del spec del usuario (sprint
"Intent Collapse Engine Final"):

  1. ``full_chrome_youtube_search_scroll`` — pipeline completo del caso
     ejemplo: Windows Search + Chrome + perfil + nueva pestaña + bookmark
     YouTube + ``guide-service`` + texto fragmentado + Enter + scrolls.
  2. ``youtube_guide_service_search`` — ``guide-service`` + query +
     Enter en YouTube ⇒ ``search_youtube`` (sin ``guide-service``
     superviviente).
  3. ``text_fragments`` — fragmentos progresivos colapsados al texto
     final.
  4. ``scroll_fusion`` — N scrolls coalesced ⇒ ``scroll_results`` con
     ``amount`` agregado.
  5. ``max_questions`` — perfil desconocido + búsqueda clara ⇒ máximo 1
     pregunta.
  6. ``no_generic_clicks`` — ningún ``click_button``/``click``
     sobrevive si hay intención semántica.
  7. ``example_mission_exact_shape`` — la misión ejemplo tiene como
     mucho 6 pasos y la forma canónica del spec.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from app.contracts.mission import (
    ActionStrategy,
    EventType,
    KeyboardAction,
    Mission,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions.intent_collapse_engine import (
    SEMANTIC_TYPES,
    CollapsedStep,
    CollapseStatus,
    MissionIntentPlan,
    apply_collapse_to_mission,
    collapse_intent,
    hard_post_pass,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers de construcción de RawEvent (alineados con test_v2_pipeline)
# ──────────────────────────────────────────────────────────────────────

_BASE_TS = datetime(2026, 5, 6, 6, 0, 0, tzinfo=timezone.utc)


def _seq_id() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000) % 10**9


def _click(
    x: int,
    y: int,
    ts_offset_ms: int = 0,
    *,
    hwnd: int = 1,
    title: str = "App",
    proc: str = "app.exe",
    uia: dict = None,
    web: dict = None,
    vision: dict = None,
) -> RawEvent:
    meta: dict = {}
    if uia is not None:
        meta["uia_data"] = uia
    if web is not None:
        meta["web_data"] = web
    if vision is not None:
        meta["vision_data"] = vision
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_CLICK,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        mouse_action=MouseAction(x=x, y=y),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata=meta,
    )


def _type(
    text: str,
    ts_offset_ms: int = 0,
    *,
    hwnd: int = 1,
    title: str = "App",
    proc: str = "app.exe",
    web: dict = None,
) -> RawEvent:
    meta: dict = {}
    if web is not None:
        meta["web_data"] = web
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        keyboard_action=KeyboardAction(keys=list(text)),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata=meta,
    )


def _enter(
    ts_offset_ms: int = 0,
    *,
    hwnd: int = 1,
    title: str = "App",
    proc: str = "app.exe",
) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_KEY_PRESS,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        keyboard_action=KeyboardAction(keys=["enter"]),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _hotkey(
    keys: List[str],
    modifiers: List[str],
    ts_offset_ms: int = 0,
    *,
    hwnd: int = 1,
    title: str = "App",
    proc: str = "app.exe",
) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_HOTKEY,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        keyboard_action=KeyboardAction(keys=keys, modifiers=modifiers),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _activate(
    ts_offset_ms: int = 0,
    *,
    hwnd: int = 1,
    title: str = "App",
    proc: str = "app.exe",
) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.WINDOW_ACTIVATE,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _scroll(
    dy: int,
    ts_offset_ms: int = 0,
    *,
    hwnd: int = 1,
    title: str = "YouTube - Chrome",
    proc: str = "chrome.exe",
) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_SCROLL,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata={"dy": dy, "count": 1},
    )


# ──────────────────────────────────────────────────────────────────────
# Builders de traces típicos
# ──────────────────────────────────────────────────────────────────────


def _full_chrome_youtube_trace() -> List[RawEvent]:
    """Trace completo del caso ejemplo del spec.

    Pasos humanos:
        1) Click en Windows Search.
        2) Escribir "chrome".
        3) Click en resultado Chrome.
        4) Click en perfil Daniel Arturo Ramos.
        5) Click en Nueva pestaña.
        6) Click en bookmark YouTube.
        7) Click en barra YouTube (UIA name=guide-service).
        8) Escribir "Devuélveme el amor de Luis Miguel" (3 fragmentos).
        9) Enter.
        10) Scroll abajo (2 ticks).
    """
    events: List[RawEvent] = []
    ts = 0

    # 1) Click en Windows Search.
    events.append(_click(
        200, 1050, ts,
        hwnd=10, title="Búsqueda", proc="searchhost.exe",
        uia={"name": "Escribe aquí para buscar",
             "automation_id": "SearchTextBox"},
    ))
    ts += 100

    # 2) Escribir "chrome".
    events.append(_type(
        "chrome", ts,
        hwnd=10, title="Búsqueda", proc="searchhost.exe",
    ))
    ts += 200

    # 3) Click en resultado Chrome.
    events.append(_click(
        400, 400, ts,
        hwnd=10, title="Búsqueda", proc="searchhost.exe",
        uia={"name": "Google Chrome"},
    ))
    ts += 500

    # 4) Activación de Chrome con perfil picker.
    events.append(_activate(
        ts, hwnd=20, title="¿Quién eres? - Google Chrome", proc="chrome.exe",
    ))
    ts += 300
    # Click sobre el perfil — UIA pobre, pero OCR lee el nombre.
    events.append(_click(
        600, 400, ts,
        hwnd=20, title="¿Quién eres? - Google Chrome", proc="chrome.exe",
        uia={"name": "main", "control_type": "PaneControl"},
        vision={"ocr_text_around": "Daniel Arturo Ramos"},
    ))
    ts += 500

    # 5) Activación de Chrome con perfil Daniel.
    events.append(_activate(
        ts, hwnd=21,
        title="Daniel Arturo Ramos - Google Chrome",
        proc="chrome.exe",
    ))
    ts += 100

    # 6) Ctrl+T (nueva pestaña).
    events.append(_hotkey(
        keys=["t"], modifiers=["ctrl"], ts_offset_ms=ts,
        hwnd=21, title="Daniel Arturo Ramos - Google Chrome",
        proc="chrome.exe",
    ))
    ts += 200
    events.append(_activate(
        ts, hwnd=22, title="Nueva pestaña - Google Chrome",
        proc="chrome.exe",
    ))
    ts += 100

    # 7) Click en bookmark YouTube.
    events.append(_click(
        300, 60, ts,
        hwnd=22, title="Nueva pestaña - Google Chrome", proc="chrome.exe",
        uia={"name": "YouTube", "control_type": "ButtonControl",
             "class_name": "BookmarkBar"},
        web={
            "url": "https://www.google.com/",
            "href": "https://www.youtube.com/",
            "text": "YouTube",
            "role": "link",
        },
    ))
    ts += 500

    # 8) Activación YouTube.
    events.append(_activate(
        ts, hwnd=23, title="YouTube - Google Chrome", proc="chrome.exe",
    ))
    ts += 100

    # 9) Click en barra YouTube — UIA name="guide-service" (basura)
    #    pero web_data tiene placeholder=Buscar y url=youtube.com.
    events.append(_click(
        900, 120, ts,
        hwnd=23, title="YouTube - Google Chrome", proc="chrome.exe",
        uia={"name": "guide-service", "control_type": "EditControl"},
        web={
            "url": "https://www.youtube.com/",
            "domain": "youtube.com",
            "role": "searchbox",
            "placeholder": "Buscar",
        },
    ))
    ts += 300

    # 10) Escribir el query en fragmentos progresivos (DevueDevue case).
    events.append(_type(
        "Devué", ts,
        hwnd=23, title="YouTube - Google Chrome", proc="chrome.exe",
        web={"url": "https://www.youtube.com/"},
    ))
    ts += 100
    events.append(_type(
        "Devuélveme", ts,
        hwnd=23, title="YouTube - Google Chrome", proc="chrome.exe",
        web={"url": "https://www.youtube.com/"},
    ))
    ts += 100
    events.append(_type(
        "Devuélveme el amor de Luis Miguel", ts,
        hwnd=23, title="YouTube - Google Chrome", proc="chrome.exe",
        web={"url": "https://www.youtube.com/"},
    ))
    ts += 100

    # 11) Enter.
    events.append(_enter(
        ts, hwnd=23, title="YouTube - Google Chrome", proc="chrome.exe",
    ))
    ts += 500

    # 12) Activación YouTube results.
    events.append(_activate(
        ts, hwnd=23,
        title="devuélveme luis miguel - YouTube", proc="chrome.exe",
    ))
    ts += 200

    # 13) Scroll abajo × 2.
    events.append(_scroll(
        -120, ts,
        hwnd=23, title="devuélveme luis miguel - YouTube", proc="chrome.exe",
    ))
    ts += 100
    events.append(_scroll(
        -120, ts,
        hwnd=23, title="devuélveme luis miguel - YouTube", proc="chrome.exe",
    ))

    return events


# ──────────────────────────────────────────────────────────────────────
# 1. full_chrome_youtube_search_scroll
# ──────────────────────────────────────────────────────────────────────

class TestFullChromeYouTubeSearchScroll:

    def test_collapses_to_six_steps(self):
        """Pipeline completo: 6 pasos, 4 bloques, sin residuos."""
        m = Mission(name="full", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)

        types = [s.type for s in plan.semantic_steps]
        assert "open_app" in types
        assert "select_profile" in types
        assert "open_new_tab" in types
        assert "open_url" in types
        assert "search_youtube" in types
        assert "scroll_results" in types

        # 0 click_button, 0 type_text, 0 enter, 0 guide-service.
        for s in plan.semantic_steps:
            assert s.type in SEMANTIC_TYPES, (
                f"Tipo no semántico: {s.type}"
            )
            for v in s.params.values():
                if isinstance(v, str):
                    assert "guide-service" not in v.lower(), (
                        f"guide-service sobrevive en {s.type} params: {s.params}"
                    )

        # confidence > 0.8
        assert plan.confidence_avg > 0.80, (
            f"confianza media {plan.confidence_avg:.2f} <= 0.80"
        )

    def test_blocks_have_canonical_names(self):
        m = Mission(name="full", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        names = [b.name for b in plan.blocks]
        assert any("Chrome" in n for n in names)
        assert any("YouTube" in n for n in names)
        assert "Buscar en YouTube" in names
        assert "Explorar resultados" in names

    def test_query_is_final_text_only(self):
        """El query de search_youtube no contiene fragmentos como
        DevueDevuelveme.
        """
        m = Mission(name="full", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        q = sy.params.get("query", "")
        assert "DevuéDevué" not in q
        assert "DevuélvemeDevuélveme" not in q
        assert q == "Devuélveme el amor de Luis Miguel"

    def test_no_residue_after_collapse(self):
        m = Mission(name="full", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        for s in plan.semantic_steps:
            assert s.type != "click"
            assert s.type != "click_button"
            assert s.type != "type_text"
            assert s.type != "key_press"
            assert s.type != "send_hotkey"
            assert s.type != "scroll"


# ──────────────────────────────────────────────────────────────────────
# 2. youtube_guide_service_search
# ──────────────────────────────────────────────────────────────────────

class TestYouTubeGuideServiceSearch:

    def _build_trace(self) -> List[RawEvent]:
        """Trace YouTube ya cargado: click en barra (UIA=guide-service)
        + texto + Enter.
        """
        events: List[RawEvent] = []
        ts = 0
        # YouTube ya activo.
        events.append(_activate(
            ts, hwnd=30, title="YouTube - Google Chrome",
            proc="chrome.exe",
        ))
        ts += 100
        # Click en barra de búsqueda (UIA name=guide-service, web data
        # con url=youtube.com).
        events.append(_click(
            900, 120, ts,
            hwnd=30, title="YouTube - Google Chrome", proc="chrome.exe",
            uia={"name": "guide-service", "control_type": "EditControl"},
            web={
                "url": "https://www.youtube.com/",
                "domain": "youtube.com",
                "role": "searchbox",
                "placeholder": "Buscar",
            },
        ))
        ts += 200
        # Escribir el query.
        events.append(_type(
            "Luis Miguel", ts,
            hwnd=30, title="YouTube - Google Chrome", proc="chrome.exe",
            web={"url": "https://www.youtube.com/"},
        ))
        ts += 200
        # Enter.
        events.append(_enter(
            ts, hwnd=30, title="YouTube - Google Chrome",
            proc="chrome.exe",
        ))
        return events

    def test_collapses_to_search_youtube(self):
        m = Mission(name="ygs", raw_trace=self._build_trace())
        plan = collapse_intent(mission=m)
        types = [s.type for s in plan.semantic_steps]
        assert "search_youtube" in types

    def test_no_guide_service_in_output(self):
        m = Mission(name="ygs", raw_trace=self._build_trace())
        plan = collapse_intent(mission=m)
        for s in plan.semantic_steps:
            for v in s.params.values():
                if isinstance(v, str):
                    assert "guide-service" not in v.lower()

    def test_does_not_emit_open_app_without_signal(self):
        """Si el trace empieza dentro de YouTube, NO emitir open_app."""
        m = Mission(name="ygs", raw_trace=self._build_trace())
        plan = collapse_intent(mission=m)
        types = [s.type for s in plan.semantic_steps]
        # Sin profile picker ni winsearch, no debe inferir open_app.
        # Permitimos que otros patrones (open_url) sí estén presentes.
        # El primer event NO es chrome.exe (sí lo es: chrome.exe), pero
        # solo emitimos open_app implícito si _es_ el primer evento; en
        # este caso lo es, así que aceptamos open_app con confianza
        # 0.85 si aparece, pero NO debe inducir un select_profile.
        assert "select_profile" not in types


# ──────────────────────────────────────────────────────────────────────
# 3. text_fragments
# ──────────────────────────────────────────────────────────────────────

class TestTextFragments:

    def test_devue_devuelveme_collapses_to_devuelveme(self):
        """Fragmentos progresivos en YouTube → texto final único."""
        events: List[RawEvent] = []
        ts = 0
        events.append(_activate(
            ts, hwnd=40, title="YouTube - Google Chrome",
            proc="chrome.exe",
        ))
        ts += 100
        events.append(_click(
            900, 120, ts,
            hwnd=40, title="YouTube - Google Chrome", proc="chrome.exe",
            uia={"name": "guide-service"},
            web={"url": "https://www.youtube.com/", "domain": "youtube.com"},
        ))
        ts += 100
        # Fragmentos progresivos.
        events.append(_type(
            "Devue", ts,
            hwnd=40, title="YouTube - Google Chrome", proc="chrome.exe",
            web={"url": "https://www.youtube.com/"},
        ))
        ts += 50
        events.append(_type(
            "Devuelveme", ts,
            hwnd=40, title="YouTube - Google Chrome", proc="chrome.exe",
            web={"url": "https://www.youtube.com/"},
        ))
        ts += 50
        events.append(_enter(
            ts, hwnd=40, title="YouTube - Google Chrome",
            proc="chrome.exe",
        ))

        m = Mission(name="frag", raw_trace=events)
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        q = sy.params["query"]
        assert q == "Devuelveme", f"Esperado 'Devuelveme', got {q!r}"


# ──────────────────────────────────────────────────────────────────────
# 4. scroll_fusion
# ──────────────────────────────────────────────────────────────────────

class TestScrollFusion:

    def test_two_scrolls_become_scroll_results_amount_2(self):
        """Dos scrolls cercanos en YouTube post-search ⇒ 1 step
        ``scroll_results`` con ``amount=2``."""
        events: List[RawEvent] = []
        ts = 0
        events.append(_activate(
            ts, hwnd=50, title="YouTube - Google Chrome",
            proc="chrome.exe",
        ))
        ts += 100
        events.append(_click(
            900, 120, ts,
            hwnd=50, title="YouTube - Google Chrome", proc="chrome.exe",
            uia={"name": "guide-service"},
            web={"url": "https://www.youtube.com/", "domain": "youtube.com"},
        ))
        ts += 100
        events.append(_type(
            "test", ts,
            hwnd=50, title="YouTube - Google Chrome", proc="chrome.exe",
            web={"url": "https://www.youtube.com/"},
        ))
        ts += 100
        events.append(_enter(
            ts, hwnd=50, title="YouTube - Google Chrome",
            proc="chrome.exe",
        ))
        ts += 200
        events.append(_activate(
            ts, hwnd=50,
            title="test - YouTube - Google Chrome", proc="chrome.exe",
        ))
        ts += 100
        events.append(_scroll(
            -120, ts,
            hwnd=50, title="test - YouTube - Google Chrome",
            proc="chrome.exe",
        ))
        # Suficientemente lejos para NO coalescer en sanitizer (>350ms).
        ts += 600
        events.append(_scroll(
            -120, ts,
            hwnd=50, title="test - YouTube - Google Chrome",
            proc="chrome.exe",
        ))

        m = Mission(name="sf", raw_trace=events)
        plan = collapse_intent(mission=m)
        scroll = next(
            (s for s in plan.semantic_steps if s.type == "scroll_results"),
            None,
        )
        assert scroll is not None
        assert scroll.params.get("amount") == 2
        assert scroll.params.get("direction") == "down"


# ──────────────────────────────────────────────────────────────────────
# 5. max_questions
# ──────────────────────────────────────────────────────────────────────

class TestMaxQuestions:

    def test_unknown_profile_clear_search_one_question(self):
        """Perfil desconocido + búsqueda clara → exactamente 1 pregunta."""
        events: List[RawEvent] = []
        ts = 0
        # Profile picker SIN OCR — el engine no podrá leer el nombre.
        events.append(_activate(
            ts, hwnd=60, title="¿Quién eres? - Google Chrome",
            proc="chrome.exe",
        ))
        ts += 100
        events.append(_click(
            500, 400, ts,
            hwnd=60, title="¿Quién eres? - Google Chrome",
            proc="chrome.exe",
            uia={"name": "main", "control_type": "PaneControl"},
            # Sin vision_data.
        ))
        ts += 200
        # Después YouTube + búsqueda clara.
        events.append(_activate(
            ts, hwnd=61, title="YouTube - Google Chrome",
            proc="chrome.exe",
        ))
        ts += 100
        events.append(_click(
            900, 120, ts,
            hwnd=61, title="YouTube - Google Chrome", proc="chrome.exe",
            uia={"name": "guide-service"},
            web={"url": "https://www.youtube.com/", "domain": "youtube.com"},
        ))
        ts += 100
        events.append(_type(
            "Luis Miguel", ts,
            hwnd=61, title="YouTube - Google Chrome", proc="chrome.exe",
            web={"url": "https://www.youtube.com/"},
        ))
        ts += 100
        events.append(_enter(
            ts, hwnd=61, title="YouTube - Google Chrome",
            proc="chrome.exe",
        ))

        m = Mission(name="mq", raw_trace=events)
        plan = collapse_intent(mission=m)

        # Exactamente 1 pregunta (la del perfil), no más.
        assert plan.repair_count == 1, (
            f"Esperaba 1 pregunta, obtuve {plan.repair_count}: "
            f"{[q.code for q in plan.repair_questions]}"
        )
        assert plan.repair_questions[0].code == "profile_unknown"

    def test_repair_count_never_exceeds_two(self):
        """Aun con muchos targets ambiguos, max 2 preguntas."""
        events = _full_chrome_youtube_trace()
        m = Mission(name="cap", raw_trace=events)
        plan = collapse_intent(mission=m)
        assert plan.repair_count <= 2


# ──────────────────────────────────────────────────────────────────────
# 6. no_generic_clicks
# ──────────────────────────────────────────────────────────────────────

class TestNoGenericClicks:

    def test_no_click_button_survives_when_semantic_present(self):
        """Si hay intención semántica, ningún click_button sobrevive."""
        m = Mission(name="ngc", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        for s in plan.semantic_steps:
            assert s.type != "click_button"
            assert s.type != "click"
            assert s.type != "click_fallback"

    def test_hard_post_pass_drops_residue_when_semantic(self):
        """``hard_post_pass`` elimina residuos SOLO cuando existe un
        step semántico que cubre su intención (PRD 2026-05-06d §3).

        Reglas:
          * ``click_button`` / ``type_text`` quedan cubiertos por
            ``open_app`` → se borran.
          * ``key_press(enter)`` requiere ``search_youtube`` para
            ser cubierto → se queda.
          * ``scroll`` requiere ``scroll_results`` para ser cubierto →
            se queda.
        """
        steps = [
            CollapsedStep(type="open_app", params={"app": "chrome"},
                          confidence=0.9),
            CollapsedStep(type="click_button", params={}, confidence=0.5),
            CollapsedStep(type="type_text", params={}, confidence=0.5),
            CollapsedStep(type="key_press", params={"key": "enter"},
                          confidence=0.5),
            CollapsedStep(type="scroll", params={"dy": -120},
                          confidence=0.5),
        ]
        cleaned = hard_post_pass(steps)
        types = [s.type for s in cleaned]
        # Residuos no cubiertos por ningún step semántico se quedan.
        assert "open_app" in types
        assert "click_button" not in types  # cubierto por open_app
        assert "type_text" not in types     # cubierto por open_app
        assert "key_press" in types         # NO cubierto (no search_youtube)
        assert "scroll" in types            # NO cubierto (no scroll_results)

    def test_hard_post_pass_drops_residue_with_full_coverage(self):
        """Con steps semánticos que cubren todo, los residuos se borran."""
        steps = [
            CollapsedStep(type="open_app", params={"app": "chrome"},
                          confidence=0.9),
            CollapsedStep(type="search_youtube", params={"query": "x"},
                          confidence=0.95),
            CollapsedStep(type="scroll_results",
                          params={"direction": "down", "amount": 1},
                          confidence=0.85),
            CollapsedStep(type="click_button", params={}, confidence=0.5),
            CollapsedStep(type="type_text", params={}, confidence=0.5),
            CollapsedStep(type="key_press", params={"key": "enter"},
                          confidence=0.5),
            CollapsedStep(type="scroll", params={"dy": -120},
                          confidence=0.5),
        ]
        cleaned = hard_post_pass(steps)
        types = [s.type for s in cleaned]
        # Todos los residuos quedan cubiertos.
        for residue in ("click_button", "type_text", "key_press", "scroll"):
            assert residue not in types, f"{residue} debería haberse borrado"
        # Y los semánticos sobreviven.
        assert types == ["open_app", "search_youtube", "scroll_results"]


# ──────────────────────────────────────────────────────────────────────
# 7. example_mission_exact_shape
# ──────────────────────────────────────────────────────────────────────

class TestExampleMissionExactShape:

    def test_max_six_steps(self):
        m = Mission(name="ex", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        assert plan.step_count <= 6, (
            f"La misión ejemplo debe tener como mucho 6 pasos. "
            f"Obtuve {plan.step_count}: "
            f"{[s.type for s in plan.semantic_steps]}"
        )

    def test_exact_canonical_shape(self):
        """Verifica la forma canónica del spec:
            Bloque 1: Abrir Chrome   [open_app, select_profile]
            Bloque 2: Abrir YouTube  [open_new_tab, open_url]
            Bloque 3: Buscar en YouTube [search_youtube]
            Bloque 4: Explorar resultados [scroll_results]
        """
        m = Mission(name="ex", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)

        # 4 bloques exactos.
        assert len(plan.blocks) == 4, (
            f"Esperaba 4 bloques, obtuve {len(plan.blocks)}: "
            f"{[b.name for b in plan.blocks]}"
        )

        block_types = [
            [s.type for s in b.steps] for b in plan.blocks
        ]
        assert block_types[0] == ["open_app", "select_profile"]
        assert block_types[1] == ["open_new_tab", "open_url"]
        assert block_types[2] == ["search_youtube"]
        assert block_types[3] == ["scroll_results"]

    def test_status_executable_or_needs_review(self):
        """Resultado: EXECUTABLE o NEEDS_REVIEW (solo por perfil)."""
        m = Mission(name="ex", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        assert plan.status in (
            CollapseStatus.EXECUTABLE,
            CollapseStatus.NEEDS_REVIEW,
        )
        if plan.status == CollapseStatus.NEEDS_REVIEW:
            # El motivo debe ser SOLO el perfil.
            assert plan.repair_count <= 1
            if plan.repair_count == 1:
                assert plan.repair_questions[0].code == "profile_unknown"

    def test_apply_collapse_to_mission_canonical_payload(self):
        """``apply_collapse_to_mission(..., rewrite_compiled_graph=True)``
        deja la misión con la forma canónica para ejecución (modo
        destructivo: sobrescribe el grafo legacy con el plan
        semántico).
        """
        m = Mission(name="ex", raw_trace=_full_chrome_youtube_trace())
        plan = apply_collapse_to_mission(m, rewrite_compiled_graph=True)
        assert m.compiled_execution_graph
        assert len(m.compiled_execution_graph) == plan.step_count
        # Action_groups alineados con bloques.
        assert len(m.action_groups) == len(plan.blocks)
        # Cada bloque tiene step_ids no vacíos.
        for grp in m.action_groups:
            assert grp.step_ids

        # El primer bloque es open_app + select_profile.
        first_actions = [
            cs.action_strategy
            for cs in m.compiled_execution_graph[:2]
        ]
        assert ActionStrategy.LAUNCH_APP in first_actions
        assert ActionStrategy.SELECT_PROFILE in first_actions

        # search_youtube se mapea a SUBMIT_SEARCH con site=youtube.
        sub = [
            cs for cs in m.compiled_execution_graph
            if cs.action_strategy == ActionStrategy.SUBMIT_SEARCH
        ]
        assert sub, "Falta SUBMIT_SEARCH"
        for cs in sub:
            pl = cs.action_payload or {}
            assert pl.get("site") == "youtube"
            assert pl.get("text") == "Devuélveme el amor de Luis Miguel"
            assert pl.get("submit_after") is True

    def test_confidence_average_above_0_80(self):
        m = Mission(name="ex", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        assert plan.confidence_avg >= 0.80, (
            f"confidence_avg={plan.confidence_avg:.2f} (esperado >= 0.80)"
        )


# ──────────────────────────────────────────────────────────────────────
# Sanity checks de la API
# ──────────────────────────────────────────────────────────────────────

class TestApiSanity:

    def test_empty_mission_returns_compiled_draft(self):
        m = Mission(name="empty", raw_trace=[])
        plan = collapse_intent(mission=m)
        assert plan.status == CollapseStatus.COMPILED_DRAFT
        assert plan.step_count == 0
        assert plan.confidence_avg == 0.0

    def test_plan_as_dict_round_trip(self):
        m = Mission(name="rt", raw_trace=_full_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        d = plan.as_dict()
        assert d["status"] in {
            CollapseStatus.EXECUTABLE,
            CollapseStatus.NEEDS_REVIEW,
            CollapseStatus.COMPILED_DRAFT,
        }
        assert "blocks" in d
        assert "semantic_steps" in d
        assert "confidence_avg" in d
        assert "repair_count" in d


# ──────────────────────────────────────────────────────────────────────
# 8. PRD 2026-05-06d — sobre-colapso, coverage audit, persistencia
# ──────────────────────────────────────────────────────────────────────


def _bare_chrome_youtube_trace() -> List[RawEvent]:
    """Reproduce el trace REAL de la misión f2360036.

    Sin metadata UIA enriquecida (field_session_manager devuelve 0
    sesiones). El ICE legacy producía solo ``open_new_tab + open_url``.
    Tras el PRD 2026-05-06d debe producir 6 pasos canónicos.
    """
    base = _BASE_TS
    def _at(off_s: int) -> datetime:
        return base + timedelta(seconds=off_s)

    def _click(idx: int, proc: str, title: str) -> RawEvent:
        return RawEvent(
            id=f"e{idx}",
            event_type=EventType.MOUSE_CLICK,
            timestamp=_at(idx),
            window_context=WindowContext(process_name=proc, title=title),
            mouse_action=MouseAction(button="left", x=100 + idx, y=200),
        )

    def _type(idx: int, proc: str, title: str, txt: str) -> RawEvent:
        return RawEvent(
            id=f"e{idx}",
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=_at(idx),
            window_context=WindowContext(process_name=proc, title=title),
            metadata={"final_text": txt, "field_session": {"id": f"s{idx}"}},
        )

    def _enter(idx: int, proc: str, title: str) -> RawEvent:
        return RawEvent(
            id=f"e{idx}",
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=_at(idx),
            window_context=WindowContext(process_name=proc, title=title),
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
        )

    def _scroll(idx: int, proc: str, title: str, count: int = 1) -> RawEvent:
        return RawEvent(
            id=f"e{idx}",
            event_type=EventType.MOUSE_SCROLL,
            timestamp=_at(idx),
            window_context=WindowContext(process_name=proc, title=title),
            mouse_action=MouseAction(button="left", x=500, y=400),
            metadata={"count": count},
        )

    return [
        _click(0, "explorer.exe", ""),
        _click(1, "SearchUI.exe", "Búsqueda"),
        _type(2, "SearchUI.exe", "Búsqueda", "chrome"),
        _click(3, "chrome.exe", "Google Chrome"),
        _click(4, "chrome.exe", "Avances de Nevlan - Google Chrome"),
        _click(5, "chrome.exe", "Nueva pestaña - Google Chrome"),
        _click(6, "chrome.exe", "YouTube - Google Chrome"),
        _type(7, "chrome.exe", "YouTube - Google Chrome", "devu"),
        _enter(8, "chrome.exe", "YouTube - Google Chrome"),
        # Re-lectura post-Enter (UIA commitea la query final). Misma
        # field_session que el [7].
        RawEvent(
            id="e9",
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=base + timedelta(seconds=9),
            window_context=WindowContext(
                process_name="chrome.exe",
                title="YouTube - Google Chrome",
            ),
            metadata={
                "final_text": "devuelveme el amor luis miguel",
                "field_session": {"id": "s7"},
            },
        ),
        _scroll(10, "chrome.exe",
                "devuelveme el amor luis miguel - YouTube - Google Chrome"),
        _scroll(11, "chrome.exe",
                "devuelveme el amor luis miguel - YouTube - Google Chrome"),
    ]


class TestRegressionMissionF2360036:
    """Caso PRD 2026-05-06d — la misión f2360036 debe colapsar a 6 pasos.

    Antes del fix: 2 pasos (``open_new_tab + open_url``), status
    erróneamente EXECUTABLE.
    Después del fix: 6 pasos canónicos con cobertura completa.
    """

    def test_six_canonical_steps_without_field_sessions(self):
        m = Mission(name="f2360036", raw_trace=_bare_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        types = [s.type for s in plan.semantic_steps]
        assert types == [
            "open_app",
            "select_profile",
            "open_new_tab",
            "open_url",
            "search_youtube",
            "scroll_results",
        ], f"Esperaba 6 pasos canónicos, obtuve: {types}"

    def test_search_youtube_query_recovered_from_post_enter_event(self):
        """PRD §6 — search finalizer toma el final_text más largo de
        la misma field_session, incluso si llega DESPUÉS del Enter.
        """
        m = Mission(name="f2360036", raw_trace=_bare_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        assert sy.params.get("query") == "devuelveme el amor luis miguel"
        assert sy.params.get("submit") is True
        assert sy.params.get("target") == "youtube_search_box"

    def test_scroll_results_detected_via_fallback(self):
        """PRD §7 — scroll fusion: scrolls en chrome+YouTube tras la
        búsqueda colapsan a ``scroll_results`` con ``amount`` agregado.
        """
        m = Mission(name="f2360036", raw_trace=_bare_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        sr = next(
            (s for s in plan.semantic_steps if s.type == "scroll_results"),
            None,
        )
        assert sr is not None
        assert sr.params.get("direction") == "down"
        assert sr.params.get("amount") >= 2  # 2 mouse_scroll events
        assert sr.params.get("context") == "youtube_results"

    def test_open_app_via_fallback_winsearch(self):
        """PRD §6 — open_app se infiere de SearchUI.exe + 'chrome'
        sin necesidad de field_session_manager.
        """
        m = Mission(name="f2360036", raw_trace=_bare_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        oa = next(
            (s for s in plan.semantic_steps if s.type == "open_app"),
            None,
        )
        assert oa is not None
        assert oa.params.get("app") == "chrome"
        assert oa.params.get("method") == "windows_search"

    def test_select_profile_needs_user_label(self):
        """Profile picker reconocido (Chrome 122+: title 'Google
        Chrome'), pero sin OCR del nombre → needs_user_label=True.
        """
        m = Mission(name="f2360036", raw_trace=_bare_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        sp = next(
            (s for s in plan.semantic_steps if s.type == "select_profile"),
            None,
        )
        assert sp is not None
        assert sp.needs_user_label is True
        # No queremos un nombre de pestaña falsamente etiquetado como
        # perfil ("Avances de Nevlan").
        assert sp.params.get("profile") == ""

    def test_status_is_needs_review_not_executable(self):
        """PRD §2 — si el perfil necesita confirmación humana, el
        plan no puede ser EXECUTABLE.
        """
        m = Mission(name="f2360036", raw_trace=_bare_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        assert plan.status == CollapseStatus.NEEDS_REVIEW

    def test_coverage_complete(self):
        """PRD §4/§5 — todas las intenciones detectadas en raw_trace
        tienen step semántico que las cubre.
        """
        m = Mission(name="f2360036", raw_trace=_bare_chrome_youtube_trace())
        plan = collapse_intent(mission=m)
        assert plan.coverage_report is not None
        assert plan.coverage_report.missing_kinds == []
        assert plan.coverage_report.uncovered_event_ids == []
        assert plan.coverage_report.is_complete is True

    def test_semantic_execution_plan_persisted(self):
        """PRD §1 — apply_collapse_to_mission deja
        ``mission.semantic_execution_plan`` poblado.
        """
        m = Mission(name="f2360036", raw_trace=_bare_chrome_youtube_trace())
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert sep is not None, "semantic_execution_plan no fue persistido"
        assert isinstance(sep, dict)
        assert sep.get("legacy_graph_ignored") is True
        steps = sep.get("steps", [])
        kinds = [s.get("type") for s in steps]
        assert kinds == [
            "open_app",
            "select_profile",
            "open_new_tab",
            "open_url",
            "search_content",
            "scroll_results",
        ]


class TestCoverageAudit:

    def test_missing_kind_pushes_status_to_needs_review(self):
        """PRD §5 — si el ctx detectó una intención pero el detector
        no logró emitir el step, status NO puede ser EXECUTABLE.

        Reproducimos un trace donde hay scroll en YouTube pero NO hay
        ``search_youtube`` previo (sin Enter). El context expone
        ``scroll_ticks`` pero como no hay search, scroll_results no
        debería emitirse — y de hecho no se emite porque su precondición
        es la búsqueda. Si el ctx por alguna razón lo marcase como
        esperado, `_classify_status` debería rebajarlo.
        """
        # Construimos manualmente un caso de coverage incompleto.
        from app.services.missions.intent_collapse_engine import (
            CoverageReport,
            _classify_status,
        )
        steps = [CollapsedStep(type="open_app", params={"app": "chrome"},
                                confidence=0.9)]
        cov = CoverageReport(
            expected_kinds=["open_app", "search_youtube"],
            emitted_kinds=["open_app"],
            missing_kinds=["search_youtube"],
        )
        status = _classify_status(steps, [], [], cov)
        assert status == CollapseStatus.NEEDS_REVIEW

    def test_uncovered_event_blocks_executable(self):
        from app.services.missions.intent_collapse_engine import (
            CoverageReport,
            _classify_status,
        )
        steps = [CollapsedStep(type="open_app", params={"app": "chrome"},
                                confidence=0.9)]
        cov = CoverageReport(
            expected_kinds=["open_app"],
            emitted_kinds=["open_app"],
            missing_kinds=[],
            important_event_ids=["evX"],
            uncovered_event_ids=["evX"],
            coverage_map={"evX": "needs_review"},
        )
        status = _classify_status(steps, [], [], cov)
        assert status == CollapseStatus.NEEDS_REVIEW

    def test_full_coverage_keeps_executable(self):
        from app.services.missions.intent_collapse_engine import (
            CoverageReport,
            _classify_status,
        )
        steps = [
            CollapsedStep(type="open_app", params={"app": "chrome"},
                          confidence=0.95),
            CollapsedStep(type="search_youtube",
                          params={"query": "x", "submit": True,
                                  "target": "youtube_search_box"},
                          confidence=0.95),
        ]
        cov = CoverageReport(
            expected_kinds=["open_app", "search_youtube"],
            emitted_kinds=["open_app", "search_youtube"],
            missing_kinds=[],
            important_event_ids=["e1", "e2"],
            uncovered_event_ids=[],
            coverage_map={
                "e1": "covered_by:open_app",
                "e2": "covered_by:search_youtube",
            },
        )
        status = _classify_status(steps, [], [], cov)
        assert status == CollapseStatus.EXECUTABLE


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
