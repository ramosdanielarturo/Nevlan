"""
ArthurOS Unit Tests — Field Editing Session (FASE 3 + FASE 12)
--------------------------------------------------------------
Verifica que la "Field Editing Session" se compila como UN solo paso de
texto con valor final limpio, sin importar cuántos backspaces/correcciones
haya intercalado el usuario.

Esto cubre el caso PRD §12 #1: "click en campo + escribir con errores y
backspaces + corregir + guardar" → SET_FIELD_VALUE con valor final limpio.
"""
from __future__ import annotations

from app.contracts.mission import (
    Mission, RawEvent, EventType, MouseAction, KeyboardAction,
    ActionStrategy,
)
from app.services.missions.compiler import MissionCompiler


def _click_edit(uia_aid: str = "email", uia_name: str = "Email") -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=100, y=200, button="left"),
        metadata={"uia": {
            "name": uia_name,
            "automation_id": uia_aid,
            "control_type": "EditControl",
            "class_name": "Edit",
        }},
    )


def _key(*keys: str, modifiers: list[str] | None = None) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(
            keys=list(keys), modifiers=modifiers or []
        ),
    )


def _backspace() -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=["Key.backspace"]),
    )


def _compile(events: list[RawEvent]) -> Mission:
    m = Mission(name="t")
    m.raw_trace = events
    return MissionCompiler.compile_mission(m)


# ──────────────────────────────────────────────────────────────────
# Compilación de la "field session"
# ──────────────────────────────────────────────────────────────────

class TestFieldSessionCompile:
    def test_typing_with_backspaces_collapses_to_clean_final_text(self):
        # Simulamos un humano que escribe "danil" → backspace → "el"
        # → resultado final esperado: "daniel"
        events = [
            _click_edit(),
            _key("d"), _key("a"), _key("n"), _key("i"), _key("l"),
            _backspace(),  # borra la "l"
            _key("e"), _key("l"),
            _key("Key.tab"),  # cierra la sesión
        ]
        m = _compile(events)
        graph = m.compiled_execution_graph

        # Debe existir un paso de SET_FIELD_VALUE/TYPE_TEXT con texto final
        # = "daniel" (no "danillel" ni "danil").
        text_step = next(
            (s for s in graph
             if s.action_strategy in (
                 ActionStrategy.SET_FIELD_VALUE, ActionStrategy.TYPE_TEXT
             )),
            None,
        )
        assert text_step is not None, \
            "Debería existir un SET_FIELD_VALUE/TYPE_TEXT con valor final"
        actual = (text_step.action_payload or {}).get("text") or ""
        assert actual == "daniel", \
            f"Esperaba 'daniel', obtuve {actual!r}"

    def test_typing_then_full_delete_drops_step(self):
        # Si el usuario escribe y borra todo, el compiler NO debería
        # emitir un paso vacío.
        events = [
            _click_edit(),
            _key("a"), _key("b"), _key("c"),
            _backspace(), _backspace(), _backspace(),
            _key("Key.tab"),
        ]
        m = _compile(events)
        graph = m.compiled_execution_graph

        # No debe existir ningún paso de texto con texto vacío.
        for s in graph:
            if s.action_strategy in (
                ActionStrategy.SET_FIELD_VALUE, ActionStrategy.TYPE_TEXT
            ):
                text = (s.action_payload or {}).get("text") or ""
                assert text != "", \
                    "No debe quedar un paso de texto vacío en el grafo"

    def test_click_field_plus_text_becomes_set_field_value(self):
        # Click sobre EditControl + texto → debe fusionarse a
        # SET_FIELD_VALUE (no debe quedar como CLICK + TYPE_TEXT separados).
        events = [
            _click_edit("searchInput", "Buscar"),
            _key("h"), _key("o"), _key("l"), _key("a"),
            _key("Key.tab"),
        ]
        m = _compile(events)
        graph = m.compiled_execution_graph

        sfv = [s for s in graph
               if s.action_strategy == ActionStrategy.SET_FIELD_VALUE]
        assert len(sfv) >= 1, \
            f"Esperaba al menos un SET_FIELD_VALUE, obtuve {[s.action_strategy for s in graph]}"
        assert sfv[0].action_payload.get("text") == "hola"

    def test_no_typewrite_when_set_field_value_available(self):
        """PRD §12 #7: si hay fill/setvalue/paste, no debe usarse typewrite.
        En el grafo compilado, una sesión sobre EditControl debe terminar
        siendo SET_FIELD_VALUE (rapida vía locator.fill/ValuePattern), no
        un TYPE_TEXT crudo (que en player V3 cae a typewrite tecla por tecla).
        """
        events = [
            _click_edit("nameInput", "Nombre"),
            _key("D"), _key("A"), _key("N"), _key("I"), _key("E"), _key("L"),
            _key("Key.enter"),
        ]
        m = _compile(events)
        graph = m.compiled_execution_graph

        has_set_field_value = any(
            s.action_strategy == ActionStrategy.SET_FIELD_VALUE for s in graph
        )
        # Aceptamos también NAVIGATE_OR_SEARCH/SUBMIT_SEARCH si el patrón A/B
        # se disparó: en ambos casos el player usa fill, no typewrite.
        has_fast_form = any(
            s.action_strategy in (
                ActionStrategy.NAVIGATE_OR_SEARCH,
                ActionStrategy.SUBMIT_SEARCH,
            )
            for s in graph
        )
        assert has_set_field_value or has_fast_form, \
            f"Esperaba SET_FIELD_VALUE / NAVIGATE_OR_SEARCH / SUBMIT_SEARCH, "\
            f"obtuve {[s.action_strategy for s in graph]}"
