"""
Nevlan — Diálogos del flujo de ejecución usan NevlanDialog (PRD 2026-05-08c §I + §K)
=====================================================================================

PRD §I: ningún popup del módulo de Misiones puede salir vacío o con
texto invisible. Los flujos críticos de **Aceptar riesgo**, **Probar
paso**, **Ejecutar** y **Recompilar** ahora usan ``NevlanDialog`` (no
``QMessageBox`` nativo).

Este test inspecciona la fuente para garantizar que las funciones del
flujo crítico:

  1. Importan los helpers ``show_error`` / ``show_warning`` /
     ``show_confirmation``.
  2. NO usan ``QMessageBox.warning`` ni ``QMessageBox.critical`` sin
     título + mensaje literales.
  3. Pasan ``expert_details`` con la representación de la excepción
     cuando aplica (sin volcar el stacktrace al título).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_CENTER = REPO_ROOT / "app" / "interfaces" / "desktop" / "automation_center.py"
MISSION_REVIEW = REPO_ROOT / "app" / "interfaces" / "desktop" / "mission_review.py"


def _function_body(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    idx = src.index(f"def {name}")
    end = src.index("\n    def ", idx + 1)
    return src[idx:end]


# ─────────────────────────────────────────────────────────────────────
# Funciones del flujo crítico que DEBEN usar NevlanDialog
# ─────────────────────────────────────────────────────────────────────


class TestRunScheduledMissionUsesNevlanDialog:
    """``_run_scheduled_mission_now`` y `_edit_schedule_from_upcoming`
    no pueden mostrar errores con ``QMessageBox`` nativo: el usuario
    los reportó como ventanas que aparecían vacías o con título
    confuso."""

    def test_run_scheduled_uses_nevlan(self):
        body = _function_body(AUTO_CENTER, "_run_scheduled_mission_now")
        assert "show_error" in body or "show_warning" in body, (
            "_run_scheduled_mission_now debe usar NevlanDialog para "
            "errores, no QMessageBox nativo."
        )

    def test_run_scheduled_no_qmessagebox_critical(self):
        body = _function_body(AUTO_CENTER, "_run_scheduled_mission_now")
        assert "QMessageBox.critical" not in body
        assert "QMessageBox.warning" not in body

    def test_run_scheduled_passes_expert_details_on_exceptions(self):
        body = _function_body(AUTO_CENTER, "_run_scheduled_mission_now")
        # Cuando hay excepción, el detail técnico va a expert_details
        # (no al mensaje principal).
        assert "expert_details=" in body


class TestRecompileUsesNevlanDialog:
    """``_on_recompile`` debe reportar errores con NevlanDialog
    (PRD §I) — el mensaje viejo era ``QMessageBox.critical(self,
    "Error", f"No se pudo recompilar: {e}")``, donde ``e`` podía
    ocupar varias líneas y romper el layout del popup."""

    def test_recompile_uses_nevlan(self):
        # Buscamos en todo el archivo porque _on_recompile es método y
        # puede no aparecer textualmente con ese nombre exacto.
        src = AUTO_CENTER.read_text(encoding="utf-8")
        # Localizamos la sección del recompile error.
        idx = src.find("recompile fallo")
        assert idx > 0, "no encontré el handler de recompile"
        # Tomamos los siguientes 600 caracteres y verificamos NevlanDialog.
        snippet = src[idx: idx + 800]
        assert "show_error" in snippet, (
            "El handler de recompile debe usar NevlanDialog show_error."
        )
        assert "QMessageBox.critical" not in snippet


class TestAcceptWeakStepRiskUsesNevlanDialog:
    """``accept_weak_step_risk`` ya estaba migrado en el PRD §D.
    Lo repetimos aquí para garantizar regresión."""

    def test_uses_show_error_or_show_warning(self):
        body = _function_body(MISSION_REVIEW, "accept_weak_step_risk")
        assert "show_error" in body or "show_warning" in body
        assert "QMessageBox.critical" not in body


class TestTestStepUsesNevlanDialog:
    """``_test_step`` (botón "Probar paso") debe usar NevlanDialog
    también — fue migrado en PRD §J."""

    def test_uses_show_warning(self):
        body = _function_body(AUTO_CENTER, "_test_step")
        assert "show_warning" in body or "show_error" in body, (
            "_test_step debe reportar resultado con NevlanDialog."
        )

    def test_does_not_use_qmessagebox_native(self):
        body = _function_body(AUTO_CENTER, "_test_step")
        assert "QMessageBox.critical" not in body
        assert "QMessageBox.warning" not in body


# ─────────────────────────────────────────────────────────────────────
# Garantía global: NevlanDialog nunca produce popups vacíos
# (test rápido del normalizador).
# ─────────────────────────────────────────────────────────────────────


class TestNevlanDialogNeverEmpty:
    def test_empty_spec_normalizes_to_visible_title_and_message(self):
        from app.interfaces.desktop.nevlan_dialog import (
            NevlanDialogSpec, EMPTY_TITLE_FALLBACK, EMPTY_MESSAGE_FALLBACK,
            MIN_BUTTON,
        )
        s = NevlanDialogSpec().normalized()
        assert s.title == EMPTY_TITLE_FALLBACK
        assert s.message == EMPTY_MESSAGE_FALLBACK
        # Garantía de al menos un botón clickable.
        assert s.primary_label or s.secondary_label or s.cancel_label
        assert s.primary_label == MIN_BUTTON  # default insertado

    def test_caller_text_preserved_when_provided(self):
        from app.interfaces.desktop.nevlan_dialog import NevlanDialogSpec
        s = NevlanDialogSpec(
            title="No se pudo ejecutar",
            message="Detalle del problema.",
            primary_label="Reintentar",
        ).normalized()
        assert s.title == "No se pudo ejecutar"
        assert s.message == "Detalle del problema."
        assert s.primary_label == "Reintentar"
