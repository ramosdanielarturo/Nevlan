"""
ArthurOS Services - LLM Base Interface
--------------------------------------
Contrato común para proveedores LLM (Groq / OpenAI / Ollama).

Objetivo:
- Mantener al Agent agnóstico al proveedor.
- Normalizar la salida (texto + tool calls) a un modelo simple.
- Evitar dependencias cruzadas entre capas.

Notas:
- 'messages' sigue el formato estilo OpenAI:
  [{"role": "system"|"user"|"assistant"|"tool", "content": "...", ...}]
- 'tools' sigue el formato de function calling (OpenAI/Groq compatible).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

JSONDict = Dict[str, Any]


@dataclass(frozen=True)
class LLMToolCall:
    """Representa una llamada a herramienta solicitada por el LLM."""
    id: str
    name: str
    arguments: JSONDict = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Respuesta normalizada del LLM."""
    content: str = ""
    tool_calls: List[LLMToolCall] = field(default_factory=list)


class LLMProvider(ABC):
    """Interfaz base de un proveedor LLM."""

    @abstractmethod
    def generate_text(self, prompt: str, system_prompt: str = "") -> str:
        """Genera una respuesta de texto simple (sin tool calling)."""
        raise NotImplementedError

    @abstractmethod
    def generate_reply(
        self,
        messages: Sequence[JSONDict],
        tools: Optional[Sequence[JSONDict]] = None,
        temperature: float = 0.3,
    ) -> LLMResponse:
        """
        Genera una respuesta (puede incluir tool calls).
        Retorna un LLMResponse normalizado.
        """
        raise NotImplementedError
