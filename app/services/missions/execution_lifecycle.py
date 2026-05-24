"""
Nevlan — Execution Lifecycle (PRD 2026-05-08c §exec-lifecycle)
================================================================

Modelo único de "qué pasó realmente" cuando el usuario presiona
**Ejecutar**. Antes la decisión final flotaba entre el
``ExecutionTracker`` (status string libre), el ``RunnerDecision``
del bridge (con ``decision.result`` opcional) y la ventana
``ExecutionControlWidget`` (que se cerraba con timers ad-hoc).
Resultado: el historial decía "Ejecutado" aunque el worker
nunca hubiese arrancado un solo paso.

Este módulo introduce la **fuente única de verdad** del ciclo
de vida de una ejecución:

  * :class:`ExecutionStatus` — enum cerrado de estados.
  * :class:`StepExecutionEntry` — un evento por step real
    (no por click humano).
  * :class:`ExecutionTrace` — la lista cronológica de events.
  * :class:`ExecutionRun` — agregado mutable que la UI/bridge
    actualizan vía ``mark_*``.
  * :func:`derive_history_label` — string honesto para el
    historial. NUNCA dice "Ejecutado" si el trace está vacío.

Reglas duras (no negociables):

  1. ``mark_succeeded()`` exige al menos un step ``succeeded``
     en el trace. Si está vacío, FALLA con
     :class:`ExecutionLifecycleError` y el caller debe usar
     :meth:`ExecutionRun.mark_failed_no_steps_started`.
  2. Estado terminal (``succeeded``/``failed``/``cancelled``/
     ``timed_out``/``stuck``) es **inmutable**: una segunda
     llamada ``mark_*`` lanza la excepción. Esto evita races
     entre cierre de ventana, worker thread, cancel manual.
  3. Cualquier excepción del worker debe pasar por
     :meth:`mark_failed_with_exception` — esa transición lo
     registra en ``error_code="WORKER_EXCEPTION"`` y deja
     terminal_signal_received=True para que el watchdog cierre
     la ventana.

Uso típico (UI + worker):

    run = ExecutionRun(mission_id=mid, mission_name=name, total_steps=6)
    run.mark_started()                      # status=running
    run.mark_step_started("s1", "open_app") # status=step_running
    run.mark_step_succeeded("s1", strategy_used="windows_search")
    ...
    if all_steps_ok:
        run.mark_succeeded()
    else:
        run.mark_failed(NO_STEPS_STARTED)

    label = derive_history_label(run)
    # "Ejecutado" / "Falló" / "No iniciado" / "Cancelado" / "Sin respuesta"
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ─── Estados terminales y no terminales (PRD §A) ──────────────────────


class ExecutionStatus(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    STEP_RUNNING = "step_running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    STUCK = "stuck"


# Conjunto inmutable (terminales): ningún ``mark_*`` posterior está
# permitido. Útil para validar transiciones y para que el tracker
# decida si registrar terminal_signal_received.
TERMINAL_STATUSES: frozenset = frozenset({
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.TIMED_OUT,
    ExecutionStatus.STUCK,
})


# ─── Códigos de error (PRD §D) ────────────────────────────────────────


# El worker corrió 0 steps a pesar de no estar bloqueado por gate.
NO_STEPS_STARTED = "NO_STEPS_STARTED"
# El bridge nunca llamó al executor (ej. ``decision.result is None``
# y ``decision.blocked is False``).
EXECUTOR_NOT_INVOKED = "EXECUTOR_NOT_INVOKED"
# El thread del worker nunca arrancó (ej. crash antes de ``mark_started``).
WORKER_DID_NOT_START = "WORKER_DID_NOT_START"
# El plan llegó vacío al runtime ⇒ no hay nada que ejecutar.
PLAN_EMPTY_AT_RUNTIME = "PLAN_EMPTY_AT_RUNTIME"
# El bridge recibió la misión pero el plan semántico nunca se pasó
# al SmartMissionExecutor.
SEMANTIC_PLAN_NOT_PASSED_TO_EXECUTOR = "SEMANTIC_PLAN_NOT_PASSED_TO_EXECUTOR"
# El worker terminó (thread join) sin emitir ``finished_*`` — el
# watchdog cerró la ventana porque no se podía esperar más.
WORKER_FINISHED_WITHOUT_TERMINAL_SIGNAL = (
    "WORKER_FINISHED_WITHOUT_TERMINAL_SIGNAL"
)
# La ventana flotante alcanzó timeout (heartbeat sin progreso).
PROGRESS_DIALOG_TIMEOUT = "PROGRESS_DIALOG_TIMEOUT"
# El usuario pulsó Cancelar.
USER_CANCELLED = "USER_CANCELLED"
# Excepción no esperada en el worker (try/except final del thread).
WORKER_EXCEPTION = "WORKER_EXCEPTION"
# Misión bloqueada por approval gate antes de empezar.
GATE_BLOCKED = "GATE_BLOCKED"
# Tipo semántico no soportado por el runtime: el executor encontró un
# step.kind para el que NO hay builder/executor registrado. Falla
# visible (no se cuelga, no se marca success) y deja en historial:
#   error_code=UNSUPPORTED_SEMANTIC_STEP_TYPE
# El message incluye step_id, step_type, human_label y la lista de
# executors disponibles para que el usuario sepa qué cambiar.
UNSUPPORTED_SEMANTIC_STEP_TYPE = "UNSUPPORTED_SEMANTIC_STEP_TYPE"
# select_profile no pudo resolver el perfil pedido (profile_name
# inválido, profile picker invisible, perfil no listado, etc.). Falla
# visible en lugar de quedarse en bucle de recovery indefinido.
PROFILE_SELECTION_FAILED = "PROFILE_SELECTION_FAILED"


KNOWN_ERROR_CODES: frozenset = frozenset({
    NO_STEPS_STARTED,
    EXECUTOR_NOT_INVOKED,
    WORKER_DID_NOT_START,
    PLAN_EMPTY_AT_RUNTIME,
    SEMANTIC_PLAN_NOT_PASSED_TO_EXECUTOR,
    WORKER_FINISHED_WITHOUT_TERMINAL_SIGNAL,
    PROGRESS_DIALOG_TIMEOUT,
    USER_CANCELLED,
    WORKER_EXCEPTION,
    GATE_BLOCKED,
    UNSUPPORTED_SEMANTIC_STEP_TYPE,
    PROFILE_SELECTION_FAILED,
})


class ExecutionLifecycleError(Exception):
    """Transición inválida del lifecycle (terminal → algo, etc.)."""


# ─── Trace por step (PRD §D) ──────────────────────────────────────────


# Estado por step.
STEP_STARTED = "started"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_NEEDS_HUMAN = "needs_human"


_VALID_STEP_STATUSES: frozenset = frozenset({
    STEP_STARTED, STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED,
    STEP_NEEDS_HUMAN,
})


@dataclass
class StepExecutionEntry:
    """Un evento por step real ejecutado por el runtime.

    NO confundir con ``StepLine`` del summary (que describe el
    plan estático). Aquí cada ``StepExecutionEntry`` es prueba
    de que ese step **realmente fue invocado**.
    """

    step_id: str
    step_type: str
    status: str
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    finished_at: Optional[datetime] = None
    executor_used: str = ""
    strategy_used: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_type": self.step_type,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "executor_used": self.executor_used,
            "strategy_used": self.strategy_used,
            "evidence": dict(self.evidence or {}),
            "error": self.error,
            "duration_ms": int(self.duration_ms or 0),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StepExecutionEntry":
        def _dt(v: Any) -> Optional[datetime]:
            if not v:
                return None
            try:
                return datetime.fromisoformat(str(v))
            except Exception:
                return None
        return cls(
            step_id=str(d.get("step_id") or ""),
            step_type=str(d.get("step_type") or ""),
            status=str(d.get("status") or STEP_STARTED),
            started_at=_dt(d.get("started_at")) or datetime.now(timezone.utc),
            finished_at=_dt(d.get("finished_at")),
            executor_used=str(d.get("executor_used") or ""),
            strategy_used=str(d.get("strategy_used") or ""),
            evidence=dict(d.get("evidence") or {}),
            error=d.get("error"),
            duration_ms=int(d.get("duration_ms") or 0),
        )


@dataclass
class ExecutionTrace:
    """Lista cronológica de :class:`StepExecutionEntry`.

    Es la fuente de verdad sobre **qué pasos realmente corrieron**.
    El historial NUNCA puede afirmar "Ejecutado" si esta lista está
    vacía o no tiene ningún entry con ``status="succeeded"``.
    """

    entries: List[StepExecutionEntry] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def started_step_ids(self) -> List[str]:
        return [e.step_id for e in self.entries if e.status == STEP_STARTED]

    @property
    def succeeded_step_ids(self) -> List[str]:
        return [e.step_id for e in self.entries if e.status == STEP_SUCCEEDED]

    @property
    def failed_step_ids(self) -> List[str]:
        return [e.step_id for e in self.entries if e.status == STEP_FAILED]

    @property
    def has_any_succeeded(self) -> bool:
        return any(e.status == STEP_SUCCEEDED for e in self.entries)

    @property
    def all_steps_succeeded(self) -> bool:
        """True ⇔ existe al menos un entry y todos los entries finales
        de cada ``step_id`` son ``succeeded`` o ``skipped``.

        ``started`` sin terminal posterior se considera incompleto.
        """
        if not self.entries:
            return False
        last_by_step: Dict[str, str] = {}
        for e in self.entries:
            last_by_step[e.step_id] = e.status
        return all(
            st in (STEP_SUCCEEDED, STEP_SKIPPED)
            for st in last_by_step.values()
        )

    def to_list(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.entries]

    @classmethod
    def from_list(cls, items: List[Dict[str, Any]]) -> "ExecutionTrace":
        return cls(entries=[StepExecutionEntry.from_dict(d) for d in (items or [])])


# ─── Run agregado (PRD §A + §B + §C) ──────────────────────────────────


@dataclass
class ExecutionRun:
    """Snapshot mutable del ciclo de vida COMPLETO de una ejecución.

    El caller (UI + worker thread) usa los métodos ``mark_*`` para
    transitar. Las transiciones inválidas levantan
    :class:`ExecutionLifecycleError`.
    """

    mission_id: str
    mission_name: str = ""
    total_steps: int = 0
    trigger: str = "manual"
    plan_source: str = ""        # "semantic_execution_plan" | "compiled_execution_graph"
    executor_used: str = ""      # "smart" | "auto_learning" | "legacy"
    status: ExecutionStatus = ExecutionStatus.QUEUED
    error_code: str = ""
    error_message: str = ""
    last_progress_at: Optional[datetime] = None
    queued_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    terminal_signal_received: bool = False
    trace: ExecutionTrace = field(default_factory=ExecutionTrace)
    # Pista serializada (dataclass asdict de RepairBrief) cuando el runtime
    # detecta fallo localizable tras una ejecución smart — evita races UI.
    repair_ui_hint: Optional[Dict[str, Any]] = None
    # Lock interno: las transiciones se sincronizan para evitar que
    # un cancel manual + un ``finished`` natural se pisen.
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False, compare=False,
    )

    # ─── helpers ───────────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def duration_ms(self) -> int:
        if self.started_at is None:
            return 0
        end = self.finished_at or datetime.now(timezone.utc)
        return int((end - self.started_at).total_seconds() * 1000)

    def _require_non_terminal(self, what: str) -> None:
        if self.status in TERMINAL_STATUSES:
            raise ExecutionLifecycleError(
                f"{what}: la ejecución ya está en estado terminal "
                f"{self.status.value!r}; no se puede transicionar."
            )

    def _stamp_progress(self) -> None:
        self.last_progress_at = datetime.now(timezone.utc)

    # ─── transiciones públicas ────────────────────────────────

    def mark_started(self, *, executor_used: str = "") -> None:
        """El worker realmente arrancó. ``status=running``."""
        with self._lock:
            self._require_non_terminal("mark_started")
            self.status = ExecutionStatus.RUNNING
            if executor_used:
                self.executor_used = executor_used
            self.started_at = datetime.now(timezone.utc)
            self._stamp_progress()

    def mark_step_started(
        self,
        step_id: str,
        step_type: str,
        *,
        executor_used: str = "",
    ) -> StepExecutionEntry:
        with self._lock:
            self._require_non_terminal("mark_step_started")
            entry = StepExecutionEntry(
                step_id=step_id,
                step_type=step_type,
                status=STEP_STARTED,
                executor_used=executor_used or self.executor_used,
            )
            self.trace.entries.append(entry)
            self.status = ExecutionStatus.STEP_RUNNING
            self._stamp_progress()
            return entry

    def mark_step_succeeded(
        self,
        step_id: str,
        *,
        strategy_used: str = "",
        evidence: Optional[Dict[str, Any]] = None,
        duration_ms: int = 0,
    ) -> StepExecutionEntry:
        with self._lock:
            self._require_non_terminal("mark_step_succeeded")
            entry = StepExecutionEntry(
                step_id=step_id,
                step_type=self._step_type_from_history(step_id),
                status=STEP_SUCCEEDED,
                finished_at=datetime.now(timezone.utc),
                executor_used=self.executor_used,
                strategy_used=strategy_used,
                evidence=dict(evidence or {}),
                duration_ms=int(duration_ms or 0),
            )
            self.trace.entries.append(entry)
            self.status = ExecutionStatus.RUNNING
            self._stamp_progress()
            return entry

    def mark_step_skipped(
        self,
        step_id: str,
        *,
        reason: str = "",
        duration_ms: int = 0,
    ) -> StepExecutionEntry:
        with self._lock:
            self._require_non_terminal("mark_step_skipped")
            entry = StepExecutionEntry(
                step_id=step_id,
                step_type=self._step_type_from_history(step_id),
                status=STEP_SKIPPED,
                finished_at=datetime.now(timezone.utc),
                executor_used=self.executor_used,
                error=reason or None,
                duration_ms=int(duration_ms or 0),
            )
            self.trace.entries.append(entry)
            self.status = ExecutionStatus.RUNNING
            self._stamp_progress()
            return entry

    def mark_step_failed(
        self,
        step_id: str,
        *,
        error: str = "",
        strategy_used: str = "",
        duration_ms: int = 0,
    ) -> StepExecutionEntry:
        with self._lock:
            self._require_non_terminal("mark_step_failed")
            entry = StepExecutionEntry(
                step_id=step_id,
                step_type=self._step_type_from_history(step_id),
                status=STEP_FAILED,
                finished_at=datetime.now(timezone.utc),
                executor_used=self.executor_used,
                strategy_used=strategy_used,
                error=error or None,
                duration_ms=int(duration_ms or 0),
            )
            self.trace.entries.append(entry)
            self.status = ExecutionStatus.RUNNING
            self._stamp_progress()
            return entry

    def mark_step_needs_human(
        self,
        step_id: str,
        *,
        reason: str = "",
    ) -> StepExecutionEntry:
        with self._lock:
            self._require_non_terminal("mark_step_needs_human")
            entry = StepExecutionEntry(
                step_id=step_id,
                step_type=self._step_type_from_history(step_id),
                status=STEP_NEEDS_HUMAN,
                finished_at=datetime.now(timezone.utc),
                executor_used=self.executor_used,
                error=reason or None,
            )
            self.trace.entries.append(entry)
            self._stamp_progress()
            return entry

    # ─── transiciones terminales ──────────────────────────────

    def mark_succeeded(self) -> None:
        """Ejecución completa y exitosa. Exige trace no vacío con
        todos los steps en ``succeeded``/``skipped`` y ningún
        ``failed``/``needs_human``/``started`` colgado."""
        with self._lock:
            self._require_non_terminal("mark_succeeded")
            if self.trace.is_empty:
                raise ExecutionLifecycleError(
                    "mark_succeeded: el execution_trace está vacío. "
                    "No se puede marcar como exitosa una ejecución "
                    "sin pasos. Usa mark_failed_no_steps_started."
                )
            if not self.trace.has_any_succeeded:
                raise ExecutionLifecycleError(
                    "mark_succeeded: el trace no tiene ningún step "
                    "succeeded. No se permite marcar éxito sin "
                    "evidencia de al menos un paso completado."
                )
            if not self.trace.all_steps_succeeded:
                raise ExecutionLifecycleError(
                    "mark_succeeded: hay steps cuyo estado final NO "
                    "es succeeded/skipped (por ejemplo 'failed' o "
                    "'started' sin terminar). No se puede marcar "
                    "éxito en una corrida con pasos incompletos."
                )
            self.status = ExecutionStatus.SUCCEEDED
            self.error_code = ""
            self.error_message = ""
            self.finished_at = datetime.now(timezone.utc)
            self.terminal_signal_received = True

    def mark_failed(
        self,
        error_code: str,
        *,
        message: str = "",
    ) -> None:
        with self._lock:
            self._require_non_terminal("mark_failed")
            if not error_code or not error_code.strip():
                error_code = "WORKER_FAILED"
            self.status = ExecutionStatus.FAILED
            self.error_code = error_code
            self.error_message = message or ""
            self.finished_at = datetime.now(timezone.utc)
            self.terminal_signal_received = True

    def mark_failed_no_steps_started(self, *, message: str = "") -> None:
        """Atajo para el caso "el worker no arrancó ningún paso".
        Forza ``error_code=NO_STEPS_STARTED`` aunque el caller
        olvide pasarlo."""
        self.mark_failed(NO_STEPS_STARTED, message=message)

    def mark_failed_with_exception(self, exc: BaseException) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        self.mark_failed(WORKER_EXCEPTION, message=msg)

    def mark_cancelled(self, *, message: str = "") -> None:
        with self._lock:
            self._require_non_terminal("mark_cancelled")
            self.status = ExecutionStatus.CANCELLED
            self.error_code = USER_CANCELLED
            self.error_message = message or "El usuario canceló la ejecución."
            self.finished_at = datetime.now(timezone.utc)
            self.terminal_signal_received = True

    def mark_timed_out(self, *, message: str = "") -> None:
        with self._lock:
            self._require_non_terminal("mark_timed_out")
            self.status = ExecutionStatus.TIMED_OUT
            self.error_code = PROGRESS_DIALOG_TIMEOUT
            self.error_message = message or (
                "La ejecución excedió el tiempo máximo permitido."
            )
            self.finished_at = datetime.now(timezone.utc)
            self.terminal_signal_received = True

    def mark_stuck(self, *, message: str = "") -> None:
        with self._lock:
            self._require_non_terminal("mark_stuck")
            self.status = ExecutionStatus.STUCK
            self.error_code = self.error_code or PROGRESS_DIALOG_TIMEOUT
            self.error_message = message or (
                "El worker dejó de responder (sin heartbeat)."
            )
            self.finished_at = datetime.now(timezone.utc)
            self.terminal_signal_received = True

    def force_terminal_for_dangling_worker(self) -> None:
        """Watchdog: el worker terminó sin emitir terminal — la
        ventana se queda colgada. Forza ``failed`` con
        ``WORKER_FINISHED_WITHOUT_TERMINAL_SIGNAL``.
        """
        with self._lock:
            if self.status in TERMINAL_STATUSES:
                return
            self.mark_failed(
                WORKER_FINISHED_WITHOUT_TERMINAL_SIGNAL,
                message=(
                    "El worker terminó sin emitir señal terminal "
                    "(success/failure/cancel/timeout). El watchdog "
                    "cerró la ventana de ejecución."
                ),
            )

    # ─── serialización ────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "mission_name": self.mission_name,
            "total_steps": int(self.total_steps or 0),
            "trigger": self.trigger,
            "plan_source": self.plan_source,
            "executor_used": self.executor_used,
            "status": self.status.value,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "last_progress_at": (
                self.last_progress_at.isoformat()
                if self.last_progress_at else None
            ),
            "terminal_signal_received": bool(self.terminal_signal_received),
            "duration_ms": self.duration_ms,
            "execution_trace": self.trace.to_list(),
            "started_step_ids": list(self.trace.started_step_ids),
            "succeeded_step_ids": list(self.trace.succeeded_step_ids),
            "failed_step_ids": list(self.trace.failed_step_ids),
        }

    # ─── internals ────────────────────────────────────────────

    def _step_type_from_history(self, step_id: str) -> str:
        for e in reversed(self.trace.entries):
            if e.step_id == step_id and e.step_type:
                return e.step_type
        return ""


# ─── Historial honesto (PRD §C) ───────────────────────────────────────


# Mapa estado → label visible en historial. NUNCA usamos
# "Ejecutado" para algo que no sea ``ExecutionStatus.SUCCEEDED``.
_HISTORY_LABELS = {
    ExecutionStatus.QUEUED: "En cola",
    ExecutionStatus.STARTING: "Arrancando",
    ExecutionStatus.RUNNING: "En ejecución",
    ExecutionStatus.STEP_RUNNING: "En ejecución",
    ExecutionStatus.SUCCEEDED: "Ejecutado",
    ExecutionStatus.CANCELLED: "Cancelado",
    ExecutionStatus.TIMED_OUT: "Sin respuesta",
    ExecutionStatus.STUCK: "Sin respuesta",
}


def derive_history_label(run: ExecutionRun) -> str:
    """String honesto para mostrar en el historial.

    Reglas (PRD §C):
      * ``succeeded`` → "Ejecutado".
      * ``failed`` con ``error_code=NO_STEPS_STARTED`` → "No iniciado".
      * ``failed`` general → "Falló".
      * ``cancelled`` → "Cancelado".
      * ``timed_out`` / ``stuck`` → "Sin respuesta".
      * Otros (queued/starting/running/step_running) → "En ejecución".
    """
    if run.status == ExecutionStatus.FAILED:
        if run.error_code == NO_STEPS_STARTED:
            return "No iniciado"
        return "Falló"
    return _HISTORY_LABELS.get(run.status, "En ejecución")


def is_history_executed(run: ExecutionRun) -> bool:
    """¿Este run debe aparecer como "Ejecutado" en el historial?

    Sí solo si el lifecycle dice ``succeeded`` Y el trace tiene
    al menos un step ``succeeded``. Doble seguro contra runs
    persistidos con status incoherente.
    """
    return (
        run.status == ExecutionStatus.SUCCEEDED
        and run.trace.has_any_succeeded
    )


def derive_history_icon(run: ExecutionRun) -> str:
    if run.status == ExecutionStatus.SUCCEEDED:
        return "✅"
    if run.status == ExecutionStatus.FAILED:
        return "❌"
    if run.status == ExecutionStatus.CANCELLED:
        return "⏹"
    if run.status in (ExecutionStatus.TIMED_OUT, ExecutionStatus.STUCK):
        return "⏱"
    return "▶"


__all__ = [
    "ExecutionLifecycleError",
    "ExecutionRun",
    "ExecutionStatus",
    "ExecutionTrace",
    "StepExecutionEntry",
    "TERMINAL_STATUSES",
    "STEP_STARTED",
    "STEP_SUCCEEDED",
    "STEP_FAILED",
    "STEP_SKIPPED",
    "STEP_NEEDS_HUMAN",
    "NO_STEPS_STARTED",
    "EXECUTOR_NOT_INVOKED",
    "WORKER_DID_NOT_START",
    "PLAN_EMPTY_AT_RUNTIME",
    "SEMANTIC_PLAN_NOT_PASSED_TO_EXECUTOR",
    "WORKER_FINISHED_WITHOUT_TERMINAL_SIGNAL",
    "PROGRESS_DIALOG_TIMEOUT",
    "USER_CANCELLED",
    "WORKER_EXCEPTION",
    "GATE_BLOCKED",
    "UNSUPPORTED_SEMANTIC_STEP_TYPE",
    "PROFILE_SELECTION_FAILED",
    "KNOWN_ERROR_CODES",
    "derive_history_label",
    "derive_history_icon",
    "is_history_executed",
]
