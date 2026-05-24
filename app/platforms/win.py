"""
ArthurOS Platforms - Windows Driver
-----------------------------------
Implementación específica para Windows 10/11.

Principios:
- Lo más estándar posible (sin pywin32).
- Clipboard vía PowerShell (suficiente para MVP).
- Timeouts y manejo de errores para evitar cuelgues.
"""

from __future__ import annotations

import os
import platform
import subprocess
import getpass
from typing import Any, Dict, Optional

from app.core.logger import log
from app.platforms.base import PlatformDriver


class WindowsDriver(PlatformDriver):
    @property
    def platform_name(self) -> str:
        return "windows"

    def get_system_info(self) -> Dict[str, Any]:
        # os.getlogin() puede fallar en servicios/tareas programadas; getpass es más robusto.
        try:
            user = getpass.getuser()
        except Exception:
            user = None

        return {
            "os": "Windows",
            "release": platform.release(),
            "version": platform.version(),
            "architecture": platform.machine(),
            "processor": platform.processor(),
            "user": user,
        }

    def open_url(self, url: str) -> bool:
        try:
            os.startfile(url)  # type: ignore[attr-defined]  # Windows only
            return True
        except Exception as e:
            log.error(f"WindowsDriver.open_url failed for {url!r}: {e}")
            return False

    def open_file(self, path: str) -> bool:
        try:
            if not path:
                return False
            # Validación ligera: si existe como archivo/ruta local, ok; si no, aún puede ser URI.
            if os.path.exists(path) or "://" in path:
                os.startfile(path)  # type: ignore[attr-defined]  # Windows only
                return True
            log.warning(f"WindowsDriver.open_file: path no existe: {path!r}")
            return False
        except Exception as e:
            log.error(f"WindowsDriver.open_file failed for {path!r}: {e}")
            return False

    def run_shell_command(self, command: str) -> str:
        """Ejecuta PowerShell y devuelve stdout (o lanza RuntimeError)."""
        if not command or not isinstance(command, str):
            raise RuntimeError("Comando inválido")

        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Timeout ejecutando PowerShell") from e
        except Exception as e:
            raise RuntimeError(f"Fallo ejecutando PowerShell: {e}") from e

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            # Subimos stderr para troubleshooting; la capa superior decidirá cómo mostrarlo.
            raise RuntimeError(stderr or f"PowerShell returned code {result.returncode}")

        return stdout

    def get_clipboard_text(self) -> Optional[str]:
        # Nota: puede devolver múltiples líneas.
        try:
            return self.run_shell_command("Get-Clipboard")
        except Exception as e:
            log.debug(f"WindowsDriver.get_clipboard_text failed: {e}")
            return None

    def set_clipboard_text(self, text: str) -> bool:
        # Mejor práctica: enviar por stdin para evitar problemas de escape/injection.
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard"],
                input=text if text is not None else "",
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                log.debug(f"WindowsDriver.set_clipboard_text stderr: {(proc.stderr or '').strip()}")
                return False
            return True
        except Exception as e:
            log.debug(f"WindowsDriver.set_clipboard_text failed: {e}")
            return False
