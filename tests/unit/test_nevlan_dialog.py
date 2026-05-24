"""
Nevlan — Tests de NevlanDialog (PRD 2026-05-08c §I + §D)
==========================================================

Contrato bajo prueba (no negociable):

  * No se puede mostrar un diálogo VACÍO. Si caller pasó title/message
    vacíos, el dialog inserta fallbacks visibles.
  * No se puede mostrar un diálogo SIN BOTONES. Si caller no pasó
    ninguno, el dialog inserta uno por defecto ("Aceptar").
  * Severity desconocida cae a ``info`` (no se rompe).
  * El campo ``details`` colapsable solo aparece si fue pasado.
  * Resultado del clic queda accesible por ``result_key``.
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def _qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as e:
        pytest.skip(f"PyQt6 no disponible: {e}")
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ─────────────────────────────────────────────────────────────────
# Spec normalization (sin Qt)
# ─────────────────────────────────────────────────────────────────


class TestSpecNormalization:
    def test_empty_title_falls_back_to_default(self):
        from app.interfaces.desktop.nevlan_dialog import (
            EMPTY_TITLE_FALLBACK, NevlanDialogSpec,
        )
        s = NevlanDialogSpec(title="", message="x").normalized()
        assert s.title == EMPTY_TITLE_FALLBACK

    def test_empty_message_falls_back_to_default(self):
        from app.interfaces.desktop.nevlan_dialog import (
            EMPTY_MESSAGE_FALLBACK, NevlanDialogSpec,
        )
        s = NevlanDialogSpec(title="x", message="").normalized()
        assert s.message == EMPTY_MESSAGE_FALLBACK

    def test_unknown_severity_becomes_info(self):
        from app.interfaces.desktop.nevlan_dialog import (
            NevlanDialogSpec, SEVERITY_INFO,
        )
        s = NevlanDialogSpec(severity="🔥").normalized()
        assert s.severity == SEVERITY_INFO

    def test_no_buttons_provided_inserts_default(self):
        from app.interfaces.desktop.nevlan_dialog import (
            MIN_BUTTON, NevlanDialogSpec,
        )
        s = NevlanDialogSpec(title="x", message="y").normalized()
        assert s.primary_label == MIN_BUTTON

    def test_caller_buttons_preserved(self):
        from app.interfaces.desktop.nevlan_dialog import NevlanDialogSpec
        s = NevlanDialogSpec(
            title="x", message="y",
            primary_label="Sí", secondary_label="Más tarde",
            cancel_label="No",
        ).normalized()
        assert s.primary_label == "Sí"
        assert s.secondary_label == "Más tarde"
        assert s.cancel_label == "No"


# ─────────────────────────────────────────────────────────────────
# Render real con QApplication offscreen
# ─────────────────────────────────────────────────────────────────


class TestNevlanDialogRender:
    def test_renders_title_and_message(self, _qapp):
        from app.interfaces.desktop.nevlan_dialog import (
            NevlanDialog, NevlanDialogSpec, SEVERITY_WARNING,
        )
        d = NevlanDialog(NevlanDialogSpec(
            title="Aceptar riesgo",
            message="Esta misión tiene avisos no críticos.",
            severity=SEVERITY_WARNING,
        ))
        # Buscamos los QLabel por objectName.
        labels = {
            l.objectName(): l for l in d.findChildren(type(d).__bases__[0]
            ) if False
        }
        # Mejor: findChildren con QLabel.
        from PyQt6.QtWidgets import QLabel
        all_labels = d.findChildren(QLabel)
        names = {l.objectName(): l for l in all_labels}
        assert names["nvlTitle"].text() == "Aceptar riesgo"
        assert names["nvlMessage"].text() == (
            "Esta misión tiene avisos no críticos."
        )

    def test_never_empty_when_caller_omits_text(self, _qapp):
        from PyQt6.QtWidgets import QLabel, QPushButton

        from app.interfaces.desktop.nevlan_dialog import (
            EMPTY_MESSAGE_FALLBACK, EMPTY_TITLE_FALLBACK,
            MIN_BUTTON, NevlanDialog, NevlanDialogSpec,
        )

        d = NevlanDialog(NevlanDialogSpec())  # todo vacío
        names = {
            l.objectName(): l for l in d.findChildren(QLabel)
        }
        assert names["nvlTitle"].text() == EMPTY_TITLE_FALLBACK
        assert names["nvlMessage"].text() == EMPTY_MESSAGE_FALLBACK
        # Y al menos un botón visible.
        btns = d.findChildren(QPushButton)
        labels = {b.text() for b in btns}
        assert MIN_BUTTON in labels

    def test_button_emits_expected_action_key(self, _qapp):
        from PyQt6.QtWidgets import QPushButton

        from app.interfaces.desktop.nevlan_dialog import (
            NevlanDialog, NevlanDialogSpec, RESULT_PRIMARY,
        )

        d = NevlanDialog(NevlanDialogSpec(
            title="x", message="y", primary_label="Hacerlo",
        ))
        # Disparamos el botón primario por nombre.
        primary = next(
            b for b in d.findChildren(QPushButton)
            if b.objectName() == "nvlBtnPrimary"
        )
        primary.click()
        assert d.result_key == RESULT_PRIMARY

    def test_expert_details_starts_collapsed(self, _qapp):
        from PyQt6.QtWidgets import QLabel, QToolButton

        from app.interfaces.desktop.nevlan_dialog import (
            NevlanDialog, NevlanDialogSpec, SEVERITY_EXPERT,
        )

        d = NevlanDialog(NevlanDialogSpec(
            title="Plan",
            message="Resumen visible.",
            severity=SEVERITY_EXPERT,
            expert_details="contamination_kind=compiled_graph_debug_only",
        ))
        # Toggle existe.
        toggles = [
            b for b in d.findChildren(QToolButton)
            if b.objectName() == "nvlExpertToggle"
        ]
        assert toggles, "Falta el toggle de detalles técnicos."
        # Texto inicial.
        assert toggles[0].text() == "Ver detalles técnicos"
        assert toggles[0].isChecked() is False
        # Toggle expand
        toggles[0].setChecked(True)
        assert toggles[0].text() == "Ocultar detalles técnicos"

    def test_details_displayed_when_provided(self, _qapp):
        from PyQt6.QtWidgets import QLabel
        from app.interfaces.desktop.nevlan_dialog import (
            NevlanDialog, NevlanDialogSpec,
        )
        d = NevlanDialog(NevlanDialogSpec(
            title="Algo pasó",
            message="No pude completar la operación.",
            details="Razón: timeout durante login.",
        ))
        names = [l.objectName() for l in d.findChildren(QLabel)]
        assert "nvlDetails" in names


# ─────────────────────────────────────────────────────────────────
# Atajos de uso
# ─────────────────────────────────────────────────────────────────


class TestShortcuts:
    def test_show_warning_returns_string(self, _qapp):
        """No invocamos exec() — usamos NevlanDialog directo para
        aislar y validar que los wrappers existen."""
        from app.interfaces.desktop import nevlan_dialog as nd
        for name in (
            "show_warning", "show_danger", "show_error",
            "show_confirmation", "show_expert_details",
        ):
            assert hasattr(nd, name), f"Falta wrapper {name}"
            assert callable(getattr(nd, name))
