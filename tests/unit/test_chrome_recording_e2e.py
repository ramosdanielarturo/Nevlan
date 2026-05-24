"""End-to-end del bug Chrome / Windows Search.

Cubre los seis casos del PRD §1:
  1. Test E2E sintético: clic+chrome+enter → 1-2 pasos LAUNCH_APP=Chrome.
  2. Eventos fuera de orden por threading → compiler reordena por timestamp.
  3. URL interna leída por la app vs buffer del usuario → gana buffer.
  4. Búsqueda en inglés "Type here to search" → app_name=Chrome.
  5. Búsqueda en español "Escribe aquí para buscar" → app_name=Chrome.
  6. Búsqueda sin Enter pero con paso siguiente del proceso target.

NUNCA debe quedar:
  - "Escribir 'chro'" + "Escribir 'me'"
  - "Click en 'Escribe aquí para buscar'" suelto
  - "Escribir 'chrome://profile-picker/'"
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from app.contracts.mission import (
    Mission, RawEvent, EventType, MissionStatus, ActionStrategy,
    KeyboardAction, MouseAction, WindowContext, CompiledStep,
    TargetContext, InterpretedStep,
)
from app.services.missions.compiler import MissionCompiler
from app.services.missions.field_commit import (
    FieldCommitResult, BROWSER_INTERNAL_URL_PREFIXES,
    is_browser_internal_url,
    READ_SOURCE_KEYBOARD_URL_SANITIZED,
)


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _ts(base: datetime, ms: int) -> datetime:
    return base + timedelta(milliseconds=ms)


def _click(
    *, x: int = 200, y: int = 1000,
    name: str, ct: str = "EditControl",
    process_name: str = "searchhost.exe",
    ts: datetime, sequence_id: int,
) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=x, y=y),
        window_context=WindowContext(
            title="Search", process_name=process_name,
        ),
        timestamp=ts,
        sequence_id=sequence_id,
        metadata={
            "uia": {
                "name": name,
                "control_type": ct,
                "automation_id": "SearchTextBox",
            },
        },
    )


def _key(
    char: str, *, ts: datetime, sequence_id: int,
    process_name: str = "searchhost.exe",
) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=[char], modifiers=[]),
        window_context=WindowContext(process_name=process_name),
        timestamp=ts,
        sequence_id=sequence_id,
    )


def _enter(*, ts: datetime, sequence_id: int) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
        window_context=WindowContext(process_name="searchhost.exe"),
        timestamp=ts,
        sequence_id=sequence_id,
    )


def _compile(events: List[RawEvent]) -> Mission:
    m = Mission(name="chrome_e2e", status=MissionStatus.RECORDING)
    m.raw_trace = list(events)
    return MissionCompiler.compile_mission(m)


# ──────────────────────────────────────────────────────────────────
# 1. End-to-end sintético: 4 raw events → LAUNCH_APP Chrome
# ──────────────────────────────────────────────────────────────────

class TestEndToEndLaunchChrome:
    def _build_trace(self, search_name: str) -> List[RawEvent]:
        t0 = datetime.now(timezone.utc)
        events: List[RawEvent] = []
        seq = 0

        seq += 1
        events.append(_click(name=search_name, ts=_ts(t0, 0), sequence_id=seq))
        for i, ch in enumerate("chrome"):
            seq += 1
            events.append(_key(ch, ts=_ts(t0, 50 + i * 30), sequence_id=seq))
        seq += 1
        events.append(_enter(ts=_ts(t0, 350), sequence_id=seq))
        return events

    def _assert_launch_chrome_clean(self, m: Mission) -> None:
        graph = m.compiled_execution_graph
        assert 1 <= len(graph) <= 2, (
            f"Esperaba 1–2 pasos humanos, obtuve {len(graph)}: "
            f"{[s.action_strategy for s in graph]}"
        )
        launch = [s for s in graph
                  if s.action_strategy == ActionStrategy.LAUNCH_APP]
        assert launch, (
            f"Falta LAUNCH_APP. Estrategias presentes: "
            f"{[s.action_strategy for s in graph]}"
        )
        app_name = (launch[0].action_payload or {}).get("app_name", "")
        assert app_name == "Chrome", f"app_name='{app_name}'"

        # Descripción contiene "Abrir Chrome".
        descs = " | ".join(s.description for s in m.interpreted_steps)
        assert "Abrir Chrome" in descs, descs

        # No hay basura técnica.
        for s in graph:
            text = (s.action_payload or {}).get("text", "") or ""
            assert "chrome://profile-picker" not in text.lower(), text
            assert text.lower() not in ("chro", "me"), text

    def test_spanish_search_box(self):
        events = self._build_trace("Escribe aquí para buscar")
        m = _compile(events)
        self._assert_launch_chrome_clean(m)

    def test_english_search_box(self):
        events = self._build_trace("Type here to search")
        m = _compile(events)
        self._assert_launch_chrome_clean(m)


# ──────────────────────────────────────────────────────────────────
# 2. Eventos fuera de orden — threading
# ──────────────────────────────────────────────────────────────────

class TestOutOfOrderEvents:
    def test_keyboard_arrives_before_click_compiler_reorders(self):
        """Llegan teclas antes que el click en la lista, pero el click
        TIENE timestamp anterior (es decir, ocurrió antes y luego pynput
        lo entregó tarde). El compiler debe reordenar por timestamp.
        """
        t0 = datetime.now(timezone.utc)

        # Reproducir bug: en el trace real, las teclas (capturadas por el
        # thread de teclado) ya están en el buffer antes que el click
        # (capturado por el thread de mouse). Sus ``timestamp`` reales SÍ
        # están en orden correcto pero la LISTA llega cruzada.
        events: List[RawEvent] = []
        seq = 0
        for i, ch in enumerate("chrome"):
            seq += 1
            # Teclas con timestamps en t=100..225ms.
            events.append(_key(ch, ts=_ts(t0, 100 + i * 25),
                               sequence_id=seq))
        seq += 1
        events.append(_enter(ts=_ts(t0, 320), sequence_id=seq))
        seq += 1
        # Ahora añadimos el click pero con timestamp PREVIO (t=0) — esa
        # es la situación que rompe sin pre-sort.
        events.append(_click(name="Type here to search",
                             ts=_ts(t0, 0), sequence_id=seq))

        # Sanity: el orden de captura (lista) NO está ordenado por ts.
        order_in = [e.timestamp for e in events]
        assert order_in != sorted(order_in), order_in

        m = _compile(events)
        order_out = [e.timestamp for e in m.raw_trace]
        assert order_out == sorted(order_out), (
            "Compiler debe reordenar por timestamp"
        )

        graph = m.compiled_execution_graph
        launch = [s for s in graph
                  if s.action_strategy == ActionStrategy.LAUNCH_APP]
        assert launch, [s.action_strategy for s in graph]
        assert (launch[0].action_payload or {}).get("app_name") == "Chrome"

    def test_sequence_id_breaks_timestamp_tie(self):
        """Eventos con el MISMO timestamp se rompen por sequence_id."""
        t = datetime.now(timezone.utc)
        click = _click(name="Type here to search", ts=t, sequence_id=2)
        # Tecla con MISMO ts pero sequence_id 1 (capturada antes del click
        # según el contador). El sort debe ponerla primero — pero la
        # lógica de búsqueda es: click → text → enter. Lo que probamos
        # es que el sort SE MANTIENE estable y NO crashea.
        key = _key("a", ts=t, sequence_id=1)
        m = _compile([click, key])
        # Con esos timestamps iguales, sequence_id determina el orden.
        # No nos importa el contenido del compile; solo la estabilidad.
        assert m.raw_trace[0].sequence_id == 1
        assert m.raw_trace[1].sequence_id == 2


# ──────────────────────────────────────────────────────────────────
# 3. URL interna leída vs buffer
# ──────────────────────────────────────────────────────────────────

class TestUrlInternalSanity:
    def test_chrome_profile_picker_is_blocked(self):
        assert is_browser_internal_url("chrome://profile-picker/")
        assert is_browser_internal_url("CHROME://profile-picker")
        assert is_browser_internal_url("edge://settings/")
        assert is_browser_internal_url("about:blank")
        assert is_browser_internal_url("chrome-extension://abc")
        assert is_browser_internal_url("file:///C:/foo")
        assert not is_browser_internal_url("https://google.com")
        assert not is_browser_internal_url("Chrome")
        assert not is_browser_internal_url("")
        assert not is_browser_internal_url(None)

    def test_field_commit_chooses_buffer_over_internal_url(self):
        """Si la app devuelve chrome:// y el buffer es 'Chrome', gana 'Chrome'."""
        from app.services.missions.recorder import MissionRecorder

        rec = MissionRecorder("t")
        rec._field_session_active = True
        rec._field_session_label = "Buscar"
        rec._field_session_buf = list("Chrome")
        rec._field_session_initial_value = ""
        rec._field_session_last_known_value = "chrome://profile-picker/"
        rec._field_session_target_meta = {
            "name": "Buscar", "control_type": "EditControl",
            "automation_id": "SearchBox",
        }

        # El test no puede leer UIA real; simulamos lectura "URL interna".
        rec._read_field_value_now = lambda: "chrome://profile-picker/"  # type: ignore

        fc = rec._build_field_commit_result(prefer_uia=True)
        assert fc is not None
        assert fc.final_text == "Chrome", fc.final_text
        assert fc.read_source == READ_SOURCE_KEYBOARD_URL_SANITIZED
        assert fc.user_typed_text == "Chrome"
        assert fc.final_field_value == "chrome://profile-picker/"
        assert fc.source_is_url is False  # ya descartada
        assert fc.chosen_final_text == "Chrome"
        assert "url_internal" in fc.reason


