"""Tests para el post-pass ``_post_pass_collapse_partial_text``.

Bug 2026-05-03: el grabador emitía una sesión de texto múltiples veces
fragmentando "Devuelveme el amor luis miguel" en 7 pasos:

    "D"
    "Devuelveme el amor"
    "Devuelveme el amor L"
    "Devuelveme el amor Luis"
    ...

Comportamiento esperado: si los pasos son consecutivos, del mismo
campo, y uno es prefijo del siguiente, el post-pass conserva solo el
último (más completo).
"""
from __future__ import annotations

from app.contracts.mission import (
    ActionStrategy, CompiledStep, InterpretedStep, TargetContext,
)
from app.services.missions.compiler import MissionCompiler


def _text_step(text: str, label: str = "Buscar",
               process: str = "chrome.exe") -> CompiledStep:
    return CompiledStep(
        action_strategy=ActionStrategy.TYPE_TEXT,
        target_context=TargetContext(
            window_title="YouTube", process_name=process,
        ),
        action_payload={"text": text, "field_label": label},
    )


def _interp(step: CompiledStep, desc: str = "") -> InterpretedStep:
    return InterpretedStep(
        raw_event_ids=[],
        description=desc or f"Escribir '{step.action_payload.get('text', '')}'",
    )


def _click_step() -> CompiledStep:
    return CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=TargetContext(process_name="chrome.exe"),
        action_payload={},
    )


class TestCollapsePrefixesSameField:
    def test_simple_prefix_collapses(self):
        a = _text_step("D")
        b = _text_step("Devuelveme")
        c = _text_step("Devuelveme el amor")
        d = _text_step("Devuelveme el amor luis miguel")
        compiled = [a, b, c, d]
        interp = [_interp(s) for s in compiled]
        out_c, out_i = MissionCompiler._post_pass_collapse_partial_text(
            compiled, interp,
        )
        assert len(out_c) == 1
        assert out_c[0].action_payload["text"] == "Devuelveme el amor luis miguel"
        absorbed = out_c[0].action_payload.get("partial_text_absorbed") or []
        assert "D" in absorbed
        assert "Devuelveme" in absorbed
        assert "Devuelveme el amor" in absorbed

    def test_case_insensitive_prefix(self):
        """'Chrome' + 'chrome el amor' → conservar el más largo."""
        a = _text_step("Chrome")
        b = _text_step("chrome el amor")
        out_c, _ = MissionCompiler._post_pass_collapse_partial_text(
            [a, b], [_interp(a), _interp(b)],
        )
        assert len(out_c) == 1
        assert out_c[0].action_payload["text"] == "chrome el amor"

    def test_different_labels_do_not_collapse(self):
        """Distintos field_label → no fusionar."""
        a = _text_step("user", label="Usuario")
        b = _text_step("user@example.com", label="Email")
        out_c, _ = MissionCompiler._post_pass_collapse_partial_text(
            [a, b], [_interp(a), _interp(b)],
        )
        assert len(out_c) == 2

    def test_click_between_does_not_collapse_across(self):
        """Si hay un click intermedio, las dos partes son sesiones distintas."""
        a = _text_step("hola")
        click = _click_step()
        b = _text_step("hola mundo")
        compiled = [a, click, b]
        interp = [_interp(a), _interp(click, "🖱 Click"), _interp(b)]
        out_c, _ = MissionCompiler._post_pass_collapse_partial_text(
            compiled, interp,
        )
        assert len(out_c) == 3

    def test_non_prefix_does_not_collapse(self):
        """'usuario' + 'password' → no se fusionan."""
        a = _text_step("usuario", label="Usuario")
        b = _text_step("password", label="Contraseña")
        out_c, _ = MissionCompiler._post_pass_collapse_partial_text(
            [a, b], [_interp(a), _interp(b)],
        )
        assert len(out_c) == 2

    def test_keeps_raw_event_lineage(self):
        a = _text_step("D")
        b = _text_step("Daniel")
        ia = InterpretedStep(
            raw_event_ids=["evt1", "evt2"],
            description="x",
        )
        ib = InterpretedStep(
            raw_event_ids=["evt3"],
            description="y",
        )
        out_c, out_i = MissionCompiler._post_pass_collapse_partial_text(
            [a, b], [ia, ib],
        )
        assert len(out_c) == 1
        # Linaje preservado y deduplicado:
        assert set(out_i[0].raw_event_ids) == {"evt1", "evt2", "evt3"}

    def test_no_text_steps_pass_through(self):
        click = _click_step()
        out_c, out_i = MissionCompiler._post_pass_collapse_partial_text(
            [click], [_interp(click, "Click")],
        )
        assert len(out_c) == 1
        assert out_c[0] is click


class TestHumanizeFieldLabel:
    """Tests para ``_humanize_field_label`` (label técnico → humano)."""

    def test_guide_service_replaced_by_buscar(self):
        step = CompiledStep(
            action_strategy=ActionStrategy.TYPE_TEXT,
            target_context=TargetContext(
                process_name="chrome.exe",
                web_data={
                    "domain": "youtube.com",
                    "role": "searchbox",
                    "placeholder": "Buscar",
                    "label": "",
                },
            ),
            action_payload={
                "text": "devuelveme el amor",
                "field_label": "guide-service",
            },
        )
        i = InterpretedStep(
            raw_event_ids=[],
            description="⌨ Rellenar campo 'guide-service' con 'devuelveme...'",
        )
        MissionCompiler._humanize_field_label(step, i)
        assert step.action_payload.get("field_label") == "Buscar"
        assert step.action_payload.get("original_field_label") == "guide-service"
        assert "guide-service" not in i.description.lower()
        assert "buscar" in i.description.lower()

    def test_human_label_preserved(self):
        """Si el label ya es humano, no se toca."""
        step = CompiledStep(
            action_strategy=ActionStrategy.TYPE_TEXT,
            target_context=TargetContext(
                process_name="chrome.exe",
                web_data={"placeholder": "Buscar"},
            ),
            action_payload={"text": "hola", "field_label": "Usuario"},
        )
        i = InterpretedStep(
            raw_event_ids=[],
            description="⌨ Rellenar campo 'Usuario' con 'hola'",
        )
        MissionCompiler._humanize_field_label(step, i)
        assert step.action_payload.get("field_label") == "Usuario"

    def test_role_searchbox_falls_back_to_buscar(self):
        """Sin placeholder ni label, role=searchbox da label='Buscar'."""
        step = CompiledStep(
            action_strategy=ActionStrategy.TYPE_TEXT,
            target_context=TargetContext(
                process_name="chrome.exe",
                web_data={"role": "searchbox"},
            ),
            action_payload={
                "text": "x", "field_label": "container-root-app",
            },
        )
        i = InterpretedStep(raw_event_ids=[], description="x")
        MissionCompiler._humanize_field_label(step, i)
        assert step.action_payload.get("field_label") == "Buscar"
