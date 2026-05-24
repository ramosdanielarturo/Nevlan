"""
ArthurOS Platforms - Base Driver (Interface)
--------------------------------------------
Define la interfaz abstracta que todos los sistemas operativos deben implementar.
Esto garantiza que el resto de la app sea agnóstica a la plataforma.

Nota de diseño:
- Los drivers NO deciden seguridad. Solo ejecutan acciones técnicas.
- La capa app/security debe decidir ALLOW / CONFIRM / DENY.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class PlatformDriver(ABC):
    """Contrato común para drivers por sistema operativo."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Nombre del SO (ej: 'windows', 'macos', 'linux')."""
        raise NotImplementedError

    @abstractmethod
    def get_system_info(self) -> Dict[str, Any]:
        """Devuelve info básica: versión, usuario, arquitectura, etc."""
        raise NotImplementedError

    @abstractmethod
    def open_url(self, url: str) -> bool:
        """Abre una URL en el navegador predeterminado."""
        raise NotImplementedError

    @abstractmethod
    def open_file(self, path: str) -> bool:
        """Abre un archivo con el programa asociado por defecto."""
        raise NotImplementedError

    @abstractmethod
    def run_shell_command(self, command: str) -> str:
        """
        Ejecuta un comando de terminal y devuelve stdout.

        ⚠️ PELIGROSO: debe estar protegido por la capa Policy/Security.
        Se recomienda que la capa superior:
        - valide allowlists/scopes
        - aplique timeouts
        - audite intentos
        """
        raise NotImplementedError

    @abstractmethod
    def get_clipboard_text(self) -> Optional[str]:
        """Lee texto del portapapeles."""
        raise NotImplementedError

    @abstractmethod
    def set_clipboard_text(self, text: str) -> bool:
        """Escribe texto en el portapapeles."""
        raise NotImplementedError

    # ---- Helpers seguros (no abstract) ----
    def run_shell_command_safe(self, command: str) -> Dict[str, Any]:
        """Wrapper que nunca lanza excepción; útil para UI/telemetría."""
        try:
            out = self.run_shell_command(command)
            return {"success": True, "stdout": out, "error": None}
        except Exception as e:  # noqa: BLE001 (queremos capturar todo)
            return {"success": False, "stdout": "", "error": str(e)}
