"""
Nevlan — Tests del modelo ``ExecutionRun`` (PRD 2026-05-08c §A, §C, §D)
=========================================================================

Cubre las reglas duras del PRD del bloque "Execution Lifecycle":

  * ``mark_succeeded`` requiere trace no vacía con al menos un step
    ``succeeded`` (regla anti-mentira).
  * ``mark_failed_no_steps_started`` aplica ``error_code=NO_STEPS_STARTED``.
  * Estados terminales son inmutables (segunda transición => error).
  * ``derive_history_label`` produce los labels honestos esperados.
  * ``is_history_executed`` = False cuando trace vacío aunque
    status diga ``succeeded`` (defensa de UI).
"""
from __future__ import annotations

import pytest

from app.services.missions.execution_lifecycle import (
    ExecutionLifecycleError,
    ExecutionRun,
    ExecutionStatus,
    ExecutionTrace,
    NO_STEPS_STARTED,
    PROGRESS_DIALOG_TIMEOUT,
    USER_CANCELLED,
    WORKER_EXCEPTION,
    derive_history_icon,
    derive_history_label,
    is_history_executed,
)


# ─── Construcción del run ─────────────────────────────────────────────


def test_execution_run_starts_in_queued_status() -> None:
    run = ExecutionRun(mission_id="m1", mission_name="demo", total_steps=3)
    assert run.status == ExecutionStatus.QUEUED
    assert not run.is_terminal
    assert run.trace.is_empty


def test_history_does_not_mark_succeeded_on_start() -> None:
    """Recién creado: el historial NUNCA debe decir 'Ejecutado'."""
    run = ExecutionRun(mission_id="m1", mission_name="demo", total_steps=3)
    assert derive_history_label(run) == "En cola"
    assert not is_history_executed(run)


def test_history_marks_running_when_worker_starts() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=2)
    run.mark_started(executor_used="smart")
    assert run.status == ExecutionStatus.RUNNING
    assert run.executor_used == "smart"
    assert derive_history_label(run) == "En ejecución"
    assert run.started_at is not None


# ─── Trace ────────────────────────────────────────────────────────────


def test_mark_step_started_appends_trace_entry() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=2)
    run.mark_started()
    entry = run.mark_step_started("s1", "open_app", executor_used="smart")
    assert entry.step_id == "s1"
    assert entry.step_type == "open_app"
    assert entry.status == "started"
    assert run.status == ExecutionStatus.STEP_RUNNING
    assert run.trace.started_step_ids == ["s1"]


def test_mark_step_succeeded_records_strategy_and_evidence() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=1)
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded(
        "s1",
        strategy_used="windows_search",
        evidence={"app": "chrome.exe"},
        duration_ms=750,
    )
    succeeded = [
        e for e in run.trace.entries if e.status == "succeeded"
    ]
    assert len(succeeded) == 1
    e = succeeded[0]
    assert e.strategy_used == "windows_search"
    assert e.evidence == {"app": "chrome.exe"}
    assert e.duration_ms == 750
    assert run.trace.has_any_succeeded


def test_execution_trace_records_step_started_then_succeeded() -> None:
    """PRD §D: cada step deja al menos started + (succeeded|failed)."""
    run = ExecutionRun(mission_id="m1", total_steps=2)
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded("s1")
    run.mark_step_started("s2", "search_youtube")
    run.mark_step_succeeded("s2")
    assert len(run.trace.entries) == 4
    assert [e.status for e in run.trace.entries] == [
        "started", "succeeded", "started", "succeeded",
    ]
    assert run.trace.all_steps_succeeded


# ─── Reglas terminales ────────────────────────────────────────────────


def test_no_steps_started_cannot_be_success() -> None:
    """PRD §A.6: no se permite ``mark_succeeded`` con trace vacía."""
    run = ExecutionRun(mission_id="m1", total_steps=3)
    run.mark_started()
    with pytest.raises(ExecutionLifecycleError):
        run.mark_succeeded()
    assert not run.is_terminal  # nada cambió


def test_mark_failed_no_steps_started_sets_error_code() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=3)
    run.mark_started()
    run.mark_failed_no_steps_started(message="vacío")
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == NO_STEPS_STARTED
    assert run.terminal_signal_received is True
    assert derive_history_label(run) == "No iniciado"


