"""
ArthurOS Contracts: SystemEvent
-------------------------------
Eventos internos del sistema (pub/sub). No llevan lógica, solo estructura.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from pydantic import BaseModel, Field, ConfigDict


class SystemEvent(BaseModel):
    """
    Evento del sistema.
    - timestamp: hora en UTC (timezone-aware)
    - source: quién lo emitió (system/ui/brain/tool:xxx)
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description='Ej: "app.started", "speech.detected"')
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = Field(default="system")
    correlation_id: str = Field(
        default="",
        description="ID para trazar el flujo completo (intent->toolcall->result->events).",
    )
