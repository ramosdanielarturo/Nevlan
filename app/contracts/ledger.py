"""
ArthurOS Contracts: Run Ledger & Execution
------------------------------------------
Define modelos para trackear ejecuciones, histórico de jobs y evidencias
para reportería y auditoría del comportamiento RPA.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field, ConfigDict


class JobSource(str, Enum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    EVENT_TRIGGER = "event_trigger"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobInstance(BaseModel):
    """Representa una intención de ejecución de una misión."""
    model_config = ConfigDict(extra="forbid")
    
    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    mission_id: str
    version_snapshot: int = Field(..., description="Versión de la misión en este punto")
    source: JobSource
    trigger_id: Optional[str] = None
    
    status: JobStatus = JobStatus.QUEUED
    queued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    executor_node: str = "local"


class RunRecord(BaseModel):
    """Representa el resultado final (La bitácora/Ledger) de un Job ejecutado."""
    model_config = ConfigDict(extra="forbid")
    
    run_id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    job_id: str
    mission_id: str
    
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    duration_ms: int = 0
    
    status: JobStatus = JobStatus.RUNNING
    
    # Métricas Resiliencia
    total_steps: int = 0
    steps_executed: int = 0
    retries_used: int = 0
    fallbacks_triggered: int = 0
    validations_passed: int = 0
    
    # Error Trace
    error_message: Optional[str] = None
    failed_step_id: Optional[str] = None
    
    # Evidencia
    evidence_refs: List[str] = Field(default_factory=list, description="Rutas a screenshots de éxito/fallo")
    context_variables: Dict[str, Any] = Field(default_factory=dict, description="Datos extraídos en vuelo")
