"""
ArthurOS Services - Smart Runner Bridge
---------------------------------------
Decide qué ejecutor usa Nevlan para una ``Mission`` concreta:

  1.  ``AutoLearningExecutor`` — para misiones **semánticas**
      (``EXECUTABLE`` + bloques reconocibles). Camino oficial.
      Internamente envuelve ``SmartMissionExecutor`` y enchufa
      ExecutionMemoryStore + StrategyScoringEngine + TargetMemory +
      MissionFailureClassifier + AdaptiveTimeouts + MachineProfile +
      MissionGraphRepair + ExecutionQuality.
  2.  ``MissionPlayer`` (legacy) — sólo como *fallback* para misiones
      viejas sin contrato semántico.

El comportamiento default cambió en 2026-05-06: el bridge ya **no**
construye un ``SmartMissionExecutor`` desnudo. Si por alguna razón
quieres ese comportamiento legacy (debug aislado, repro determinista),
pasa ``use_auto_learning=False`` a ``run_mission_smart``.

Este módulo es el único punto de entrada desde la UI (AutomationCenter /
MissionReview). Encapsula:

* el *gate* de :func:`classify_status` (PRD §3 — prohibido ejecutar si la
  misión tiene bloqueantes humanos: perfil vacío, target vacío, kind
  desconocido, step rojo, ``/temp/`` en assets, validación NONE, etc.);
* la traducción semántica vía :func:`adapt_mission_to_steps`;
* los callbacks de UX (mensaje humano, modo experto, step_done) que la
  UI conecta al overlay de ejecución.

Uso típico (UI o test E2E)::

    from app.services.missions.smart_runner_bridge import run_mission_smart

    decision = run_mission_smart(
        mission,
        on_status=lambda m: print(m),
        on_human_help_needed=lambda o: ...,
        on_step_done=lambda o: ...,
        expert_mode=False,
    )
    if decision.blocked:
        open_quick_reinforcement(decision.blockers)
    else:
        show_result(decision.result)

Diseño:
  * Sin efectos en la UI: el bridge **no** crea widgets, solo llama
    callbacks. Cada interfaz (PyQt / CLI / headless tests) los conecta
    a su gusto.
  * Sin dependencia de Qt: importable en tests sin display.
  * Totalmente sincrónico: el caller decide si lo envuelve en un hilo.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.contracts.mission import Mission, MissionStatus
from app.core.config import get_settings
from app.core.logger import log
from app.services.missions.approval_gate import (
    ApprovalReport,
    check_mission_approvable,
    classify_status,
)
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.semantic_adapter import (
    AdaptedBlocker,
    AdaptedMission,
    adapt_mission_to_steps,
    is_mission_semantic,
)
from app.services.missions.semantic_execution_plan import (
    READY_BLOCKER_CODES,
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_semantic_plan_to_mission,
    build_semantic_execution_plan,
    has_semantic_plan,
    semantic_plan_to_mission_steps,
)
from app.services.missions.semantic_intent_promotion_engine import (
    apply_promotion_to_mission,
)
from app.services.missions.intent_collapse_engine import CollapseStatus
from app.services.telemetry.sipe_metrics import sipe_metrics
from app.services.missions.smart_executor import (
    MissionResult,
    SmartMissionExecutor,
    StepOutcome,
)

# Imports defensivos: si por alguna razón el módulo de auto-learning no
# carga (ej. SQLite ausente en una build minimal), caemos al executor
# clásico para no perder funcionalidad.
try:
    from app.services.missions.auto_learning_executor import (
        AutoLearningExecutor,
        AutoLearningResult,
        HumanCorrectionCb,
    )
    from app.services.missions.execution_quality import ExecutionQualityReport
    from app.services.missions.mission_graph_repair import (
        MissionGraphRepairSuggestion,
    )
    _AUTO_LEARNING_AVAILABLE = True
except Exception:  # pragma: no cover
    AutoLearningExecutor = None  # type: ignore
    AutoLearningResult = None  # type: ignore
    HumanCorrectionCb = Callable[[StepOutcome], Optional[Dict[str, Any]]]  # type: ignore
    ExecutionQualityReport = None  # type: ignore
    MissionGraphRepairSuggestion = None  # type: ignore
    _AUTO_LEARNING_AVAILABLE = False


# ─── Tipos de callbacks ────────────────────────────────────────────────

StatusCb = Callable[[str], None]
HumanHelpCb = Callable[[StepOutcome], None]
ProgressCb = Callable[[StepOutcome], None]
# PRD 2026-05-09 §exec-progress: callback que se dispara ANTES de
# ejecutar cada step (con index 1-based + total). La UI lo usa para
# mover el contador de "paso 0 de N" → "paso 1 de N" al instante en
# que arranca el primer step.
StepStartedCb = Callable[[Any], None]


# ─── Resultado del bridge ─────────────────────────────────────────────


@dataclass
class RunnerDecision:
    """Snapshot de lo que decidió (y ejecutó) el bridge.

    Attributes:
        player_used: ``"smart"`` | ``"legacy"`` | ``"none"``.
        blocked: True si la misión quedó parada por el gate (no se
            ejecutó NADA).
        blockers: lista de motivos humanos por los que no pudo ejecutar.
        result: :class:`MissionResult` cuando se ejecutó con Smart
            Executor. ``None`` en el caso legacy o blocked.
        report: snapshot del approval_gate al momento de decidir.
        status_used: :class:`MissionStatus` calculado por el gate.
        used_fallback: True cuando el SmartExecutor hubiera podido correr
            pero la misión era legacy; el caller decide si realmente
            lanza el player.
        reason: string corto explicando la decisión (para logs y
            reporte E2E).
        started_at / finished_at: timestamps (epoch segs).
    """

    player_used: str = "none"
    blocked: bool = False
    blockers: List[AdaptedBlocker] = field(default_factory=list)
    result: Optional[MissionResult] = None
    report: Optional[ApprovalReport] = None
    status_used: Optional[MissionStatus] = None
    used_fallback: bool = False
    reason: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    adapted: Optional[AdaptedMission] = None
    # Auto-Learning Executor outputs (None si se desactivó vía
    # ``use_auto_learning=False`` o si la lib no estaba disponible).
    quality_report: Optional[Any] = None        # ExecutionQualityReport
    suggestions: List[Any] = field(default_factory=list)  # MissionGraphRepairSuggestion
    auto_learning_enabled: bool = False
    # PRD 2026-05-06c — telemetría del semantic_execution_plan.
    # ``source`` indica de dónde salieron los pasos que recibió el
    # SmartExecutor: ``"semantic_execution_plan"`` (camino canónico),
    # ``"compiled_execution_graph"`` (fallback de compatibilidad cuando
    # el plan no se pudo construir) o ``""`` cuando se ejecutó por la
    # vía legacy.
    source: str = ""
    legacy_graph_ignored: bool = False
    coords_used: bool = False
    semantic_plan: Optional[SemanticExecutionPlan] = None
    semantic_step_kinds: List[str] = field(default_factory=list)
    # PRD 2026-05-14 §SIPE-Telemetry — per-decision wiring path
    # del Semantic Intent Promotion Engine. Útil para tests E2E,
    # dashboards y para decidir cuándo retirar el camino on-demand.
    #
    # Valores posibles de ``sipe_path``:
    #   * ``"already_promoted"`` — la misión llegó con SEP source=SIPE,
    #     no se reprocesó.
    #   * ``"promoted_at_runtime"`` — el bridge invocó SIPE y promovió
    #     en este run.
    #   * ``"legacy_fallback"`` — SIPE falló, cayó a build clásico.
    #   * ``"failure"`` — SIPE Y el clásico fallaron.
    #   * ``""`` — no aplicable (camino legacy puro / blocked / etc.).
    sipe_path: str = ""
    sipe_promotions_count: int = 0

    @property
    def duration_ms(self) -> int:
        end = self.finished_at or time.time()
        return int((end - self.started_at) * 1000)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable para logs / test assertions."""
        d: Dict[str, Any] = {
            "player_used": self.player_used,
            "blocked": self.blocked,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "status_used": (
                self.status_used.value if self.status_used else None
            ),
            "blockers": [
                {"code": b.code, "message": b.message,
                 "step_index": b.step_index, "step_type": b.step_type}
                for b in self.blockers
            ],
            "result_status": (
                self.result.status if self.result else None
            ),
            "result_failed_step_id": (
                self.result.failed_step_id if self.result else None
            ),
            "auto_learning_enabled": self.auto_learning_enabled,
            "source": self.source,
            "legacy_graph_ignored": self.legacy_graph_ignored,
            "coords_used": self.coords_used,
            "semantic_step_kinds": list(self.semantic_step_kinds),
            "sipe_path": self.sipe_path,
            "sipe_promotions_count": self.sipe_promotions_count,
        }
        if self.quality_report is not None:
            try:
                d["quality"] = self.quality_report.to_dict()
            except Exception:
                d["quality"] = None
        if self.suggestions:
            try:
                d["suggestions"] = [s.to_dict() for s in self.suggestions]
            except Exception:
                d["suggestions"] = []
        return d


