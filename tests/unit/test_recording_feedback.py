"""Tests del builder ``recording_feedback.build_recording_feedback`` (PRD §11).

Cubre:
  - Click con UIA name → "Clic en botón 'X'"
  - Click sin UIA pero con app conocida → "Clic en Chrome"
  - Click sólo con coords → muestra X/Y + confianza baja
  - Texto con correcciones → muestra valor final, no microteclas
  - Ctrl+S → "Guardar (Ctrl+S)"
  - Tab x3 → "Tabular × 3"
  - Scroll → "Scroll hacia abajo"
  - Modo experto sí muestra detalles técnicos
  - Modo normal no muestra JSON crudo
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.contracts.mission import (
    RawEvent, EventType, MouseAction, KeyboardAction, DragAction,
    WindowContext,
)
from app.services.missions.recording_feedback import (
    build_recording_feedback, RecordingFeedback,
)


def _ev_click(
    *, x=200, y=300, name="", aid="", ct="ButtonControl",
    process="", web=None, sig=None,
) -> RawEvent:
    md = {}
    if name or aid or ct:
        md["uia"] = {"name": name, "automation_id": aid, "control_type": ct}
    if web:
        md["web"] = web
    if sig:
        md["target_signature"] = sig
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=x, y=y),
        window_context=WindowContext(process_name=process or None),
        timestamp=datetime.now(timezone.utc),
        metadata=md,
    )


# ──────────────────────────────────────────────────────────────────
# Clicks
# ──────────────────────────────────────────────────────────────────

class TestClickFeedback:
    def test_uia_button_with_name(self):
        ev = _ev_click(name="Inicio", ct="ButtonControl",
                       sig={"confidence": {
                           "confidence_score": 88.0,
                           "target_quality": "green",
                       }})
        fb = build_recording_feedback(raw_event=ev)
        assert "Clic en botón" in fb.label_es
        assert "'Inicio'" in fb.label_es
        assert fb.confidence_score == 88.0
        assert fb.quality_level == "good"
        assert fb.icon == "🖱"

    def test_uia_edit_field(self):
        ev = _ev_click(name="Usuario", ct="EditControl")
        fb = build_recording_feedback(raw_event=ev)
        assert "Clic en campo" in fb.label_es
        assert "'Usuario'" in fb.label_es

    def test_no_uia_uses_app_name(self):
        ev = _ev_click(process="chrome.exe")
        fb = build_recording_feedback(raw_event=ev)
        assert "Clic en Chrome" in fb.label_es

    def test_only_coords_low_confidence(self):
        ev = _ev_click(x=240, y=812)  # sin uia/process
        fb = build_recording_feedback(raw_event=ev)
        assert "X=240" in fb.label_es and "Y=812" in fb.label_es
        # Sin señales → quality weak
        assert fb.quality_level in ("weak", "")
        # Score estimado debe ser bajo
        assert (fb.confidence_score or 100) < 50

    def test_double_click(self):
        from app.contracts.mission import EventType
        ev = _ev_click(name="archivo.txt", ct="ListItemControl")
        ev2 = ev.model_copy(update={"event_type": EventType.MOUSE_DOUBLE_CLICK})
        fb = build_recording_feedback(raw_event=ev2)
        assert "Doble clic" in fb.label_es
        assert "🖱🖱" == fb.icon

    def test_right_click(self):
        from app.contracts.mission import EventType
        ev = _ev_click(name="link", ct="HyperlinkControl")
        ev2 = ev.model_copy(update={"event_type": EventType.MOUSE_RIGHT_CLICK})
        fb = build_recording_feedback(raw_event=ev2)
        assert "Clic derecho" in fb.label_es


# ──────────────────────────────────────────────────────────────────
# Texto / Field session
# ──────────────────────────────────────────────────────────────────

class TestFieldSessionFeedback:
    def test_open_phase(self):
        fb = build_recording_feedback(field_session={
            "phase": "open", "label": "Buscar",
        })
        assert "Editando campo" in fb.label_es
        assert "'Buscar'" in fb.label_es

    def test_committed_phase(self):
        fb = build_recording_feedback(field_session={
            "phase": "committed", "label": "Nombre", "value": "Daniel",
        })
        assert "Rellenar Nombre con 'Daniel'" in fb.label_es
        # Mientras se compila la microhistoria de teclas NO aparece.
        assert "backspace" not in fb.label_es.lower()
        assert "shift" not in fb.label_es.lower()

    def test_committed_with_chrome_url_marks_weak(self):
        fb = build_recording_feedback(field_session={
            "phase": "committed", "label": "Buscar",
            "value": "chrome://profile-picker/",
        })
        assert fb.quality_level == "weak"
        assert "Captura débil" in fb.label_es

    def test_writing_phase_shows_buffer_only(self):
        fb = build_recording_feedback(field_session={
            "phase": "writing", "buffer": "Daniel",
        })
        # No debe mostrar tecla por tecla; solo el buffer.
        assert fb.label_es == "Texto capturado: 'Daniel'"


class TestKeyboardTypeTextFinalText:
    def test_field_session_event(self):
        ev = RawEvent(
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            keyboard_action=None,
            timestamp=datetime.now(timezone.utc),
            metadata={
                "field_session": True,
                "final_text": "Daniel",
                "field_label": "Nombre",
                "committed_read_source": "uia",
                "field_commit": {
                    "user_typed_text": "Daniel",
                    "final_field_value": "Daniel",
                    "source_is_url": False,
                    "source_is_input_value": True,
                    "reason": "value_read_via_uia",
                },
            },
        )
        fb = build_recording_feedback(raw_event=ev)
        assert "Rellenar 'Nombre' con 'Daniel'" in fb.label_es
        # En modo experto debe haber detalles.
        assert any("read_source" in d for d in fb.expert_details)
        assert any("source_is_url" in d for d in fb.expert_details)


# ──────────────────────────────────────────────────────────────────
# Hotkeys / teclas especiales
# ──────────────────────────────────────────────────────────────────

class TestHotkeyFeedback:
    def test_ctrl_s(self):
        ev = RawEvent(
            event_type=EventType.KEYBOARD_KEY_PRESS,
            keyboard_action=KeyboardAction(keys=["s"], modifiers=["ctrl"]),
            timestamp=datetime.now(timezone.utc),
        )
        fb = build_recording_feedback(raw_event=ev)
        assert "Guardar (Ctrl+S)" in fb.label_es
        assert fb.icon == "💾"

    def test_alt_tab(self):
        ev = RawEvent(
            event_type=EventType.KEYBOARD_KEY_PRESS,
            keyboard_action=KeyboardAction(keys=["tab"], modifiers=["alt"]),
            timestamp=datetime.now(timezone.utc),
        )
        fb = build_recording_feedback(raw_event=ev)
        assert "Cambiar ventana" in fb.label_es

    def test_special_tab_alone(self):
        ev = RawEvent(
            event_type=EventType.KEYBOARD_KEY_PRESS,
            keyboard_action=KeyboardAction(keys=["tab"], modifiers=[]),
            timestamp=datetime.now(timezone.utc),
        )
        fb = build_recording_feedback(raw_event=ev)
        # No es hotkey; presenta "Tabular".
        assert "Tabular" in fb.label_es

    def test_unknown_combo_falls_back_to_combo_str(self):
        ev = RawEvent(
            event_type=EventType.KEYBOARD_KEY_PRESS,
            keyboard_action=KeyboardAction(
                keys=["q"], modifiers=["ctrl", "shift"],
            ),
            timestamp=datetime.now(timezone.utc),
        )
        fb = build_recording_feedback(raw_event=ev)
        # Debe contener "Ctrl+...+Q" o similar.
        assert "Q" in fb.label_es


# ──────────────────────────────────────────────────────────────────
# Drag / Scroll
# ──────────────────────────────────────────────────────────────────

class TestDragScrollFeedback:
    def test_scroll_down(self):
        ev = RawEvent(
            event_type=EventType.MOUSE_SCROLL,
            mouse_action=MouseAction(x=100, y=200, button="scroll"),
            timestamp=datetime.now(timezone.utc),
            metadata={"scroll": {"dx": 0, "dy": -3}},
        )
        fb = build_recording_feedback(raw_event=ev)
        assert "Scroll hacia abajo" in fb.label_es

    def test_drag(self):
        ev = RawEvent(
            event_type=EventType.MOUSE_DRAG,
            drag_action=DragAction(
                start_x=10, start_y=20, end_x=200, end_y=300, duration=0.5,
            ),
            timestamp=datetime.now(timezone.utc),
        )
        fb = build_recording_feedback(raw_event=ev)
        assert "Arrastrar" in fb.label_es


# ──────────────────────────────────────────────────────────────────
# Payload para overlay
# ──────────────────────────────────────────────────────────────────

class TestOverlayPayload:
    def test_payload_does_not_leak_raw_json(self):
        ev = _ev_click(name="Buscar", ct="EditControl")
        fb = build_recording_feedback(raw_event=ev)
        payload = fb.to_overlay_payload()
        # En modo normal no debe mostrarse el target_signature crudo.
        assert "target_signature" not in payload
        assert "uia_data" not in payload
        # La descripción humana sí debe estar.
        assert "Clic" in payload["text"]

    def test_expert_details_isolated_from_text(self):
        ev = RawEvent(
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=datetime.now(timezone.utc),
            metadata={
                "field_session": True,
                "final_text": "x",
                "field_label": "F",
                "committed_read_source": "uia",
                "field_commit": {"user_typed_text": "x"},
            },
        )
        fb = build_recording_feedback(raw_event=ev)
        # El expert_details existe pero no se mete en la descripción humana.
        assert fb.expert_details
        for d in fb.expert_details:
            assert d not in fb.label_es
