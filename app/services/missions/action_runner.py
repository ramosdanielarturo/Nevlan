"""
ArthurOS Services - ActionRunner (Smart Executor)
-------------------------------------------------
Ejecuta estrategias **identificadas por string** para cada *kind* de
``MissionStep``. El identificador llega del ``StepContract``:

  - "open_app:os_startfile"
  - "open_url:hotkey_ctrl_l"
  - "search_youtube:dom_input"
  - …

Diseño:
  * Una sola entrada (``run_strategy``) que despacha por nombre.
  * Cada estrategia es una función pequeña que devuelve ``StrategyResult``
    con ok/false + mensaje + duración. NUNCA lanza excepciones: si algo
    falla, se reporta y el caller decide.
  * Imports defensivos: pyautogui, uiautomation, playwright son
    opcionales. Una estrategia que requiera una dependencia ausente
    devuelve ``ok=False`` con motivo claro.
  * NO valida la postcondition: eso es trabajo del Smart Executor con
    el ``StateDetector``. Aquí sólo "intentamos hacer la cosa".

Esto deja al executor con una superficie pequeña y testeable: el árbol
de decisión vive en los *contratos*, la ejecución vive aquí.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from app.core.logger import log
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.launch_apps import (
    is_app_running,
    process_names_for,
    canonical_app_name,
)
from app.services.missions.state_detector import StateSnapshot


# ─── Imports opcionales ───────────────────────────────────────────────
try:
    import pyautogui  # type: ignore
    HAS_PYAUTOGUI = True
    pyautogui.FAILSAFE = False
except Exception:
    pyautogui = None  # type: ignore
    HAS_PYAUTOGUI = False

try:
    import uiautomation as auto  # type: ignore
    HAS_UIA = True
except Exception:
    auto = None  # type: ignore
    HAS_UIA = False

try:
    import pygetwindow as gw  # type: ignore
    HAS_GW = True
except Exception:
    gw = None  # type: ignore
    HAS_GW = False


# ──────────────────────────────────────────────────────────────────────
# Resultado canónico
# ──────────────────────────────────────────────────────────────────────

@dataclass
class StrategyResult:
    ok: bool
    strategy: str
    message: str = ""
    duration_ms: int = 0
    extra: Optional[Dict[str, Any]] = None


# Type alias: implementación concreta de una estrategia.
StrategyFn = Callable[[MissionStep, StateSnapshot], StrategyResult]


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _ok(strategy: str, msg: str = "", **extra: Any) -> StrategyResult:
    return StrategyResult(ok=True, strategy=strategy, message=msg, extra=extra or None)


def _fail(strategy: str, msg: str, **extra: Any) -> StrategyResult:
    return StrategyResult(ok=False, strategy=strategy, message=msg, extra=extra or None)


def _hotkey(*keys: str) -> bool:
    if not HAS_PYAUTOGUI:
        return False
    try:
        pyautogui.hotkey(*keys)
        return True
    except Exception as e:
        log.debug(f"hotkey {keys}: {e}")
        return False


def _type_text(text: str, interval: float = 0.0) -> bool:
    if not HAS_PYAUTOGUI:
        return False
    try:
        pyautogui.typewrite(text, interval=interval)
        return True
    except Exception as e:
        log.debug(f"typewrite '{text[:40]}': {e}")
        return False


def _press(key: str) -> bool:
    if not HAS_PYAUTOGUI:
        return False
    try:
        pyautogui.press(key)
        return True
    except Exception as e:
        log.debug(f"press {key}: {e}")
        return False


def _ensure_browser_foreground() -> bool:
    """Activa Chrome/Edge/Brave/Firefox antes de hotkeys de omnibox o búsqueda."""
    if not HAS_GW or gw is None:
        return False
    hints = (
        "YouTube",
        "Google Chrome",
        "Chrome",
        "Microsoft Edge",
        "Edge",
        "Brave",
        "Firefox",
    )
    try:
        for hint in hints:
            for win in gw.getWindowsWithTitle(hint):
                title = (win.title or "").strip()
                if not title or not getattr(win, "visible", True):
                    continue
                try:
                    if getattr(win, "isMinimized", False):
                        win.restore()
                    win.activate()
                    time.sleep(0.35)
                    return True
                except Exception:
                    continue
        for win in gw.getAllWindows():
            title = (win.title or "").lower()
            if not title or not getattr(win, "visible", True):
                continue
            if any(
                tok in title
                for tok in ("chrome", "youtube", "edge", "brave", "firefox")
            ):
                try:
                    if getattr(win, "isMinimized", False):
                        win.restore()
                    win.activate()
                    time.sleep(0.35)
                    return True
                except Exception:
                    continue
    except Exception as e:
        log.debug(f"ensure_browser_foreground: {e}")
    return False


def _get_playwright_page(connect_only: bool = False):
    """Ruta segura: nunca falla, devuelve None si no hay browser."""
    try:
        import asyncio

        try:
            asyncio.get_running_loop()
            connect_only = True
        except RuntimeError:
            pass
        from app.skills.tools.web import _get_page  # type: ignore
        return _get_page(connect_only=connect_only)
    except Exception as e:
        log.debug(f"playwright page: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────
# Estrategias: open_app
# ──────────────────────────────────────────────────────────────────────

def _open_app_os_startfile(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    name = (step.params.get("name") or "").strip()
    if not name:
        return _fail("open_app:os_startfile", "sin nombre de app")
    canonical = canonical_app_name(name) or name
    # Idempotencia: si ya corre, ya está.
    if is_app_running(name):
        return _ok("open_app:os_startfile", f"{canonical} ya corre")

    procs = process_names_for(name)
    candidates = []
    if procs:
        candidates.extend(procs)
    candidates.append(canonical)
    candidates.append(name)
    last_err = ""
    for c in candidates:
        try:
            if hasattr(os, "startfile"):
                os.startfile(c)  # type: ignore[attr-defined]
                return _ok("open_app:os_startfile", f"startfile({c})")
        except Exception as e:
            last_err = str(e)
            continue
    return _fail("open_app:os_startfile", last_err or "startfile no disponible")


def _open_app_start_command(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    """Win+R no — usamos ``cmd /c start "" <name>``."""
    name = (step.params.get("name") or "").strip()
    if not name:
        return _fail("open_app:start_command", "sin nombre de app")
    canonical = canonical_app_name(name) or name
    if is_app_running(name):
        return _ok("open_app:start_command", f"{canonical} ya corre")
    try:
        # ``shell=True`` es necesario para ``start`` (built-in del shell).
        subprocess.Popen(
            f'start "" "{canonical}"',
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return _ok("open_app:start_command", f"start {canonical}")
    except Exception as e:
        return _fail("open_app:start_command", str(e))


def _open_app_windows_search(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    """Último recurso: Win → escribe nombre → Enter."""
    name = (step.params.get("name") or "").strip()
    if not name:
        return _fail("open_app:windows_search", "sin nombre")
    if is_app_running(name):
        return _ok("open_app:windows_search", "ya corre")
    if not _hotkey("win"):
        return _fail("open_app:windows_search", "no pude abrir Windows Search")
    time.sleep(0.5)
    canonical = canonical_app_name(name) or name
    if not _type_text(canonical, interval=0.02):
        return _fail("open_app:windows_search", "no pude escribir el nombre")
    time.sleep(0.4)
    if not _press("enter"):
        return _fail("open_app:windows_search", "no pude presionar Enter")
    return _ok("open_app:windows_search", f"buscado '{canonical}'")


# ──────────────────────────────────────────────────────────────────────
# Estrategias: select_profile
# ──────────────────────────────────────────────────────────────────────

def _select_profile_skip_if_loaded(
    step: MissionStep, state: StateSnapshot
) -> StrategyResult:
    """Si Chrome ya tiene URL real (y el picker NO está visible),
    asumimos perfil cargado → skip.

    PRD 2026-05-03d §6: si no podemos verificar el perfil exacto,
    permitimos el skip pero lo marcamos como ``verified_profile=False``
    en el extra para que el LearningLogger lo distinga de un skip
    "fuerte" (título con nombre de perfil).
    """
    picker_titles = (
        "choose your profile", "elige tu perfil",
        "quién está usando", "who's using chrome",
        "choose a profile",
    )
    if state.title_contains(*picker_titles):
        return _fail("select_profile:skip_if_loaded",
                     "profile picker todavía visible")
    has_url = state.url_contains("http://", "https://") and not state.url_contains(
        "chrome://profile-picker"
    )
    if not has_url:
        return _fail("select_profile:skip_if_loaded",
                     "todavía en picker o cargando")
    profile = (step.params.get("profile_name") or "").strip()
    if profile and state.title_contains(profile):
        return _ok(
            "select_profile:skip_if_loaded",
            f"perfil '{profile}' verificado en título",
            verified_profile=True,
        )
    # Sin pista del perfil exacto: skip SUAVE — no es un éxito silencioso,
    # lo marcamos como verified_profile=False para la auditoría.
    return _ok(
        "select_profile:skip_if_loaded",
        "Chrome ya cargado (perfil no verificable explícitamente)",
        verified_profile=False,
    )


def _select_profile_uia_text(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    if not HAS_UIA:
        return _fail("select_profile:uia_text_match", "uiautomation no disponible")
    profile = (step.params.get("profile_name") or "").strip()
    if not profile:
        return _fail(
            "select_profile:uia_text_match",
            "sin profile_name — pidiendo intervención",
            ask_human=True,
        )
    try:
        # Buscamos un control con Name == profile_name dentro del profile picker.
        picker = auto.WindowControl(searchDepth=2, ClassName="Chrome_WidgetWin_1")
        if not picker.Exists(0.5, 0):
            return _fail("select_profile:uia_text_match", "profile picker no visible")
        target = picker.TextControl(searchDepth=10, Name=profile)
        if not target.Exists(0.8, 0):
            target = picker.ButtonControl(searchDepth=10, Name=profile)
        if not target.Exists(0.8, 0):
            return _fail(
                "select_profile:uia_text_match",
                f"perfil '{profile}' no encontrado por UIA",
            )
        try:
            target.Click(simulateMove=False)
        except Exception:
            target.GetClickablePoint()  # type: ignore
            return _fail("select_profile:uia_text_match", "click UIA falló")
        return _ok("select_profile:uia_text_match", f"click '{profile}'")
    except Exception as e:
        return _fail("select_profile:uia_text_match", str(e))


def _select_profile_visible_text_ocr(
    step: MissionStep, state: StateSnapshot
) -> StrategyResult:
    """OCR ligero: si el nombre del perfil aparece en visible_text, click.

    Por ahora reportamos como ``ok=False`` cuando no podemos confirmar:
    OCR full-screen es caro, y preferimos que el RecoveryEngine pida
    intervención antes que clickear coordenadas adivinadas. El gancho
    queda listo para que un módulo de visión (no implementado aquí)
    rellene este hueco.
    """
    profile = (step.params.get("profile_name") or "").strip()
    if not profile:
        return _fail(
            "select_profile:visible_text_ocr",
            "sin profile_name",
            ask_human=True,
        )
    visible = state.visible_text or ""
    if profile.lower() in visible.lower():
        # En este punto sabríamos las coords del texto. Como el state
        # detector no las almacena, el SmartExecutor llamará al
        # RecoveryEngine con la receta visual.
        return _fail(
            "select_profile:visible_text_ocr",
            f"texto '{profile}' visible pero sin coords — pasar a visual_asset",
        )
    return _fail(
        "select_profile:visible_text_ocr",
        f"'{profile}' no aparece en visible_text",
    )


def _select_profile_visual_asset(
    step: MissionStep, _state: StateSnapshot
) -> StrategyResult:
    """Hook para template-match con un asset guardado del perfil.

    Si el step trae ``params['profile_asset_path']`` apuntando a un PNG
    del avatar/nombre, hacemos pyautogui.locateOnScreen.
    """
    asset = (step.params.get("profile_asset_path") or "").strip()
    if not asset or not os.path.exists(asset):
        return _fail("select_profile:visual_asset", "sin asset visual")
    if not HAS_PYAUTOGUI:
        return _fail("select_profile:visual_asset", "pyautogui no disponible")
    try:
        loc = pyautogui.locateCenterOnScreen(asset, confidence=0.85)  # type: ignore
        if not loc:
            return _fail("select_profile:visual_asset", "asset no encontrado en pantalla")
        pyautogui.click(loc.x, loc.y)
        return _ok("select_profile:visual_asset", f"click visual ({loc.x},{loc.y})")
    except Exception as e:
        return _fail("select_profile:visual_asset", str(e))


def _select_profile_relative_coords(
    step: MissionStep, state: StateSnapshot
) -> StrategyResult:
    """Coords relativas SOLO si el usuario las confirmó previamente.

    Convención: ``params['confirmed_coords']`` = ``{"x": int, "y": int}``
    en el sistema de la ventana del picker. Sin confirmación, paramos
    para evitar clickear donde no toca.
    """
    coords = step.params.get("confirmed_coords")
    if not isinstance(coords, dict) or "x" not in coords or "y" not in coords:
        return _fail(
            "select_profile:relative_coords",
            "no hay coords confirmadas; pidiendo intervención",
            ask_human=True,
        )
    if not HAS_PYAUTOGUI:
        return _fail("select_profile:relative_coords", "pyautogui no disponible")
    bbox = state.active_window_bbox or {}
    x = int(coords["x"]) + int(bbox.get("x", 0))
    y = int(coords["y"]) + int(bbox.get("y", 0))
    try:
        pyautogui.click(x, y)
        return _ok("select_profile:relative_coords", f"click rel ({x},{y})")
    except Exception as e:
        return _fail("select_profile:relative_coords", str(e))


# ──────────────────────────────────────────────────────────────────────
# Estrategias: open_new_tab
# ──────────────────────────────────────────────────────────────────────

def _open_new_tab_hotkey(_step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    if _hotkey("ctrl", "t"):
        return _ok("open_new_tab:hotkey_ctrl_t", "Ctrl+T enviado")
    return _fail("open_new_tab:hotkey_ctrl_t", "no pude enviar Ctrl+T")


def _open_new_tab_uia(_step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    if not HAS_UIA:
        return _fail("open_new_tab:uia_button", "uiautomation no disponible")
    try:
        win = auto.GetForegroundControl()
        if not win:
            return _fail("open_new_tab:uia_button", "no hay foco")
        names_order: List[str] = []
        pref = (_step.params or {}).get("arl_uia_control_names_try_first")
        if isinstance(pref, str) and pref.strip():
            names_order.append(pref.strip())
        elif isinstance(pref, (list, tuple)):
            for x in pref:
                xs = str(x).strip()
                if xs:
                    names_order.append(xs)
        for name in (
            *names_order,
            "Nueva pestaña",
            "New Tab",
            "Nueva pestaña (Ctrl+T)",
        ):
            btn = win.ButtonControl(searchDepth=10, Name=name)
            if btn.Exists(0.4, 0):
                btn.Click(simulateMove=False)
                return _ok("open_new_tab:uia_button", f"click '{name}'")
        return _fail("open_new_tab:uia_button", "botón nueva pestaña no encontrado")
    except Exception as e:
        return _fail("open_new_tab:uia_button", str(e))


def _open_new_tab_vision(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    """Hook visual: requiere un asset del botón "+" guardado."""
    asset = (step.params.get("plus_button_asset") or "").strip()
    if not asset or not os.path.exists(asset) or not HAS_PYAUTOGUI:
        return _fail("open_new_tab:vision_coords", "sin asset visual / pyautogui")
    try:
        loc = pyautogui.locateCenterOnScreen(asset, confidence=0.85)  # type: ignore
        if not loc:
            return _fail("open_new_tab:vision_coords", "asset '+' no encontrado")
        pyautogui.click(loc.x, loc.y)
        return _ok("open_new_tab:vision_coords", f"click ({loc.x},{loc.y})")
    except Exception as e:
        return _fail("open_new_tab:vision_coords", str(e))


# ──────────────────────────────────────────────────────────────────────
# Estrategias: open_url
# ──────────────────────────────────────────────────────────────────────

def _resolve_url(step: MissionStep) -> str:
    """Reusa la lógica de alias del módulo de contratos."""
    from app.services.missions.execution_contracts import resolve_url_alias
    url, _domain = resolve_url_alias(step)
    return url


def _open_url_hotkey(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    """Ctrl+L → limpia la barra → escribe URL → Enter (PRD §7).

    Limpiar la barra con Ctrl+A + Del antes de escribir evita pegar la
    URL "al final" de lo que ya había — bug observado cuando Chrome
    deja la última URL seleccionada y typewrite escribe al lado.
    """
    url = _resolve_url(step)
    if not url:
        return _fail("open_url:hotkey_ctrl_l", "sin URL")
    _ensure_browser_foreground()
    if not _hotkey("ctrl", "l"):
        return _fail("open_url:hotkey_ctrl_l", "no pude enfocar address bar")
    time.sleep(0.25)
    # Limpiar cualquier texto previo en la omnibox.
    _hotkey("ctrl", "a")
    time.sleep(0.05)
    _press("delete")
    time.sleep(0.08)
    if not _type_text(url, interval=0.005):
        return _fail("open_url:hotkey_ctrl_l", "no pude escribir URL")
    time.sleep(0.15)
    if not _press("enter"):
        return _fail("open_url:hotkey_ctrl_l", "no pude presionar Enter")
    return _ok("open_url:hotkey_ctrl_l", f"navegando a {url}")


def _open_url_playwright_goto(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    url = _resolve_url(step)
    if not url:
        return _fail("open_url:playwright_goto", "sin URL")
    page = _get_playwright_page(connect_only=False)
    if page is None:
        return _fail("open_url:playwright_goto", "sin página Playwright")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=12000)
        try:
            page.bring_to_front()
        except Exception:
            pass
        return _ok("open_url:playwright_goto", f"page.goto({url})")
    except Exception as e:
        return _fail("open_url:playwright_goto", str(e))


def _open_url_bookmark_uia(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    """Click por UIA en el bookmark con nombre = alias."""
    if not HAS_UIA:
        return _fail("open_url:bookmark_uia", "uiautomation no disponible")
    name = (step.params.get("alias") or "").strip()
    if not name:
        return _fail("open_url:bookmark_uia", "sin alias para bookmark")
    try:
        win = auto.GetForegroundControl()
        if not win:
            return _fail("open_url:bookmark_uia", "no hay foco")
        bm = win.ButtonControl(searchDepth=12, Name=name)
        if not bm.Exists(0.5, 0):
            bm = win.HyperlinkControl(searchDepth=12, Name=name)
        if not bm.Exists(0.5, 0):
            return _fail("open_url:bookmark_uia", f"bookmark '{name}' no visible")
        bm.Click(simulateMove=False)
        return _ok("open_url:bookmark_uia", f"click bookmark '{name}'")
    except Exception as e:
        return _fail("open_url:bookmark_uia", str(e))


def _open_url_vision(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    asset = (step.params.get("bookmark_asset") or "").strip()
    if not asset or not os.path.exists(asset) or not HAS_PYAUTOGUI:
        return _fail("open_url:vision_coords", "sin asset visual / pyautogui")
    try:
        loc = pyautogui.locateCenterOnScreen(asset, confidence=0.85)  # type: ignore
        if not loc:
            return _fail("open_url:vision_coords", "asset bookmark no encontrado")
        pyautogui.click(loc.x, loc.y)
        return _ok("open_url:vision_coords", f"click ({loc.x},{loc.y})")
    except Exception as e:
        return _fail("open_url:vision_coords", str(e))


# ──────────────────────────────────────────────────────────────────────
# Estrategias: search_youtube
# ──────────────────────────────────────────────────────────────────────

def _search_youtube_dom(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    query = (step.params.get("query") or "").strip()
    if not query:
        return _fail("search_youtube:dom_input", "sin query")
    page = _get_playwright_page(connect_only=False)
    if page is None:
        return _fail("search_youtube:dom_input", "sin página Playwright")
    try:
        # Selector canónico de YouTube. Ojo: en algunas variantes la
        # caja vive en un shadow DOM; ``input[name="search_query"]``
        # sigue siendo accesible.
        loc = page.locator('input[name="search_query"]').first
        loc.wait_for(state="visible", timeout=4000)
        loc.fill(query)
        loc.press("Enter")
        return _ok("search_youtube:dom_input", f"buscado '{query}'")
    except Exception as e:
        return _fail("search_youtube:dom_input", str(e))


def _search_youtube_dom_search_input(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Selector alternativo DOM ``#search-input input`` (PRD §8).

    Algunas variantes del layout de YouTube renderizan la caja dentro
    de un web component ``ytd-searchbox``; el shadow-root expone
    ``#search-input > input``. Probar este selector antes de caer a
    UIA reduce la latencia típica ~300 ms cuando el primer selector
    canónico falla por A/B tests de YouTube.
    """
    query = (step.params.get("query") or "").strip()
    if not query:
        return _fail("search_youtube:dom_search_input", "sin query")
    page = _get_playwright_page(connect_only=False)
    if page is None:
        return _fail("search_youtube:dom_search_input",
                     "sin página Playwright")
    try:
        # ``#search-input input`` es el selector más resiliente a
        # cambios; atravesarlo con "piercing" no hace falta porque
        # Playwright cruza shadow DOM por defecto.
        loc = page.locator("#search-input input").first
        loc.wait_for(state="visible", timeout=3500)
        try:
            loc.click()
        except Exception:
            pass
        loc.fill(query)
        loc.press("Enter")
        return _ok(
            "search_youtube:dom_search_input",
            f"buscado '{query}' (fallback DOM)",
        )
    except Exception as e:
        return _fail("search_youtube:dom_search_input", str(e))


