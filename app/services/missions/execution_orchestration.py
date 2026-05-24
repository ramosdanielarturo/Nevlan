"""
Nevlan — Execution Orchestration (PRD 2026-05-08c §exec-lifecycle)
====================================================================

Punto único donde una ejecución se materializa como un
:class:`ExecutionRun` honesto. Este módulo es **puro** (sin Qt) para
poder testearlo sin abrir UI ni montar el bridge real.

API pública:

  * :func:`execute_with_lifecycle` — toma una misión + un run vacío,
    delega en ``run_mission_smart`` y, sea cual sea el resultado,
    deja el run en estado terminal correcto (succeeded/failed/...).

  * :func:`evaluate_decision` — testeable a mano: recibe un
    ``RunnerDecision`` ficticio (mocks) y un ``ExecutionRun``, y
    aplica el lifecycle.

Reglas críticas (no se pueden saltar):

  1. Si ``decision.result is None`` y ``not decision.blocked`` ⇒
     ``mark_failed(EXECUTOR_NOT_INVOKED)``.
  2. Si ``decision.blocked`` ⇒ ``mark_failed(GATE_BLOCKED)`` con
     ``message=decision.reason``.
  3. Si ``decision.result.outcomes`` está vacía ⇒
     ``mark_failed(NO_STEPS_STARTED)``.
  4. Si todos los outcomes son ``success`` o ``skipped`` y al
     menos uno es ``success`` ⇒ ``mark_succeeded``.
  5. En cualquier otro caso ⇒ ``mark_failed("WORKER_FAILED")`` con
     mensaje del último error útil.
  6. Si la corrida arrojó excepción ⇒
     ``mark_failed_with_exception(exc)``.

Telemetría adicional:

  * ``run.plan_source`` se setea desde ``decision.source``.
  * ``run.executor_used`` queda como ``"smart"`` o ``"legacy"``.
  * Cada ``StepOutcome`` se traduce a un par
    (``mark_step_started`` + ``mark_step_*``) para que la trace
    sea cronológicamente honesta y rica.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Optional

from app.core.logger import log
from app.services.missions.execution_lifecycle import (
    EXECUTOR_NOT_INVOKED,
    GATE_BLOCKED,
    NO_STEPS_STARTED,
    PLAN_EMPTY_AT_RUNTIME,
    SEMANTIC_PLAN_NOT_PASSED_TO_EXECUTOR,
    STEP_STARTED,
    USER_CANCELLED,
    ExecutionRun,
    ExecutionStatus,
)


# Tipo opaco: en pruebas el caller pasa duck-typed mocks.
RunnerDecisionLike = Any
StepOutcomeLike = Any


def _trace_last_status_for_step(
    run: ExecutionRun, step_id: str,
) -> str:
    """Devuelve el ``status`` del último entry del trace para
    ``step_id`` (o ``""`` si el step nunca se registró).

    Útil para deduplicar ``mark_step_started``: si el callback
    ``on_step_started`` ya marcó el step como ``started``, el
    posterior ``_record_outcome`` no debe re-añadir otra entrada
    ``started`` (eso duplica la trace y rompe ``all_steps_succeeded``).
    """
    for e in reversed(run.trace.entries):
        if e.step_id == step_id:
            return e.status
    return ""


def _record_outcome(
    run: ExecutionRun, outcome: StepOutcomeLike,
) -> None:
    """Traduce un :class:`smart_executor.StepOutcome` al lifecycle."""
    step_id = str(getattr(outcome, "step_id", "") or "")
    kind = str(getattr(outcome, "kind", "") or "")
    status = str(getattr(outcome, "status", "") or "")
    strategy = str(getattr(outcome, "strategy_used", "") or "")
    duration = int(getattr(outcome, "duration_ms", 0) or 0)
    error = str(getattr(outcome, "error", "") or "")
    state_after = getattr(outcome, "state_after", None) or {}

    # PRD 2026-05-09 §exec-progress: si el callback ``on_step_started``
    # ya añadió la entrada ``started`` para este step, no la
    # duplicamos. Mantenemos la backward-compat (tests/legacy) cuando
    # el runner no usa ``on_step_started``: en ese caso el trace
    # estaría vacío y debemos emitir el ``started`` aquí.
    last_status = _trace_last_status_for_step(run, step_id)
    if last_status != STEP_STARTED:
        try:
            run.mark_step_started(step_id, kind)
        except Exception:
            # Si el run ya estaba terminal (race), abortamos silencioso —
            # un terminal posterior ganará igualmente.
            return

    try:
        if status == "success":
            run.mark_step_succeeded(
                step_id,
                strategy_used=strategy,
                evidence={"state_after": state_after},
                duration_ms=duration,
            )
        elif status == "skipped":
            run.mark_step_skipped(
                step_id, reason=error or "skipped", duration_ms=duration,
            )
        elif status == "needs_human":
            run.mark_step_needs_human(
                step_id, reason=error or "needs_human",
            )
        elif status == "failed":
            run.mark_step_failed(
                step_id,
                error=error or "step_failed",
                strategy_used=strategy,
                duration_ms=duration,
            )
        else:
            run.mark_step_failed(
                step_id,
                error=f"unknown_outcome_status:{status}",
                strategy_used=strategy,
                duration_ms=duration,
            )
    except Exception as e:
        log.debug(f"_record_outcome: ignorando transición tras terminal: {e}")


def evaluate_decision(
    run: ExecutionRun,
    decision: RunnerDecisionLike,
    *,
    consume_outcomes: bool = True,
) -> None:
    """Aplica el lifecycle al ``run`` según un ``RunnerDecision``.

    ``consume_outcomes=False`` se usa cuando los outcomes ya fueron
    registrados antes (vía callback ``on_step_done`` que escribió
    en el run). En ese caso solo ajustamos el estado terminal final.
    """
    if run.is_terminal:
        return

    blocked = bool(getattr(decision, "blocked", False))
    reason = str(getattr(decision, "reason", "") or "")
    result = getattr(decision, "result", None)
    source = str(getattr(decision, "source", "") or "")

    if source:
        run.plan_source = source

    if blocked:
        run.mark_failed(GATE_BLOCKED, message=reason or "Misión bloqueada antes de empezar.")
        return

    if result is None:
        # No hubo executor invocado: o falló el bridge antes de
        # arrancar, o la misión cayó por una rama legacy/blocked
        # silenciosa.
        run.mark_failed(
            EXECUTOR_NOT_INVOKED,
            message=reason or "El bridge no invocó el executor.",
        )
        return

    outcomes = list(getattr(result, "outcomes", []) or [])
    if consume_outcomes:
        for o in outcomes:
            _record_outcome(run, o)

    last_message = str(getattr(result, "last_message", "") or "")
    final_status = str(getattr(result, "status", "") or "")

    if not outcomes and run.trace.is_empty:
        # PRD §A.6: ningún paso inició.
        if final_status in ("success", "succeeded"):
            run.mark_failed(
                NO_STEPS_STARTED,
                message=(
                    "El executor reportó success sin haber registrado "
                    "ningún step en la trace."
                ),
            )
        else:
            run.mark_failed(
                NO_STEPS_STARTED,
                message=last_message or "Ningún step inició.",
            )
        return

    # Llegamos aquí: hay al menos un outcome / entry de trace.
    if not run.trace.has_any_succeeded:
        run.mark_failed(
            "WORKER_FAILED",
            message=last_message or "Ningún step llegó a succeeded.",
        )
        return

    if run.trace.all_steps_succeeded and final_status in ("success", "succeeded"):
        run.mark_succeeded()
    else:
        # Hubo éxitos parciales pero el executor reporta fallo o
        # alguno quedó en "needs_human"/"failed".
        run.mark_failed(
            "WORKER_FAILED",
            message=last_message or "La misión no completó todos los steps.",
        )


def execute_with_lifecycle(
    mission: Any,
    run: ExecutionRun,
    *,
    runner: Callable[..., Any],
    on_status: Optional[Callable[[str], None]] = None,
    on_human_help_needed: Optional[Callable[[Any], None]] = None,
    on_step_done: Optional[Callable[[Any], None]] = None,
    on_step_started: Optional[Callable[[Any], None]] = None,
    on_step_outcome_for_run: bool = True,
    expert_mode: bool = False,
    allow_legacy_fallback: bool = False,
    extra_runner_kwargs: Optional[dict] = None,
) -> "RunnerDecisionLike":
    """Orquestador honesto que delega en ``runner`` (típicamente
    ``run_mission_smart``) y mantiene ``run`` consistente con el
    PRD §A.

    El callable ``runner`` debe respetar la firma de
    :func:`run_mission_smart`. Devuelve el ``RunnerDecision`` real
    (útil para logs/telemetría adicionales).

    El run se marca como ``starting`` antes de invocar al runner y
    como ``running`` cuando el runner empieza a emitir progreso
    (primer ``on_status`` o primer step). Si nunca emite nada, el
    estado terminal será ``failed/NO_STEPS_STARTED``.
    """
    extra = dict(extra_runner_kwargs or {})

    if not run.executor_used:
        run.executor_used = "smart"

    started_flag = {"v": False}

    def _ensure_started() -> None:
        if started_flag["v"]:
            return
        started_flag["v"] = True
        try:
            run.mark_started(executor_used=run.executor_used or "smart")
        except Exception as e:
            log.debug(f"execute_with_lifecycle: mark_started ignorado: {e}")

    def _wrapped_status(msg: str) -> None:
        _ensure_started()
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    def _wrapped_step_done(outcome: Any) -> None:
        _ensure_started()
        if on_step_outcome_for_run:
            _record_outcome(run, outcome)
        if on_step_done is not None:
            try:
                on_step_done(outcome)
            except Exception as e:
                log.debug(f"execute_with_lifecycle: on_step_done falló: {e}")

    def _wrapped_step_started(payload: Any) -> None:
        """PRD 2026-05-09 §exec-progress: el smart_executor llama esto
        ANTES de ejecutar cada step. Marcamos el lifecycle como
        ``started`` al instante (UI mueve "paso 0" → "paso 1") y
        propagamos al callback del caller (la UI usa ``index`` y
        ``total`` para el label).
        """
        _ensure_started()
        if on_step_outcome_for_run:
            try:
                step_id = str(getattr(payload, "step_id", "") or "")
                kind = str(getattr(payload, "kind", "") or "")
                if step_id:
                    run.mark_step_started(step_id, kind)
            except Exception as e:
                log.debug(
                    f"execute_with_lifecycle: mark_step_started falló: {e}"
                )
        if on_step_started is not None:
            try:
                on_step_started(payload)
            except Exception as e:
                log.debug(
                    f"execute_with_lifecycle: on_step_started falló: {e}"
                )

    try:
        decision = runner(
            mission,
            on_status=_wrapped_status,
            on_human_help_needed=on_human_help_needed,
            on_step_done=_wrapped_step_done,
            on_step_started=_wrapped_step_started,
            expert_mode=expert_mode,
            allow_legacy_fallback=allow_legacy_fallback,
            **extra,
        )
    except BaseException as e:  # captura amplia: cualquier crash
        run.mark_failed_with_exception(e)
        log.exception(f"execute_with_lifecycle: runner crashed: {e}")
        return None

    # Si el runner devolvió sin emitir nada y nunca llamamos
    # mark_started (caso ``decision.blocked`` retornado de inmediato),
    # el evaluate_decision se encargará de mover el lifecycle.
    evaluate_decision(run, decision, consume_outcomes=False)

    run.repair_ui_hint = None
    try:
        if run.is_terminal and run.status == ExecutionStatus.FAILED:
            skip_ec = frozenset({
                str(GATE_BLOCKED),
                str(EXECUTOR_NOT_INVOKED),
                str(NO_STEPS_STARTED),
                str(USER_CANCELLED),
                "",
            })
            if str(run.error_code or "") not in skip_ec:
                from app.services.missions.repair_context import (
                    infer_repair_brief_from_run,
                )

                rb = infer_repair_brief_from_run(
                    run, mission, run_error_message=str(run.error_message or ""),
                )
                if rb is not None:
                    run.repair_ui_hint = asdict(rb)
    except Exception as e:
        log.debug(f"execute_with_lifecycle: repair_ui_hint omitido: {e}")
    return decision


__all__ = [
    "evaluate_decision",
    "execute_with_lifecycle",
]
