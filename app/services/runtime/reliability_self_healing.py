"""Self-healing helpers — tolerancia y scoring sin nueva capa cognitiva.

Se apoya en señales ya existentes (LOP, execution_truth, ambigüedad, ORG,
ARL). Las funciones son puras / deterministas para tests y para futuros
hooks en SmartExecutor."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


class SelfHealingStep(str, Enum):
    """Orden antes de pedir repair / re-record sólo-del-bloque (beta privada Nevlan).

    Consolidación ``provider/context`` en un sólo escalón (sin tab/window aparte).
    """

    SEMANTIC_RELOCATION = "semantic_relocation"
    LIVE_ENTITY_REMATCH = "live_entity_rematch"
    CONTINUITY_RECOVERY = "continuity_recovery"
    VALIDATOR_ADAPTATION = "validator_adaptation"
    ALTERNATIVE_TRANSITION = "alternative_transition"
    PROVIDER_CONTEXT_RECOVERY = "provider_context_recovery"
    FOCUS_RECOVERY = "focus_recovery"
    LEARNED_STRATEGY_RETRY = "learned_strategy_retry"
    MICRO_REPAIR = "micro_repair"
    RERECORD_BLOCK_ONLY = "rerecord_block_only"


SELF_HEALING_BEFORE_REPAIR_ORDER: Tuple[SelfHealingStep, ...] = (
    SelfHealingStep.SEMANTIC_RELOCATION,
    SelfHealingStep.LIVE_ENTITY_REMATCH,
    SelfHealingStep.CONTINUITY_RECOVERY,
    SelfHealingStep.VALIDATOR_ADAPTATION,
    SelfHealingStep.ALTERNATIVE_TRANSITION,
    SelfHealingStep.PROVIDER_CONTEXT_RECOVERY,
    SelfHealingStep.FOCUS_RECOVERY,
    SelfHealingStep.LEARNED_STRATEGY_RETRY,
    SelfHealingStep.MICRO_REPAIR,
    SelfHealingStep.RERECORD_BLOCK_ONLY,
)


def self_healing_steps_before_repair() -> Tuple[SelfHealingStep, ...]:
    return SELF_HEALING_BEFORE_REPAIR_ORDER


def should_attempt_self_healing_before_repair(
    *,
    arl_rounds_used: int,
    arl_cap: int,
    coords_only_residual: bool,
    loops_detected: int,
) -> bool:
    """Evita bucles: no insistir si sólo quedan coords ciegos o ARL agotado."""
    if loops_detected >= 3:
        return False
    if coords_only_residual:
        return False
    if arl_cap > 0 and arl_rounds_used >= arl_cap:
        return False
    return True


def validator_drift_tolerance_multiplier(*, drift_hints: int, base: float = 1.0) -> float:
    """Amplía margen de validación ante deriva leve (layout / tema)."""
    d = max(0, int(drift_hints))
    return float(min(2.2, base + 0.08 * min(d, 8)))


def layout_shift_tolerance_score(*, lop_confidence: float, layout_shift_events: int) -> float:
    """0–1: cuánto toleramos cambio de layout con la confianza LOP actual."""
    lc = max(0.0, min(1.0, float(lop_confidence)))
    ls = max(0, int(layout_shift_events))
    penalty = min(0.45, 0.06 * ls)
    return max(0.0, min(1.0, 0.55 * lc + 0.35 - penalty))


def recovery_path_score(
    *,
    arl_recoveries: int,
    continuity_preserved: bool,
    fallback_pressures: int,
    validator_failures: int,
) -> float:
    """Ranking simple de trayectorias de recuperación (mayor = mejor)."""
    score = 0.55
    score += min(0.25, 0.06 * max(0, arl_recoveries))
    if continuity_preserved:
        score += 0.12
    score -= min(0.3, 0.04 * max(0, fallback_pressures))
    score -= min(0.2, 0.05 * max(0, validator_failures))
    return max(0.0, min(1.0, score))


def adaptive_wait_stabilization_ms(*, base_ms: float, volatility_hint: float) -> float:
    """Espera estabilizadora ante carga lenta / jitter (cap suave)."""
    v = max(0.0, min(1.0, float(volatility_hint)))
    extra = 120.0 + 380.0 * v
    return float(min(3500.0, max(base_ms, base_ms + extra)))


def stale_target_invalidation_hint(*, last_seen_age_s: float, dom_revision_delta: int) -> bool:
    """Heurística barata para invalidar target obsoleto antes de re-match."""
    if last_seen_age_s > 8.0:
        return True
    if dom_revision_delta >= 2:
        return True
    return False


def scroll_continuity_recovery_hint(*, scroll_delta_expected: int, scroll_observed: int) -> str:
    """Sugerencia textual para recuperación de scroll (telemetría / UI experto)."""
    delta = abs(int(scroll_observed) - int(scroll_delta_expected))
    if delta <= 1:
        return "scroll_continuity_ok"
    if delta <= 4:
        return "scroll_continuity_minor_resync"
    return "scroll_continuity_major_resync"


def ambiguity_downgrade_recovery_allowed(*, aggregate_ambiguity: float, threshold: float = 0.62) -> bool:
    """Si ambigüedad baja del umbral tras re-evaluación, permitir otro intento SAFE."""
    return float(aggregate_ambiguity) < float(threshold)


def semantic_relocation_boost(*, org_edges: int, goor_hints: int) -> float:
    """Boost acotado cuando hay grafo objetivo / GOOR sombra."""
    e = max(0, int(org_edges))
    g = max(0, int(goor_hints))
    return float(min(0.35, 0.02 * min(e, 8) + 0.03 * min(g, 4)))


@dataclass
class ContinuityRetryBudget:
    """Preserva continuidad sin bucles infinitos."""

    max_retries_per_step: int = 3
    attempts: Dict[str, int] = field(default_factory=dict)

    def can_retry(self, step_key: str) -> bool:
        return int(self.attempts.get(step_key, 0)) < self.max_retries_per_step

    def register_attempt(self, step_key: str) -> None:
        self.attempts[step_key] = int(self.attempts.get(step_key, 0)) + 1


def live_entity_rematch_priority(*, strategies_ranked: Sequence[str]) -> List[str]:
    """Prioriza estrategias no-coords antes que coords en re-resolución viva."""
    out: List[str] = []
    coords_like = []
    for s in strategies_ranked:
        sl = str(s).lower()
        if any(x in sl for x in ("coords", "vision_", "visual_", "click_fallback")):
            coords_like.append(s)
        else:
            out.append(s)
    return out + coords_like


__all__ = [
    "SELF_HEALING_BEFORE_REPAIR_ORDER",
    "SelfHealingStep",
    "adaptive_wait_stabilization_ms",
    "ambiguity_downgrade_recovery_allowed",
    "layout_shift_tolerance_score",
    "live_entity_rematch_priority",
    "recovery_path_score",
    "scroll_continuity_recovery_hint",
    "semantic_relocation_boost",
    "self_healing_steps_before_repair",
    "should_attempt_self_healing_before_repair",
    "stale_target_invalidation_hint",
    "validator_drift_tolerance_multiplier",
    "ContinuityRetryBudget",
]
