"""
ArthurOS Services - SmartMissionExecutor (Smart Executor)
---------------------------------------------------------
Máquina de estados autocurable para ejecutar misiones por *intención*.

Ya no reproducimos eventos lineales: cada paso pasa por un ciclo
detect → precondition → action → postcondition → checkpoint, y si algo
no cuadra, llama al ``RecoveryEngine``. Si la recuperación tampoco
arregla, el executor **se detiene** y pide intervención humana.

Reglas (PRD literales):
  - Si falla la postcondition, está prohibido ejecutar el siguiente step.
  - El executor es más rápido que un humano usando atajos cuando puede,
    pero más seguro que una macro porque verifica constante.
  - Aprende: cada éxito/fallo se registra en el LearningLogger y mueve
    la prioridad de las estrategias.

Uso típico:

    steps = [
        MissionStep(id="s1", kind="open_app", params={"name": "chrome"}),
        MissionStep(id="s2", kind="select_profile",
                    params={"profile_name": "Daniel Arturo Ramos"}),
        MissionStep(id="s3", kind="open_new_tab"),
        MissionStep(id="s4", kind="open_url", params={"alias": "youtube"}),
        MissionStep(id="s5", kind="search_youtube",
                    params={"query": "Devuélveme el amor de Luis Miguel"}),
    ]
    executor = SmartMissionExecutor()
    result = executor.run_mission(steps)

UX:
  - El executor publica mensajes humanos vía ``on_status`` y
    ``on_human_help_needed``: la UI los renderiza al usuario.
  - Logs técnicos van a ``log`` (loguru) — se ocultan al usuario salvo
    modo experto.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.logger import log
from app.services.missions.action_runner import ActionRunner, StrategyResult
from app.services.missions.checkpoints import CheckpointManager
from app.services.missions.execution_contracts import (
    MissionStep,
    StepContract,
    build_contract,
)
from app.services.missions.execution_learning import (
    LearningLogger,
    get_learning_logger,
)
from app.services.missions.recovery_engine import (
    MAX_PER_STRATEGY,
    RecoveryEngine,
    RecoveryPlan,
)
from app.services.missions.state_detector import (
    StateDetector,
    StateSnapshot,
)


# ──────────────────────────────────────────────────────────────────────
# Resultados
# ──────────────────────────────────────────────────────────────────────

@dataclass
class StepStarted:
    """Snapshot inmutable que se emite ANTES de ejecutar un step.

    PRD 2026-05-09 §exec-progress: la UI debe poder mostrar
    "Ejecutando paso 1 de 6" al instante en que el worker arranca el
    primer paso, no después de que termine. Antes este executor sólo
    publicaba ``on_step_done`` (post-mortem), por lo que la ventana
    flotante quedaba en "paso 0 de 6" indefinidamente.

    ``index`` es 1-based: el paso que va a ejecutarse a continuación.
    ``total`` es la cantidad total de steps que recibió ``run_mission``.
    """

    step_id: str
    kind: str
    human_label: str
    index: int
    total: int


@dataclass
class StepOutcome:
    step_id: str
    kind: str
    status: str                      # "success" | "skipped" | "failed" | "needs_human"
    strategy_used: Optional[str] = None
    retries: int = 0
    duration_ms: int = 0
    human_message: str = ""
    error: str = ""
    state_before: Optional[Dict[str, Any]] = None
    state_after: Optional[Dict[str, Any]] = None


@dataclass
class MissionResult:
    run_id: str
    status: str                      # "success" | "stopped_for_human" | "failed"
    outcomes: List[StepOutcome] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    last_message: str = ""
    failed_step_id: Optional[str] = None

    @property
    def duration_ms(self) -> int:
        end = self.finished_at or time.time()
        return int((end - self.started_at) * 1000)


# ──────────────────────────────────────────────────────────────────────
# Callbacks de UX (todos opcionales)
# ──────────────────────────────────────────────────────────────────────

StatusCb = Callable[[str], None]                  # mensaje humano
HumanHelpCb = Callable[[StepOutcome], None]       # se acabó la autonomía
ProgressCb = Callable[[StepOutcome], None]        # paso terminó (ok/skip)
# PRD 2026-05-09 §exec-progress: paso a punto de iniciar (pre-acción).
StepStartedCb = Callable[["StepStarted"], None]


# ──────────────────────────────────────────────────────────────────────
# Estrategias prohibidas como PRIMARIA (PRD 2026-05-06c).
# ──────────────────────────────────────────────────────────────────────
#
# El SmartExecutor jamás puede arrancar un step con una de estas
# estrategias en primer lugar (son visuales/coords legacy). Pueden
# vivir al final como red de seguridad. ``_filter_strategies`` las
# mueve al final del ranking si aparecen como primera opción.

_FORBIDDEN_PRIMARY_HINTS = (
    "vision_coords",
    "relative_coords",
    "visual_asset",
    "click_button",
    "group_control",
    "guide_service",
    "video_stream",
    "use_coords",
)


def _is_forbidden_primary(strategy: str) -> bool:
    s = (strategy or "").lower()
    return any(h in s for h in _FORBIDDEN_PRIMARY_HINTS)


def is_emergency_coordinates_strategy(strategy: Optional[str]) -> bool:
    """True si la estrategia indica coords/visión/video de emergencia (click "ciego")."""

    return _is_forbidden_primary(strategy or "")


def _resolve_strategies(
    step: MissionStep,
    contract: StepContract,
    mission: Optional[Any] = None,
) -> List[str]:
    """Decide la lista final de estrategias a probar para ``step``.

    Reglas (PRD 2026-05-06c):
    1. Si ``step.params`` trae ``preferred_strategy``/``fallback_strategies``
       (lo inyecta el ``semantic_execution_plan``), esa lista MANDA.
       El executor confía en el plan canónico.
    2. Si no, usa ``contract.all_strategies()`` (default histórico).
    3. En cualquiera de los dos casos: las estrategias prohibidas como
       primaria nunca pueden ir primero. Si aparecen, se mueven al
       final como red de seguridad. Esto evita que el executor empiece
       por ``*:visual_asset`` o ``*:relative_coords`` "porque el
       LearningLogger las rankeó".

    UOL (Universal Operational Language): cuando el paso trae contrato UOL,
    se priorizan sus estrategias antes de ``smart_route`` legacy.
    """
    plan_pref = str(step.params.get("preferred_strategy") or "").strip()
    plan_fb = step.params.get("fallback_strategies") or []
    if plan_pref:
        out: List[str] = [plan_pref]
        for s in plan_fb:
            ss = str(s or "").strip()
            if ss and ss not in out:
                out.append(ss)
        # Cualquier estrategia del contract que no esté ya y NO sea
        # prohibida la añadimos al final como último recurso.
        for s in contract.all_strategies():
            if s not in out and not _is_forbidden_primary(s):
                out.append(s)
        # Las prohibidas del contract van al final del todo.
        for s in contract.all_strategies():
            if s not in out and _is_forbidden_primary(s):
                out.append(s)
        base = out
    else:
        raw = list(contract.all_strategies())
        safe = [s for s in raw if not _is_forbidden_primary(s)]
        risky = [s for s in raw if _is_forbidden_primary(s)]
        base = safe + risky

    try:
        from app.services.missions.universal_operational_language import (
            resolve_uol_runtime_strategies,
        )

        base_out = resolve_uol_runtime_strategies(step, base)
    except Exception:
        base_out = base

    try:
        from app.services.runtime.adaptive_runtime_strategy_synthesis import (
            inject_arss_strategies,
        )

        return inject_arss_strategies(step, base_out, mission)
    except Exception:
        return base_out


# ──────────────────────────────────────────────────────────────────────
# Excepciones internas
# ──────────────────────────────────────────────────────────────────────

class _NeedsHuman(Exception):
    """Señal interna: el flujo del step requiere intervención humana."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


# ──────────────────────────────────────────────────────────────────────
# Executor
# ──────────────────────────────────────────────────────────────────────

