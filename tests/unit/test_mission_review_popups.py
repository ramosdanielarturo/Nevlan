"""
Nevlan — Popups del Mission Review (PRD 2026-05-08c §D + §I)
==============================================================

Bug original:
    "Cuando se da click a 'Aceptar riesgo', sale una ventana de error
    sin texto visible."

Fix:
    ``accept_weak_step_risk`` ahora usa ``NevlanDialog`` (no
    ``QMessageBox`` nativo) y maneja explícitamente:

      * éxito → diálogo Nevlan con mensaje claro.
      * fallo durante el guardado → diálogo Nevlan tipo error con
        título, mensaje y detalles técnicos colapsables.
      * índice fuera de rango → diálogo Nevlan tipo error con detalle
        sobre la causa.

Estos tests inspeccionan el código fuente (rápido, headless).
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MR = REPO_ROOT / "app" / "interfaces" / "desktop" / "mission_review.py"


def _function_body(name: str) -> str:
    src = MR.read_text(encoding="utf-8")
    idx = src.index(f"def {name}")
    end = src.index("\n    def ", idx + 1)
    return src[idx:end]


class TestAcceptWeakStepRiskUsesNevlanDialog:
    def test_imports_show_warning_or_show_error(self):
        body = _function_body("accept_weak_step_risk")
        assert (
            "show_warning" in body or "show_error" in body
            or "NevlanDialog" in body
        ), (
            "El handler debe usar NevlanDialog para que NUNCA aparezca "
            "una ventana sin texto visible."
        )

    def test_success_path_has_explicit_message(self):
        body = _function_body("accept_weak_step_risk")
        # El mensaje visible al usuario.
        assert "Riesgo aceptado" in body
        assert "Queda registrado" in body

    def test_failure_path_has_explicit_message(self):
        body = _function_body("accept_weak_step_risk")
        assert "No pude registrar" in body, (
            "Si el guardado falla, debe haber mensaje explícito."
        )

    def test_out_of_range_index_handled(self):
        body = _function_body("accept_weak_step_risk")
        # Antes era un return silencioso. Ahora debe haber un error
        # visible.
        assert "fuera del rango" in body or "show_error" in body

    def test_no_silent_returns_only_qmessagebox(self):
        """No debe haber ``QMessageBox`` nativo dentro del handler."""
        body = _function_body("accept_weak_step_risk")
        assert "QMessageBox.information" not in body
        assert "QMessageBox.warning" not in body
        assert "QMessageBox.critical" not in body
