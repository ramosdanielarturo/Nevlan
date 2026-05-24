"""
Nevlan — Dynamic Wait Manager
==============================

Reemplaza ``sleep(ms)`` por ``WaitUntil(condition)``: el ejecutor
NO depende de delays humanos (que cambian con la latencia de red,
SSD, tema oscuro, antivirus…) sino de **condiciones reales del
sistema**.

Convertibles típicos
--------------------

    sleep(1500)   →  wait_until(chrome_open, timeout=10s)
    sleep(800)    →  wait_until(youtube_loaded, timeout=10s)
    sleep(500)    →  wait_until(input_visible, timeout=5s)
    sleep(2000)   →  wait_until(results_visible, timeout=10s)
    sleep(300)    →  wait_until(window_focused(title), timeout=3s)

API mental
----------

Cada *condición* es una callable ``(StateSnapshot) -> bool``.  La función
``wait_until`` corre ``state_detector.detect_state()`` cada N ms hasta
que la condición devuelve ``True`` o vence el timeout.

El módulo expone un **catálogo de condiciones** preescritas:

* ``chrome_open``: hay un proceso ``chrome.exe`` corriendo y al menos
  una ventana con clase Chrome activa.
* ``window_with_title(substr)``: ventana cuya foreground title contenga
  ``substr`` (case-insensitive).
* ``url_contains(substr)``: la URL del browser activo contiene ``substr``.
* ``youtube_loaded``: ``url_contains("youtube.com")`` AND la página
  Playwright responde.
* ``input_visible(role|name|css)``: el state ve un input UIA o DOM con
  los atributos pedidos.
* ``results_visible(site)``: heurística por sitio para "ya cargaron
  los resultados de la búsqueda".

NOTA: si ``state_detector`` no está disponible (entorno headless / CI),
``wait_until`` cae a ``time.sleep(timeout/2)`` para no bloquear los
tests pero deja warning en log.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from app.core.logger import log


# ──────────────────────────────────────────────────────────────────────
# Resultado de wait
# ──────────────────────────────────────────────────────────────────────

@dataclass
class WaitResult:
    """Resultado de una espera dinámica."""
    matched: bool = False
    elapsed_ms: int = 0
    polled: int = 0
    last_error: str = ""

    @property
    def timed_out(self) -> bool:
        return not self.matched


# ──────────────────────────────────────────────────────────────────────
# Loop principal
# ──────────────────────────────────────────────────────────────────────

def wait_until(
    condition: Callable[[Any], bool],
    *,
    timeout_ms: int = 10_000,
    poll_ms: int = 200,
    label: str = "",
    snapshot_factory: Optional[Callable[[], Any]] = None,
) -> WaitResult:
    """Espera hasta que la condición sea True o se agote el timeout.

    Args:
        condition: callable que recibe el ``StateSnapshot`` y devuelve
            ``True`` cuando la condición se cumple.
        timeout_ms: tope absoluto en milisegundos.
        poll_ms: intervalo entre polls de estado.
        label: etiqueta humana para logs (ej. "youtube_loaded").
        snapshot_factory: callable opcional que produce el snapshot. Si
            no se da, usamos ``state_detector.detect_state()``.

    Devuelve un ``WaitResult`` con ``matched/elapsed_ms/polled``.
    """
    started = time.monotonic()
    deadline = started + (timeout_ms / 1000.0)
    polled = 0
    last_err = ""
    factory = snapshot_factory or _default_snapshot_factory()

    while True:
        polled += 1
        try:
            snap = factory()
        except Exception as e:
            last_err = f"snapshot: {e}"
            snap = None
        if snap is not None:
            try:
                ok = bool(condition(snap))
            except Exception as e:
                last_err = f"condition: {e}"
                ok = False
            if ok:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                log.debug(
                    f"[wait_manager] '{label}' OK en {elapsed_ms}ms "
                    f"(polls={polled})"
                )
                return WaitResult(
                    matched=True, elapsed_ms=elapsed_ms,
                    polled=polled, last_error=last_err,
                )
        # Timeout?
        now = time.monotonic()
        if now >= deadline:
            elapsed_ms = int((now - started) * 1000)
            log.warning(
                f"[wait_manager] '{label}' TIMEOUT en {elapsed_ms}ms "
                f"(polls={polled}, last={last_err!r})"
            )
            return WaitResult(
                matched=False, elapsed_ms=elapsed_ms,
                polled=polled, last_error=last_err,
            )
        time.sleep(poll_ms / 1000.0)


def _default_snapshot_factory() -> Callable[[], Any]:
    """Devuelve un factory que llama a ``state_detector.detect_state``.

    Si ``state_detector`` no está importable (CI / tests),
    devolvemos un factory que produce ``None`` — la condición nunca
    será True y se irá a timeout, que es la respuesta segura.
    """
    try:
        from app.services.missions.state_detector import detect_state

        def factory() -> Any:
            return detect_state(quick=True)

        return factory
    except Exception as e:
        log.debug(f"[wait_manager] state_detector no disponible: {e}")

        def factory() -> Any:
            return None

        return factory


# ──────────────────────────────────────────────────────────────────────
# Catálogo de condiciones reusables
# ──────────────────────────────────────────────────────────────────────

def chrome_open(snap: Any) -> bool:
    """¿Hay un Chrome/Edge/Brave corriendo o foreground?"""
    if snap is None:
        return False
    procs = [str(p).lower() for p in (getattr(snap, "running_processes", []) or [])]
    if any(p in {"chrome.exe", "msedge.exe", "brave.exe"} for p in procs):
        return True
    app = (getattr(snap, "active_app", None) or "").lower()
    return app in {"chrome.exe", "msedge.exe", "brave.exe"}


def window_with_title(substr: str) -> Callable[[Any], bool]:
    """Cierre que verifica si la ventana foreground contiene ``substr``."""
    needle = (substr or "").strip().lower()

    def _cond(snap: Any) -> bool:
        if snap is None or not needle:
            return False
        title = (getattr(snap, "active_window_title", None) or "").lower()
        return needle in title

    _cond.__name__ = f"window_with_title({substr!r})"
    return _cond


def url_contains(substr: str) -> Callable[[Any], bool]:
    """¿La URL del browser activo contiene ``substr``?"""
    needle = (substr or "").strip().lower()

    def _cond(snap: Any) -> bool:
        if snap is None or not needle:
            return False
        url = (getattr(snap, "browser_url", None) or "").lower()
        return needle in url

    _cond.__name__ = f"url_contains({substr!r})"
    return _cond


def youtube_loaded(snap: Any) -> bool:
    """YouTube cargado: URL contiene youtube.com Y Playwright responde."""
    if snap is None:
        return False
    if not url_contains("youtube.com")(snap) and not url_contains("youtu.be")(snap):
        return False
    return bool(getattr(snap, "has_playwright_page", False))


def input_visible(
    *,
    role: Optional[str] = None,
    name: Optional[str] = None,
    css: Optional[str] = None,
) -> Callable[[Any], bool]:
    """¿El state detecta un input con esos atributos?

    Implementación pragmática: busca en ``uia_visible_names`` y, si hay
    Playwright disponible, en los DOM targets. Suficiente para los
    casos del PRD; el smart-executor ya tiene el resolver completo.
    """
    needle = (name or "").strip().lower()

    def _cond(snap: Any) -> bool:
        if snap is None:
            return False
        if needle:
            names = [str(n).lower() for n in getattr(snap, "uia_visible_names", [])]
            if any(needle in n for n in names):
                return True
        if css:
            return bool(getattr(snap, "dom_targets_available", False))
        if role:
            # Heurística suave: si hay UIA disponible, asumimos que el
            # role-checker más fino lo hará el resolver.
            return bool(getattr(snap, "uia_targets_available", False))
        return False

    label = name or css or role or "input"
    _cond.__name__ = f"input_visible({label})"
    return _cond


def results_visible(site: str) -> Callable[[Any], bool]:
    """Heurística por sitio: ya cargaron resultados.

    YouTube → URL incluye 'results' o '/watch'; visible_text incluye
              'YouTube' / 'vista' (idioma). Genérica: visible_text != ""
    """
    s = (site or "").strip().lower()

    def _cond(snap: Any) -> bool:
        if snap is None:
            return False
        if s == "youtube":
            url = (getattr(snap, "browser_url", None) or "").lower()
            if "youtube.com/results" in url or "youtube.com/watch" in url:
                return True
            text = (getattr(snap, "visible_text", "") or "").lower()
            return any(k in text for k in ("vista", "views", "subido", "uploaded"))
        # Genérica: hay texto visible no trivial.
        text = (getattr(snap, "visible_text", "") or "").strip()
        return len(text) > 50

    _cond.__name__ = f"results_visible({site})"
    return _cond


# ──────────────────────────────────────────────────────────────────────
# Conversor: misión legacy con sleeps → misión con waits
# ──────────────────────────────────────────────────────────────────────

# Mapa: contexto del step previo → condición a esperar tras él.
# Es heurístico pero cubre los casos del spec del usuario.
_DEFAULT_WAITS = {
    "open_app/chrome": ("chrome_open", 10_000),
    "open_app/msedge": ("chrome_open", 10_000),
    "open_url/youtube.com": ("youtube_loaded", 10_000),
    "open_new_tab": ("input_visible:address_bar", 5_000),
    "select_profile": ("window_with_title:Chrome", 10_000),
    "search_youtube": ("results_visible:youtube", 12_000),
}


def suggest_wait_for(prev_kind: str, prev_params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Dado el step anterior, sugiere un ``wait`` step intermedio.

    Devuelve un dict ``{"kind": "wait_for_state", "params": {...}}``
    listo para ser inyectado entre dos pasos del Smart Executor, o
    ``None`` si no aplica.

    Ejemplos:

        prev = open_app/chrome  →  wait_for_state(chrome_open, 10s)
        prev = open_url/youtube  →  wait_for_state(youtube_loaded, 10s)

    El compiler V2 lo usa para inyectar waits implícitos durante la
    expansión de la misión a steps de ejecución.
    """
    pk = prev_kind or ""
    p = prev_params or {}
    key = ""
    if pk == "open_app":
        name = str(p.get("name") or p.get("app") or "").lower()
        key = f"open_app/{name}"
    elif pk == "open_url":
        url = str(p.get("url") or p.get("alias") or "").lower()
        for host in ("youtube.com", "google.com", "github.com"):
            if host in url:
                key = f"open_url/{host}"
                break
    elif pk in {"open_new_tab", "select_profile", "search_youtube"}:
        key = pk

    if key not in _DEFAULT_WAITS:
        return None
    cond_name, timeout_ms = _DEFAULT_WAITS[key]
    return {
        "kind": "wait_for_state",
        "params": {
            "condition": cond_name,
            "timeout_ms": timeout_ms,
        },
    }


__all__ = [
    "WaitResult",
    "wait_until",
    "chrome_open",
    "window_with_title",
    "url_contains",
    "youtube_loaded",
    "input_visible",
    "results_visible",
    "suggest_wait_for",
]