class SmartMissionExecutor:
    """Ejecutor basado en estado, autocurable, con aprendizaje.

    No depende del MissionPlayer legacy. Puede operar sobre una lista de
    ``MissionStep`` directamente — útil para misiones nuevas escritas
    en el formato semántico.
    """

    def __init__(
        self,
        *,
        run_id: Optional[str] = None,
        state_detector: Optional[StateDetector] = None,
        action_runner: Optional[ActionRunner] = None,
        checkpoints: Optional[CheckpointManager] = None,
        learning: Optional[LearningLogger] = None,
        on_status: Optional[StatusCb] = None,
        on_human_help_needed: Optional[HumanHelpCb] = None,
        on_step_done: Optional[ProgressCb] = None,
        on_step_started: Optional[StepStartedCb] = None,
        expert_mode: bool = False,
        mission_ref: Optional[Any] = None,
    ) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        self.mission_ref = mission_ref
        self.detector = state_detector or StateDetector()
        self.runner = action_runner or ActionRunner(mission_ref=mission_ref)
        self.checkpoints = checkpoints or CheckpointManager(run_id=self.run_id)
        self.learning = learning or get_learning_logger()
        self.recovery = RecoveryEngine(
            action_runner=self.runner,
            checkpoints=self.checkpoints,
            learning=self.learning,
        )
        self.on_status = on_status
        self.on_human_help_needed = on_human_help_needed
        self.on_step_done = on_step_done
        self.on_step_started = on_step_started
        self.expert_mode = expert_mode
        self._last_sll_explanations: Dict[str, Any] = {}
        self._omp_prior_kinds: List[str] = []
        self._last_osue_wait: Optional[Any] = None

    # ── API pública ──────────────────────────────────────────────
    def run_mission(
        self,
        steps: List[MissionStep],
        *,
        start_step_index: int = 0,
    ) -> MissionResult:
        result = MissionResult(run_id=self.run_id, status="success")
        log.info(f"SmartMissionExecutor: run_id={self.run_id} steps={len(steps)}")

        self._omp_prior_kinds = []

        total = len(steps)
        i = max(0, min(int(start_step_index or 0), total - 1)) if total > 0 else 0
        # Permitimos que un rollback retroceda el índice; por eso el
        # while con índice mutable.
        while i < total:
            step = steps[i]
            # PRD 2026-05-09 §exec-progress: anunciamos el paso ANTES
            # de empezar a ejecutarlo. Esto permite que la UI mueva
            # "paso 0 de N" → "paso 1 de N" en el instante en que el
            # worker arranca, sin esperar al primer ``on_step_done``.
            if self.on_step_started:
                try:
                    self.on_step_started(StepStarted(
                        step_id=step.id,
                        kind=step.kind,
                        human_label=step.human_label or step.describe(),
                        index=i + 1,
                        total=total,
                    ))
                except Exception:
                    pass
            outcome = self._run_step(step, step_index=i)
            result.outcomes.append(outcome)
            if self.on_step_done:
                try:
                    self.on_step_done(outcome)
                except Exception:
                    pass

            if outcome.status in ("success", "skipped"):
                self._omp_prior_kinds.append(step.kind)
                i += 1
                continue

            if outcome.status == "needs_human":
                result.status = "stopped_for_human"
                result.failed_step_id = step.id
                result.last_message = outcome.human_message or (
                    "Necesito tu ayuda para continuar."
                )
                self._announce(result.last_message)
                if self.on_human_help_needed:
                    try:
                        self.on_human_help_needed(outcome)
                    except Exception:
                        pass
                break

            # status == failed → STOP — PRD: prohibido avanzar.
            result.status = "failed"
            result.failed_step_id = step.id
            result.last_message = outcome.human_message or (
                "El paso falló y no es seguro continuar."
            )
            break

        result.finished_at = time.time()
        log.info(
            f"SmartMissionExecutor done status={result.status} "
            f"steps_ok={sum(1 for o in result.outcomes if o.status in ('success', 'skipped'))}"
            f" duration_ms={result.duration_ms}"
        )
        try:
            from app.core.config import get_settings as _gs_ompc

            if getattr(_gs_ompc(), "OMPC_ENABLED", False):
                from app.services.runtime.operational_memory import (
                    observe_smart_mission_execution,
                )

                observe_smart_mission_execution(self.mission_ref, steps, result)
        except Exception as e:
            log.debug("OMPC observe smart mission: %s", e)
        try:
            from app.services.missions.uol_runtime_telemetry import (
                persist_acceptance_report_on_mission,
            )

            persist_acceptance_report_on_mission(self.mission_ref)
        except Exception as e:
            log.debug("[UOL telemetry] acceptance report: %s", e)
        try:
            from app.services.runtime.operational_runtime_consistency_engine import (
                observe_mission_after_run,
            )

            observe_mission_after_run(self.mission_ref)
        except Exception as e:
            log.debug("[ORCE] post-run reconcile: %s", e)
        try:
            from app.services.runtime.adaptive_runtime_strategy_synthesis import (
                process_arss_after_orce,
            )

            process_arss_after_orce(self.mission_ref)
        except Exception as e:
            log.debug("[ARSS] post-ORCE synthesis: %s", e)
        try:
            from app.services.runtime.beta_runtime_acceptance import (
                build_and_persist_beta_runtime_acceptance,
            )

            build_and_persist_beta_runtime_acceptance(
                self.mission_ref,
                run_id=self.run_id,
                mission_result=result,
            )
        except Exception as e:
            log.debug("[BetaAcceptance] post-run: %s", e)
        return result

    # ── Loop por step ────────────────────────────────────────────
    def _run_step(self, step: MissionStep, *, step_index: int = 0) -> StepOutcome:
        try:
            from app.services.missions.universal_operational_language import (
                enrich_mission_step_with_uol,
            )

            step = enrich_mission_step_with_uol(step)
        except Exception:
            pass
        contract = build_contract(step)

        try:
            from app.services.missions.uol_runtime_telemetry import start_step_telemetry

            _uol_tel = start_step_telemetry(
                getattr(self, "mission_ref", None),
                step,
                run_id=self.run_id,
            )
        except Exception:
            _uol_tel = None

        # PRD 2026-05-09 §exec-runtime: si el ``kind`` no tiene builder
        # registrado, ``build_contract`` devuelve un sentinela con
        # ``is_unknown_kind=True``. NO entramos al loop de recovery
        # (eso colgaría la ejecución durante minutos buscando
        # estrategias inexistentes); fallamos visible al instante con
        # ``UNSUPPORTED_SEMANTIC_STEP_TYPE`` para que el progress
        # dialog cierre y el historial diga "Falló".
        if getattr(contract, "is_unknown_kind", False):
            return self._unsupported_kind_outcome(step)

        attempts: Dict[str, int] = {}

        # 0. Mensaje "estoy mirando"
        if contract.human_announce:
            self._announce(contract.human_announce)

        t0 = time.time()
        retries = 0
        step_deadline = self._step_deadline_mono(t0, step)

        def _step_time_exceeded() -> bool:
            return time.monotonic() >= step_deadline

        def _step_timeout_outcome(*, strategy: str = "step:timeout") -> StepOutcome:
            from app.services.runtime.entity_runtime_resolution import ENTITY_RUNTIME_TIMEOUT

            err = ENTITY_RUNTIME_TIMEOUT
            if step.kind not in ("select_entity_from_collection", "select_profile"):
                err = "STEP_RUNTIME_TIMEOUT"
            return self._needs_human_outcome(
                step,
                contract,
                "Tiempo agotado en este paso; revisa la pantalla e inténtalo de nuevo.",
                before,
                t0,
                retries=retries,
                strategy=strategy,
                error=err,
            )

        # 1. Detectar
        state = self._detect(deep=False)
        # Para steps sensibles a UIA (perfil), pedimos deep — vale los
        # ~150 ms extra.
        if step.kind in ("select_profile", "select_entity_from_collection"):
            state = self._detect(deep=True)
        before = state.to_dict()

        # 2b. UOOL — autoridad decisoria única antes de capas autónomas.
        self._uool_decision = None
        try:
            from app.core.config import get_settings as _gs_uool
            from app.contracts.mission import OperationalActionKind
            from app.services.runtime.unified_operational_orchestrator import (
                orchestrate_step_preflight,
            )

            _uool_cfg = _gs_uool()
            if (
                getattr(_uool_cfg, "UOOL_ENABLED", False)
                and getattr(_uool_cfg, "UOOL_COORDINATE_SMART_EXECUTOR", True)
                and getattr(self, "mission_ref", None) is not None
            ):
                uool_dec = orchestrate_step_preflight(
                    self.mission_ref,
                    _uool_cfg,
                    step_index=step_index,
                    step=step,
                    state_snapshot=state,
                    on_status=self.on_status,
                    expert_mode=self.expert_mode,
                )
                if uool_dec is not None:
                    self._uool_decision = uool_dec
                    if uool_dec.action == OperationalActionKind.STOP:
                        return self._needs_human_outcome(
                            step,
                            contract,
                            uool_dec.human_message or "No es seguro continuar.",
                            before,
                            t0,
                            retries=retries,
                            strategy="uool:unsafe_stop",
                        )
                    if uool_dec.action == OperationalActionKind.ASK_HUMAN:
                        return self._needs_human_outcome(
                            step,
                            contract,
                            uool_dec.human_message or "Necesita aclaración.",
                            before,
                            t0,
                            retries=retries,
                            strategy="uool:ask_human",
                        )
                    if uool_dec.action == OperationalActionKind.WAIT:
                        wait_ms = int(getattr(_uool_cfg, "UOOL_WAIT_MS", 800) or 800)
                        if getattr(_uool_cfg, "OCRP_ENABLED", False):
                            from app.services.runtime.operational_convergence_program import (
                                ocrp_adaptive_wait_ms,
                            )

                            wait_ms = ocrp_adaptive_wait_ms(
                                getattr(self, "mission_ref", None),
                                _uool_cfg,
                            )
                        if wait_ms > 0:
                            time.sleep(wait_ms / 1000.0)
        except Exception as e:
            log.debug("[UOOL] step preflight: %s", e)

        # 2. Atajo: si la postcondición ya se cumple, SKIP.
        # PRD 2026-05-10j §Self-healing: el strategy_used debe quedar
        # como ``"skip:already_satisfied"`` para que el reviewer/log
        # vea "Paso omitido porque el outcome ya está logrado" en lugar
        # de un None opaco. Esto NO afecta la decisión — el status
        # sigue siendo "skipped".
        if contract.postcondition(state):
            msg = contract.human_skip or contract.human_done or "Ya está listo."
            self._announce(msg)
            outcome = StepOutcome(
                step_id=step.id,
                kind=step.kind,
                status="skipped",
                strategy_used="skip:already_satisfied",
                human_message=msg,
                duration_ms=int((time.time() - t0) * 1000),
                state_before=before,
                state_after=before,
            )
            self.learning.record_success(
                run_id=self.run_id,
                kind=step.kind,
                step_id=step.id,
                strategy="skip:already_satisfied",
                retries=0,
                duration_ms=outcome.duration_ms,
                state_before=before,
                state_after=before,
            )
            self._maybe_checkpoint(step, contract, state)
            return outcome

        # 3. Validar precondition (con recuperación si falla)
        try:
            state = self._ensure_precondition(step, contract, state, attempts)
        except _NeedsHuman as nh:
            return self._needs_human_outcome(
                step, contract, nh.message, before, t0, retries=retries
            )

        # 3a-pre. Identity context: skip if the requested session/profile is already active.
        try:
            from app.services.runtime.select_identity_context_runtime import (
                is_identity_selection_step,
                skip_if_active_identity_context,
            )

            if is_identity_selection_step(step):
                identity_skip = skip_if_active_identity_context(
                    step, state, getattr(self, "mission_ref", None),
                )
                if identity_skip is not None:
                    msg = (
                        identity_skip.message
                        or contract.human_skip
                        or "Contexto de identidad ya activo."
                    )
                    self._announce(msg)
                    outcome = StepOutcome(
                        step_id=step.id,
                        kind=step.kind,
                        status="skipped",
                        strategy_used=identity_skip.strategy,
                        human_message=msg,
                        duration_ms=int((time.time() - t0) * 1000),
                        state_before=before,
                        state_after=before,
                        error=identity_skip.failure_reason or "",
                    )
                    self.learning.record_success(
                        run_id=self.run_id,
                        kind=step.kind,
                        step_id=step.id,
                        strategy=identity_skip.strategy,
                        retries=0,
                        duration_ms=outcome.duration_ms,
                        state_before=before,
                        state_after=before,
                    )
                    self._maybe_checkpoint(step, contract, state)
                    return self._uol_telemetry_finish_step(_uol_tel, outcome)
        except Exception as e:
            log.debug("[select_identity_context] pre-uol skip: %s", e)

        # 3a. UOL-native handlers (COI → UCES → ERL → UIA, sin coords primarios).
        uol_hook = None
        try:
            from app.services.missions.uol_action_handlers import (
                try_uol_first_runtime,
            )

            uol_hook = try_uol_first_runtime(
                step, state, getattr(self, "mission_ref", None),
            )
        except Exception as e:
            log.debug("[UOL] first-runtime hook: %s", e)

        if _step_time_exceeded():
            return self._uol_telemetry_finish_step(
                _uol_tel,
                _step_timeout_outcome(strategy="step:timeout_pre_uol"),
            )

        if _uol_tel and _uol_tel.active and uol_hook:
            try:
                _uol_tel.record_uol_hook_attempt(
                    strategy=uol_hook.strategy_used or str(
                        step.params.get("preferred_strategy") or "",
                    ),
                    success=bool(
                        uol_hook.executed
                        and uol_hook.strategy_result
                        and uol_hook.strategy_result.ok,
                    ),
                    message=(
                        uol_hook.strategy_result.message
                        if uol_hook.strategy_result
                        else ""
                    ),
                    resolution_path=str(
                        (uol_hook.strategy_result.extra or {}).get("resolution_path")
                        if uol_hook.strategy_result and uol_hook.strategy_result.extra
                        else "uol_hook",
                    ),
                    runtime_resolution_substeps=(
                        list((uol_hook.strategy_result.extra or {}).get("runtime_resolution_substeps") or [])
                        if uol_hook.strategy_result and uol_hook.strategy_result.extra
                        else None
                    ),
                    execution_latency_ms=int(
                        (uol_hook.strategy_result.extra or {}).get("execution_latency_ms") or 0
                    )
                    if uol_hook.strategy_result and uol_hook.strategy_result.extra
                    else 0,
                    blocked_ambiguity=bool(uol_hook.blocked_ambiguity),
                )
            except Exception:
                pass

        if uol_hook and uol_hook.blocked_ambiguity:
            err_code = ""
            if uol_hook.strategy_result and uol_hook.strategy_result.extra:
                err_code = str(
                    uol_hook.strategy_result.extra.get("entity_failure_reason") or "",
                ).strip()
            _out = self._needs_human_outcome(
                step,
                contract,
                uol_hook.human_message or "Confirmación humana requerida.",
                before,
                t0,
                retries=retries,
                strategy="uol:ambiguity",
                error=err_code,
            )
            return self._uol_telemetry_finish_step(_uol_tel, _out)

        try:
            from app.services.runtime.entity_runtime_resolution import (
                OPEN_TAB_NOT_CONFIRMED,
                SEARCH_RESULTS_NOT_CONFIRMED,
                is_context_creation_step,
                is_surface_operation_step,
                sanitize_non_entity_step_failure_reason,
                surface_operation_failure_reason,
            )
        except Exception:
            is_context_creation_step = lambda _s: False  # type: ignore[assignment,misc]
            is_surface_operation_step = lambda _s: False  # type: ignore[assignment,misc]
            sanitize_non_entity_step_failure_reason = lambda _s, r: r  # type: ignore[assignment,misc]
            surface_operation_failure_reason = lambda _s: SEARCH_RESULTS_NOT_CONFIRMED  # type: ignore[assignment,misc]
            OPEN_TAB_NOT_CONFIRMED = "OPEN_TAB_NOT_CONFIRMED"
            SEARCH_RESULTS_NOT_CONFIRMED = "SEARCH_RESULTS_NOT_CONFIRMED"

        if (
            uol_hook
            and uol_hook.attempted
            and not uol_hook.executed
            and is_context_creation_step(step)
        ):
            fail_reason = OPEN_TAB_NOT_CONFIRMED
            msg = "No pude abrir la nueva pestaña."
            if uol_hook.strategy_result:
                msg = uol_hook.strategy_result.message or msg
                extra = uol_hook.strategy_result.extra or {}
                if isinstance(extra, dict):
                    fail_reason = str(
                        extra.get("open_tab_failure_reason") or fail_reason,
                    )
            return self._uol_telemetry_finish_step(
                _uol_tel,
                self._needs_human_outcome(
                    step,
                    contract,
                    msg,
                    before,
                    t0,
                    retries=retries,
                    strategy=uol_hook.strategy_used or "open_tab:uol_hotkey",
                    error=fail_reason,
                ),
            )

        if (
            uol_hook
            and uol_hook.attempted
            and not uol_hook.executed
            and step.kind in ("fill_form", "fill_field")
        ):
            fail_reason = "FILL_FIELD_NOT_FOUND"
            msg = "No pude rellenar el campo."
            strat = uol_hook.strategy_used or "fill_form:uol_fields"
            if uol_hook.strategy_result:
                msg = uol_hook.strategy_result.message or msg
                extra = uol_hook.strategy_result.extra or {}
                if isinstance(extra, dict):
                    fail_reason = str(
                        extra.get("fill_failure_reason") or fail_reason,
                    )
            fail_reason = sanitize_non_entity_step_failure_reason(step, fail_reason)
            return self._uol_telemetry_finish_step(
                _uol_tel,
                self._needs_human_outcome(
                    step,
                    contract,
                    msg,
                    before,
                    t0,
                    retries=retries,
                    strategy=strat,
                    error=fail_reason,
                ),
            )

        if (
            uol_hook
            and uol_hook.attempted
            and not uol_hook.executed
            and step.kind in ("submit_form", "submit_input")
        ):
            fail_reason = "SUBMIT_OUTCOME_NOT_CONFIRMED"
            msg = "No pude enviar el formulario."
            strat = uol_hook.strategy_used or "submit_form:uol_submit"
            if uol_hook.strategy_result:
                msg = uol_hook.strategy_result.message or msg
                extra = uol_hook.strategy_result.extra or {}
                if isinstance(extra, dict):
                    fail_reason = str(
                        extra.get("submit_failure_reason") or fail_reason,
                    )
            fail_reason = sanitize_non_entity_step_failure_reason(step, fail_reason)
            return self._uol_telemetry_finish_step(
                _uol_tel,
                self._needs_human_outcome(
                    step,
                    contract,
                    msg,
                    before,
                    t0,
                    retries=retries,
                    strategy=strat,
                    error=fail_reason,
                ),
            )

        if (
            uol_hook
            and uol_hook.attempted
            and not uol_hook.executed
            and step.kind == "switch_context"
        ):
            fail_reason = "CONTEXT_SWITCH_NOT_CONFIRMED"
            msg = "No pude cambiar el contexto."
            strat = uol_hook.strategy_used or "switch_context:uol_context"
            if uol_hook.strategy_result:
                msg = uol_hook.strategy_result.message or msg
                extra = uol_hook.strategy_result.extra or {}
                if isinstance(extra, dict):
                    fail_reason = str(
                        extra.get("context_failure_reason") or fail_reason,
                    )
            fail_reason = sanitize_non_entity_step_failure_reason(step, fail_reason)
            return self._uol_telemetry_finish_step(
                _uol_tel,
                self._needs_human_outcome(
                    step,
                    contract,
                    msg,
                    before,
                    t0,
                    retries=retries,
                    strategy=strat,
                    error=fail_reason,
                ),
            )

        if (
            uol_hook
            and uol_hook.attempted
            and not uol_hook.executed
            and step.kind == "confirm_dialog"
        ):
            fail_reason = "DIALOG_DISMISS_NOT_CONFIRMED"
            msg = "No pude confirmar el diálogo."
            strat = uol_hook.strategy_used or "confirm_dialog:uol_dialog"
            if uol_hook.strategy_result:
                msg = uol_hook.strategy_result.message or msg
                extra = uol_hook.strategy_result.extra or {}
                if isinstance(extra, dict):
                    fail_reason = str(
                        extra.get("dialog_failure_reason") or fail_reason,
                    )
            fail_reason = sanitize_non_entity_step_failure_reason(step, fail_reason)
            return self._uol_telemetry_finish_step(
                _uol_tel,
                self._needs_human_outcome(
                    step,
                    contract,
                    msg,
                    before,
                    t0,
                    retries=retries,
                    strategy=strat,
                    error=fail_reason,
                ),
            )

        if (
            uol_hook
            and uol_hook.executed
            and uol_hook.strategy_result
            and uol_hook.strategy_result.ok
        ):
            if contract.postcondition_settle_ms > 0:
                time.sleep(contract.postcondition_settle_ms / 1000.0)
            try:
                state_after = self._await_postcondition(
                    contract,
                    deadline_ms=contract.postcondition_timeout_ms,
                    step=step,
                )
            except _NeedsHuman as nh:
                return self._needs_human_outcome(
                    step,
                    contract,
                    nh.message,
                    before,
                    t0,
                    retries=retries,
                    strategy="uol:osue_blocked",
                )
            uol_extra = (
                uol_hook.strategy_result.extra
                if uol_hook.strategy_result and uol_hook.strategy_result.extra
                else {}
            )
            handler_validated = (
                isinstance(uol_extra, dict)
                and uol_extra.get("validation_status") == "passed"
            )
            if contract.postcondition(state_after) or (
                (is_context_creation_step(step) or is_surface_operation_step(step))
                and handler_validated
            ):
                strat = uol_hook.strategy_used or "uol:executed"
                duration_ms = int((time.time() - t0) * 1000)
                self.learning.record_success(
                    run_id=self.run_id,
                    kind=step.kind,
                    step_id=step.id,
                    strategy=strat,
                    retries=retries,
                    duration_ms=duration_ms,
                    state_before=before,
                    state_after=state_after.to_dict(),
                )
                self._maybe_sll_record(
                    step,
                    before,
                    strat,
                    success=True,
                    duration_ms=float(duration_ms),
                    recovery_pass=0,
                    validator_passed=True,
                )
                msg = contract.human_done or uol_hook.strategy_result.message or "Acción UOL ejecutada."
                self._announce(msg)
                self._maybe_checkpoint(step, contract, state_after)
                return self._uol_telemetry_finish_step(
                    _uol_tel,
                    StepOutcome(
                        step_id=step.id,
                        kind=step.kind,
                        status="success",
                        strategy_used=strat,
                        retries=retries,
                        duration_ms=duration_ms,
                        human_message=msg,
                        state_before=before,
                        state_after=state_after.to_dict(),
                    ),
                    validation_passed=True,
                )
            if is_context_creation_step(step):
                fail_reason = OPEN_TAB_NOT_CONFIRMED
                extra = uol_hook.strategy_result.extra if uol_hook.strategy_result else {}
                if isinstance(extra, dict):
                    fail_reason = str(
                        extra.get("open_tab_failure_reason") or OPEN_TAB_NOT_CONFIRMED,
                    )
                return self._uol_telemetry_finish_step(
                    _uol_tel,
                    self._needs_human_outcome(
                        step,
                        contract,
                        uol_hook.strategy_result.message
                        if uol_hook.strategy_result
                        else "No confirmé la nueva pestaña.",
                        before,
                        t0,
                        retries=retries,
                        strategy=uol_hook.strategy_used or "open_tab:uol_hotkey",
                        state_after=state_after.to_dict(),
                        error=fail_reason,
                    ),
                )
            if is_surface_operation_step(step):
                fail_reason = surface_operation_failure_reason(step)
                extra = uol_hook.strategy_result.extra if uol_hook.strategy_result else {}
                if isinstance(extra, dict):
                    fail_reason = str(
                        extra.get("search_failure_reason")
                        or extra.get("surface_failure_reason")
                        or fail_reason,
                    )
                return self._uol_telemetry_finish_step(
                    _uol_tel,
                    self._needs_human_outcome(
                        step,
                        contract,
                        uol_hook.strategy_result.message
                        if uol_hook.strategy_result
                        else "No confirmé el resultado de la búsqueda.",
                        before,
                        t0,
                        retries=retries,
                        strategy=uol_hook.strategy_used or "search_content:uol_surface",
                        state_after=state_after.to_dict(),
                        error=fail_reason,
                    ),
                )

        # 3b. Entity-first (ERL) antes de TargetResolver legacy / coords.
        entity_hook = None
        _allow_entity_first = True
        if self._uool_decision is not None:
            _allow_entity_first = bool(self._uool_decision.allow_entity_first)
        if (
            _allow_entity_first
            and not is_context_creation_step(step)
            and not is_surface_operation_step(step)
        ):
            try:
                from app.services.runtime.entity_runtime_resolution import (
                    entity_runtime_budget_from_settings,
                    try_entity_first_runtime,
                )

                entity_budget = entity_runtime_budget_from_settings()
                entity_hook = try_entity_first_runtime(
                    step, state, getattr(self, "mission_ref", None),
                    expert_mode=self.expert_mode,
                    budget=entity_budget,
                )
            except Exception as e:
                log.debug("[ERL] entity-first hook: %s", e)

        if _step_time_exceeded():
            return self._uol_telemetry_finish_step(
                _uol_tel,
                _step_timeout_outcome(strategy="step:timeout_post_entity"),
            )

        if entity_hook and entity_hook.failure_reason:
            err = entity_hook.failure_reason
            try:
                from app.services.runtime.entity_runtime_resolution import (
                    sanitize_non_entity_step_failure_reason,
                )

                err = sanitize_non_entity_step_failure_reason(step, err)
            except Exception:
                pass
            return self._uol_telemetry_finish_step(
                _uol_tel,
                self._needs_human_outcome(
                    step,
                    contract,
                    entity_hook.human_message or "No pude resolver la entidad.",
                    before,
                    t0,
                    retries=retries,
                    strategy=f"entity_resolution:{entity_hook.failure_reason}",
                    error=err,
                ),
            )

        if entity_hook and entity_hook.blocked_unsafe:
            return self._needs_human_outcome(
                step,
                contract,
                entity_hook.human_message or "Confirmación humana requerida.",
                before,
                t0,
                retries=retries,
                strategy="entity_resolution:blocked_unsafe",
            )

        if entity_hook and entity_hook.blocked_ambiguity:
            return self._uol_telemetry_finish_step(
                _uol_tel,
                self._needs_human_outcome(
                    step,
                    contract,
                    entity_hook.human_message or "Varias entidades similares; elige una.",
                    before,
                    t0,
                    retries=retries,
                    strategy="entity_resolution:ambiguity",
                    error=entity_hook.failure_reason or "ENTITY_AMBIGUOUS",
                ),
            )

        if (
            entity_hook
            and entity_hook.executed
            and entity_hook.strategy_result
            and entity_hook.strategy_result.ok
        ):
            if contract.postcondition_settle_ms > 0:
                time.sleep(contract.postcondition_settle_ms / 1000.0)
            try:
                state_after = self._await_postcondition(
                    contract,
                    deadline_ms=contract.postcondition_timeout_ms,
                    step=step,
                )
            except _NeedsHuman as nh:
                return self._needs_human_outcome(
                    step,
                    contract,
                    nh.message,
                    before,
                    t0,
                    retries=retries,
                    strategy="uol:osue_blocked",
                )
            if contract.postcondition(state_after):
                strat = f"entity_resolution:{entity_hook.strategy_used or 'resolved'}"
                duration_ms = int((time.time() - t0) * 1000)
                self.learning.record_success(
                    run_id=self.run_id,
                    kind=step.kind,
                    step_id=step.id,
                    strategy=strat,
                    retries=retries,
                    duration_ms=duration_ms,
                    state_before=before,
                    state_after=state_after.to_dict(),
                )
                self._maybe_sll_record(
                    step,
                    before,
                    strat,
                    success=True,
                    duration_ms=float(duration_ms),
                    recovery_pass=0,
                    validator_passed=True,
                )
                msg = contract.human_done or "Entidad ejecutada."
                self._announce(msg)
                self._maybe_checkpoint(step, contract, state_after)
                return StepOutcome(
                    step_id=step.id,
                    kind=step.kind,
                    status="success",
                    strategy_used=strat,
                    retries=retries,
                    duration_ms=duration_ms,
                    human_message=msg,
                    state_before=before,
                    state_after=state_after.to_dict(),
                )
            last_error = (
                entity_hook.strategy_result.message
                or "postcondition no cumplida tras entity_resolution"
            )
            log.debug("[ERL] entity click ok pero postcondition falló: %s", last_error)

        # 4. Ejecutar acción con prioridad y fallbacks (+ rondas opcionales ARL).
        candidate_strategies = _resolve_strategies(step, contract, self.mission_ref)
        if step.params.get("uol_action"):
            try:
                from app.services.missions.uol_action_handlers import (
                    filter_coords_primary_strategies,
                )

                candidate_strategies = filter_coords_primary_strategies(
                    candidate_strategies,
                )
            except Exception:
                pass
        if entity_hook and entity_hook.skip_legacy_coords:
            try:
                from app.services.runtime.entity_runtime_resolution import (
                    filter_strategies_when_entity_present,
                )

                candidate_strategies = filter_strategies_when_entity_present(
                    candidate_strategies,
                    entity_resolved=bool(entity_hook.resolved),
                )
            except Exception:
                pass

        def _rank_for_arl_pass(pass_index: int) -> Tuple[List[str], Optional[str]]:
            if step.params.get("preferred_strategy"):
                base = list(candidate_strategies)
            else:
                from app.services.runtime.strategy_learning_layer import (
                    build_context_from_semantic_step,
                    execution_signals_for_mission,
                    explanations_as_human_summary,
                    rank_strategies_with_learning,
                )

                ctx = build_context_from_semantic_step(
                    step, self.mission_ref, before,
                )
                truth, sep = execution_signals_for_mission(self.mission_ref)
                ompc_sub: Optional[Dict[str, float]] = None
                try:
                    from app.core.config import get_settings as _gs_ompc

                    if getattr(_gs_ompc(), "OMPC_ENABLED", False) and getattr(
                        _gs_ompc(), "SLL_ENABLED", False,
                    ):
                        from app.services.runtime.operational_memory import (
                            ompc_strategy_boosts_for_prefix,
                        )

                        ompc_sub = ompc_strategy_boosts_for_prefix(
                            self.mission_ref,
                            self._omp_prior_kinds,
                            current_step_kind=str(step.kind),
                        )
                except Exception:
                    ompc_sub = None
                ranked, expl = rank_strategies_with_learning(
                    list(candidate_strategies),
                    ctx,
                    learning=self.learning,
                    execution_truth_confirmed=truth,
                    sep_alignment_score=sep,
                    ompc_substring_boosts=ompc_sub,
                )
                self._last_sll_explanations = expl
                base = list(ranked)
                base = (
                    [s for s in base if not _is_forbidden_primary(s)]
                    + [s for s in base if _is_forbidden_primary(s)]
                )
                if self.expert_mode and pass_index == 0:
                    for line in explanations_as_human_summary(expl, top_n=4):
                        self._log_expert(f"[SLL] {line}")
            if pass_index > 0:
                from app.services.runtime.arl_sep_recovery import (
                    ARL_UNSAFE_COORDS_ONLY,
                    filter_strategies_exclude_emergency_coords,
                )

                base, unsafe_reason = filter_strategies_exclude_emergency_coords(base)
                if unsafe_reason == ARL_UNSAFE_COORDS_ONLY:
                    return [], unsafe_reason
            return base, None

        arl_cap = 0
        try:
            from app.core.config import get_settings as _gs_arl

            _cfg_arl = _gs_arl()
            if getattr(_cfg_arl, "ARL_ENABLED", False):
                arl_cap = int(getattr(_cfg_arl, "ARL_SEP_RECOVERY_MAX_ROUNDS", 0) or 0)
        except Exception:
            arl_cap = 0

        last_error = ""
        last_strategy_used: Optional[str] = None
        last_res_ok = True
        success = False
        state_after = state

        recovery_pass = 0
        while True:
            ranked, arl_coord_block = _rank_for_arl_pass(recovery_pass)
            if arl_coord_block:
                try:
                    from app.services.runtime.adaptive_runtime_layer import arl_append_audit_event

                    mr = getattr(self, "mission_ref", None)
                    if mr is not None:
                        arl_append_audit_event(
                            mr,
                            {
                                "type": "sep_smart_executor_recovery",
                                "skipped": True,
                                "reason": arl_coord_block,
                                "step_kind": step.kind,
                                "step_id": step.id,
                                "decision_summary": {
                                    "decision": "REQUEST_HUMAN",
                                    "reason": arl_coord_block,
                                },
                            },
                        )
                except Exception:
                    pass
                return self._needs_human_outcome(
                    step,
                    contract,
                    "Tras adaptación ARL no queda ninguna estrategia segura; "
                    "sólo coords/visión de emergencia. Se requiere intervención humana.",
                    before,
                    t0,
                    retries=retries,
                    strategy=last_strategy_used,
                    state_after=state_after.to_dict(),
                )

            idx = 0
            while idx < len(ranked):
                strategy = ranked[idx]
                if attempts.get(strategy, 0) >= MAX_PER_STRATEGY:
                    idx += 1
                    continue
                attempts[strategy] = attempts.get(strategy, 0) + 1
                retries += 1
                self._log_expert(f"→ {step.kind} :: try {strategy} (#{attempts[strategy]})")

                res = self.runner.run_strategy(strategy, step, state)
                last_strategy_used = strategy
                last_res_ok = bool(res.ok)

                if _uol_tel and _uol_tel.active:
                    try:
                        _uol_tel.record_strategy_attempt(
                            strategy,
                            res,
                            validation_passed=False,
                        )
                    except Exception:
                        pass

                if not res.ok and res.extra and res.extra.get("ask_human"):
                    self._maybe_sll_record(
                        step,
                        before,
                        strategy,
                        success=False,
                        duration_ms=float(res.duration_ms or 0),
                        recovery_pass=recovery_pass,
                        human_intervention=True,
                    )
                    return self._needs_human_outcome(
                        step,
                        contract,
                        res.message or "Necesito una pista del usuario.",
                        before,
                        t0,
                        retries=retries,
                        strategy=strategy,
                    )

                if contract.postcondition_settle_ms > 0:
                    time.sleep(contract.postcondition_settle_ms / 1000.0)

                try:
                    state_after = self._await_postcondition(
                        contract,
                        deadline_ms=contract.postcondition_timeout_ms,
                        step=step,
                    )
                except _NeedsHuman as nh:
                    return self._needs_human_outcome(
                        step,
                        contract,
                        nh.message,
                        before,
                        t0,
                        retries=retries,
                        strategy=strategy,
                    )

                if contract.postcondition(state_after):
                    success = True
                    if _uol_tel and _uol_tel.active:
                        try:
                            _uol_tel.record_strategy_attempt(
                                strategy,
                                res,
                                validation_passed=True,
                            )
                        except Exception:
                            pass
                    break

                last_error = self._osue_postcondition_error() or res.message or "postcondition no cumplida"
                self.learning.record_failure(
                    run_id=self.run_id,
                    kind=step.kind,
                    step_id=step.id,
                    strategy=strategy,
                    error=last_error,
                    retries=retries,
                    duration_ms=res.duration_ms,
                    state_before=before,
                    state_after=state_after.to_dict(),
                )
                self._maybe_sll_record(
                    step,
                    before,
                    strategy,
                    success=False,
                    duration_ms=float(res.duration_ms or 0),
                    recovery_pass=recovery_pass,
                )
                idx += 1

            if success:
                break

            if recovery_pass >= arl_cap:
                break

            _allow_arl = True
            if self._uool_decision is not None:
                _allow_arl = bool(self._uool_decision.allow_arl_recovery)
            if not _allow_arl:
                break

            from app.core.config import get_settings as _gs_arl_eval
            from app.services.runtime.arl_sep_recovery import evaluate_sep_arl_recovery

            verdict = evaluate_sep_arl_recovery(
                mission=getattr(self, "mission_ref", None),
                step=step,
                contract=contract,
                observed_snapshot=state_after,
                before_state=before,
                last_strategy_used=last_strategy_used,
                last_strategy_ok=last_res_ok,
                last_error=last_error,
                arl_round_done=recovery_pass,
                max_extra_rounds=arl_cap,
                retry_budget=int(getattr(_gs_arl_eval(), "ARL_MAX_RETRIES", 1) or 1),
            )
            recovery_pass += 1
            if verdict.escalate_human_immediate:
                return self._needs_human_outcome(
                    step,
                    contract,
                    verdict.human_message or "ARL: se requiere intervención humana.",
                    before,
                    t0,
                    retries=retries,
                    strategy=last_strategy_used,
                    state_after=state_after.to_dict(),
                )
            if not verdict.retry_step:
                break
            attempts.clear()
            state = state_after

        if success:
            duration_ms = int((time.time() - t0) * 1000)
            self.learning.record_success(
                run_id=self.run_id,
                kind=step.kind,
                step_id=step.id,
                strategy=last_strategy_used or "?",
                retries=retries,
                duration_ms=duration_ms,
                state_before=before,
                state_after=state_after.to_dict(),
            )
            self._maybe_sll_record(
                step,
                before,
                last_strategy_used,
                success=True,
                duration_ms=float(duration_ms),
                recovery_pass=recovery_pass,
                validator_passed=True,
            )
            msg = contract.human_done or "Listo."
            self._announce(msg)
            self._maybe_checkpoint(step, contract, state_after)
            return self._uol_telemetry_finish_step(
                _uol_tel,
                StepOutcome(
                    step_id=step.id,
                    kind=step.kind,
                    status="success",
                    strategy_used=last_strategy_used,
                    retries=retries,
                    duration_ms=duration_ms,
                    human_message=msg,
                    state_before=before,
                    state_after=state_after.to_dict(),
                ),
                validation_passed=True,
            )

        # 5. Recovery por postcondición — no rollback si hubo fase transitoria post-submit.
        try:
            from app.services.runtime.entity_runtime_resolution import (
                SEARCH_RESULTS_NOT_CONFIRMED,
            )
            from app.services.runtime.operational_state_understanding_engine import (
                is_search_submit_step,
                search_wait_should_block_recovery,
            )
        except Exception:
            is_search_submit_step = lambda _s: False  # type: ignore[assignment,misc]
            search_wait_should_block_recovery = lambda _w: False  # type: ignore[assignment,misc]
            SEARCH_RESULTS_NOT_CONFIRMED = "SEARCH_RESULTS_NOT_CONFIRMED"

        wr = getattr(self, "_last_osue_wait", None)
        if is_search_submit_step(step) and search_wait_should_block_recovery(wr):
            fail_reason = SEARCH_RESULTS_NOT_CONFIRMED
            if wr and getattr(wr, "needs_human", False):
                fail_reason = str(getattr(wr, "reason", "") or "BLOCKED_STATE")
            return self._uol_telemetry_finish_step(
                _uol_tel,
                self._needs_human_outcome(
                    step,
                    contract,
                    "No confirmé los resultados de búsqueda a tiempo.",
                    before,
                    t0,
                    retries=retries,
                    strategy=last_strategy_used or "search_content:uol_surface",
                    state_after=state_after.to_dict(),
                    error=fail_reason,
                ),
            )

        try:
            state_after = self._recover_postcondition_loop(
                step, contract, state_after, attempts
            )
        except _NeedsHuman as nh:
            return self._needs_human_outcome(
                step, contract, nh.message, before, t0,
                retries=retries, strategy=last_strategy_used,
                state_after=state_after.to_dict(),
            )

        # Si llegamos aquí: postcondición se cumplió tras recovery
        duration_ms = int((time.time() - t0) * 1000)
        msg = contract.human_done or "Listo."
        self._announce(msg)
        self._maybe_checkpoint(step, contract, state_after)
        return StepOutcome(
            step_id=step.id,
            kind=step.kind,
            status="success",
            strategy_used=last_strategy_used,
            retries=retries,
            duration_ms=duration_ms,
            human_message=msg,
            state_before=before,
            state_after=state_after.to_dict(),
        )

    # ── Sub-rutinas ──────────────────────────────────────────────

    def _uol_telemetry_finish_step(
        self,
        tracker: Any,
        outcome: StepOutcome,
        *,
        validation_passed: bool = False,
    ) -> StepOutcome:
        try:
            if tracker is not None and tracker.active and not tracker._finalized:
                fail_reason = outcome.error or (
                    "" if outcome.status == "success" else (outcome.human_message or "")
                )
                try:
                    from app.services.runtime.entity_runtime_resolution import (
                        sanitize_non_entity_step_failure_reason,
                    )
                    from app.services.missions.execution_contracts import MissionStep

                    step_ref = MissionStep(
                        id=outcome.step_id,
                        kind=outcome.kind,
                        params=getattr(tracker, "step", None).params
                        if getattr(tracker, "step", None) is not None
                        else {},
                    )
                    fail_reason = sanitize_non_entity_step_failure_reason(
                        step_ref, fail_reason,
                    )
                except Exception:
                    pass
                tracker.finalize(
                    success=outcome.status in ("success", "skipped"),
                    validation_passed=validation_passed or outcome.status == "success",
                    failure_reason=fail_reason,
                    strategy_used=outcome.strategy_used,
                )
        except Exception as exc:
            log.debug("[UOL telemetry] finalize: %s", exc)
        return outcome

    def _detect(self, deep: bool = False) -> StateSnapshot:
        return self.detector.detect(deep=deep)

    def _await_postcondition(
        self,
        contract: StepContract,
        deadline_ms: int,
        *,
        step: Optional[MissionStep] = None,
    ) -> StateSnapshot:
        """Espera postcondición: OSUE state-wait si habilitado; polling legacy si no."""
        if step is not None:
            try:
                from app.services.runtime.arl_sep_recovery import maybe_adjust_postcondition_deadline_ms

                deadline_ms = maybe_adjust_postcondition_deadline_ms(step, deadline_ms)
            except Exception:
                pass

        try:
            from app.services.runtime.operational_state_understanding_engine import osue_enabled

            if osue_enabled():
                return self._await_postcondition_osue(
                    contract,
                    deadline_ms,
                    step=step,
                )
        except _NeedsHuman:
            raise
        except Exception as exc:
            log.debug("[OSUE] postcondition hook: %s", exc)

        return self._await_postcondition_legacy(contract, deadline_ms)

    def _await_postcondition_legacy(
        self,
        contract: StepContract,
        deadline_ms: int,
    ) -> StateSnapshot:
        """Polling barato: detectamos cada 350 ms hasta cumplir o timeout."""
        end = time.time() + (deadline_ms / 1000.0)
        last = self._detect()
        while time.time() < end:
            if contract.postcondition(last):
                return last
            time.sleep(0.35)
            last = self._detect()
        return last

    def _await_postcondition_osue(
        self,
        contract: StepContract,
        deadline_ms: int,
        *,
        step: Optional[MissionStep] = None,
    ) -> StateSnapshot:
        from app.services.runtime.operational_state_understanding_engine import (
            OsueReadiness,
            blocked_states_for_semantic_type,
            build_operational_state_snapshot,
            build_osue_input_from_state_snapshot,
            evaluate_step_compatibility,
            is_search_submit_step,
            persist_osue_wait_on_mission,
            resolve_machine_step_for_mission,
            resolve_postcondition_target_states,
            resolve_search_osue_wait_profile,
            run_osue_postcondition_wait,
        )

        mission = getattr(self, "mission_ref", None)
        step_id = step.id if step else ""
        machine_step = resolve_machine_step_for_mission(mission, step_id)
        step_kind = step.kind if step else ""
        semantic = machine_step.semantic_type if machine_step else step_kind

        targets = resolve_postcondition_target_states(machine_step, step_kind)
        blocked = blocked_states_for_semantic_type(semantic)
        lineage = list(getattr(mission, "osue_state_lineage", None) or []) if mission else []

        def snapshot_factory():
            state = self._detect(deep=True)
            extra_validators: Dict[str, Any] = {}
            if step is not None and is_search_submit_step(step):
                extra_validators["post_submit"] = True
                extra_validators["form_submitted"] = True
            inp = build_osue_input_from_state_snapshot(
                state,
                mission=mission,
                step=step,
                machine_step=machine_step,
                state_lineage=lineage,
                extra_validators=extra_validators or None,
            )
            snap = build_operational_state_snapshot(inp)
            if snap.operational_state_type.value not in lineage[-1:]:
                lineage.append(snap.operational_state_type.value)
            return snap

        wait_profile = None
        if step is not None and is_search_submit_step(step):
            wait_profile = resolve_search_osue_wait_profile(step, deadline_ms=deadline_ms)

        if machine_step is not None:
            initial = snapshot_factory()
            compat = evaluate_step_compatibility(initial, machine_step)
            if compat.readiness == OsueReadiness.BLOCKED and initial.auth_state == "required":
                from app.services.runtime.operational_state_understanding_engine import OsueWaitResult

                wait_result = OsueWaitResult(
                    needs_human=True,
                    reason="OSUE_BLOCKED:auth_required_state",
                    expected_state=targets[-1] if targets else "",
                    actual_state=initial.operational_state_type.value,
                    target_states=list(targets),
                    stability_score=initial.state_stability_score,
                    ambiguity_score=initial.ambiguity_score,
                    blockers=list(initial.runtime_blockers),
                    state_lineage=list(lineage),
                )
                self._last_osue_wait = wait_result
                persist_osue_wait_on_mission(mission, step_id=step_id, wait_result=wait_result)
                raise _NeedsHuman(
                    "Autenticación requerida — intervención humana necesaria.",
                    reason=wait_result.reason,
                )

        wait_result = run_osue_postcondition_wait(
            target_states=targets,
            snapshot_factory=snapshot_factory,
            timeout_ms=deadline_ms,
            poll_ms=200,
            blocked_states=blocked,
            wait_profile=wait_profile,
        )
        final_state = self._detect(deep=True)

        if not wait_result.matched and contract.postcondition(final_state):
            from app.services.runtime.operational_state_understanding_engine import OsueWaitResult

            wait_result = OsueWaitResult(
                matched=True,
                reason="OSUE_VALIDATOR_ALIGNED:postcondition_met",
                expected_state=wait_result.expected_state or (targets[-1] if targets else ""),
                actual_state=wait_result.actual_state or targets[-1] if targets else "",
                target_states=list(targets),
                elapsed_ms=wait_result.elapsed_ms,
                polled=wait_result.polled,
                stability_score=wait_result.stability_score,
                ambiguity_score=wait_result.ambiguity_score,
                blockers=list(wait_result.blockers),
                state_lineage=list(wait_result.state_lineage or lineage),
            )

        self._last_osue_wait = wait_result
        persist_osue_wait_on_mission(mission, step_id=step_id, wait_result=wait_result)

        if wait_result.needs_human:
            raise _NeedsHuman(
                "Estado operacional bloqueado — intervención humana necesaria.",
                reason=wait_result.reason,
            )

        if wait_result.timed_out and not contract.postcondition(final_state):
            log.debug(
                "[OSUE] postcondition timeout step=%s expected=%s actual=%s",
                step_id,
                wait_result.expected_state,
                wait_result.actual_state,
            )

        if wait_result.incompatible and not contract.postcondition(final_state):
            log.debug(
                "[OSUE] state incompatible step=%s reason=%s",
                step_id,
                wait_result.reason,
            )

        return final_state

    def _osue_postcondition_error(self) -> str:
        wr = getattr(self, "_last_osue_wait", None)
        if wr is None:
            return ""
        reason = str(getattr(wr, "reason", "") or "")
        if reason == "RESULTS_TIMEOUT":
            return "SEARCH_RESULTS_NOT_CONFIRMED"
        if getattr(wr, "timed_out", False):
            return "OSUE_STATE_TIMEOUT"
        if getattr(wr, "incompatible", False):
            return "OSUE_STATE_INCOMPATIBLE"
        if reason:
            return reason
        return ""

    def _ensure_precondition(
        self,
        step: MissionStep,
        contract: StepContract,
        state: StateSnapshot,
        attempts: Dict[str, int],
    ) -> StateSnapshot:
        if contract.precondition_grace_ms > 0:
            time.sleep(contract.precondition_grace_ms / 1000.0)
            state = self._detect()
        if contract.precondition(state):
            return state

        # Recovery de precondition: invocamos al motor.
        plan = self.recovery.recover_precondition(step, contract, state, attempts)
        if plan.escalate_human:
            raise _NeedsHuman(
                plan.human_message
                or "No tengo cómo preparar el sistema para este paso.",
                reason=plan.reason,
            )
        if plan.human_message:
            self._announce(plan.human_message)
        # Intentamos las estrategias del plan; cualquiera puede traer
        # el sistema al estado correcto (p.ej. abrir Chrome para que
        # 'open_url' tenga su precondición).
        for strategy in plan.next_strategies:
            attempts[strategy] = attempts.get(strategy, 0) + 1
            res = self.runner.run_strategy(strategy, step, state)
            if not res.ok and res.extra and res.extra.get("ask_human"):
                raise _NeedsHuman(res.message or "Necesito tu ayuda.")
            time.sleep(0.6)
            state = self._detect()
            if contract.precondition(state):
                return state
        raise _NeedsHuman(
            plan.human_message
            or "No logré preparar el sistema para este paso."
        )

    def _recover_postcondition_loop(
        self,
        step: MissionStep,
        contract: StepContract,
        state_after: StateSnapshot,
        attempts: Dict[str, int],
    ) -> StateSnapshot:
        """Itera el RecoveryEngine hasta cumplir postcondición o agotarse."""
        # Hard cap de seguridad: no más de 6 ciclos de recovery.
        for cycle in range(6):
            plan = self.recovery.recover_postcondition(
                step, contract, state_after, attempts
            )
            if plan.human_message:
                self._announce(plan.human_message)

            if plan.escalate_human:
                raise _NeedsHuman(
                    plan.human_message
                    or "Necesito intervención para arreglar este paso.",
                    reason=plan.reason,
                )

            if plan.rollback_to_checkpoint:
                self._log_expert(
                    f"recovery rollback → {plan.rollback_to_checkpoint}"
                )
                # En este executor minimalista, el rollback se traduce a
                # "esperar y volver a detectar" — un executor más
                # ambicioso podría re-ejecutar pasos hacia el checkpoint.
                time.sleep(0.5)

            for strategy in plan.next_strategies:
                attempts[strategy] = attempts.get(strategy, 0) + 1
                res = self.runner.run_strategy(strategy, step, state_after)
                if not res.ok and res.extra and res.extra.get("ask_human"):
                    raise _NeedsHuman(res.message or "Necesito tu ayuda.")
                if contract.postcondition_settle_ms > 0:
                    time.sleep(contract.postcondition_settle_ms / 1000.0)
                state_after = self._await_postcondition(
                    contract,
                    contract.postcondition_timeout_ms,
                    step=step,
                )
                if contract.postcondition(state_after):
                    self.learning.record_success(
                        run_id=self.run_id,
                        kind=step.kind,
                        step_id=step.id,
                        strategy=strategy,
                        retries=attempts[strategy],
                        duration_ms=res.duration_ms,
                        correction_applied=plan.correction_label,
                        state_after=state_after.to_dict(),
                    )
                    return state_after
                self.learning.record_failure(
                    run_id=self.run_id,
                    kind=step.kind,
                    step_id=step.id,
                    strategy=strategy,
                    error=res.message or "postcondition no cumplida",
                    retries=attempts[strategy],
                    duration_ms=res.duration_ms,
                    state_after=state_after.to_dict(),
                )
            # Fin de ciclo: si seguimos en false, repetimos plan (se
            # filtran las estrategias agotadas ⇒ eventualmente vacía
            # ⇒ escalate_human).
        raise _NeedsHuman(
            "Agoté los reintentos de recuperación para este paso."
        )

    def _maybe_checkpoint(
        self,
        step: MissionStep,
        contract: StepContract,
        state: StateSnapshot,
    ) -> None:
        if not contract.checkpoint_key:
            return
        try:
            self.checkpoints.save_from_state(
                key=contract.checkpoint_key,
                step_id=step.id,
                state=state,
                block_id=step.block_id,
                recovery_hint=contract.human_done,
            )
        except Exception as e:
            log.debug(f"checkpoint save falló (ignorado): {e}")

    def _maybe_sll_record(
        self,
        step: MissionStep,
        state_before: Dict[str, Any],
        strategy: Optional[str],
        *,
        success: bool,
        duration_ms: float,
        recovery_pass: int = 0,
        validator_passed: Optional[bool] = None,
        human_intervention: bool = False,
        mission_terminal_failed: bool = False,
        fallback_to_sep: bool = False,
        arl_failure: bool = False,
    ) -> None:
        if not strategy or str(strategy).startswith("skip:"):
            return
        try:
            from app.core.config import get_settings

            if not get_settings().SLL_ENABLED:
                return
        except Exception:
            return
        try:
            from app.services.runtime.strategy_learning_layer import (
                build_context_from_semantic_step,
                record_observation,
            )

            ctx = build_context_from_semantic_step(
                step, self.mission_ref, state_before,
            )
            record_observation(
                context=ctx,
                strategy_name=str(strategy),
                success=success,
                duration_ms=float(duration_ms),
                mission=self.mission_ref,
                validator_passed=validator_passed,
                fallback_to_sep=fallback_to_sep,
                arl_recovery=(recovery_pass > 0 and success),
                arl_failure=arl_failure,
                human_intervention=human_intervention,
                mission_terminal_failed=mission_terminal_failed,
            )
        except Exception as e:
            log.debug("SLL record skipped: %s", e)

    def _step_deadline_mono(self, t0: float, step: MissionStep) -> float:
        try:
            from app.core.config import get_settings

            cfg = get_settings()
            if step.kind in ("select_entity_from_collection", "select_profile"):
                ms = int(getattr(cfg, "ENTITY_STEP_HARD_TIMEOUT_MS", 45_000))
            else:
                ms = int(getattr(cfg, "SMART_EXECUTOR_STEP_TIMEOUT_MS", 90_000))
        except Exception:
            ms = 45_000 if step.kind in ("select_entity_from_collection", "select_profile") else 90_000
        return time.monotonic() + (ms / 1000.0)

    def _needs_human_outcome(
        self,
        step: MissionStep,
        contract: StepContract,
        message: str,
        before: Dict[str, Any],
        t0: float,
        *,
        retries: int = 0,
        strategy: Optional[str] = None,
        state_after: Optional[Dict[str, Any]] = None,
        error: str = "",
    ) -> StepOutcome:
        try:
            from app.core.config import get_settings as _cfg_sh

            if getattr(_cfg_sh(), "RELIABILITY_HARDENING_ENABLED", False):
                try:
                    from app.services.runtime.self_healing_wiring import (
                        record_ordered_pre_repair_phases_before_human,
                    )

                    record_ordered_pre_repair_phases_before_human(
                        self.mission_ref,
                        step_id=getattr(step, "id", None),
                        hint="needs_human_pending",
                    )
                except Exception as _shy:
                    log.debug("[SmartExecutor] pre-human self-healing trace: %s", _shy)
        except Exception:
            pass

        msg = message or contract.human_recover or "Necesito tu ayuda para continuar."
        # PRD 2026-05-09 §exec-runtime: cuando el step de selección de
        # perfil agota recovery sin satisfacer la postcondition (perfil
        # no existe, picker no clickeable, profile_name vacío…), el
        # historial debe quedar con el código canónico
        # ``PROFILE_SELECTION_FAILED`` para que el usuario sepa que
        # arreglar es el perfil, no el botón.
        error_code = str(error or "").strip()
        if not error_code and step.kind in ("select_profile", "select_entity_from_collection"):
            error_code = "PROFILE_SELECTION_FAILED"
        return StepOutcome(
            step_id=step.id,
            kind=step.kind,
            status="needs_human",
            strategy_used=strategy,
            retries=retries,
            duration_ms=int((time.time() - t0) * 1000),
            human_message=msg,
            error=error_code,
            state_before=before,
            state_after=state_after,
        )

    def _unsupported_kind_outcome(self, step: MissionStep) -> StepOutcome:
        """Construye el outcome canónico ``UNSUPPORTED_SEMANTIC_STEP_TYPE``.

        El error incluye:
          * ``step_id`` y ``step_type`` para que el log sea trazable.
          * ``human_label`` (lo que la UI le mostraba al usuario).
          * ``available_executors`` (lista de ``kind`` que SÍ tienen
            builder), para que el equipo sepa qué soporta el runtime
            y pueda añadir el faltante.
        """
        from app.services.missions.execution_contracts import (
            supported_kinds,
        )
        kinds = supported_kinds()
        human_label = step.human_label or step.describe()
        msg = (
            f"UNSUPPORTED_SEMANTIC_STEP_TYPE: step_id={step.id} "
            f"step_type={step.kind!r} "
            f"human_label={human_label!r} "
            f"available_executors={kinds}"
        )
        log.error(msg)
        try:
            self._announce(
                f"⚠ Tipo no soportado: '{step.kind}' "
                f"({human_label}). Cierra esta ventana."
            )
        except Exception:
            pass
        return StepOutcome(
            step_id=step.id,
            kind=step.kind,
            status="failed",
            strategy_used=None,
            retries=0,
            duration_ms=0,
            human_message=(
                f"Tipo de paso no soportado por el ejecutor: "
                f"'{step.kind}'. {human_label}"
            ),
            error="UNSUPPORTED_SEMANTIC_STEP_TYPE",
        )

    # ── UX helpers ───────────────────────────────────────────────
    def _announce(self, message: str) -> None:
        if not message:
            return
        if self.on_status:
            try:
                self.on_status(message)
            except Exception:
                pass
        log.info(f"[Nevlan] {message}")

    def _log_expert(self, message: str) -> None:
        if self.expert_mode:
            log.info(f"[expert] {message}")
        else:
            log.debug(f"[expert] {message}")


