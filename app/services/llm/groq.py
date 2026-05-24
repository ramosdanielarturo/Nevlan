"""
ArthurOS Services - Groq Provider
---------------------------------
Proveedor de alta velocidad usando Groq Cloud.

- Implementa tool calling (formato compatible OpenAI).
- Normaliza la respuesta a LLMResponse.
- FIX: Desactiva parallel_tool_calls para evitar errores de sintaxis en Llama 3.

Dependencia: `pip install groq`
"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional, Sequence

from app.core.config import settings
from app.core.logger import log
from app.services.llm.base import JSONDict, LLMProvider, LLMResponse, LLMToolCall

try:
    from groq import Groq
except Exception:  # pragma: no cover
    Groq = None  # type: ignore


class GroqProvider(LLMProvider):
    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        if Groq is None:
            raise ImportError("Falta dependencia 'groq'. Instala con: pip install groq")

        resolved_key = api_key or getattr(settings, "GROQ_API_KEY", None) or os.getenv("GROQ_API_KEY")
        if not resolved_key:
            raise RuntimeError("GroqProvider: falta GROQ_API_KEY (en .env o variable de entorno)")

        self.client = Groq(api_key=resolved_key)

        # Usamos el modelo más reciente y estable
        self.model = (
            model
            or getattr(settings, "GROQ_MODEL", None)
            or os.getenv("GROQ_MODEL")
            or "llama-3.3-70b-versatile"
        )

    def generate_text(self, prompt: str, system_prompt: str = "") -> str:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt or ""},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
            )
            msg = resp.choices[0].message
            return (msg.content or "").strip()
        except Exception as e:
            log.error(f"GroqProvider.generate_text error: {e}")
            return f"Error generando texto: {e}"

    def _parse_tool_calls(self, message: Any) -> List[LLMToolCall]:
        tool_calls: List[LLMToolCall] = []
        raw_calls = getattr(message, "tool_calls", None) or []
        for tc in raw_calls:
            try:
                call_id = getattr(tc, "id", "") or ""
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", None) or ""
                raw_args = getattr(fn, "arguments", None) or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except Exception:
                    args = {}
                tool_calls.append(LLMToolCall(id=call_id, name=name, arguments=args))
            except Exception:
                continue
        return tool_calls

    def generate_reply(
        self,
        messages: Sequence[JSONDict],
        tools: Optional[Sequence[JSONDict]] = None,
        temperature: float = 0.3,
    ) -> LLMResponse:
        try:
            # FIX CRÍTICO: parallel_tool_calls=False estabiliza Llama 3 en Groq
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=list(messages),
                tools=list(tools) if tools else None,
                tool_choice="auto" if tools else None,
                temperature=temperature,
                parallel_tool_calls=False if tools else None 
            )
            msg = resp.choices[0].message
            content = (msg.content or "").strip()
            tool_calls = self._parse_tool_calls(msg)
            return LLMResponse(content=content, tool_calls=tool_calls)
        except Exception as e:
            log.error(f"GroqProvider.generate_reply error: {e}")
            # Retornamos un mensaje de error amigable para que la UI no explote
            return LLMResponse(content="Tuve un problema técnico conectando con mi cerebro. ¿Podrías intentar de nuevo?")