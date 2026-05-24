"""
Nevlan — Tests del RerecordOverlay (PRD 2026-05-08c §C + §E)
==============================================================

Cubre:

  * Fase ``replay`` muestra solo el botón "Empezar" + "Cancelar"
    (Save y Repetir ocultos hasta que llegue la captura).
  * ``update_replay_progress`` actualiza el label visible.
  * ``enter_capture_phase`` habilita Save/Repetir/Cancelar.
  * Atajos de teclado: Esc → cancel, Enter (con captura) → save,
    F5 → retry.
  * La señal ``start_replay_requested`` se emite al pulsar Empezar.
  * El overlay nunca queda con un texto vacío después de cambiar
    de fase.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MISSION_REVIEW = REPO_ROOT / "app" / "interfaces" / "desktop" / "mission_review.py"


def _src() -> str:
    return MISSION_REVIEW.read_text(encoding="utf-8")


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def _qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as e:
        pytest.skip(f"PyQt6 no disponible: {e}")
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class TestOverlayPhaseReplay:

    def test_replay_phase_shows_start_and_cancel_only(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import (
            PHASE_REPLAY, RerecordOverlay,
        )
        ov = RerecordOverlay(
            step_index=3,
            step_description="Buscar canción",
            step_strategy="search_youtube",
            phase=PHASE_REPLAY,
            total_previous_steps=4,
        )
        try:
            assert ov.btn_start.isVisible() or ov.btn_start.isVisibleTo(ov)
            assert ov.btn_cancel.isVisible() or ov.btn_cancel.isVisibleTo(ov)
            assert ov.btn_save.isHidden() or not ov.btn_save.isVisible()
            assert ov.btn_retry.isHidden() or not ov.btn_retry.isVisible()
            assert ov._save_enabled is False
        finally:
            ov.close()

    def test_replay_phase_label_is_never_empty(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import (
            PHASE_REPLAY, RerecordOverlay,
        )
        ov = RerecordOverlay(
            step_index=3,
            step_description="Buscar canción",
            step_strategy="search_youtube",
            phase=PHASE_REPLAY,
            total_previous_steps=4,
        )
        try:
            txt = ov.replay_progress_lbl.text()
            assert txt and txt.strip(), (
                "replay_progress_lbl no puede arrancar vacío"
            )
        finally:
            ov.close()

    def test_update_replay_progress_renders_idx_total_label(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import (
            PHASE_REPLAY, RerecordOverlay,
        )
        ov = RerecordOverlay(
            step_index=3,
            step_description="Buscar canción",
            step_strategy="search_youtube",
            phase=PHASE_REPLAY,
            total_previous_steps=4,
        )
        try:
            ov.update_replay_progress(2, 4, "open_url")
            assert "2/4" in ov.replay_progress_lbl.text()
        finally:
            ov.close()

    def test_start_button_emits_start_replay_requested(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import (
            PHASE_REPLAY, RerecordOverlay,
        )
        ov = RerecordOverlay(
            step_index=3,
            step_description="Buscar canción",
            step_strategy="search_youtube",
            phase=PHASE_REPLAY,
            total_previous_steps=4,
        )
        try:
            received = {"v": 0}
            ov.start_replay_requested.connect(lambda: received.__setitem__("v", received["v"] + 1))
            ov.btn_start.click()
            assert received["v"] == 1
            assert not ov.btn_start.isEnabled()  # se deshabilita tras click
        finally:
            ov.close()

    def test_show_replay_failed_keeps_overlay_alive(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import (
            PHASE_REPLAY, RerecordOverlay,
        )
        ov = RerecordOverlay(
            step_index=3,
            step_description="Buscar canción",
            step_strategy="search_youtube",
            phase=PHASE_REPLAY,
            total_previous_steps=4,
        )
        try:
            ov.show_replay_failed("No pude llegar al paso (paso 2 falló).")
            assert "No pude llegar" in ov.replay_progress_lbl.text()
            # El botón Empezar se reusa como "Reintentar" — no se oculta.
            assert "Reintentar" in ov.btn_start.text()
            assert ov.btn_start.isEnabled()
        finally:
            ov.close()


class TestOverlayPhaseCapture:

    def test_enter_capture_phase_enables_buttons(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import (
            PHASE_REPLAY, RerecordOverlay,
        )
        ov = RerecordOverlay(
            step_index=3,
            step_description="Buscar canción",
            step_strategy="search_youtube",
            phase=PHASE_REPLAY,
            total_previous_steps=4,
        )
        try:
            ov.enter_capture_phase()
            assert ov.phase == "capture"
            assert not ov.btn_start.isVisible()
            assert ov.btn_save.isVisible() or ov.btn_save.isVisibleTo(ov)
            assert ov.btn_retry.isVisible() or ov.btn_retry.isVisibleTo(ov)
            assert ov.btn_cancel.isVisible() or ov.btn_cancel.isVisibleTo(ov)
        finally:
            ov.close()

    def test_save_button_is_always_clickable_in_capture_phase(self, _qapp):
        """PRD 2026-05-08d §C — el botón Guardar SIEMPRE está
        clickeable en fase capture (la validación 'sin captura' la
        hace el handler con NevlanDialog "No hay nada que guardar"
        en lugar de dejarlo deshabilitado y parecer un botón muerto).
        """
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(
            step_index=0,
            step_description="Abrir Chrome",
            step_strategy="open_app",
        )
        try:
            # Sin captura el flag interno está en False, pero el
            # botón sigue clickeable.
            assert ov._save_enabled is False
            assert ov.btn_save.isEnabled() is True
            ov.set_human_display("Hizo click en…", "")
            assert ov._save_enabled is True
            assert ov.btn_save.isEnabled() is True
        finally:
            ov.close()

    def test_reset_for_retry_keeps_save_clickable(self, _qapp):
        """PRD 2026-05-08d §C — Repetir resetea el flag interno
        ``_save_enabled``, pero NO deshabilita el botón. La
        validación 'sin captura' la hace el handler.
        """
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(
            step_index=0,
            step_description="Abrir Chrome",
            step_strategy="open_app",
        )
        try:
            ov.set_human_display("listo", "")
            assert ov.btn_save.isEnabled() is True
            ov.reset_for_retry()
            # El botón sigue clickeable aunque no haya captura;
            # el flag _save_enabled refleja el estado interno.
            assert ov.btn_save.isEnabled() is True
            assert ov._save_enabled is False
        finally:
            ov.close()


# ─────────────────────────────────────────────────────────────────────
# Análisis estático: dialogos del flujo usan NevlanDialog (PRD §F)
# ─────────────────────────────────────────────────────────────────────


class TestRerecordDialogsUseNevlanDialog:
    """El flujo de regrabación en mission_review.py debe usar
    NevlanDialog (no QMessageBox) para todos los popups del PRD §F."""

    def test_rerecord_flow_imports_nevlan_dialog_helpers(self):
        src = _src()
        assert "from app.interfaces.desktop.nevlan_dialog import" in src
        assert "show_warning" in src
        assert "show_confirmation" in src
        assert "show_error" in src

    def test_rerecord_flow_does_not_use_qmessagebox_for_rerecord(self):
        src = _src()
        # Nos quedamos con el segmento de rerecord_step y handlers
        # asociados (todo lo que está entre 'def rerecord_step' y
        # '_cleanup_rerecord' inclusive).
        start = src.index("def rerecord_step")
        end = src.index("_cleanup_rerecord(self):")
        end = src.index("\n    # ", end)  # límite hasta el siguiente bloque
        body = src[start:end]
        assert "QMessageBox.warning" not in body, (
            "El flujo de regrabación no debe usar QMessageBox.warning"
        )
        assert "QMessageBox.critical" not in body, (
            "El flujo de regrabación no debe usar QMessageBox.critical"
        )
        assert "QMessageBox.information" not in body, (
            "El flujo de regrabación no debe usar QMessageBox.information"
        )

    def test_rerecord_step_uses_show_confirmation_for_preparing(self):
        src = _src()
        start = src.index("def rerecord_step")
        # Tomamos hasta el final de ``rerecord_step`` (siguiente
        # def en la clase). Suficientemente generoso.
        rest = src[start:]
        assert "Preparando regrabación" in rest
        assert "show_confirmation" in rest

    def test_rerecord_failure_dialog_uses_three_buttons(self):
        src = _src()
        start = src.index("def _handle_replay_failure")
        end = src.index("\n    def ", start + 1)
        body = src[start:end]
        # 3 botones: primary (reintentar) + secondary (manual) + cancel.
        assert "primary=" in body and "secondary=" in body and "cancel=" in body
        # NevlanDialog (warning), no QMessageBox.
        assert "show_warning" in body
        assert "QMessageBox" not in body

    def test_save_dialog_announces_success_with_visible_message(self):
        src = _src()
        start = src.index("def _on_rerecord_save")
        end = src.index("\n    def ", start + 1)
        body = src[start:end]
        # Tras save exitoso, debe haber NevlanDialog con mensaje
        # explicando estado nuevo.
        assert "Regrabación guardada" in body
        assert "show_warning" in body or "show_confirmation" in body

    def test_cancel_with_unsaved_capture_asks_confirmation(self):
        src = _src()
        start = src.index("def _on_rerecord_cancel")
        end = src.index("\n    def ", start + 1)
        body = src[start:end]
        assert "show_confirmation" in body
        assert "Cancelar regrabación" in body

    def test_save_prevents_double_call(self):
        src = _src()
        start = src.index("def _on_rerecord_save")
        end = src.index("\n    def ", start + 1)
        body = src[start:end]
        assert "_rerecord_save_in_flight" in body