# ─── API pública ──────────────────────────────────────────────────────


def decide_player(mission: Mission) -> str:
    """Devuelve ``"smart"``, ``"legacy"`` o ``"blocked"``.

    No ejecuta nada — es el pre-check que la UI llama para decidir si
    mostrar el botón "▶ Ejecutar (inteligente)" o el botón "▶ Ejecutar
    (legacy)", y si abrir Quick Reinforcement primero.

    Reglas (PRD §1, §3):
      * Si ``classify_status(mission)`` no es ``EXECUTABLE`` ⇒
        ``"blocked"``.
      * Si la misión tiene bloques semánticos reconocibles ⇒ ``"smart"``.
      * En otro caso (misión legacy pura) ⇒ ``"legacy"``.
    """
    try:
        st = classify_status(mission)
    except Exception as e:  # defensivo: si el gate peta, mejor bloquear.
        log.exception(f"decide_player: classify_status falló: {e}")
        return "blocked"
    if st != MissionStatus.EXECUTABLE:
        return "blocked"
    return "smart" if is_mission_semantic(mission) else "legacy"


def run_mission_smart(
    mission: Mission,
    *,
    on_status: Optional[StatusCb] = None,
    on_human_help_needed: Optional[HumanHelpCb] = None,
    on_step_done: Optional[ProgressCb] = None,
    on_step_started: Optional[StepStartedCb] = None,
    expert_mode: bool = False,
    allow_legacy_fallback: bool = True,
    run_legacy_player: Optional[Callable[[Mission], Any]] = None,
    use_auto_learning: bool = True,
    private_mode: Optional[bool] = None,
    on_human_correction: Optional["HumanCorrectionCb"] = None,
    on_repair_suggestion: Optional[Callable[[Any], str]] = None,
    semantic_start_step_index: int = 0,
) -> RunnerDecision:
    """Ejecuta una misión con el **Smart Executor** cuando procede.

    Args:
        mission: misión a ejecutar. Debe tener ``compiled_execution_graph``
            ya finalizado (assets sin ``/temp/``) para pasar el gate.
        on_status: callback de mensajes humanos (``"Verificando Chrome…"``,
            ``"Buscando en YouTube…"``, etc.).
        on_human_help_needed: callback cuando el executor se queda sin
            ideas y pide intervención (``StepOutcome.status=="needs_human"``).
        on_step_done: callback por cada step completado (ok/skipped/failed).
        expert_mode: si ``True``, el executor emite logs técnicos
            adicionales (precondition / strategy / postcondition /
            recovery / checkpoint) que la UI puede renderizar.
        allow_legacy_fallback: si ``False``, el bridge no llamará a
            ``run_legacy_player`` aunque la misión sea legacy — en su
            lugar marca ``blocked=True`` con motivo ``"no_semantic"``.
            Útil para E2E "sólo Smart Executor".
        run_legacy_player: callable opcional para el fallback legacy
            (``player.MissionPlayer(...).replay()`` envuelto). El bridge
            **no** importa PyQt ni crea threads: delega esa
            responsabilidad a la UI.
        use_auto_learning: por defecto ``True``. Cuando es ``True`` el
            bridge instancia ``AutoLearningExecutor`` (memoria estructurada
            + scoring contextual + clasificador de fallos + adaptive
            timeouts + repair suggestions + quality report). Pásalo a
            ``False`` para volver al ``SmartMissionExecutor`` desnudo
            (debug aislado, repro determinista).
        private_mode: si se pasa ``True``, el ExecutionMemoryStore
            asociado a esta ejecución no escribirá nada a disco. Sólo
            tiene efecto si ``use_auto_learning=True``.
        on_human_correction: callback opcional ``(StepOutcome) -> dict|None``
            para capturar dónde el humano "señaló" el target cuando el
            executor pidió ayuda. La corrección se persiste y se
            reutiliza en próximas ejecuciones (caso de aceptación #4).
        on_repair_suggestion: callback opcional para confirmar al usuario
            una sugerencia de cambio de ``preferred_strategy``. Recibe la
            ``MissionGraphRepairSuggestion`` y devuelve ``"accept"``,
            ``"session_only"`` o ``"reject"``.
        semantic_start_step_index: ejecutar sólo desde este índice (0-based)
            en la lista ``steps`` del plan semántico o adaptador. Sirve para
            continuar después de reparar un paso en Mission Review sin
            re-ejecutar los anteriores.

    Returns:
        :class:`RunnerDecision` con el detalle de qué pasó. Si
        ``use_auto_learning=True``, incluye ``quality_report`` y
        ``suggestions``.
    """
    decision = RunnerDecision(
        report=None,
        started_at=time.time(),
    )

    try:
        from app.services.runtime.nevlan_runtime_convergence import (
            enforce_single_executive_source,
        )

        enforce_single_executive_source(mission)
    except Exception as _exec_src_exc:
        log.debug("[SmartBridge] enforce_single_executive_source: %s", _exec_src_exc)

    _cfg_bridge = get_settings()
    try:
        from app.services.runtime.fase5_deterministic_gate import (
            apply_fase5_prod_runtime_policy,
        )

        allow_legacy_fallback, use_auto_learning = apply_fase5_prod_runtime_policy(
            _cfg_bridge,
            allow_legacy_fallback=allow_legacy_fallback,
            use_auto_learning=use_auto_learning,
        )
    except Exception as _fase5_exc:
        log.debug("[SmartBridge] fase5 runtime policy: %s", _fase5_exc)
    if (
        on_status is not None
        and not expert_mode
        and getattr(_cfg_bridge, "MAGIC_UX_SANITIZE_RUNNER_STATUS", False)
    ):
        from app.interfaces.desktop.magic_ux_status import sanitize_magic_ux_runner_message

        _orig_on_status = on_status

        def on_status(m):  # type: ignore[misc,assignment]
            _orig_on_status(sanitize_magic_ux_runner_message(str(m), expert_mode=False))

    # 1) Approval gate (PRD §3)
    try:
        report = check_mission_approvable(mission)
    except Exception as e:
        log.exception(f"run_mission_smart: approval_gate falló: {e}")
        decision.blocked = True
        decision.reason = f"approval_gate_exception: {e}"
        decision.finished_at = time.time()
        return decision

    decision.report = report
    try:
        decision.status_used = classify_status(mission)
    except Exception:
        decision.status_used = MissionStatus.NEEDS_REVIEW

    if decision.status_used != MissionStatus.EXECUTABLE:
        blockers = [
            AdaptedBlocker(
                code=v.code,
                message=v.message,
                step_index=None,
                step_type=None,
            )
            for v in report.errors
        ]
        decision.blocked = True
        decision.blockers = blockers
        decision.reason = f"gate_status={decision.status_used.value}"
        _announce_blocked(on_status, blockers)
        decision.finished_at = time.time()
        log.info(
            f"[SmartBridge] misión bloqueada por gate ({decision.reason}) "
            f"mission_id={getattr(mission, 'id', '?')}"
        )
        return decision

    # 2) ¿Es semántica? → Smart Executor. Si no, fallback.
    if not is_mission_semantic(mission):
        decision.used_fallback = True
        if not allow_legacy_fallback or run_legacy_player is None:
            decision.blocked = True
            decision.reason = "no_semantic_and_fallback_disabled"
            decision.blockers = [AdaptedBlocker(
                code="legacy_only",
                message=(
                    "Esta misión no tiene bloques semánticos reconocibles "
                    "y el modo 'sólo inteligente' está activo."
                ),
            )]
            decision.finished_at = time.time()
            return decision

        if on_status:
            try:
                on_status(
                    "Esta misión usa el ejecutor antiguo; puede ser menos precisa."
                )
            except Exception:
                pass
        decision.player_used = "legacy"
        decision.reason = "mission_is_legacy"
        log.info(
            "[SmartBridge] misión legacy → usando MissionPlayer antiguo "
            f"mission_id={getattr(mission, 'id', '?')}"
        )
        try:
            result = run_legacy_player(mission)
        except Exception as e:
            log.exception(f"legacy player fallo: {e}")
            decision.blocked = True
            decision.reason = f"legacy_player_exception: {e}"
        decision.finished_at = time.time()
        return decision

    # 3) Semántica ⇒ usar SemanticExecutionPlan como FUENTE ÚNICA.
    #
    # PRD 2026-05-06c: si la misión es EXECUTABLE, ignoramos el
    # ``compiled_execution_graph`` legacy (que arrastra targets con
    # bbox, fallback_strategy=use_coords y bloques GroupControl/
    # video-stream) y construimos los pasos del executor desde el
    # ``semantic_execution_plan``. Si el plan no existe en la misión
    # lo construimos al vuelo y lo persistimos para próximas
    # ejecuciones.
    plan: Optional[SemanticExecutionPlan] = None
    plan_source = "semantic_execution_plan"
    legacy_graph_ignored = False
    try:
        # PRD 2026-05-14 §SIPE: el bridge SIEMPRE pasa por el
        # Semantic Intent Promotion Engine, que:
        #   1. Asegura que el ICE corrió (apply_collapse_to_mission).
        #   2. Construye SEP con tipos genéricos
        #      (open_site/search_site/web_search en lugar de
        #      open_url/search_youtube/search_web).
        #   3. Recalcula intent_status / not_ready_reasons /
        #      ready_provenance vía attach_intent_status_to_plan.
        #   4. Marca compiled_execution_graph como debug-only.
        # Si la misión ya tiene un SEP "promovido" persistido y SIPE no
        # detecta cambios, la operación es idempotente.
        _PROMOTED_SEP_SOURCES = frozenset(
            {"semantic_intent_promotion_engine", "tel_sep_promoter_primary"},
        )
        already_promoted = (
            isinstance(mission.semantic_execution_plan, dict)
            and mission.semantic_execution_plan.get("source") in _PROMOTED_SEP_SOURCES
            and bool(mission.semantic_execution_plan.get("steps"))
        )
        if already_promoted:
            # Reusamos el blob promovido sin re-correr SIPE para no
            # recalcular en cada run.
            plan = build_semantic_execution_plan(mission, use_existing=True)
            sipe_metrics.record_already_promoted()
            decision.sipe_path = "already_promoted"
            decision.sipe_promotions_count = 0
        else:
            sipe_result = apply_promotion_to_mission(mission)
            plan = sipe_result.plan
            promotions_count = len(sipe_result.promotions)
            sipe_metrics.record_promoted_at_runtime(
                promotions_count=promotions_count,
            )
            decision.sipe_path = "promoted_at_runtime"
            decision.sipe_promotions_count = promotions_count
        legacy_graph_ignored = True
        # Defensivo: aunque apply_promotion_to_mission ya lo hace,
        # también lo fijamos aquí por si el caller pasó use_existing.
        try:
            mission.legacy_compiled_graph_purpose = "legacy_debug_only"
        except Exception:
            pass
    except Exception as e:
        log.warning(
            f"[SmartBridge] semantic plan construction falló: {e}; "
            "intentando build_semantic_execution_plan clásico."
        )
        try:
            plan = build_semantic_execution_plan(mission, use_existing=True)
            if not has_semantic_plan(mission):
                try:
                    attach_semantic_plan_to_mission(mission, plan=plan)
                except Exception as inner:
                    log.debug(
                        f"[SmartBridge] persistir plan fallback falló: {inner}"
                    )
            legacy_graph_ignored = True
            try:
                mission.legacy_compiled_graph_purpose = "legacy_debug_only"
            except Exception:
                pass
            sipe_metrics.record_legacy_fallback(reason="sipe_exception")
            decision.sipe_path = "legacy_fallback"
        except Exception as e2:
            log.warning(
                f"[SmartBridge] build_semantic_execution_plan también "
                f"falló: {e2}; cayendo a semantic_adapter."
            )
            plan = None
            sipe_metrics.record_failure(
                reason="sipe_and_classic_failed",
            )
            decision.sipe_path = "failure"

    try:
        from app.services.missions.tel_sep_promoter import (
            run_tel_sep_hybrid_promotion,
        )

        run_tel_sep_hybrid_promotion(mission, get_settings())
        blob_rf = getattr(mission, "semantic_execution_plan", None)
        if isinstance(blob_rf, dict) and blob_rf.get("steps"):
            plan = SemanticExecutionPlan.from_dict(blob_rf)
    except Exception as tel_promo_e:
        log.debug("[SmartBridge] tel_sep_hybrid_promotion: %s", tel_promo_e)

    # ── Cinturón de seguridad PRD 2026-05-07 follow-up.
    #
    # Si la misión TIENE un plan semántico utilizable para routing
    # (``has_valid_semantic_execution_plan == True``) pero la
    # validación estricta encuentra contaminación legacy adentro
    # (``click``/``mouse_scroll``/dicts vacíos/no-dicts), NUNCA caemos
    # a ``compiled_execution_graph``. El gate ya debería haberlo
    # bloqueado, pero protegemos por si algún caller bypasea
    # ``check_mission_approvable``.
    #
    # Importante: este cinturón NO dispara cuando no hay plan en
    # absoluto — eso lo maneja el flujo normal ICE → build → adapter.
    try:
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
            validate_semantic_execution_plan_strict,
        )
        plan_in_routing_path = has_valid_semantic_execution_plan(mission)
        strict_result = (
            validate_semantic_execution_plan_strict(mission)
            if plan_in_routing_path else None
        )
    except Exception:
        strict_result = None
        plan_in_routing_path = False

    if (
        plan_in_routing_path
        and strict_result is not None
        and not strict_result.is_valid
    ):
        decision.blocked = True
        decision.blockers = [
            AdaptedBlocker(
                code=v.code,
                message=v.message,
                step_index=v.step_index if v.step_index >= 0 else None,
                step_type=None,
            )
            for v in strict_result.violations
        ]
        decision.reason = "semantic_plan_contaminated"
        decision.source = plan_source
        decision.legacy_graph_ignored = True
        decision.semantic_plan = plan
        if plan is not None:
            decision.semantic_step_kinds = list(plan.kinds)
        _announce_blocked(on_status, decision.blockers)
        decision.finished_at = time.time()
        log.warning(
            "[SmartBridge] plan semántico contaminado — NO caemos a "
            "legacy. mission_id="
            f"{getattr(mission, 'id', '?')} "
            f"violations={[v.code for v in strict_result.violations]}"
        )
        return decision

    steps: List[MissionStep] = []
    blockers: List[AdaptedBlocker] = []

    if plan is not None and plan.steps:
        # Bloquear si algún step del plan requiere confirmación humana
        # (perfil ambiguo, query vacío, url vacía, etc.). Emitimos
        # tanto el código legacy (``empty_profile``…) como el
        # READY-blocker canónico (``EMPTY_PROFILE_NAME_IN_SELECT_PROFILE``…)
        # para que UIs antiguas y nuevas vean el bloqueo en su
        # propio idioma.
        for sp in plan.steps:
            if sp.needs_user_label:
                blockers.extend(_emit_blockers_for_plan_step(sp))
        if blockers:
            decision.blocked = True
            decision.blockers = blockers
            decision.reason = "semantic_plan_blockers"
            decision.source = plan_source
            decision.legacy_graph_ignored = legacy_graph_ignored
            decision.semantic_plan = plan
            decision.semantic_step_kinds = list(plan.kinds)
            _announce_blocked(on_status, blockers)
            decision.finished_at = time.time()
            log.info(
                "[SmartBridge] misión bloqueada por semantic_plan "
                f"mission_id={getattr(mission, 'id', '?')} "
                f"blockers={[b.code for b in blockers]}"
            )
            return decision

        steps = semantic_plan_to_mission_steps(plan)
        try:
            from app.services.missions.strategy_packs import apply_strategy_packs

            steps = apply_strategy_packs(steps, mission)
        except Exception:
            pass
        decision.source = plan_source
        decision.legacy_graph_ignored = legacy_graph_ignored
        decision.semantic_plan = plan
        decision.semantic_step_kinds = list(plan.kinds)
        try:
            from app.services.telemetry.uic_metrics import uic_metrics
            for st in steps:
                uic_metrics.record_capability_used(st.kind)
                ps = (st.params or {}).get("preferred_strategy")
                if ps:
                    uic_metrics.record_strategy_used(str(ps))
        except Exception:
            pass
    else:
        # FASE 0.2 — CEG nunca ejecuta si SEP existe en la misión.
        try:
            from app.services.missions.semantic_execution_plan import (
                has_valid_semantic_execution_plan,
            )

            if has_valid_semantic_execution_plan(mission):
                decision.blocked = True
                decision.blockers = [
                    AdaptedBlocker(
                        code="SEP_LEGACY_GRAPH_FORBIDDEN",
                        message=(
                            "La misión tiene semantic_execution_plan; "
                            "compiled_execution_graph es debug-only y no puede ejecutar."
                        ),
                    ),
                ]
                decision.reason = "sep_present_legacy_graph_forbidden"
                decision.source = plan_source or "semantic_execution_plan"
                decision.legacy_graph_ignored = True
                _announce_blocked(on_status, decision.blockers)
                decision.finished_at = time.time()
                log.warning(
                    "[SmartBridge] SEP presente — CEG prohibido. mission_id="
                    f"{getattr(mission, 'id', '?')}"
                )
                return decision
        except Exception as _sep_ceg_exc:
            log.debug("[SmartBridge] sep/ceg guard: %s", _sep_ceg_exc)

        # Fallback puro de compat: adapter clásico sobre
        # compiled_execution_graph. Sólo entra cuando el ICE
        # no pudo emitir ningún paso (misión muy degradada).
        log.info(
            "[SmartBridge] semantic_plan vacío; usando "
            "compiled_execution_graph (adapter clásico)."
        )
        adapted = adapt_mission_to_steps(
            mission,
            mission_id=getattr(mission, "id", None),
        )
        decision.adapted = adapted
        decision.source = "compiled_execution_graph"
        decision.legacy_graph_ignored = False
        if adapted.blockers:
            decision.blocked = True
            decision.blockers = list(adapted.blockers)
            decision.reason = "adapter_blockers"
            _announce_blocked(on_status, adapted.blockers)
            decision.finished_at = time.time()
            log.info(
                "[SmartBridge] misión bloqueada por adapter "
                f"mission_id={getattr(mission, 'id', '?')} "
                f"blockers={[b.code for b in adapted.blockers]}"
            )
            return decision
        if not adapted.steps:
            decision.blocked = True
            decision.reason = "no_adaptable_steps"
            decision.blockers = [AdaptedBlocker(
                code="empty_steps",
                message=(
                    "No hay pasos semánticos adaptables en esta misión. "
                    "Recompílala o revisa el Mission Review."
                ),
            )]
            _announce_blocked(on_status, decision.blockers)
            decision.finished_at = time.time()
            return decision
        steps = list(adapted.steps)
        decision.semantic_step_kinds = [s.kind for s in steps]

    decision.player_used = "smart"
    run_id = f"smart-{getattr(mission, 'id', 'm')}-{uuid.uuid4().hex[:8]}"
    mission_id = getattr(mission, "id", None)
    resume_i = max(0, int(semantic_start_step_index or 0))
    if steps:
        resume_i = max(0, min(resume_i, len(steps) - 1))
    else:
        resume_i = 0
    if resume_i > 0 and steps:
        log.info(
            f"[SmartBridge] semantic_resume start_step_index={resume_i} "
            f"steps_total={len(steps)} mission_id={mission_id}"
        )

    _oss = get_settings()
    _lat_collector = None
    if getattr(_oss, "RUNTIME_LATENCY_REPORT_ENABLED", False):
        from app.services.runtime.runtime_latency_budget import RuntimeLatencyCollector

        _lat_collector = RuntimeLatencyCollector()
    cob_should_run = bool(
        getattr(_oss, "COB_EXECUTION_PILOT_ENABLED", False)
        and resume_i == 0
        and (
            not getattr(_oss, "ONE_SHOT_RELIABILITY_ENABLED", False)
            or getattr(_oss, "ONE_SHOT_ALLOW_COB_PILOT", False)
        ),
    )

    use_al = bool(use_auto_learning) and _AUTO_LEARNING_AVAILABLE
    decision.auto_learning_enabled = use_al

    player_label = "AutoLearningExecutor" if use_al else "SmartMissionExecutor"
    kinds_repr = "[" + ", ".join(decision.semantic_step_kinds) + "]"
    log.info(
        f"[SmartBridge] player={player_label} "
        f"source={decision.source} "
        f"steps={kinds_repr} "
        f"legacy_graph_ignored={str(decision.legacy_graph_ignored).lower()} "
        f"coords_used={str(decision.coords_used).lower()} "
        f"mission_id={mission_id} step_count={len(steps)}"
    )

    if getattr(_oss, "ONE_SHOT_RELIABILITY_ENABLED", False) and steps:
        if _lat_collector is not None:
            _lat_collector.mark_start("one_shot_preflight")
        try:
            from app.services.runtime.one_shot_reliability_orchestrator import (
                OneShotPreflightOutcome,
                attach_live_perception,
                build_one_shot_audit,
                choose_plan_source,
                compute_one_shot_readiness,
                prepare_mission_for_reliable_execution,
                select_runtime_mode,
            )

            prepare_mission_for_reliable_execution(mission, _oss)
            attach_live_perception(mission, _oss)
            readiness = compute_one_shot_readiness(
                mission,
                _oss,
                bridge_source=str(decision.source or ""),
                steps_count=len(steps),
            )
            plan_src = choose_plan_source(mission, _oss)
            cob_should_run = bool(
                getattr(_oss, "COB_EXECUTION_PILOT_ENABLED", False)
                and resume_i == 0
                and getattr(_oss, "ONE_SHOT_ALLOW_COB_PILOT", False),
            )
            rt_mode = select_runtime_mode(
                mission,
                _oss,
                plan_source=plan_src,
                cob_pilot_will_run=cob_should_run,
                bridge_used_legacy=False,
            )
            mission.one_shot_reliability_report = build_one_shot_audit(
                mission,
                _oss,
                readiness=readiness,
                plan_source=plan_src,
                runtime_mode=rt_mode,
                bridge_source=str(decision.source or ""),
                cob_pilot_will_run=cob_should_run,
            )
            if getattr(_oss, "ONE_SHOT_PREFLIGHT_REQUIRED", True):
                if readiness.outcome not in (
                    OneShotPreflightOutcome.READY_TO_EXECUTE,
                    OneShotPreflightOutcome.READY_WITH_WARNINGS,
                ):
                    decision.blocked = True
                    decision.reason = f"one_shot_preflight:{readiness.outcome.value}"
                    msg = (
                        readiness.warnings[0]
                        if readiness.warnings
                        else (
                            readiness.reasons[0]
                            if readiness.reasons
                            else readiness.outcome.value
                        )
                    )
                    decision.blockers = [
                        AdaptedBlocker(
                            code="one_shot_preflight",
                            message=f"No se ejecuta (confiabilidad one-shot): {msg}",
                            step_index=None,
                            step_type=None,
                        ),
                    ]
                    _announce_blocked(on_status, decision.blockers)
                    decision.finished_at = time.time()
                    return decision
        except Exception as _ose:
            log.warning("[SmartBridge] one-shot preflight omitido: %s", _ose)
        finally:
            if _lat_collector is not None:
                _lat_collector.mark_end("one_shot_preflight")

    if getattr(_oss, "UOOL_ENABLED", False) and steps:
        if _lat_collector is not None:
            _lat_collector.mark_start("uool_preflight")
        try:
            from app.services.runtime.unified_operational_orchestrator import (
                prepare_uool_for_execution,
            )

            uool_pf = prepare_uool_for_execution(mission, _oss)
            if getattr(_oss, "UOOL_PREFLIGHT_REQUIRED", False) and not uool_pf.get("ok", True):
                decision.blocked = True
                decision.reason = f"uool_preflight:{uool_pf.get('blocked', 'blocked')}"
                msg = str(uool_pf.get("blocked") or "uool_preflight_blocked")
                decision.blockers = [
                    AdaptedBlocker(
                        code="uool_preflight",
                        message=f"No se ejecuta (orquestación operacional): {msg}",
                        step_index=None,
                        step_type=None,
                    ),
                ]
                _announce_blocked(on_status, decision.blockers)
                decision.finished_at = time.time()
                return decision
        except Exception as _uool_err:
            log.warning("[SmartBridge] UOOL preflight omitido: %s", _uool_err)
        finally:
            if _lat_collector is not None:
                _lat_collector.mark_end("uool_preflight")

    if getattr(_oss, "OCRP_ENABLED", False) and steps:
        if _lat_collector is not None:
            _lat_collector.mark_start("ocrp_preflight")
        try:
            from app.services.runtime.operational_convergence_program import (
                prepare_ocrp_for_execution,
            )

            ocrp_pf = prepare_ocrp_for_execution(mission, _oss)
            if getattr(_oss, "OCRP_BLOCK_UNSTABLE_MISSIONS", False) and not ocrp_pf.get("ok", True):
                decision.blocked = True
                decision.reason = f"ocrp_preflight:{ocrp_pf.get('blocked', 'blocked')}"
                decision.blockers = [
                    AdaptedBlocker(
                        code="ocrp_preflight",
                        message=(
                            "No se ejecuta (convergencia operacional insuficiente): "
                            f"{ocrp_pf.get('blocked', 'unstable')}"
                        ),
                        step_index=None,
                        step_type=None,
                    ),
                ]
                _announce_blocked(on_status, decision.blockers)
                decision.finished_at = time.time()
                return decision
        except Exception as _ocrp_err:
            log.warning("[SmartBridge] OCRP preflight omitido: %s", _ocrp_err)
        finally:
            if _lat_collector is not None:
                _lat_collector.mark_end("ocrp_preflight")

    cob_pilot_info = None
    if steps:
        try:
            if cob_should_run:
                from app.services.missions.cob_execution_pilot import (
                    cob_pilot_try_prefix_or_fallback,
                )

                cob_pilot_info = cob_pilot_try_prefix_or_fallback(
                    mission,
                    semantic_step_count=len(steps),
                    start_step_index=resume_i,
                    on_status=on_status,
                )
        except Exception as e:
            log.warning("[SmartBridge] COB Execution Pilot omitido: %s", e)
            cob_pilot_info = None

        if cob_pilot_info is not None:
            aud = cob_pilot_info.get("audit_final")
            if isinstance(aud, dict):
                mission.cob_execution_pilot_audit = aud
            if cob_pilot_info.get("completed_all_via_pilot") and cob_pilot_info.get(
                "mission_result",
            ):
                decision.reason = "cob_execution_pilot_completed"
                decision.result = cob_pilot_info["mission_result"]
                decision.finished_at = time.time()
                if on_status:
                    try:
                        on_status("Listo.")
                    except Exception:
                        pass
                return decision

            nx = cob_pilot_info.get("next_sep_step_index")
            if nx is not None:
                nx = int(max(0, nx))
                if nx >= len(steps):
                    mr = cob_pilot_info.get("mission_result")
                    if mr:
                        decision.result = mr
                        decision.reason = "cob_execution_pilot_completed_edge"
                        decision.finished_at = time.time()
                        return decision
                else:
                    resume_i = nx
            if resume_i > 0 and steps:
                log.info(
                    f"[SmartBridge] cob_pilot resume SEP at index={resume_i} "
                    f"steps_total={len(steps)} mission_id={mission_id}"
                )

    if steps:
        try:
            from app.services.runtime.one_shot_reliability_orchestrator import (
                run_execution_with_reliability_guards,
            )

            on_step_started, on_step_done = run_execution_with_reliability_guards(
                mission,
                _oss,
                on_step_started=on_step_started,
                on_step_done=on_step_done,
            )
        except Exception as _osg:
            log.debug("[SmartBridge] one_shot guards omitidos: %s", _osg)
        try:
            from app.services.runtime.lop_sensor_bridge import augment_smart_runner_callbacks_for_lop_shadow

            on_step_started, on_step_done = augment_smart_runner_callbacks_for_lop_shadow(
                mission,
                get_settings(),
                on_step_started,
                on_step_done,
            )
        except Exception as _lop_wrap_err:
            log.debug("[SmartBridge] LOP runner shadow augment omitido: %s", _lop_wrap_err)

    if getattr(_oss, "PRODUCTION_TELEMETRY_ENABLED", False) and steps:
        try:
            from app.services.runtime.runtime_production_telemetry import record_runtime_event

            record_runtime_event(
                mission,
                kind="smart_run_armed",
                payload={"run_id": run_id, "step_count": len(steps)},
            )
        except Exception:
            pass

    if use_al:
        decision.reason = "auto_learning_executor"
        try:
            executor = AutoLearningExecutor(  # type: ignore[misc]
                mission_id=mission_id,
                run_id=run_id,
                on_status=on_status,
                on_human_help_needed=on_human_help_needed,
                on_step_done=on_step_done,
                on_step_started=on_step_started,
                on_human_correction=on_human_correction,
                on_repair_suggestion=on_repair_suggestion,
                expert_mode=expert_mode,
                private_mode=private_mode,
                mission_ref=mission,
            )
            if _lat_collector is not None:
                _lat_collector.mark_start("smart_executor_run")
            try:
                al_result = executor.run_mission(
                    steps, start_step_index=resume_i,
                )
            finally:
                if _lat_collector is not None:
                    _lat_collector.mark_end("smart_executor_run")
        except Exception as e:
            log.exception(f"AutoLearningExecutor exception: {e}")
            decision.blocked = True
            decision.reason = f"auto_learning_exception: {e}"
            decision.finished_at = time.time()
            return decision
        decision.result = al_result.mission_result
        decision.quality_report = al_result.quality_report
        decision.suggestions = list(al_result.suggestions or [])
    else:
        # Camino legacy explícito (use_auto_learning=False) o cuando el
        # paquete auto-learning no está disponible.
        decision.reason = (
            "smart_executor_no_auto_learning"
            if not use_auto_learning
            else "smart_executor_auto_learning_unavailable"
        )
        executor = SmartMissionExecutor(
            run_id=run_id,
            on_status=on_status,
            on_human_help_needed=on_human_help_needed,
            on_step_done=on_step_done,
            on_step_started=on_step_started,
            expert_mode=expert_mode,
            mission_ref=mission,
        )
        try:
            if _lat_collector is not None:
                _lat_collector.mark_start("smart_executor_run")
            try:
                result = executor.run_mission(steps, start_step_index=resume_i)
            finally:
                if _lat_collector is not None:
                    _lat_collector.mark_end("smart_executor_run")
        except Exception as e:
            log.exception(f"SmartMissionExecutor exception: {e}")
            decision.blocked = True
            decision.reason = f"smart_exception: {e}"
            decision.finished_at = time.time()
            return decision
        decision.result = result

    # Detectar coords_used post-ejecución: si alguna estrategia
    # ``*coords*`` o ``*visual*`` aparece en los outcomes, lo dejamos
    # registrado (emergencia explícita).
    if decision.result is not None:
        try:
            for o in decision.result.outcomes:
                strat = (getattr(o, "strategy_used", "") or "").lower()
                if any(h in strat for h in (
                    "coords", "visual_asset", "vision_coords",
                )):
                    decision.coords_used = True
                    break
        except Exception:
            pass

    if getattr(_oss, "ONE_SHOT_RELIABILITY_ENABLED", False) and decision.result is not None:
        try:
            from app.services.runtime.one_shot_reliability_orchestrator import record_runtime_learning

            learn = record_runtime_learning(
                mission,
                _oss,
                steps=steps,
                mission_result=decision.result,
            )
            rep = getattr(mission, "one_shot_reliability_report", None)
            if isinstance(rep, dict):
                rep["post_execution_learning"] = learn
                rep["coords_used_runtime"] = decision.coords_used
                rep["execution_audit_complete"] = True
        except Exception as _oru:
            log.debug("[SmartBridge] one-shot post-run: %s", _oru)

    if getattr(_oss, "OCRP_ENABLED", False) and decision.result is not None:
        try:
            from app.services.runtime.operational_convergence_program import (
                record_post_run_convergence,
            )

            step_outcomes = [
                str(getattr(o, "status", "") or "")
                for o in (getattr(decision.result, "outcomes", None) or [])
            ]
            ocrp_post = record_post_run_convergence(
                mission,
                _oss,
                run_id=run_id,
                status=str(getattr(decision.result, "status", "") or ""),
                duration_ms=float(getattr(decision.result, "duration_ms", 0) or 0),
                step_outcomes=step_outcomes,
            )
            rep = getattr(mission, "ocrp_repeatability_report", None)
            if isinstance(rep, dict):
                rep["post_run"] = ocrp_post
        except Exception as _ocrp_post:
            log.debug("[SmartBridge] OCRP post-run: %s", _ocrp_post)

    try:
        if getattr(_oss, "RUNTIME_LATENCY_REPORT_ENABLED", False) and _lat_collector is not None:
            from app.services.runtime.runtime_latency_budget import build_runtime_latency_report

            mission.runtime_latency_report = build_runtime_latency_report(
                _lat_collector,
                semantic_step_count=len(steps) if steps else None,
            )
        if getattr(_oss, "RELIABILITY_HARDENING_ENABLED", False):
            from app.services.missions.mission_health_score import compute_mission_health_score

            compute_mission_health_score(mission, persist=True)
            if getattr(_oss, "BETA_PRIVATE_RELIABILITY_PROFILE", False):
                try:
                    from app.services.runtime.beta_private_acceptance_score import (
                        compute_beta_private_acceptance_score,
                    )

                    mission.beta_private_acceptance_snapshot = compute_beta_private_acceptance_score(
                        mission,
                        mission_result=decision.result,
                    )
                except Exception as _bpa:
                    log.debug("[SmartBridge] beta acceptance omitido: %s", _bpa)
            try:
                from app.services.runtime.beta_runtime_acceptance import (
                    build_and_persist_beta_runtime_acceptance,
                )

                build_and_persist_beta_runtime_acceptance(
                    mission,
                    run_id=run_id,
                    mission_result=decision.result,
                )
            except Exception as _bra:
                log.debug("[SmartBridge] beta runtime acceptance: %s", _bra)
        if getattr(_oss, "PRODUCTION_TELEMETRY_ENABLED", False):
            from app.services.runtime.runtime_production_telemetry import record_runtime_event

            st = getattr(decision.result, "status", None) if decision.result else None
            record_runtime_event(
                mission,
                kind="smart_run_complete",
                payload={
                    "reason": decision.reason,
                    "result_status": st,
                    "coords_used": decision.coords_used,
                },
            )
    except Exception as _hard_exc:
        log.debug("[SmartBridge] reliability hardening finalize: %s", _hard_exc)

    decision.finished_at = time.time()

    # FASE 0.5 — telemetría obligatoria por run (audit JSONL + estrategia/paso).
    try:
        from app.services.runtime.execution_run_audit import record_execution_run_audit

        record_execution_run_audit(
            mission=mission,
            decision=decision,
            settings=_oss,
        )
    except Exception as _era_exc:
        log.debug("[SmartBridge] execution_run_audit: %s", _era_exc)

    # Anuncio final coherente con el PRD §13.
    final_result = decision.result
    if on_status and final_result is not None:
        try:
            if final_result.status == "success":
                # Si tenemos quality_report, usamos el resumen humano.
                if decision.quality_report is not None:
                    summary = getattr(
                        decision.quality_report, "user_facing_summary",
                        None,
                    )
                    on_status(summary or "Listo.")
                else:
                    on_status("Listo.")
            elif final_result.status == "stopped_for_human":
                on_status(
                    final_result.last_message
                    or "Necesito ayuda para continuar."
                )
            else:
                on_status(final_result.last_message or "La ejecución se detuvo.")
        except Exception:
            pass
    return decision


