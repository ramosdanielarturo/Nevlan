"""
ArthurOS Services - StateDetector (Smart Executor)
--------------------------------------------------
Snapshot de la realidad ANTES (y DESPUÉS) de cada step de una misión.

Este módulo es el *"sentido común"* del Smart Executor: en lugar de
ejecutar pasos a ciegas, primero **mira el escritorio** y devuelve un
``StateSnapshot`` describiendo qué hay en pantalla:

  - app/proceso activo (psutil)
  - título de la ventana activa (pygetwindow / Win32 GetForegroundWindow)
  - URL del navegador, si Chrome está corriendo con CDP en :9222
    (Playwright vía ``app.skills.tools.web._get_page(connect_only=True)``)
  - texto visible / OCR ligero (opcional, perezoso)
  - targets UIA disponibles bajo el foco (uiautomation)
  - targets DOM si hay página Playwright accesible
  - tamaño de pantalla y dpi_scale (ctypes)

Diseño:
  * Imports defensivos: nunca rompe si falta una dependencia opcional.
  * Sin side-effects: NO clickea, NO mueve foco, NO lanza Chrome.
  * Rápido por defecto (<300 ms): el OCR y el dump UIA se hacen sólo si
    el caller los pide explícitamente, porque son los pasos caros.
  * Stateless: cada llamada a ``detect()`` recolecta de cero. El Smart
    Executor cachea fuera de aquí cuando lo necesita.

El ``StateSnapshot`` es la entrada de los predicados ``precondition`` /
``postcondition`` definidos en ``execution_contracts.py``.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import log


# ─── Imports opcionales (defensivos) ──────────────────────────────────
try:  # psutil — listar procesos
    import psutil  # type: ignore
    HAS_PSUTIL = True
except Exception:
    psutil = None  # type: ignore
    HAS_PSUTIL = False

try:  # pygetwindow — ventana activa, títulos
    import pygetwindow as gw  # type: ignore
    HAS_GW = True
except Exception:
    gw = None  # type: ignore
    HAS_GW = False

try:  # uiautomation — árbol UIA
    import uiautomation as auto  # type: ignore
    HAS_UIA = True
except Exception:
    auto = None  # type: ignore
    HAS_UIA = False

try:  # ctypes para Windows API y DPI
    import ctypes  # type: ignore
    HAS_CTYPES = True
except Exception:
    ctypes = None  # type: ignore
    HAS_CTYPES = False


_OWN_PID = os.getpid()


# ──────────────────────────────────────────────────────────────────────
# StateSnapshot
# ──────────────────────────────────────────────────────────────────────

@dataclass
class StateSnapshot:
    """Foto del escritorio en un instante.

    Todos los campos son opcionales: si una sonda falla, el campo queda
    vacío en lugar de lanzar excepción. Las precondiciones deben tolerar
    ausencia (p.ej. "no veo URL" ≠ "URL distinta a la esperada").
    """

    timestamp: float = field(default_factory=time.time)

    # ── Proceso / ventana activa ──────────────────────────────────
    active_app: Optional[str] = None            # "chrome.exe", "EXCEL.EXE"
    active_pid: Optional[int] = None
    active_window_title: Optional[str] = None   # título completo
    active_window_class: Optional[str] = None   # WindowClass (Win32)
    active_window_bbox: Optional[Dict[str, int]] = None  # x,y,w,h

    # ── Procesos relevantes corriendo ─────────────────────────────
    running_processes: List[str] = field(default_factory=list)

    # ── Browser / web ─────────────────────────────────────────────
    browser_url: Optional[str] = None
    browser_title: Optional[str] = None
    browser_domain: Optional[str] = None
    has_playwright_page: bool = False

    # ── Pantalla / DPI ───────────────────────────────────────────
    screen_size: Optional[Tuple[int, int]] = None
    dpi_scale: Optional[float] = None

    # ── UIA / DOM (perezoso) ─────────────────────────────────────
    uia_targets_available: bool = False
    uia_visible_names: List[str] = field(default_factory=list)
    dom_targets_available: bool = False

    # ── OCR / texto visible (perezoso) ───────────────────────────
    visible_text: Optional[str] = None

    # ── Diagnóstico ──────────────────────────────────────────────
    detect_ms: int = 0
    errors: List[str] = field(default_factory=list)

    # ──────────────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # Helpers semánticos usados por las precondiciones.
    def is_process_running(self, *names: str) -> bool:
        wanted = {n.strip().lower() for n in names if n}
        if not wanted:
            return False
        running = {p.lower() for p in self.running_processes}
        return any(w in running for w in wanted)

    def active_app_matches(self, *names: str) -> bool:
        if not self.active_app:
            return False
        cur = self.active_app.lower()
        return any(n.strip().lower() == cur for n in names if n)

    def title_contains(self, *fragments: str) -> bool:
        t = (self.active_window_title or "").lower()
        if not t:
            return False
        return any(f.strip().lower() in t for f in fragments if f)

    def url_contains(self, *fragments: str) -> bool:
        u = (self.browser_url or "").lower()
        if not u:
            return False
        return any(f.strip().lower() in u for f in fragments if f)

    def domain_matches(self, *domains: str) -> bool:
        d = (self.browser_domain or "").lower()
        if not d:
            return False
        return any(dom.strip().lower() in d for dom in domains if dom)


# ──────────────────────────────────────────────────────────────────────
# StateDetector
# ──────────────────────────────────────────────────────────────────────

class StateDetector:
    """Sondas defensivas: cada ``detect()`` colecta lo que pueda.

    Uso:

        det = StateDetector()
        snap = det.detect()                # rápido, sin OCR ni UIA dump
        snap2 = det.detect(deep=True)       # con UIA visible_names
    """

    def __init__(self) -> None:
        self._dpi_cached: Optional[float] = None
        self._screen_cached: Optional[Tuple[int, int]] = None

    # ── Pública ──────────────────────────────────────────────────
    def detect(self, deep: bool = False, want_url: bool = True) -> StateSnapshot:
        t0 = time.time()
        snap = StateSnapshot()

        self._probe_active_window(snap)
        self._probe_running_processes(snap)
        if want_url:
            self._probe_browser(snap)
        self._probe_screen(snap)
        if deep:
            self._probe_uia_visible(snap)

        snap.detect_ms = int((time.time() - t0) * 1000)
        return snap

    # ── Sondas internas ──────────────────────────────────────────
    def _probe_active_window(self, snap: StateSnapshot) -> None:
        """Ventana en foco: título, clase, bbox, pid, exe."""
        if not HAS_CTYPES:
            return
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return
            # Título
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            snap.active_window_title = (buf.value or "").strip() or None
            # ClassName
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            snap.active_window_class = (cls_buf.value or "").strip() or None
            # Bounding box
            try:
                from ctypes import wintypes  # type: ignore
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                snap.active_window_bbox = {
                    "x": int(rect.left),
                    "y": int(rect.top),
                    "w": int(rect.right - rect.left),
                    "h": int(rect.bottom - rect.top),
                }
            except Exception:
                pass
            # PID
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            snap.active_pid = int(pid.value) if pid.value else None
            # Nombre exe
            if snap.active_pid and HAS_PSUTIL:
                try:
                    p = psutil.Process(snap.active_pid)
                    snap.active_app = p.name()
                except Exception:
                    pass
        except Exception as e:
            snap.errors.append(f"active_window: {e}")

    def _probe_running_processes(self, snap: StateSnapshot) -> None:
        if not HAS_PSUTIL:
            return
        try:
            seen: List[str] = []
            for p in psutil.process_iter(attrs=("name",)):  # type: ignore
                try:
                    n = (p.info.get("name") or "").strip()
                except Exception:
                    continue
                if n:
                    seen.append(n)
            snap.running_processes = seen
        except Exception as e:
            snap.errors.append(f"processes: {e}")

    def _probe_browser(self, snap: StateSnapshot) -> None:
        """URL del navegador vía Playwright (modo connect-only).

        NO lanza Chrome: si CDP no está disponible o el page está
        ocupado por otro thread, simplemente no llena ``browser_url``.
        Esto preserva la regla "el detector no toca el sistema".
        """
        try:
            from app.skills.tools.web import _get_page  # type: ignore
            page = _get_page(connect_only=True)
            if page is None:
                return
            snap.has_playwright_page = True
            snap.dom_targets_available = True
            try:
                snap.browser_url = (page.url or None)
            except Exception:
                snap.browser_url = None
            try:
                snap.browser_title = page.title() or None
            except Exception:
                pass
            if snap.browser_url:
                try:
                    from urllib.parse import urlparse
                    snap.browser_domain = urlparse(snap.browser_url).netloc or None
                except Exception:
                    pass
        except Exception as e:
            # No es error fatal: simplemente no hay navegador.
            log.debug(f"StateDetector.browser: {e}")

    def _probe_screen(self, snap: StateSnapshot) -> None:
        if self._screen_cached is None and HAS_CTYPES:
            try:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                user32.SetProcessDPIAware()
                w = user32.GetSystemMetrics(0)
                h = user32.GetSystemMetrics(1)
                self._screen_cached = (int(w), int(h))
            except Exception as e:
                snap.errors.append(f"screen_size: {e}")
        snap.screen_size = self._screen_cached

        if self._dpi_cached is None and HAS_CTYPES:
            try:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
                # Importante: en x64 los HANDLE son 64-bit. Sin
                # ``restype = c_void_p`` ctypes asume ``c_int`` y
                # el HDC se trunca → OverflowError al pasarlo a
                # GetDeviceCaps. (Bug detectado el 2026-05-03.)
                user32.GetDC.restype = ctypes.c_void_p
                user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
                gdi32.GetDeviceCaps.restype = ctypes.c_int
                hdc = user32.GetDC(None)
                LOGPIXELSX = 88
                dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
                user32.ReleaseDC(None, hdc)
                if dpi:
                    self._dpi_cached = float(dpi) / 96.0
            except Exception as e:
                snap.errors.append(f"dpi: {e}")
        snap.dpi_scale = self._dpi_cached

    def _probe_uia_visible(self, snap: StateSnapshot) -> None:
        """Lista nombres de controles UIA visibles bajo la ventana activa.

        Caro (~150 ms): sólo se llama con ``deep=True``. Útil cuando una
        precondición depende de "veo el botón X" sin OCR.
        """
        if not HAS_UIA:
            return
        try:
            ctrl = auto.GetFocusedControl()  # type: ignore
            if ctrl is None:
                return
            snap.uia_targets_available = True
            names: List[str] = []

            def _walk(c, depth: int) -> None:
                if depth > 3 or len(names) >= 50:
                    return
                try:
                    nm = (getattr(c, "Name", "") or "").strip()
                    if nm and len(nm) <= 80:
                        names.append(nm)
                    for child in c.GetChildren() or []:
                        _walk(child, depth + 1)
                except Exception:
                    return

            try:
                top = ctrl.GetTopLevelControl() or ctrl
            except Exception:
                top = ctrl
            _walk(top, 0)
            snap.uia_visible_names = names[:50]
        except Exception as e:
            snap.errors.append(f"uia: {e}")


# ──────────────────────────────────────────────────────────────────────
# Singleton conveniente
# ──────────────────────────────────────────────────────────────────────

_default_detector: Optional[StateDetector] = None


def detect_state(deep: bool = False, want_url: bool = True) -> StateSnapshot:
    """Atajo: detectar estado usando un detector compartido."""
    global _default_detector
    if _default_detector is None:
        _default_detector = StateDetector()
    return _default_detector.detect(deep=deep, want_url=want_url)


__all__ = [
    "StateSnapshot",
    "StateDetector",
    "detect_state",
]
