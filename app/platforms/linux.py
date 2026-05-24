"""ArthurOS Platforms - Linux Driver (skeleton MVP).

Implementación mínima:
- open_url/open_file con xdg-open
- clipboard opcional con xclip o wl-clipboard (si está instalado)
"""

from __future__ import annotations

import os
import platform
import subprocess
import getpass
from typing import Any, Dict, Optional

from app.core.logger import log
from app.platforms.base import PlatformDriver


class LinuxDriver(PlatformDriver):
    @property
    def platform_name(self) -> str:
        return "linux"

    def get_system_info(self) -> Dict[str, Any]:
        try:
            user = getpass.getuser()
        except Exception:
            user = None
        return {
            "os": "Linux",
            "release": platform.release(),
            "version": platform.version(),
            "architecture": platform.machine(),
            "processor": platform.processor(),
            "user": user,
        }

    def open_url(self, url: str) -> bool:
        try:
            subprocess.run(["xdg-open", url], check=False)
            return True
        except Exception as e:
            log.error(f"LinuxDriver.open_url failed: {e}")
            return False

    def open_file(self, path: str) -> bool:
        try:
            subprocess.run(["xdg-open", path], check=False)
            return True
        except Exception as e:
            log.error(f"LinuxDriver.open_file failed: {e}")
            return False

    def run_shell_command(self, command: str) -> str:
        try:
            result = subprocess.run(
                ["bash", "-lc", command],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or "").strip() or f"exit {result.returncode}")
            return (result.stdout or "").strip()
        except Exception as e:
            raise RuntimeError(f"Linux shell error: {e}") from e

    def get_clipboard_text(self) -> Optional[str]:
        # Probamos wl-paste (Wayland) y luego xclip (X11)
        for cmd in (["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"]):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                if result.returncode == 0:
                    return result.stdout
            except FileNotFoundError:
                continue
            except Exception:
                continue
        return None

    def set_clipboard_text(self, text: str) -> bool:
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            try:
                result = subprocess.run(cmd, input=text or "", text=True, timeout=3)
                if result.returncode == 0:
                    return True
            except FileNotFoundError:
                continue
            except Exception:
                continue
        return False