# ─── Helpers ──────────────────────────────────────────────────────────


def _announce_blocked(
    on_status: Optional[StatusCb],
    blockers: List[AdaptedBlocker],
) -> None:
    if not on_status:
        return
    if not blockers:
        try:
            on_status("No puedo ejecutar esta misión: revísala antes.")
        except Exception:
            pass
        return
    first = blockers[0]
    try:
        on_status(first.message)
    except Exception:
        pass


def _blocker_code_for_plan_step(sp: SemanticPlanStep) -> str:
    """Mapea un step del plan que necesita confirmación a un blocker code.

    Códigos alineados con ``approval_gate._HUMAN_REVIEW_CODES`` para que
    la UI los muestre en el mismo lenguaje que las violaciones del gate.

    Mantenemos la semántica histórica (snake_case): la UI que sigue
    leyendo ``empty_profile``/``empty_query``/``empty_url``/``empty_app``
    no se rompe. El código canónico READY-blocker se emite en
    paralelo como un AdaptedBlocker adicional (ver
    :func:`_ready_blocker_code_for_plan_step` y
    :func:`_collect_plan_blockers`).
    """
    if sp.type == "select_profile":
        return "empty_profile"
    if sp.type == "open_url":
        return "empty_url"
    if sp.type in ("search_youtube", "search_content", "search_site"):
        return "empty_query"
    if sp.type == "open_app":
        return "empty_app"
    return "needs_user_label"


