"""
Nevlan — Execution Confidence System (PRD 2026-05-12 §Fase 2E)
================================================================

Reemplaza los booleanos ambiguos del executor por **tres estados
explícitos** que la UI y el Player pueden interpretar sin adivinar:

  * :attr:`ExecutionConfidence.SAFE_TO_EXECUTE` — clickear libremente.
    Confianza estructural alta (UIA/DOM verified) o visual fuerte
    (fingerprint score ≥ 0.78). Sin safety_reason de bloqueo.

  * :attr:`ExecutionConfidence.SAFE_WITH_CONFIRMATION` — clickear pero
    pedir confirmación visual al humano antes. Confianza media
    (fingerprint score 0.60-0.77, ``needs_confirmation=True``) o
    coords-only en modo emergencia (sin fingerprint disponible).

  * :attr:`ExecutionConfidence.UNSAFE_DO_NOT_CLICK` — no clickear.
    La safety gate del resolver demotó el resultado a ``method="none"``,
    el fingerprint disponible falló al relocate, o no hay target
    válido. El Player debe abrir el modo reparación / preguntar.

Diseño:
  * Pure-function. ``compute_execution_confidence(resolution)`` no toca
    el SO, no llama probes; solo lee campos del
    :class:`TargetResolutionResult` ya producido.
  * Estable: el contrato es público y testeable.
  * Compatible con el Player existente: el método sigue siendo
    accesible vía ``result.method``; este módulo solo añade una capa
    semántica encima.

Política PRD:
  * UNSAFE NUNCA se debe pasar al ActionRunner como "intenta igual".
    Lo bloqueamos con un ``ExecutionDecision.deny`` explícito.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


__all__ = [
    "ExecutionConfidence",
    "ExecutionDecision",
    "compute_execution_confidence",
    "compute_execution_decision",
]


class ExecutionConfidence(Enum):
    """Tri-state explícito para el Player (PRD §2E)."""

    SAFE_TO_EXECUTE = "safe_to_execute"
    SAFE_WITH_CONFIRMATION = "safe_with_confirmation"
    UNSAFE_DO_NOT_CLICK = "unsafe_do_not_click"

    @property
    def is_safe(self) -> bool:
        """True si el Player puede proceder (con o sin confirmación)."""
        return self in (
            ExecutionConfidence.SAFE_TO_EXECUTE,
            ExecutionConfidence.SAFE_WITH_CONFIRMATION,
        )

    @property
    def requires_confirmation(self) -> bool:
        return self is ExecutionConfidence.SAFE_WITH_CONFIRMATION


@dataclass(frozen=True)
class ExecutionDecision:
    """Resultado de :func:`compute_execution_decision`.

    Atomic: un Player que recibe un :class:`ExecutionDecision` tiene
    todo lo necesario para decidir qué hacer sin re-leer el resolver.
    """

    confidence: ExecutionConfidence
    proceed: bool
    needs_confirmation: bool
    block_reason: str
    human_message: str
    # Pasamos también la resolution original para audit.
    method: str = "none"
    target_kind: str = "none"
    score: int = 0
    safety_reason: str = ""

    @property
    def is_safe(self) -> bool:
        return self.confidence.is_safe


# ──────────────────────────────────────────────────────────────────────
# Umbrales (PRD §2D)
# ──────────────────────────────────────────────────────────────────────

#: Score ≥ este valor en visual_fingerprint = strong → SAFE_TO_EXECUTE.
_VISUAL_STRONG_SCORE: int = 78

#: Métodos estructurales (UIA/DOM) — siempre SAFE_TO_EXECUTE si
#: score ≥ HIGH_CONFIDENCE_THRESHOLD del resolver (65).
_STRUCTURAL_METHODS: tuple = ("web_locator", "uia_exact", "uia_semantic")

#: safety_reasons que fuerzan SAFE_WITH_CONFIRMATION en vez de SAFE.
_REASONS_REQUIRE_CONFIRMATION: frozenset = frozenset({
    "visual_fingerprint_medium_confidence",
    "coords_emergency_no_fingerprint_available",
})

#: safety_reasons que fuerzan UNSAFE.
_REASONS_UNSAFE: frozenset = frozenset({
    "fingerprint_failed_no_blind_coords",
})


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────


def compute_execution_confidence(resolution: Any) -> ExecutionConfidence:
    """Mapea un :class:`TargetResolutionResult` a un estado tri-state.

    Reglas (PRD §2E):
      1. ``method == "none"`` o cualquier ``safety_reason`` en
         :data:`_REASONS_UNSAFE` → UNSAFE_DO_NOT_CLICK.
      2. ``safety_reason`` en :data:`_REASONS_REQUIRE_CONFIRMATION` →
         SAFE_WITH_CONFIRMATION.
      3. ``needs_confirmation = True`` → SAFE_WITH_CONFIRMATION.
      4. Método estructural (web/uia) con score ≥ 65 → SAFE_TO_EXECUTE.
      5. Método ``visual_fingerprint`` con score ≥ 78 → SAFE_TO_EXECUTE.
      6. Cualquier otro caso con score ≥ 65 y método != ``none`` →
         SAFE_WITH_CONFIRMATION (conservador).
      7. Score < 65 → UNSAFE_DO_NOT_CLICK.

    Args:
        resolution: ``TargetResolutionResult``-shaped. Solo se leen los
            atributos ``method``, ``score``, ``needs_confirmation``,
            ``safety_reason``. Acepta duck-typing para tests fáciles.

    Returns:
        :class:`ExecutionConfidence`.
    """
    method = getattr(resolution, "method", "none") or "none"
    score = int(getattr(resolution, "score", 0) or 0)
    needs_conf = bool(getattr(resolution, "needs_confirmation", False))
    safety = str(getattr(resolution, "safety_reason", "") or "")

    # Regla 1: UNSAFE explícito.
    if method == "none":
        return ExecutionConfidence.UNSAFE_DO_NOT_CLICK
    if safety in _REASONS_UNSAFE:
        return ExecutionConfidence.UNSAFE_DO_NOT_CLICK

    # Regla 2 & 3: safety_reason o needs_confirmation pide humano.
    if safety in _REASONS_REQUIRE_CONFIRMATION or needs_conf:
        return ExecutionConfidence.SAFE_WITH_CONFIRMATION

    # Regla 4: estructural fuerte.
    if method in _STRUCTURAL_METHODS and score >= 65:
        return ExecutionConfidence.SAFE_TO_EXECUTE

    # Regla 5: visual fuerte.
    if method == "visual_fingerprint" and score >= _VISUAL_STRONG_SCORE:
        return ExecutionConfidence.SAFE_TO_EXECUTE

    # Regla 6: conservador — pedir confirmación si no llegó a strong.
    if score >= 65:
        return ExecutionConfidence.SAFE_WITH_CONFIRMATION

    # Regla 7: score bajo → no clickear.
    return ExecutionConfidence.UNSAFE_DO_NOT_CLICK


def compute_execution_decision(
    resolution: Any,
    *,
    step_label: str = "",
) -> ExecutionDecision:
    """Versión "todo en uno" para el Player.

    Devuelve un :class:`ExecutionDecision` listo para consumir:
    si ``proceed`` es False, NO se debe clickear bajo ninguna razón.
    Si ``needs_confirmation`` es True, primero pedir confirmación.
    """
    conf = compute_execution_confidence(resolution)
    method = str(getattr(resolution, "method", "none") or "none")
    target_kind = str(getattr(resolution, "target_kind", "none") or "none")
    score = int(getattr(resolution, "score", 0) or 0)
    safety_reason = str(getattr(resolution, "safety_reason", "") or "")

    proceed = conf.is_safe
    needs_conf = conf.requires_confirmation

    if conf is ExecutionConfidence.UNSAFE_DO_NOT_CLICK:
        if safety_reason == "fingerprint_failed_no_blind_coords":
            human = (
                f"No encontré el target del paso «{step_label}». "
                "Confirma manualmente o regraba este paso."
            ) if step_label else (
                "No encontré el target. Confirma manualmente o regraba."
            )
            block_reason = "fingerprint_failed_no_blind_coords"
        elif method == "none":
            human = (
                f"No tengo evidencia suficiente para ejecutar «{step_label}»."
            ) if step_label else "No tengo evidencia suficiente para ejecutar."
            block_reason = "no_resolution"
        else:
            human = (
                f"Confianza baja en el target del paso «{step_label}»."
            ) if step_label else "Confianza baja en el target."
            block_reason = "low_score"
    elif conf is ExecutionConfidence.SAFE_WITH_CONFIRMATION:
        if safety_reason == "coords_emergency_no_fingerprint_available":
            human = (
                f"Voy a usar coordenadas grabadas para «{step_label}». "
                "Confirma que sigue siendo correcto."
            ) if step_label else (
                "Voy a usar coordenadas grabadas. Confirma que sigue siendo correcto."
            )
        else:
            human = (
                f"Encontré el target con confianza media. "
                f"Confirma para ejecutar «{step_label}»."
            ) if step_label else "Encontré el target con confianza media. Confirma para ejecutar."
        block_reason = ""
    else:  # SAFE_TO_EXECUTE
        human = ""
        block_reason = ""

    return ExecutionDecision(
        confidence=conf,
        proceed=proceed,
        needs_confirmation=needs_conf,
        block_reason=block_reason,
        human_message=human,
        method=method,
        target_kind=target_kind,
        score=score,
        safety_reason=safety_reason,
    )
