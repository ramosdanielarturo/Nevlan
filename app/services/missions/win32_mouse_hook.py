"""
Nevlan — Win32 Low-Level Mouse Hook Adapter (PRD 2026-05-10j)
================================================================

Adapter del sistema operativo que dispara la primera fase del
:mod:`intent_interception_layer` (``on_mouse_down``) **antes** de que
el click humano cambie la pantalla.

Por qué un hook adicional al ``pynput`` que ya usa el recorder
--------------------------------------------------------------

El callback ``on_click(pressed=True)`` de pynput corre tras la cola
de mensajes de Windows: cuando llega, el click ya pasó por el árbol
del foco y la primera repintada parcial pudo haber empezado. Para la
regla del PRD "evidencia pre-click atómica", necesitamos engancharnos
al ``WH_MOUSE_LL`` que se ejecuta en la frontera del sistema, **antes**
que las aplicaciones reciban el ``WM_LBUTTONDOWN``. En ese instante
es seguro afirmar que la pantalla todavía es la versión que el humano
vio.

Cómo se acopla con la capa
--------------------------

El adapter sigue la regla "observa, no decide". Cada vez que el SO
emite ``WM_LBUTTONDOWN`` / ``WM_RBUTTONDOWN`` / ``WM_MBUTTONDOWN``:

  1. Si el punto cae sobre una ventana de Nevlan, IGNORA (no es paso).
  2. Llama ``layer.on_mouse_down(x, y)`` para que la capa fije
     ``pre_snapshot`` + ``before_anchor``.
  3. **Deja pasar el evento** al SO (``CallNextHookEx``). NO bloquea.

El ``mouse_up`` corre por pynput (que ya funciona) e invoca
``layer.on_mouse_up(x, y)`` desde el ``MouseListener`` del recorder.

Garantías de seguridad
----------------------

  * **Solo Windows**: en cualquier otro SO, ``Win32MouseHookAdapter``
    queda en modo ``no-op`` y ``is_available`` devuelve ``False``.
  * **Idempotente**: ``start()`` / ``stop()`` se pueden llamar varias
    veces sin colgar el hook.
  * **Crash-safe**: si la capa lanza dentro del hook, atrapamos la
    excepción y dejamos pasar el evento (jamás dejamos al usuario sin
    mouse).
  * **Bucle de mensajes propio**: corre en un thread dedicado con su
    propia ``GetMessage`` loop — requisito de Windows para hooks LL.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Optional


# ──────────────────────────────────────────────────────────────────────
# Detección de plataforma (no levantar al importar en macOS/Linux)
# ──────────────────────────────────────────────────────────────────────

_IS_WINDOWS = sys.platform.startswith("win")


# ──────────────────────────────────────────────────────────────────────
# Constantes Win32 (subset que necesitamos)
# ──────────────────────────────────────────────────────────────────────

WH_MOUSE_LL = 14

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208

# Para identificar el botón en el callback que la capa entiende.
_BUTTON_BY_WM = {
    WM_LBUTTONDOWN: "left",
    WM_LBUTTONUP: "left",
    WM_RBUTTONDOWN: "right",
    WM_RBUTTONUP: "right",
    WM_MBUTTONDOWN: "middle",
    WM_MBUTTONUP: "middle",
}

_DOWN_MESSAGES = frozenset({WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN})


# ──────────────────────────────────────────────────────────────────────
# Estructuras / firmas (solo si Windows está disponible)
# ──────────────────────────────────────────────────────────────────────


if _IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    LowLevelMouseProc = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_int,
        wintypes.WPARAM,
        ctypes.c_void_p,
    )

    _SetWindowsHookExW = _user32.SetWindowsHookExW
    _SetWindowsHookExW.argtypes = (
        ctypes.c_int, LowLevelMouseProc, wintypes.HINSTANCE, wintypes.DWORD,
    )
    _SetWindowsHookExW.restype = ctypes.c_void_p

    _UnhookWindowsHookEx = _user32.UnhookWindowsHookEx
    _UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
    _UnhookWindowsHookEx.restype = wintypes.BOOL

    _CallNextHookEx = _user32.CallNextHookEx
    _CallNextHookEx.argtypes = (
        ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p,
    )
    _CallNextHookEx.restype = ctypes.c_long

    _GetMessageW = _user32.GetMessageW
    _GetMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG), wintypes.HWND,
        wintypes.UINT, wintypes.UINT,
    )
    _GetMessageW.restype = wintypes.BOOL

    _PostThreadMessageW = _user32.PostThreadMessageW
    _PostThreadMessageW.argtypes = (
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    )
    _PostThreadMessageW.restype = wintypes.BOOL

    _TranslateMessage = _user32.TranslateMessage
    _DispatchMessageW = _user32.DispatchMessageW

    _GetCurrentThreadId = _kernel32.GetCurrentThreadId
    _GetCurrentThreadId.restype = wintypes.DWORD

    _GetModuleHandleW = _kernel32.GetModuleHandleW
    _GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _GetModuleHandleW.restype = wintypes.HMODULE

    WM_QUIT = 0x0012
else:
    # Stubs vacíos para no romper la importación en otras plataformas.
    _user32 = None  # type: ignore[assignment]
    _kernel32 = None  # type: ignore[assignment]
    MSLLHOOKSTRUCT = None  # type: ignore[assignment]
    LowLevelMouseProc = None  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────


@dataclass
class HookEvent:
    """Evento normalizado emitido por el hook a la capa."""

    x: int
    y: int
    button: str
    ts_ms: int
    kind: str  # "down" | "up"


HookEventCb = Callable[[HookEvent], None]
OwnHwndCb = Callable[[int], bool]  # ``True`` si (x,y) cae en ventana de Nevlan.


def is_available() -> bool:
    """¿Podemos instalar el hook en este sistema?"""
    return _IS_WINDOWS


# ──────────────────────────────────────────────────────────────────────
# Adapter
# ──────────────────────────────────────────────────────────────────────


class Win32MouseHookAdapter:
    """Hook de mouse low-level con thread y message loop dedicados.

    Diseño:

      * El hook SE INSTALA en el thread que también corre la message
        loop — Windows lo exige para ``WH_MOUSE_LL``. Ese thread se crea
        en ``start()`` y se detiene en ``stop()`` posteando ``WM_QUIT``.
      * El callback es **observador**: lanza la notificación a un
        callable inyectado (``on_event``) y SIEMPRE llama
        ``CallNextHookEx`` para no romper otras aplicaciones.
      * Si ``on_event`` lanza, el hook captura la excepción y sigue —
        el peor caso es perder evidencia de un click, jamás bloquear
        el sistema.
      * En tests / no-Windows: ``start()`` es no-op y el adapter se
        comporta como si nunca hubiera estado.

    Para tests de unidad, el callable ``on_event`` se puede invocar
    sintéticamente vía ``simulate_event`` — eso permite verificar la
    integración con la capa sin tocar Win32.
    """

    def __init__(
        self,
        *,
        on_event: HookEventCb,
        is_own_hwnd_at: Optional[OwnHwndCb] = None,
    ) -> None:
        self._on_event = on_event
        self._is_own_hwnd = is_own_hwnd_at or (lambda _hwnd: False)
        self._hook_id: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._proc_ref: Any = None  # Mantiene viva la WINFUNCTYPE
        self._ready_event = threading.Event()
        self._stop_requested = False
        self._lock = threading.Lock()

    # ── API pública ──────────────────────────────────────────────
    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._hook_id is not None

    def start(self) -> bool:
        """Instala el hook. Devuelve ``True`` si se instaló, ``False``
        si el SO no lo soporta o ya estaba activo.

        Es idempotente: una segunda llamada cuando ya está corriendo
        devuelve ``True`` sin reinstalar.
        """
        if not _IS_WINDOWS:
            return False
        with self._lock:
            if self._hook_id is not None:
                return True
            self._stop_requested = False
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="nevlan-mouse-hook", daemon=True,
        )
        self._thread.start()
        # Esperamos a que el hook esté listo (o falle).
        self._ready_event.wait(timeout=2.0)
        return self.is_running

    def stop(self) -> None:
        """Detiene el hook. Idempotente."""
        if not _IS_WINDOWS:
            return
        with self._lock:
            self._stop_requested = True
            tid = self._thread_id
        if tid:
            try:
                _PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        t = self._thread
        if t is not None:
            t.join(timeout=1.5)
        with self._lock:
            self._thread = None
            self._thread_id = 0
            self._hook_id = None
            self._proc_ref = None

    def simulate_event(self, ev: HookEvent) -> None:
        """Inyecta un evento sintético para tests. NO toca el SO.

        Sirve para verificar que la integración con el callback
        ``on_event`` funciona sin necesitar Win32 real.
        """
        try:
            self._on_event(ev)
        except Exception:
            # Mantenemos el contrato de no relanzar.
            pass

    # ── Internals ────────────────────────────────────────────────
    def _thread_main(self) -> None:
        if not _IS_WINDOWS:
            self._ready_event.set()
            return
        with self._lock:
            self._thread_id = _GetCurrentThreadId()

        proc = LowLevelMouseProc(self._hook_proc)
        # Mantenemos referencia para evitar GC del callback C.
        self._proc_ref = proc
        hmod = _GetModuleHandleW(None)
        hook_id = _SetWindowsHookExW(WH_MOUSE_LL, proc, hmod, 0)
        if not hook_id:
            self._ready_event.set()
            return
        with self._lock:
            self._hook_id = hook_id
        self._ready_event.set()

        msg = wintypes.MSG()
        while not self._stop_requested:
            ret = _GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            _TranslateMessage(ctypes.byref(msg))
            _DispatchMessageW(ctypes.byref(msg))

        try:
            _UnhookWindowsHookEx(hook_id)
        except Exception:
            pass

    def _hook_proc(self, nCode, wParam, lParam):
        """Callback C invocado por Windows en este thread."""
        try:
            if nCode < 0:
                return _CallNextHookEx(self._hook_id, nCode, wParam, lParam)
            wm = int(wParam)
            if wm in _BUTTON_BY_WM:
                info = ctypes.cast(
                    lParam, ctypes.POINTER(MSLLHOOKSTRUCT),
                ).contents
                x = int(info.pt.x)
                y = int(info.pt.y)
                ts_ms = int(time.time() * 1000)
                # Filtro de ventanas propias: si el click cae sobre
                # nuestra UI, NO emitimos. Eso evita que el overlay
                # contamine la grabación.
                if self._is_nevlan_window_at(x, y):
                    return _CallNextHookEx(
                        self._hook_id, nCode, wParam, lParam,
                    )
                kind = "down" if wm in _DOWN_MESSAGES else "up"
                ev = HookEvent(
                    x=x, y=y, button=_BUTTON_BY_WM[wm],
                    ts_ms=ts_ms, kind=kind,
                )
                try:
                    self._on_event(ev)
                except Exception:
                    # No relanzamos: jamás romper la cadena del hook.
                    pass
        except Exception:
            # Catch-all: bajo ningún concepto rompemos al SO.
            pass
        return _CallNextHookEx(self._hook_id, nCode, wParam, lParam)

    def _is_nevlan_window_at(self, x: int, y: int) -> bool:
        try:
            from ctypes import wintypes as _w
            pt = _w.POINT(int(x), int(y))
            hwnd = _user32.WindowFromPoint(pt)
            if not hwnd:
                return False
            return bool(self._is_own_hwnd(int(hwnd)))
        except Exception:
            return False


__all__ = [
    "HookEvent",
    "Win32MouseHookAdapter",
    "is_available",
    "WM_LBUTTONDOWN",
    "WM_LBUTTONUP",
    "WM_RBUTTONDOWN",
    "WM_RBUTTONUP",
    "WM_MBUTTONDOWN",
    "WM_MBUTTONUP",
]
