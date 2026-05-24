"""E2E del caso real reportado el 2026-05-03 — Chrome + YouTube.

ACCIÓN HUMANA:
    1. Click en barra buscadora de Windows ("Escribe para buscar")
    2. Escribir "Chrome" (con CapsLock × 2 antes y entre letras)
    3. Click en icono de Chrome
    4. Click en perfil "Daniel Arturo Ramos"
    5. Click en "Nueva pestaña"
    6. Click en icono de YouTube
    7. Click en barra buscadora de YouTube (placeholder "Buscar")
    8. Escribir "devuelveme el amor luis miguel"
    9. Enter

CÓMO SE GRABABA MAL (síntomas reales del log del usuario):
    - "Click en 'Escribe aquí para buscar.'"
    - "Presionar CAPSLOCK × 2"           ← BASURA
    - "Escribir 'C'"                     ← FRAGMENTO
    - "Click en 'Google Chrome'"
    - "Escribir 'hrome'"                 ← FRAGMENTO
    - "Click en 'ArthurOS 2026.docx'"
    - "Click en 'Nueva pestaña'"
    - "Click en 'Youtube'"
    - "Escribir 'https://www.youtube.com/'"  ← URL FALSA, no escrita
    - "Presionar CAPSLOCK"               ← BASURA
    - "Click en 'guide-service'"         ← LABEL TÉCNICO
    - "Presionar CAPSLOCK"               ← BASURA
    - "Escribir 'D'"                     ← FRAGMENTO
    - "Escribir 'Devuelveme el amor'"    ← FRAGMENTO
    - "Escribir 'Devuelveme el amor L'"  ← FRAGMENTO
    - "Escribir 'Devuelveme el amor Luis'"
    - "Escribir 'Devuelveme el amor Luis M'"
    - "Confirmar Enter"
    - "Escribir 'devuelveme el amor luis miguel'"  ← FINAL desordenado

RESULTADO ESPERADO TRAS ESTE SPRINT:
    - Abrir Chrome (o equivalente: clic Win Search + texto Chrome → LAUNCH_APP)
    - Click en perfil "Daniel Arturo Ramos" o paso humano equivalente
    - Click en "Nueva pestaña"
    - Click en YouTube
    - Buscar en YouTube "devuelveme el amor luis miguel"

NUNCA en modo normal:
    - CAPSLOCK como paso visible
    - https://www.youtube.com/ como TYPE_TEXT
    - guide-service como label
    - fragmentos partidos de "Chrome" / "Devuelveme..."
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from app.contracts.mission import (
    ActionStrategy, EventType, KeyboardAction, Mission, MouseAction,
    RawEvent, WindowContext,
)
from app.services.missions.compiler import MissionCompiler


def _ts(base: datetime, ms: int) -> datetime:
    return base + timedelta(milliseconds=ms)


# ── Helpers ────────────────────────────────────────────────────────


def _click_winsearch(ts: datetime, seq: int) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=200, y=1050),
        window_context=WindowContext(
            title="Búsqueda", process_name="searchhost.exe",
        ),
        timestamp=ts,
        sequence_id=seq,
        metadata={
            "uia": {
                "name": "Escribe aquí para buscar",
                "control_type": "EditControl",
                "automation_id": "SearchTextBox",
            },
        },
    )


def _key(char: str, ts: datetime, seq: int,
         process: str = "searchhost.exe",
         capslock_on: bool = False,
         shift: bool = False) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(
            keys=[char], modifiers=(["shift"] if shift else []),
        ),
        window_context=WindowContext(process_name=process),
        timestamp=ts,
        sequence_id=seq,
        metadata={"capslock_on": capslock_on},
    )


def _capslock(ts: datetime, seq: int) -> RawEvent:
    """RawEvent legacy de CapsLock — el listener nuevo NO lo emite, pero
    grabaciones antiguas pueden tenerlo. El compiler debe absorberlo.
    """
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=["capslock"], modifiers=[]),
        window_context=WindowContext(process_name="searchhost.exe"),
        timestamp=ts,
        sequence_id=seq,
    )


def _enter(ts: datetime, seq: int,
           process: str = "chrome.exe") -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
        window_context=WindowContext(process_name=process),
        timestamp=ts,
        sequence_id=seq,
    )


def _click_chrome_result(ts: datetime, seq: int) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=400, y=400),
        window_context=WindowContext(
            title="Búsqueda", process_name="searchhost.exe",
        ),
        timestamp=ts,
        sequence_id=seq,
        metadata={
            "uia": {
                "name": "Google Chrome, Aplicación de escritorio",
                "control_type": "ListItemControl",
            },
        },
    )


def _click_chrome_window(ts: datetime, seq: int, title: str) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=600, y=300),
        window_context=WindowContext(title=title, process_name="chrome.exe"),
        timestamp=ts,
        sequence_id=seq,
        metadata={"uia": {"name": title, "control_type": "ButtonControl"}},
    )


def _click_youtube_search_field(ts: datetime, seq: int) -> RawEvent:
    """Simula click en el campo de búsqueda de YouTube. El UIA name
    captura la podredumbre técnica ``guide-service`` (componente
    Polymer interno), pero el web data tiene placeholder=Buscar.
    """
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=900, y=120),
        window_context=WindowContext(
            title="YouTube - Google Chrome", process_name="chrome.exe",
        ),
        timestamp=ts,
        sequence_id=seq,
        metadata={
            "uia": {
                "name": "guide-service",  # ← BASURA
                "control_type": "EditControl",
            },
            "web": {
                "url": "https://www.youtube.com/",
                "domain": "youtube.com",
                "role": "searchbox",
                "placeholder": "Buscar",
                "label": "",
                "tag": "input",
                "type": "search",
                "input_value_at_capture": "",
                "locators": [
                    {"kind": "role", "role": "searchbox", "name": "Buscar"},
                ],
                "bbox": {"left": 800, "top": 100, "width": 400, "height": 40},
            },
        },
    )


def _click_youtube_icon_in_new_tab(ts: datetime, seq: int) -> RawEvent:
    """El usuario hace click en el icono de YouTube en una nueva pestaña.
    Esto NO debe traducirse en TYPE_TEXT con la URL del navegador
    — bug 2026-05-03 donde aparecía 'Escribir https://www.youtube.com/'.
    """
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=300, y=400),
        window_context=WindowContext(
            title="Nueva pestaña - Google Chrome", process_name="chrome.exe",
        ),
        timestamp=ts,
        sequence_id=seq,
        metadata={
            "uia": {"name": "Youtube", "control_type": "ButtonControl"},
            "web": {
                "url": "https://www.google.com/",
                "domain": "google.com",
                "role": "link",
                "text": "YouTube",
                "href": "https://www.youtube.com/",
                "tag": "a",
                "locators": [
                    {"kind": "role", "role": "link", "name": "YouTube"},
                ],
            },
        },
    )


# ── Test sintético del caso completo ───────────────────────────────


class TestRealRecordingChromeYoutube:
    def _build_raw_trace(self) -> List[RawEvent]:
        """Construye el trace EXACTO del caso real reportado, incluido
        el ruido de CapsLock y URLs falsas.
        """
        base = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)
        seq = [0]

        def nx() -> int:
            seq[0] += 1
            return seq[0]

        def at(ms: int) -> datetime:
            return _ts(base, ms)

        events: List[RawEvent] = []

        # 1) Click en Windows Search
        events.append(_click_winsearch(at(0), nx()))
        # 2) CapsLock × 2 (cancelan, casing OFF)
        events.append(_capslock(at(50), nx()))
        events.append(_capslock(at(60), nx()))
        # 3) Escribir "Chrome" — típicamente el shift de la C ya da
        #    mayúscula. Pero simulamos el caso real donde se grabó
        #    una sesión de texto que se fragmentó por CapsLock legacy.
        for i, ch in enumerate("Chrome"):
            events.append(_key(ch.lower(), at(100 + i * 30), nx(),
                               capslock_on=False))
        # 4) Click en resultado Chrome
        events.append(_click_chrome_result(at(500), nx()))
        # 5) Click ventana Chrome (perfil Daniel)
        events.append(_click_chrome_window(at(900), nx(),
                                            "Daniel Arturo Ramos - Chrome"))
        # 6) Click en Nueva pestaña
        events.append(_click_chrome_window(at(1500), nx(),
                                            "Nueva pestaña - Google Chrome"))
        # 7) Click en YouTube icono (NO debe generar TYPE_TEXT con URL)
        events.append(_click_youtube_icon_in_new_tab(at(2000), nx()))
        # 8) Click en search field de YouTube (UIA name=guide-service)
        events.append(_click_youtube_search_field(at(2500), nx()))
        # 9) CapsLock entre fragmentos (caso real reportado)
        events.append(_capslock(at(2550), nx()))
        # 10) Escribir "devuelveme el amor luis miguel"
        for i, ch in enumerate("devuelveme el amor luis miguel"):
            events.append(_key(ch, at(2600 + i * 30), nx(),
                               process="chrome.exe", capslock_on=False))
        # 11) Enter para enviar búsqueda
        events.append(_enter(at(3500), nx(), process="chrome.exe"))
        return events

    def test_capslock_never_appears_as_step(self):
        """En modo normal, CapsLock NUNCA aparece como paso."""
        m = Mission(name="real_case", raw_trace=self._build_raw_trace())
        MissionCompiler.compile_mission(m)

        for c, i in zip(m.compiled_execution_graph, m.interpreted_steps):
            desc_lo = (i.description or "").lower()
            assert "capslock" not in desc_lo, (
                f"CapsLock NO debe aparecer en descripciones humanas. "
                f"Encontrado en: {i.description}"
            )
            # En el payload tampoco como hotkey:
            if c.action_strategy == ActionStrategy.SEND_HOTKEY:
                pl = c.action_payload or {}
                key = (pl.get("hotkey") or pl.get("key") or "").lower()
                keys = [str(k).lower() for k in (pl.get("keys") or [])]
                assert key != "capslock"
                assert "capslock" not in keys

    def test_chrome_text_not_split_by_capslock(self):
        """El texto 'chrome' NO debe quedar partido en 'C' + 'hrome'.

        El usuario tipeó CHROME completo. Aunque entre las letras haya
        toggles legacy de CapsLock, el compilador debe absorberlos y
        producir un único TYPE_TEXT (que después puede convertirse en
        LAUNCH_APP por el patrón Windows Search).
        """
        m = Mission(name="real_case", raw_trace=self._build_raw_trace())
        MissionCompiler.compile_mission(m)

        # Buscamos cualquier rastro de fragmentos:
        for c in m.compiled_execution_graph:
            if c.action_strategy in (
                ActionStrategy.TYPE_TEXT, ActionStrategy.SET_FIELD_VALUE,
                ActionStrategy.LAUNCH_APP,
            ):
                pl = c.action_payload or {}
                t = (pl.get("text") or pl.get("raw_typed") or "").lower()
                # No debe haber un paso cuyo texto sea SOLO un fragmento:
                assert t not in ("c", "hrome", "h", "rome", "ome"), (
                    f"Fragmento detectado: '{t}' (action={c.action_strategy})"
                )

    def test_winsearch_chrome_collapses_to_launch_app(self):
        """El patrón click WinSearch + chrome + click resultado/Enter
        debe colapsar a LAUNCH_APP=Chrome.
        """
        m = Mission(name="real_case", raw_trace=self._build_raw_trace())
        MissionCompiler.compile_mission(m)
        strats = [c.action_strategy for c in m.compiled_execution_graph]
        assert ActionStrategy.LAUNCH_APP in strats, (
            "Faltó la detección Windows Search → LAUNCH_APP. "
            f"Estrategias: {[s.value for s in strats]}"
        )
        for c in m.compiled_execution_graph:
            if c.action_strategy == ActionStrategy.LAUNCH_APP:
                pl = c.action_payload or {}
                assert (pl.get("app_name") or "").lower().startswith("chrome")

    def test_no_url_typed_text_for_youtube_icon_click(self):
        """Click en icono de YouTube (sin escribir nada) NO debe generar
        un paso TYPE_TEXT con 'https://www.youtube.com/'. La URL viene
        de page.url, no es texto del usuario.
        """
        m = Mission(name="real_case", raw_trace=self._build_raw_trace())
        MissionCompiler.compile_mission(m)
        for c in m.compiled_execution_graph:
            if c.action_strategy in (
                ActionStrategy.TYPE_TEXT, ActionStrategy.SET_FIELD_VALUE,
            ):
                pl = c.action_payload or {}
                t = str(pl.get("text", "")).lower()
                assert not t.startswith("https://"), (
                    f"URL como texto escrito detectada: '{t}'"
                )
                assert not t.startswith("http://"), (
                    f"URL como texto escrito detectada: '{t}'"
                )
                assert "youtube.com" not in t or "devuelveme" in t, (
                    f"URL de YouTube en TYPE_TEXT no proviene del usuario: '{t}'"
                )

    def test_youtube_search_described_as_buscar_en_youtube(self):
        """La búsqueda final en YouTube debe leerse como 'Buscar en YouTube'
        en modo normal (no como 'Rellenar campo guide-service').
        """
        m = Mission(name="real_case", raw_trace=self._build_raw_trace())
        MissionCompiler.compile_mission(m)
        descriptions = [
            (i.description or "")
            for i in m.interpreted_steps
        ]
        joined = " | ".join(descriptions).lower()
        # No debe quedar "guide-service" visible en modo normal:
        assert "guide-service" not in joined, (
            f"'guide-service' (label técnico) NO debe aparecer en modo "
            f"normal. Pasos: {descriptions}"
        )
        # Debe haber al menos un paso con la intención de búsqueda en YouTube:
        has_youtube_search = any(
            "youtube" in d.lower() and (
                "buscar" in d.lower() or "search" in d.lower()
            )
            for d in descriptions
        )
        # Si no hay "Buscar en YouTube", al menos debe haber algún
        # texto final con la búsqueda completa, no fragmentos.
        if not has_youtube_search:
            text_steps = [
                str((c.action_payload or {}).get("text", ""))
                for c in m.compiled_execution_graph
                if c.action_strategy in (
                    ActionStrategy.TYPE_TEXT, ActionStrategy.SET_FIELD_VALUE,
                )
            ]
            full_text = "devuelveme el amor luis miguel"
            assert any(
                full_text in t.lower() for t in text_steps
            ), (
                f"Tampoco se encontró el texto completo de búsqueda. "
                f"Pasos texto: {text_steps}"
            )

    def test_no_partial_text_fragments(self):
        """No debe haber pasos consecutivos donde uno es prefijo del otro
        del mismo campo (síntoma de Field Editing Session fragmentada).
        """
        m = Mission(name="real_case", raw_trace=self._build_raw_trace())
        MissionCompiler.compile_mission(m)

        text_steps = [
            (i, c)
            for i, c in enumerate(m.compiled_execution_graph)
            if c.action_strategy in (
                ActionStrategy.TYPE_TEXT, ActionStrategy.SET_FIELD_VALUE,
            )
        ]
        # Para cada par consecutivo de TYPE_TEXT, no debe ser prefijo:
        for k in range(len(text_steps) - 1):
            i_a, a = text_steps[k]
            i_b, b = text_steps[k + 1]
            ta = str((a.action_payload or {}).get("text", ""))
            tb = str((b.action_payload or {}).get("text", ""))
            if not ta or not tb:
                continue
            # Solo cuenta como bug si están "cerca" (consecutivos en el flow):
            if i_b - i_a > 2:
                continue
            assert not (
                len(ta) < len(tb) and tb.lower().startswith(ta.lower())
            ), (
                f"Fragmento prefijo detectado: '{ta}' antes de '{tb}'. "
                "El post-pass collapse_partial_text debe absorberlos."
            )

    def test_step_count_is_reasonable(self):
        """El usuario hizo 8 acciones humanas. La grabación NO debe
        producir 20+ pasos como pasaba antes. Aceptamos hasta 12 (con
        algún margen) pero no más.
        """
        m = Mission(name="real_case", raw_trace=self._build_raw_trace())
        MissionCompiler.compile_mission(m)
        n = len(m.compiled_execution_graph)
        assert n <= 12, (
            f"La compilación generó {n} pasos. Demasiado ruido. "
            f"Pasos: {[s.value for s in [c.action_strategy for c in m.compiled_execution_graph]]}"
        )
