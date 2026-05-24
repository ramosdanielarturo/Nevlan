"""Tests del catálogo de aliases LAUNCH_APP (PRD §6).

Comprueba:
  - Resolución de aliases comunes (Chrome, Excel, Word, Edge, Firefox).
  - Tolerancia a espacios, mayúsculas/minúsculas, separadores.
  - process_names devueltos.
  - Apps desconocidas no rompen, devuelven canonical=name.
"""
from __future__ import annotations

import pytest

from app.services.missions.launch_apps import (
    AppAlias, resolve_app_alias, canonical_app_name, display_name_for,
    process_names_for, known_aliases,
)


class TestKnownAliases:
    @pytest.mark.parametrize("typed,canonical,proc", [
        ("chrome", "Chrome", "chrome.exe"),
        ("Chrome", "Chrome", "chrome.exe"),
        ("GOOGLE-CHROME", "Chrome", "chrome.exe"),
        ("Google Chrome", "Chrome", "chrome.exe"),
        ("chromium", "Chrome", "chrome.exe"),
        ("excel", "Excel", "EXCEL.EXE"),
        ("Microsoft Excel", "Excel", "EXCEL.EXE"),
        ("word", "Word", "WINWORD.EXE"),
        ("microsoft  word", "Word", "WINWORD.EXE"),
        ("powerpoint", "PowerPoint", "POWERPNT.EXE"),
        ("ppt", "PowerPoint", "POWERPNT.EXE"),
        ("outlook", "Outlook", "OUTLOOK.EXE"),
        ("edge", "Edge", "msedge.exe"),
        ("firefox", "Firefox", "firefox.exe"),
        ("brave", "Brave", "brave.exe"),
        ("notepad", "Notepad", "notepad.exe"),
        ("bloc de notas", "Notepad", "notepad.exe"),
        ("vscode", "VS Code", "Code.exe"),
        ("vs code", "VS Code", "Code.exe"),
    ])
    def test_resolution(self, typed, canonical, proc):
        assert canonical_app_name(typed) == canonical
        procs = process_names_for(typed)
        assert proc in procs


class TestUnknownAliases:
    def test_unknown_passes_through(self):
        a = resolve_app_alias("FooBarApp")
        assert a.canonical == "FooBarApp"
        assert a.display_name == "FooBarApp"
        assert a.process_names == ()

    def test_empty_returns_empty(self):
        a = resolve_app_alias("")
        assert a.canonical == ""
        assert a.process_names == ()


class TestDisplayName:
    def test_chrome_long_form(self):
        assert display_name_for("chrome") == "Google Chrome"
        assert display_name_for("excel") == "Microsoft Excel"
        assert display_name_for("word") == "Microsoft Word"


class TestKnownAliasesList:
    def test_list_includes_all_short_canonicals(self):
        names = known_aliases()
        for short in ("Chrome", "Excel", "Word", "Edge", "Firefox", "VS Code"):
            assert short in names, f"{short} missing"


# ──────────────────────────────────────────────────────────────────
# Player LAUNCH_APP — sin tocar el OS, sólo verifica que el flujo
# usa las helpers correctas y que pyautogui (stub) recibe llamadas
# coherentes en el camino "Windows Search".
# ──────────────────────────────────────────────────────────────────

class TestPlayerExecuteLaunchApp:
    def _player(self):
        from app.contracts.mission import Mission, MissionStatus
        from app.services.missions.player import MissionPlayer
        m = Mission(name="t", status=MissionStatus.COMPILED)
        p = MissionPlayer(m)
        return p

    def test_missing_app_name_raises(self):
        p = self._player()
        with pytest.raises(RuntimeError):
            p._execute_launch_app({})

    def test_falls_back_to_windows_search_for_unknown_app(self, monkeypatch):
        """Una app desconocida (sin process_names) cae directo a Windows Search."""
        p = self._player()

        # Forzar: ya no está corriendo + start_executable falla.
        monkeypatch.setattr(
            "app.services.missions.launch_apps.is_app_running",
            lambda name: None,
        )
        from app.services.missions import player as player_mod
        monkeypatch.setattr(
            player_mod.MissionPlayer, "_try_start_executable",
            staticmethod(lambda exe: False),
        )
        monkeypatch.setattr(
            player_mod.MissionPlayer, "_try_run_dialog",
            lambda self, exe: False,
        )
        monkeypatch.setattr(
            player_mod.MissionPlayer, "_wait_for_process_ready",
            staticmethod(lambda procs, timeout_s=4.0: False),
        )

        # Capturar invocaciones de pyautogui.
        calls = []

        import sys
        pa = sys.modules.get("pyautogui")
        assert pa is not None
        monkeypatch.setattr(pa, "hotkey",
                            lambda *a, **k: calls.append(("hotkey", a)))
        monkeypatch.setattr(pa, "typewrite",
                            lambda *a, **k: calls.append(("typewrite", a)))
        monkeypatch.setattr(pa, "press",
                            lambda *a, **k: calls.append(("press", a)))

        # Acortar el wait de "Windows Search ready" para no esperar
        # 1.5s en CI.
        monkeypatch.setattr(
            player_mod.MissionPlayer, "_wait_window_search_ready",
            staticmethod(lambda timeout_s=1.5: None),
        )

        p._execute_launch_app({"app_name": "MiAppRara"})

        # El último press debería ser Enter, hubo un typewrite con el
        # nombre, y un hotkey "win".
        kinds = [c[0] for c in calls]
        assert "hotkey" in kinds
        assert "typewrite" in kinds
        assert any(c == ("press", ("enter",)) for c in calls), calls

    def test_when_app_already_running_only_focuses(self, monkeypatch):
        p = self._player()
        from app.services.missions import player as player_mod
        from app.services.missions import launch_apps

        focused = []
        monkeypatch.setattr(launch_apps, "is_app_running",
                            lambda name: True)
        monkeypatch.setattr(
            player_mod.MissionPlayer, "_focus_running_app",
            staticmethod(lambda procs: focused.append(list(procs))),
        )
        # No debería caer a Windows Search.
        called_search = []
        monkeypatch.setattr(
            player_mod.MissionPlayer, "_wait_window_search_ready",
            staticmethod(
                lambda timeout_s=1.5: called_search.append(True)
            ),
        )
        import sys
        pa = sys.modules.get("pyautogui")
        monkeypatch.setattr(pa, "hotkey",
                            lambda *a, **k: called_search.append("hotkey"))
        monkeypatch.setattr(pa, "typewrite",
                            lambda *a, **k: called_search.append("type"))

        p._execute_launch_app({"app_name": "Chrome"})

        assert focused, "Debe enfocarse la ventana existente"
        # Los process_names para Chrome → ['chrome.exe']
        assert "chrome.exe" in focused[0]
        assert called_search == [], (
            f"No debería tocar Windows Search; fue: {called_search}"
        )
