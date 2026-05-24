"""
ArthurOS Services - AutoLearningExecutor (Auto-Learning Executor)
-----------------------------------------------------------------
Wrapper sobre ``SmartMissionExecutor`` que añade el bucle completo de
auto-aprendizaje:

  - ExecutionMemoryStore     (memoria estructurada: SQLite + JSONL)
  - StrategyScoringEngine    (rank por aprendizaje real, con decay)
  - TargetMemory             (consulta y aprende por target/domain/app)
  - MissionFailureClassifier (12 categorías + recovery recomendada)
  - AdaptiveTimeouts         (p95+margen por condition)
  - MachineProfile           (reliability por técnica de la máquina)
  - ExecutionQualityCalculator (score de calidad post-mortem)
  - MissionGraphRepair       (sugerencias de preferred_strategy)
  - HumanCorrectionLearning  (preguntar 1 vez, recordar siempre)

Filosofía:
  * No reescribimos `SmartMissionExecutor`. Lo COMPONEMOS.
  * Hookeamos los tres puntos clave usando los callbacks ya existentes
    (``on_step_done``, ``on_human_help_needed``, ``on_status``) +
    ``LearningLogger`` (que el SmartMissionExecutor ya invoca).
  * Antes de ejecutar la misión, ajustamos las prioridades preferidas en
    `LearningLogger` para que ``rank_strategies`` ya refleje el
    aprendizaje contextual. Esto es **no invasivo**: no tocamos el
    código del executor.

Comportamiento:
  * `run_mission(...)` ejecuta `SmartMissionExecutor.run_mission` y
    paralelamente:
      - inicia un run en ExecutionMemoryStore
      - registra cada `StepOutcome` en el store con clasificación de fallo
      - actualiza TargetMemory cuando hay éxito
      - registra observación adaptativa de timeout
      - al final calcula ExecutionQualityReport y la añade al run
      - emite sugerencias de MissionGraphRepair (si aplica)

Resultado: un objeto enriquecido `AutoLearningResult` con `mission_result`
+ `quality_report` + `suggestions`.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.core.logger import log
from app.services.missions.action_runner import ActionRunner
from app.services.missions.adaptive_timeouts import (
    AdaptiveTimeouts,
    get_adaptive_timeouts,
)
from app.services.missions.checkpoints import CheckpointManager
from app.services.missions.execution_contracts import MissionStep, build_contract
from app.services.missions.execution_learning import (
    LearningLogger,
    get_learning_logger,
)
from app.services.missions.execution_memory_store import (
    ExecutionMemoryStore,
    StepAttemptRecord,
    get_execution_memory_store,
)
from app.services.missions.execution_quality import (
    ExecutionQualityCalculator,
    ExecutionQualityReport,
    get_execution_quality_calculator,
)
from app.services.missions.failure_classifier import (
    MissionFailureClassifier,
    MissionFailureType,
    RecoveryRecommendation,
    get_failure_classifier,
)
from app.services.missions.machine_profile import (
    MachineProfile,
    get_machine_profile,
)
from app.services.missions.mission_graph_repair import (
    MissionGraphRepair,
    MissionGraphRepairSuggestion,
    get_mission_graph_repair,
)
from app.services.missions.smart_executor import (
    HumanHelpCb,
    MissionResult,
    ProgressCb,
    SmartMissionExecutor,
    StatusCb,
    StepOutcome,
)
from app.services.missions.state_detector import StateDetector
from app.services.missions.strategy_scoring import (
    StrategyScoringEngine,
    get_strategy_scoring_engine,
)
from app.services.missions.target_memory import (
    TargetMemory,
    get_target_memory,
)


# ──────────────────────────────────────────────────────────────────────
# Resultado enriquecido
# ──────────────────────────────────────────────────────────────────────

@dataclass
class AutoLearningResult:
    mission_result: MissionResult
    quality_report: Optional[ExecutionQualityReport] = None
    suggestions: List[MissionGraphRepairSuggestion] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.mission_result.run_id

    @property
    def status(self) -> str:
        return self.mission_result.status

    @property
    def quality_score(self) -> Optional[float]:
        return self.quality_report.quality_score if self.quality_report else None


# ──────────────────────────────────────────────────────────────────────
# Hook de "human correction" (callback que el caller puede inyectar)
# ──────────────────────────────────────────────────────────────────────

# Firma: (StepOutcome) -> Dict | None
# Si el callback devuelve un dict con la corrección humana, AutoLearning
# la persiste para reutilizarla en próximas ejecuciones.
HumanCorrectionCb = Callable[[StepOutcome], Optional[Dict[str, Any]]]


# ──────────────────────────────────────────────────────────────────────
# Executor
# ──────────────────────────────────────────────────────────────────────

class AutoLearningExecutor:
    """SmartMissionExecutor + memoria estructurada + aprendizaje contextual.

    Uso:

        executor = AutoLearningExecutor(mission_id="my_mission")
        result = executor.run_mission(steps)
        print(result.quality_report.user_facing_summary)

    Parametros notables:
      * ``private_mode``: si True, no se persiste nada (override).
      * ``on_human_correction``: callback opcional para capturar la
        corrección humana después de un ``needs_human``.
      * ``on_repair_suggestion``: callback opcional para confirmar al
        usuario una sugerencia de cambio de preferred_strategy.
    """

    def __init__(
        self,
        *,
        mission_id: Optional[str] = None,
        run_id: Optional[str] = None,
        state_detector: Optional[StateDetector] = None,
        action_runner: Optional[ActionRunner] = None,
        checkpoints: Optional[CheckpointManager] = None,
        learning: Optional[LearningLogger] = None,
        memory_store: Optional[ExecutionMemoryStore] = None,
        scoring: Optional[StrategyScoringEngine] = None,
        targets: Optional[TargetMemory] = None,
        classifier: Optional[MissionFailureClassifier] = None,
        timeouts: Optional[AdaptiveTimeouts] = None,
        machine: Optional[MachineProfile] = None,
        quality: Optional[ExecutionQualityCalculator] = None,
        repair: Optional[MissionGraphRepair] = None,
        on_status: Optional[StatusCb] = None,
        on_human_help_needed: Optional[HumanHelpCb] = None,
        on_step_done: Optional[ProgressCb] = None,
        on_step_started: Optional[Callable[[Any], None]] = None,
        on_human_correction: Optional[HumanCorrectionCb] = None,
        on_repair_suggestion: Optional[Callable[[MissionGraphRepairSuggestion], str]] = None,
        expert_mode: bool = False,
        private_mode: Optional[bool] = None,
        mission_ref: Optional[Any] = None,
    ) -> None:
        self.mission_id = mission_id
        self.run_id = run_id or str(uuid.uuid4())
        self.expert_mode = bool(expert_mode)

        # ── Resolución del store + learning logger ─────────────────
        # Crítico para modo privado: NO podemos invocar a
        # ``get_*_*()`` (singletons globales) porque ellos materializan
        # el ``ExecutionMemoryStore`` de var/missions/learning/. En modo
        # privado, todos los componentes deben colgar de un store
        # privado en RAM.
        if private_mode is True:
            self.store = ExecutionMemoryStore(private_mode=True)
            # LearningLogger efímero: el SmartMissionExecutor interno
            # llamará a record_success/failure y el singleton global
            # escribiría a var/missions/learning/*.jsonl. Lo aislamos.
            self.learning = _make_ephemeral_learning_logger()
        else:
            self.store = memory_store or get_execution_memory_store()
            self.learning = learning or get_learning_logger()

        # ── Componentes derivados ──────────────────────────────────
        # Si el caller pasó un store custom o estamos en modo privado,
        # construimos componentes nuevos colgados de ese store. Si no,
        # podemos reutilizar los singletons globales que ya conocen el
        # store global (más eficiente, evita duplicar caches).
        if memory_store is not None or private_mode is True:
            self.scoring = StrategyScoringEngine(
                store=self.store, learning=self.learning,
            )
            self.targets = TargetMemory(store=self.store)
            self.classifier = classifier or MissionFailureClassifier()
            self.timeouts = AdaptiveTimeouts(store=self.store)
            self.machine = MachineProfile(store=self.store)
            self.quality = ExecutionQualityCalculator(
                store=self.store, scoring=self.scoring, targets=self.targets,
            )
            self.repair = MissionGraphRepair(
                store=self.store, scoring=self.scoring,
            )
        else:
            self.scoring = scoring or get_strategy_scoring_engine()
            self.targets = targets or get_target_memory()
            self.classifier = classifier or get_failure_classifier()
            self.timeouts = timeouts or get_adaptive_timeouts()
            self.machine = machine or get_machine_profile()
            self.quality = quality or get_execution_quality_calculator()
            self.repair = repair or get_mission_graph_repair()

        self._smart = SmartMissionExecutor(
            run_id=self.run_id,
            state_detector=state_detector,
            action_runner=action_runner,
            checkpoints=checkpoints,
            learning=self.learning,
            on_status=on_status,
            on_human_help_needed=self._wrap_human_cb(on_human_help_needed, on_human_correction),
            on_step_done=self._wrap_progress_cb(on_step_done),
            on_step_started=on_step_started,
            expert_mode=expert_mode,
            mission_ref=mission_ref,
        )
        self._on_repair_suggestion = on_repair_suggestion
        self._suggestions: List[MissionGraphRepairSuggestion] = []
        # Cache del último step que pasó por on_step_done (para
        # context adicional sobre fallos / corrupciones humanas).
        self._last_outcome: Optional[StepOutcome] = None
        # Cache de "¿qué app/domain pertenece a qué step?" para no
        # tener que reconstruirlo en el callback.
        self._step_context: Dict[str, Dict[str, Any]] = {}

    # ── API pública ────────────────────────────────────────────
    def run_mission(
        self,
        steps: Sequence[MissionStep],
        *,
        start_step_index: int = 0,
    ) -> AutoLearningResult:
        # Pre-flight: store start_run, contextos por step.
        self.store.start_run(
            run_id=self.run_id,
            mission_id=self.mission_id,
            machine_id=self.machine.machine_id,
        )
        for s in steps:
            self._step_context[s.id] = self._extract_context(s)

        result = self._smart.run_mission(
            list(steps), start_step_index=start_step_index,
        )

        # Quality + sugerencias.
        quality_report: Optional[ExecutionQualityReport] = None
        try:
            quality_report = self.quality.compute(self.run_id, run_status=result.status)
        except Exception as e:
            log.debug(f"compute quality falló: {e}")

        suggestions = self._build_suggestions(steps)

        # Cierre del run.
        try:
            self.store.finish_run(
                self.run_id, status=result.status,
                quality_score=(quality_report.quality_score if quality_report else None),
            )
        except Exception as e:
            log.debug(f"finish_run falló: {e}")

        # Para cada sugerencia, si tenemos callback, preguntamos al usuario.
        if self._on_repair_suggestion:
            try:
                from app.core.config import get_settings as _gte

                mr = getattr(self._smart, "mission_ref", None)
                if mr is not None and getattr(_gte(), "PRODUCTION_TELEMETRY_ENABLED", False):
                    from app.services.runtime.runtime_production_telemetry import record_runtime_event

                    record_runtime_event(
                        mr,
                        kind="repair_suggestion_offer",
                        payload={"suggestion_count": len(suggestions)},
                    )
            except Exception as _rso:
                log.debug("repair_offer telemetry omitido: %s", _rso)
            for s in suggestions:
                try:
                    decision = self._on_repair_suggestion(s) or "reject"
                    self.repair.mark_decision(s, str(decision).strip().lower())
                except Exception as e:
                    log.debug(f"on_repair_suggestion callback falló: {e}")

        return AutoLearningResult(
            mission_result=result,
            quality_report=quality_report,
            suggestions=suggestions,
        )

    # ── Hooks ──────────────────────────────────────────────────
    def _wrap_progress_cb(
        self, original: Optional[ProgressCb]
    ) -> ProgressCb:
        def _hook(outcome: StepOutcome) -> None:
            self._last_outcome = outcome
            self._record_outcome(outcome)
            if outcome.status == "failed" or (
                outcome.status == "success" and outcome.retries > 0
            ):
                self._update_target_memory_on_outcome(outcome)
            if outcome.status == "success":
                self._record_timeout_observation(outcome, success=True)
            elif outcome.status == "failed":
                self._record_timeout_observation(outcome, success=False)
            if original:
                try:
                    original(outcome)
                except Exception as e:
                    log.debug(f"on_step_done caller falló: {e}")
        return _hook

    def _wrap_human_cb(
        self,
        original: Optional[HumanHelpCb],
        on_correction: Optional[HumanCorrectionCb],
    ) -> HumanHelpCb:
        def _hook(outcome: StepOutcome) -> None:
            # Antes de pedir humano, intentamos buscar una corrección
            # previa para este (step_kind, semantic_target). Si la hay,
            # la marcamos como "usada"; el caller podría leerla del
            # store y aplicarla. (No mutamos el flujo del SmartExecutor;
            # el dueño de la UX decide qué hacer.)
            ctx = self._step_context.get(outcome.step_id, {})
            try:
                prior = self.store.find_user_correction(
                    step_kind=outcome.kind,
                    semantic_target=ctx.get("semantic_target"),
                    domain=ctx.get("domain"),
                    app=ctx.get("app"),
                )
                if prior:
                    cid = int(prior.get("id") or 0)
                    if cid:
                        self.store.mark_user_correction_used(cid)
            except Exception as e:
                log.debug(f"reuse correction lookup falló: {e}")

            # Captura nueva corrección si el caller provee callback.
            if on_correction is not None:
                try:
                    payload = on_correction(outcome) or None
                    if payload:
                        payload = dict(payload)
                        payload.setdefault("run_id", self.run_id)
                        payload.setdefault("mission_id", self.mission_id)
                        payload.setdefault("step_id", outcome.step_id)
                        payload.setdefault("step_kind", outcome.kind)
                        payload.setdefault("semantic_target", ctx.get("semantic_target"))
                        payload.setdefault("domain", ctx.get("domain"))
                        payload.setdefault("app", ctx.get("app"))
                        self.store.record_user_correction(payload)
                except Exception as e:
                    log.debug(f"on_human_correction callback falló: {e}")

            if original:
                try:
                    original(outcome)
                except Exception as e:
                    log.debug(f"on_human_help_needed caller falló: {e}")
        return _hook

    # ── Memory / records ───────────────────────────────────────
    def _record_outcome(self, outcome: StepOutcome) -> None:
        ctx = self._step_context.get(outcome.step_id, {})
        ok = outcome.status in ("success", "skipped")
        # Clasificar fallo si aplica.
        failure_type: Optional[str] = None
        recommendation: Optional[RecoveryRecommendation] = None
        if not ok:
            try:
                rec = self.classifier.classify(
                    error=outcome.error or outcome.human_message,
                    step_kind=outcome.kind,
                    expected_app=ctx.get("expected_app"),
                    expected_url_substring=ctx.get("expected_url_substring"),
                    state_before=outcome.state_before,
                    state_after=outcome.state_after,
                    prior_success_for_strategy=self._has_prior_success(
                        outcome.kind, outcome.strategy_used or "",
                    ),
                    retries_so_far=outcome.retries,
                    wait_was_long=(outcome.duration_ms > 5000),
                )
                recommendation = rec
                failure_type = rec.failure_type.value
                # Activamos modo "network slow" si aplica.
                if rec.failure_type == MissionFailureType.NETWORK_SLOW:
                    self.timeouts.mark_network_slow()
                # Patrones globales.
                self.store.record_failure_pattern(
                    run_id=self.run_id,
                    mission_id=self.mission_id,
                    step_id=outcome.step_id,
                    step_kind=outcome.kind,
                    strategy=outcome.strategy_used,
                    failure_type=failure_type,
                    error_signature=(outcome.error or outcome.human_message or "")[:200] or None,
                    recovery_label=",".join(rec.actions) if rec else None,
                    recovered=False,
                )
            except Exception as e:
                log.debug(f"classifier falló: {e}")

        record = StepAttemptRecord(
            run_id=self.run_id,
            mission_id=self.mission_id,
            step_id=outcome.step_id,
            step_kind=outcome.kind,
            attempt_index=max(outcome.retries, 1),
            strategy_used=outcome.strategy_used or ("skip" if outcome.status == "skipped" else "?"),
            success=ok,
            retries=int(outcome.retries),
            duration_ms=int(outcome.duration_ms),
            semantic_target=ctx.get("semantic_target"),
            app=ctx.get("app"),
            domain=ctx.get("domain"),
            window_title=(outcome.state_after or {}).get("active_window_title")
                if outcome.state_after else None,
            state_before=outcome.state_before,
            state_after=outcome.state_after,
            recovery_used=None,
            user_intervention=(outcome.status == "needs_human"),
            failure_type=failure_type,
            error=(outcome.error or outcome.human_message)[:500] if (outcome.error or outcome.human_message) else None,
        )
        try:
            self.store.record_step_attempt(record)
        except Exception as e:
            log.debug(f"record_step_attempt falló: {e}")

        # Strategy scoring contextual.
        if outcome.strategy_used:
            try:
                self.store.upsert_strategy_score(
                    step_kind=outcome.kind,
                    strategy=outcome.strategy_used,
                    success=ok,
                    duration_ms=int(outcome.duration_ms),
                    scope_app=ctx.get("app"),
                    scope_domain=ctx.get("domain"),
                    scope_target=ctx.get("semantic_target"),
                )
                # También global (sin scope), para fallback.
                self.store.upsert_strategy_score(
                    step_kind=outcome.kind,
                    strategy=outcome.strategy_used,
                    success=ok,
                    duration_ms=int(outcome.duration_ms),
                )
            except Exception as e:
                log.debug(f"upsert_strategy_score falló: {e}")
            # Reliability por técnica de la máquina.
            try:
                self.machine.observe(outcome.strategy_used, success=ok)
            except Exception as e:
                log.debug(f"machine.observe falló: {e}")

    def _update_target_memory_on_outcome(self, outcome: StepOutcome) -> None:
        ctx = self._step_context.get(outcome.step_id, {})
        target = ctx.get("semantic_target")
        if not target:
            return
        try:
            if outcome.status == "success":
                self.targets.record_success(
                    target,
                    strategy=outcome.strategy_used,
                    domain=ctx.get("domain"),
                    app=ctx.get("app"),
                    selectors=ctx.get("selectors") or None,
                    uia=ctx.get("uia") or None,
                    ocr_anchor=ctx.get("ocr_anchor"),
                    visual_asset_hash=ctx.get("visual_asset_hash"),
                )
            elif outcome.status == "failed":
                self.targets.record_failure(
                    target,
                    strategy=outcome.strategy_used,
                    domain=ctx.get("domain"),
                    app=ctx.get("app"),
                )
        except Exception as e:
            log.debug(f"update_target_memory_on_outcome falló: {e}")

    def _record_timeout_observation(
        self, outcome: StepOutcome, *, success: bool,
    ) -> None:
        ctx = self._step_context.get(outcome.step_id, {})
        # El "condition" que mejor describe la espera es el postcondition
        # del step (lo abstraemos a "<kind>_loaded").
        condition = f"{outcome.kind}_loaded"
        try:
            self.timeouts.record_observation(
                condition,
                duration_ms=int(outcome.duration_ms),
                success=success,
                app=ctx.get("app"),
                domain=ctx.get("domain"),
            )
        except Exception as e:
            log.debug(f"record_timeout_observation falló: {e}")

    # ── Step context ──────────────────────────────────────────
    def _extract_context(self, step: MissionStep) -> Dict[str, Any]:
        """Saca semantic_target/app/domain del MissionStep para usarlos
        como scope de aprendizaje."""
        params = dict(step.params or {})
        target_ctx = params.get("target_context") or {}
        if not isinstance(target_ctx, dict):
            target_ctx = {}

        semantic_target = (
            params.get("semantic_target")
            or target_ctx.get("semantic_target")
            or target_ctx.get("label")
            or _guess_target_from_step(step)
        )

        domain = (
            params.get("domain")
            or target_ctx.get("domain")
            or _guess_domain_from_url(params.get("url") or params.get("alias"))
            or _guess_domain_from_kind(step.kind)
        )
        app = (
            params.get("app")
            or target_ctx.get("app")
            or _guess_app_from_kind(step.kind, params)
        )

        # Selectors / uia para target memory.
        selectors = []
        uia: Dict[str, Any] = {}
        if isinstance(target_ctx, dict):
            for s in target_ctx.get("selectors") or []:
                if isinstance(s, str) and s:
                    selectors.append(s)
            uia_blob = target_ctx.get("uia")
            if isinstance(uia_blob, dict):
                uia = uia_blob

        ocr_anchor = (
            target_ctx.get("ocr_anchor") if isinstance(target_ctx, dict) else None
        )
        visual_asset_hash = (
            target_ctx.get("visual_asset_hash") if isinstance(target_ctx, dict) else None
        )

        return {
            "semantic_target": semantic_target,
            "domain": domain,
            "app": app,
            "expected_app": app,
            "expected_url_substring": _expected_url_substring_from_kind(step.kind, params),
            "selectors": selectors,
            "uia": uia,
            "ocr_anchor": ocr_anchor,
            "visual_asset_hash": visual_asset_hash,
            "contract_kind": step.kind,
        }

    def _has_prior_success(self, step_kind: str, strategy: str) -> bool:
        if not strategy:
            return False
        try:
            rows = self.store.list_strategy_scores(step_kind=step_kind)
            for r in rows:
                if r.get("strategy") == strategy and int(r.get("ok") or 0) > 0:
                    return True
        except Exception:
            pass
        try:
            score = self.scoring.score_strategy(step_kind, strategy)
            return score.samples > 0 and score.score > 0.55
        except Exception:
            return False

    # ── Suggestions ───────────────────────────────────────────
    def _build_suggestions(
        self, steps: Sequence[MissionStep],
    ) -> List[MissionGraphRepairSuggestion]:
        if not self.mission_id:
            return []
        suggestions: List[MissionGraphRepairSuggestion] = []
        for step in steps:
            try:
                contract = build_contract(step)
            except Exception:
                continue
            current_preferred = (
                contract.preferred_strategy
                if hasattr(contract, "preferred_strategy")
                else (contract.all_strategies()[0] if contract.all_strategies() else "")
            )
            if not current_preferred:
                continue
            ctx = self._step_context.get(step.id, {})
            try:
                suggestion = self.repair.evaluate(
                    mission_id=self.mission_id,
                    step_kind=step.kind,
                    current_preferred=current_preferred,
                    candidates=[s for s in contract.all_strategies() if s != current_preferred],
                    step_id=step.id,
                    scope_app=ctx.get("app"),
                    scope_domain=ctx.get("domain"),
                    scope_target=ctx.get("semantic_target"),
                )
            except Exception as e:
                log.debug(f"repair.evaluate falló: {e}")
                suggestion = None
            if suggestion is not None:
                suggestions.append(suggestion)
        # De-duplicamos por (step_kind, suggested) — la misma sugerencia
        # repetida n veces sólo aparece una.
        seen = set()
        uniq: List[MissionGraphRepairSuggestion] = []
        for s in suggestions:
            key = (s.step_kind, s.suggested_preferred)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(s)
        return uniq


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _make_ephemeral_learning_logger() -> LearningLogger:
    """Crea un ``LearningLogger`` **en RAM**, sin escribir a disco.

    Se usa en modo privado para que el ``SmartMissionExecutor`` interno
    pueda llamar a ``record_success`` / ``record_failure`` sin que el
    JSONL global de ``var/missions/learning/`` se materialice.
    """
    import threading
    logger = LearningLogger.__new__(LearningLogger)
    logger._lock = threading.RLock()
    logger._stats = {}
    # Override de los métodos privados que tocan disco. Mantenemos las
    # estadísticas en memoria para que ``rank_strategies`` siga
    # funcionando dentro de la misma sesión.
    logger._append = lambda rec: None  # type: ignore[attr-defined]
    logger._save_stats = lambda: None  # type: ignore[attr-defined]
    return logger




def _guess_app_from_kind(kind: str, params: Dict[str, Any]) -> Optional[str]:
    if kind in ("open_app", "select_profile", "open_new_tab", "open_url",
                "search_youtube", "navigate_to"):
        return "chrome.exe"
    if "app" in params and isinstance(params["app"], str):
        return params["app"]
    return None


def _guess_domain_from_kind(kind: str) -> Optional[str]:
    if kind in ("search_youtube",):
        return "youtube.com"
    if kind in ("search_google",):
        return "google.com"
    return None


def _guess_domain_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    s = str(url).lower()
    if "youtube.com" in s or s == "youtube":
        return "youtube.com"
    if "google.com" in s or s == "google":
        return "google.com"
    if "://" in s:
        try:
            host = s.split("://", 1)[1].split("/", 1)[0]
            return host
        except Exception:
            return None
    return None


def _guess_target_from_step(step: MissionStep) -> Optional[str]:
    p = step.params or {}
    if "semantic_target" in p and isinstance(p["semantic_target"], str):
        return p["semantic_target"]
    if step.kind == "search_youtube":
        return "youtube_search_box"
    if step.kind == "search":
        return "search_box"
    if step.kind == "open_url" and p.get("alias"):
        return f"url:{p['alias']}"
    return None


def _expected_url_substring_from_kind(kind: str, params: Dict[str, Any]) -> Optional[str]:
    if kind in ("open_url", "navigate_to"):
        url = params.get("url") or params.get("alias") or ""
        if "youtube" in str(url).lower():
            return "youtube.com"
        if "google" in str(url).lower():
            return "google.com"
        if isinstance(url, str) and "://" in url:
            try:
                return url.split("://", 1)[1].split("/", 1)[0]
            except Exception:
                return None
    if kind == "search_youtube":
        return "youtube.com"
    return None


# ──────────────────────────────────────────────────────────────────────
# Atajo
# ──────────────────────────────────────────────────────────────────────

def run_mission_with_learning(
    steps: Sequence[MissionStep],
    *,
    mission_id: Optional[str] = None,
    expert_mode: bool = False,
    private_mode: Optional[bool] = None,
    on_status: Optional[StatusCb] = None,
    on_human_help_needed: Optional[HumanHelpCb] = None,
    on_step_done: Optional[ProgressCb] = None,
    on_human_correction: Optional[HumanCorrectionCb] = None,
    on_repair_suggestion: Optional[Callable[[MissionGraphRepairSuggestion], str]] = None,
) -> AutoLearningResult:
    """Atajo de alto nivel."""
    executor = AutoLearningExecutor(
        mission_id=mission_id,
        expert_mode=expert_mode,
        private_mode=private_mode,
        on_status=on_status,
        on_human_help_needed=on_human_help_needed,
        on_step_done=on_step_done,
        on_human_correction=on_human_correction,
        on_repair_suggestion=on_repair_suggestion,
    )
    return executor.run_mission(steps)


__all__ = [
    "AutoLearningExecutor",
    "AutoLearningResult",
    "HumanCorrectionCb",
    "run_mission_with_learning",
]
