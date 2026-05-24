"""WebRecorder NUNCA debe lanzar Chrome durante la grabación.

Bug real (2026-05-01):
    El usuario empezaba a grabar y le aparecían MUCHAS ventanas de
    Chrome. La causa era ``WebRecorder.capture_web_target`` llamando a
    ``app.skills.tools.web._get_page()``. Esa función:
      1. Si no había Chrome con CDP en :9222, lanzaba Chrome con
         ``subprocess.Popen`` (vía ``_launch_chrome_with_debugging``).
      2. Si la llamada venía de otro thread (los listeners de teclado/
         ratón corren en threads de pyautogui), CERRABA el browser y
         arrancaba uno nuevo. En cada click sobre Chrome.

Fix:
    ``_get_page`` aceptó un parámetro ``connect_only`` (default False).
    Cuando es True (modo grabador):
      - NUNCA llama a ``_launch_chrome_with_debugging``.
      - NUNCA cierra ``_BROWSER`` global.
      - Si no hay CDP disponible, retorna ``None`` y el grabador hace
        fallback a UIA/visión.

Estos tests fijan el contrato: el grabador no puede dejar caer Chrome
sobre la pantalla del usuario, pase lo que pase.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest


# ──────────────────────────────────────────────────────────────────────
# Helpers: parchamos ``app.skills.tools.web`` con un módulo falso que
# expone ``_get_page`` igual al real (con el parámetro ``connect_only``)
# para no requerir Playwright instalado en CI.
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def web_module(monkeypatch: pytest.MonkeyPatch):
    """Recarga app.skills.tools.web con un stub mínimo que respeta el
    contrato de connect_only.
    """
    # Removemos cualquier import previo para garantizar partida limpia.
    for mod in [
        "app.skills.tools.web",
        "app.services.missions.web_recorder",
    ]:
        sys.modules.pop(mod, None)

    # Stubs de pydantic-style imports que web.py necesita.
    import app.skills.tools.web as web

    # Confirmamos la firma del nuevo connect_only.
    import inspect

    sig = inspect.signature(web._get_page)
    assert "connect_only" in sig.parameters, (
        "_get_page debe aceptar connect_only para que el grabador no "
        "pueda lanzar Chrome accidentalmente."
    )
    return web


class TestGetPageConnectOnly:
    def test_connect_only_returns_none_when_cdp_unavailable(self, web_module):
        """Si no hay CDP disponible, ``connect_only=True`` devuelve None
        sin llamar a ``_launch_chrome_with_debugging``.
        """
        with patch.object(web_module, "_is_chrome_debugging_available",
                          return_value=False), \
             patch.object(web_module, "_launch_chrome_with_debugging") as m_launch:
            result = web_module._get_page(connect_only=True)
            assert result is None
            assert m_launch.call_count == 0, (
                "_launch_chrome_with_debugging se invocó pese a "
                "connect_only=True — esto reabriría el bug de las "
                "múltiples ventanas de Chrome."
            )

    def test_connect_only_does_not_close_existing_browser(self, web_module):
        """Cuando otro thread ya tiene un browser global, ``connect_only``
        NO debe cerrarlo (el código viejo hacía thread-reset y cerraba).
        """
        # Sentinel: si alguien llama .close() lo detectamos.
        closed_calls = []

        class _FakeBrowser:
            def is_connected(self):
                return True
            def close(self):
                closed_calls.append(True)

        web_module._BROWSER = _FakeBrowser()
        web_module._PAGE = "page-from-other-thread"
        web_module._PLAYWRIGHT = "pw-from-other-thread"

        try:
            with patch.object(web_module, "_is_chrome_debugging_available",
                              return_value=False), \
                 patch.object(web_module, "_launch_chrome_with_debugging") as m_launch:
                result = web_module._get_page(connect_only=True)
                assert result is None
                assert closed_calls == [], (
                    "connect_only=True cerró el browser global de otro "
                    "thread: ese era exactamente el bug."
                )
                assert m_launch.call_count == 0
        finally:
            # Restauramos para no contaminar otros tests.
            web_module._BROWSER = None
            web_module._PAGE = None
            web_module._PLAYWRIGHT = None


class TestWebRecorderNeverLaunchesChrome:
    def test_capture_web_target_does_not_launch_chrome(self):
        """``capture_web_target`` no debe lanzar Chrome bajo ningún
        camino. Si el bridge web devuelve None / lanza, el recorder
        retorna None silenciosamente.
        """
        from app.services.missions import web_recorder

        # Simulamos: CDP no disponible. capture_web_target debe
        # devolver None sin tocar Chrome.
        called: list = []

        def _fake_get_page(*, connect_only=False):
            called.append({"connect_only": connect_only})
            return None  # CDP no disponible

        with patch("app.skills.tools.web._get_page", side_effect=_fake_get_page):
            result = web_recorder.capture_web_target(
                x=100, y=100, process_name="chrome.exe", hwnd=0,
            )
            assert result is None
            # Si _get_page se llamó, DEBE ser con connect_only=True.
            for call in called:
                assert call["connect_only"] is True, (
                    f"WebRecorder llamó _get_page sin connect_only: {call}"
                )

    def test_capture_web_target_ignores_non_browser_processes(self):
        """Si el proceso no es un navegador, no se invoca ``_get_page``."""
        from app.services.missions import web_recorder

        called = []
        with patch("app.skills.tools.web._get_page",
                   side_effect=lambda **kw: called.append(kw)):
            r = web_recorder.capture_web_target(
                x=10, y=10, process_name="EXCEL.EXE", hwnd=0,
            )
            assert r is None
            assert called == []
