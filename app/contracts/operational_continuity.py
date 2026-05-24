"""Operational Continuity Runtime (OCR) — contratos modo sombra (Fase 1).

No ejecuta pasos DOM/coords; describe continuidad operacional declarativa
alineada con ORG, TEL y execution truth.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class OperationalContinuityState(BaseModel):
    """Instantánea lógica de continuidad (no física/layout)."""

    model_config = ConfigDict(extra="ignore")

    state_id: str = Field(default="", description="Id estable dentro del snapshot OCR.")
    operational_state_label: str = ""
    cumulative_progress_hint: float = Field(default=0.0, ge=0.0, le=1.0)
    invariant_tags: List[str] = Field(default_factory=list)
    validity_under_declared_truth: bool = True
    notes: str = ""


class OperationalGoalExecution(BaseModel):
    """Ejecución simulada de un goal operacional sobre ORG (sombra)."""

    model_config = ConfigDict(extra="ignore")

    goal_id: str = ""
    headline: str = ""
    desired_terminal_states: List[str] = Field(default_factory=list)
    simulation_path_states: List[str] = Field(default_factory=list)
    progress_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    satisfied_shadow: bool = False
    blocked_reason: str = ""


class OperationalTransitionAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transition_id: str = ""
    from_operational_state: str = ""
    to_operational_state: str = ""
    legitimacy_score: float = Field(default=0.0, ge=0.0, le=1.0)
    declared_in_org: bool = False
    would_validate_shadow: bool = False
    rationale: str = ""


class OperationalRecoveryAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    recovery_edge_id: str = ""
    recovery_tier: str = Field(
        default="structural",
        description="semantic | structural | procedural (orden preferido OCR).",
    )
    symptom_state: str = ""
    target_state: str = ""
    recovery_class: str = ""
    description: str = ""
    evidence_sources: List[str] = Field(default_factory=list)


class OperationalContinuitySnapshot(BaseModel):
    """Vista única OCR para auditoría Mission / UI experto."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(default="ocr_shadow_1", description="Versión OCR sombra.")

    continuity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    continuity_stability_score: float = Field(default=0.0, ge=0.0, le=1.0)

    inferred_current_operational_state: str = ""
    desired_operational_hint: str = ""
    active_goal_headline: str = ""

    predicted_next_states: List[str] = Field(default_factory=list)

    drift_detections: List[str] = Field(default_factory=list)
    continuity_recoveries: List[OperationalRecoveryAttempt] = Field(default_factory=list)
    continuity_breaks: List[str] = Field(default_factory=list)

    replay_dependence_estimate: float = Field(default=0.0, ge=0.0, le=1.0)
    operational_autonomy_score: float = Field(default=0.0, ge=0.0, le=1.0)

    metrics: Dict[str, float] = Field(default_factory=dict)
    continuity_states: List[OperationalContinuityState] = Field(default_factory=list)

    goal_execution_shadow: Optional[OperationalGoalExecution] = None
    transition_attempts_sample: List[OperationalTransitionAttempt] = Field(default_factory=list)

    ocr_vs_sep: Dict[str, Any] = Field(default_factory=dict)
    build_sources_echo: List[str] = Field(default_factory=list)

    generated_at_iso: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


__all__ = [
    "OperationalContinuitySnapshot",
    "OperationalContinuityState",
    "OperationalGoalExecution",
    "OperationalRecoveryAttempt",
    "OperationalTransitionAttempt",
]
