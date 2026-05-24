"""
ArthurOS Services - OpenAI Provider
----------------------------------
Proveedor para OpenAI (modelo de razonamiento/calidad).

- Implementa tool calling (formato OpenAI).
- Normaliza la respuesta a LLMResponse.

Dependencia: `pip install openai`
"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional, Sequence

from app.core.config import settings
from app.core.logger import log
from app.services.llm.base import JSONDict, LLMProvider, LLMResponse, LLMToolCall

try:
    # OpenAI Python SDK 1.x
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        if OpenAI is None:
            raise ImportError("Falta dependencia 'openai'. Instala con: pip install openai")

        resolved_key = api_key or getattr(settings, "OPENAI_API_KEY", None) or os.getenv("OPENAI_API_KEY")
        if not resolved_key:
            raise RuntimeError("OpenAIProvider: falta OPENAI_API_KEY (en .env o variable de entorno)")

        self.client = OpenAI(api_key=resolved_key, base_url=base_url or os.getenv("OPENAI_BASE_URL"))
        self.model = (
            model
            or getattr(settings, "OPENAI_MODEL", None)
            or os.getenv("OPENAI_MODEL")
            or "gpt-4o-mini"
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
            log.error(f"OpenAIProvider.generate_text error: {e}")
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
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=list(messages),
                tools=list(tools) if tools else None,
                tool_choice="auto" if tools else None,
                temperature=temperature,
            )
            msg = resp.choices[0].message
            content = (msg.content or "").strip()
            tool_calls = self._parse_tool_calls(msg)
            return LLMResponse(content=content, tool_calls=tool_calls)
        except Exception as e:
            log.error(f"OpenAIProvider.generate_reply error: {e}")
            raise
