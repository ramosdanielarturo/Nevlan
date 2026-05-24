"""
Nevlan — Rerecord Session Orchestrator (PRD 2026-05-08c §E + §F)
=================================================================

Servicio sin Qt que orquesta una sesión de regrabación de un paso N
con replay previo de los pasos 1..N-1.

Contrato (idéntico al doc ``docs/plan_regrabacion_paso_replay.md``):

    1. ``start(mission, target_step_index)`` crea una sesión y
       captura un snapshot inmutable de la misión para poder
       restaurar bit-perfecto en ``cancel()``.
    2. ``proceed_to_capture()`` ejecuta los pasos 0..N-1 del
       ``semantic_execution_plan`` mediante un ``executor`` inyectable
       (default: ``SmartMissionExecutor``) y deja la sesión en
       ``state="waiting_for_user"``. Si falla, ``state="failed"`` con
       ``error_message`` no vacío.
    3. ``capture_user_event(evidence)`` recibe la evidencia capturada
       por el overlay y construye el ``replacement_step``. Aplica
       validación mínima — captura inválida ⇒ ``state="waiting_for_user"``
       (NO consume el intento).
    4. ``save()`` invoca ``mission_review_confirm.rerecord_step`` con
       la evidencia, persiste audit trail en ``rerecord_history`` y
       recomputa ``intent_status``. Devuelve el plan resultante.
    5. ``cancel()`` restaura el snapshot inicial sin tocar disco y
       libera workers/listeners.

Diseño deliberado
-----------------

* **Sin Qt**. El módulo es pure-python para que pueda llamarse desde
  tests headless y desde la UI por igual. La UI corre el orchestrator
  en un ``QThread`` o ``QObject.moveToThread`` para no congelar.
* **Inyección del executor**. En tests reemplazamos
  ``executor_factory`` por un stub que no toque el sistema.
* **Cooperative cancel**. Antes de cada step el replay revisa
  ``is_cancel_requested``. Si el usuario presiona Cancelar, el
  worker termina sin completar la cadena.
* **Snapshot bit-perfecto**. Usamos ``mission.model_dump`` (Pydantic)
  para serializar a dict y restaurar con ``mission.model_validate``
  en ``cancel()`` — más robusto que ``copy.deepcopy`` para
  estructuras anidadas con ``BaseModel``.
* **Idempotencia de ``save``**. Llamar dos veces no duplica entradas
  en ``rerecord_history`` ni reemplaza dos veces el step
  (segunda llamada lanza ``RerecordSessionError``).

Flujo típico (UI):

    orch = RerecordOrchestrator()
    session = orch.start(mission, target_step_index=4)
    # Worker thread: orch.proceed_to_capture()
    # Cuando session.state == "waiting_for_user", abrir captura.
    orch.capture_user_event(evidence)
    plan = orch.save()
    # Cierre limpio:
    orch.release()

Estados:

    "idle"              → recién creado, sin start
    "preparing"         → start() llamado, antes de replay
    "replaying"         → proceed_to_capture() en curso
    "waiting_for_user"  → replay terminado, esperando captura
    "captured"          → user_event recibido y validado
    "saved"             → save() exitoso
    "cancelled"         → cancel() (snapshot restaurado)
    "failed"            → replay falló, ``error_message`` no vacío
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.contracts.mission import (
    Mission,
    RerecordHistoryEntry,
)
from app.core.logger import log


# ─── Estados ────────────────────────────────────────────────────────────

STATE_IDLE = "idle"
STATE_PREPARING = "preparing"
STATE_REPLAYING = "replaying"
STATE_WAITING_FOR_USER = "waiting_for_user"
STATE_CAPTURED = "captured"
STATE_SAVED = "saved"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"


# Frase fija para errores de replay (PRD §6).
REPLAY_FAILED_MESSAGE = "No pude llegar automáticamente al paso a regrabar."


class RerecordSessionError(Exception):
    """Se lanza cuando el orchestrator recibe una llamada inválida
    para su estado actual (ej. ``save()`` antes de capturar)."""


# ─── Tipos de callbacks ─────────────────────────────────────────────────

ProgressCb = Callable[[int, int, str], None]
"""Firma: ``(step_index, total_steps, human_label)``."""

StateChangeCb = Callable[[str], None]
"""Firma: ``(new_state)``."""


# ─── Sesión ─────────────────────────────────────────────────────────────


@dataclass
class RerecordSession:
    """Snapshot mutable del estado de la sesión.

    La UI lee estos campos para renderizar progreso, copy y botones.
    El orchestrator es el único autorizado para mutarlos — el caller
    NO debe modificarlos directamente.
    """

    rerecord_session_id: str
    mission_id: str
    target_step_id: str
    target_step_index: int
    original_step_snapshot: Dict[str, Any]
    state: str = STATE_IDLE
    progress: float = 0.0
    progress_step_index: int = 0
    progress_total_steps: int = 0
    progress_label: str = ""
    error_message: str = ""
    error_failed_step_id: Optional[str] = None
    captured_evidence: List[Dict[str, Any]] = field(default_factory=list)
    replacement_step_snapshot: Dict[str, Any] = field(default_factory=dict)
    affected_steps: List[str] = field(default_factory=list)
    previous_steps_replayed: List[str] = field(default_factory=list)
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    saved_at: Optional[datetime] = None


# ─── Orchestrator ───────────────────────────────────────────────────────


class RerecordOrchestrator:
    """Máquina de estados aislada de la UI.

    Es seguro pasar callbacks (``on_progress``, ``on_state_change``)
    para reaccionar a transiciones. La UI también puede hacer pull
    leyendo ``orch.session.state`` directamente.

    El orquestador es **single-shot**: una vez ``saved`` o
    ``cancelled``, hay que crear uno nuevo para regrabar otro paso.
    """

    def __init__(
        self,
        *,
        executor_factory: Optional[Callable[[], Any]] = None,
        on_progress: Optional[ProgressCb] = None,
        on_state_change: Optional[StateChangeCb] = None,
    ) -> None:
        self._executor_factory = executor_factory
        self._on_progress = on_progress
        self._on_state_change = on_state_change
        self._cancel_requested = threading.Event()
        self._lock = threading.RLock()
        self._mission: Optional[Mission] = None
        self._snapshot_blob: Optional[Dict[str, Any]] = None
        self._session: Optional[RerecordSession] = None

    # ── Properties ──────────────────────────────────────────────

    @property
    def session(self) -> Optional[RerecordSession]:
        return self._session

    @property
    def is_cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    # ── Helpers ─────────────────────────────────────────────────

    def _set_state(self, new_state: str) -> None:
        with self._lock:
            if self._session is None:
                return
            old = self._session.state
            if old == new_state:
                return
            self._session.state = new_state
        cb = self._on_state_change
        if cb is not None:
            try:
                cb(new_state)
            except Exception as e:  # pragma: no cover
                log.warning(f"[rerecord] state_change cb raised: {e}")

    def _emit_progress(self, idx: int, total: int, label: str) -> None:
        with self._lock:
            if self._session is None:
                return
            self._session.progress_step_index = idx
            self._session.progress_total_steps = max(total, 0)
            self._session.progress_label = label
            self._session.progress = (
                float(idx) / float(total) if total > 0 else 0.0
            )
        cb = self._on_progress
        if cb is not None:
            try:
                cb(idx, total, label)
            except Exception as e:  # pragma: no cover
                log.warning(f"[rerecord] progress cb raised: {e}")

    def _require_mission(self) -> Mission:
        if self._mission is None:
            raise RerecordSessionError("Sesión no iniciada (mission=None).")
        return self._mission

    def _require_session(self) -> RerecordSession:
        if self._session is None:
            raise RerecordSessionError("Sesión no iniciada.")
        return self._session

    # ── API pública ─────────────────────────────────────────────

    def start(
        self,
        mission: Mission,
        *,
        target_step_index: int,
    ) -> RerecordSession:
        """Crea la sesión y captura snapshot bit-perfecto de la misión.

        El target debe ser un índice válido dentro del
        ``semantic_execution_plan.steps``. Si no hay plan, levanta
        ``RerecordSessionError`` — sin plan no hay cómo regrabar de
        forma semántica (la regla §1: nunca usar legacy graph).
        """
        if mission is None:
            raise RerecordSessionError("mission no puede ser None.")
        plan_blob = mission.semantic_execution_plan or {}
        steps = list(plan_blob.get("steps") or [])
        if not steps:
            raise RerecordSessionError(
                "La misión no tiene semantic_execution_plan; "
                "regrabación bloqueada (PRD §A.1)."
            )
        if target_step_index < 0 or target_step_index >= len(steps):
            raise RerecordSessionError(
                f"target_step_index={target_step_index} "
                f"fuera de rango (0..{len(steps) - 1})."
            )

        target_step = steps[target_step_index]
        snapshot = mission.model_dump(mode="json")

        session = RerecordSession(
            rerecord_session_id=str(uuid.uuid4()),
            mission_id=str(mission.id),
            target_step_id=str(target_step.get("id") or ""),
            target_step_index=int(target_step_index),
            original_step_snapshot=dict(target_step),
        )
        with self._lock:
            self._mission = mission
            self._snapshot_blob = snapshot
            self._session = session
            self._cancel_requested.clear()
        self._set_state(STATE_PREPARING)
        log.info(
            f"[rerecord] start session={session.rerecord_session_id} "
            f"mission={session.mission_id} target_idx={target_step_index} "
            f"target_id={session.target_step_id!r}"
        )
        return session

    def request_cancel(self) -> None:
        """Marca cooperative-cancel. El replay y workers deben
        revisar ``is_cancel_requested`` periódicamente."""
        self._cancel_requested.set()

    def proceed_to_capture(self) -> bool:
        """Ejecuta los pasos 0..N-1 del plan semántico y deja la
        sesión en ``waiting_for_user``.

        * Devuelve ``True`` si llegó al punto de captura.
        * Devuelve ``False`` si falló (``state="failed"``,
          ``error_message`` no vacío).
        * Si ``target_step_index == 0``, no ejecuta nada y pasa
          directamente a ``waiting_for_user``.
        * Si ``request_cancel()`` se llama durante el replay,
          aborta cooperativamente (``state="cancelled"``).
        """
        mission = self._require_mission()
        session = self._require_session()
        if session.state not in (STATE_PREPARING, STATE_FAILED):
            raise RerecordSessionError(
                f"proceed_to_capture inválido en state={session.state!r}"
            )

        plan_blob = mission.semantic_execution_plan or {}
        steps = list(plan_blob.get("steps") or [])
        previous = steps[: session.target_step_index]

        if not previous:
            # Caso: regrabar el paso 1 (no hay anteriores).
            log.info(
                f"[rerecord] no hay pasos previos; "
                f"saltamos directo a waiting_for_user"
            )
            session.previous_steps_replayed = []
            session.error_message = ""
            session.error_failed_step_id = None
            self._set_state(STATE_WAITING_FOR_USER)
            self._emit_progress(0, 0, "Empieza la regrabación")
            return True

        self._set_state(STATE_REPLAYING)
        self._emit_progress(
            0,
            len(previous),
            f"Preparando ejecución de {len(previous)} paso(s) previo(s)…",
        )

        # Construimos las MissionStep canonicas vía
        # ``semantic_plan_to_mission_steps`` para que el SmartExecutor
        # use exactamente el mismo path que la ejecución oficial. Pero
        # solo de los previos.
        try:
            from app.services.missions.semantic_execution_plan import (
                SemanticExecutionPlan,
                semantic_plan_to_mission_steps,
            )
            full_plan = SemanticExecutionPlan.from_dict(plan_blob)
            full_plan.steps = full_plan.steps[: session.target_step_index]
            mission_steps = semantic_plan_to_mission_steps(full_plan)
        except Exception as e:
            session.error_message = (
                f"{REPLAY_FAILED_MESSAGE} "
                f"(no pude preparar los pasos previos: {e})"
            )
            session.error_failed_step_id = None
            self._set_state(STATE_FAILED)
            return False

        executed_ids: List[str] = []
        executor = self._build_executor()

        # Reportamos un progreso intermedio por step. Para soportar
        # cooperative cancel y heartbeat, pedimos al executor que nos
        # avise tras cada paso vía ``on_step_done``. El callback es
        # opcional — los executors falsos (tests) pueden ignorarlo.
        cancel_seen = {"v": False}

        def on_step_done(outcome: Any) -> None:
            try:
                step_id = getattr(outcome, "step_id", "") or ""
                kind = getattr(outcome, "kind", "") or ""
                status = getattr(outcome, "status", "") or ""
            except Exception:
                step_id, kind, status = "", "", ""
            if step_id:
                executed_ids.append(step_id)
            idx_done = len(executed_ids)
            label_human = self._humanize_step_label(
                kind=kind,
                fallback=f"Paso {idx_done}",
            )
            self._emit_progress(
                idx_done,
                len(previous),
                f"Ejecutando paso {idx_done}/{len(previous)}: "
                f"{label_human} [{status}]",
            )
            if self._cancel_requested.is_set():
                cancel_seen["v"] = True

        try:
            self._inject_callback(executor, "on_step_done", on_step_done)
        except Exception:  # pragma: no cover
            pass

        try:
            result = self._run_executor(executor, mission_steps)
        except Exception as e:
            session.error_message = (
                f"{REPLAY_FAILED_MESSAGE} "
                f"(error inesperado durante el replay: {e})"
            )
            session.error_failed_step_id = None
            self._set_state(STATE_FAILED)
            return False

        if self._cancel_requested.is_set() or cancel_seen["v"]:
            log.info("[rerecord] replay cancelado cooperativamente")
            session.previous_steps_replayed = list(executed_ids)
            self._restore_snapshot()
            self._set_state(STATE_CANCELLED)
            return False

        result_status = self._extract_result_status(result)
        result_failed_id = self._extract_failed_step_id(result)
        result_msg = self._extract_result_message(result)

        session.previous_steps_replayed = list(executed_ids)
        if result_status == "success":
            session.error_message = ""
            session.error_failed_step_id = None
            self._set_state(STATE_WAITING_FOR_USER)
            self._emit_progress(
                len(previous),
                len(previous),
                "Llegué al paso a regrabar. Realiza la acción.",
            )
            return True

        # Cualquier otra cosa = fallo visible.
        if result_msg:
            tail = f" Detalle: {result_msg}"
        else:
            tail = ""
        session.error_message = REPLAY_FAILED_MESSAGE + tail
        session.error_failed_step_id = result_failed_id
        self._set_state(STATE_FAILED)
        return False

    def skip_replay_and_capture_manually(self) -> None:
        """Salta el replay previo y va directo a capturar el paso N.

        Usado cuando el replay falló y el usuario eligió "Regrabar
        desde aquí manualmente" (PRD §6 — manejo de fallos previos).
        """
        session = self._require_session()
        if session.state not in (STATE_FAILED, STATE_PREPARING):
            raise RerecordSessionError(
                f"skip_replay inválido en state={session.state!r}"
            )
        session.error_message = ""
        session.error_failed_step_id = None
        self._set_state(STATE_WAITING_FOR_USER)
        self._emit_progress(0, 0, "Captura manual: realiza el paso.")

    def capture_user_event(
        self,
        evidence: List[Dict[str, Any]],
        *,
        validator: Optional[Callable[[List[Dict[str, Any]]], bool]] = None,
    ) -> bool:
        """Recibe la evidencia capturada por el overlay y la valida.

        ``evidence`` es la lista de eventos crudos (RawEvent en dict)
        producida por ``StepFragmentRecorder``. Si está vacía o el
        validador la rechaza, la sesión se queda en
        ``waiting_for_user`` (sin consumir el intento) y devuelve
        ``False`` — la UI debe pedir al usuario que repita.
        """
        session = self._require_session()
        if session.state not in (STATE_WAITING_FOR_USER, STATE_CAPTURED):
            raise RerecordSessionError(
                f"capture_user_event inválido en state={session.state!r}"
            )
        if not evidence:
            session.captured_evidence = []
            session.replacement_step_snapshot = {}
            self._set_state(STATE_WAITING_FOR_USER)
            return False
        if validator is not None:
            try:
                ok = bool(validator(evidence))
            except Exception:
                ok = False
            if not ok:
                session.captured_evidence = []
                session.replacement_step_snapshot = {}
                self._set_state(STATE_WAITING_FOR_USER)
                return False
        session.captured_evidence = list(evidence)
        # El replacement_step_snapshot se construye en ``save()``.
        self._set_state(STATE_CAPTURED)
        return True

    def discard_capture(self) -> None:
        """Botón "Repetir": descarta captura actual sin tocar la
        misión y vuelve a ``waiting_for_user``."""
        session = self._require_session()
        if session.state in (STATE_SAVED, STATE_CANCELLED):
            raise RerecordSessionError(
                f"discard_capture inválido en state={session.state!r}"
            )
        session.captured_evidence = []
        session.replacement_step_snapshot = {}
        self._set_state(STATE_WAITING_FOR_USER)

    def save(self) -> Any:
        """Aplica el reemplazo del paso, recompila plan y persiste
        el audit trail. Devuelve el ``SemanticExecutionPlan``
        resultante.

        Reglas:
          * Solo válido en ``state="captured"``.
          * Idempotente sobre la misma sesión: una segunda llamada
            lanza ``RerecordSessionError`` (no muta nada).
          * Si la aplicación falla (e.g. compile_fragment vacío),
            la sesión queda en ``failed`` y ``error_message`` queda
            no vacío.
        """
        mission = self._require_mission()
        session = self._require_session()
        if session.state != STATE_CAPTURED:
            raise RerecordSessionError(
                f"save inválido en state={session.state!r}"
            )

        from app.services.missions.mission_review_confirm import (
            rerecord_step,
        )

        try:
            plan, replacement_snapshot, affected = rerecord_step(
                mission,
                step_index=session.target_step_index,
                new_step_evidence=list(session.captured_evidence),
                rerecorded_at=datetime.now(timezone.utc),
                user_action="rerecord",
                rerecord_session_id=session.rerecord_session_id,
                started_at=session.started_at,
                previous_steps_replayed=list(session.previous_steps_replayed),
                original_step_snapshot=dict(session.original_step_snapshot),
            )
        except Exception as e:
            session.error_message = f"No pude guardar la regrabación: {e}"
            self._set_state(STATE_FAILED)
            log.error(f"[rerecord] save fallido: {e}")
            raise

        session.replacement_step_snapshot = dict(replacement_snapshot or {})
        session.affected_steps = list(affected or [])
        session.saved_at = datetime.now(timezone.utc)
        self._set_state(STATE_SAVED)
        log.info(
            f"[rerecord] saved session={session.rerecord_session_id} "
            f"target_idx={session.target_step_index} "
            f"affected={len(session.affected_steps)}"
        )
        return plan

    def cancel(self) -> Mission:
        """Restaura el snapshot inicial y deja la sesión en
        ``cancelled``. Devuelve la misión restaurada.

        Es seguro llamar ``cancel()`` desde cualquier estado salvo
        ``saved`` (la misión ya cambió y no debemos rebobinar a
        ciegas — la UI debe haber refrescado a ese punto).
        """
        session = self._require_session()
        if session.state == STATE_SAVED:
            raise RerecordSessionError(
                "cancel inválido tras save; la misión ya cambió."
            )
        mission = self._restore_snapshot()
        self._set_state(STATE_CANCELLED)
        log.info(
            f"[rerecord] cancelled session={session.rerecord_session_id}"
        )
        return mission

    def release(self) -> None:
        """Limpia recursos. La UI lo debe llamar en ``closeEvent``.

        Idempotente: se puede llamar múltiples veces."""
        with self._lock:
            self._cancel_requested.set()
            # No reiniciamos el snapshot por si el caller quiere leerlo
            # (audit forense post-mortem). Solo soltamos referencias.
            self._mission = None

    # ── Helpers internos ────────────────────────────────────────

    def _restore_snapshot(self) -> Mission:
        if self._mission is None or self._snapshot_blob is None:
            raise RerecordSessionError(
                "No hay snapshot para restaurar (¿start no se llamó?)."
            )
        snapshot = self._snapshot_blob
        # Reaplicamos los campos clave directamente sobre la
        # instancia actual para preservar identidad (la UI mantiene
        # la referencia y no se beneficia de un objeto nuevo).
        try:
            restored = Mission.model_validate(snapshot)
        except Exception as e:
            log.warning(
                f"[rerecord] no pude validar snapshot, "
                f"aplico merge directo: {e}"
            )
            restored = None

        if restored is not None:
            for fld in restored.model_fields_set:
                try:
                    setattr(self._mission, fld, getattr(restored, fld))
                except Exception:
                    pass
            # Forzamos campos críticos siempre, aunque no estén en
            # ``model_fields_set`` (ej. listas que se modificaron in-place).
            for fld in (
                "raw_trace",
                "interpreted_steps",
                "compiled_execution_graph",
                "semantic_execution_plan",
                "review_confirmations",
                "rerecord_history",
                "tags",
            ):
                try:
                    setattr(self._mission, fld, getattr(restored, fld))
                except Exception:
                    pass
        return self._mission

    def _build_executor(self) -> Any:
        if self._executor_factory is not None:
            return self._executor_factory()
        from app.services.missions.smart_executor import SmartMissionExecutor
        return SmartMissionExecutor()

    @staticmethod
    def _inject_callback(executor: Any, attr: str, cb: Callable) -> None:
        if hasattr(executor, attr):
            setattr(executor, attr, cb)

    @staticmethod
    def _run_executor(executor: Any, mission_steps: List[Any]) -> Any:
        # SmartMissionExecutor expone ``run_mission(steps)``. Tests
        # pueden inyectar un fake con la misma firma. Si nada de eso
        # existe, intentamos ``__call__``.
        if hasattr(executor, "run_mission"):
            return executor.run_mission(mission_steps)
        if callable(executor):
            return executor(mission_steps)
        raise RerecordSessionError(
            f"executor no soportado: {type(executor).__name__}"
        )

    @staticmethod
    def _extract_result_status(result: Any) -> str:
        if result is None:
            return "failed"
        status = getattr(result, "status", None)
        if status is None and isinstance(result, dict):
            status = result.get("status")
        return str(status or "failed")

    @staticmethod
    def _extract_failed_step_id(result: Any) -> Optional[str]:
        if result is None:
            return None
        sid = getattr(result, "failed_step_id", None)
        if sid is None and isinstance(result, dict):
            sid = result.get("failed_step_id")
        if sid is None:
            return None
        return str(sid)

    @staticmethod
    def _extract_result_message(result: Any) -> str:
        if result is None:
            return ""
        msg = getattr(result, "last_message", None)
        if msg is None and isinstance(result, dict):
            msg = result.get("last_message") or result.get("message")
        return str(msg or "")

    @staticmethod
    def _humanize_step_label(*, kind: str, fallback: str) -> str:
        kind = (kind or "").strip().lower()
        mapping = {
            "open_app": "Abriendo aplicación",
            "select_profile": "Seleccionando perfil",
            "open_new_tab": "Abriendo nueva pestaña",
            "open_url": "Navegando a URL",
            "search_youtube": "Buscando en YouTube",
            "scroll_results": "Desplazando resultados",
            "click": "Click",
            "type_text": "Escribiendo texto",
            "send_hotkey": "Atajo de teclado",
            "wait_for": "Esperando estado",
            "confirm": "Confirmando",
        }
        return mapping.get(kind, fallback)


__all__ = [
    "RerecordOrchestrator",
    "RerecordSession",
    "RerecordSessionError",
    "REPLAY_FAILED_MESSAGE",
    "STATE_IDLE",
    "STATE_PREPARING",
    "STATE_REPLAYING",
    "STATE_WAITING_FOR_USER",
    "STATE_CAPTURED",
    "STATE_SAVED",
    "STATE_CANCELLED",
    "STATE_FAILED",
]
