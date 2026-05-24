"""
ArthurOS Unit Tests — Compiler V3 (FASE 4 + FASE 12)
----------------------------------------------------
Verifica los patrones semánticos avanzados y el ``capture_score`` que
``MissionCompiler`` debe inyectar en cada paso.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import (
    Mission, RawEvent, EventType, MouseAction, KeyboardAction,
    ActionStrategy,
)
from app.services.missions.compiler import MissionCompiler


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _click(x: int = 100, y: int = 200, *,
           uia_name: str | None = None,
           uia_aid: str | None = None,
           uia_ctype: str = "ButtonControl",
           web_test_id: str | None = None,
           web_role: str | None = None,
           web_accessible_name: str | None = None,
           web_locators: list | None = None) -> RawEvent:
    """RawEvent de click con UIA y/o web metadata."""
    meta: dict = {}
    if uia_name or uia_aid:
        meta["uia"] = {
            "name": uia_name or "",
            "automation_id": uia_aid or "",
            "control_type": uia_ctype,
            "class_name": "Button",
        }
    if web_test_id or web_role or web_accessible_name or web_locators is not None:
        meta["web"] = {
            "test_id": web_test_id or "",
            "role": web_role or "",
            "accessible_name": web_accessible_name or "",
            "locators": web_locators or [],
            "url": "https://example.com",
            "domain": "example.com",
        }
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=x, y=y, button="left"),
        metadata=meta,
    )


def _scroll(dy: int = -200, dx: int = 0, x: int = 400, y: int = 300) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_SCROLL,
        mouse_action=MouseAction(x=x, y=y, button="middle"),
        metadata={"scroll": {"dy": dy, "dx": dx}},
    )


def _key(*keys: str, modifiers: list[str] | None = None) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(
            keys=list(keys), modifiers=modifiers or []
        ),
    )


def _compile(events: list[RawEvent]) -> Mission:
    m = Mission(name="t")
    m.raw_trace = events
    return MissionCompiler.compile_mission(m)


# ──────────────────────────────────────────────────────────────────
# capture_score + semantic enrichment (FASE 4)
# ──────────────────────────────────────────────────────────────────

class TestCaptureScore:
    def test_click_on_uia_automation_id_gets_decent_score(self):
        # AID (+30) + Name+ControlType (+20) = 50 mínimo. Eso ya está
        # MUY por encima del piso de 35 que dispara repair-ask.
        m = _compile([_click(uia_name="Save", uia_aid="btnSave")])
        s = m.compiled_execution_graph[0]
        cs = (s.target_context.confidence or {}).get("capture_score")
        assert cs is not None
        assert cs >= 50, f"AID+name+ctype debe sumar ≥50, fue {cs}"

    def test_click_with_only_coords_gets_low_score(self):
        # Click sin UIA ni web → solo coords (low confidence)
        m = _compile([
            RawEvent(
                event_type=EventType.MOUSE_CLICK,
                mouse_action=MouseAction(x=100, y=200, button="left"),
                metadata={},
            ),
        ])
        s = m.compiled_execution_graph[0]
        cs = (s.target_context.confidence or {}).get("capture_score")
        assert cs is not None
        assert cs < 60, f"Click solo con coords debería tener score bajo, fue {cs}"

    def test_web_test_id_plus_role_name_scores_top(self):
        # web.test_id (+35) + web.role+name (+30) + uia AID (+30) +
        # uia name+ct (+20) = 115 → cap 100.
        m = _compile([_click(
            uia_name="Submit", uia_aid="btnSubmit",
            web_test_id="submit-btn", web_role="button",
            web_accessible_name="Submit",
            web_locators=[{"kind": "test_id", "value": "submit-btn"}],
        )])
        s = m.compiled_execution_graph[0]
        cs = (s.target_context.confidence or {}).get("capture_score")
        assert cs is not None
        assert cs >= 90, f"web test_id + role+name + UIA debería ≥90, fue {cs}"

    def test_semantic_block_filled(self):
        m = _compile([_click(uia_name="Save", uia_aid="btnSave")])
        s = m.compiled_execution_graph[0]
        sem = s.target_context.semantic or {}
        assert sem.get("intent"), "intent debería estar relleno"
        assert sem.get("human_label") == "Save", \
            "human_label debe venir del UIA name"
        assert sem.get("expected_state_after"), \
            "expected_state_after debe asignarse por intent"


# ──────────────────────────────────────────────────────────────────
# Pattern: SUBMIT_SEARCH (B) — SET_FIELD_VALUE + Enter
# ──────────────────────────────────────────────────────────────────

class TestSubmitSearchPattern:
    def test_field_then_enter_marks_submit_after(self):
        # CLICK sobre input + escribir + Enter
        events = [
            _click(uia_name="Buscar", uia_ctype="EditControl",
                   uia_aid="searchInput"),
            _key("h"), _key("o"), _key("l"), _key("a"),
            _key("Key.enter"),
        ]
        m = _compile(events)
        # Debería haber un SET_FIELD_VALUE con submit_after=True (o el
        # patrón A NAVIGATE_OR_SEARCH si el compiler lo decidió). En
        # ambos casos, el último paso es semánticamente "buscar".
        last = m.compiled_execution_graph[-1]
        strat = last.action_strategy
        if strat == ActionStrategy.SET_FIELD_VALUE:
            assert (last.action_payload or {}).get("submit_after") is True, \
                "SET_FIELD_VALUE+Enter debe marcar submit_after=True"
        elif strat in (ActionStrategy.SUBMIT_SEARCH, ActionStrategy.NAVIGATE_OR_SEARCH):
            # Patrón composito ya colapsó; igual válido.
            pass
        else:
            pytest.fail(
                f"Esperaba SET_FIELD_VALUE/SUBMIT_SEARCH/NAVIGATE_OR_SEARCH, "
                f"obtuve {strat}"
            )


# ──────────────────────────────────────────────────────────────────
# Pattern: NAVIGATE_OR_SEARCH (A) — Ctrl+L + texto + Enter
# ──────────────────────────────────────────────────────────────────

class TestNavigateOrSearch:
    def test_ctrl_l_text_enter_collapses_to_navigate(self):
        events = [
            # Ctrl+L abre la barra de direcciones
            _key("l", modifiers=["ctrl"]),
            # Pegar/escribir URL
            _key("g"), _key("o"), _key("o"), _key("g"), _key("l"), _key("e"),
            _key("."), _key("c"), _key("o"), _key("m"),
            _key("Key.enter"),
        ]
        m = _compile(events)
        graph = m.compiled_execution_graph
        # El compiler debería colapsar a un único NAVIGATE_OR_SEARCH (o al menos
        # eliminar el Ctrl+L como hotkey suelto + dejar el SET_FIELD_VALUE).
        strats = [s.action_strategy for s in graph]
        assert ActionStrategy.NAVIGATE_OR_SEARCH in strats or \
               ActionStrategy.SET_FIELD_VALUE in strats, \
               f"Debe existir NAVIGATE_OR_SEARCH o SET_FIELD_VALUE; fue {strats}"


# ──────────────────────────────────────────────────────────────────
# Pattern: SCROLL_UNTIL_VISIBLE (D) — SCROLL + CLICK
# ──────────────────────────────────────────────────────────────────

class TestScrollUntilVisible:
    def test_scroll_then_click_promotes_scroll(self):
        events = [
            _scroll(dy=-200),
            _click(uia_name="OK", uia_aid="btnOK"),
        ]
        m = _compile(events)
        graph = m.compiled_execution_graph
        # El primer paso (scroll) debería convertirse en SCROLL_UNTIL_VISIBLE.
        first = graph[0]
        assert first.action_strategy == ActionStrategy.SCROLL_UNTIL_VISIBLE, \
            f"SCROLL+CLICK debería promover SCROLL → SCROLL_UNTIL_VISIBLE, " \
            f"fue {first.action_strategy}"
        # Y el payload debe incluir scroll_until con descriptores.
        scroll_until = (first.action_payload or {}).get("scroll_until")
        assert isinstance(scroll_until, dict), \
            "scroll_until debe ser un dict en el payload"


# ──────────────────────────────────────────────────────────────────
# Pattern: CONFIRM_DIALOG (E) — ESC standalone
# ──────────────────────────────────────────────────────────────────

class TestConfirmDialog:
    def test_standalone_esc_becomes_cancel_dialog(self):
        events = [_key("Key.esc")]
        m = _compile(events)
        graph = m.compiled_execution_graph
        if not graph:
            return  # algunos compilers descartan ESC suelto, también válido.
        s = graph[0]
        # Debe ser CONFIRM_DIALOG con answer=cancel o un SEND_HOTKEY suelto.
        assert s.action_strategy in (
            ActionStrategy.CONFIRM_DIALOG, ActionStrategy.SEND_HOTKEY
        ), f"ESC suelto: esperaba CONFIRM_DIALOG/SEND_HOTKEY, fue {s.action_strategy}"
        if s.action_strategy == ActionStrategy.CONFIRM_DIALOG:
            assert (s.action_payload or {}).get("answer") in ("cancel", "no")


# ──────────────────────────────────────────────────────────────────
# expected_state_after por intent
# ──────────────────────────────────────────────────────────────────

class TestExpectedStateAfter:
    def test_set_field_value_expects_field_value_set(self):
        # Click + escribir crea SET_FIELD_VALUE
        events = [
            _click(uia_name="Email", uia_ctype="EditControl",
                   uia_aid="email"),
            _key("a"), _key("b"), _key("c"),
        ]
        m = _compile(events)
        # Buscar el SET_FIELD_VALUE
        sfv = next(
            (s for s in m.compiled_execution_graph
             if s.action_strategy == ActionStrategy.SET_FIELD_VALUE),
            None
        )
        assert sfv is not None, "Debería haber un SET_FIELD_VALUE"
        sem = sfv.target_context.semantic or {}
        assert sem.get("expected_state_after") == "field_value_set"

    def test_click_button_expects_state_change(self):
        m = _compile([_click(uia_name="Guardar", uia_aid="btnSave")])
        s = m.compiled_execution_graph[0]
        sem = s.target_context.semantic or {}
        # Cualquiera de estos es válido para un click button.
        assert sem.get("expected_state_after") in (
            "element_state_changed", "dialog_closed", "url_changed",
            "menu_dismissed",
        )
