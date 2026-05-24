"""Crash handler — captura crashes para no quedar a ciegas.

Bug original (2026-05-01):
    Tras detener una grabación, al abrir la pantalla de review, la app
    cerraba sin dejar rastro en logs. La última línea escrita era
    ``mission_review:__init__:165 - Review de automatización: ...`` y
    luego silencio absoluto. Sin un hook de crashes era imposible saber
    qué fallaba.

Estos tests fijan el contrato:
    - ``install_crash_handlers`` es idempotente.
    - Un excepthook de main escribe a ``var/crashes/``.
    - Un excepthook de thread escribe a ``var/crashes/``.
    - ``write_manual_crash`` escribe el traceback completo.
    - El archivo incluye system info y lista de threads vivos.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_crashes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirigimos ``crashes_dir`` al tmp_path para no ensuciar var/."""
    from app.core import crash_handler as ch

    monkeypatch.setattr(ch, "_crashes_dir", lambda: tmp_path)
    yield tmp_path


# ── install_crash_handlers ────────────────────────────────────────────


class TestInstallation:
    def test_idempotent(self):
        """Llamar dos veces no rompe ni instala dos hooks distintos."""
        from app.core import crash_handler as ch

        prev_main = sys.excepthook
        prev_thread = threading.excepthook

        ch.install_crash_handlers()
        first_main = sys.excepthook
        first_thread = threading.excepthook

        ch.install_crash_handlers()
        second_main = sys.excepthook
        second_thread = threading.excepthook

        # Después de la primera instalación los hooks deben haber
        # cambiado, pero la segunda llamada no debe sustituirlos otra
        # vez (idempotencia).
        assert first_main is second_main
        assert first_thread is second_thread

        # Restauramos para no contaminar otros tests.
        sys.excepthook = prev_main
        try:
            threading.excepthook = prev_thread  # type: ignore[assignment]
        except Exception:
            pass
        ch._INSTALLED = False


# ── write_manual_crash ────────────────────────────────────────────────


class TestWriteManualCrash:
    def test_persists_traceback(self, _isolated_crashes_dir):
        from app.core.crash_handler import write_manual_crash

        try:
            raise ValueError("boom for test")
        except ValueError as e:
            path = write_manual_crash("test_reason", e)

        assert path is not None
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "ValueError" in content
        assert "boom for test" in content
        assert "active_threads=" in content, (
            "El crash report debe incluir lista de threads vivos para "
            "diagnosticar races con pyautogui/pynput."
        )
        # Filename incluye el reason para clasificar manualmente.
        assert "test_reason" in path.name


# ── _hook_main captura excepciones del main thread ────────────────────


class TestMainHook:
    def test_main_hook_writes_file(self, _isolated_crashes_dir):
        from app.core import crash_handler as ch

        # Simulamos cargar un excepthook custom y dispararlo.
        try:
            raise RuntimeError("simulated main crash")
        except RuntimeError:
            exc_type, exc_value, exc_tb = sys.exc_info()

        ch._hook_main(exc_type, exc_value, exc_tb)

        files = list(_isolated_crashes_dir.glob("crash_*main_exception.log"))
        assert len(files) >= 1
        body = files[0].read_text(encoding="utf-8")
        assert "RuntimeError" in body
        assert "simulated main crash" in body


# ── _hook_thread captura excepciones de threads daemon ────────────────


class TestThreadHook:
    def test_thread_hook_writes_file(self, _isolated_crashes_dir):
        from app.core import crash_handler as ch
        from threading import ExceptHookArgs

        # Construimos manualmente unos args como los daría threading.
        try:
            raise OSError("simulated thread crash")
        except OSError:
            exc_type, exc_value, exc_tb = sys.exc_info()

        class _FakeThread:
            name = "PynputListener"
            daemon = True

        # ``threading.ExceptHookArgs`` es un namedtuple.
        args = ExceptHookArgs([exc_type, exc_value, exc_tb, _FakeThread()])
        ch._hook_thread(args)

        files = list(_isolated_crashes_dir.glob("crash_*thread_exception.log"))
        assert len(files) >= 1
        body = files[0].read_text(encoding="utf-8")
        assert "PynputListener" in body
        assert "OSError" in body
        assert "simulated thread crash" in body


# ── system info y threads incluidos ───────────────────────────────────


class TestCrashContent:
    def test_includes_system_info(self, _isolated_crashes_dir):
        from app.core.crash_handler import write_manual_crash

        try:
            raise Exception("x")
        except Exception as e:
            path = write_manual_crash("x", e)

        body = path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        assert "python=" in body
        assert "platform=" in body
        assert f"pid={os.getpid()}" in body
