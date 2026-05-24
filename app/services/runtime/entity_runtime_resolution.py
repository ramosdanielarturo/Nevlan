"""
Entity-first runtime hook — SmartExecutor / MissionPlayer
---------------------------------------------------------
Resuelve y ejecuta entidades operacionales **antes** del TargetResolver
legacy, coords o estrategias de emergencia.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import OperationalEntity, OperationalEntityType
from app.core.logger import log
from app.services.missions.action_runner import StrategyResult, _fail, _ok
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime.entity_resolution_layer import (
    EntityMemoryStore,
    EntityResolutionRuntimeResult,
    _text_similarity,
    extract_entity_from_semantic_step,
    is_generic_entity_label,
    match_entity_against_runtime,
    normalize_entity_name,
    resolve_entity_for_runtime,
)

# Umbrales runtime
RUNTIME_MATCH_MIN = 0.72
RUNTIME_EXECUTE_MIN = 0.78
AMBIGUITY_SIMILARITY_MIN = 0.80
AMBIGUITY_SCORE_GAP = 0.06

# Step kinds que nunca auto-ejecutan vía ERL (safety)
UNSAFE_STEP_KIND_MARKERS: frozenset = frozenset({
    "pay", "payment", "pago", "transfer", "delete", "eliminar", "remove",
    "password", "auth", "login", "sign_in", "2fa", "otp", "permission",
    "confirm_destructive", "destructive", "wire_transfer", "checkout",
})

UNSAFE_ENTITY_TYPE_BLOCK: frozenset = frozenset({
    OperationalEntityType.UNKNOWN,
})

UNSAFE_LABEL_MARKERS: frozenset = frozenset({
    "pagar", "pay now", "eliminar", "delete", "transferir", "password",
    "contraseña", "confirmar eliminación", "borrar permanentemente",
})

COORD_STRATEGY_MARKERS: Tuple[str, ...] = (
    "coords",
    "relative_coords",
    "visual_asset",
    "use_coords",
    "coordinate",
)

# Códigos canónicos de fallo entity-first (UOL / ORCE / Beta Acceptance).
ENTITY_RUNTIME_TIMEOUT = "ENTITY_RUNTIME_TIMEOUT"
ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
ENTITY_AMBIGUOUS = "ENTITY_AMBIGUOUS"
ENTITY_CONTEXT_ALREADY_ACTIVE = "ENTITY_CONTEXT_ALREADY_ACTIVE"
ENTITY_CONTEXT_NOT_VERIFIABLE = "ENTITY_CONTEXT_NOT_VERIFIABLE"
ENTITY_SELECTOR_NOT_VISIBLE = "ENTITY_SELECTOR_NOT_VISIBLE"
NO_STATE_PROGRESS = "NO_STATE_PROGRESS"

# Context creation / open-tab failures (never ENTITY_*).
OPEN_TAB_NOT_CONFIRMED = "OPEN_TAB_NOT_CONFIRMED"
CONTEXT_CREATION_TIMEOUT = "CONTEXT_CREATION_TIMEOUT"
ADDRESS_SURFACE_NOT_READY = "ADDRESS_SURFACE_NOT_READY"

# Surface / input / navigation failures (never ENTITY_*).
SEARCH_RESULTS_NOT_CONFIRMED = "SEARCH_RESULTS_NOT_CONFIRMED"
SEARCH_INPUT_NOT_FOCUSED = "SEARCH_INPUT_NOT_FOCUSED"
SEARCH_SUBMIT_NOT_CONFIRMED = "SEARCH_SUBMIT_NOT_CONFIRMED"
BROWSE_VIEWPORT_NOT_CHANGED = "BROWSE_VIEWPORT_NOT_CHANGED"

_CONTEXT_CREATION_STEP_KINDS = frozenset({
    "open_new_tab",
    "open_tab",
    "create_new_context",
})
_CONTEXT_CREATION_UOL_ACTIONS = frozenset({
    "open_tab",
})
_CONTEXT_CREATION_ELEMENT_TYPES = frozenset({
    "context_creation_affordance",
})

_SURFACE_OPERATION_STEP_KINDS = frozenset({
    "search_site",
    "search_youtube",
    "search_content",
    "search_web",
    "focus_input",
    "submit_input",
    "browse_results",
    "scroll_results",
    "open_site",
    "open_url",
    "navigate_to_resource",
    "fill_form",
    "fill_field",
    "submit_form",
    "switch_context",
    "confirm_dialog",
})
_SURFACE_OPERATION_UOL_ACTIONS = frozenset({
    "search_content",
    "focus_input",
    "submit_input",
    "browse_results",
    "navigate_to_location",
    "fill_field",
    "switch_context",
    "confirm_action",
})


def is_context_creation_step(step: MissionStep) -> bool:
    """True si el paso crea contexto/pestaña — fuera del hook entity-first."""
    kind = str(step.kind or "").strip()
    if kind in _CONTEXT_CREATION_STEP_KINDS:
        return True
    uol = str(step.params.get("uol_action") or "").strip().lower()
    if uol in _CONTEXT_CREATION_UOL_ACTIONS:
        return True
    ident = str(step.params.get("operational_element_type") or "").strip().lower()
    if ident in _CONTEXT_CREATION_ELEMENT_TYPES:
        return True
    return False


def sanitize_context_creation_failure_reason(step: MissionStep, reason: str) -> str:
    """Evita códigos ENTITY_* en telemetría de pasos open_tab/create_context."""
    code = str(reason or "").strip()
    if not is_context_creation_step(step):
        return code
    if code.startswith("ENTITY_") or code in {NO_STATE_PROGRESS, ENTITY_RUNTIME_TIMEOUT}:
        return OPEN_TAB_NOT_CONFIRMED
    return code or OPEN_TAB_NOT_CONFIRMED


def is_surface_operation_step(step: MissionStep) -> bool:
    """True si el paso opera sobre superficie/input — fuera del hook entity-first."""
    kind = str(step.kind or "").strip()
    if kind in _SURFACE_OPERATION_STEP_KINDS:
        return True
    uol = str(step.params.get("uol_action") or "").strip().lower()
    if uol in _SURFACE_OPERATION_UOL_ACTIONS:
        return True
    return False


def surface_operation_failure_reason(step: MissionStep) -> str:
    """Código de fallo canónico para pasos de superficie (no entidad)."""
    kind = str(step.kind or "").strip().lower()
    uol = str(step.params.get("uol_action") or "").strip().lower()
    if kind in {"focus_input"} or uol == "focus_input":
        return SEARCH_INPUT_NOT_FOCUSED
    if kind in {"submit_input"} or uol == "submit_input":
        return SEARCH_SUBMIT_NOT_CONFIRMED
    if kind in {"browse_results", "scroll_results"} or uol == "browse_results":
        return BROWSE_VIEWPORT_NOT_CHANGED
    if kind in {"open_site", "open_url", "navigate_to_resource"} or uol == "navigate_to_location":
        return ADDRESS_SURFACE_NOT_READY
    if kind in {"fill_form", "fill_field"} or uol == "fill_field":
        return "FILL_VALUE_NOT_CONFIRMED"
    if kind in {"submit_form"} or uol == "submit_input":
        return "SUBMIT_OUTCOME_NOT_CONFIRMED"
    if kind in {"switch_context"} or uol == "switch_context":
        return "CONTEXT_SWITCH_NOT_CONFIRMED"
    if kind in {"confirm_dialog"} or uol == "confirm_action":
        return "DIALOG_DISMISS_NOT_CONFIRMED"
    return SEARCH_RESULTS_NOT_CONFIRMED


def sanitize_surface_operation_failure_reason(step: MissionStep, reason: str) -> str:
    """Evita códigos ENTITY_* en telemetría de pasos search/input/browse/navigate."""
    code = str(reason or "").strip()
    if not is_surface_operation_step(step):
        return code
    if code.startswith("ENTITY_") or code in {NO_STATE_PROGRESS, ENTITY_RUNTIME_TIMEOUT}:
        return surface_operation_failure_reason(step)
    return code or surface_operation_failure_reason(step)


def sanitize_non_entity_step_failure_reason(step: MissionStep, reason: str) -> str:
    """Unifica sanitización para pasos que no deben reportar ENTITY_*."""
    if is_context_creation_step(step):
        return sanitize_context_creation_failure_reason(step, reason)
    if is_surface_operation_step(step):
        return sanitize_surface_operation_failure_reason(step, reason)
    return str(reason or "").strip()


@dataclass
class EntityRuntimeBudget:
    """Presupuesto de tiempo e intentos para entity-first runtime."""

    deadline_mono: float
    max_attempts: int = 3
    attempts_used: int = 0
    last_viewport_signature: str = ""
    stagnant_rounds: int = 0

    def remaining_ms(self) -> float:
        return max(0.0, (self.deadline_mono - time.monotonic()) * 1000.0)

    def exceeded(self) -> bool:
        return time.monotonic() >= self.deadline_mono

    def consume_attempt(self) -> bool:
        """Registra intento; True si aún hay presupuesto de intentos."""
        self.attempts_used += 1
        return self.attempts_used <= self.max_attempts

    def note_progress(self, viewport_signature: str) -> bool:
        """True si hubo avance observable; False si estancado."""
        sig = str(viewport_signature or "").strip()
        if not sig:
            return True
        if sig == self.last_viewport_signature:
            self.stagnant_rounds += 1
        else:
            self.stagnant_rounds = 0
            self.last_viewport_signature = sig
        return self.stagnant_rounds < 2


def entity_runtime_budget_from_settings(settings_obj: Any = None) -> EntityRuntimeBudget:
    timeout_ms = 12_000
    max_attempts = 3
    try:
        if settings_obj is None:
            from app.core.config import get_settings

            settings_obj = get_settings()
        timeout_ms = int(getattr(settings_obj, "ENTITY_RUNTIME_TIMEOUT_MS", 12_000))
        max_attempts = int(getattr(settings_obj, "ENTITY_MAX_RESOLUTION_ATTEMPTS", 3))
    except Exception:
        pass
    return EntityRuntimeBudget(
        deadline_mono=time.monotonic() + (timeout_ms / 1000.0),
        max_attempts=max_attempts,
    )


def uia_walk_budget_sec(settings_obj: Any = None, budget: Optional[EntityRuntimeBudget] = None) -> float:
    ms = 2500
    try:
        if settings_obj is None:
            from app.core.config import get_settings

            settings_obj = get_settings()
        ms = int(getattr(settings_obj, "ENTITY_UIA_WALK_BUDGET_MS", 2500))
    except Exception:
        pass
    if budget is not None:
        ms = min(ms, int(budget.remaining_ms()))
    return max(0.05, ms / 1000.0)


def _entity_failure_result(
    out: EntityRuntimeHookResult,
    audit: EntityRuntimeAuditEntry,
    reason: str,
    mission: Any,
    *,
    human_message: str = "",
) -> EntityRuntimeHookResult:
    audit.blocked_reason = reason
    audit.resolved = False
    out.attempted = True
    out.failure_reason = reason
    if reason == ENTITY_RUNTIME_TIMEOUT:
        out.timed_out = True
    elif reason == ENTITY_AMBIGUOUS:
        out.blocked_ambiguity = True
    elif reason == NO_STATE_PROGRESS:
        out.no_state_progress = True
    out.human_message = human_message or {
        ENTITY_RUNTIME_TIMEOUT: "Tiempo agotado buscando el elemento en pantalla.",
        ENTITY_NOT_FOUND: "No encontré el elemento en pantalla.",
        ENTITY_AMBIGUOUS: "Hay varias opciones similares; indica cuál elegir.",
        ENTITY_CONTEXT_ALREADY_ACTIVE: "El contexto de identidad solicitado ya está activo.",
        ENTITY_CONTEXT_NOT_VERIFIABLE: (
            "No pude verificar el contexto de identidad activo en pantalla."
        ),
        ENTITY_SELECTOR_NOT_VISIBLE: (
            "No hay selector de identidad visible y la sesión no está lista."
        ),
        NO_STATE_PROGRESS: "No hubo avance al buscar el elemento.",
    }.get(reason, "No pude resolver la entidad.")
    append_entity_runtime_audit(mission, audit)
    return out


@dataclass
class EntityRuntimeAuditEntry:
    step_id: str = ""
    entity_id: str = ""
    entity_type: str = ""
    display_name: str = ""
    attempted: bool = False
    resolved: bool = False
    confidence: float = 0.0
    strategy_used: str = ""
    fallback_used: bool = False
    candidates_count: int = 0
    ambiguity: bool = False
    memory_updated: bool = False
    blocked_reason: str = ""
    method: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class EntityRuntimeHookResult:
    """Resultado del hook entity-first en un step."""

    attempted: bool = False
    resolved: bool = False
    executed: bool = False
    blocked_unsafe: bool = False
    blocked_ambiguity: bool = False
    strategy_used: str = ""
    confidence: float = 0.0
    fallback_used: bool = False
    audit: Optional[EntityRuntimeAuditEntry] = None
    strategy_result: Optional[StrategyResult] = None
    skip_legacy_coords: bool = False
    human_message: str = ""
    failure_reason: str = ""
    timed_out: bool = False
    no_state_progress: bool = False


def append_entity_runtime_audit(mission: Any, entry: EntityRuntimeAuditEntry) -> None:
    if mission is None:
        return
    try:
        rows = list(getattr(mission, "entity_runtime_resolution_audit", None) or [])
        rows.append(asdict(entry))
        setattr(mission, "entity_runtime_resolution_audit", rows[-200:])
    except Exception as exc:
        log.debug("[ERL runtime] audit append: %s", exc)


def entity_from_mission_step(step: MissionStep, mission: Any = None) -> Optional[OperationalEntity]:
    """Carga ``operational_entity`` del step o la deriva del SEP/misión."""
    pars = dict(step.params or {})
    raw = pars.get("operational_entity")
    if isinstance(raw, dict) and raw.get("display_name"):
        try:
            return OperationalEntity.model_validate(raw)
        except Exception:
            pass
    blob = {
        "id": step.id,
        "type": step.kind,
        "params": pars,
        "human_label": step.human_label or "",
    }
    ent = extract_entity_from_semantic_step(blob, mission)
    if ent:
        return ent
    display = str(
        pars.get("profile")
        or pars.get("profile_name")
        or pars.get("query")
        or pars.get("label")
        or pars.get("label_hint")
        or "",
    ).strip()
    if (not display or is_generic_entity_label(display)) and mission is not None:
        for tb in list(getattr(mission, "truth_blocks", None) or []):
            tw = dict(getattr(tb, "context_window", None) or {})
            tel = dict(tw.get("tel_derived_params") or {})
            hint = str(
                tel.get("label_hint")
                or tel.get("profile")
                or tel.get("query")
                or "",
            ).strip()
            if hint and not is_generic_entity_label(hint):
                display = hint
                break
    if not display:
        return None
    try:
        from app.services.runtime.entity_resolution_layer import (
            extract_operational_entity,
        )

        return extract_operational_entity(
            capture_bundle={
                "display_name": display,
                "uia_name": display,
                "step_type": step.kind,
                "trusted_pre_action_identity": bool(
                    pars.get("trusted_pre_action_identity"),
                ),
                "target_identity_isolation": {
                    "trusted_pre_action_identity": True,
                    "target_label": display,
                    "target_identity_confidence": float(
                        pars.get("target_identity_confidence") or 0.88,
                    ),
                },
            },
            step_type_hint=step.kind,
            store_on_success=False,
        )
    except Exception:
        return None


def is_unsafe_entity_auto_execute(
    step: MissionStep,
    entity: OperationalEntity,
) -> Tuple[bool, str]:
    """Bloquea auto-ejecución en contextos sensibles."""
    kind_l = str(step.kind or "").lower()
    for tok in UNSAFE_STEP_KIND_MARKERS:
        if kind_l == tok or kind_l.endswith(f"_{tok}") or kind_l.startswith(f"{tok}_"):
            return True, f"unsafe_step_kind:{tok}"
    if entity.entity_type in UNSAFE_ENTITY_TYPE_BLOCK and not entity.display_name:
        return True, "unsafe_entity_type_unknown"
    label_l = f"{entity.display_name} {step.human_label or ''}".lower()
    for tok in UNSAFE_LABEL_MARKERS:
        if tok in label_l:
            return True, f"unsafe_label:{tok}"
    role = str(entity.operational_role or "").lower()
    if any(x in role for x in ("payment", "delete", "auth", "destructive")):
        return True, f"unsafe_operational_role:{role}"
    return False, ""


def collect_runtime_candidates(
    state: StateSnapshot,
    *,
    deep: bool = False,
    budget: Optional[EntityRuntimeBudget] = None,
) -> List[Dict[str, Any]]:
    """Candidatos runtime desde UIA/OCR visibles (agnóstico de app)."""
    if budget is not None and budget.exceeded():
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def _add(label: str, **extra: Any) -> None:
        lab = str(label or "").strip()
        if not lab or is_generic_entity_label(lab):
            return
        key = normalize_entity_name(lab)
        if not key or key in seen:
            return
        seen.add(key)
        out.append({"label": lab, "name": lab, "uia_name": lab, **extra})

    for name in list(state.uia_visible_names or [])[:80]:
        _add(name, source="uia_visible")

    if state.visible_text:
        for chunk in str(state.visible_text).splitlines():
            chunk = chunk.strip()
            if len(chunk) >= 3:
                _add(chunk, source="ocr_line", ocr_text=chunk)

    if deep and (budget is None or budget.remaining_ms() > 200):
        try:
            import uiautomation as auto  # type: ignore

            root = auto.GetForegroundControl()
            if root and root.Exists(0, 0):
                walk_deadline = time.monotonic() + uia_walk_budget_sec(budget=budget)
                for depth, ctrl, _ in auto.WalkControl(root, maxDepth=8):
                    if time.monotonic() >= walk_deadline:
                        log.debug("[ERL runtime] uia walk budget exhausted")
                        break
                    if depth > 8:
                        break
                    try:
                        nm = (ctrl.Name or "").strip()
                    except Exception:
                        nm = ""
                    if nm:
                        _add(nm, source="uia_walk", automation_id=str(
                            getattr(ctrl, "AutomationId", "") or "",
                        ))
        except Exception as exc:
            log.debug("[ERL runtime] uia walk: %s", exc)

    return out


def _count_ambiguous_candidates(
    entity: OperationalEntity,
    candidates: Sequence[Dict[str, Any]],
) -> Tuple[int, float]:
    """Cuenta candidatos fuertemente similares (multi-target)."""
    scores: List[float] = []
    for cand in candidates:
        label = str(
            cand.get("label") or cand.get("name") or cand.get("uia_name") or "",
        )
        if not label:
            continue
        sim = _text_similarity(entity.display_name, label)
        if sim >= AMBIGUITY_SIMILARITY_MIN:
            scores.append(sim)
    if len(scores) < 2:
        return len(scores), max(scores) if scores else 0.0
    scores.sort(reverse=True)
    if scores[0] - scores[1] < AMBIGUITY_SCORE_GAP:
        return len(scores), scores[0]
    return 1, scores[0]


def _build_runtime_context(
    step: MissionStep,
    state: StateSnapshot,
    mission: Any = None,
) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {
        "step_id": step.id,
        "step_kind": step.kind,
        "window_title": state.active_window_title or "",
        "process_name": state.active_app or "",
        "browser_domain": state.browser_domain or "",
        "browser_url": state.browser_url or "",
    }
    if mission is not None:
        ctx["mission_id"] = str(getattr(mission, "id", "") or "")
    return ctx


def resolve_entity_runtime_ordered(
    entity: OperationalEntity,
    candidates: Sequence[Dict[str, Any]],
    *,
    context: Optional[Dict[str, Any]] = None,
    mission: Any = None,
    step: Optional[MissionStep] = None,
    state: Optional[StateSnapshot] = None,
) -> EntityResolutionRuntimeResult:
    """Orden: match → histórico → refresh UIA → ARL hint → legacy noop."""
    ctx = dict(context or {})
    res = resolve_entity_for_runtime(
        entity,
        candidates,
        context=ctx,
        record_success=False,
    )
    if res.resolved and res.confidence >= RUNTIME_MATCH_MIN:
        return res

    # Refresh candidatos (deep UIA) — relocation ligera
    if state is not None:
        deep_cands = collect_runtime_candidates(state, deep=True)
        if deep_cands:
            res2 = resolve_entity_for_runtime(
                entity, deep_cands, context=ctx, record_success=False,
            )
            if res2.resolved and res2.confidence >= res.confidence:
                return res2

    # ARL-assisted: segundo pase de estado si hay misión compilada
    if mission is not None and step is not None:
        try:
            from app.services.runtime.arl_sep_recovery import evaluate_sep_arl_recovery
            from app.services.missions.execution_contracts import build_contract

            contract = build_contract(step)
            verdict = evaluate_sep_arl_recovery(
                mission=mission,
                step=step,
                contract=contract,
                observed_snapshot=state,
                before_state=ctx,
                last_strategy_used="entity_resolution",
                last_strategy_ok=False,
                last_error="entity_unresolved",
                arl_round_done=0,
                max_extra_rounds=1,
                retry_budget=1,
            )
            if verdict.retry_step and state is not None:
                from app.services.missions.state_detector import StateDetector

                fresh = StateDetector().detect(deep=True)
                c3 = collect_runtime_candidates(fresh, deep=True)
                if c3:
                    res3 = resolve_entity_for_runtime(
                        entity, c3, context=ctx, record_success=False,
                    )
                    if res3.resolved:
                        res3.notes.append("arl_state_refresh")
                        return res3
        except Exception as exc:
            log.debug("[ERL runtime] ARL assist: %s", exc)

    return res


def execute_entity_target_click(
    runtime_target: Dict[str, Any],
    entity: OperationalEntity,
) -> StrategyResult:
    """Ejecuta click UIA por nombre de entidad (sin coords primarios)."""
    label = str(
        runtime_target.get("label")
        or runtime_target.get("name")
        or runtime_target.get("uia_name")
        or entity.display_name,
    ).strip()
    if not label:
        return _fail("entity_resolution", "sin label para click")

    try:
        import uiautomation as auto  # type: ignore
    except Exception:
        return _fail("entity_resolution", "uiautomation no disponible")

    try:
        root = auto.GetForegroundControl()
        if not root or not root.Exists(0.5, 0):
            return _fail("entity_resolution", "sin ventana en foco")
        target = root.TextControl(searchDepth=12, Name=label)
        if not target.Exists(0.6, 0):
            target = root.ButtonControl(searchDepth=12, Name=label)
        if not target.Exists(0.6, 0):
            target = root.ListItemControl(searchDepth=12, Name=label)
        if not target.Exists(0.6, 0):
            return _fail("entity_resolution", f"control '{label[:80]}' no encontrado")
        target.Click(simulateMove=False)
        return _ok(
            "entity_resolution",
            f"click entidad '{label[:80]}'",
            method="entity_resolution",
            entity_id=entity.entity_id,
            resolution_strategy=runtime_target.get("_erl_strategy", ""),
        )
    except Exception as exc:
        return _fail("entity_resolution", str(exc))


def _inject_entity_params(step: MissionStep, entity: OperationalEntity) -> None:
    """Sincroniza params ejecutables con la entidad resuelta."""
    pars = step.params
    name = entity.display_name
    if step.kind == "select_profile" or entity.entity_type in (
        OperationalEntityType.USER_PROFILE,
        OperationalEntityType.BROWSER_PROFILE,
    ):
        pars["profile_name"] = name
        pars["profile"] = name
    elif entity.entity_type == OperationalEntityType.SEARCH_QUERY:
        pars["query"] = name
    pars["operational_entity"] = entity.model_dump(mode="json")
    pars["entity_resolution_confidence"] = entity.confidence


def record_entity_runtime_success(
    mission: Any,
    step: MissionStep,
    entity: OperationalEntity,
    *,
    strategy: str,
    runtime_target: Dict[str, Any],
    context: Dict[str, Any],
    validator_passed: bool = True,
) -> None:
    """Memoria + SLL + OMPC tras éxito."""
    store = EntityMemoryStore()
    try:
        store.upsert_entity(entity)
        store.record_resolution_success(entity.entity_id, strategy, context)
    except Exception as exc:
        log.debug("[ERL runtime] memory: %s", exc)

    strat_full = f"entity_resolution:{strategy}"
    try:
        from app.core.config import get_settings

        if get_settings().SLL_ENABLED:
            from app.services.runtime.strategy_learning_layer import (
                build_context_from_semantic_step,
                record_observation,
            )

            record_observation(
                context=build_context_from_semantic_step(step, mission, context),
                strategy_name=strat_full,
                success=True,
                duration_ms=0.0,
                mission=mission,
                validator_passed=validator_passed,
            )
    except Exception as exc:
        log.debug("[ERL runtime] SLL: %s", exc)

    try:
        from app.core.config import get_settings

        if get_settings().OMPC_ENABLED:
            audit = getattr(mission, "ompc_entity_resolution_audit", None)
            if not isinstance(audit, list):
                audit = []
            audit.append({
                "kind": "successful_entity_resolution",
                "step_id": step.id,
                "entity_id": entity.entity_id,
                "entity_type": entity.entity_type.value,
                "strategy": strategy,
                "ts": time.time(),
            })
            setattr(mission, "ompc_entity_resolution_audit", audit[-100:])
    except Exception:
        pass


def record_entity_runtime_failure(
    mission: Any,
    step: MissionStep,
    entity: Optional[OperationalEntity],
    *,
    strategy: str,
    context: Dict[str, Any],
    ambiguous: bool,
) -> None:
    if ambiguous:
        return
    try:
        from app.core.config import get_settings

        if not get_settings().SLL_ENABLED:
            return
        from app.services.runtime.strategy_learning_layer import (
            build_context_from_semantic_step,
            record_observation,
        )

        record_observation(
            context=build_context_from_semantic_step(step, mission, context),
            strategy_name=f"entity_resolution:{strategy or 'unresolved'}",
            success=False,
            duration_ms=0.0,
            mission=mission,
            validator_passed=False,
        )
    except Exception:
        pass


def filter_strategies_when_entity_present(
    strategies: List[str],
    *,
    entity_resolved: bool,
) -> List[str]:
    """Si ERL resolvió, excluir coords/emergencia del ranking."""
    if not entity_resolved:
        return strategies
    out: List[str] = []
    for s in strategies:
        sl = s.lower()
        if any(m in sl for m in COORD_STRATEGY_MARKERS):
            continue
        out.append(s)
    return out if out else strategies


def try_entity_first_runtime(
    step: MissionStep,
    state: StateSnapshot,
    mission: Any = None,
    *,
    expert_mode: bool = False,
    budget: Optional[EntityRuntimeBudget] = None,
) -> EntityRuntimeHookResult:
    """Hook principal: entity-first antes del loop de estrategias legacy."""
    budget = budget or entity_runtime_budget_from_settings()
    audit = EntityRuntimeAuditEntry(step_id=str(step.id or ""))
    out = EntityRuntimeHookResult(audit=audit)

    if is_context_creation_step(step) or is_surface_operation_step(step):
        return out

    if budget.exceeded():
        return _entity_failure_result(out, audit, ENTITY_RUNTIME_TIMEOUT, mission)
    if not budget.consume_attempt():
        return _entity_failure_result(out, audit, NO_STATE_PROGRESS, mission)

    pars = dict(step.params or {})
    if pars.get("operational_collection") and pars.get("collection_entity"):
        try:
            from app.services.runtime.universal_collection_entity_engine import (
                append_collection_audit,
                try_collection_entity_first_runtime,
            )
            if budget.exceeded():
                return _entity_failure_result(out, audit, ENTITY_RUNTIME_TIMEOUT, mission)
            candidates_pre = collect_runtime_candidates(state, deep=True, budget=budget)
            coll_match = try_collection_entity_first_runtime(
                step,
                candidates_pre,
                mission=mission,
                context=_build_runtime_context(step, state, mission),
            )
            append_collection_audit(
                mission,
                {
                    "step_id": step.id,
                    "collection_type": (pars.get("operational_collection") or {}).get(
                        "collection_type", "?",
                    ),
                    "entity_name": (pars.get("collection_entity") or {}).get(
                        "display_name", "",
                    ),
                    "confidence": coll_match.confidence,
                    "strategy": coll_match.strategy,
                    "matched": coll_match.matched,
                },
            )
            if coll_match.matched and coll_match.entity:
                entity = entity_from_mission_step(step, mission)
                if entity is None and pars.get("operational_entity"):
                    try:
                        entity = OperationalEntity.model_validate(
                            pars["operational_entity"],
                        )
                    except Exception:
                        entity = None
                if entity and coll_match.confidence >= RUNTIME_EXECUTE_MIN:
                    audit.attempted = True
                    audit.resolved = True
                    audit.confidence = coll_match.confidence
                    audit.strategy_used = f"uces:{coll_match.strategy}"
                    out.attempted = True
                    out.resolved = True
                    out.confidence = coll_match.confidence
                    out.strategy_used = audit.strategy_used
                    out.skip_legacy_coords = True
                    click_res = execute_entity_target_click(
                        {"label": coll_match.entity.display_name},
                        entity,
                    )
                    out.strategy_result = click_res
                    if click_res.ok:
                        out.executed = True
                        record_entity_runtime_success(
                            mission,
                            step,
                            entity,
                            strategy=coll_match.strategy,
                            runtime_target={"label": coll_match.entity.display_name},
                            context=_build_runtime_context(step, state, mission),
                        )
                        append_entity_runtime_audit(mission, audit)
                        return out
        except Exception as exc:
            log.debug("[UCES runtime] collection-first: %s", exc)

        # LONE — entidad no visible en viewport actual: navegar espacio vivo.
        if not out.executed and pars.get("operational_collection"):
            if budget.exceeded():
                return _entity_failure_result(out, audit, ENTITY_RUNTIME_TIMEOUT, mission)
            if not budget.consume_attempt():
                return _entity_failure_result(out, audit, NO_STATE_PROGRESS, mission)
            try:
                from app.contracts.mission import OperationalCollection
                from app.services.runtime.lone_runtime_bridge import (
                    is_lone_live_bridge_available,
                    run_lone_recovery_with_live_perception,
                )
                from app.services.runtime.live_operational_navigation_engine import (
                    try_live_navigation_recovery,
                )

                ent_for_lone = entity_from_mission_step(step, mission)
                if ent_for_lone:
                    coll_raw = pars.get("operational_collection")
                    stored_coll = None
                    try:
                        stored_coll = OperationalCollection.model_validate(coll_raw)
                    except Exception:
                        pass
                    if is_lone_live_bridge_available(mission):
                        lone = run_lone_recovery_with_live_perception(
                            step,
                            state,
                            mission,
                            target_entity=ent_for_lone,
                            target_collection=stored_coll,
                            budget=budget,
                        )
                    else:
                        lone = try_live_navigation_recovery(
                            step,
                            state,
                            mission,
                            target_entity=ent_for_lone,
                            target_collection=stored_coll,
                            deadline_mono=budget.deadline_mono,
                            progress_budget=budget,
                        )
                    if lone.stopped_reason in ("entity_runtime_timeout", "no_state_progress"):
                        reason = (
                            NO_STATE_PROGRESS
                            if lone.stopped_reason == "no_state_progress"
                            else ENTITY_RUNTIME_TIMEOUT
                        )
                        return _entity_failure_result(out, audit, reason, mission)
                    if lone.request_human:
                        out.blocked_unsafe = lone.request_human
                        out.human_message = lone.human_message or out.human_message
                        append_entity_runtime_audit(mission, audit)
                        return out
                    if lone.success and lone.entity:
                        audit.strategy_used = f"lone:{lone.stopped_reason}"
                        audit.confidence = lone.confidence
                        click_res = execute_entity_target_click(
                            {"label": lone.entity.display_name},
                            ent_for_lone,
                        )
                        out.strategy_result = click_res
                        if click_res.ok:
                            out.executed = True
                            out.resolved = True
                            out.confidence = lone.confidence
                            out.skip_legacy_coords = True
                            append_entity_runtime_audit(mission, audit)
                            return out
            except Exception as lone_exc:
                log.debug("[LONE] recovery: %s", lone_exc)

    from app.services.runtime.select_identity_context_runtime import (
        detect_identity_selector_visible,
        is_identity_selection_step,
        map_entity_failure_for_identity_context,
        skip_if_active_identity_context,
        verify_active_identity_context,
    )

    if is_identity_selection_step(step):
        skip = skip_if_active_identity_context(step, state, mission)
        if skip is not None:
            audit.resolved = True
            audit.strategy_used = skip.strategy
            audit.confidence = float(skip.verification.confidence if skip.verification else 1.0)
            out.resolved = True
            out.executed = True
            out.skip_legacy_coords = True
            out.strategy_used = skip.strategy
            out.strategy_result = skip.to_strategy_result()
            append_entity_runtime_audit(mission, audit)
            return out

    entity = entity_from_mission_step(step, mission)
    if entity is None:
        reason = ENTITY_NOT_FOUND
        if is_identity_selection_step(step):
            selector_visible, _ = detect_identity_selector_visible(state, step, mission)
            verification = verify_active_identity_context(step, state, mission)
            reason = map_entity_failure_for_identity_context(
                ENTITY_NOT_FOUND,
                selector_visible=selector_visible,
                verification=verification,
            )
        audit.blocked_reason = reason
        out.failure_reason = reason
        append_entity_runtime_audit(mission, audit)
        return out

    if budget.exceeded():
        return _entity_failure_result(out, audit, ENTITY_RUNTIME_TIMEOUT, mission)

    audit.attempted = True
    audit.entity_id = entity.entity_id
    audit.entity_type = (
        entity.entity_type.value
        if hasattr(entity.entity_type, "value")
        else str(entity.entity_type)
    )
    audit.display_name = entity.display_name

    unsafe, unsafe_reason = is_unsafe_entity_auto_execute(step, entity)
    if unsafe:
        audit.blocked_reason = unsafe_reason
        audit.ambiguity = False
        out.blocked_unsafe = True
        out.attempted = True
        out.human_message = (
            "Esta acción requiere confirmación humana antes de ejecutarse."
        )
        append_entity_runtime_audit(mission, audit)
        return out

    candidates = collect_runtime_candidates(state, deep=bool(
        step.kind in ("select_profile", "click", "select_option"),
    ), budget=budget)
    if budget.exceeded():
        return _entity_failure_result(out, audit, ENTITY_RUNTIME_TIMEOUT, mission)
    audit.candidates_count = len(candidates)

    amb_count, _top = _count_ambiguous_candidates(entity, candidates)
    if amb_count >= 2:
        audit.ambiguity = True
        audit.blocked_reason = ENTITY_AMBIGUOUS
        audit.resolved = False
        out.attempted = True
        out.blocked_ambiguity = True
        out.failure_reason = ENTITY_AMBIGUOUS
        out.human_message = (
            "Hay varias opciones similares en pantalla; indica cuál elegir."
        )
        record_entity_runtime_failure(
            mission, step, entity, strategy="ambiguity",
            context=_build_runtime_context(step, state, mission),
            ambiguous=True,
        )
        append_entity_runtime_audit(mission, audit)
        return out

    ctx = _build_runtime_context(step, state, mission)
    resolution = resolve_entity_runtime_ordered(
        entity,
        candidates,
        context=ctx,
        mission=mission,
        step=step,
        state=state,
    )

    audit.resolved = bool(resolution.resolved)
    audit.confidence = float(resolution.confidence or 0.0)
    audit.strategy_used = str(resolution.strategy or "")
    audit.fallback_used = bool(resolution.used_legacy_fallback)
    out.resolved = resolution.resolved
    out.confidence = audit.confidence
    out.strategy_used = audit.strategy_used
    out.fallback_used = audit.fallback_used

    if not resolution.resolved or resolution.confidence < RUNTIME_MATCH_MIN:
        audit.blocked_reason = ENTITY_NOT_FOUND
        out.failure_reason = ENTITY_NOT_FOUND
        record_entity_runtime_failure(
            mission, step, entity,
            strategy=resolution.strategy or "unresolved",
            context=ctx, ambiguous=False,
        )
        append_entity_runtime_audit(mission, audit)
        out.attempted = True
        return out

    match = match_entity_against_runtime(entity, candidates, context=ctx)
    rt = dict(match.runtime_target or {})
    rt["_erl_strategy"] = resolution.strategy

    if resolution.confidence < RUNTIME_EXECUTE_MIN:
        audit.blocked_reason = "execute_threshold_not_met"
        append_entity_runtime_audit(mission, audit)
        out.attempted = True
        out.resolved = True
        return out

    _inject_entity_params(step, entity)
    exec_res = execute_entity_target_click(rt, entity)
    out.strategy_result = exec_res
    audit.method = "entity_resolution"

    if exec_res.ok:
        out.executed = True
        out.skip_legacy_coords = True
        audit.memory_updated = True
        record_entity_runtime_success(
            mission, step, entity,
            strategy=resolution.strategy,
            runtime_target=rt,
            context=ctx,
        )
    else:
        audit.blocked_reason = exec_res.message or "click_failed"
        record_entity_runtime_failure(
            mission, step, entity,
            strategy=resolution.strategy,
            context=ctx, ambiguous=False,
        )

    append_entity_runtime_audit(mission, audit)
    out.attempted = True
    return out


def expert_audit_lines(mission: Any) -> List[str]:
    """Líneas para Mission Review experto."""
    rows = list(getattr(mission, "entity_runtime_resolution_audit", None) or [])
    lines: List[str] = []
    for r in rows[-20:]:
        if not isinstance(r, dict):
            continue
        if not r.get("attempted"):
            continue
        lines.append(
            f"ERL step={r.get('step_id', '?')[:12]} "
            f"ent={str(r.get('display_name', ''))[:40]} "
            f"conf={float(r.get('confidence') or 0):.2f} "
            f"strat={r.get('strategy_used') or '-'} "
            f"resolved={r.get('resolved')} "
            f"fallback={r.get('fallback_used')}"
        )
    return lines


__all__ = [
    "ADDRESS_SURFACE_NOT_READY",
    "CONTEXT_CREATION_TIMEOUT",
    "ENTITY_AMBIGUOUS",
    "ENTITY_CONTEXT_ALREADY_ACTIVE",
    "ENTITY_CONTEXT_NOT_VERIFIABLE",
    "ENTITY_NOT_FOUND",
    "ENTITY_RUNTIME_TIMEOUT",
    "ENTITY_SELECTOR_NOT_VISIBLE",
    "NO_STATE_PROGRESS",
    "BROWSE_VIEWPORT_NOT_CHANGED",
    "OPEN_TAB_NOT_CONFIRMED",
    "SEARCH_INPUT_NOT_FOCUSED",
    "SEARCH_RESULTS_NOT_CONFIRMED",
    "SEARCH_SUBMIT_NOT_CONFIRMED",
    "is_context_creation_step",
    "is_surface_operation_step",
    "sanitize_context_creation_failure_reason",
    "sanitize_non_entity_step_failure_reason",
    "sanitize_surface_operation_failure_reason",
    "surface_operation_failure_reason",
    "EntityRuntimeAuditEntry",
    "EntityRuntimeBudget",
    "EntityRuntimeHookResult",
    "append_entity_runtime_audit",
    "collect_runtime_candidates",
    "entity_from_mission_step",
    "entity_runtime_budget_from_settings",
    "execute_entity_target_click",
    "expert_audit_lines",
    "filter_strategies_when_entity_present",
    "is_unsafe_entity_auto_execute",
    "record_entity_runtime_failure",
    "record_entity_runtime_success",
    "resolve_entity_runtime_ordered",
    "try_entity_first_runtime",
]
