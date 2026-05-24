"""Tests para FieldCommitResult enriquecido (PRD §2.6).

Comprueba las banderas:
  - user_typed_text
  - final_field_value
  - source_is_url / source_is_app_state / source_is_input_value
  - chosen_final_text
  - reason
"""
from __future__ import annotations

from typing import Optional

from app.services.missions.field_commit import (
    FieldCommitResult, build_keyboard_type_text_event,
    is_browser_internal_url,
    READ_SOURCE_DOM, READ_SOURCE_UIA,
    READ_SOURCE_LAST_KNOWN, READ_SOURCE_KEYBOARD,
    READ_SOURCE_KEYBOARD_URL_SANITIZED,
)
from app.services.missions.recorder import MissionRecorder


def _prepare(
    *, buf: str, label: str = "Buscar",
    initial: Optional[str] = "", last_known: Optional[str] = None,
    read_value: Optional[str] = None,
    web_meta: Optional[dict] = None,
) -> MissionRecorder:
    rec = MissionRecorder("t")
    rec._field_session_active = True
    rec._field_session_label = label
    rec._field_session_buf = list(buf)
    rec._field_session_initial_value = initial
    rec._field_session_last_known_value = (
        last_known if last_known is not None else initial
    )
    rec._field_session_target_meta = {
        "name": label, "control_type": "EditControl",
        "automation_id": "field-x",
    }
    rec._field_session_web_meta = dict(web_meta or {})
    rec._read_field_value_now = lambda: read_value  # type: ignore
    return rec


class TestFieldCommitMetadata:
    def test_uia_input_value_wins_when_user_typed(self):
        """UIA devuelve "Daniel Ramos" → ese es el valor real."""
        rec = _prepare(
            buf="Daniel Ramos", read_value="Daniel Ramos",
            initial="", last_known="Daniel Ramos",
        )
        fc = rec._build_field_commit_result(prefer_uia=True)
        assert fc is not None
        assert fc.final_text == "Daniel Ramos"
        assert fc.read_source == READ_SOURCE_UIA
        assert fc.user_typed_text == "Daniel Ramos"
        assert fc.final_field_value == "Daniel Ramos"
        assert fc.source_is_input_value is True
        assert fc.source_is_url is False
        assert fc.source_is_app_state is False
        assert fc.chosen_final_text == "Daniel Ramos"
        assert "value_read_via_uia" in fc.reason

    def test_dom_read_marks_dom_source(self):
        """Si hay web_meta, prefer_uia + read_value devuelve "dom"."""
        rec = _prepare(
            buf="hola", read_value="hola",
            web_meta={"locators": [{"kind": "test_id", "value": "x"}]},
        )
        fc = rec._build_field_commit_result(prefer_uia=True)
        assert fc is not None
        assert fc.read_source == READ_SOURCE_DOM
        assert fc.source_is_input_value is True

    def test_unchanged_field_uses_last_known(self):
        """Si la app no cambió el valor del initial pero last_known sí, gana last_known."""
        rec = _prepare(
            buf="abc", read_value="prefilled",
            initial="prefilled", last_known="prefilled+abc",
        )
        fc = rec._build_field_commit_result(prefer_uia=True)
        assert fc is not None
        assert fc.final_text == "prefilled+abc"
        assert fc.read_source == READ_SOURCE_LAST_KNOWN
        assert fc.reason == "field_unchanged_use_last_known"

    def test_no_read_falls_back_to_buffer(self):
        rec = _prepare(buf="texto", read_value=None,
                       initial=None, last_known=None)
        fc = rec._build_field_commit_result(prefer_uia=True)
        assert fc is not None
        assert fc.final_text == "texto"
        assert fc.read_source == READ_SOURCE_KEYBOARD
        assert fc.user_typed_text == "texto"
        assert fc.source_is_input_value is False
        assert "fallback_user_buffer" in fc.reason

    def test_chrome_internal_url_overridden_by_buffer(self):
        rec = _prepare(
            buf="Chrome", read_value="chrome://profile-picker/",
            initial="", last_known="chrome://profile-picker/",
        )
        fc = rec._build_field_commit_result(prefer_uia=True)
        assert fc is not None
        assert fc.final_text == "Chrome"
        assert fc.read_source == READ_SOURCE_KEYBOARD_URL_SANITIZED
        assert fc.user_typed_text == "Chrome"
        assert fc.final_field_value == "chrome://profile-picker/"
        # IMPORTANTE: source_is_url se desmarca tras override.
        assert fc.source_is_url is False
        assert fc.chosen_final_text == "Chrome"
        assert "url_internal_overridden_by_buffer" in fc.reason

    def test_app_state_marked_when_user_typed_nothing(self):
        """Si el usuario no tipeó nada y la app devuelve un valor, es app_state."""
        rec = _prepare(
            buf="", read_value="https://example.com/",
            initial="https://example.com/",
            last_known="https://example.com/",
        )
        fc = rec._build_field_commit_result(prefer_uia=True)
        assert fc is not None
        # No es URL interna, así que no se descarta.
        assert fc.final_field_value == "https://example.com/"
        # Como read==initial Y no hay buffer, source_is_app_state=True.
        assert fc.source_is_app_state is True


class TestKeyboardTypeTextEventCarriesMetadata:
    def test_event_metadata_includes_field_commit_block(self):
        fc = FieldCommitResult(
            final_text="Daniel",
            label="Nombre",
            read_source=READ_SOURCE_UIA,
            initial_value="",
            anchor_uia={"automation_id": "input-name"},
            anchor_web={},
            window_context=None,
            user_typed_text="Daniel",
            final_field_value="Daniel",
            source_is_url=False,
            source_is_app_state=False,
            source_is_input_value=True,
            chosen_final_text="Daniel",
            reason="value_read_via_uia",
        )
        ev = build_keyboard_type_text_event(fc=fc)
        assert ev.metadata["field_session"] is True
        assert ev.metadata["final_text"] == "Daniel"
        assert ev.metadata["field_label"] == "Nombre"
        fc_block = ev.metadata.get("field_commit") or {}
        assert fc_block["user_typed_text"] == "Daniel"
        assert fc_block["final_field_value"] == "Daniel"
        assert fc_block["source_is_url"] is False
        assert fc_block["source_is_input_value"] is True
        assert fc_block["chosen_final_text"] == "Daniel"
        assert fc_block["reason"] == "value_read_via_uia"


class TestIsBrowserInternalUrl:
    def test_known_prefixes(self):
        assert is_browser_internal_url("chrome://settings")
        assert is_browser_internal_url("CHROME://SETTINGS")
        assert is_browser_internal_url("chrome-extension://abc")
        assert is_browser_internal_url("edge://flags")
        assert is_browser_internal_url("about:blank")
        assert is_browser_internal_url("brave://welcome")
        assert is_browser_internal_url("opera://settings")
        assert is_browser_internal_url("vivaldi://about")
        assert is_browser_internal_url("file:///C:/x.txt")
        assert is_browser_internal_url("view-source:https://x")
        assert is_browser_internal_url("devtools://devtools/x")

    def test_falsey_values(self):
        assert not is_browser_internal_url("")
        assert not is_browser_internal_url(None)
        assert not is_browser_internal_url("https://google.com")
        assert not is_browser_internal_url("Chrome")
        assert not is_browser_internal_url("    ")
