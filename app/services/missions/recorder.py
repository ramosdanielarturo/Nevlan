"""
ArthurOS Services - Mission Recorder
----------------------------------
Captura eventos de input con contexto rico (UIA, DOM, Vision).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Optional, Callable, List, Dict, Any, Tuple
from datetime import datetime, timezone
from pathlib import Path

from app.contracts.mission import (
    Mission, RawEvent, EventType, MouseAction, KeyboardAction, DragAction,
    WindowContext, MissionStatus, Annotation, AnnotationType,
)
from app.core.logger import log
from app.core.config import settings
from app.services.missions.field_commit import (
    FieldCommitResult,
    build_keyboard_type_text_event,
    is_browser_internal_url,
    READ_SOURCE_DOM,
    READ_SOURCE_UIA,
    READ_SOURCE_LAST_KNOWN,
    READ_SOURCE_KEYBOARD,
    READ_SOURCE_KEYBOARD_URL_SANITIZED,
)
from app.services.missions.target_signature import build_target_signature
from app.services.missions.recording_feedback import (
    build_recording_feedback,
)
from app.services.missions.visual_capture import (
    capture_visual_target,
    SRC_CLICK_FALLBACK,
)
from app.services.missions.recorder_capture_contract import (
    PostActionScheduler,
    PostActionState,
    ScreenshotRingBuffer,
    apply_capture_contract_to_metadata,
    build_capture_contract,
    capture_target_anchor,
    capture_uia_parent_chain,
    enforce_target_identity_isolation,
    is_uia_weak,
    maybe_run_ocr,
    attach_precapture_diagnostics,
    CAPTURE_PHASE_PRE_CLICK,
    ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
    CAPTURE_PHASE_POST_ACTION,
    ANCHOR_SOURCE_PYNPUT_POST_CAPTURE,
    TARGET_PRECAPTURE_MISSING,
)

# Opcional: obtener process_name a partir del pid
try:
    import psutil
    _HAS_PSUTIL = True
except Exception:
    _HAS_PSUTIL = False

# Win32 helpers opcionales para HWND + PID de la ventana activa
try:
    import ctypes
    from ctypes import wintypes
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _HAS_W32 = True
except Exception:
    _HAS_W32 = False

# Try imports for Rich Context
try:
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(1.0)  # fast timeout
    HAS_UIA = True
except ImportError:
    HAS_UIA = False


_GENERIC_CT = {
    "GroupControl", "PaneControl", "CustomControl", "DocumentControl",
    "WindowControl", "ToolBarControl", "StatusBarControl",
}


def _is_meaningful(elem) -> bool:
    """Un elemento es útil si tiene Name no vacío que no sea solo su ControlType,
    o bien tiene AutomationId. Los contenedores genéricos no califican."""
    if elem is None:
        return False
    try:
        name = (elem.Name or "").strip()
        ct = (elem.ControlTypeName or "").strip()
        aid = (elem.AutomationId or "").strip()
        if aid:
            return True
        if name and name.lower() != ct.lower() and ct not in _GENERIC_CT:
            return True
        if name and ct not in _GENERIC_CT:
            return True
        return False
    except Exception:
        return False


def _refine_to_meaningful(elem, x: int, y: int, max_depth: int = 6):
    """Si `elem` es un contenedor genérico, intenta descender a un hijo más
    específico (Edit, Button, Hyperlink, etc.) que contenga el punto (x, y).
    Si no hay nada mejor, devuelve el original."""
    if elem is None:
        return None
    if _is_meaningful(elem):
        return elem
    try:
        best = elem
        cursor = elem
        for _ in range(max_depth):
            children = list(cursor.GetChildren() or [])
            picked = None
            for ch in children:
                try:
                    r = ch.BoundingRectangle
                    if r and r.left <= x <= r.right and r.top <= y <= r.bottom:
                        picked = ch
                        if _is_meaningful(ch):
                            best = ch
                        break
                except Exception:
                    continue
            if picked is None:
                break
            cursor = picked
            if _is_meaningful(cursor):
                best = cursor
        return best
    except Exception:
        return elem

# ============================================================================
# Listeners
# ============================================================================

class InputListener(ABC):
    @abstractmethod
    def start(self) -> None: pass
    @abstractmethod
    def stop(self) -> None: pass
    @abstractmethod
    def is_active(self) -> bool: pass


# ============================================================================
# Helpers de contexto de ventana (PID, HWND, process_name, bbox, title)
# ============================================================================

_OWN_PID: int = os.getpid()

# Constantes Win32
_GA_ROOT = 2  # GetAncestor flag


def _capture_post_state_default() -> Dict[str, Any]:
    """Capture-state callback que devuelve el estado actual para el
    Post-Action scheduler. Lee window_context del foreground (no del
    punto del click — ya pasó). Pure best-effort.
    """
    out: Dict[str, Any] = {}
    try:
        wc = _get_window_context()
        if wc is not None:
            out["hwnd"] = getattr(wc, "hwnd", None)
            out["title"] = getattr(wc, "title", "") or ""
            out["process"] = getattr(wc, "process_name", "") or ""
    except Exception:
        pass
    # URL: si tenemos web_recorder cargado, intentamos URL del browser activo.
    try:
        from app.services.missions.web_recorder import (
            current_browser_url,  # type: ignore
        )
        url = current_browser_url()
        if url:
            out["url"] = str(url)
    except Exception:
        pass
    return out


def _is_own_hwnd(hwnd: int) -> bool:
    """True si el HWND pertenece a Nevlan (mismo PID)."""
    if not _HAS_W32 or not hwnd:
        return False
    try:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value) == _OWN_PID
    except Exception:
        return False


def _hwnd_info(hwnd: int) -> Tuple[Optional[int], Optional[str], Optional[str], Optional[Dict[str, int]]]:
    """Devuelve (pid, title, process_name, bbox) para un HWND."""
    pid_val: Optional[int] = None
    proc_name: Optional[str] = None
    title: Optional[str] = None
    bbox: Optional[Dict[str, int]] = None
    if not _HAS_W32 or not hwnd:
        return (None, None, None, None)
    try:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        if pid.value:
            pid_val = int(pid.value)
    except Exception:
        pass
    if pid_val and _HAS_PSUTIL:
        try:
            proc_name = psutil.Process(pid_val).name()
        except Exception:
            proc_name = None
    try:
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(int(hwnd), buf, 256)
        title = buf.value or None
    except Exception:
        pass
    try:
        rect = wintypes.RECT()
        if _user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            bbox = {
                "left": int(rect.left),
                "top": int(rect.top),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
            }
    except Exception:
        pass
    return (pid_val, title, proc_name, bbox)


def _get_window_context_at_point(x: int, y: int) -> Optional[WindowContext]:
    """Contexto de la ventana TOP-LEVEL bajo el punto (x, y).

    Esto es lo correcto cuando se captura un click: no nos importa qué ventana
    tiene foco (puede ser Nevlan), sino qué app recibe realmente el click.
    Saltamos cualquier HWND que pertenezca a Nevlan.
    """
    if not _HAS_W32:
        return _get_window_context()
    try:
        pt = wintypes.POINT(int(x), int(y))
        hwnd = _user32.WindowFromPoint(pt)
        if not hwnd:
            return _get_window_context()
        # Subir al root (top-level) para obtener la ventana lógica, no el child
        root = _user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
        # Si es Nevlan, intentamos subir la pila — pero WindowFromPoint ya
        # devuelve la ventana visible más arriba en ese punto. Si es Nevlan,
        # es porque el usuario clickeó sobre Nevlan. En ese caso, mejor no
        # devolver un contexto que ensucie el paso.
        if _is_own_hwnd(int(root)):
            return _get_window_context()  # fallback a foreground
        pid_val, title, proc_name, bbox = _hwnd_info(int(root))
        return WindowContext(
            hwnd=int(root),
            title=title,
            process_name=proc_name,
            bounding_box=bbox,
        )
    except Exception as e:
        log.debug(f"window_at_point failed: {e}")
        return _get_window_context()


def _get_window_context() -> Optional[WindowContext]:
    """Contexto rico de la ventana activa (foreground).

    Se mantiene como fallback para eventos que no tienen posición natural
    (p.ej. teclado). Incluye title + bounding_box vía pygetwindow y, si es
    posible, HWND y process_name vía Win32 + psutil.
    """
    hwnd_val: Optional[int] = None
    pid_val: Optional[int] = None
    proc_name: Optional[str] = None

    # 1) HWND + pid desde Win32 (barato y fiable)
    if _HAS_W32:
        try:
            hwnd = _user32.GetForegroundWindow()
            if hwnd and not _is_own_hwnd(int(hwnd)):
                hwnd_val = int(hwnd)
                pid = wintypes.DWORD()
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    pid_val = int(pid.value)
        except Exception as e:
            log.debug(f"win32 ctx failed: {e}")

    # 2) process_name desde psutil (si está)
    if pid_val and _HAS_PSUTIL:
        try:
            proc_name = psutil.Process(pid_val).name()
        except Exception:
            proc_name = None

    # 3) title + bbox con pygetwindow (como fallback)
    title = None
    bbox = None
    if hwnd_val:
        _, title, _, bbox = _hwnd_info(hwnd_val)
    if title is None or bbox is None:
        try:
            import pygetwindow
            active = pygetwindow.getActiveWindow()
            if active:
                title = title or active.title
                if not bbox:
                    bbox = {
                        "left": int(active.left),
                        "top": int(active.top),
                        "width": int(active.width),
                        "height": int(active.height),
                    }
        except Exception:
            pass

    if not any((hwnd_val, title, bbox, proc_name)):
        return None
    return WindowContext(
        hwnd=hwnd_val,
        title=title,
        process_name=proc_name,
        bounding_box=bbox,
    )


# ============================================================================
# MouseListener — click / doble-click / right-click / drag / scroll
# ============================================================================

# Umbrales para detección de doble-click y drag.
_DOUBLE_CLICK_WINDOW_S = 0.40     # tiempo máximo entre clicks
_DOUBLE_CLICK_PIX = 8             # separación máxima entre clicks
_DRAG_MIN_PIX = 8                 # desplazamiento mínimo para considerar drag
_DRAG_MAX_DURATION_S = 15.0       # sanity cap
_SCROLL_COALESCE_S = 0.35         # ventana para coalescer ticks de scroll


class MouseListener(InputListener):
    """Listener enriquecido que distingue click / double-click / right / drag / scroll."""

    def __init__(
        self,
        callback: Callable[[RawEvent], None],
        *,
        mission_id: str = "",
        pre_buffer: Optional[ScreenshotRingBuffer] = None,
        post_scheduler: Optional[PostActionScheduler] = None,
        on_post_action: Optional[Callable[[str, PostActionState], None]] = None,
        intent_layer_handle: Any = None,
        strict_intent_interception: bool = False,
    ):
        self.callback = callback
        self._mission_id = (mission_id or "").strip()
        self._listener = None
        self._active = False

        # Estado para doble-click
        self._last_click: Dict[str, Any] = {}

        # Estado para drag
        self._drag_state: Dict[str, Any] = {}  # {down_x, down_y, t0, button, pending}

        # Estado para coalescer scroll
        self._scroll_state: Dict[str, Any] = {}

        # PRD 2026-05-10h (Recorder Capture Contract):
        # buffer pre-click + post-action scheduler son opcionales; si
        # vienen None, el contract reportará has_pre_snapshot / has_post_state
        # = False honestamente sin romper el listener.
        self._pre_buffer = pre_buffer
        self._post_scheduler = post_scheduler
        self._on_post_action = on_post_action

        # PRD 2026-05-10j (Intent Interception Layer):
        # Si la integración pasó un ``LayerHandle``, cada click pasará
        # por la capa. Si ``strict_intent_interception=True`` y la
        # capa no llegó, ``_process_click`` rechazará el click (no
        # cae al legacy).
        self._intent_handle = intent_layer_handle
        self._strict_intent = bool(strict_intent_interception)

        # FASE 1 — pre-click: anchor en mouse_down (pynput pressed=True).
        self._pending_click_lock = threading.Lock()
        self._pending_click: Optional[Dict[str, Any]] = None

    # ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._active:
            return
        try:
            from pynput.mouse import Listener as PyMouseListener

            def on_click(x, y, button, pressed):
                btn_str = str(button).split(".")[-1]
                now = time.time()
                if pressed:
                    # PRD 2026-05-10j: la capa de intercept se entera
                    # del mouse_down ANTES de cualquier procesamiento.
                    # Si el Win32 hook está activo, ya disparó. Llamar
                    # aquí también es seguro (la capa es idempotente —
                    # respeta el primer mouse_down activo).
                    if self._intent_handle is not None:
                        try:
                            from app.services.missions.recorder_intent_integration import (
                                notify_mouse_down,
                            )
                            notify_mouse_down(self._intent_handle, int(x), int(y))
                        except Exception as e:
                            log.debug(
                                f"intent_layer.notify_mouse_down failed: {e}"
                            )
                    # PCOF: ancla mouse_down antes de mouse_up / transición UI.
                    try:
                        from app.services.runtime.pre_click_operational_freeze import (
                            notify_pcof_mouse_down,
                            pcof_enabled,
                        )
                        if pcof_enabled():
                            wc_dict: Dict[str, Any] = {}
                            try:
                                wctx = _get_window_context_at_point(int(x), int(y))
                                if wctx is not None:
                                    wc_dict = {
                                        "title": getattr(wctx, "title", "") or "",
                                        "process_name": getattr(
                                            wctx, "process_name", "",
                                        ) or "",
                                        "hwnd": getattr(wctx, "hwnd", None),
                                    }
                            except Exception:
                                pass
                            pre_mousedown_path: Optional[str] = None
                            if self._pre_buffer is not None:
                                try:
                                    _pre_md = self._pre_buffer.get_before(
                                        int(time.time() * 1000),
                                    )
                                    if _pre_md is not None:
                                        pre_mousedown_path = _pre_md.full_path
                                except Exception:
                                    pass
                            notify_pcof_mouse_down(
                                int(x), int(y),
                                window_context=wc_dict,
                                pre_snapshot_path=pre_mousedown_path,
                            )
                    except Exception as e:
                        log.debug(f"pcof.notify_mouse_down failed: {e}")
                    # Cualquier press abre una posible sesión de drag
                    self._drag_state = {
                        "down_x": x, "down_y": y, "t0": now, "button": btn_str,
                        "fired_click": False,
                    }
                    try:
                        wctx = _get_window_context_at_point(int(x), int(y))
                        pre_anchor = capture_target_anchor(
                            wctx,
                            None,
                            phase=CAPTURE_PHASE_PRE_CLICK,
                            source=ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
                        )
                        with self._pending_click_lock:
                            self._pending_click = {
                                "x": int(x),
                                "y": int(y),
                                "button": btn_str,
                                "t_down": now,
                                "window_ctx": wctx,
                                "before_anchor": pre_anchor,
                            }
                    except Exception as e:
                        log.debug(f"pre_click pending capture failed: {e}")
                    return
                # released ===========================
                # ── Captura del timestamp REAL ─────────────────────
                # Tomamos timestamp + sequence_id INMEDIATAMENTE en
                # el callback de pynput, antes de hacer cualquier
                # trabajo pesado (UIA, screenshot, web). Bug pre-2026:
                # el ``_process_click`` se ejecuta en otro thread
                # y asignaba ``timestamp = datetime.now()`` después
                # de UIA/screenshot. Eso podía retrasar el timestamp
                # 100-300ms y poner el click DESPUÉS del primer
                # carácter tipeado, partiendo "Chrome" en "C" + click
                # + "hrome".
                capture_ts = datetime.now(timezone.utc)
                from app.contracts.mission import _next_raw_event_sequence_id
                capture_seq = _next_raw_event_sequence_id()

                ds = self._drag_state or {}
                if ds:
                    dx = x - ds.get("down_x", x)
                    dy = y - ds.get("down_y", y)
                    dur = now - ds.get("t0", now)
                    if (abs(dx) >= _DRAG_MIN_PIX or abs(dy) >= _DRAG_MIN_PIX) and dur <= _DRAG_MAX_DURATION_S:
                        # Lo tratamos como DRAG, no como click
                        threading.Thread(
                            target=self._process_drag,
                            args=(ds["down_x"], ds["down_y"], x, y, dur,
                                  ds["button"], capture_ts, capture_seq),
                            daemon=True,
                        ).start()
                        self._drag_state = {}
                        return
                # Si no fue drag → flush como click en el punto de release
                # (compatible con comportamiento estándar de Windows).
                pending_click: Optional[Dict[str, Any]] = None
                with self._pending_click_lock:
                    pending_click = self._pending_click
                    self._pending_click = None
                threading.Thread(
                    target=self._process_click,
                    args=(x, y, btn_str, capture_ts, capture_seq, pending_click),
                    daemon=True,
                ).start()
                self._drag_state = {}

            def on_scroll(x, y, dx, dy):
                self._on_scroll_tick(x, y, dx, dy)

            self._listener = PyMouseListener(on_click=on_click, on_scroll=on_scroll)
            self._listener.start()
            self._active = True
            log.debug("MouseListener started (rich: click/dbl/drag/scroll)")
        except Exception as e:
            log.error(f"Error starting mouse listener: {e}")

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None
        self._active = False
        # flush scroll pendiente
        self._flush_scroll()

    def is_active(self) -> bool:
        return self._active

    # ──────────────────────────────────────────────────────────────
    # Click / doble-click
    # ──────────────────────────────────────────────────────────────
    def _process_click(
        self, x: int, y: int, btn_str: str,
        capture_ts: Optional[datetime] = None,
        capture_seq: Optional[int] = None,
        pending_click: Optional[Dict[str, Any]] = None,
    ):
        # ── PRD 2026-05-10j (fail-safe Intent Interception) ──
        # Si la grabación es estricta y la capa NO está activa,
        # rechazamos el click. NO debemos caer al pipeline legacy.
        if self._strict_intent and self._intent_handle is None:
            try:
                from app.services.missions.recorder_intent_integration import (
                    RecorderUnsafeError,
                )
                raise RecorderUnsafeError(
                    "Recorder en modo estricto sin IntentInterceptionLayer "
                    "activa: click descartado (PRD §10 no-regresión)."
                )
            except ImportError:
                # Si ni siquiera podemos importar el módulo de
                # integración, definitivamente no es seguro continuar.
                log.error(
                    "process_click bloqueado: integración intent inalcanzable."
                )
                return
        try:
            # ── Guard duro: si el click cae sobre una ventana de Nevlan
            # (overlay, panel, pill, etc.) NO debe convertirse en un paso
            # de la misión. _is_own_hwnd filtra por PID para garantizarlo.
            if _HAS_W32:
                try:
                    pt = wintypes.POINT(int(x), int(y))
                    hwnd_under = _user32.WindowFromPoint(pt)
                    if hwnd_under:
                        root_hwnd = _user32.GetAncestor(hwnd_under, _GA_ROOT) or hwnd_under
                        if _is_own_hwnd(int(root_hwnd)):
                            log.debug(f"Click Nevlan ignorado: ({x},{y})")
                            return
                except Exception:
                    pass

            now = time.time()
            event_type = EventType.MOUSE_CLICK
            if btn_str == "right":
                event_type = EventType.MOUSE_RIGHT_CLICK

            # ── Doble-click para botón izquierdo ──
            is_dbl = False
            if btn_str == "left":
                lc = self._last_click
                if lc and (now - lc.get("t", 0)) <= _DOUBLE_CLICK_WINDOW_S:
                    if abs(x - lc.get("x", x)) <= _DOUBLE_CLICK_PIX and abs(y - lc.get("y", y)) <= _DOUBLE_CLICK_PIX:
                        is_dbl = True
                self._last_click = {"x": x, "y": y, "t": now}

            if is_dbl:
                event_type = EventType.MOUSE_DOUBLE_CLICK

            # Contexto basado en el PUNTO del click (no en el foreground):
            # así, si Nevlan o el overlay estaban arriba, no contaminan.
            window_ctx = _get_window_context_at_point(x, y)
            if pending_click and pending_click.get("window_ctx") is not None:
                window_ctx = pending_click.get("window_ctx") or window_ctx
            # FASE 1: identidad solo desde anchor pre_click (mouse_down).
            had_pending = bool(
                pending_click and pending_click.get("before_anchor") is not None
            )
            before_anchor = (
                pending_click.get("before_anchor") if had_pending else None
            )
            missing_precapture = ""
            if before_anchor is None:
                missing_precapture = TARGET_PRECAPTURE_MISSING
            metadata = self._capture_uia(x, y)
            attach_precapture_diagnostics(
                metadata,
                before_anchor=before_anchor,
                had_pending=had_pending,
                missing_reason=missing_precapture,
            )

            # ── Web capture (Chrome/Edge/Firefox) ─────────────────
            # Si la app destino es un navegador soportado, intentamos
            # extraer locators Playwright. Best-effort: si Playwright
            # no está disponible o el CDP no responde, simplemente
            # seguimos sin web_data.
            try:
                from app.services.missions.web_recorder import (
                    capture_web_target, is_web_process,
                )
                if (window_ctx and is_web_process(getattr(window_ctx, "process_name", None))):
                    # Pasamos el HWND raíz del navegador para que
                    # web_recorder pueda hacer la conversión absoluta→
                    # viewport via Win32 (sin clamping inseguro).
                    web = capture_web_target(
                        x, y,
                        window_ctx.process_name,
                        hwnd=getattr(window_ctx, "hwnd", None),
                    )
                    if web:
                        metadata["web"] = web
            except Exception as e:
                log.debug(f"web capture failed: {e}")
            # ── Visual capture (bbox-first) ──────────────────────
            # Pipeline: select_target_bbox → asset_bbox con margen → 3 PNGs
            # (small/mid/full) → validación. Si no encontramos un target
            # específico, ``capture_visual_target`` cae a ``click_fallback``
            # con ``quality_level=low`` para que Mission Review pida refuerzo.
            try:
                _hw = None
                try:
                    _hw = int(getattr(window_ctx, "hwnd", 0) or 0) or None
                except Exception:
                    _hw = None
                window_bbox_dict = None
                if window_ctx and getattr(window_ctx, "bounding_box", None):
                    bb = window_ctx.bounding_box or {}
                    window_bbox_dict = {
                        "left": int(bb.get("left", 0)),
                        "top": int(bb.get("top", 0)),
                        "width": int(bb.get("width", 0)),
                        "height": int(bb.get("height", 0)),
                    }
                dpi_scale = None
                if _hw and sys.platform == "win32":
                    try:
                        dpi_scale = round(
                            float(_user32.GetDpiForWindow(int(_hw))) / 96.0, 3,
                        ) or None
                    except Exception:
                        dpi_scale = None

                assets_dir = (
                    settings.VAR_PATH / "missions" / "assets" / self._mission_id
                    if self._mission_id
                    else settings.VAR_PATH / "missions" / "assets" / "temp"
                )
                assets_dir.mkdir(parents=True, exist_ok=True)
                visual = capture_visual_target(
                    click_xy=(x, y),
                    uia_metadata=metadata.get("uia"),
                    web_metadata=metadata.get("web"),
                    window_bbox=window_bbox_dict,
                    dpi_scale=dpi_scale,
                    assets_dir=assets_dir,
                )
            except Exception as e:
                log.debug(f"visual_capture failed: {e}")
                visual = None

            small_snap_path: Optional[str] = None
            mid_snap_path: Optional[str] = None
            if visual is not None:
                # Backward-compat: el compiler/player leen estos campos.
                # Solo escribimos ``anchor_bbox`` y ``click_offset`` cuando
                # el target NO es ``click_fallback`` — un fallback sintético
                # alrededor del click no es un anchor confiable.
                if visual.source != SRC_CLICK_FALLBACK:
                    metadata["anchor_bbox"] = dict(visual.target_bbox)
                    metadata["click_offset"] = {
                        "dx": int(visual.click_offset_px[0]),
                        "dy": int(visual.click_offset_px[1]),
                    }
                # Inyectamos toda la metadata enriquecida (forward-compat).
                metadata.update(visual.to_metadata())
                small_snap_path = visual.small_path
                mid_snap_path = visual.mid_path

            try:
                metadata["target_signature"] = build_target_signature(
                    x=x, y=y,
                    metadata=dict(metadata),
                    window_ctx=window_ctx,
                    small_snapshot=small_snap_path,
                    mid_snapshot=mid_snap_path,
                    hwnd_root=_hw,
                    small_px=100,
                    mid_px=600,
                )
            except Exception as e:
                log.debug(f"target_signature failed: {e}")

            # PRD 2026-05-12 (Visual Fingerprint Engine):
            # Computamos pHash + dHash + ORB sobre el crop ``small``
            # del target. La huella vive en metadata["visual_fingerprint"]
            # y la consume:
            #   * CandidateFusionEngine como evidencia adicional
            #     (sube confidence cuando hay anchor estructural pixel-perfect),
            #   * SmartMissionExecutor cuando el target se movió y
            #     necesita relocalizarlo en el frame actual.
            # Es best-effort: si cv2/numpy faltan o el crop no existe,
            # se guarda quality="insufficient" honestamente.
            try:
                from app.services.missions.visual_fingerprint import (
                    compute_fingerprint, HAS_CV2,
                )
                if HAS_CV2 and small_snap_path:
                    vfp = compute_fingerprint(small_snap_path)
                    if vfp.available:
                        metadata["visual_fingerprint"] = vfp.to_dict()
            except Exception as e:
                log.debug(f"visual_fingerprint failed: {e}")

            # Coords relativas a la ventana: permiten al player reconstruir el
            # click aun cuando la ventana se haya movido o reescalado.
            if window_ctx and getattr(window_ctx, "bounding_box", None):
                try:
                    bb = window_ctx.bounding_box or {}
                    ww = max(1, int(bb.get("width") or 0))
                    hh = max(1, int(bb.get("height") or 0))
                    rel_x = (x - int(bb.get("left") or 0)) / ww
                    rel_y = (y - int(bb.get("top") or 0)) / hh
                    # Clamp [0,1] para evitar valores absurdos si el bbox está mal.
                    rel_x = max(0.0, min(1.0, rel_x))
                    rel_y = max(0.0, min(1.0, rel_y))
                    metadata["rel_win"] = {"fx": round(rel_x, 6), "fy": round(rel_y, 6)}
                except Exception:
                    pass

            # ── PRD 2026-05-10h (Recorder Capture Contract) ──
            # 1) Pre-action snapshot del ring buffer: el frame ANTES del
            #    click suele contener la card que el humano vio. Sin
            #    esto, si la pantalla cambia tras el click, perdemos
            #    para siempre la evidencia visual del target.
            click_ts_ms = int(capture_ts.timestamp() * 1000) if capture_ts \
                else int(time.time() * 1000)
            pre_snap = None
            if self._pre_buffer is not None:
                try:
                    pre_snap = self._pre_buffer.get_before(click_ts_ms)
                except Exception as e:
                    log.debug(f"pre_buffer.get_before failed: {e}")

            # 2) OCR on-demand sobre la región expandida del pre-snap
            #    SOLO si UIA es estructuralmente débil (GroupControl
            #    vacío / click_fallback / quality low). Regla general,
            #    no app-specific.
            ocr_result = None
            try:
                window_area = 0
                if window_ctx and getattr(window_ctx, "bounding_box", None):
                    bb = window_ctx.bounding_box or {}
                    window_area = int(bb.get("width") or 0) * int(
                        bb.get("height") or 0
                    )
                if is_uia_weak(
                    metadata.get("uia"),
                    target_source=metadata.get("target_source"),
                    quality_level=metadata.get("quality_level"),
                    window_area=window_area,
                ) and pre_snap is not None and pre_snap.full_path:
                    ocr_result = maybe_run_ocr(
                        pre_snapshot_full=pre_snap.full_path,
                        click_xy=(x, y),
                    )
            except Exception as e:
                log.debug(f"ocr_fallback failed: {e}")

            # 3) Inyectamos pre_snapshot + ocr + parent_chain (ya escrito
            #    por _capture_uia) en metadata bajo las keys que los
            #    consumers esperan.
            try:
                parent_chain = (metadata.get("uia") or {}).get("parent_chain")
                apply_capture_contract_to_metadata(
                    metadata,
                    pre_snapshot=pre_snap,
                    parent_chain=parent_chain,
                    ocr=ocr_result,
                )
            except Exception as e:
                log.debug(f"apply_capture_contract failed: {e}")

            # 3a) Pre-Click Operational Freeze (PCOF): consolidar identidad
            #     operacional ANTES de comparar target vs outcome.
            try:
                from app.services.runtime.pre_click_operational_freeze import (
                    pcof_enabled,
                    process_click_with_pcof,
                )
                if pcof_enabled():
                    wc_pcof: Dict[str, Any] = {}
                    if window_ctx is not None:
                        wc_pcof = {
                            "title": getattr(window_ctx, "title", "") or "",
                            "process_name": getattr(
                                window_ctx, "process_name", "",
                            ) or "",
                            "hwnd": getattr(window_ctx, "hwnd", None),
                        }
                    pre_path = pre_snap.full_path if pre_snap is not None else None
                    metadata = process_click_with_pcof(
                        metadata,
                        x=int(x),
                        y=int(y),
                        window_context=wc_pcof,
                        pre_snapshot_path=pre_path,
                    )
            except Exception as e:
                log.debug(f"pcof.process_click failed: {e}")

            # 3a-FFEC) Promover freeze → Canonical Operational Intent antes
            #     de intent layer / capture contract (verdad PRE inmutable).
            try:
                from app.services.missions.freeze_first_execution_core import (
                    promote_and_attach_from_metadata,
                )
                promote_and_attach_from_metadata(
                    metadata,
                    mission=self.mission,
                )
            except Exception as e:
                log.debug(f"ffec.promote failed: {e}")

            # 3b) Target ↔ outcome isolation (PRD 2026-05-10i):
            #     re-leemos el anchor AHORA (después de toda la
            #     captura UIA/web/visual) y, si el estado cambió,
            #     migramos las identidades contaminadas a
            #     ``debug_after_state``. Las funciones que arman
            #     ``target_identity`` (recorder_truth_layer,
            #     screen_element_reader) leen ``metadata["uia"]`` /
            #     ``metadata["web"]`` / ``metadata["anchor_bbox"]``,
            #     así que retirar esas keys es suficiente para
            #     garantizar que la post-action UIA NO se use como
            #     identidad.
            try:
                after_anchor = capture_target_anchor(
                    _get_window_context_at_point(x, y),
                    metadata.get("web"),
                    phase=CAPTURE_PHASE_POST_ACTION,
                    source=ANCHOR_SOURCE_PYNPUT_POST_CAPTURE,
                )
                enforce_target_identity_isolation(
                    metadata,
                    before=before_anchor,
                    after=after_anchor,
                    click_xy=(int(x), int(y)),
                )
            except Exception as e:
                log.debug(f"enforce_target_identity_isolation failed: {e}")

            # 4) Capture contract: declara honestamente qué llegó.
            try:
                contract = build_capture_contract(
                    pre_snapshot=pre_snap,
                    post_state=None,  # se cubre en post_action diferido.
                    uia=metadata.get("uia"),
                    parent_chain=(metadata.get("uia") or {}).get(
                        "parent_chain"
                    ),
                    ocr=ocr_result,
                    target_source=metadata.get("target_source"),
                    quality_level=metadata.get("quality_level"),
                    target_identity_isolation=metadata.get(
                        "target_identity_isolation"
                    ),
                    canonical_operational_intent=metadata.get(
                        "canonical_operational_intent"
                    ),
                )
                metadata["capture_contract"] = contract
            except Exception as e:
                log.debug(f"build_capture_contract failed: {e}")

            # ── PRD 2026-05-10j (Intent Interception Layer) ──
            # Pasamos los datos ya capturados a la capa para que decida
            # confidence + ambiguity + evidence_sources y enriquezca el
            # metadata con ``target_identity``, ``intent_decision``,
            # ``fusion_audit``. La capa no reejecuta UIA/DOM/OCR — solo
            # fusiona con lo que ya extrajimos.
            if self._intent_handle is not None:
                try:
                    from app.services.missions.recorder_intent_integration import (
                        process_click_through_layer,
                    )
                    window_dict: Dict[str, Any] = {}
                    if window_ctx is not None:
                        window_dict = {
                            "process_name": getattr(
                                window_ctx, "process_name", "",
                            ) or "",
                            "title": getattr(window_ctx, "title", "") or "",
                            "hwnd": getattr(window_ctx, "hwnd", None),
                            "url": (metadata.get("web") or {}).get("url") or "",
                        }
                    intent_meta = process_click_through_layer(
                        self._intent_handle,
                        x=int(x), y=int(y), click_ms=int(click_ts_ms),
                        uia=metadata.get("uia"),
                        dom=metadata.get("web"),
                        ocr=ocr_result,
                        visual=(
                            visual.to_metadata() if visual is not None else None
                        ),
                        parent_chain=(metadata.get("uia") or {}).get(
                            "parent_chain"
                        ),
                        window_ctx=window_dict,
                        visual_fingerprint=metadata.get("visual_fingerprint"),
                        strict=self._strict_intent,
                    )
                    if intent_meta:
                        coi_raw = metadata.get("canonical_operational_intent") or {}
                        if isinstance(coi_raw, dict) and coi_raw.get("canonical_truth"):
                            ent = coi_raw.get("entity") or {}
                            nm = str(
                                (ent.get("display_name") if isinstance(ent, dict) else "")
                                or "",
                            ).strip()
                            intent_meta["intent_decision"] = "accept"
                            intent_meta["intent_confidence"] = max(
                                float(intent_meta.get("intent_confidence") or 0.0),
                                float(coi_raw.get("truth_strength") or 0.9),
                            )
                            intent_meta["ffec_canonical_override"] = True
                            if nm:
                                tid = dict(intent_meta.get("target_identity") or {})
                                tid["label"] = nm
                                tid["confidence_label"] = "high"
                                intent_meta["target_identity"] = tid
                        metadata["intent_layer"] = intent_meta
                except Exception as e:
                    log.debug(f"intent_layer.process_click failed: {e}")

            # Si tenemos timestamp/sequence_id capturados ANTES de UIA/web/screenshot,
            # usamos esos. Bug pre-2026: asignar el timestamp acá
            # después del trabajo pesado retrasaba el evento y partía
            # las sesiones de texto cuando un click coincidía con
            # tipeo rápido.
            evt_ts = capture_ts or datetime.now(timezone.utc)
            evt_kwargs = dict(
                event_type=event_type,
                mouse_action=MouseAction(x=x, y=y, button=btn_str),
                window_context=window_ctx,
                timestamp=evt_ts,
                metadata={
                    **metadata,
                    "processed_timestamp": (
                        datetime.now(timezone.utc).isoformat()
                    ),
                    "listener_type": "mouse",
                },
                snapshot_ref=small_snap_path,
            )
            if capture_seq is not None:
                evt_kwargs["sequence_id"] = capture_seq
            step = RawEvent(**evt_kwargs)
            self.callback(step)

            # 5) Post-action capture diferida (300/800/1500 ms): el
            #    scheduler corre en background y, al detectar cambio
            #    de title/hwnd/url, invoca on_post_action que escribe
            #    en el RawEvent ya emitido. NO bloquea el listener.
            #    Aunque la grabación se detenga inmediatamente, el
            #    scheduler.wait_all() en stop() le da hasta 2 s para
            #    completar.
            if (self._post_scheduler is not None
                    and self._on_post_action is not None):
                try:
                    before_state = {
                        "hwnd": getattr(window_ctx, "hwnd", None),
                        "title": getattr(window_ctx, "title", "") or "",
                        "url": (metadata.get("web") or {}).get("url") or "",
                    }
                    self._post_scheduler.schedule(
                        event_id=step.id,
                        click_ts_ms=click_ts_ms,
                        before_state=before_state,
                        capture_state=_capture_post_state_default,
                        capture_screenshot=None,
                        on_complete=lambda ps, eid=step.id: (
                            self._on_post_action(eid, ps)
                            if self._on_post_action else None
                        ),
                    )
                except Exception as e:
                    log.debug(f"post_action schedule failed: {e}")
        except Exception as e:
            log.error(f"process_click FAILED: {e}")

    # ──────────────────────────────────────────────────────────────
    # Drag & drop
    # ──────────────────────────────────────────────────────────────
    def _process_drag(
        self, sx: int, sy: int, ex: int, ey: int, duration: float, btn_str: str,
        capture_ts: Optional[datetime] = None,
        capture_seq: Optional[int] = None,
    ):
        try:
            window_ctx = _get_window_context_at_point(sx, sy)
            metadata = self._capture_uia(sx, sy)
            metadata["drag"] = {"start": [sx, sy], "end": [ex, ey], "duration_s": round(duration, 3)}
            small_snap = self._capture_snapshot(sx, sy, size=100)

            evt_ts = capture_ts or datetime.now(timezone.utc)
            evt_kwargs = dict(
                event_type=EventType.MOUSE_DRAG,
                drag_action=DragAction(start_x=sx, start_y=sy, end_x=ex, end_y=ey, duration=max(0.1, duration)),
                window_context=window_ctx,
                timestamp=evt_ts,
                metadata={
                    **metadata,
                    "processed_timestamp": (
                        datetime.now(timezone.utc).isoformat()
                    ),
                    "listener_type": "mouse",
                },
                snapshot_ref=str(small_snap) if small_snap else None,
            )
            if capture_seq is not None:
                evt_kwargs["sequence_id"] = capture_seq
            step = RawEvent(**evt_kwargs)
            self.callback(step)
        except Exception as e:
            log.error(f"process_drag FAILED: {e}")

    # ──────────────────────────────────────────────────────────────
    # Scroll (coalesce)
    # ──────────────────────────────────────────────────────────────
    def _on_scroll_tick(self, x: int, y: int, dx: int, dy: int):
        st = self._scroll_state
        now = time.time()
        if not st or (now - st.get("last_t", 0)) > _SCROLL_COALESCE_S or (x, y) != (st.get("x"), st.get("y")):
            # flush anterior
            if st:
                self._flush_scroll_locked(st)
            st = {"x": x, "y": y, "dx": 0, "dy": 0, "first_t": now, "last_t": now}
            self._scroll_state = st
        st["dx"] += int(dx)
        st["dy"] += int(dy)
        st["last_t"] = now
        # timer corto para flush si no llegan más ticks
        def _deferred():
            time.sleep(_SCROLL_COALESCE_S + 0.05)
            cur = self._scroll_state
            if cur is st and (time.time() - st["last_t"]) >= _SCROLL_COALESCE_S:
                self._flush_scroll_locked(st)
                if self._scroll_state is st:
                    self._scroll_state = {}
        threading.Thread(target=_deferred, daemon=True).start()

    def _flush_scroll(self):
        if self._scroll_state:
            self._flush_scroll_locked(self._scroll_state)
            self._scroll_state = {}

    def _flush_scroll_locked(self, st: Dict[str, Any]):
        try:
            x, y = st["x"], st["y"]
            dx, dy = st["dx"], st["dy"]
            if dx == 0 and dy == 0:
                return
            metadata = {"scroll": {"dx": int(dx), "dy": int(dy)}}
            window_ctx = _get_window_context_at_point(x, y)
            # UIA opcional
            metadata.update(self._capture_uia(x, y))
            step = RawEvent(
                event_type=EventType.MOUSE_SCROLL,
                mouse_action=MouseAction(x=x, y=y, button="scroll"),
                window_context=window_ctx,
                timestamp=datetime.now(timezone.utc),
                metadata=metadata,
            )
            self.callback(step)
        except Exception as e:
            log.error(f"scroll flush: {e}")

    # ──────────────────────────────────────────────────────────────
    # UIA + snapshots
    # ──────────────────────────────────────────────────────────────
    def _capture_uia(self, x: int, y: int) -> Dict[str, Any]:
        """Captura UIA enriquecida: name/aid/class/control_type + bbox estructurado.

        Cambio 2026-05-03: ya NO rechazamos aquí los contenedores grandes —
        eso lo decide ``visual_capture.select_target_bbox`` con la regla
        canónica del 25% del área de la ventana y la lista negra de
        nombres técnicos. Aquí siempre devolvemos el bbox crudo del
        elemento que UIA reporta tras ``_refine_to_meaningful``: el
        downstream sabe descartarlo si no califica.

        ``metadata["anchor_bbox"]`` y ``metadata["click_offset"]`` los
        escribe ``_process_click`` desde la salida ya validada del
        pipeline visual.
        """
        metadata: Dict[str, Any] = {}
        if not HAS_UIA:
            return metadata
        try:
            with auto.UIAutomationInitializerInThread():
                elem = auto.ControlFromPoint(x, y)
                try:
                    nh = getattr(elem, "NativeWindowHandle", 0) or 0
                    if nh and _is_own_hwnd(int(nh)):
                        log.debug(
                            "UIA skip: elemento bajo click pertenece a Nevlan"
                        )
                        return metadata
                except Exception:
                    pass
                elem = _refine_to_meaningful(elem, x, y)
                if elem:
                    bbox_struct: Optional[Dict[str, int]] = None
                    try:
                        r = elem.BoundingRectangle
                        left = int(getattr(r, "left", 0))
                        top = int(getattr(r, "top", 0))
                        right = int(getattr(r, "right", 0))
                        bottom = int(getattr(r, "bottom", 0))
                        w = max(0, right - left)
                        h = max(0, bottom - top)
                        if w > 0 and h > 0:
                            bbox_struct = {
                                "left": left, "top": top,
                                "width": w, "height": h,
                            }
                    except Exception:
                        pass

                    uia_info: Dict[str, Any] = {
                        "name": elem.Name,
                        "control_type": elem.ControlTypeName,
                        "automation_id": elem.AutomationId,
                        "class_name": elem.ClassName,
                        "rect": str(elem.BoundingRectangle),
                    }
                    if bbox_struct:
                        uia_info["bbox"] = bbox_struct
                    # PRD 2026-05-10h (Recorder Capture Contract):
                    # captura del parent_chain como evidencia
                    # adicional. Es especialmente útil cuando el elem
                    # es un GroupControl genérico — el padre o un
                    # sibling cercano suelen tener el texto real.
                    try:
                        chain = capture_uia_parent_chain(
                            elem, click_xy=(int(x), int(y)),
                        )
                        if chain is not None:
                            uia_info["parent_chain"] = chain
                    except Exception as e:
                        log.debug(f"parent_chain capture failed: {e}")
                    metadata["uia"] = uia_info
        except Exception as e:
            log.debug(f"UIA capture failed: {e}")
        return metadata

    def _capture_snapshot(self, x: int, y: int, size: int = 100, suffix: str = "") -> Optional[Path]:
        """Crop alrededor del punto (size px por lado). suffix='mid' para contexto ancho."""
        try:
            assets_dir = (
                settings.VAR_PATH / "missions" / "assets" / self._mission_id
                if self._mission_id
                else settings.VAR_PATH / "missions" / "assets" / "temp"
            )
            assets_dir.mkdir(parents=True, exist_ok=True)
            tag = f"_{suffix}" if suffix else ""
            filename = f"step{tag}_{uuid.uuid4().hex[:8]}.png"
            path = assets_dir / filename

            from PIL import ImageGrab
            half = max(50, size // 2)
            left, top = int(x - half), int(y - half)
            right, bottom = int(x + half), int(y + half)

            try:
                screenshot = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
            except Exception:
                screenshot = ImageGrab.grab(
                    bbox=(max(0, left), max(0, top), max(half * 2, right), max(half * 2, bottom))
                )
            screenshot.save(path)
            return path
        except Exception as e:
            log.debug(f"Snapshot failed ({suffix or 'small'}): {e}")
            return None


# ============================================================================
# KeyboardListener — rich capture: modifiers reales, hotkeys, secuencias
# ============================================================================

# Qué consideramos "modifiers" activos durante la grabación
_MODIFIER_ALIASES = {
    # pynput representa modificadores como Key.ctrl_l / Key.ctrl_r / etc.
    "ctrl_l": "ctrl", "ctrl_r": "ctrl", "ctrl": "ctrl",
    "shift_l": "shift", "shift_r": "shift", "shift": "shift",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt", "alt": "alt",
    "cmd": "win", "cmd_l": "win", "cmd_r": "win", "win": "win",
}


def _normalize_pynput_key(key) -> str:
    """Convierte el objeto key de pynput en un string canónico.

    - 'a' / '1' / ';'  → tal cual (en minúsculas)
    - Key.tab → 'tab', Key.enter → 'enter', Key.esc → 'esc'
    - Key.ctrl_l → 'ctrl_l', Key.alt_r → 'alt_r' (se simplifica en compiler)
    """
    try:
        char = getattr(key, "char", None)
        if char is not None and char != "":
            return str(char).lower()
    except Exception:
        pass
    s = str(key).lower()
    if s.startswith("key."):
        s = s[4:]
    return s.strip("'\"")


def _is_modifier(k: str) -> bool:
    return k in _MODIFIER_ALIASES


class KeyboardListener(InputListener):
    """Captura presión + liberación, rastreando modificadores activos.

    Cada key_press se emite con modifiers (lista canónica) ya resuelta.
    Esto permite al compiler construir hotkeys reales (ctrl+c, shift+tab, etc.).

    ── CapsLock absorbido como estado de texto ─────────────────────
    CapsLock NO es una acción del usuario, es un estado del teclado.
    Nunca lo emitimos como ``RawEvent`` independiente — ¡jamás como
    paso visible! Solo lo aplicamos al casing de las teclas
    siguientes. Bug observado en grabaciones reales el 2026-05-03:

        click "Escribe para buscar"
        Presionar CAPSLOCK × 2     ← BASURA
        Escribir "C"
        Presionar CAPSLOCK          ← BASURA
        Escribir "hrome"            ← FRAGMENTO

    Resultado deseado:
        click "Escribe para buscar"
        Escribir "Chrome"
    """

    def __init__(self, callback: Callable[[RawEvent], None]):
        self.callback = callback
        self._listener = None
        self._active = False
        self._active_modifiers: set = set()
        # ``capslock_on`` es un toggle. Empezamos asumiendo OFF; cada
        # press de CapsLock alterna. Esto puede salirse de sync con el
        # estado real del teclado al inicio de una grabación, pero el
        # usuario lo arregla con un toggle más. NO usamos GetKeyState
        # de Win32 porque no necesitamos exactitud absoluta — solo
        # consistencia DENTRO de la sesión grabada para no perder
        # mayúsculas.
        self._capslock_on: bool = False
        # Inicialización best-effort: si Win32 está disponible, leemos
        # el estado real al arrancar para empezar sincronizados.
        try:
            if _HAS_W32:
                # GetKeyState(VK_CAPITAL=0x14) → bit bajo = toggled
                state = _user32.GetKeyState(0x14) & 0x0001
                self._capslock_on = bool(state)
        except Exception:
            self._capslock_on = False

    def start(self) -> None:
        if self._active:
            return
        try:
            from pynput.keyboard import Listener as PyKeyListener

            def on_press(key):
                try:
                    k = _normalize_pynput_key(key)
                except Exception:
                    k = str(key)

                # ── CapsLock: ABSORBER, nunca emitir ────────────────
                # El toggle modifica el casing de las teclas
                # siguientes pero NUNCA aparece como paso. Bug del
                # 2026-05-03: aparecía como "Presionar CAPSLOCK"
                # entre fragmentos de texto, partiendo "Chrome" en
                # "C" + "hrome".
                if k in ("capslock", "caps_lock"):
                    self._capslock_on = not self._capslock_on
                    return

                if _is_modifier(k):
                    canon = _MODIFIER_ALIASES[k]
                    self._active_modifiers.add(canon)
                    # No publicamos evento suelto por modificadores: sólo cambian estado.
                    # Esto evita ruido tipo "Key.ctrl_l" como tecla independiente.
                    return

                # Aplicamos el efecto de CapsLock al carácter:
                # CapsLock invierte el casing de letras. Combinado
                # con shift, los efectos se cancelan (CapsLock+shift+a → a).
                if (
                    self._capslock_on
                    and isinstance(k, str)
                    and len(k) == 1
                    and k.isalpha()
                ):
                    # Invertimos el casing. ``_normalize_pynput_key`` ya
                    # devuelve la letra en minúsculas, pero al combinar
                    # con shift + capslock el resultado debe ser minúscula.
                    if "shift" in self._active_modifiers:
                        k = k.lower()
                    else:
                        k = k.upper()

                # Capturar timestamp INMEDIATAMENTE (antes incluso
                # de leer el window context que puede ser caro).
                capture_ts = datetime.now(timezone.utc)
                mods = sorted(self._active_modifiers)
                # ``window_context`` es una operación de Win32 que
                # puede tardar 1-5ms. Para teclas eso es aceptable —
                # son ~50-200ms entre teclas humanas. Pero dejamos
                # registro del retraso para diagnóstico si el orden
                # se rompe.
                step = RawEvent(
                    event_type=EventType.KEYBOARD_KEY_PRESS,
                    keyboard_action=KeyboardAction(keys=[k], modifiers=mods),
                    window_context=_get_window_context(),
                    timestamp=capture_ts,
                    metadata={
                        **({"modifiers_active": mods} if mods else {}),
                        "capslock_on": bool(self._capslock_on),
                        "processed_timestamp": (
                            datetime.now(timezone.utc).isoformat()
                        ),
                        "listener_type": "keyboard",
                    },
                )
                self.callback(step)

            def on_release(key):
                try:
                    k = _normalize_pynput_key(key)
                except Exception:
                    k = str(key)
                if _is_modifier(k):
                    canon = _MODIFIER_ALIASES[k]
                    self._active_modifiers.discard(canon)

            self._listener = PyKeyListener(on_press=on_press, on_release=on_release)
            self._listener.start()
            self._active = True
        except Exception:
            self._active = False

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None
        self._active = False
        self._active_modifiers.clear()

    def is_active(self) -> bool:
        return self._active


# ============================================================================
# MissionRecorder
# ============================================================================

class MissionRecorder:
    def __init__(self, mission_name: str = ""):
        self._lock = threading.Lock()
        self.mission_name = mission_name or f"mission_{int(time.time())}"
        self.mission: Optional[Mission] = None
        self._recording = False
        self._paused = False
        self._start_time: float = 0
        self._paused_duration: float = 0
        self._pause_start: float = 0
        self._last_step_time: float = 0.0
        self._last_step_id: Optional[str] = None

        self.mouse_listener: Optional[MouseListener] = None
        self.keyboard_listener: Optional[KeyboardListener] = None

        self._pending_annotations: List[Annotation] = []
        # Configuración de ventana de tiempo (ej. 3.5 segundos)
        self.ATTACH_WINDOW_SEC = 3.5

        # ── Field Editing Session (en vivo) ─────────────────────
        # Una "sesión" empieza cuando el usuario hace foco en un campo
        # y empieza a tipear. Se cierra cuando: tab/enter/esc, click
        # fuera, hotkey de navegación, o fin de grabación. Mientras
        # está abierta, NO publicamos cada tecla en el bus — solo un
        # "Escribiendo en {campo}…" inicial. Al cerrar, leemos el
        # valor real del campo (UIA ValuePattern o DOM) y publicamos
        # "Rellenar {campo} con {valor}".
        #
        # Estrategia de lectura transaccional (clave para FASE 3):
        # - Mantenemos un *snapshot continuo* del valor leído tras
        #   cada tecla (best-effort). Así, cuando llega un evento de
        #   cierre (click fuera, Tab, Enter), aunque el foco ya haya
        #   migrado, tenemos el último valor confiable.
        # - Identidad del campo cacheada (AutomationId+ControlType+Name
        #   y, para web, locator favorito) para releer SIN depender
        #   de GetFocusedControl al cerrar.
        self._field_session_active: bool = False
        self._field_session_label: str = ""
        self._field_session_anchor_id: Optional[str] = None  # raw_event id que abrió la sesión
        self._field_session_buf: List[str] = []
        self._field_session_target_meta: Dict[str, Any] = {}
        self._field_session_web_meta: Dict[str, Any] = {}
        self._field_session_last_known_value: Optional[str] = None
        self._field_session_initial_value: Optional[str] = None
        self._field_session_dirty: bool = False
        self._field_session_anchor_snapshot: Optional[str] = None
        self._field_session_anchor_winctx: Optional[WindowContext] = None
        self._prepend_trace_events: List[RawEvent] = []
        self._suppress_current_event_trace: bool = False

        # PRD 2026-05-10h (Recorder Capture Contract):
        # buffer pre-click + post-action scheduler. Se inicializan
        # tarde (en start()) porque dependen de assets_dir de la misión.
        self._pre_buffer: Optional[ScreenshotRingBuffer] = None
        self._post_scheduler: Optional[PostActionScheduler] = None

        # PRD 2026-05-10j (Intent Interception Layer):
        # Handle de la capa + flag de modo seguro. Si es True, la
        # grabación falla cuando la capa no se pudo construir.
        # Default True: la nueva regla de producto es "premium o
        # nada", pero se puede desactivar en tests viejos vía
        # ``strict_intent_interception=False``.
        self._intent_handle: Any = None
        self.strict_intent_interception: bool = True
        # Permitir instalar el Win32 hook (default True en Windows).
        # Tests / entornos no-Windows lo apagan.
        self.install_win32_hook: bool = True

    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def release_input_listeners(self) -> None:
        """Detiene pynput de forma idempotente.

        Debe llamarse SIN sostener ``self._lock`` para no bloquear con el
        hilo de pynput que aún podría estar en ``_on_event``.
        Un segundo ``start`` con listeners vivos provoca cierres/instabilidad
        de la app en Windows.
        """
        if self.mouse_listener:
            try:
                self.mouse_listener.stop()
            except Exception as e:
                log.debug(f"mouse_listener.stop: {e}")
            self.mouse_listener = None
        if self.keyboard_listener:
            try:
                self.keyboard_listener.stop()
            except Exception as e:
                log.debug(f"keyboard_listener.stop: {e}")
            self.keyboard_listener = None

    def start(self) -> Mission:
        with self._lock:
            if self._recording:
                raise RuntimeError("Already recording")

            self.mission = Mission(name=self.mission_name, status=MissionStatus.RECORDING)
            try:
                from app.services.missions.recorder_semantic_shadow import (
                    is_shadow_enabled as _shadow_on,
                )
                if _shadow_on(settings):
                    self.mission.semantic_first_mode = "shadow"
                    self.mission.semantic_first_version = "1"
                if getattr(settings, "SEMANTIC_SESSION_SHADOW_ENABLED", False):
                    self.mission.session_shadow_mode = True
                if getattr(settings, "SEMANTIC_COB_SHADOW_ENABLED", False):
                    self.mission.cob_shadow_mode = True
                if getattr(settings, "TEL_SHADOW_ENABLED", False):
                    self.mission.tel_shadow_mode = True
            except Exception:
                pass

            self._recording = True
            self._start_time = time.time()

            # Assign assets path
            assets_dir = settings.VAR_PATH / "missions" / "assets" / self.mission.id
            assets_dir.mkdir(parents=True, exist_ok=True)

            # PRD 2026-05-10h (Recorder Capture Contract):
            # buffer + scheduler. Si PIL no está, el grabber retorna
            # None silenciosamente y get_before también; el contract
            # reportará has_pre_snapshot=False honestamente.
            buffer_dir = assets_dir / "_prebuf"
            self._pre_buffer = ScreenshotRingBuffer(buffer_dir)
            self._post_scheduler = PostActionScheduler()

            # PRD 2026-05-10j (Intent Interception Layer):
            # Construimos la capa antes de los listeners para que el
            # ``MouseListener`` pueda referirla en cada click. Si no
            # se puede construir y estamos en modo estricto, levantamos.
            try:
                from app.services.missions.recorder_intent_integration import (
                    build_layer_for_recording,
                    ensure_layer_or_fail,
                )
                self._intent_handle = build_layer_for_recording(
                    pre_buffer=self._pre_buffer,
                    post_scheduler=self._post_scheduler,
                    install_win32_hook=self.install_win32_hook,
                    is_own_hwnd=lambda hwnd: _is_own_hwnd(int(hwnd)),
                )
                ensure_layer_or_fail(
                    self._intent_handle,
                    strict=self.strict_intent_interception,
                )
            except Exception as e:
                # En modo estricto re-lanzamos para fail-safe; en
                # non-strict seguimos con _intent_handle=None.
                self._intent_handle = None
                if self.strict_intent_interception:
                    self._recording = False
                    log.error(
                        f"MissionRecorder: IntentInterceptionLayer no "
                        f"disponible y modo estricto activo: {e}"
                    )
                    raise
                log.debug(f"IntentInterceptionLayer build failed (non-strict): {e}")

            self.mouse_listener = MouseListener(
                self._on_event,
                mission_id=str(self.mission.id),
                pre_buffer=self._pre_buffer,
                post_scheduler=self._post_scheduler,
                on_post_action=self._patch_post_action,
                intent_layer_handle=self._intent_handle,
                strict_intent_interception=self.strict_intent_interception,
            )
            self.keyboard_listener = KeyboardListener(self._on_event)

        try:
            try:
                if self._pre_buffer:
                    self._pre_buffer.start()
            except Exception as e:
                log.debug(f"pre_buffer.start failed: {e}")
            self.mouse_listener.start()
            self.keyboard_listener.start()
        except Exception as e:
            log.error(f"MissionRecorder: fallo al arrancar listeners: {e}")
            with self._lock:
                self._recording = False
            self.release_input_listeners()
            raise
        with self._lock:
            return self.mission  # type: ignore[return-value]

    def stop(self) -> Mission:
        m: Optional[Mission] = None
        with self._lock:
            if not self._recording:
                raise RuntimeError("Not recording")
            # Cerramos cualquier Field Editing Session pendiente para que
            # el valor final aparezca como un paso "Rellenar X con Y".
            try:
                self._close_field_session(prefer_uia=True)
            except Exception as e:
                log.debug(f"close_field_session on stop: {e}")
            # Commits sintéticos encolados al cerrar sesión (sin evento de teclado).
            while self._prepend_trace_events:
                pe = self._prepend_trace_events.pop(0)
                try:
                    self._append_single_raw_event(pe)
                except Exception as e2:
                    log.debug(f"flush prepend on stop: {e2}")
            self._recording = False
            m = self.mission
            if m:
                m.status = MissionStatus.DRAFT
        # FUERA del lock: detener pynput (puede bloquear) sin riesgo de
        # interbloqueo con _on_event que también toma _lock.
        self.release_input_listeners()
        # PRD 2026-05-10h: dar al scheduler tiempo (≤ 2 s) para que el
        # post_action del último click termine. Esto cubre el caso
        # crítico: el usuario detiene la grabación inmediatamente
        # después del click — sin esto, el last click se quedaba sin
        # after_state y Outcome Intelligence no podía confirmar.
        try:
            if self._post_scheduler is not None:
                self._post_scheduler.wait_all()
        except Exception as e:
            log.debug(f"post_scheduler.wait_all failed: {e}")
        try:
            if self._pre_buffer is not None:
                self._pre_buffer.stop()
        except Exception as e:
            log.debug(f"pre_buffer.stop failed: {e}")
        # PRD 2026-05-10j: apagamos el hook Win32 de la capa si quedó vivo.
        try:
            if self._intent_handle is not None:
                self._intent_handle.shutdown()
        except Exception as e:
            log.debug(f"intent_handle.shutdown failed: {e}")
        self._intent_handle = None
        if not m:
            raise RuntimeError("MissionRecorder sin misión al detener")
        try:
            from app.services.missions.truth_extraction_layer import (
                apply_tel_to_mission_record,
            )

            apply_tel_to_mission_record(m)
        except Exception as e:
            log.debug(f"tel apply stop: {e}")
        return m

    def pause(self):
        with self._lock:
            if self._recording and not self._paused:
                self._paused = True
                self._pause_start = time.time()

    def resume(self):
        with self._lock:
            if self._recording and self._paused:
                self._paused = False
                self._paused_duration += time.time() - self._pause_start

    def add_annotation(self, type_str: str, value: str, target_id: Optional[str] = None):
        with self._lock:
            if not self.mission: return
            ann = Annotation(annotation_type=AnnotationType(type_str), value=value)
            
            # Lógica de Pegado Automático
            if not target_id:
                time_since_last_step = time.time() - self._last_step_time
                if self._last_step_id and time_since_last_step <= self.ATTACH_WINDOW_SEC:
                    ann.target_step_id = self._last_step_id
                    self.mission.user_annotations.append(ann)
                else:
                    self._pending_annotations.append(ann)
            else:
                ann.target_step_id = target_id
                self.mission.user_annotations.append(ann)

    # ─────────────────────────────────────────────────────────────
    # Field Editing Session — narración en vivo, NO graba cada tecla
    # ─────────────────────────────────────────────────────────────

    _CLOSING_SPECIAL_KEYS = {"tab", "enter", "return", "escape", "esc",
                             "up", "down", "pageup", "pagedown",
                             "f1", "f2", "f3", "f4", "f5", "f6"}
    # Teclas que NUNCA cierran la sesión: son toggles de estado del
    # teclado o teclas que solo modifican la siguiente. CapsLock es la
    # más importante — bug 2026-05-03: cerraba la sesión y partía
    # "Chrome" en "C" + "hrome".
    _ABSORBABLE_KEYS = {"capslock", "caps_lock", "numlock", "num_lock",
                        "scrolllock", "scroll_lock"}

    def _publish_live(self, text: str, icon: str = "⌨️",
                       quality: Optional[str] = None) -> None:
        """Publica un paso 'humano' al overlay de grabación.

        ``quality`` puede ser:
          - "good": señales fuertes (UIA AutomationId, web locator estable)
          - "medium": señales aceptables (UIA name, anchor visual)
          - "weak": solo coords/visión débil
          - None: la UI elige color por defecto
        """
        try:
            from app.runtime.bus import bus
            from app.contracts.events import SystemEvent
            payload = {"text": text, "icon": icon}
            if quality:
                payload["quality"] = quality
            bus.publish(SystemEvent(name="mission.live_step", payload=payload))
        except Exception:
            pass

    @staticmethod
    def _quality_from_step(step: RawEvent) -> str:
        """Heurística rápida sobre la calidad del target capturado en el
        evento crudo. No es definitiva (el compiler V3 calcula
        ``capture_score`` real), pero permite dar feedback inmediato
        durante la grabación.
        """
        meta = (step.metadata or {})
        web = meta.get("web") or {}
        uia = meta.get("uia") or {}
        # Web con locators estables → buena calidad
        for spec in (web.get("locators") or []):
            kind = spec.get("kind")
            if kind in ("test_id", "role", "label") and spec.get("value"):
                return "good"
        # UIA AutomationId
        if isinstance(uia, dict) and (uia.get("automation_id") or "").strip():
            return "good"
        # web genérico o UIA name razonable
        if (web.get("locators") or []) or (
            isinstance(uia, dict) and (uia.get("name") or "").strip()
        ):
            return "medium"
        return "weak"

    def _publish_field_writing(self, label: str) -> None:
        try:
            from app.runtime.bus import bus
            from app.contracts.events import SystemEvent
            bus.publish(SystemEvent(
                name="mission.field_writing",
                payload={"label": label or "campo"},
            ))
        except Exception:
            pass

    def _publish_field_committed(
        self,
        label: str,
        value: str,
        *,
        uia_hint: Optional[Dict[str, Any]] = None,
        web_hint: Optional[Dict[str, Any]] = None,
    ) -> None:
        short = value if len(value) <= 60 else value[:60] + "…"
        try:
            from app.runtime.bus import bus
            from app.contracts.events import SystemEvent
            bus.publish(SystemEvent(
                name="mission.field_committed",
                payload={"label": label or "campo", "value_preview": short},
            ))
        except Exception:
            pass
        # También aparece como live_step (UI lista de pasos):
        pretty = f"📝 Rellenar {label or 'campo'} con '{short}'"
        # Quality según lo que se almacenó al abrir la sesión.
        uia_meta = uia_hint if uia_hint is not None else (self._field_session_target_meta or {})
        web_meta = web_hint if web_hint is not None else (self._field_session_web_meta or {})
        if any(
            (s.get("kind") in ("test_id", "role", "label") and s.get("value"))
            for s in (web_meta.get("locators") or [])
        ) or (uia_meta.get("automation_id") or "").strip():
            q = "good"
        elif (web_meta.get("locators") or []) or (uia_meta.get("name") or "").strip():
            q = "medium"
        else:
            q = "weak"
        self._publish_live(pretty, icon="📝", quality=q)

    # ── Persistencia compacta del valor final en la traza (no micro-teclas)

    def _clear_field_session_state(self) -> None:
        """Borra estado en memoria de la sesión (post-commit)."""
        self._field_session_active = False
        self._field_session_label = ""
        self._field_session_anchor_id = None
        self._field_session_buf = []
        self._field_session_target_meta = {}
        self._field_session_web_meta = {}
        self._field_session_last_known_value = None
        self._field_session_initial_value = None
        self._field_session_dirty = False
        self._field_session_anchor_snapshot = None
        self._field_session_anchor_winctx = None

    def _should_emit_field_synth_step(
        self, fc: FieldCommitResult, dirty: bool
    ) -> bool:
        ini = fc.initial_value or ""
        fin = fc.final_text or ""
        if fin.strip() != (ini.strip()) or fin != ini:
            return True
        return bool(dirty and fin.strip() != "")

    def _build_field_commit_result(
        self, prefer_uia: bool,
    ) -> Optional[FieldCommitResult]:
        if not self._field_session_active:
            return None
        label = self._field_session_label
        initial = self._field_session_initial_value
        last_known = self._field_session_last_known_value
        user_buf = "".join(self._field_session_buf)
        user_buf_clean = user_buf.strip()

        # ── Lectura del campo (UIA o DOM) ─────────────────────────
        read_src = READ_SOURCE_KEYBOARD
        read_value: Optional[str] = None
        if prefer_uia:
            read_value = self._read_field_value_now()
            if read_value is not None:
                read_src = (
                    READ_SOURCE_DOM
                    if self._field_session_web_meta
                    else READ_SOURCE_UIA
                )

        value = ""
        chosen_reason = ""

        # Caso 1: la app no cambió el campo respecto a su valor inicial
        # pero el usuario sí escribió → preferir el último valor visto
        # mientras tipeaba.
        if (
            read_value is not None
            and initial is not None
            and read_value == initial
            and last_known is not None
            and last_known != initial
        ):
            value = last_known
            read_src = READ_SOURCE_LAST_KNOWN
            chosen_reason = "field_unchanged_use_last_known"
        elif read_value is not None:
            value = read_value
            chosen_reason = f"value_read_via_{read_src}"

        if not value and last_known and last_known != (initial or ""):
            value = last_known
            read_src = READ_SOURCE_LAST_KNOWN
            chosen_reason = "fallback_last_known"

        if not value:
            value = user_buf
            read_src = READ_SOURCE_KEYBOARD
            chosen_reason = "fallback_user_buffer"

        # ── Sanity check: URL como contaminación ─────────────────
        # Cubre dos casos:
        #   1) URL interna del navegador (chrome://, edge://, etc.):
        #      siempre es ruido si el usuario no la escribió.
        #   2) URL externa (https://www.youtube.com/, etc.) cuando el
        #      usuario no la escribió y/o el buffer dice otra cosa:
        #      DOM/UIA estaba leyendo ``page.url`` en lugar del input.
        #
        # Bug 2026-05-03: Nevlan grababa ``Escribir "https://www.youtube.com/"``
        # cuando el usuario solo había hecho click en el icono de YouTube.
        # La URL venía de ``page.url`` por lectura agresiva del DOM.
        from app.services.missions.field_commit import is_url_contamination

        url_contamination = is_url_contamination(
            read_value=value,
            user_typed=user_buf_clean,
        )
        source_is_url = is_browser_internal_url(value)

        if url_contamination:
            log.debug(
                f"field_commit: URL contamination detectada "
                f"(read={str(value)[:60]}, buf={user_buf_clean[:60]}), "
                "usando buffer del usuario."
            )
            value = user_buf
            read_src = READ_SOURCE_KEYBOARD_URL_SANITIZED
            chosen_reason = (
                "url_internal_overridden_by_buffer"
                if source_is_url
                else "external_url_overridden_by_buffer"
            )
            source_is_url = False  # ya descartamos la URL

        # Banderas semánticas para UI/tests/expert mode.
        source_is_app_state = (
            read_value is not None
            and initial is not None
            and read_value == initial
            and (not user_buf_clean)
        )
        source_is_input_value = read_src in (READ_SOURCE_UIA, READ_SOURCE_DOM)

        snap_ref = self._field_session_anchor_snapshot
        wc = self._field_session_anchor_winctx
        return FieldCommitResult(
            final_text=str(value),
            label=label or "",
            read_source=read_src,
            initial_value=initial,
            anchor_uia=dict(self._field_session_target_meta),
            anchor_web=dict(self._field_session_web_meta),
            window_context=wc,
            anchor_snapshot_ref=snap_ref,
            user_typed_text=user_buf,
            final_field_value=read_value,
            source_is_url=bool(source_is_url),
            source_is_app_state=bool(source_is_app_state),
            source_is_input_value=bool(source_is_input_value),
            chosen_final_text=str(value),
            reason=chosen_reason or "n/a",
        )

    def _read_field_value_via_dom(self) -> Optional[str]:
        """Intenta leer el valor real del campo via DOM (Playwright).
        Solo activo si la sesión recordó web_meta del paso de apertura
        (capturado por web_recorder en el click que enfocó el campo).
        Devuelve None si no se puede (no hay browser, no hay locator).
        """
        web = self._field_session_web_meta or {}
        if not web:
            return None
        try:
            from app.skills.tools.web import _get_page  # type: ignore
            page = _get_page()
        except Exception:
            return None
        # Probamos primero el locator más específico (test_id, role+name, label).
        for spec in (web.get("locators") or []):
            kind = (spec.get("kind") or "").lower()
            try:
                loc = None
                if kind == "test_id" and spec.get("value"):
                    loc = page.get_by_test_id(spec["value"])
                elif kind == "role" and spec.get("role"):
                    loc = page.get_by_role(spec["role"], name=spec.get("name") or None)
                elif kind == "label" and spec.get("value"):
                    loc = page.get_by_label(spec["value"])
                elif kind == "placeholder" and spec.get("value"):
                    loc = page.get_by_placeholder(spec["value"])
                elif kind == "css" and spec.get("value"):
                    loc = page.locator(spec["value"])
                elif kind == "xpath" and spec.get("value"):
                    loc = page.locator(f"xpath={spec['value']}")
                if loc is None:
                    continue
                if "nth" in spec:
                    try:
                        loc = loc.nth(int(spec["nth"]))
                    except Exception:
                        pass
                v = loc.input_value(timeout=200)
                if v is not None:
                    return str(v)
            except Exception:
                continue
        # Fallback: usar el bbox del paso de apertura para `elementFromPoint`
        # vía evaluate. Más débil pero útil cuando los locators no validaron.
        bbox = (web.get("bbox") or {})
        if bbox:
            try:
                cx = int(bbox.get("left", 0)) + int(bbox.get("width", 0)) // 2
                cy = int(bbox.get("top", 0)) + int(bbox.get("height", 0)) // 2
                # Compensar scroll: el bbox lo guardamos en coords del documento
                sx = int(web.get("scroll_x", 0) or 0)
                sy = int(web.get("scroll_y", 0) or 0)
                v = page.evaluate(
                    "(args) => {"
                    " const el = document.elementFromPoint(args.x, args.y);"
                    " return el && el.value != null ? String(el.value) : null;"
                    "}",
                    {"x": int(cx - sx), "y": int(cy - sy)})
                if v is not None:
                    return str(v)
            except Exception:
                pass
        return None

    def _read_field_value_via_uia(self) -> Optional[str]:
        """Intenta leer el valor real del campo enfocado vía UIA ValuePattern.

        Estrategia:
          1) Buscar por la identidad cacheada del campo
             (AutomationId / Name + ControlType) usando el HWND de la
             ventana del paso de apertura. Esto NO depende del foco
             actual, por lo que sigue funcionando aunque el click de
             cierre ya haya movido el foco.
          2) Fallback: GetFocusedControl().
        Devuelve None si no se puede.
        """
        if not HAS_UIA:
            return None
        meta = self._field_session_target_meta or {}
        aid = (meta.get("automation_id") or "").strip() or None
        name = (meta.get("name") or "").strip() or None
        ct = (meta.get("control_type") or "").strip() or None
        try:
            with auto.UIAutomationInitializerInThread():
                el = None
                # 1) buscar por identidad cacheada
                if aid or (name and ct):
                    try:
                        kwargs = {}
                        if aid:
                            kwargs["AutomationId"] = aid
                        if ct:
                            kwargs["ControlTypeName"] = ct
                        if name:
                            kwargs["Name"] = name
                        # Ámbito: descendientes del desktop. Es lento pero
                        # solo lo hacemos al cerrar la sesión (1 vez).
                        candidate = auto.GetRootControl().Control(**kwargs)
                        if candidate and candidate.Exists(maxSearchSeconds=0.4):
                            el = candidate
                    except Exception:
                        el = None
                # 2) fallback a foco actual
                if el is None:
                    try:
                        el = auto.GetFocusedControl()
                    except Exception:
                        el = None
                if el is None:
                    return None
                try:
                    vp = el.GetValuePattern()
                except Exception:
                    vp = None
                if vp is None:
                    return None
                v = getattr(vp, "Value", None)
                if v is None:
                    return None
                return str(v)
        except Exception:
            return None

    def _read_field_value_now(self) -> Optional[str]:
        """Lee el valor "ahora" combinando DOM (web) + UIA (desktop).
        Para web prioriza DOM porque es más fiable que UIA en sitios
        modernos (contenteditable, custom inputs).
        """
        # 1) si la sesión tiene metadata web, intentar DOM primero.
        if self._field_session_web_meta:
            v = self._read_field_value_via_dom()
            if v is not None:
                return v
        # 2) UIA (desktop o web sin DOM).
        return self._read_field_value_via_uia()

    def _refresh_field_value_cache(self) -> None:
        """Refresca el snapshot del valor actual del campo. Best-effort
        y silencioso: si no se puede leer, conserva el valor previo.
        Llamado tras cada tecla imprimible/backspace para que, cuando
        llegue un evento de cierre, tengamos el valor más reciente
        aunque el foco ya haya cambiado.
        """
        try:
            v = self._read_field_value_now()
        except Exception:
            v = None
        if v is not None:
            self._field_session_last_known_value = v

    def _open_field_session(self, step: RawEvent) -> None:
        """Abre una sesión de edición a partir del evento que la disparó.
        El step puede ser CLICK sobre Edit o el primer KEY_PRESS imprimible.
        """
        uia = (step.metadata or {}).get("uia") or {}
        web = (step.metadata or {}).get("web") or {}
        ctype = (uia.get("control_type") or "").lower()
        web_role = (web.get("role") or "").lower()
        web_tag = (web.get("tag") or "").lower()
        is_edit_like = (
            ctype in {"editcontrol", "edit", "documentcontrol",
                      "passwordcontrol", "comboboxcontrol"}
            or web_role in {"textbox", "searchbox", "combobox", "spinbutton"}
            or web_tag in {"input", "textarea", "select"}
        )
        # Si no es claramente un edit, igual abrimos sesión cuando haya
        # texto: muchas web apps no exponen ControlType correcto.
        label = (uia.get("name") or "").strip()
        if not label and web:
            label = (web.get("accessible_name") or web.get("label")
                     or web.get("placeholder") or web.get("test_id")
                     or web.get("name_attr") or "").strip()
        if not label:
            label = (uia.get("automation_id") or "").strip()
        self._field_session_active = True
        self._field_session_label = label
        self._field_session_anchor_id = step.id
        self._field_session_buf = []
        self._field_session_target_meta = dict(uia)
        self._field_session_web_meta = dict(web) if web else {}
        self._field_session_dirty = False
        self._field_session_anchor_snapshot = step.snapshot_ref
        self._field_session_anchor_winctx = step.window_context
        # Snapshot inicial del valor (para detectar si la app prerellenó
        # el campo o si el usuario solo modificó parte).
        self._field_session_initial_value = None
        try:
            initial = web.get("input_value_at_capture") if web else None
            if initial is None:
                initial = self._read_field_value_now()
            self._field_session_initial_value = initial
            self._field_session_last_known_value = initial
        except Exception:
            self._field_session_last_known_value = None
        if is_edit_like:
            self._publish_live(f"📝 Enfocando campo {label or '...'}", icon="📝")
        self._publish_field_writing(label)

    def _close_field_session(self, prefer_uia: bool = True) -> None:
        """Cierra la sesión: encola RawEvent(KEYBOARD_TYPE_TEXT) con valor final."""
        if not self._field_session_active:
            return
        dirty = self._field_session_dirty
        fc = self._build_field_commit_result(prefer_uia=prefer_uia)
        self._clear_field_session_state()
        if fc is None:
            return
        if self._should_emit_field_synth_step(fc, dirty):
            try:
                self._prepend_trace_events.append(
                    build_keyboard_type_text_event(fc=fc)
                )
            except Exception as e:
                log.debug(f"field commit synthetic event: {e}")
        if fc.final_text:
            self._publish_field_committed(
                fc.label, fc.final_text,
                uia_hint=fc.anchor_uia or {},
                web_hint=fc.anchor_web or {},
            )

    def _update_field_session(self, step: RawEvent) -> None:
        """Logica principal de sesionización en vivo."""
        et = step.event_type
        ka = step.keyboard_action

        # 1) Click → cualquier sesión abierta se cierra (focus-out).
        if et in (EventType.MOUSE_CLICK, EventType.MOUSE_DOUBLE_CLICK,
                  EventType.MOUSE_RIGHT_CLICK):
            self._close_field_session(prefer_uia=True)
            uia = (step.metadata or {}).get("uia") or {}
            ctype = (uia.get("control_type") or "").lower()
            if ctype in {"editcontrol", "edit", "documentcontrol",
                          "passwordcontrol", "comboboxcontrol"}:
                # Click sobre un campo → abrimos sesión inmediatamente
                self._open_field_session(step)
            else:
                # Click normal → narración humana centralizada
                quality = self._quality_from_step(step)
                try:
                    fb = build_recording_feedback(
                        raw_event=step,
                        target_signature=(step.metadata or {}).get(
                            "target_signature"
                        ),
                        quality=quality,
                    )
                    self._publish_live(
                        fb.label_es,
                        icon=fb.icon or "🖱",
                        quality=fb.quality_level or quality,
                    )
                except Exception as e:
                    log.debug(f"build_recording_feedback (click) failed: {e}")
                    tname = uia.get("name") or ""
                    if et == EventType.MOUSE_CLICK:
                        self._publish_live(
                            f"🖱️ Clic en '{tname}'" if tname else "🖱️ Clic",
                            quality=quality,
                        )
                    elif et == EventType.MOUSE_DOUBLE_CLICK:
                        self._publish_live("🖱️🖱️ Doble clic", quality=quality)
                    else:
                        self._publish_live("🖱️▸ Clic derecho", quality=quality)
            return

        if et == EventType.KEYBOARD_KEY_PRESS and ka:
            keys = ka.keys or []
            mods = ka.modifiers or []
            if not keys:
                return
            k = (keys[0] or "").lower().strip()
            non_shift_mods = [m for m in mods if m != "shift"]

            # 2) Hotkey real (ctrl/alt/win):
            # - Ctrl+V (paste), Ctrl+X (cut), Ctrl+A (select all),
            #   Ctrl+Z/Y (undo/redo), Ctrl+Backspace (palabra atrás)
            #   son ediciones DENTRO del campo. NO cierran sesión:
            #   solo refrescamos el snapshot del valor real para
            #   mantener last_known_value alineado con el campo.
            INLINE_EDIT_HOTKEYS = {
                ("ctrl",): {"v", "x", "a", "z", "y", "backspace"},
            }
            mods_tuple = tuple(non_shift_mods)
            inline = (
                self._field_session_active
                and mods_tuple in INLINE_EDIT_HOTKEYS
                and k in INLINE_EDIT_HOTKEYS[mods_tuple]
            )
            if inline:
                self._refresh_field_value_cache()
                self._field_session_dirty = True
                self._suppress_current_event_trace = True
                return

            if non_shift_mods:
                self._close_field_session(prefer_uia=True)
                combo = "+".join([m.capitalize() for m in mods] + [k.upper()])
                self._publish_live(f"⌨️ {combo}")
                return

            # CapsLock / NumLock / ScrollLock: NUNCA cierran sesión
            # ni emiten paso. Son toggles que solo modifican casing
            # de las teclas siguientes (ya manejado por el listener).
            # Bug 2026-05-03: aparecía "Presionar CAPSLOCK" como paso
            # entre fragmentos de texto.
            if k in self._ABSORBABLE_KEYS:
                self._suppress_current_event_trace = True
                return

            # Correcciones dentro del campo (antes de Tab/Enter/etc.)
            if k == "backspace":
                if self._field_session_active and self._field_session_buf:
                    self._field_session_buf.pop()
                    self._field_session_dirty = True
                if self._field_session_active:
                    self._refresh_field_value_cache()
                    self._field_session_dirty = True
                self._suppress_current_event_trace = True
                return

            if k == "delete":
                if self._field_session_active:
                    self._refresh_field_value_cache()
                    self._field_session_dirty = True
                self._suppress_current_event_trace = True
                return

            # 3) Tecla especial → puede cerrar sesión.
            if k in self._CLOSING_SPECIAL_KEYS or (
                len(k) > 1 and k not in {"space", "backspace", "delete"}
            ):
                self._close_field_session(prefer_uia=True)
                pretty = {
                    "tab": "Navegar al siguiente campo (Tab)",
                    "enter": "Confirmar (Enter)",
                    "esc": "Cancelar (Esc)", "escape": "Cancelar (Esc)",
                }.get(k, f"Presionar {k.upper()}")
                self._publish_live(f"⌨️ {pretty}")
                return

            # 4) Carácter imprimible: si no había sesión, abrirla.
            if k == "space":
                ch = " "
            elif len(k) == 1:
                ch = (keys[0]).upper() if "shift" in mods else keys[0]
            else:
                return
            if not self._field_session_active:
                self._open_field_session(step)
            self._field_session_buf.append(ch)
            self._field_session_dirty = True
            # Snapshot transaccional del valor real tras CADA tecla.
            self._refresh_field_value_cache()
            self._suppress_current_event_trace = True
            return

        # 5) Cualquier otro evento (drag/scroll) → cerrar sesión.
        self._close_field_session(prefer_uia=True)

    def _append_single_raw_event(self, step: RawEvent) -> None:
        """Adjunta ``step`` al trace (`self._recording` debe estar activo)."""
        elapsed = time.time() - self._start_time - self._paused_duration
        step.offset_ms = int(elapsed * 1000)
        if self.mission.raw_trace:
            prev = self.mission.raw_trace[-1]
            prev.delay_until_next_ms = step.offset_ms - prev.offset_ms
        if self._pending_annotations:
            for ann in self._pending_annotations:
                ann.target_step_id = step.id
                self.mission.user_annotations.append(ann)
            self._pending_annotations.clear()
        if self.mission and self.mission.id:
            try:
                from app.services.missions.asset_finalizer import (
                    promote_raw_event_snapshots,
                )
                promote_raw_event_snapshots(step, str(self.mission.id))
            except Exception as e:
                log.error(f"Failed to promote snapshot assets: {e}")

        if not self.mission:
            return
        self.mission.raw_trace.append(step)
        try:
            meta = dict(step.metadata or {})
            from app.services.missions.freeze_first_execution_core import (
                apply_ffec_after_raw_event,
            )
            coi = apply_ffec_after_raw_event(self.mission, step.id, meta)
            if coi is not None:
                step.metadata = meta
        except Exception as _ffec_e:
            log.debug(f"ffec promote after raw event: {_ffec_e}")
        try:
            from app.services.missions import recorder_semantic_shadow as _rss
            if _rss.is_shadow_enabled(settings) and _rss.mission_shadow_active(
                self.mission,
            ):
                _rss.emit_for_raw_event(self.mission, step)
        except Exception as _e:
            log.debug(f"semantic_shadow emit: {_e}")
        try:
            from app.services.missions.semantic_session_engine import (
                observe_after_record_step as _sess_observe,
            )
            _sess_observe(self.mission, step)
        except Exception as _e:
            log.debug(f"semantic_session observe: {_e}")
        self._last_step_time = time.time()
        self._last_step_id = step.id
        log.debug(
            f"Recorded step: {step.event_type} (UIA: {'Yes' if step.metadata.get('uia') else 'No'})"
        )

    def _patch_post_action(
        self, event_id: str, post_state: PostActionState,
    ) -> None:
        """Callback invocado por ``PostActionScheduler`` cuando el
        post-action capture termina. Escribe ``post_action_state`` y
        recompone ``capture_contract`` del RawEvent ya emitido.

        PRD 2026-05-10h: este patch corre EN OTRO HILO. Tomamos el
        lock para no chocar con ``_on_event``. Si la misión cerró o el
        evento desapareció (raro), no hace nada.
        """
        if post_state is None:
            return
        with self._lock:
            if not self.mission:
                return
            target = None
            for ev in self.mission.raw_trace:
                if ev.id == event_id:
                    target = ev
                    break
            if target is None:
                return
            meta = dict(target.metadata or {})
            meta["post_action_state"] = post_state.to_metadata()
            # Recomputar capture_contract con post_state (FFEC: no invalidar PRE).
            try:
                from app.services.missions.freeze_first_execution_core import (
                    mark_transition_observed,
                    patch_capture_contract_after_post_action,
                )
                contract = dict(meta.get("capture_contract") or {})
                contract["has_post_state"] = True
                contract = patch_capture_contract_after_post_action(contract, meta)
                if meta.get("canonical_operational_intent"):
                    mark_transition_observed(meta, observed=True)
                else:
                    missing = [
                        k.replace("has_", "")
                        for k in (
                            "has_pre_snapshot", "has_post_state", "has_uia",
                            "has_parent_chain", "has_ocr_or_text",
                        )
                        if not contract.get(k)
                    ]
                    contract["missing"] = missing
                    contract["capture_complete"] = not missing
                meta["capture_contract"] = contract
            except Exception:
                pass
            target.metadata = meta

    def _on_event(self, step: RawEvent):
        with self._lock:
            if not self._recording or self._paused or not self.mission:
                return

            self._suppress_current_event_trace = False
            self._update_field_session(step)

            staged = list(self._prepend_trace_events)
            self._prepend_trace_events.clear()
            chunks: List[RawEvent] = staged + ([] if self._suppress_current_event_trace else [step])
            if not chunks:
                return

            for blk in chunks:
                self._append_single_raw_event(blk)


# Globals
_current_recorder: Optional[MissionRecorder] = None
_recorder_lock = threading.Lock()

def get_recorder() -> Optional[MissionRecorder]:
    with _recorder_lock: return _current_recorder


def _dispose_stale_recorder_unsafe() -> None:
    """Libera un MissionRecorder colgado (listeners vivos, _recording False).

    Solo con ``_recorder_lock`` sostenido. Limpia el global.
    """
    global _current_recorder
    r = _current_recorder
    if r is None:
        return
    _current_recorder = None
    try:
        with r._lock:  # noqa: SLF001
            r._recording = False  # noqa: SLF001
    except Exception:
        pass
    try:
        r.release_input_listeners()
    except Exception as e:
        log.warning(f"dispose_stale release_input_listeners: {e}")


def start_recording(name: str = "") -> Mission:
    with _recorder_lock:
        global _current_recorder
        if _current_recorder and _current_recorder._recording:
            raise RuntimeError("Active recording")
        # Referencia colgada (p. ej. stop() falló a medias o excepción rara)
        if _current_recorder is not None and not _current_recorder._recording:
            _dispose_stale_recorder_unsafe()

        # ── Candado global: nunca grabar mientras un player ejecuta ──
        # Sin este check, el scheduler podía disparar una misión
        # programada justo cuando el usuario empezaba a grabar; eso
        # generaba "ventanas de Chrome saliendo solas" y la grabación
        # capturaba inputs sintéticos del player.
        try:
            from app.services.missions.runtime_locks import (
                locks as _runtime_locks,
                publish_blocked_event,
            )
            if not _runtime_locks.acquire_recorder():
                snap = _runtime_locks.snapshot()
                if snap.player_active:
                    msg = (
                        "No se puede grabar: hay una automatización "
                        "ejecutándose en este momento. Espera a que "
                        "termine o detenla y vuelve a intentar."
                    )
                    publish_blocked_event(
                        kind="recorder",
                        reason="player_busy",
                        message=msg,
                    )
                else:
                    msg = "Ya hay una grabación activa."
                    publish_blocked_event(
                        kind="recorder",
                        reason="recorder_busy",
                        message=msg,
                    )
                raise RuntimeError(msg)
        except RuntimeError:
            raise
        except Exception:
            # Si runtime_locks no está disponible (entornos antiguos
            # sin migración), seguimos con el comportamiento previo.
            pass

        new_r = MissionRecorder(name)
        _current_recorder = new_r
    try:
        return new_r.start()
    except Exception:
        with _recorder_lock:
            if _current_recorder is new_r:
                _current_recorder = None
        try:
            new_r.release_input_listeners()
        except Exception:
            pass
        # Liberamos el candado del recorder si la inicialización falló.
        try:
            from app.services.missions.runtime_locks import locks as _runtime_locks
            _runtime_locks.release_recorder()
        except Exception:
            pass
        raise

def is_recording_active() -> bool:
    """Consulta segura del estado de grabación."""
    return bool(_current_recorder and _current_recorder._recording)

def stop_recording() -> Mission:
    """Detiene y devuelve la misión, limpia el global sin pisar otra sesión
    que hubiera empezado en otro hilo.
    """
    with _recorder_lock:
        global _current_recorder
        if not _current_recorder:
            raise RuntimeError("No recording")
        r = _current_recorder
    m: Optional[Mission] = None
    try:
        m = r.stop()
    except Exception as e:
        log.error(f"stop_recording: stop() falló: {e}")
        m = r.mission
        try:
            r.release_input_listeners()
        except Exception as e2:
            log.debug(f"stop_recording: release post-falla: {e2}")
    finally:
        with _recorder_lock:
            # Solo quitar r si todavía es el activo; si entre medias arrancó
            # otra grabación, no tocar su puntero.
            if _current_recorder is r:
                _current_recorder = None
        # Liberar siempre el candado global del grabador para que el
        # scheduler / player puedan retomar el control. Hacerlo fuera del
        # ``with _recorder_lock`` evita reentrar bajo el mismo lock.
        try:
            from app.services.missions.runtime_locks import locks as _runtime_locks
            _runtime_locks.release_recorder()
        except Exception:
            pass
    if m is None:
        raise RuntimeError("No se pudo detener la grabación correctamente")
    return m

def stop_and_compile() -> Mission:
    """
    Pipeline unificado canónico: stop → compile → save → publish review.
    TODOS los caminos (voz, botón, tool_call) deben usar esta función.
    """
    from app.services.missions.compiler import MissionCompiler
    from app.services.missions.store import mission_store
    
    mission = stop_recording()
    mission = MissionCompiler.compile_mission(mission)
    mission_store.save(mission)
    
    # Publicar evento para abrir UI de revisión
    try:
        from app.runtime.bus import bus
        from app.contracts.events import SystemEvent
        bus.publish(SystemEvent(name="mission.review_requested", payload={"mission_id": mission.id}))
    except Exception as e:
        log.error(f"Error publicando mission.review_requested: {e}")
    
    return mission

def pause_recording():
    with _recorder_lock:
        if _current_recorder: _current_recorder.pause()

def resume_recording():
    with _recorder_lock:
        if _current_recorder: _current_recorder.resume()


# ============================================================================
# StepFragmentRecorder — Micro-grabador para parchear un paso individual
# ============================================================================

class StepFragmentRecorder:
    """
    Fragmento para regrabar pasos individuales.
    Delega en ``MissionRecorder`` para **Field Editing Session** idéntica
    al grabador principal (valor final compacto).
    """

    FRAG_MARKER = "__nevlan_step_fragment_v1__"

    def __init__(self):
        self._lock = threading.Lock()
        self._recording = False
        self._delegated_mr: Optional[MissionRecorder] = None
        self.mouse_listener: Optional[MouseListener] = None
        self.keyboard_listener: Optional[KeyboardListener] = None

    def start(self):
        with self._lock:
            if self._recording:
                raise RuntimeError("Fragment already recording")
            self._recording = True
            self._delegated_mr = MissionRecorder(self.FRAG_MARKER)
            m = self._delegated_mr.start()
            frag_trace: List[RawEvent] = []
            m.raw_trace = frag_trace
            self.mouse_listener = self._delegated_mr.mouse_listener
            self.keyboard_listener = self._delegated_mr.keyboard_listener
            log.info("StepFragmentRecorder started")

    def stop(self) -> List[RawEvent]:
        mr: Optional[MissionRecorder] = None
        with self._lock:
            if not self._recording or self._delegated_mr is None:
                raise RuntimeError("Fragment not recording")
            mr = self._delegated_mr
            self._recording = False
            self._delegated_mr = None
            self.mouse_listener = None
            self.keyboard_listener = None
        mission = mr.stop()
        out = list(mission.raw_trace)
        log.info(f"StepFragmentRecorder stopped: {len(out)} eventos")
        return out

    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def get_event_count(self) -> int:
        mr = self._delegated_mr
        if mr and mr.mission and mr.mission.raw_trace is not None:
            return len(mr.mission.raw_trace)
        return 0

    def get_events(self) -> List[RawEvent]:
        mr = self._delegated_mr
        if mr and mr.mission and mr.mission.raw_trace is not None:
            return list(mr.mission.raw_trace)
        return []


def compile_fragment(raw_events: List[RawEvent]):
    """
    Compila una lista de RawEvents en CompiledSteps + InterpretedSteps.
    Reutiliza MissionCompiler sobre una misión temporal.
    Retorna (compiled_steps, interpreted_steps).
    """
    from app.services.missions.compiler import MissionCompiler
    
    temp_mission = Mission(name="_fragment_temp", status=MissionStatus.RECORDING)
    temp_mission.raw_trace = raw_events
    temp_mission = MissionCompiler.compile_mission(temp_mission)
    
    return temp_mission.compiled_execution_graph, temp_mission.interpreted_steps
