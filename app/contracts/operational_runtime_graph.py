"""Operational Runtime Graph (ORG) — contratos serialized en Mission (sombra Fase 1)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class OperationalRuntimeState(BaseModel):
    """Instantánea de progreso operacional durante simulación sombra."""

    model_config = ConfigDict(extra="ignore")

    state_id: str = ""
    node_id: str = ""
    cumulative_progress: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: Dict[str, Any] = Field(default_factory=dict)


class OperationalRuntimeGoal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal_id: str
    headline: str
    rationale: str = ""
    originating_truth_ids: List[str] = Field(default_factory=list)
    originating_session_ids: List[str] = Field(default_factory=list)
    originating_cob_ids: List[str] = Field(default_factory=list)
    sep_alignment_hint: str = ""
    ambiguity_notes: List[str] = Field(default_factory=list)


class OperationalRuntimeTransition(BaseModel):
    """Arco capacidad-agnóstico (no clic ni keypress)."""

    model_config = ConfigDict(extra="ignore")

    transition_id: str
    from_operational_state: str
    to_operational_state: str
    description: str = ""
    legitimate_surface_classes: List[str] = Field(
        default_factory=list,
        description="Familias válidas para satisfacer la transición (p.ej. url_surface, launcher_surface).",
    )
    expected_validator_keys: List[str] = Field(default_factory=list)


class OperationalRuntimeRecoveryEdge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    edge_id: str
    symptom_state: str
    target_state: str
    recovery_class: str = ""
    description: str = ""
    evidence_sources: List[str] = Field(default_factory=list)


class OperationalRuntimeNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str
    operational_state: str

    truth_block_id: str = ""
    truth_type: str = ""

    expected_validators: List[str] = Field(default_factory=list)
    acceptable_variants: List[str] = Field(default_factory=list)
    recovery_options: List[str] = Field(default_factory=list)
    transition_expectations: List[str] = Field(default_factory=list)

    execution_truth_snapshot: Dict[str, Any] = Field(default_factory=dict)
    ambiguity_snapshot: Dict[str, Any] = Field(default_factory=dict)

    stability_score: float = 0.5
    replay_anchor_candidates: List[str] = Field(default_factory=list)
    context_requirements: Dict[str, Any] = Field(default_factory=dict)
    success_signals: List[str] = Field(default_factory=list)
    failure_signals: List[str] = Field(default_factory=list)


class OperationalRuntimeGraph(BaseModel):
    """Grafo operacional — autoridad declarativa paralela al SEP procedural."""

    model_config = ConfigDict(extra="ignore")

    version: str = Field(default="1.0", description="Schema ORG serialized en Mission.")

    nodes: List[OperationalRuntimeNode] = Field(default_factory=list)
    transitions: List[OperationalRuntimeTransition] = Field(default_factory=list)
    goals: List[OperationalRuntimeGoal] = Field(default_factory=list)
    recovery_edges: List[OperationalRuntimeRecoveryEdge] = Field(default_factory=list)

    metrics: Dict[str, float] = Field(default_factory=dict)
    predicted_next_states: List[str] = Field(default_factory=list)

    shadows_runtime: bool = True
    build_sources_used: List[str] = Field(default_factory=list)


__all__ = [
    "OperationalRuntimeGraph",
    "OperationalRuntimeGoal",
    "OperationalRuntimeNode",
    "OperationalRuntimeRecoveryEdge",
    "OperationalRuntimeState",
    "OperationalRuntimeTransition",
]