# ──────────────────────────────────────────────────────────────────────
# Helper de alto nivel
# ──────────────────────────────────────────────────────────────────────

def run_mission(
    steps: List[MissionStep],
    *,
    on_status: Optional[StatusCb] = None,
    on_human_help_needed: Optional[HumanHelpCb] = None,
    on_step_done: Optional[ProgressCb] = None,
    on_step_started: Optional[StepStartedCb] = None,
    expert_mode: bool = False,
) -> MissionResult:
    """Atajo: ejecuta una lista de ``MissionStep`` con un executor fresco."""
    executor = SmartMissionExecutor(
        on_status=on_status,
        on_human_help_needed=on_human_help_needed,
        on_step_done=on_step_done,
        on_step_started=on_step_started,
        expert_mode=expert_mode,
    )
    return executor.run_mission(steps)


def run_single_semantic_plan_step_from_mission(
    mission: Any,
    step_index: int,
    *,
    expert_mode: bool = False,
) -> Tuple[bool, str]:
    """Ejecuta exactamente un paso del ``semantic_execution_plan`` (índice 0-based).

    Usado por la UI cuando los pasos visibles vienen del SEP y ya no coinciden
    con ``compiled_execution_graph`` (Mission Truth Gate).
    """
    from app.services.missions.semantic_execution_plan import (
        SemanticExecutionPlan,
        semantic_plan_to_mission_steps,
    )

    blob = getattr(mission, "semantic_execution_plan", None) or {}
    steps_blob = list(blob.get("steps") or [])
    if not steps_blob:
        return False, "Esta misión no tiene pasos en el plan semántico."
    if step_index < 0 or step_index >= len(steps_blob):
        return False, (
            f"Paso {step_index + 1} fuera de rango "
            f"({len(steps_blob)} pasos en el plan)."
        )
    try:
        plan = SemanticExecutionPlan.from_dict(blob)
    except Exception as e:
        log.debug(f"[semantic_step_test] from_dict: {e}")
        return False, f"No pude leer el plan semántico: {e}"
    mission_steps = semantic_plan_to_mission_steps(plan)
    if step_index >= len(mission_steps):
        return False, "El paso no se pudo convertir a ejecución semántica."
    executor = SmartMissionExecutor(expert_mode=expert_mode, mission_ref=mission)
    result = executor.run_mission([mission_steps[step_index]])
    if result.status == "success":
        return True, ""
    detail = (result.last_message or "").strip()
    if result.outcomes:
        oc = result.outcomes[-1]
        tail = (oc.error or oc.human_message or "").strip()
        if tail:
            detail = f"{detail}\n{tail}".strip() if detail else tail
    return False, detail or "El paso no se completó."


__all__ = [
    "SmartMissionExecutor",
    "MissionResult",
    "StepOutcome",
    "StepStarted",
    "StepStartedCb",
    "run_mission",
    "run_single_semantic_plan_step_from_mission",
    "is_emergency_coordinates_strategy",
]
