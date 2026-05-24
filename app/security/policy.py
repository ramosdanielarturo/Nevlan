"""ArthurOS Security - Policy Manager

Evalúa si una ToolCall está permitida, denegada o requiere confirmación.

Estrategia (Windows-first, UX pragmática):
- En DEV: permitir acciones comunes de automatización (desktop.mouse/keyboard) para avanzar rápido.
- En PROD: pedir confirmación para acciones que modifiquen sistema/archivos o ejecuten shell.

Reglas:
- DENY: acciones prohibidas por entorno.
- CONFIRM: acciones de alto riesgo.
- ALLOW: lectura y automatización básica (según entorno).
"""

from __future__ import annotations

from enum import Enum
from typing import List

from pydantic import BaseModel

from app.core.config import settings
from app.contracts.tool_call import ToolCall


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class PolicyResult(BaseModel):
    decision: Decision
    reason: str


class PolicyManager:
    def __init__(self):
        # Alto riesgo: siempre confirmar (o negar en prod)
        self.high_risk_prefixes: List[str] = [
            "shell.",
            "fs.write.anywhere.",
            "os.shutdown",
            "process.kill",
        ]

    def evaluate(self, tool_call: ToolCall) -> PolicyResult:
        name = (tool_call.tool_name or "").lower()

        # 1) Producción: shell prohibida por defecto
        if settings.is_prod and name.startswith("shell."):
            return PolicyResult(decision=Decision.DENY, reason="Shell prohibida en Producción")

        # 2) Alto riesgo -> confirmación
        if any(name.startswith(p) for p in self.high_risk_prefixes):
            return PolicyResult(decision=Decision.CONFIRM, reason=f"Acción de alto riesgo: {name}")

        # 3) Lecturas seguras
        if name.startswith("fs.read") or name.startswith("web.") or name.startswith("os.get_system_info") or name.startswith("desktop.window.list"):
            return PolicyResult(decision=Decision.ALLOW, reason="Operación segura (read-only)")

        # 4) Escrituras en sandbox: permitir en DEV, confirmar en PROD
        if name.startswith("fs.write."):
            if settings.is_prod:
                return PolicyResult(decision=Decision.CONFIRM, reason="Escritura en FS (prod)")
            return PolicyResult(decision=Decision.ALLOW, reason="Escritura en sandbox (dev)")

        # 5) Automatización desktop: permitir en DEV, confirmar en PROD
        if name.startswith("desktop.") or name.startswith("vision."):
            if settings.is_prod:
                return PolicyResult(decision=Decision.CONFIRM, reason="Automatización desktop requiere confirmación en prod")
            return PolicyResult(decision=Decision.ALLOW, reason="Automatización desktop permitida en dev")

        # 6) Default: paranoia sana
        return PolicyResult(decision=Decision.CONFIRM, reason="Tool sin clasificación")


policy = PolicyManager()
