"""
ArthurOS Contracts: Intent
--------------------------
Define el formato único para expresar qué quiso decir el usuario, sin importar
si la entrada viene de voz, texto, API, etc.

Este módulo NO ejecuta lógica; solo define el "idioma" común del sistema.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict
from pydantic import BaseModel, Field, ConfigDict


class IntentType(str, Enum):
    UNKNOWN = "unknown"
    CONTROL = "control"       # "Cierra la ventana"
    QUERY = "query"           # "Qué hora es"
    WORKFLOW = "workflow"     # "Investiga sobre X"
    CHAT = "chat"             # "Hola, cómo estás"


class UserIntent(BaseModel):
    """
    Intención normalizada del usuario.
    - correlation_id: une este intent con toolcalls/result/events del mismo turno.
    """
    model_config = ConfigDict(extra="forbid")

    raw_query: str = Field(..., description="Lo que dijo el usuario literalmente")
    intent_type: IntentType = Field(default=IntentType.UNKNOWN)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    detected_language: str = Field(default="es")
    correlation_id: str = Field(
        default="",
        description="ID para trazar el flujo completo (intent->toolcall->result->events).",
    )