def test_history_marks_failed_if_no_steps_started() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=3)
    run.mark_started()
    run.mark_failed_no_steps_started()
    assert derive_history_label(run) == "No iniciado"
    assert derive_history_icon(run) == "❌"
    assert not is_history_executed(run)


def test_history_marks_succeeded_only_after_all_steps_succeed() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=2)
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded("s1")
    # Antes de terminar el segundo, NO se puede marcar success.
    run.mark_step_started("s2", "open_url")
    run.mark_step_failed("s2", error="boom")
    with pytest.raises(ExecutionLifecycleError):
        run.mark_succeeded()
    # Falla controlada:
    run.mark_failed("WORKER_FAILED", message="step s2 falló")
    assert derive_history_label(run) == "Falló"


def test_history_marks_succeeded_when_all_succeed() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=2)
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded("s1")
    run.mark_step_started("s2", "open_url")
    run.mark_step_succeeded("s2")
    run.mark_succeeded()
    assert derive_history_label(run) == "Ejecutado"
    assert is_history_executed(run)
    assert run.terminal_signal_received is True


def test_history_marks_cancelled_on_user_cancel() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=3)
    run.mark_started()
    run.mark_cancelled()
    assert run.status == ExecutionStatus.CANCELLED
    assert run.error_code == USER_CANCELLED
    assert derive_history_label(run) == "Cancelado"


def test_history_marks_timed_out_without_heartbeat() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=3)
    run.mark_started()
    run.mark_timed_out(message="dialog timeout")
    assert run.status == ExecutionStatus.TIMED_OUT
    assert run.error_code == PROGRESS_DIALOG_TIMEOUT
    assert derive_history_label(run) == "Sin respuesta"


def test_terminal_state_is_immutable() -> None:
    """Segunda transición a estado terminal lanza error."""
    run = ExecutionRun(mission_id="m1", total_steps=1)
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded("s1")
    run.mark_succeeded()
    with pytest.raises(ExecutionLifecycleError):
        run.mark_cancelled()
    with pytest.raises(ExecutionLifecycleError):
        run.mark_failed("X")


def test_mark_failed_with_exception_sets_worker_exception_code() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=2)
    run.mark_started()
    try:
        raise RuntimeError("bridge crashed")
    except RuntimeError as e:
        run.mark_failed_with_exception(e)
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == WORKER_EXCEPTION
    assert "bridge crashed" in run.error_message


def test_force_terminal_for_dangling_worker() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=2)
    run.mark_started()
    run.force_terminal_for_dangling_worker()
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == "WORKER_FINISHED_WITHOUT_TERMINAL_SIGNAL"
    # Idempotente: una segunda llamada no relanza.
    run.force_terminal_for_dangling_worker()
    assert run.status == ExecutionStatus.FAILED


# ─── Trace.from_list / to_list ────────────────────────────────────────


def test_trace_serialization_round_trip() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=1)
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded("s1", strategy_used="X", evidence={"k": 1})
    raw = run.trace.to_list()
    rebuilt = ExecutionTrace.from_list(raw)
    assert rebuilt.has_any_succeeded
    assert rebuilt.entries[0].step_id == "s1"
    assert rebuilt.entries[1].strategy_used == "X"


# ─── is_history_executed defensivo ────────────────────────────────────


def test_is_history_executed_false_when_status_succeeded_but_trace_empty() -> None:
    """Defensa: si por algún motivo se forzó SUCCEEDED sin trace
    (load de JSON corrupto), la UI debe verlo como NO ejecutado."""
    run = ExecutionRun(mission_id="m1", total_steps=1)
    # Forzamos status SUCCEEDED *sin pasar por mark_succeeded* —
    # simula carga desde JSON antiguo.
    run.status = ExecutionStatus.SUCCEEDED
    assert not is_history_executed(run)


def test_is_history_executed_true_with_real_trace() -> None:
    run = ExecutionRun(mission_id="m1", total_steps=1)
    run.mark_started()
    run.mark_step_started("s1", "open_app")
    run.mark_step_succeeded("s1")
    run.mark_succeeded()
    assert is_history_executed(run)
