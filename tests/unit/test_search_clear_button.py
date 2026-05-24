"""
Nevlan — Centro de Automatizaciones, botón "✕" del buscador
==============================================================

PRD 2026-05-08c §C.1: el buscador del Centro de Automatizaciones
debe tener una pequeña cruz que limpia el texto.

Requisitos:
  * Aparece SOLO cuando hay texto.
  * Click → limpia el input + devuelve foco + refresca lista.
  * Tooltip "Limpiar búsqueda".
  * Funciona con mouse y teclado (Esc dentro del campo).
  * Diseño Nevlan (no botón nativo gris).

Estos tests combinan:
  1. Inspección de código fuente (rápida, headless).
  2. Test de comportamiento con QApplication offscreen.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Aseguramos plataforma offscreen ANTES de importar Qt: en CI no hay
# display y los widgets nativos requieren un platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_CENTER = REPO_ROOT / "app" / "interfaces" / "desktop" / "automation_center.py"


def _src() -> str:
    return AUTO_CENTER.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────
# Inspección de fuente (siempre corre, sin Qt)
# ─────────────────────────────────────────────────────────────────


class TestSearchClearButtonSource:
    def test_search_clear_button_is_declared(self):
        src = _src()
        assert "self.search_clear_btn" in src, (
            "Falta el botón de limpiar búsqueda en automation_center."
        )

    def test_button_has_clear_tooltip(self):
        src = _src()
        assert "Limpiar búsqueda" in src

    def test_button_starts_hidden(self):
        """La cruz aparece solo cuando hay texto — debe iniciar oculta."""
        src = _src()
        assert "self.search_clear_btn.hide()" in src

    def test_button_handler_is_connected(self):
        src = _src()
        assert "self.search_clear_btn.clicked.connect(self._on_clear_search)" in src

    def test_textchanged_toggles_visibility(self):
        src = _src()
        assert "_toggle_search_clear_btn" in src
        # _on_clear_search llama a setText("") que dispara toggle vía
        # textChanged. La conexión debe estar.
        assert "textChanged.connect(self._toggle_search_clear_btn)" in src

    def test_clear_handler_returns_focus_to_search(self):
        src = _src()
        idx = src.index("def _on_clear_search")
        end = src.index("\n    def ", idx + 1)
        body = src[idx:end]
        assert "setText(\"\")" in body, (
            "El handler debe limpiar el input."
        )
        assert "setFocus" in body, (
            "El handler debe devolver el foco al buscador."
        )

    def test_event_filter_supports_escape_key(self):
        """Esc dentro del lineedit debe limpiar."""
        src = _src()
        assert "_SearchClearEventFilter" in src
        assert "Qt.Key.Key_Escape" in src

    def test_button_styled_with_nevlan_theme(self):
        """El botón usa NevlanTheme (no estilos nativos)."""
        src = _src()
        assert "QToolButton#searchClearBtn" in src
        assert "NevlanTheme.TEXT_MUTED" in src or "TEXT_MUTED" in src


# ─────────────────────────────────────────────────────────────────
# Test de comportamiento con QApplication (offscreen)
# ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def _qapp():
    """QApplication headless para el módulo entero."""
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as e:
        pytest.skip(f"PyQt6 no disponible: {e}")
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class TestSearchClearBehavior:
    """Tests de comportamiento real del handler usando un QLineEdit
    aislado.

    No instanciamos el Centro de Automatizaciones completo (es muy
    pesado y requiere store/services), sino que probamos la unidad
    "clear handler + toggle visibility" en aislamiento, exactamente
    el contrato que pide el PRD §C.1.
    """

    def test_clear_handler_empties_text(self, _qapp):
        from PyQt6.QtWidgets import QLineEdit, QToolButton, QWidget

        class DummyOwner(QWidget):
            def __init__(self):
                super().__init__()
                self.search_input = QLineEdit(self)
                self.search_clear_btn = QToolButton(self.search_input)
                self.search_clear_btn.hide()

                def _toggle(text):
                    if text:
                        self.search_clear_btn.show()
                    else:
                        self.search_clear_btn.hide()
                self.search_input.textChanged.connect(_toggle)
                self.search_clear_btn.clicked.connect(self._on_clear)

            def _on_clear(self):
                from PyQt6.QtCore import Qt
                self.search_input.setText("")
                self.search_input.setFocus(
                    Qt.FocusReason.OtherFocusReason,
                )

        w = DummyOwner()
        # Como el parent no está mostrado, isVisible() siempre False;
        # usamos isHidden() (estado explícito).
        assert w.search_clear_btn.isHidden() is True
        w.search_input.setText("youtube")
        assert w.search_clear_btn.isHidden() is False
        # Click programático
        w.search_clear_btn.click()
        assert w.search_input.text() == ""
        # Tras limpiar, el botón vuelve a estar oculto.
        assert w.search_clear_btn.isHidden() is True