def _search_youtube_uia(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    if not HAS_UIA:
        return _fail("search_youtube:uia_searchbox", "uiautomation no disponible")
    query = (step.params.get("query") or "").strip()
    if not query:
        return _fail("search_youtube:uia_searchbox", "sin query")
    try:
        win = auto.GetForegroundControl()
        if not win:
            return _fail("search_youtube:uia_searchbox", "no hay foco")
        for name in ("Buscar", "Search", "Search YouTube", "Buscar en YouTube"):
            box = win.EditControl(searchDepth=12, Name=name)
            if box.Exists(0.4, 0):
                try:
                    box.Click(simulateMove=False)
                    box.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
                    box.SendKeys(query, waitTime=0.02)
                    box.SendKeys("{Enter}", waitTime=0.05)
                    return _ok("search_youtube:uia_searchbox", f"UIA '{name}' → {query}")
                except Exception as e:
                    log.debug(f"uia search input fallo: {e}")
                    continue
        return _fail("search_youtube:uia_searchbox", "searchbox UIA no encontrada")
    except Exception as e:
        return _fail("search_youtube:uia_searchbox", str(e))


def _search_youtube_ocr(step: MissionStep, state: StateSnapshot) -> StrategyResult:
    """Si visible_text contiene 'Buscar', clickear esa zona — gancho
    para el módulo de visión. Hoy reporta False y el recovery escala."""
    visible = state.visible_text or ""
    if "buscar" in visible.lower() or "search" in visible.lower():
        return _fail(
            "search_youtube:ocr_buscar",
            "texto 'Buscar' detectado pero OCR de coords no implementado",
        )
    return _fail("search_youtube:ocr_buscar", "no veo texto 'Buscar'")


def _search_youtube_visual(step: MissionStep, _state: StateSnapshot) -> StrategyResult:
    asset = (step.params.get("search_asset") or "").strip()
    query = (step.params.get("query") or "").strip()
    if not asset or not os.path.exists(asset) or not HAS_PYAUTOGUI:
        return _fail("search_youtube:visual_asset", "sin asset visual / pyautogui")
    try:
        loc = pyautogui.locateCenterOnScreen(asset, confidence=0.85)  # type: ignore
        if not loc:
            return _fail("search_youtube:visual_asset", "search box visual no encontrado")
        pyautogui.click(loc.x, loc.y)
        time.sleep(0.2)
        if query:
            pyautogui.typewrite(query, interval=0.01)
            pyautogui.press("enter")
        return _ok("search_youtube:visual_asset", f"click+type ({loc.x},{loc.y})")
    except Exception as e:
        return _fail("search_youtube:visual_asset", str(e))


def _search_youtube_relative(step: MissionStep, state: StateSnapshot) -> StrategyResult:
    coords = step.params.get("search_coords_rel")
    query = (step.params.get("query") or "").strip()
    if not isinstance(coords, dict) or "fx" not in coords or "fy" not in coords:
        return _fail(
            "search_youtube:relative_coords",
            "sin coords relativas confirmadas",
            ask_human=True,
        )
    if not HAS_PYAUTOGUI:
        return _fail("search_youtube:relative_coords", "pyautogui no disponible")
    bbox = state.active_window_bbox or {}
    if not bbox:
        return _fail("search_youtube:relative_coords", "sin bbox de ventana")
    x = int(bbox.get("x", 0)) + int(coords["fx"] * bbox.get("w", 0))
    y = int(bbox.get("y", 0)) + int(coords["fy"] * bbox.get("h", 0))
    try:
        pyautogui.click(x, y)
        time.sleep(0.2)
        if query:
            pyautogui.typewrite(query, interval=0.01)
            pyautogui.press("enter")
        return _ok("search_youtube:relative_coords", f"click rel ({x},{y})")
    except Exception as e:
        return _fail("search_youtube:relative_coords", str(e))


# ──────────────────────────────────────────────────────────────────────
# Estrategias: scroll_results
# ──────────────────────────────────────────────────────────────────────


def _scroll_amount_clicks(step: MissionStep) -> int:
    """Cuántos clicks de rueda dar (default 2)."""
    raw = step.params.get("amount", 2)
    try:
        n = max(1, int(raw))
    except Exception:
        n = 2
    return min(n, 20)


def _scroll_direction(step: MissionStep) -> str:
    d = str(step.params.get("direction") or "down").strip().lower()
    return d if d in {"down", "up"} else "down"


def _scroll_results_wheel(
    step: MissionStep, _state: StateSnapshot
) -> StrategyResult:
    """Scroll preferred: rueda del mouse (sin clicar nada)."""
    if not HAS_PYAUTOGUI:
        return _fail("scroll_results:wheel", "pyautogui no disponible")
    n = _scroll_amount_clicks(step)
    direction = _scroll_direction(step)
    # Magnitud: cada "click" del usuario equivale aprox. a 600 px.
    magnitude = 600 * n * (1 if direction == "down" else -1)
    try:
        pyautogui.scroll(-magnitude)  # invertimos: scroll(-) = abajo
        return _ok(
            "scroll_results:wheel",
            f"wheel dir={direction} amount={n}",
        )
    except Exception as e:
        return _fail("scroll_results:wheel", f"wheel falló: {e}")


def _scroll_results_keyboard(
    step: MissionStep, _state: StateSnapshot
) -> StrategyResult:
    """Scroll por teclado: PageDown/PageUp."""
    if not HAS_PYAUTOGUI:
        return _fail("scroll_results:keyboard", "pyautogui no disponible")
    n = _scroll_amount_clicks(step)
    direction = _scroll_direction(step)
    key = "pagedown" if direction == "down" else "pageup"
    try:
        for _ in range(n):
            pyautogui.press(key)
            time.sleep(0.05)
        return _ok(
            "scroll_results:keyboard",
            f"keyboard dir={direction} amount={n}",
        )
    except Exception as e:
        return _fail("scroll_results:keyboard", f"keyboard falló: {e}")


def _scroll_results_js_scrollby(
    step: MissionStep, _state: StateSnapshot
) -> StrategyResult:
    """Scroll vía Playwright (solo si hay browser conectado)."""
    page = _get_playwright_page(connect_only=True)
    if page is None:
        return _fail(
            "scroll_results:js_scrollby",
            "playwright no disponible",
        )
    n = _scroll_amount_clicks(step)
    direction = _scroll_direction(step)
    delta = 800 * n * (1 if direction == "down" else -1)
    try:
        page.evaluate(f"window.scrollBy(0, {delta});")
        return _ok(
            "scroll_results:js_scrollby",
            f"js scrollBy(0,{delta})",
        )
    except Exception as e:
        return _fail("scroll_results:js_scrollby", f"js scroll falló: {e}")


# ──────────────────────────────────────────────────────────────────────
# Estrategias: use_selected_profile (PRD 2026-05-09 §exec-runtime)
# ──────────────────────────────────────────────────────────────────────


def _use_selected_profile_noop(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """No-op seguro: el usuario indicó "usa el perfil ya seleccionado".

    Si Chrome está corriendo, devolvemos ``ok`` y el executor avanza
    al siguiente paso. No tocamos el profile picker para no romper la
    sesión activa del usuario.
    """
    name = (step.params.get("app") or "chrome").strip()
    if not is_app_running(name):
        return _fail(
            "use_selected_profile:noop",
            f"{name} no está corriendo (no se puede usar perfil activo)",
        )
    return _ok(
        "use_selected_profile:noop",
        f"{name} activo — usando perfil ya seleccionado",
    )


def _use_selected_profile_check_chrome(
    step: MissionStep, state: StateSnapshot,
) -> StrategyResult:
    """Fallback de validación: verificamos Chrome activo en state.

    Útil si ``is_app_running`` falla por alguna razón ambiental — el
    StateSnapshot ya tiene la verdad sobre procesos visibles.
    """
    if state.is_process_running("chrome.exe", "msedge.exe"):
        return _ok(
            "use_selected_profile:check_chrome_active",
            "navegador activo confirmado vía state",
        )
    return _fail(
        "use_selected_profile:check_chrome_active",
        "navegador no activo en state",
    )


# ──────────────────────────────────────────────────────────────────────
# Estrategias: search_web (PRD 2026-05-09 §exec-runtime)
# ──────────────────────────────────────────────────────────────────────


def _search_web_address_bar(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Ctrl+L → limpiar barra → tipear query → Enter.

    Es el camino canónico para una búsqueda desde la barra de
    direcciones (omnibox). Cubre Chrome/Edge/Brave/Firefox.
    """
    query = (step.params.get("query") or "").strip()
    if not query:
        return _fail("search_web:address_bar_type_enter", "sin query")
    _ensure_browser_foreground()
    if not _hotkey("ctrl", "l"):
        return _fail(
            "search_web:address_bar_type_enter",
            "no pude enfocar la barra de direcciones",
        )
    time.sleep(0.25)
    _hotkey("ctrl", "a")
    time.sleep(0.05)
    _press("delete")
    time.sleep(0.08)
    if not _type_text(query, interval=0.01):
        return _fail(
            "search_web:address_bar_type_enter",
            "no pude tipear la query",
        )
    time.sleep(0.15)
    submit = bool(step.params.get("submit", True))
    if submit and not _press("enter"):
        return _fail(
            "search_web:address_bar_type_enter",
            "no pude presionar Enter",
        )
    return _ok(
        "search_web:address_bar_type_enter",
        f"buscando '{query}' en omnibox",
    )


def _search_web_dom_search_input(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Fallback DOM: input genérico de búsqueda (Playwright).

    Algunos sitios renderizan su propio buscador (no se usa la
    omnibox). Si Playwright está conectado, intentamos llenarlo.
    """
    query = (step.params.get("query") or "").strip()
    if not query:
        return _fail("search_web:dom_search_input", "sin query")
    page = _get_playwright_page(connect_only=False)
    if page is None:
        return _fail(
            "search_web:dom_search_input",
            "playwright no disponible",
        )
    try:
        loc = page.locator(
            "input[type='search'], input[name='q'], input[name='search_query']"
        ).first
        loc.wait_for(state="visible", timeout=3500)
        loc.fill(query)
        loc.press("Enter")
        return _ok(
            "search_web:dom_search_input",
            f"buscado '{query}' (DOM)",
        )
    except Exception as e:
        return _fail("search_web:dom_search_input", str(e))


def _search_web_keyboard_focus(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Último recurso: asume foco actual y tipea + Enter.

    No abre la omnibox — útil si el usuario ya tenía el cursor en un
    campo de texto. Se ejecuta sólo como fallback para no pisar texto
    en sitios random.
    """
    query = (step.params.get("query") or "").strip()
    if not query:
        return _fail("search_web:keyboard_focus_then_type", "sin query")
    if not _type_text(query, interval=0.015):
        return _fail(
            "search_web:keyboard_focus_then_type",
            "no pude tipear la query",
        )
    submit = bool(step.params.get("submit", True))
    if submit and not _press("enter"):
        return _fail(
            "search_web:keyboard_focus_then_type",
            "no pude presionar Enter",
        )
    return _ok(
        "search_web:keyboard_focus_then_type",
        f"buscado '{query}' (focus actual)",
    )


# ──────────────────────────────────────────────────────────────────────
# Estrategias: open_search_result (PRD 2026-05-09 §exec-runtime)
# ──────────────────────────────────────────────────────────────────────


def _open_search_result_dom_link(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Click en un link cuyo texto contiene ``params['title']``.

    Preferimos DOM porque es más rápido y exacto que UIA en páginas
    web. Si no hay browser conectado, devolvemos fail y el executor
    cae al fallback UIA.
    """
    title = (step.params.get("title") or "").strip()
    if not title:
        return _fail("open_search_result:dom_link_by_title", "sin título")
    page = _get_playwright_page(connect_only=False)
    if page is None:
        return _fail(
            "open_search_result:dom_link_by_title",
            "playwright no disponible",
        )
    try:
        # Usamos un fragmento "fuerte" del título para tolerar
        # variaciones (acentos, sufijos como "- Wikipedia").
        first_token = ""
        for tok in title.replace(",", " ").split():
            if len(tok) >= 4:
                first_token = tok
                break
        target = first_token or title
        loc = page.get_by_role("link", name=target).first  # type: ignore
        loc.wait_for(state="visible", timeout=4000)
        loc.click()
        return _ok(
            "open_search_result:dom_link_by_title",
            f"click DOM link '{target}'",
        )
    except Exception as e:
        return _fail("open_search_result:dom_link_by_title", str(e))


def _open_search_result_uia(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Fallback UIA: busca un control con Name == fragmento del título."""
    if not HAS_UIA:
        return _fail(
            "open_search_result:uia_text_match",
            "uiautomation no disponible",
        )
    title = (step.params.get("title") or "").strip()
    if not title:
        return _fail("open_search_result:uia_text_match", "sin título")
    try:
        win = auto.GetForegroundControl()
        if not win:
            return _fail(
                "open_search_result:uia_text_match", "no hay foco",
            )
        first_token = ""
        for tok in title.replace(",", " ").split():
            if len(tok) >= 4:
                first_token = tok
                break
        target_name = first_token or title
        ctrl = win.HyperlinkControl(searchDepth=15, SubName=target_name)
        if not ctrl.Exists(0.6, 0):
            ctrl = win.TextControl(searchDepth=15, SubName=target_name)
        if not ctrl.Exists(0.6, 0):
            return _fail(
                "open_search_result:uia_text_match",
                f"resultado '{target_name}' no encontrado vía UIA",
            )
        try:
            ctrl.Click(simulateMove=False)
        except Exception:
            return _fail(
                "open_search_result:uia_text_match",
                "click UIA falló",
            )
        return _ok(
            "open_search_result:uia_text_match",
            f"click UIA '{target_name}'",
        )
    except Exception as e:
        return _fail("open_search_result:uia_text_match", str(e))


def _open_search_result_keyboard(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Último recurso: Tab al primer link foco-able y Enter.

    Útil cuando ni DOM ni UIA pudieron localizar el resultado, pero
    sabemos que estamos sobre una página de resultados típica.
    """
    if not HAS_PYAUTOGUI:
        return _fail(
            "open_search_result:keyboard_tab_then_enter",
            "pyautogui no disponible",
        )
    try:
        for _ in range(5):
            pyautogui.press("tab")
            time.sleep(0.05)
        pyautogui.press("enter")
        return _ok(
            "open_search_result:keyboard_tab_then_enter",
            "Tab×5 + Enter (heurística)",
        )
    except Exception as e:
        return _fail(
            "open_search_result:keyboard_tab_then_enter", str(e),
        )


# ──────────────────────────────────────────────────────────────────────
# Estrategias: scroll_page (PRD 2026-05-09 §exec-runtime)
# ──────────────────────────────────────────────────────────────────────


def _scroll_page_wheel(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Scroll con la rueda del mouse — equivalente genérico de
    ``scroll_results:wheel`` pero sin asumir contexto YouTube.
    """
    if not HAS_PYAUTOGUI:
        return _fail("scroll_page:wheel", "pyautogui no disponible")
    n = _scroll_amount_clicks(step)
    direction = _scroll_direction(step)
    magnitude = 600 * n * (1 if direction == "down" else -1)
    try:
        pyautogui.scroll(-magnitude)
        return _ok(
            "scroll_page:wheel",
            f"wheel dir={direction} amount={n}",
        )
    except Exception as e:
        return _fail("scroll_page:wheel", f"wheel falló: {e}")


def _scroll_page_keyboard(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Scroll con PageDown/PageUp."""
    if not HAS_PYAUTOGUI:
        return _fail("scroll_page:keyboard", "pyautogui no disponible")
    n = _scroll_amount_clicks(step)
    direction = _scroll_direction(step)
    key = "pagedown" if direction == "down" else "pageup"
    try:
        for _ in range(n):
            pyautogui.press(key)
            time.sleep(0.05)
        return _ok(
            "scroll_page:keyboard",
            f"keyboard dir={direction} amount={n}",
        )
    except Exception as e:
        return _fail("scroll_page:keyboard", f"keyboard falló: {e}")


def _scroll_page_js_scrollby(
    step: MissionStep, _state: StateSnapshot,
) -> StrategyResult:
    """Scroll vía Playwright (window.scrollBy)."""
    page = _get_playwright_page(connect_only=True)
    if page is None:
        return _fail("scroll_page:js_scrollby", "playwright no disponible")
    n = _scroll_amount_clicks(step)
    direction = _scroll_direction(step)
    delta = 800 * n * (1 if direction == "down" else -1)
    try:
        page.evaluate(f"window.scrollBy(0, {delta});")
        return _ok(
            "scroll_page:js_scrollby",
            f"js scrollBy(0,{delta})",
        )
    except Exception as e:
        return _fail("scroll_page:js_scrollby", f"js scroll falló: {e}")


# ──────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────
# Estrategias genéricas reutilizables (Semantic Intent Promotion Engine).
#
# El SIPE emite intents genéricos (``open_site``, ``search_site``,
# ``web_search``) con ``params["site"]`` o ``params["engine"]`` que
# identifica el destino. Estas estrategias hacen *smart routing* hacia
# las estrategias específicas existentes según el sitio/engine que
# venga en el step.
#
# Diseño:
#   * NO duplican lógica ejecutiva — solo elijen y delegan.
#   * Si ``site`` no se reconoce, caen a la estrategia genérica más
#     conservadora (``search_web:address_bar_type_enter`` para búsquedas
#     y ``open_url:hotkey_ctrl_l`` para aperturas).
#   * Mantienen el mismo contrato de ``StrategyResult``.
# ──────────────────────────────────────────────────────────────────────


def _relabeled_strategy_result(res: StrategyResult, strategy: str) -> StrategyResult:
    """Preserva ok/message/duración pero uniforma la etiqueta de estrategia."""
    return StrategyResult(
        ok=res.ok,
        strategy=strategy,
        message=res.message,
        duration_ms=res.duration_ms,
        extra=res.extra,
    )


def _search_site_youtube_dom_input(
    step: MissionStep, state: StateSnapshot,
) -> StrategyResult:
    """Misma ejecución que ``search_youtube:dom_input`` con etiqueta canónica."""
    inner = _search_youtube_dom(step, state)
    return _relabeled_strategy_result(inner, "search_site:youtube_dom_input")


def _open_site_smart_route(
    step: MissionStep, state: StateSnapshot,
) -> StrategyResult:
    """``open_site`` genérico: abre cualquier sitio web por URL.

    Reusa ``open_url:hotkey_ctrl_l`` (Ctrl+L + URL + Enter) — que es
    independiente del sitio. ``params["site"]`` es solo metadata para
    UX/logs (no cambia la ejecución).
    """
    return _open_url_hotkey(step, state)


def _search_site_smart_route(
    step: MissionStep, state: StateSnapshot,
) -> StrategyResult:
    """``search_site`` genérico: busca dentro de un sitio.

    Si ``params["site"]`` es ``"youtube"``, delega a
    ``search_youtube:dom_input`` (DOM-first). Para cualquier otro sitio
    cae a ``search_web:address_bar_type_enter`` que es el camino
    universal (Ctrl+L + query + Enter cae en la omnibox y el navegador
    decide a qué motor mandarla).
    """
    site = (
        str(step.params.get("site") or step.params.get("search_site") or "")
        .strip()
        .lower()
    )
    if site == "youtube":
        return _search_youtube_dom(step, state)
    return _search_web_address_bar(step, state)


def _web_search_smart_route(
    step: MissionStep, state: StateSnapshot,
) -> StrategyResult:
    """``web_search`` genérico: búsqueda en buscador (Google/Bing/...).

    Delega a ``search_web:address_bar_type_enter``. ``params["engine"]``
    es metadata informativa.
    """
    return _search_web_address_bar(step, state)


_STRATEGY_REGISTRY: Dict[str, StrategyFn] = {
    # open_app
    "open_app:os_startfile": _open_app_os_startfile,
    "open_app:start_command": _open_app_start_command,
    "open_app:windows_search": _open_app_windows_search,
    # select_profile
    "select_profile:skip_if_loaded": _select_profile_skip_if_loaded,
    "select_profile:uia_text_match": _select_profile_uia_text,
    "select_profile:visible_text_ocr": _select_profile_visible_text_ocr,
    "select_profile:visual_asset": _select_profile_visual_asset,
    "select_profile:relative_coords": _select_profile_relative_coords,
    # open_new_tab
    "open_new_tab:hotkey_ctrl_t": _open_new_tab_hotkey,
    "open_new_tab:uia_button": _open_new_tab_uia,
    "open_new_tab:vision_coords": _open_new_tab_vision,
    # open_url
    "open_url:hotkey_ctrl_l": _open_url_hotkey,
    "open_url:playwright_goto": _open_url_playwright_goto,
    "open_url:bookmark_uia": _open_url_bookmark_uia,
    "open_url:vision_coords": _open_url_vision,
    # search_youtube
    "search_youtube:dom_input": _search_youtube_dom,
    "search_youtube:dom_search_input": _search_youtube_dom_search_input,
    "search_youtube:uia_searchbox": _search_youtube_uia,
    "search_youtube:ocr_buscar": _search_youtube_ocr,
    "search_youtube:visual_asset": _search_youtube_visual,
    "search_youtube:relative_coords": _search_youtube_relative,
    "search_site:youtube_dom_input": _search_site_youtube_dom_input,
    # scroll_results
    "scroll_results:wheel": _scroll_results_wheel,
    "scroll_results:keyboard": _scroll_results_keyboard,
    "scroll_results:js_scrollby": _scroll_results_js_scrollby,
    # use_selected_profile (PRD 2026-05-09 §exec-runtime)
    "use_selected_profile:noop": _use_selected_profile_noop,
    "use_selected_profile:check_chrome_active": _use_selected_profile_check_chrome,
    # search_web (PRD 2026-05-09 §exec-runtime)
    "search_web:address_bar_type_enter": _search_web_address_bar,
    "search_web:dom_search_input": _search_web_dom_search_input,
    "search_web:keyboard_focus_then_type": _search_web_keyboard_focus,
    # open_search_result (PRD 2026-05-09 §exec-runtime)
    "open_search_result:dom_link_by_title": _open_search_result_dom_link,
    "open_search_result:uia_text_match": _open_search_result_uia,
    "open_search_result:keyboard_tab_then_enter": _open_search_result_keyboard,
    # scroll_page (PRD 2026-05-09 §exec-runtime)
    "scroll_page:wheel": _scroll_page_wheel,
    "scroll_page:keyboard": _scroll_page_keyboard,
    "scroll_page:js_scrollby": _scroll_page_js_scrollby,
    # ── Generic patterns (Semantic Intent Promotion Engine) ──────────
    "open_site:smart_route": _open_site_smart_route,
    "search_site:smart_route": _search_site_smart_route,
    "search_content:smart_route": _search_site_smart_route,
    "web_search:smart_route": _web_search_smart_route,
}


def supported_strategies() -> List[str]:
    """Listado ordenado de estrategias registradas en el runtime.

    Útil para construir el mensaje del error
    ``UNSUPPORTED_SEMANTIC_STEP_TYPE`` (le mostramos al usuario qué
    estrategias hay para que sepa qué falta).
    """
    return sorted(_STRATEGY_REGISTRY.keys())


def register_strategy(name: str, fn: StrategyFn) -> None:
    """Permite registrar nuevas estrategias sin modificar este archivo."""
    _STRATEGY_REGISTRY[name] = fn


# ──────────────────────────────────────────────────────────────────────
# ActionRunner público
# ──────────────────────────────────────────────────────────────────────

class ActionRunner:
    """Despacha estrategias por nombre.

    Uso:
        runner = ActionRunner()
        result = runner.run_strategy("open_url:hotkey_ctrl_l", step, state)

    ``mission_ref`` opcional: handlers UOL usan COI/UCES de la misión.
    """

    def __init__(self, mission_ref: Optional[Any] = None) -> None:
        self.mission_ref = mission_ref

    def run_strategy(
        self,
        strategy: str,
        step: MissionStep,
        state: StateSnapshot,
    ) -> StrategyResult:
        try:
            from app.services.missions.uol_action_handlers import (
                is_uol_strategy,
                run_uol_strategy,
            )

            if is_uol_strategy(strategy):
                return run_uol_strategy(
                    strategy, step, state, mission=self.mission_ref,
                )
        except Exception as exc:
            log.debug("[ActionRunner] UOL dispatch: %s", exc)

        fn = _STRATEGY_REGISTRY.get(strategy)
        if fn is None:
            return _fail(strategy, f"estrategia desconocida: {strategy}")
        t0 = time.time()
        try:
            res = fn(step, state)
        except Exception as e:
            log.exception(f"strategy '{strategy}' raised")
            res = _fail(strategy, f"excepción: {e}")
        res.duration_ms = int((time.time() - t0) * 1000)
        return res


# Registro de estrategias UOL-native al cargar el runner.
try:
    from app.services.missions import uol_action_handlers as _uol_handlers  # noqa: F401

    _uol_handlers.register_uol_handlers()
except Exception as _uol_reg_exc:
    log.debug("[ActionRunner] UOL handler registration: %s", _uol_reg_exc)


__all__ = [
    "ActionRunner",
    "StrategyResult",
    "register_strategy",
    "supported_strategies",
]
