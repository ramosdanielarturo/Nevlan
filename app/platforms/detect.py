"""
ArthurOS Platforms - Auto Detection
-----------------------------------
Detecta el sistema operativo anfitrión y devuelve la instancia correcta del driver.
Se ejecuta una vez (singleton) y es thread-safe.

Overrides útiles:
- ARTHUR_PLATFORM=windows|macos|linux (para tests/CI o forzar comportamiento)
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional

from app.core.logger import log
from app.platforms.base import PlatformDriver


_lock = threading.Lock()
_current_platform: Optional[PlatformDriver] = None


def _normalize_override(val: str) -> str:
    v = (val or "").strip().lower()
    aliases = {
        "win": "windows",
        "windows": "windows",
        "mac": "macos",
        "macos": "macos",
        "osx": "macos",
        "linux": "linux",
    }
    return aliases.get(v, v)


def get_platform_driver() -> PlatformDriver:
    global _current_platform

    with _lock:
        if _current_platform is not None:
            return _current_platform

        override = _normalize_override(os.getenv("ARTHUR_PLATFORM", ""))
        if override:
            log.info(f"🧩 ARTHUR_PLATFORM override: {override}")

        if override == "windows" or (not override and sys.platform.startswith("win")):
            log.info("🖥️ Plataforma detectada: Windows")
            from app.platforms.win import WindowsDriver
            _current_platform = WindowsDriver()
            return _current_platform

        if override == "macos" or (not override and sys.platform == "darwin"):
            log.info("🍎 Plataforma detectada: macOS")
            from app.platforms.mac import MacDriver
            _current_platform = MacDriver()
            return _current_platform

        if override == "linux" or (not override and sys.platform.startswith("linux")):
            log.info("🐧 Plataforma detectada: Linux")
            from app.platforms.linux import LinuxDriver
            _current_platform = LinuxDriver()
            return _current_platform

        log.critical(f"❌ Plataforma no soportada: sys.platform={sys.platform!r}, override={override!r}")
        raise RuntimeError(f"OS no soportado: {sys.platform}")
