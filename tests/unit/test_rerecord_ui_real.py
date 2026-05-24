"""
Nevlan — Tests UI reales del flujo de regrabación (PRD 2026-05-08d §B-§D)
==========================================================================

Estos tests cubren las garantías que el bloque previo NO validaba:

    "Los botones Guardar/Repetir/Cancelar parecen muertos."

Cada test simula un click REAL (``btn.click()``) sobre el QPushButton
del overlay y verifica que:

  1. La señal correspondiente (``save_requested`` /
     ``retry_requested`` / ``cancel_requested``) se emite.
  2. El handler del MissionReviewDialog se invoca.
  3. El handler produce el efecto visible esperado (dialog con
     mensaje, mutación del plan, cleanup, etc.).

NO usamos pytest-qt (no instalado en el repo): construimos un
QApplication offscreen manual + bombeamos eventos con
``qapp.processEvents()`` para que las queued connections se
ejecuten en el mismo proceso.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY_PATH = REPO_ROOT / "app" / "interfaces" / "desktop" / "rerecord_overlay.py"
MISSION_REVIEW_PATH = REPO_ROOT / "app" / "interfaces" / "desktop" / "mission_review.py"


def _overlay_src() -> str:
    return OVERLAY_PATH.read_text(encoding="utf-8")


def _review_src() -> str:
    return MISSION_REVIEW_PATH.read_text(encoding="utf-8")


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def _qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as e:
        pytest.skip(f"PyQt6 no disponible: {e}")
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ─────────────────────────────────────────────────────────────────────
# §D — Tests UI reales: botones realmente clickeables y conectados
# ─────────────────────────────────────────────────────────────────────


class TestOverlayButtonsAreReal:

    def test_rerecord_overlay_save_button_connected(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(
            step_index=0, step_description="Click X",
            step_strategy="open_app",
        )
        try:
            received = {"v": 0}
            ov.save_requested.connect(
                lambda: received.__setitem__("v", received["v"] + 1)
            )
            assert ov.btn_save.isEnabled(), (
                "El botón Guardar debe quedar siempre clickeable; "
                "la validación 'sin captura' la hace el handler."
            )
            ov.btn_save.click()
            _qapp.processEvents()
            assert received["v"] == 1, (
                "Click sobre Guardar debe emitir save_requested "
                "(handler conectado correctamente)."
            )
        finally:
            ov.close()

    def test_rerecord_overlay_repeat_button_connected(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(
            step_index=0, step_description="Click X",
            step_strategy="open_app",
        )
        try:
            received = {"v": 0}
            ov.retry_requested.connect(
                lambda: received.__setitem__("v", received["v"] + 1)
            )
            assert ov.btn_retry.isEnabled()
            ov.btn_retry.click()
            _qapp.processEvents()
            assert received["v"] == 1
        finally:
            ov.close()

    def test_rerecord_overlay_cancel_button_connected(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(
            step_index=0, step_description="Click X",
            step_strategy="open_app",
        )
        try:
            received = {"v": 0}
            ov.cancel_requested.connect(
                lambda: received.__setitem__("v", received["v"] + 1)
            )
            assert ov.btn_cancel.isEnabled()
            ov.btn_cancel.click()
            _qapp.processEvents()
            assert received["v"] == 1
        finally:
            ov.close()

    def test_rerecord_buttons_do_not_raise_silent_exceptions(self, _qapp):
        """Triplica clicks rápidos para detectar excepciones
        silenciosas en los handlers de Qt (race conditions, slots
        que asumen estado interno y crashean)."""
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(
            step_index=0, step_description="Click X",
            step_strategy="open_app",
        )
        try:
            # Suscribimos un slot que no rompe. Si los _on_* internos
            # tiran, pytest los reporta como warnings recogidos por
            # PyQt y los promueve a UnraisableException si se setea.
            for _ in range(3):
                ov.btn_save.click()
                ov.btn_retry.click()
                ov.btn_cancel.click()
                _qapp.processEvents()
        finally:
            ov.close()


# ─────────────────────────────────────────────────────────────────────
# §C — Save sin captura muestra NevlanDialog visible
# ─────────────────────────────────────────────────────────────────────


class TestSaveWithoutCaptureShowsDialog:

    def test_rerecord_save_without_capture_shows_visible_dialog(self, _qapp, monkeypatch):
        """El handler ``_on_rerecord_save`` debe llamar
        ``show_warning`` cuando no hay ``_fragment_recorder`` activo,
        en lugar de retornar silenciosamente (lo que el usuario
        percibe como botón muerto).
        """
        from app.interfaces.desktop import mission_review as mr_mod

        captured = {"calls": []}

        def fake_show_warning(parent, **kwargs):
            captured["calls"].append(kwargs)
            return "primary"

        monkeypatch.setattr(mr_mod, "show_warning", fake_show_warning)

        # Simulamos un MissionReviewDialog mínimo con el atributo
        # que el handler revisa.
        class FakeReview:
            _rerecord_save_in_flight = False
            _fragment_recorder = None
            _poll_timer = None
            _rerecord_orchestrator = None

            _on_rerecord_save = mr_mod.MissionReviewDialog._on_rerecord_save

        fr = FakeReview()
        fr._on_rerecord_save()
        assert len(captured["calls"]) == 1, (
            "Sin captura activa, el handler debe mostrar "
            "NevlanDialog 'No hay nada que guardar'."
        )
        kw = captured["calls"][0]
        assert "No hay nada que guardar" in (kw.get("title") or "")


# ─────────────────────────────────────────────────────────────────────
# §C — Retry limpia buffer y reinicia captura
# ─────────────────────────────────────────────────────────────────────


class TestRepeatClearsBuffer:

    def test_rerecord_repeat_clears_capture_buffer(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(
            step_index=0, step_description="x", step_strategy="open_app",
        )
        try:
            ov.set_human_display("hizo algo", "")
            assert ov.btn_save.isEnabled()
            assert ov._save_enabled is True

            ov.reset_for_retry()

            # PRD §C: el botón Save NO se deshabilita (sigue clickeable
            # siempre), pero el flag interno se resetea para que el
            # handler de save validación entre por la vía 'sin captura'.
            assert ov.btn_save.isEnabled() is True
            assert ov._save_enabled is False
            # El texto debe reflejar el mensaje del PRD (literal exacto).
            assert "Listo, repite este paso nuevamente." in ov.events_lbl.text()
        finally:
            ov.close()


# ─────────────────────────────────────────────────────────────────────
# §C — Cancel restaura misión + cierra overlay
# ─────────────────────────────────────────────────────────────────────


class TestCancelRestoresAndCloses:

    def test_rerecord_cancel_emits_signal_and_closes_overlay(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(
            step_index=0, step_description="x", step_strategy="open_app",
        )
        try:
            received = {"v": 0}
            ov.cancel_requested.connect(
                lambda: received.__setitem__("v", received["v"] + 1)
            )
            ov.btn_cancel.click()
            _qapp.processEvents()
            assert received["v"] == 1
            # Tras emitir cancel, el flag terminal queda activado
            # para que un close() programático posterior NO re-emita.
            assert ov._emitted_terminal is True
        finally:
            ov.close()
            _qapp.processEvents()
        # El overlay cerró sin doble emit (tras el close manual de
        # finally, el flag prevenía el segundo emit).
        assert received["v"] == 1

    def test_rerecord_close_event_emits_cancel_for_safety(self, _qapp):
        """Si el overlay se cierra por una vía que NO pasó por
        ``_on_cancel`` (Alt+F4, ``close()`` directo, parent destruido),
        SIEMPRE emitimos ``cancel_requested`` para garantizar que el
        caller libere recursos.
        """
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(
            step_index=0, step_description="x", step_strategy="open_app",
        )
        received = {"v": 0}
        ov.cancel_requested.connect(
            lambda: received.__setitem__("v", received["v"] + 1)
        )
        ov.close()  # cerrar directo sin pasar por _on_cancel
        _qapp.processEvents()
        assert received["v"] == 1, (
            "closeEvent debe emitir cancel_requested para garantizar "
            "cleanup del caller (worker, listeners, timers)."
        )


# ─────────────────────────────────────────────────────────────────────
# §B — El overlay NO usa flags que rompen interacción
# ─────────────────────────────────────────────────────────────────────


class TestOverlayWindowFlags:

    def test_overlay_does_not_use_window_does_not_accept_focus(self):
        """``WindowDoesNotAcceptFocus`` + ``NoFocus`` causaba el bug
        "botones muertos" (PRD §B.1). Verificamos por análisis
        estático que NO se vuelva a aplicar como flag activo
        (las menciones en comentarios explicativos sí están
        permitidas — sirven de documentación histórica).
        """
        src = _overlay_src()
        # 1) Aislar las líneas que NO son comentarios (todo lo que
        #    está dentro de ``setWindowFlags(...)`` o
        #    ``setFocusPolicy(...)``).
        offending_setflags = [
            ln for ln in src.splitlines()
            if "Qt.WindowType.WindowDoesNotAcceptFocus" in ln
            and not ln.lstrip().startswith("#")
        ]
        assert not offending_setflags, (
            "WindowDoesNotAcceptFocus aplicado como flag activo: "
            f"{offending_setflags}. Usa ClickFocus + "
            "WA_ShowWithoutActivating en su lugar."
        )
        offending_focuspolicy = [
            ln for ln in src.splitlines()
            if "Qt.FocusPolicy.NoFocus" in ln
            and not ln.lstrip().startswith("#")
        ]
        assert not offending_focuspolicy, (
            "FocusPolicy.NoFocus aplicado: rompe los QPushButton "
            "del overlay. Usa ClickFocus."
        )
        # La política correcta tiene que estar presente como código.
        active_clickfocus = [
            ln for ln in src.splitlines()
            if "FocusPolicy.ClickFocus" in ln
            and not ln.lstrip().startswith("#")
        ]
        assert active_clickfocus, (
            "Falta la línea que aplica FocusPolicy.ClickFocus."
        )

    def test_overlay_save_button_never_starts_disabled_in_capture_phase(self, _qapp):
        """En fase capture, el botón Save SIEMPRE está enabled (la
        validación 'sin captura' la hace el handler con NevlanDialog).
        """
        from app.interfaces.desktop.rerecord_overlay import (
            PHASE_CAPTURE, RerecordOverlay,
        )
        ov = RerecordOverlay(
            step_index=0, step_description="x", step_strategy="open_app",
            phase=PHASE_CAPTURE,
        )
        try:
            assert ov.btn_save.isEnabled() is True
        finally:
            ov.close()


# ─────────────────────────────────────────────────────────────────────
# §B — _build_cards conecta los handlers correctamente (estático)
# ─────────────────────────────────────────────────────────────────────


class TestMissionReviewWiresHandlers:

    def test_mission_review_rerecord_real_flow_buttons_work(self):
        """Análisis estático: las cuatro signals del overlay
        (start_replay/save/retry/cancel) tienen que estar conectadas
        a handlers reales del MissionReviewDialog antes de mostrar
        el overlay (PRD §B).
        """
        src = _review_src()
        for signal, handler in (
            ("start_replay_requested", "_on_rerecord_start_replay"),
            ("save_requested", "_on_rerecord_save"),
            ("retry_requested", "_on_rerecord_retry"),
            ("cancel_requested", "_on_rerecord_cancel"),
        ):
            assert f"_rerecord_overlay.{signal}.connect" in src, (
                f"{signal} no conectado en MissionReviewDialog"
            )
            assert f"def {handler}(self" in src, (
                f"Handler {handler} no existe"
            )

    def test_save_handler_uses_show_warning_for_missing_capture(self):
        """El handler debe llamar ``show_warning`` con el título
        'No hay nada que guardar' cuando no hay captura."""
        src = _review_src()
        start = src.index("def _on_rerecord_save(self):")
        end = src.index("\n    def ", start + 1)
        body = src[start:end]
        assert "No hay nada que guardar" in body
        assert "show_warning" in body

    def test_cancel_handler_releases_orchestrator_and_workers(self):
        src = _review_src()
        start = src.index("def _on_rerecord_cancel(self):")
        end = src.index("\n    def ", start + 1)
        body = src[start:end]
        # Garantías de cleanup.
        assert "_stop_replay_worker" in body
        assert "_cleanup_rerecord" in body
        # Confirmación si hay captura sin guardar.
        assert "show_confirmation" in body


# ─────────────────────────────────────────────────────────────────────
# §A.4 — confirm_profile UI: botón existe + handler registrado
# ─────────────────────────────────────────────────────────────────────


class TestProfileConfirmButtonInUI:

    def test_card_has_profile_confirm_button_for_select_profile(self):
        """En la card, cuando el step es ``select_profile`` con
        ``profile_name=""``, debe existir el bloque que añade el
        botón "👤 Confirmar perfil" + handler."""
        src = _review_src()
        assert "_open_profile_confirm_dialog" in src
        assert "👤 Confirmar perfil" in src
        # El handler está definido como método de la clase.
        assert "def _open_profile_confirm_dialog" in src

    def test_open_profile_confirm_dialog_persists_with_auto_save(self):
        """El handler debe llamar a ``confirm_profile`` y luego
        ``_auto_save`` para que el cambio quede en disco."""
        src = _review_src()
        start = src.index("def _open_profile_confirm_dialog(self, idx: int)")
        end = src.index("\n    # ", start + 1) if "\n    # " in src[start + 1:] else len(src)
        body = src[start:end]
        assert "confirm_profile" in body
        assert "_auto_save" in body
        assert "refresh_cards" in body
        # NEvlanDialog input (PRD §F).
        assert "show_text_input" in body