def _ready_blocker_code_for_plan_step(sp: SemanticPlanStep) -> Optional[str]:
    """Devuelve el código READY-blocker canónico para un step.

    Es el código uppercase que aparece en :data:`READY_BLOCKER_CODES`
    y que la UI nueva usa para clasificar el tipo de confirmación
    mínima a mostrar (sin perder los códigos legacy).
    """
    if sp.type == "select_profile":
        candidate = (sp.params.get("profile_name")
                     or sp.params.get("profile") or "").strip()
        if candidate:
            return "PROFILE_RESOLUTION_REQUIRED"
        return "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE"
    if sp.type == "open_url":
        return "EMPTY_URL_IN_OPEN_URL"
    if sp.type in ("search_youtube", "search_content", "search_site"):
        if sp.params.get("query"):
            fid = str(sp.params.get("query_fidelity_status") or "ok")
            if fid == "missing_short_token":
                return "QUERY_MISSING_SHORT_TOKEN"
            if fid not in ("ok", ""):
                return "QUERY_FIDELITY_SUSPECT"
        return "EMPTY_QUERY_IN_SEARCH"
    if sp.type == "open_app":
        return "EMPTY_APP_IN_OPEN_APP"
    return "NEEDS_USER_LABEL_PENDING"


def _emit_blockers_for_plan_step(
    sp: SemanticPlanStep,
) -> List[AdaptedBlocker]:
    """Genera la lista canónica de blockers (legacy + READY) para un step.

    Backward-compat: emitimos PRIMERO el código snake_case legacy
    (``empty_profile``…) para que UIs antiguas sigan reconociéndolo,
    seguido del código uppercase READY-blocker (``EMPTY_PROFILE_NAME_IN_SELECT_PROFILE``…)
    que la UI nueva usa para discriminar el tipo de confirmación.
    """
    out: List[AdaptedBlocker] = []
    legacy_code = _blocker_code_for_plan_step(sp)
    out.append(AdaptedBlocker(
        code=legacy_code,
        message=sp.label_prompt or (
            f"Paso '{sp.type}' necesita confirmación humana."
        ),
        step_index=None,
        step_type=sp.type,
    ))
    ready_code = _ready_blocker_code_for_plan_step(sp)
    if ready_code and ready_code != legacy_code:
        out.append(AdaptedBlocker(
            code=ready_code,
            message=sp.label_prompt or (
                f"Paso '{sp.type}': bloqueante READY ({ready_code})."
            ),
            step_index=None,
            step_type=sp.type,
        ))
    return out


def steps_to_dicts(steps: List[MissionStep]) -> List[Dict[str, Any]]:
    """Serializa ``List[MissionStep]`` a dict plano (útil en tests)."""
    return [
        {
            "id": s.id,
            "kind": s.kind,
            "params": dict(s.params or {}),
            "block_id": s.block_id,
            "human_label": s.human_label,
        }
        for s in steps
    ]


__all__ = [
    "RunnerDecision",
    "decide_player",
    "run_mission_smart",
    "steps_to_dicts",
]
