"""Configuración global de pytest.

Objetivos (FASE 1 §8):
  - Permitir que los tests **lógicos** corran en CI headless sin necesidad
    de PyQt6 ni de pyautogui con un display real.
  - No imponer cambios en tests UI: si quieres ejecutar UI, instala PyQt6
    y los tests marcados ``@pytest.mark.ui`` correrán normales.
  - No tocar la lógica de los módulos importados; sólo asegurar que
    ``import pyautogui`` exista en cualquier entorno.

Marcadores definidos:
  - ``ui``: requiere PyQt6 / display real. Se salta si no se cumplen.
  - ``integration_real_app``: requiere apps reales (Chrome, Excel...).
  - ``unit``: ya estaba definido en pytest.ini.

Para más control, exporta ``NEVLAN_HEADLESS=1`` en CI (no es obligatorio,
los autodetect siguen funcionando).
"""
from __future__ import annotations

import os
import sys
import types
import importlib.util

import pytest


# ──────────────────────────────────────────────────────────────────
# pyautogui stub para entornos sin display
# ──────────────────────────────────────────────────────────────────

def _pyautogui_available() -> bool:
    try:
        import pyautogui  # noqa: F401
        return True
    except Exception:
        return False


def _install_pyautogui_stub() -> None:
    """Inserta un stub mínimo en ``sys.modules['pyautogui']``.

    El stub expone los símbolos que ``recorder`` / ``player`` /
    ``target_signature`` usan, pero no toca el sistema real. Esto es
    suficiente para que ``from pyautogui import ...`` no falle en CI.
    """
    if "pyautogui" in sys.modules:
        return
    mod = types.ModuleType("pyautogui")

    class _Size(tuple):
        @property
        def width(self) -> int:  # noqa: D401
            return self[0]

        @property
        def height(self) -> int:  # noqa: D401
            return self[1]

    def size(*_a, **_kw):  # type: ignore[no-redef]
        return _Size((1920, 1080))

    def _noop(*_a, **_kw):  # type: ignore[no-redef]
        return None

    def locateOnScreen(*_a, **_kw):  # type: ignore[no-redef]
        return None

    def hotkey(*_a, **_kw):  # type: ignore[no-redef]
        return None

    def typewrite(*_a, **_kw):  # type: ignore[no-redef]
        return None

    def press(*_a, **_kw):  # type: ignore[no-redef]
        return None

    def click(*_a, **_kw):  # type: ignore[no-redef]
        return None

    def moveTo(*_a, **_kw):  # type: ignore[no-redef]
        return None

    mod.size = size
    mod.locateOnScreen = locateOnScreen
    mod.hotkey = hotkey
    mod.typewrite = typewrite
    mod.press = press
    mod.click = click
    mod.moveTo = moveTo
    mod.FAILSAFE = False
    mod.PAUSE = 0.0
    mod._is_stub = True  # bandera para los tests
    sys.modules["pyautogui"] = mod


# Si pyautogui no se puede importar (no hay display, headless), instalamos
# el stub ANTES de que cualquier test importe módulos del repo.
if not _pyautogui_available():
    _install_pyautogui_stub()


# ──────────────────────────────────────────────────────────────────
# Marcadores
# ──────────────────────────────────────────────────────────────────

def pytest_configure(config: pytest.Config) -> None:  # type: ignore[name-defined]
    config.addinivalue_line("markers", "ui: requires PyQt6 + display")
    config.addinivalue_line(
        "markers",
        "integration_real_app: requires Chrome/Excel/Word — opt-in",
    )


# ──────────────────────────────────────────────────────────────────
# Skip automático para tests UI cuando no hay PyQt6
# ──────────────────────────────────────────────────────────────────

_PYQT_AVAILABLE = importlib.util.find_spec("PyQt6") is not None


def pytest_collection_modifyitems(config, items):  # type: ignore[no-redef]
    if _PYQT_AVAILABLE:
        return
    skip_ui = pytest.mark.skip(reason="PyQt6 no disponible (entorno headless)")
    for item in items:
        if "ui" in item.keywords:
            item.add_marker(skip_ui)


@pytest.fixture(scope="session")
def has_pyqt6() -> bool:  # noqa: D401
    return _PYQT_AVAILABLE


@pytest.fixture(scope="session")
def is_headless() -> bool:  # noqa: D401
    return bool(os.environ.get("NEVLAN_HEADLESS")) or not _PYQT_AVAILABLE
