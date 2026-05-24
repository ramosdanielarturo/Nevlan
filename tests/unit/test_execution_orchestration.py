"""
Nevlan — Tests del orquestador `execute_with_lifecycle` (PRD 2026-05-08c §A,B,D)
=================================================================================

Cubre las rutas críticas que el bug de ``mission_1778297133`` expuso:

  * Si el bridge devuelve ``decision.result is None`` y no está
    bloqueado ⇒ ``EXECUTOR_NOT_INVOKED``.
  * Si está bloqueado ⇒ ``GATE_BLOCKED``.
  * Si devuelve un ``MissionResult`` con ``status="success"`` y
    ``outcomes=[]`` ⇒ ``NO_STEPS_STARTED`` (ANTES marcaba succeeded).
  * Si el bridge crashea ⇒ ``WORKER_EXCEPTION``.
  * Si todos los outcomes son success y el final_status es success
    ⇒ ``mark_succeeded`` con trace correcta.
  * Si hay step failed ⇒ ``WORKER_FAILED``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

import pytest

from app.services.missions.execution_lifecycle import (
    EXECUTOR_NOT_INVOKED,
    GATE_BLOCKED,
    NO_STEPS_STARTED,
    WORKER_EXCEPTION,
    ExecutionRun,
    ExecutionStatus,
)
from app.services.missions.execution_orchestration import (
    evaluate_decision,
    execute_with_lifecycle,
)


# ─── Mocks ────────────────────────────────────────────────────────────


@dataclass
class _FakeStepOutcome:
    step_id: str
    kind: str
    status: str
    strategy_used: str = ""
    duration_ms: int = 0
    error: str = ""
    state_after: dict = field(default_factory=dict)


@dataclass
class _FakeMissionResult:
    status: str = "success"
    outcomes: List[_FakeStepOutcome] = field(default_factory=list)
    last_message: str = ""


@dataclass
class _FakeRunnerDecision:
    blocked: bool = False
    reason: str = ""
    result: Optional[_FakeMissionResult] = None
    source: str = ""


def _make_run() -> ExecutionRun:
    return ExecutionRun(
        mission_id="m_test",
        mission_name="demo",
        total_steps=2,
        executor_used="smart",
    )


# ─── evaluate_decision: rutas problemáticas ───────────────────────────


def test_evaluate_decision_no_result_emits_executor_not_invoked() -> None:
    """Bug histórico: el bridge devolvía ``decision.result=None`` y la
    UI marcaba success. Ahora => failed/EXECUTOR_NOT_INVOKED."""
    run = _make_run()
    run.mark_started()
    decision = _FakeRunnerDecision(blocked=False, result=None)
    evaluate_decision(run, decision, consume_outcomes=True)
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == EXECUTOR_NOT_INVOKED


def test_evaluate_decision_blocked_emits_gate_blocked() -> None:
    run = _make_run()
    decision = _FakeRunnerDecision(
        blocked=True, reason="approval_gate failure",
    )
    evaluate_decision(run, decision, consume_outcomes=True)
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == GATE_BLOCKED
    assert "approval_gate" in run.error_message


def test_evaluate_decision_success_no_outcomes_marks_no_steps_started() -> None:
    """Test estrella del bug ``mission_1778297133`` (PRD §A.6)."""
    run = _make_run()
    run.mark_started()
    decision = _FakeRunnerDecision(
        blocked=False,
        result=_FakeMissionResult(status="success", outcomes=[]),
    )
    evaluate_decision(run, decision, consume_outcomes=True)
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == NO_STEPS_STARTED


def test_evaluate_decision_all_success_marks_succeeded() -> None:
    run = _make_run()
    run.mark_started()
    outcomes = [
        _FakeStepOutcome(step_id="s1", kind="open_app", status="success"),
        _FakeStepOutcome(step_id="s2", kind="open_url", status="success"),
    ]
    decision = _FakeRunnerDecision(
        blocked=False,
        result=_FakeMissionResult(status="success", outcomes=outcomes),
    )
    evaluate_decision(run, decision, consume_outcomes=True)
    assert run.status == ExecutionStatus.SUCCEEDED
    assert run.trace.has_any_succeeded
    assert len(run.trace.entries) == 4  # 2 started + 2 succeeded


def test_evaluate_decision_with_failed_step_marks_worker_failed() -> None:
    run = _make_run()
    run.mark_started()
    outcomes = [
        _FakeStepOutcome(step_id="s1", kind="open_app", status="success"),
        _FakeStepOutcome(
            step_id="s2", kind="open_url", status="failed",
            error="not_found",
        ),
    ]
    decision = _FakeRunnerDecision(
        blocked=False,
        result=_FakeMissionResult(
            status="failed", outcomes=outcomes,
            last_message="step s2 falló",
        ),
    )
    evaluate_decision(run, decision, consume_outcomes=True)
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == "WORKER_FAILED"
    assert "s2" in run.error_message


def test_evaluate_decision_skipped_only_marks_failed() -> None:
    """Trace solo con skipped ⇒ no hay paso realmente ejecutado."""
    run = _make_run()
    run.mark_started()
    outcomes = [
        _FakeStepOutcome(step_id="s1", kind="open_app", status="skipped"),
    ]
    decision = _FakeRunnerDecision(
        blocked=False,
        result=_FakeMissionResult(status="success", outcomes=outcomes),
    )
    evaluate_decision(run, decision, consume_outcomes=True)
    # Sin ningún success real, no es un éxito honesto.
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == "WORKER_FAILED"


# ─── execute_with_lifecycle: integración con runner mock ──────────────


def test_execute_with_lifecycle_marks_started_when_runner_emits_status() -> None:
    """El primer ``on_status`` mueve el run a RUNNING (PRD §A.2)."""
    run = _make_run()

    def fake_runner(mission, *, on_status, on_human_help_needed,
                    on_step_done, expert_mode, allow_legacy_fallback,
                    **kwargs):
        on_status("Verificando Chrome…")
        on_step_done(_FakeStepOutcome(
            step_id="s1", kind="open_app", status="success",
        ))
        return _FakeRunnerDecision(
            blocked=False,
            result=_FakeMissionResult(
                status="success",
                outcomes=[_FakeStepOutcome(
                    step_id="s1", kind="open_app", status="success",
                )],
            ),
        )

    decision = execute_with_lifecycle(
        mission=object(), run=run, runner=fake_runner,
        on_step_done=None,
    )
    assert decision is not None
    # El status_callback en el helper hace mark_started; el run
    # debería estar en SUCCEEDED al final con trace honesta.
    assert run.status == ExecutionStatus.SUCCEEDED
    assert run.started_at is not None


def test_execute_with_lifecycle_runner_crash_marks_worker_exception() -> None:
    run = _make_run()

    def crashing_runner(*args, **kwargs):
        raise RuntimeError("bridge falló")

    decision = execute_with_lifecycle(
        mission=object(), run=run, runner=crashing_runner,
    )
    assert decision is None
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == WORKER_EXCEPTION
    assert "bridge falló" in run.error_message


def test_execute_with_lifecycle_records_step_started_for_each_outcome() -> None:
    """PRD §D: cada outcome aporta un ``step_started`` previo a su
    estado final, garantizando trace cronológica."""
    run = _make_run()

    def runner(mission, *, on_step_done, **kwargs):
        on_step_done(_FakeStepOutcome(
            step_id="s1", kind="open_app", status="success",
        ))
        on_step_done(_FakeStepOutcome(
            step_id="s2", kind="open_url", status="failed",
            error="404",
        ))
        return _FakeRunnerDecision(
            blocked=False,
            result=_FakeMissionResult(
                status="failed",
                outcomes=[
                    _FakeStepOutcome(step_id="s1", kind="open_app", status="success"),
                    _FakeStepOutcome(step_id="s2", kind="open_url", status="failed"),
                ],
            ),
        )

    execute_with_lifecycle(
        mission=object(), run=run, runner=runner,
    )
    statuses = [e.status for e in run.trace.entries]
    assert statuses == ["started", "succeeded", "started", "failed"]


def test_execute_with_lifecycle_propagates_step_done_to_ui_callback() -> None:
    """El callback original de UI (``on_step_done``) sigue recibiendo
    los outcomes — esencial para la barra de progreso del panel."""
    run = _make_run()
    seen: list = []

    def runner(mission, *, on_step_done, **kwargs):
        on_step_done(_FakeStepOutcome(
            step_id="s1", kind="open_app", status="success",
        ))
        return _FakeRunnerDecision(
            blocked=False,
            result=_FakeMissionResult(
                status="success",
                outcomes=[_FakeStepOutcome(
                    step_id="s1", kind="open_app", status="success",
                )],
            ),
        )

    execute_with_lifecycle(
        mission=object(), run=run, runner=runner,
        on_step_done=lambda o: seen.append(o.step_id),
    )
    assert seen == ["s1"]


# ─── Telemetría ───────────────────────────────────────────────────────


def test_evaluate_decision_sets_plan_source_from_decision() -> None:
    run = _make_run()
    run.mark_started()
    decision = _FakeRunnerDecision(
        blocked=False, source="semantic_execution_plan",
        result=_FakeMissionResult(
            status="success",
            outcomes=[_FakeStepOutcome(
                step_id="s1", kind="open_app", status="success",
            )],
        ),
    )
    evaluate_decision(run, decision, consume_outcomes=True)
    assert run.plan_source == "semantic_execution_plan"
