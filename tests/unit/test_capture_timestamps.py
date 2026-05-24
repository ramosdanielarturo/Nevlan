"""Tests para captura de timestamp/sequence_id REAL (antes de UIA/screenshot).

Bug pre-2026: ``MouseListener._process_click`` corre en un thread y
asignaba ``timestamp = datetime.now()`` DESPUÉS de UIA/screenshot/web.
Eso retrasaba el timestamp 100-300ms y podía poner un click DESPUÉS
de un carácter tipeado, partiendo "Chrome" en "C" + click + "hrome".

Comportamiento esperado:
  - El callback ``on_click`` de pynput captura ``capture_ts`` y
    ``capture_seq`` ANTES de lanzar el thread de procesamiento.
  - Esos valores se pasan al ``_process_click`` y al RawEvent.
  - Las metadata incluye ``processed_timestamp`` y ``listener_type``
    para diagnóstico (modo experto/debug).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.contracts.mission import (
    EventType, KeyboardAction, Mission, MouseAction, RawEvent, WindowContext,
)
from app.services.missions.compiler import MissionCompiler


class TestProcessClickRespectsCaptureTimestamp:
    """Si pasamos ``capture_ts`` al ``_process_click``, ese es el
    timestamp del RawEvent — no ``datetime.now()`` posterior.
    """

    def test_process_click_signature_accepts_capture_args(self):
        """Compatibilidad: la firma acepta capture_ts y capture_seq."""
        import inspect
        from app.services.missions.recorder import MouseListener

        sig = inspect.signature(MouseListener._process_click)
        names = list(sig.parameters.keys())
        assert "capture_ts" in names, (
            f"_process_click debe aceptar capture_ts. Firma: {names}"
        )
        assert "capture_seq" in names, (
            f"_process_click debe aceptar capture_seq. Firma: {names}"
        )

    def test_process_drag_signature_accepts_capture_args(self):
        import inspect
        from app.services.missions.recorder import MouseListener

        sig = inspect.signature(MouseListener._process_drag)
        names = list(sig.parameters.keys())
        assert "capture_ts" in names
        assert "capture_seq" in names


class TestCompilerSortsByTimestamp:
    """Si los eventos llegan al raw_trace fuera de orden por threading,
    el compiler los ordena por timestamp+sequence_id antes de compilar.
    """

    def test_click_before_typing_kept_when_appended_late(self):
        """Caso real (bug pre-sprint):
            t=50ms: usuario hace click en barra de búsqueda
            t=100ms: usuario teclea "c"
            t=120ms: usuario teclea "h"
            ...

        El thread del click hace UIA pesado y tarda 200ms en crear
        el RawEvent. Si ``_process_click`` asignara el timestamp en
        ese momento, el click quedaría DESPUÉS de las teclas y el
        texto "chrome" aparecería ANTES del click — destrozando el
        patrón Windows Search.

        Hoy el listener captura ``capture_ts`` ANTES del thread, así
        que el click conserva timestamp=50ms aunque llegue al raw_trace
        después. Verificamos que el compiler ordena por timestamp.
        """
        base = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)

        def k(ch: str, ms: int, seq: int) -> RawEvent:
            return RawEvent(
                event_type=EventType.KEYBOARD_KEY_PRESS,
                keyboard_action=KeyboardAction(keys=[ch], modifiers=[]),
                window_context=WindowContext(process_name="searchhost.exe"),
                timestamp=base + timedelta(milliseconds=ms),
                sequence_id=seq,
            )

        click = RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=400, y=400),
            window_context=WindowContext(process_name="searchhost.exe"),
            # Timestamp REAL del click: 50ms (ANTES de teclas).
            timestamp=base + timedelta(milliseconds=50),
            sequence_id=1,
            metadata={
                "uia": {
                    "name": "Escribe aquí para buscar",
                    "control_type": "EditControl",
                },
            },
        )

        # raw_trace DESORDENADO: las teclas llegaron primero porque
        # el click se demoró 200ms en procesarse (UIA + screenshot).
        events = [
            k("c", 100, 2),
            k("h", 120, 3),
            k("r", 140, 4),
            k("o", 180, 5),
            k("m", 200, 6),
            k("e", 220, 7),
            click,             # se procesó tarde, pero timestamp=50ms
        ]
        m = Mission(name="late_click", raw_trace=events)
        MissionCompiler.compile_mission(m)

        # El compiler ordena por timestamp → click primero, luego
        # "chrome" como una sola sesión de texto. El patrón Windows
        # Search → LAUNCH_APP debe colapsar todo.
        from app.contracts.mission import ActionStrategy
        strats = [c.action_strategy for c in m.compiled_execution_graph]
        assert ActionStrategy.LAUNCH_APP in strats, (
            f"Esperado LAUNCH_APP. Obtenido: {[s.value for s in strats]}"
        )
        # Y NUNCA un fragmento parcial:
        for c in m.compiled_execution_graph:
            text = ((c.action_payload or {}).get("text") or "").lower()
            raw = ((c.action_payload or {}).get("raw_typed") or "").lower()
            assert text not in ("c", "ch", "chr", "o", "om", "ome"), (
                f"Fragmento en TYPE_TEXT: '{text}'"
            )
            assert raw not in ("c", "ch", "chr", "o", "om", "ome"), (
                f"Fragmento en LAUNCH_APP raw_typed: '{raw}'"
            )

    def test_compiler_pre_sort_uses_sequence_id_as_tiebreaker(self):
        """Si dos eventos tienen el mismo timestamp, sequence_id
        rompe el empate.
        """
        base = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)

        def kb(ch: str, ms: int, seq: int) -> RawEvent:
            return RawEvent(
                event_type=EventType.KEYBOARD_KEY_PRESS,
                keyboard_action=KeyboardAction(keys=[ch], modifiers=[]),
                window_context=WindowContext(process_name="x.exe"),
                timestamp=base + timedelta(milliseconds=ms),
                sequence_id=seq,
            )

        # Mismo timestamp pero sequence diferente:
        events = [
            kb("b", 100, 2),  # llega primero pero seq=2
            kb("a", 100, 1),  # llega segundo pero seq=1
        ]
        m = Mission(name="tiebreak", raw_trace=events)
        MissionCompiler.compile_mission(m)

        text = ""
        for c in m.compiled_execution_graph:
            text += (c.action_payload or {}).get("text", "")
        assert text == "ab", (
            f"Esperado 'ab' (sequence_id como tie-breaker), obtuvo '{text}'"
        )
