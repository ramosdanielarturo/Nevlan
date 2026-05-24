"""
Nevlan — Tests de la ventana ExecutionControlWidget bindeada a lifecycle
==========================================================================

PRD 2026-05-08c §B: la ventana flotante debe emitir señal terminal
y programar cierre en TODAS las rutas:

  * ``finished_success`` (mark_smart_finished status="success").
  * ``finished_failed``  (mark_smart_finished status="failed"/"error").
  * ``cancelled``        (mark_smart_finished status="cancelled" o
                           botón Cancelar con run bindeado).
  * ``timed_out``        (mark_smart_finished status="timed_out").

Y, si el run pasa a STUCK por watchdog (sin progreso), la ventana
también debe terminar y emitir señal.

Los tests evitan ejecutar el QTimer real para no bloquear la suite —
en vez de eso invocan los métodos sincronizadamente.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def _qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as e:
        pytest.skip(f"PyQt6 no disponible: {e}")
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_widget():
    from app.interfaces.desktop.automation_center import (
        ExecutionControlWidget,
    )
    return ExecutionControlWidget(
        mission_name="demo", total_steps=3, player=None,
    )


def _connect_signal(widget):
    captured = []
    widget.lifecycle_terminal.connect(lambda v: captured.append(v))
    return captured


# ─── mark_smart_finished cubre todos los terminales ──────────────────


def test_execution_dialog_closes_on_success(_qapp) -> None:
    w = _make_widget()
    captured = _connect_signal(w)
    try:
        w.mark_smart_finished(status="success", auto_close_ms=0)
    finally:
        # Forzar cleanup sin esperar QTimer
        w._cleanup()
    assert "succeeded" in captured


def test_execution_dialog_closes_on_failure(_qapp) -> None:
    w = _make_widget()
    captured = _connect_signal(w)
    try:
        w.mark_smart_finished(
            status="failed", message="boom", auto_close_ms=0,
        )
    finally:
        w._cleanup()
    assert "failed" in captured


def test_execution_dialog_closes_on_cancel(_qapp) -> None:
    w = _make_widget()
    captured = _connect_signal(w)
    try:
        w.mark_smart_finished(status="cancelled", auto_close_ms=0)
    finally:
        w._cleanup()
    assert "cancelled" in captured


def test_execution_dialog_closes_on_timeout(_qapp) -> None:
    w = _make_widget()
    captured = _connect_signal(w)
    try:
        w.mark_smart_finished(status="timed_out", auto_close_ms=0)
    finally:
        w._cleanup()
    assert "timed_out" in captured


def test_execution_dialog_terminal_signal_emitted_only_once(_qapp) -> None:
    """Idempotente: dos llamadas al mismo terminal no duplican señal."""
    w = _make_widget()
    captured = _connect_signal(w)
    try:
        w.mark_smart_finished(status="success", auto_close_ms=0)
        w.mark_smart_finished(status="success", auto_close_ms=0)
    finally:
        w._cleanup()
    assert captured.count("succeeded") == 1


# ─── bind_lifecycle + cancel button ──────────────────────────────────


def test_cancel_button_marks_run_cancelled_and_emits_signal(_qapp) -> None:
    from app.services.missions.execution_lifecycle import (
        ExecutionRun,
        ExecutionStatus,
    )
    w = _make_widget()
    run = ExecutionRun(mission_id="m1", total_steps=1)
    run.mark_started()
    w.bind_lifecycle(run)
    captured = _connect_signal(w)
    try:
        w._cancel()
    finally:
        w._cleanup()
    assert run.status == ExecutionStatus.CANCELLED
    assert "cancelled" in captured


def test_bind_lifecycle_with_terminal_run_triggers_terminal(_qapp) -> None:
    """Si el run ya entró a estado terminal antes del primer tick,
    el watchdog debe converger igualmente."""
    from app.services.missions.execution_lifecycle import (
        ExecutionRun,
    )
    w = _make_widget()
    run = ExecutionRun(mission_id="m1", total_steps=1)
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded("s1")
    run.mark_succeeded()
    w.bind_lifecycle(run)
    captured = _connect_signal(w)
    try:
        # Forzamos un tick del watchdog manualmente.
        w._on_watchdog_tick()
    finally:
        w._cleanup()
    assert "succeeded" in captured


# ─── Verificación estática: rutas terminales en código ────────────────


def test_mark_smart_finished_handles_all_terminal_statuses() -> None:
    """Inspecciona la fuente: ``mark_smart_finished`` debe mapear
    cada uno de los terminales canónicos a una señal."""
    src = (REPO_ROOT / "app" / "interfaces" / "desktop" /
           "automation_center.py").read_text(encoding="utf-8")
    # Localizar el método.
    idx = src.index("def mark_smart_finished(")
    end = src.index("\n    def ", idx + 1)
    region = src[idx:end]
    # Verificamos la presencia de cada estado terminal en el mapeo.
    for needle in (
        '"success"',
        '"cancelled"',
        '"timed_out"',
        '"stuck"',
        '"failed"',
    ):
        assert needle in region, (
            f"mark_smart_finished no mapea {needle}"
        )


def test_widget_imports_lifecycle_module() -> None:
    """Sanity: el widget conoce el lifecycle y emite ``lifecycle_terminal``."""
    src = (REPO_ROOT / "app" / "interfaces" / "desktop" /
           "automation_center.py").read_text(encoding="utf-8")
    assert "lifecycle_terminal = pyqtSignal" in src
    assert "from app.services.missions.execution_lifecycle" in src
