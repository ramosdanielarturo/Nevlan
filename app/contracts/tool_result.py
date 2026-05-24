"""
ArthurOS Contracts: ToolResult
------------------------------
Define el formato estándar de salida para cualquier herramienta, en éxito o error.
"""
from __future__ import annotations

from typing import Any, List, Optional
from pydantic import BaseModel, Field, ConfigDict


class ToolResult(BaseModel):
    """
    Respuesta estandarizada de una herramienta.
    Reglas:
    - success=True  => error debe ser None
    - success=False => result debe ser None
    """
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(..., description="ID del ToolCall que generó esto")
    success: bool = Field(default=True)
    result: Optional[Any] = Field(default=None)
    error: Optional[str] = Field(default=None)
    artifacts: List[str] = Field(
        default_factory=list,
        description="Rutas de archivos generados (idealmente relativas al sandbox).",
    )
    correlation_id: str = Field(
        default="",
        description="ID para trazar el flujo completo (intent->toolcall->result->events).",
    )

    def model_post_init(self, __context: Any) -> None:
        if self.success and self.error is not None:
            raise ValueError("ToolResult inválido: success=True pero error no es None")
        if (not self.success) and self.result is not None:
            raise ValueError("ToolResult inválido: success=False pero result no es None")

    def to_text(self) -> str:
        """Formato legible para que el Agente/LLM lo lea rápido."""
        if self.success:
            return f"✅ Output: {self.result}"
        return f"❌ Error: {self.error}"
