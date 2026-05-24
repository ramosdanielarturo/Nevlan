"""
ArthurOS Contracts: ToolCall
----------------------------
Define el formato exacto para pedirle al sistema que ejecute una herramienta.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, ConfigDict


class ToolCall(BaseModel):
    """
    Petición de ejecución de una herramienta registrada.
    Nota: reasoning es útil para debug, pero NO debe contener secretos.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    # CORRECCIÓN: Usamos 'str' en lugar de 'UUID4' para aceptar IDs de Groq/OpenAI
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    
    tool_name: str = Field(..., description="Nombre exacto de la tool en el registro")
    arguments: Dict[str, Any] = Field(default_factory=dict)
    reasoning: Optional[str] = Field(
        default=None,
        description="Por qué se quiere usar esta tool (no incluir secretos).",
    )
    correlation_id: str = Field(
        default="",
        description="ID para trazar el flujo completo (intent->toolcall->result->events).",
    )