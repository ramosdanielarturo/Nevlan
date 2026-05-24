"""
ArthurOS Services - RecoveryEngine (Smart Executor)
---------------------------------------------------
Recetas de recuperación cuando una **precondition** o **postcondition**
no se cumple. La regla central es:

  - escalamos de la estrategia "rápida" a la "robusta"
  - nunca repetimos una estrategia ya fallida más de 2 veces
  - si la receta no logra remontar, decimos "vuelve al último checkpoint"
  - si tampoco hay checkpoint válido, devolvemos ``escalate_human=True``
    para que el SmartExecutor pare y pida intervención

API:
    engine = RecoveryEngine(action_runner, checkpoints, learning)
    plan = engine.recover_precondition(step, contract, state, attempts)
    plan = engine.recover_postcondition(step, contract, state_after, attempts)

Devuelve un ``RecoveryPlan`` que incluye:
  - ``next_strategies`` (en el orden a probar)
  - ``rollback_to_checkpoint`` (key o None)
  - ``escalate_human`` (bool) + ``reason``
  - ``human_message`` (texto para UX)

El SmartExecutor consume este plan: intenta las estrategias, y si todas
fallan, vuelve al checkpoint o pide humano.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from app.core.logger import log
from app.services.missions.execution_contracts import (
    MissionStep,
    StepContract,
)
from app.services.missions.state_detector import StateSnapshot

if TYPE_CHECKING:  # pragma: no cover
    from app.services.missions.action_runner import ActionRunner
    from app.services.missions.checkpoints import CheckpointManager
    from app.services.missions.execution_learning import LearningLogger


# Tope global PRD: ninguna estrategia se intenta más de N veces totales
# (incluye reintento natural + recuperación). 2 es el número del PRD.
MAX_PER_STRATEGY = 2


# ──────────────────────────────────────────────────────────────────────
# RecoveryPlan
# ──────────────────────────────────────────────────────────────────────

@dataclass
class RecoveryPlan:
    """Plan que el ``SmartMissionExecutor`` consume.

    Campos:
      - ``next_strategies``: orden recomendado para reintentar el step.
        Puede estar vacío si la receta determina que sólo cabe rollback
        o escalación.
      - ``rollback_to_checkpoint``: key del checkpoint al que volver
        (si lo hay). El executor decidirá si re-ejecutar pasos del
        bloque desde ese checkpoint.
      - ``escalate_human``: True ⇒ STOP, pedir intervención.
      - ``reason``: explicación corta (se loggea).
      - ``human_message``: texto amigable que la UX muestra al usuario.
      - ``correction_label``: identificador para el LearningLogger.
    """

    next_strategies: List[str] = field(default_factory=list)
    rollback_to_checkpoint: Optional[str] = None
    escalate_human: bool = False
    reason: str = ""
    human_message: str = ""
    correction_label: str = ""


# ──────────────────────────────────────────────────────────────────────
# Recipe type
# ──────────────────────────────────────────────────────────────────────

RecipeFn = "Callable[[RecoveryEngine, MissionStep, StepContract, StateSnapshot, Dict[str, int], str], RecoveryPlan]"


# ──────────────────────────────────────────────────────────────────────
# RecoveryEngine
# ──────────────────────────────────────────────────────────────────────

class RecoveryEngine:
    """Orquesta recetas de recuperación por ``recovery_strategy``.

    Recibe en su constructor las dependencias compartidas — así puede
    consultar el LearningLogger para reordenar fallbacks y el
    CheckpointManager para sugerir rollback. NO ejecuta las estrategias
    aquí; sólo devuelve el plan.
    """

    def __init__(
        self,
        action_runner: "ActionRunner",
        checkpoints: "CheckpointManager",
        learning: "LearningLogger",
    ) -> None:
        self.action_runner = action_runner
        self.checkpoints = checkpoints
        self.learning = learning

    # ── API pública ──────────────────────────────────────────────
    def recover_precondition(
        self,
        step: MissionStep,
        contract: StepContract,
        state: StateSnapshot,
        attempts: Dict[str, int],
    ) -> RecoveryPlan:
        """¿Qué hacer cuando la precondición NO se cumple?"""
        return self._dispatch(step, contract, state, attempts, phase="pre")

    def recover_postcondition(
        self,
        step: MissionStep,
        contract: StepContract,
        state_after: StateSnapshot,
        attempts: Dict[str, int],
    ) -> RecoveryPlan:
        """¿Qué hacer cuando la postcondición NO se cumple?"""
        return self._dispatch(step, contract, state_after, attempts, phase="post")

    # ── Despacho ────────────────────────────────────────────────
    def _dispatch(
        self,
        step: MissionStep,
        contract: StepContract,
        state: StateSnapshot,
        attempts: Dict[str, int],
        phase: str,
    ) -> RecoveryPlan:
        recipe = _RECIPES.get(contract.recovery_strategy)
        if recipe is None:
            recipe = _recipe_generic
        try:
            plan = recipe(self, step, contract, state, attempts, phase)
        except Exception as e:
            log.exception(f"recovery recipe '{contract.recovery_strategy}' raised")
            plan = RecoveryPlan(
                escalate_human=True,
                reason=f"recipe excepción: {e}",
                human_message="Algo se rompió en la recuperación, necesito ayuda.",
                correction_label=f"{contract.recovery_strategy}:exception",
            )
        # Filtramos estrategias agotadas: ``MAX_PER_STRATEGY`` veces
        # totales — esta es la regla "no repetir más de 2 veces".
        plan.next_strategies = [
            s for s in plan.next_strategies
            if attempts.get(s, 0) < MAX_PER_STRATEGY
        ]
        # Reordenamos por aprendizaje para que el orden histórico
        # gane sobre el orden estático del recipe (manteniendo skip
        # de las ya fallidas en exceso).
        if plan.next_strategies:
            plan.next_strategies = self.learning.rank_strategies(
                step.kind,
                plan.next_strategies,
            )
        # Si no quedan estrategias y no hay rollback, escalamos.
        if not plan.next_strategies and not plan.rollback_to_checkpoint and not plan.escalate_human:
            plan.escalate_human = True
            if not plan.reason:
                plan.reason = "agotamos estrategias y no hay checkpoint"
            if not plan.human_message:
                plan.human_message = (
                    "No pude continuar de forma segura. ¿Me ayudas un momento?"
                )
        return plan


# ──────────────────────────────────────────────────────────────────────
# Recetas por tipo
# ──────────────────────────────────────────────────────────────────────

def _remaining_strategies(
    contract: StepContract,
    attempts: Dict[str, int],
) -> List[str]:
    return [
        s for s in contract.all_strategies()
        if attempts.get(s, 0) < MAX_PER_STRATEGY
    ]


def _recipe_open_app(
    engine: "RecoveryEngine",
    step: MissionStep,
    contract: StepContract,
    state: StateSnapshot,
    attempts: Dict[str, int],
    phase: str,
) -> RecoveryPlan:
    name = (step.params.get("name") or "").strip() or "la aplicación"
    remaining = _remaining_strategies(contract, attempts)
    if not remaining:
        return RecoveryPlan(
            escalate_human=True,
            reason=f"open_app: ninguna estrategia disponible para {name}",
            human_message=f"No pude abrir {name}. ¿La abres tú y le doy continuar?",
            correction_label="recover_open_app:exhausted",
        )
    return RecoveryPlan(
        next_strategies=remaining,
        human_message=f"No detecté {name}, lo estoy abriendo.",
        correction_label="recover_open_app:retry",
    )


def _recipe_select_profile(
    engine: "RecoveryEngine",
    step: MissionStep,
    contract: StepContract,
    state: StateSnapshot,
    attempts: Dict[str, int],
    phase: str,
) -> RecoveryPlan:
    profile = (step.params.get("profile_name") or "").strip()
    # Si no tenemos nombre del perfil, no podemos adivinar. PRD: STOP.
    if not profile:
        return RecoveryPlan(
            escalate_human=True,
            reason="select_profile sin profile_name",
            human_message="No me dijiste qué perfil de Chrome usar. ¿Me ayudas a elegirlo?",
            correction_label="recover_select_profile:no_name",
        )
    # Si Chrome aún no muestra el picker, conviene esperar 1 detect más.
    # El executor lo hará reintentando; aquí ya devolvemos las estrategias.
    remaining = _remaining_strategies(contract, attempts)
    return RecoveryPlan(
        next_strategies=remaining,
        # Si hay checkpoint previo a chrome_profile_loaded — no lo hay
        # por definición — solo intentamos otra vez con escala visual.
        human_message=f"No vi el perfil '{profile}', vuelvo a intentar.",
        correction_label="recover_select_profile:retry",
    )


def _recipe_open_new_tab(
    engine: "RecoveryEngine",
    step: MissionStep,
    contract: StepContract,
    state: StateSnapshot,
    attempts: Dict[str, int],
    phase: str,
) -> RecoveryPlan:
    remaining = _remaining_strategies(contract, attempts)
    return RecoveryPlan(
        next_strategies=remaining,
        human_message="La pestaña no abrió, reintentando.",
        correction_label="recover_open_new_tab:retry",
    )


def _recipe_open_url(
    engine: "RecoveryEngine",
    step: MissionStep,
    contract: StepContract,
    state: StateSnapshot,
    attempts: Dict[str, int],
    phase: str,
) -> RecoveryPlan:
    from app.services.missions.execution_contracts import resolve_url_alias
    _url, expected_domain = resolve_url_alias(step)
    label = expected_domain or "la URL"
    remaining = _remaining_strategies(contract, attempts)

    # Si la pestaña actual no es del navegador, conviene volver al
    # checkpoint chrome_profile_loaded antes de reintentar.
    rollback = None
    if not state.is_process_running("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe"):
        if engine.checkpoints.has("chrome_profile_loaded"):
            rollback = "chrome_profile_loaded"

    return RecoveryPlan(
        next_strategies=remaining,
        rollback_to_checkpoint=rollback,
        human_message=f"No detecté {label}, lo estoy corrigiendo.",
        correction_label="recover_open_url:retry",
    )


def _recipe_search(
    engine: "RecoveryEngine",
    step: MissionStep,
    contract: StepContract,
    state: StateSnapshot,
    attempts: Dict[str, int],
    phase: str,
) -> RecoveryPlan:
    """Receta de recovery para ``search_youtube`` (PRD §8).

    Reglas:
      * Si la página actual NO es ``youtube.com`` (navegamos fuera por
        algún fallback), rollback al checkpoint ``youtube_loaded`` (o
        ``youtube.com_loaded``) antes de reintentar.
      * Si seguimos en YouTube pero la búsqueda falló, rotamos a los
        siguientes fallbacks del contrato.
      * Tras agotar estrategias (2 ciclos * N fallbacks), ``_dispatch``
        lo escalará a humano automáticamente.
    """
    rollback = None
    on_youtube = state.domain_matches("youtube.com") or state.url_contains("youtube.com")
    if not on_youtube:
        if engine.checkpoints.has("youtube.com_loaded"):
            rollback = "youtube.com_loaded"
        elif engine.checkpoints.has("youtube_loaded"):
            rollback = "youtube_loaded"

    remaining = _remaining_strategies(contract, attempts)
    return RecoveryPlan(
        next_strategies=remaining,
        rollback_to_checkpoint=rollback,
        human_message=(
            "No veo los resultados, vuelvo al último punto estable."
            if rollback else "No encontré la barra de búsqueda, reintentando."
        ),
        correction_label=(
            "recover_search:rollback" if rollback else "recover_search:retry"
        ),
    )


def _recipe_generic_click(
    engine: "RecoveryEngine",
    step: MissionStep,
    contract: StepContract,
    state: StateSnapshot,
    attempts: Dict[str, int],
    phase: str,
) -> RecoveryPlan:
    remaining = _remaining_strategies(contract, attempts)
    return RecoveryPlan(
        next_strategies=remaining,
        human_message="Reintentando…",
        correction_label="recover_generic_click:retry",
    )


def _recipe_generic(
    engine: "RecoveryEngine",
    step: MissionStep,
    contract: StepContract,
    state: StateSnapshot,
    attempts: Dict[str, int],
    phase: str,
) -> RecoveryPlan:
    remaining = _remaining_strategies(contract, attempts)
    return RecoveryPlan(
        next_strategies=remaining,
        human_message="Reintentando…",
        correction_label="recover_generic:retry",
    )


_RECIPES: Dict[str, Any] = {
    "recover_open_app": _recipe_open_app,
    "recover_select_profile": _recipe_select_profile,
    "recover_open_new_tab": _recipe_open_new_tab,
    "recover_open_url": _recipe_open_url,
    "recover_search": _recipe_search,
    "recover_generic_click": _recipe_generic_click,
    "generic": _recipe_generic,
}


def register_recipe(name: str, fn: Any) -> None:
    """Permite añadir recetas desde otros módulos."""
    _RECIPES[name] = fn


__all__ = [
    "RecoveryEngine",
    "RecoveryPlan",
    "register_recipe",
    "MAX_PER_STRATEGY",
]
