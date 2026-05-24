"""
Nevlan — Tests del ExecutionTracker (PRD 2026-05-08c §C, §D)
==============================================================

Foco en el bug de ``mission_1778297133``:

  * ``log_start`` registra ``lifecycle_status=queued`` (NO running).
  * ``log_run_started`` mueve a ``running``.
  * ``log_run_finished`` persiste lifecycle + execution_trace + error_code.
  * Si el caller usa la API legacy ``log_complete("success", ...)``
    SIN haber registrado pasos, el tracker degrada a
    ``failed/NO_STEPS_STARTED`` para no mentirle al usuario.
  * ``ExecutionEntry.history_label`` devuelve los strings honestos
    esperados por el PRD §C.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.missions.execution_lifecycle import (
    ExecutionRun,
    ExecutionStatus,
    NO_STEPS_STARTED,
)


@pytest.fixture()
def tracker_with_tmp_log(monkeypatch, tmp_path):
    """Crea una instancia limpia del tracker apuntando a tmp_path."""
    log_path = tmp_path / "execution_log.json"
    fav_path = tmp_path / "favorites.json"
    import app.services.missions.execution_tracker as tr_mod
    monkeypatch.setattr(tr_mod, "EXEC_LOG_FILE", log_path)
    monkeypatch.setattr(tr_mod, "FAVORITES_FILE", fav_path)
    # Reset singleton para que use las paths nuevas.
    tr_mod.ExecutionTracker._instance = None
    tracker = tr_mod.ExecutionTracker()
    yield tracker, log_path
    tr_mod.ExecutionTracker._instance = None


def test_log_start_registers_queued_status(tracker_with_tmp_log) -> None:
    tracker, log_path = tracker_with_tmp_log
    idx = tracker.log_start("mid", "demo", 3, "manual")
    entries = tracker._entries
    assert len(entries) == 1
    e = entries[0]
    assert e.lifecycle_status == ExecutionStatus.QUEUED
    # Histor:ial NO debe mostrar "Ejecutado" en queued.
    assert e.history_label == "En cola"
    assert not e.is_executed


def test_log_run_started_moves_to_running(tracker_with_tmp_log) -> None:
    tracker, _ = tracker_with_tmp_log
    idx = tracker.log_start("mid", "demo", 2)
    run = ExecutionRun(
        mission_id="mid", mission_name="demo", total_steps=2,
        executor_used="smart", plan_source="semantic_execution_plan",
    )
    run.mark_started()
    tracker.log_run_started(idx, run)
    e = tracker._entries[idx]
    assert e.lifecycle_status == ExecutionStatus.RUNNING
    assert e.history_label == "En ejecución"
    assert e.executor_used == "smart"
    assert e.plan_source == "semantic_execution_plan"
    assert not e.is_executed


def test_log_run_finished_persists_trace_and_lifecycle(
    tracker_with_tmp_log,
) -> None:
    tracker, log_path = tracker_with_tmp_log
    idx = tracker.log_start("mid", "demo", 1)
    run = ExecutionRun(
        mission_id="mid", mission_name="demo", total_steps=1,
        executor_used="smart", plan_source="semantic_execution_plan",
    )
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded("s1", strategy_used="A")
    run.mark_succeeded()
    tracker.log_run_finished(idx, run)
    e = tracker._entries[idx]
    assert e.lifecycle_status == ExecutionStatus.SUCCEEDED
    assert e.history_label == "Ejecutado"
    assert e.is_executed
    assert e.steps_executed == 1
    assert len(e.execution_trace) == 2
    # Persistencia en disco.
    saved = json.loads(log_path.read_text(encoding="utf-8"))
    persisted = saved["entries"][0]
    assert persisted["lifecycle_status"] == "succeeded"
    assert persisted["execution_trace"][0]["step_id"] == "s1"


def test_log_complete_legacy_success_with_empty_trace_degrades(
    tracker_with_tmp_log,
) -> None:
    """Bug histórico: ``log_complete("success", steps_executed=0)``
    pintaba "Ejecutado" sin trace real. Ahora se degrada."""
    tracker, _ = tracker_with_tmp_log
    idx = tracker.log_start("mid", "demo", 3)
    tracker.log_complete(idx, "success", steps_executed=0, error="")
    e = tracker._entries[idx]
    assert e.lifecycle_status == ExecutionStatus.FAILED
    assert e.error_code == "NO_STEPS_STARTED"
    assert e.history_label == "No iniciado"
    assert not e.is_executed


def test_log_complete_legacy_success_with_steps_keeps_succeeded(
    tracker_with_tmp_log,
) -> None:
    tracker, _ = tracker_with_tmp_log
    idx = tracker.log_start("mid", "demo", 1)
    tracker.log_complete(idx, "success", steps_executed=1, error="")
    e = tracker._entries[idx]
    # Sin trace estructurada pero con steps_executed>0 mantenemos
    # back-compat (no podemos demostrar lo contrario).
    assert e.lifecycle_status == ExecutionStatus.SUCCEEDED


def test_history_label_for_failed_no_steps_started(tracker_with_tmp_log) -> None:
    tracker, _ = tracker_with_tmp_log
    idx = tracker.log_start("mid", "demo", 3)
    run = ExecutionRun(mission_id="mid", total_steps=3, executor_used="smart")
    run.mark_started()
    run.mark_failed_no_steps_started(message="ningún paso inició")
    tracker.log_run_finished(idx, run)
    e = tracker._entries[idx]
    assert e.history_label == "No iniciado"
    assert e.error_code == NO_STEPS_STARTED


def test_history_label_for_cancelled(tracker_with_tmp_log) -> None:
    tracker, _ = tracker_with_tmp_log
    idx = tracker.log_start("mid", "demo", 1)
    run = ExecutionRun(mission_id="mid", total_steps=1)
    run.mark_started()
    run.mark_cancelled()
    tracker.log_run_finished(idx, run)
    e = tracker._entries[idx]
    assert e.history_label == "Cancelado"


def test_history_label_for_timed_out(tracker_with_tmp_log) -> None:
    tracker, _ = tracker_with_tmp_log
    idx = tracker.log_start("mid", "demo", 1)
    run = ExecutionRun(mission_id="mid", total_steps=1)
    run.mark_started()
    run.mark_timed_out()
    tracker.log_run_finished(idx, run)
    e = tracker._entries[idx]
    assert e.history_label == "Sin respuesta"


def test_history_icon_matches_lifecycle(tracker_with_tmp_log) -> None:
    tracker, _ = tracker_with_tmp_log
    idx = tracker.log_start("mid", "demo", 1)
    run = ExecutionRun(mission_id="mid", total_steps=1)
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded("s1")
    run.mark_succeeded()
    tracker.log_run_finished(idx, run)
    e = tracker._entries[idx]
    assert e.history_icon == "✅"


def test_get_failed_mission_ids_uses_lifecycle(tracker_with_tmp_log) -> None:
    tracker, _ = tracker_with_tmp_log
    # Ejecución fallida sin steps.
    i1 = tracker.log_start("m_fail", "demo1", 2)
    run_fail = ExecutionRun(mission_id="m_fail", total_steps=2)
    run_fail.mark_started()
    run_fail.mark_failed_no_steps_started()
    tracker.log_run_finished(i1, run_fail)
    # Ejecución exitosa.
    i2 = tracker.log_start("m_ok", "demo2", 1)
    run_ok = ExecutionRun(mission_id="m_ok", total_steps=1)
    run_ok.mark_started()
    run_ok.mark_step_started("s1", "open_app")
    run_ok.mark_step_succeeded("s1")
    run_ok.mark_succeeded()
    tracker.log_run_finished(i2, run_ok)
    failed = tracker.get_failed_mission_ids()
    assert "m_fail" in failed
    assert "m_ok" not in failed
