"""Goal-Oriented Operational Runtime (GOOR) — contratos Fase 1 (sombra).

No ejecuta input real ni muta SEP; describe objetivos operacionales, progreso,
transiciones candidatas y recuperaciones alineadas con ORG / TEL / OCR.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class OperationalGoalState(BaseModel):
    """Estado operacional dentro del modelo de goals GOOR."""

    model_config = ConfigDict(extra="ignore")

    state_key: str = Field(description="Operational state derivado ORG/truth/TEL.")
    sourced_from_truth_type: str = ""
    sourced_from_truth_id: str = ""
    validators_expected: List[str] = Field(default_factory=list)


class OperationalGoalProgress(BaseModel):
    """Medida de avance multi-dimensional hacia un goal declarativo."""

    model_config = ConfigDict(extra="ignore")

    goal_id: str = ""
    goal_headline: str = ""

    progress_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    continuity_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity: float = Field(default=0.0, ge=0.0, le=1.0)
    recovery_pressure: float = Field(default=0.0, ge=0.0, le=1.0)
    validator_alignment: float = Field(default=0.0, ge=0.0, le=1.0)
    operational_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    desired_terminal_states: List[str] = Field(default_factory=list)
    inferred_current_states: List[str] = Field(default_factory=list)


class OperationalGoalTransition(BaseModel):
    """Arco GOOR candidato: variante válida hacia estado deseado (no replay step único)."""

    model_config = ConfigDict(extra="ignore")

    transition_id: str = ""
    variant_label: str = Field(default="", description="Etiqueta explicativa; sin rutas DOM rígidas.")
    from_operational_state: str = ""
    to_operational_state: str = ""

    promising_score: float = Field(default=0.0, ge=0.0, le=1.0)
    distance_to_goal_terminals: Optional[int] = Field(
        default=None,
        description="Saltos declarativos hasta el primer terminal cuando aplica.",
    )

    legitimacy_from_org: float = Field(default=0.0, ge=0.0, le=1.0)
    ompc_alignment: float = Field(default=0.0, ge=0.0, le=1.0)
    sll_rank_prior: float = Field(default=0.0, ge=0.0, le=1.0)

    legitimate_surface_classes: List[str] = Field(default_factory=list)
    expected_validator_keys: List[str] = Field(default_factory=list)
    rationale: str = ""

    declares_dynamic_path: bool = Field(
        default=True,
        description="True cuando el camino puede satisfacer el goal de múltiples maneras válidas.",
    )


class OperationalGoalRecovery(BaseModel):
    """Recuperación orientada a objetivo — no repetición procedural ciega de click."""

    model_config = ConfigDict(extra="ignore")

    recovery_edge_id: str = ""
    goal_id_hint: str = ""
    symptom_state: str = ""
    target_state: str = ""
    recovery_class: str = ""
    tier: str = Field(default="structural", description="semantic | structural | procedural")
    description: str = ""
    goal_orientation_score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_sources: List[str] = Field(default_factory=list)


class OperationalDecisionContext(BaseModel):
    """Contexto estable para auditoría GOOR."""

    model_config = ConfigDict(extra="ignore")

    execution_truth_echo: Dict[str, Any] = Field(default_factory=dict)
    ocr_continuity_score: float = 0.0
    org_metric_echo: Dict[str, float] = Field(default_factory=dict)
    ompc_hints: Dict[str, Any] = Field(default_factory=dict)
    sll_echo: Dict[str, Any] = Field(default_factory=dict)
    arl_recovery_pressure: float = Field(default=0.0, ge=0.0, le=1.0)
    validator_history_drift: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity_score_aggregate: float = Field(default=0.0, ge=0.0, le=1.0)
    replay_pressure: float = Field(default=0.0, ge=0.0, le=1.0)

    inferred_current_operational_state: str = ""

    # Live Operational Perception (Fase 1 sombra) — eco compacto del último snapshot LOP.
    live_operational_perception_echo: Dict[str, Any] = Field(
        default_factory=dict,
        description="Resumen no mutante del lop_last_snapshot de la misión (surface, acción, seguridad).",
    )
    # LOP Fase 2 — snapshot vivo serializado (subset acotado; GOOR/UI puede truncar vistas).
    lop_live_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        description="Copia estable del último lop_last_snapshot cuando existe feed de sensores.",
    )


class OperationalGoalExecutionSnapshot(BaseModel):
    """Snapshot GOOR persistente para Mission Review / auditoría."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(default="goor_shadow_1")

    operational_decision_context: OperationalDecisionContext = Field(
        default_factory=OperationalDecisionContext,
    )
    goal_states: List[OperationalGoalState] = Field(default_factory=list)
    active_goal_progress: List[OperationalGoalProgress] = Field(default_factory=list)

    ranked_candidate_transitions: List[OperationalGoalTransition] = Field(default_factory=list)
    chosen_transition: Optional[OperationalGoalTransition] = None
    goal_recoveries: List[OperationalGoalRecovery] = Field(default_factory=list)

    stall_signals: List[str] = Field(default_factory=list)
    continuity_notes: List[str] = Field(default_factory=list)

    metrics: Dict[str, float] = Field(default_factory=dict)
    goor_vs_sep_comparison: Dict[str, Any] = Field(default_factory=dict)

    replay_dependency_estimate: float = Field(default=0.0, ge=0.0, le=1.0)
    operational_autonomy_estimate: float = Field(default=0.0, ge=0.0, le=1.0)

    build_sources_echo: List[str] = Field(default_factory=list)

    generated_at_iso: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


__all__ = [
    "OperationalDecisionContext",
    "OperationalGoalExecutionSnapshot",
    "OperationalGoalProgress",
    "OperationalGoalRecovery",
    "OperationalGoalState",
    "OperationalGoalTransition",
]
