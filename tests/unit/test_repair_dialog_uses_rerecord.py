"""
Nevlan — Tests del dialog "Reparar pasos" (PRD 2026-05-09 §G)
================================================================

El usuario reportó:

    "La ventana emergente que sale despues de dar click en el botón
    Reparar [...] esta obsoleta, ningún botón funciona, adicionar
    que si se va reparar un paso, se deben ejecutar los anteriores
    automaticamente, y cuando siga el paso debe salir esta ventana
    de regrar y sus botones deben funcionar al 1000%."

El dialog viejo invocaba ``pick_correct_element`` (un único click,
sin replay previo) y eso es exactamente lo que rompía la sensación
de "reparar un paso". Ahora el dialog Reparar:

  1. Por defecto invoca ``rerecord_step(idx)``: ejecuta los N pasos
     anteriores automáticamente y abre el RerecordOverlay con los
     botones Save/Repeat/Cancel funcionales (los que se arreglaron
     en `bloque B`).
  2. Tiene un botón explícito **🎯 Regrabar paso** (default) que
     dispara la regrabación.
  3. La cola **⚡ Regrabar los N en cola** encadena las
     regrabaciones reusando ``rerecord_step`` y se reanuda en
     ``_cleanup_rerecord``.
  4. Los botones del dialog son siempre clickeables (focus y window
     flags arreglados).

Estos tests validan ese contrato (estático + UI offscreen).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MISSION_REVIEW_PATH = REPO_ROOT / "app" / "interfaces" / "desktop" / "mission_review.py"


def _review_src() -> str:
    return MISSION_REVIEW_PATH.read_text(encoding="utf-8")


def _repair_weak_steps_body() -> str:
    src = _review_src()
    start = src.index("def repair_weak_steps(self):")
    end = src.index("\n    def ", start + 1)
    return src[start:end]


def _process_next_repair_in_queue_body() -> str:
    src = _review_src()
    start = src.index("def _process_next_repair_in_queue(self):")
    end = src.index("\n    def ", start + 1)
    return src[start:end]


def _cleanup_rerecord_body() -> str:
    src = _review_src()
    start = src.index("def _cleanup_rerecord(self):")
    end = src.index("\n    def ", start + 1)
    return src[start:end]


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
# §G — Dialog usa rerecord_step (no pick_correct_element)
# ─────────────────────────────────────────────────────────────────────


class TestRepairDialogUsesRerecordStep:

    def test_repair_dialog_default_handler_is_rerecord_step(self):
        """El handler por defecto del dialog Reparar debe llamar
        ``self.rerecord_step(...)``, no ``self.pick_correct_element(...)``.

        El requisito del usuario es: al "Reparar" un paso, ejecutar
        los anteriores automáticamente y abrir la ventana de regrar.
        Eso es exactamente lo que hace ``rerecord_step``.
        """
        body = _repair_weak_steps_body()
        # Comprobamos la línea exacta del despachador final.
        assert "self.rerecord_step(i)" in body, (
            "El dialog Reparar debe disparar rerecord_step (replay "
            "previo + RerecordOverlay), no pick_correct_element."
        )
        # No debe quedar el handler legacy en este flujo.
        assert "self.pick_correct_element(i)" not in body, (
            "pick_correct_element ya no es el comportamiento "
            "principal del botón Reparar."
        )

    def test_repair_dialog_has_explicit_rerecord_button(self):
        """El dialog Reparar debe exponer un QPushButton **🎯 Regrabar paso**
        para que el usuario tenga un control explícito (no sólo el
        doble-clic, que era el único atajo del dialog viejo).
        """
        body = _repair_weak_steps_body()
        assert "🎯 Regrabar paso" in body, (
            "Falta el botón explícito '🎯 Regrabar paso' en el "
            "dialog Reparar pasos."
        )
        assert "weakRepairRerecordBtn" in body, (
            "El botón Regrabar debe tener objectName "
            "'weakRepairRerecordBtn' para estilarlo."
        )

    def test_repair_dialog_queue_button_uses_rerecord_chain(self):
        """La cola debe seedearse en ``_repair_queue`` y procesarse
        con ``_process_next_repair_in_queue`` (que usa
        ``rerecord_step``), no con ``_process_next_weak`` (legacy
        ``pick_correct_element``).
        """
        body = _repair_weak_steps_body()
        assert "self._repair_queue = [" in body, (
            "La cola debe persistirse en self._repair_queue."
        )
        assert "self._process_next_repair_in_queue" in body, (
            "El botón ⚡ Cola debe disparar "
            "_process_next_repair_in_queue."
        )

    def test_process_next_repair_uses_rerecord_step(self):
        body = _process_next_repair_in_queue_body()
        assert "self.rerecord_step(idx)" in body, (
            "_process_next_repair_in_queue debe llamar rerecord_step."
        )
        assert "self.pick_correct_element" not in body, (
            "_process_next_repair_in_queue NO debe usar "
            "pick_correct_element."
        )


# ─────────────────────────────────────────────────────────────────────
# §G — _cleanup_rerecord encadena la cola
# ─────────────────────────────────────────────────────────────────────


class TestCleanupChainsRepairQueue:

    def test_cleanup_rerecord_dispatches_next_in_repair_queue(self):
        """El cleanup post-regrabación debe encadenar el siguiente
        paso de la cola si hay items pendientes en ``_repair_queue``.
        """
        body = _cleanup_rerecord_body()
        assert "_repair_queue" in body, (
            "_cleanup_rerecord debe inspeccionar self._repair_queue "
            "para encadenar la cola."
        )
        assert "_process_next_repair_in_queue" in body, (
            "_cleanup_rerecord debe lanzar "
            "_process_next_repair_in_queue cuando la cola tiene items."
        )


# ─────────────────────────────────────────────────────────────────────
# §G — Window flags del dialog Reparar (botones siempre clickeables)
# ─────────────────────────────────────────────────────────────────────


class TestRepairDialogWindowFlagsAreSane:

    def test_repair_dialog_uses_dialog_flags_with_close_button(self):
        body = _repair_weak_steps_body()
        # Window flags explícitas: Dialog + título + botón cerrar.
        assert "Qt.WindowType.Dialog" in body, (
            "El dialog Reparar debe tener Qt.WindowType.Dialog "
            "para asegurar comportamiento modal correcto."
        )
        assert "WindowCloseButtonHint" in body, (
            "El dialog Reparar debe exponer un botón de cierre "
            "estándar del SO (X) para que el usuario nunca quede "
            "atrapado."
        )
        # Foco fuerte para que los botones reciban clicks/Enter.
        assert "Qt.FocusPolicy.StrongFocus" in body, (
            "El dialog Reparar debe usar StrongFocus."
        )
        # No debe usar las flags problemáticas que rompen botones.
        offending = [
            ln for ln in body.splitlines()
            if "WindowDoesNotAcceptFocus" in ln
            and not ln.lstrip().startswith("#")
        ]
        assert not offending, (
            "WindowDoesNotAcceptFocus rompe los botones del dialog "
            f"(líneas problemáticas: {offending})."
        )

    def test_repair_dialog_default_button_is_rerecord(self):
        """El botón **🎯 Regrabar paso** debe ser el default
        (Enter dispara rerecord) para que la UX sea consistente.
        """
        body = _repair_weak_steps_body()
        assert "rerecord_btn.setDefault(True)" in body, (
            "El botón Regrabar debe ser default."
        )
        assert "rerecord_btn.setAutoDefault(True)" in body, (
            "El botón Regrabar debe responder a Enter."
        )


# ─────────────────────────────────────────────────────────────────────
# §G — Comportamiento real (offscreen): repair_weak_steps no estalla
# ─────────────────────────────────────────────────────────────────────


class TestRepairDialogDoesNotCrash:
    """Smoke test offscreen: simulamos un MissionReviewDialog con el
    mínimo estado necesario y disparamos repair_weak_steps. NO
    queremos que estalle por dependencias no satisfechas.
    """

    def test_repair_weak_steps_does_not_crash_when_no_weak_steps(
        self, _qapp, monkeypatch,
    ):
        """Si no hay pasos débiles, debe mostrar 'Misión estable' vía
        NevlanDialog (show_warning) y retornar sin abrir el QDialog
        de Reparar.
        """
        from app.interfaces.desktop import mission_review as mr_mod

        captured = {"warnings": []}

        def fake_show_warning(parent, **kw):
            captured["warnings"].append(kw.get("title") or "")
            return "primary"

        monkeypatch.setattr(mr_mod, "show_warning", fake_show_warning)

        # Patches de dependencias pesadas: que no hagan nada.
        from types import SimpleNamespace

        def fake_compute_exec(_mission):
            return SimpleNamespace(steps=[])

        monkeypatch.setattr(
            mr_mod, "compute_mission_execution_confidence", fake_compute_exec
        )

        # check_mission_approvable: report sin errores.
        class FakeGate:
            errors: list = []

        import app.services.missions.approval_gate as ag_mod
        monkeypatch.setattr(
            ag_mod, "check_mission_approvable", lambda m: FakeGate()
        )

        mission_stub = SimpleNamespace(
            compiled_execution_graph=[],
            interpreted_steps=[],
            semantic_execution_plan={},
        )

        class FakeReview:
            repair_weak_steps = mr_mod.MissionReviewDialog.repair_weak_steps

            def _stub(self, *a, **kw):
                pass

        fr = FakeReview()
        fr.mission = mission_stub
        # No debe lanzar.
        fr.repair_weak_steps()
        assert any("Misión estable" in t for t in captured["warnings"]), (
            "Sin pasos débiles, el handler debe mostrar 'Misión estable' "
            "mediante NevlanDialog."
        )