# ──────────────────────────────────────────────────────────────────
# 4. Búsqueda sin Enter pero proceso target detectado
# ──────────────────────────────────────────────────────────────────

class TestSearchWithoutEnterButProcessActivated:
    def test_collapses_when_known_alias(self):
        """Si la app es conocida (chrome), colapsa sin Enter."""
        from app.services.missions.compiler import MissionCompiler

        steps = [
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                target_context=TargetContext(
                    uia_data={"name": "Type here to search",
                              "control_type": "EditControl"},
                    process_name="searchhost.exe",
                ),
                action_payload={},
            ),
            CompiledStep(
                action_strategy=ActionStrategy.TYPE_TEXT,
                target_context=TargetContext(),
                action_payload={"text": "Chrome"},
            ),
            # NO hay Enter. Pero sí un click cuyo proceso es chrome.exe.
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                target_context=TargetContext(
                    process_name="chrome.exe",
                ),
                action_payload={},
            ),
        ]
        interp = [
            InterpretedStep(description="Click en Buscar", raw_event_ids=[]),
            InterpretedStep(description="Escribir Chrome", raw_event_ids=[]),
            InterpretedStep(description="Click en resultado", raw_event_ids=[]),
        ]

        out_c, out_i = MissionCompiler._post_pass_windows_search_launch(
            steps, interp,
        )

        assert out_c[0].action_strategy == ActionStrategy.LAUNCH_APP
        assert (out_c[0].action_payload or {}).get("app_name") == "Chrome"
        # process_confirmed se marca cuando el proceso siguiente matchea.
        assert (out_c[0].action_payload or {}).get("process_confirmed") is True

    def test_no_collapse_when_unknown_alias_and_no_enter_no_process(self):
        """Si no hay Enter, ni alias conocido, ni proceso confirmado, NO colapsa."""
        from app.services.missions.compiler import MissionCompiler

        steps = [
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                target_context=TargetContext(
                    uia_data={"name": "Type here to search",
                              "control_type": "EditControl"},
                    process_name="searchhost.exe",
                ),
                action_payload={},
            ),
            CompiledStep(
                action_strategy=ActionStrategy.TYPE_TEXT,
                target_context=TargetContext(),
                action_payload={"text": "AppQueNoExiste"},
            ),
        ]
        interp = [
            InterpretedStep(description="Click", raw_event_ids=[]),
            InterpretedStep(description="Escribir", raw_event_ids=[]),
        ]

        out_c, _ = MissionCompiler._post_pass_windows_search_launch(
            steps, interp,
        )
        # Sin Enter ni proceso target → permanece como estaba.
        assert len(out_c) == 2
        assert out_c[0].action_strategy == ActionStrategy.CLICK


# ──────────────────────────────────────────────────────────────────
# 5. La firma de la promesa: nunca muestres microeventos
# ──────────────────────────────────────────────────────────────────

class TestNoMicroeventLeaks:
    def test_compiled_steps_dont_split_chrome(self):
        t0 = datetime.now(timezone.utc)
        seq = 0
        events: List[RawEvent] = []
        seq += 1
        events.append(_click(name="Type here to search",
                             ts=_ts(t0, 0), sequence_id=seq))
        for i, ch in enumerate("chrome"):
            seq += 1
            events.append(_key(ch, ts=_ts(t0, 50 + i * 30),
                               sequence_id=seq))
        seq += 1
        events.append(_enter(ts=_ts(t0, 250), sequence_id=seq))

        m = _compile(events)
        descs = [s.description for s in m.interpreted_steps]
        joined = " | ".join(descs).lower()
        assert "chro" not in joined.replace("chrome", "X")
        assert "chrome://" not in joined
