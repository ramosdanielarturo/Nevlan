"""ArthurOS Platforms - macOS Driver (skeleton MVP).

Requiere macOS. Implementación mínima:
- open_url/open_file usando `open`
- clipboard usando pbpaste/pbcopy
- run_shell_command usando bash/zsh (ojo: policy debe protegerlo)
"""

from __future__ import annotations

import os
import platform
import subprocess
import getpass
from typing import Any, Dict, Optional

from app.core.logger import log
from app.platforms.base import PlatformDriver


class MacDriver(PlatformDriver):
    @property
    def platform_name(self) -> str:
        return "macos"

    def get_system_info(self) -> Dict[str, Any]:
        try:
            user = getpass.getuser()
        except Exception:
            user = None
        return {
            "os": "macOS",
            "release": platform.release(),
            "version": platform.version(),
            "architecture": platform.machine(),
            "processor": platform.processor(),
            "user": user,
        }

    def open_url(self, url: str) -> bool:
        try:
            subprocess.run(["open", url], check=False)
            return True
        except Exception as e:
            log.error(f"MacDriver.open_url failed: {e}")
            return False

    def open_file(self, path: str) -> bool:
        try:
            subprocess.run(["open", path], check=False)
            return True
        except Exception as e:
            log.error(f"MacDriver.open_file failed: {e}")
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
            raise RuntimeError(f"Mac shell error: {e}") from e

    def get_clipboard_text(self) -> Optional[str]:
        try:
            result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                return None
            return (result.stdout or "")
        except Exception:
            return None

    def set_clipboard_text(self, text: str) -> bool:
        try:
            p = subprocess.run(["pbcopy"], input=text or "", text=True, timeout=5)
            return p.returncode == 0
        except Exception:
            return False
