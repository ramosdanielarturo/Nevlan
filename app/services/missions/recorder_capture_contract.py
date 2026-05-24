"""
Nevlan — Recorder Capture Contract (PRD 2026-05-10h)
=====================================================

Hasta hoy el recorder podía emitir un ``mouse_click`` ciego: sin
screenshot pre-acción, sin OCR sobre la región, sin parent_chain del
UIA, y sin observar el estado posterior. Cuando el UIA solo entregaba
un ``GroupControl`` enorme y vacío (Chrome profile picker, app SAP
custom, sistema Citrix, web SPA con divs gigantes), TODOS los layers
de inteligencia que vinieron después recibían vacíos honestos:

    visual.primary_text = ""   ⇒ Screen Element Reader vacío
    ocr_text_around = ""       ⇒ Recorder Truth Layer "insufficient"
    parent_chain = None        ⇒ rescate por padre imposible
    after_state = None         ⇒ Outcome Intelligence sin señal
    profile_name = ""          ⇒ Mission Truth Gate ⇒ NEEDS_REVIEW

Este módulo establece el **Recorder Capture Contract**: cada
``mouse_click`` debe llevar evidencia mínima — o declarar
honestamente qué le falta. La regla de producto es:

    Si el humano lo ve, el recorder debe guardar evidencia de que
    estaba ahí.

Componentes (todos pure-python, sin Qt, sin LLM, sin reglas por app):

  * :class:`PreActionSnapshot`        — frame capturado ANTES del click.
  * :class:`PostActionState`          — estado observado DESPUÉS del click.
  * :class:`ScreenshotRingBuffer`     — buffer corto (5 frames @ 200 ms).
  * :class:`PostActionScheduler`      — captura diferida del after_state
                                        que sigue funcionando aunque la
                                        grabación se detenga.
  * :func:`is_uia_weak`               — heurística general: GroupControl
                                        vacío / bbox enorme / quality low.
  * :func:`maybe_run_ocr`             — OCR "on demand" sobre la región
                                        expandida del pre-snapshot SI el
                                        UIA es débil. No corre OCR si UIA
                                        es bueno (eficiencia).
  * :func:`capture_uia_parent_chain`  — sube hasta 4 niveles + siblings.
  * :func:`build_capture_contract`    — declara has_pre/has_post/has_uia/
                                        has_parent_chain/has_ocr y
                                        ``capture_complete`` honesto **y**, en paralelo, el flag
    ``sufficient_for_execution``: evidencia suficiente para ejecutar sin
    exigir el paquete de 5/5 campos "perfect_capture".

Nada de esto razona sobre intención semántica. Solo garantiza que el
``RawEvent`` que sale del recorder lleve la evidencia bruta — para que
las capas de inteligencia (Truth Layer, Screen Element Reader, Outcome
Intelligence) tengan algo que leer en lugar de campos ``None``.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple


# ──────────────────────────────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────────────────────────────

#: Frames que el ring buffer mantiene activos. PRD 2026-05-12:
#: reducido a 2 frames. Solo el más reciente se usa como pre_snapshot
#: canónico; el penúltimo queda como respaldo si el reloj es ruidoso.
DEFAULT_BUFFER_CAPACITY: int = 2

#: Período entre capturas del buffer. PRD 2026-05-12: bajado a 100 ms
#: para que el pre_snapshot sea como mucho ~100 ms anterior al click.
#: Trade-off: 10 fps full-screen vía PIL.ImageGrab — más CPU que el
#: legacy de 5 fps, pero la freshness garantiza que el frame represente
#: lo que el humano vio sin sesgo de paint.
DEFAULT_BUFFER_PERIOD_MS: int = 100

#: Edad máxima aceptable de un pre-snapshot relativo al click. PRD
#: 2026-05-12: bajado a 600 ms para que ningún frame "viejo" se cuele
#: como evidencia pre-click si el buffer se quedó atrás (CPU saturada,
#: thread pausado). Si excede, devolvemos None y el contract lo
#: reporta como ``has_pre_snapshot=False`` honestamente.
DEFAULT_PRE_SNAPSHOT_MAX_AGE_MS: int = 600

#: Latencias progresivas para capturar el after_state. La idea es:
#:   * 300 ms cubre interacciones rápidas (clic en botón, foco UIA).
#:   * 800 ms cubre apps de escritorio que renderizan tras layout.
#:   * 1500 ms cubre web con carga ligera.
#: Si ninguna detecta cambio, igual reportamos el estado actual a 1500.
DEFAULT_POST_DELAYS_MS: Tuple[int, ...] = (300, 800, 1500)

#: Espera máxima para flush del scheduler en stop(). 2 s aguanta el
#: peor caso (1.5 s de delay + ~500 ms de captura).
DEFAULT_POST_WAIT_MS: int = 2000

#: Fases formales de captura de anchor (FASE 1 / Pre-Click Capture).
CAPTURE_PHASE_PRE_CLICK: str = "pre_click"
CAPTURE_PHASE_POST_ACTION: str = "post_action"

#: Telemetría cuando no hubo pending mousedown → no inventar identidad.
TARGET_PRECAPTURE_MISSING: str = "TARGET_PRECAPTURE_MISSING"

#: Fuentes conocidas de anchor.
ANCHOR_SOURCE_PYNPUT_MOUSEDOWN: str = "pynput/mousedown"
ANCHOR_SOURCE_PYNPUT_POST_CAPTURE: str = "pynput/post_capture"
ANCHOR_SOURCE_INTENT_LAYER: str = "intent_layer/mousedown"

#: Tamaños progresivos (lado en píxeles) para crops de OCR alrededor
#: del click cuando UIA es débil. Pequeño → mediano → grande.
DEFAULT_OCR_REGION_STEPS_PX: Tuple[int, ...] = (240, 420, 640)


# ──────────────────────────────────────────────────────────────────────
# Datos
# ──────────────────────────────────────────────────────────────────────


@dataclass
class PreActionSnapshot:
    """Frame capturado por el ring buffer ANTES del click."""

    full_path: Optional[str]
    captured_at_ms: int
    age_ms: int

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "pre_action_snapshot_full": self.full_path,
            "pre_action_captured_at_ms": int(self.captured_at_ms),
            "pre_action_age_ms": int(self.age_ms),
        }


@dataclass
class TargetAnchor:
    """Snapshot del estado de la ventana / app / URL en un instante.

    Sirve para detectar si el estado del sistema cambió ENTRE el click
    y la captura UIA/web. Si cambió, esa captura ya no puede usarse
    como ``target_identity`` — pertenece al ``post_action_state``.

    Regla (FASE 1): ``target_identity`` solo puede leerse de un anchor
    con ``phase == pre_click``. Anchors ``post_action`` son debug/outcome.
    """

    hwnd: Optional[int] = None
    title: str = ""
    process: str = ""
    url: str = ""
    captured_at_ms: int = 0
    phase: str = ""
    ts_monotonic_ns: int = 0
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": str(self.title or ""),
            "process": str(self.process or ""),
            "url": str(self.url or ""),
            "captured_at_ms": int(self.captured_at_ms),
            "phase": str(self.phase or ""),
            "ts_monotonic_ns": int(self.ts_monotonic_ns or 0),
            "source": str(self.source or ""),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["TargetAnchor"]:
        if not isinstance(data, dict):
            return None
        return cls(
            hwnd=data.get("hwnd"),
            title=str(data.get("title") or ""),
            process=str(data.get("process") or ""),
            url=str(data.get("url") or ""),
            captured_at_ms=int(data.get("captured_at_ms") or 0),
            phase=str(data.get("phase") or ""),
            ts_monotonic_ns=int(data.get("ts_monotonic_ns") or 0),
            source=str(data.get("source") or ""),
        )


@dataclass
class PostActionState:
    """Estado observado tras el click en una latencia tope."""

    captured_at_ms: int
    latency_ms: int
    window_hwnd: Optional[int]
    window_title: str
    window_process: str
    url: str
    snapshot_full: Optional[str] = None
    detected_change: bool = False

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "captured_at_ms": int(self.captured_at_ms),
            "latency_ms": int(self.latency_ms),
            "window_hwnd": self.window_hwnd,
            "window_title": str(self.window_title or ""),
            "window_process": str(self.window_process or ""),
            "url": str(self.url or ""),
            "snapshot_full": self.snapshot_full,
            "detected_change": bool(self.detected_change),
        }


# ──────────────────────────────────────────────────────────────────────
# Ring buffer
# ──────────────────────────────────────────────────────────────────────


class ScreenshotRingBuffer:
    """Buffer corto de screenshots full-screen.

    Está diseñado para ser **inyectable**: el productor (PIL/ImageGrab
    en runtime, mock en tests) se pasa como ``grabber``. Si el grabber
    no está disponible o falla, el buffer simplemente no llena: nadie
    se cae, y ``get_before`` devolverá ``None`` — lo que hará que el
    capture_contract reporte ``has_pre_snapshot=False`` honestamente.
    """

    def __init__(
        self,
        assets_dir: Path,
        *,
        capacity: int = DEFAULT_BUFFER_CAPACITY,
        period_ms: int = DEFAULT_BUFFER_PERIOD_MS,
        grabber: Optional[Callable[[Path], Optional[str]]] = None,
        clock_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        self._dir = Path(assets_dir)
        self._capacity = max(1, int(capacity))
        self._period_ms = max(50, int(period_ms))
        self._grabber = grabber or _default_pil_grabber
        self._clock_ms = clock_ms or _wall_ms
        self._frames: Deque[Tuple[int, str]] = deque(maxlen=self._capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="nevlan-pre-buffer", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=1.0)
        self._thread = None

    def push(self, path: str, ts_ms: int) -> None:
        """Inyecta un frame manualmente (usado por tests)."""
        with self._lock:
            self._frames.append((int(ts_ms), str(path)))

    def get_before(
        self,
        ts_ms: int,
        *,
        max_age_ms: int = DEFAULT_PRE_SNAPSHOT_MAX_AGE_MS,
    ) -> Optional[PreActionSnapshot]:
        """Frame más reciente capturado **antes** de ``ts_ms``.

        Devuelve ``None`` si:
          * el buffer está vacío, o
          * el frame más reciente es más viejo que ``max_age_ms``, o
          * todos los frames son posteriores al click (anomalía de reloj).
        """
        with self._lock:
            best: Optional[Tuple[int, str]] = None
            for fts, path in reversed(self._frames):
                if fts <= ts_ms:
                    best = (fts, path)
                    break
        if best is None:
            return None
        fts, path = best
        age = int(ts_ms) - int(fts)
        if age < 0 or age > int(max_age_ms):
            return None
        return PreActionSnapshot(
            full_path=path, captured_at_ms=int(fts), age_ms=int(age),
        )

    # ──────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                frame_path = self._grabber(self._dir)
            except Exception:
                frame_path = None
            if frame_path:
                self.push(frame_path, self._clock_ms())
            # Sleep en pasos cortos para responder rápido a stop().
            slept = 0
            step = 50
            while slept < self._period_ms and not self._stop.is_set():
                time.sleep(step / 1000.0)
                slept += step


def _wall_ms() -> int:
    return int(time.time() * 1000)


def _default_pil_grabber(assets_dir: Path) -> Optional[str]:
    """Captura full-screen vía PIL si está disponible. Best-effort."""
    try:
        from PIL import ImageGrab  # type: ignore
    except Exception:
        return None
    try:
        try:
            img = ImageGrab.grab(all_screens=True)
        except TypeError:
            img = ImageGrab.grab()
        path = assets_dir / f"prebuf_{uuid.uuid4().hex[:10]}.png"
        img.save(path)
        return str(path)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# Post-action scheduler
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _PendingPost:
    event_id: str
    thread: threading.Thread
    start_ms: int


class PostActionScheduler:
    """Agenda capturas diferidas del estado posterior a un click.

    Cada captura corre en su propio hilo daemon. ``stop()`` espera con
    timeout para que la última captura termine — así, aunque la
    grabación se detenga inmediatamente después del click, el last
    click obtiene su after_state.
    """

    def __init__(self) -> None:
        self._pending: List[_PendingPost] = []
        self._lock = threading.Lock()

    def schedule(
        self,
        *,
        event_id: str,
        click_ts_ms: int,
        delays_ms: Tuple[int, ...] = DEFAULT_POST_DELAYS_MS,
        before_state: Dict[str, Any],
        capture_state: Callable[[], Dict[str, Any]],
        capture_screenshot: Optional[Callable[[], Optional[str]]] = None,
        on_complete: Callable[[PostActionState], None] = lambda x: None,
        sleep_ms: Callable[[int], None] = lambda ms: time.sleep(ms / 1000.0),
        clock_ms: Callable[[], int] = _wall_ms,
    ) -> None:
        """Programa la captura diferida.

        ``capture_state`` debe devolver un dict con
        ``{"hwnd", "title", "process", "url"}`` (todos opcionales). Se
        invoca tras cada delay; en cuanto detectamos cambio respecto a
        ``before_state``, capturamos screenshot y emitimos
        ``on_complete(post_state)`` con ``detected_change=True``. Si
        ningún delay detecta cambio, emitimos al final el estado del
        último delay con ``detected_change=False``.
        """

        def _worker() -> None:
            last_state: Dict[str, Any] = {}
            last_latency = 0
            for delay in delays_ms:
                # Dormimos respecto al click_ts_ms, no acumulado, para
                # que las latencias reportadas sean homogéneas.
                target = int(click_ts_ms) + int(delay)
                now = clock_ms()
                wait = max(0, target - now)
                if wait:
                    sleep_ms(wait)
                last_latency = clock_ms() - int(click_ts_ms)
                try:
                    last_state = dict(capture_state() or {})
                except Exception:
                    last_state = {}
                if _state_differs(before_state, last_state):
                    snap = None
                    try:
                        if capture_screenshot is not None:
                            snap = capture_screenshot()
                    except Exception:
                        snap = None
                    post = PostActionState(
                        captured_at_ms=clock_ms(),
                        latency_ms=int(last_latency),
                        window_hwnd=last_state.get("hwnd"),
                        window_title=str(last_state.get("title") or ""),
                        window_process=str(last_state.get("process") or ""),
                        url=str(last_state.get("url") or ""),
                        snapshot_full=snap,
                        detected_change=True,
                    )
                    try:
                        on_complete(post)
                    except Exception:
                        pass
                    return
            # Sin cambio detectado: reportamos último estado.
            snap = None
            try:
                if capture_screenshot is not None:
                    snap = capture_screenshot()
            except Exception:
                snap = None
            post = PostActionState(
                captured_at_ms=clock_ms(),
                latency_ms=int(last_latency),
                window_hwnd=last_state.get("hwnd"),
                window_title=str(last_state.get("title") or ""),
                window_process=str(last_state.get("process") or ""),
                url=str(last_state.get("url") or ""),
                snapshot_full=snap,
                detected_change=False,
            )
            try:
                on_complete(post)
            except Exception:
                pass

        t = threading.Thread(
            target=_worker, name=f"nevlan-post-{event_id[:6]}", daemon=True,
        )
        with self._lock:
            self._pending.append(
                _PendingPost(event_id=event_id, thread=t, start_ms=clock_ms())
            )
        t.start()

    def wait_all(self, *, timeout_ms: int = DEFAULT_POST_WAIT_MS) -> None:
        """Bloquea hasta que todos los workers pendientes terminen, con
        un timeout total. Llamarlo desde ``MissionRecorder.stop()``.
        """
        deadline = _wall_ms() + int(timeout_ms)
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        for p in pending:
            remaining = max(0.0, (deadline - _wall_ms()) / 1000.0)
            if remaining <= 0:
                break
            try:
                p.thread.join(timeout=remaining)
            except Exception:
                pass


def _state_differs(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    if not after:
        return False
    for key in ("hwnd", "title", "url"):
        bv = before.get(key)
        av = after.get(key)
        if bv is None and av is None:
            continue
        if (bv or "") != (av or ""):
            return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Target ↔ Outcome isolation (PRD 2026-05-10i)
# ──────────────────────────────────────────────────────────────────────


def capture_target_anchor(
    window_ctx: Any,
    web_meta: Optional[Dict[str, Any]] = None,
    *,
    phase: str = CAPTURE_PHASE_POST_ACTION,
    source: str = "",
    clock_ms: Optional[Callable[[], int]] = None,
    monotonic_ns: Optional[int] = None,
) -> TargetAnchor:
    """Captura un anchor estructural del estado en un instante.

    FASE 1 — dos fases formales:

      * ``phase="pre_click"`` — mouse_down / antes de mutación UI (identidad).
      * ``phase="post_action"`` — tras captura UIA/web (outcome/debug).
    """
    clk = int((clock_ms or _wall_ms)())
    mono = int(monotonic_ns if monotonic_ns is not None else time.monotonic_ns())
    hwnd: Optional[int] = None
    title = ""
    proc = ""
    if window_ctx is not None:
        if isinstance(window_ctx, dict):
            hwnd = window_ctx.get("hwnd")
            title = str(window_ctx.get("title") or "")
            proc = str(window_ctx.get("process_name") or window_ctx.get("process") or "")
        else:
            hwnd = getattr(window_ctx, "hwnd", None)
            title = str(getattr(window_ctx, "title", "") or "")
            proc = str(getattr(window_ctx, "process_name", "") or "")
    url = ""
    if isinstance(web_meta, dict):
        url = str(web_meta.get("url") or "")
    return TargetAnchor(
        hwnd=hwnd,
        title=title,
        process=proc,
        url=url,
        captured_at_ms=clk,
        phase=str(phase or ""),
        ts_monotonic_ns=mono,
        source=str(source or ""),
    )


def is_identity_anchor(anchor: Optional[TargetAnchor]) -> bool:
    """True si el anchor puede usarse para ``target_identity``."""
    return (
        anchor is not None
        and str(anchor.phase or "") == CAPTURE_PHASE_PRE_CLICK
    )


def resolve_identity_anchor(
    before: Optional[TargetAnchor],
    after: Optional[TargetAnchor] = None,
) -> Optional[TargetAnchor]:
    """Devuelve el anchor autorizado para identidad (solo ``pre_click``)."""
    _ = after
    if is_identity_anchor(before):
        return before
    return None


def detect_state_change(
    before: Optional[TargetAnchor],
    after: Optional[TargetAnchor],
) -> Dict[str, bool]:
    """Compara ``before`` con ``after`` y reporta qué cambió.

    Devuelve un dict con flags estructurales (no nombres por app):

      * ``title_changed`` — la ventana cambió de title.
      * ``hwnd_changed`` — la ventana foco cambió de handle.
      * ``app_changed``   — el process_name cambió (cambió la app).
      * ``url_changed``   — la URL del frame web cambió.
      * ``any_changed``   — al menos uno de los anteriores.

    Si cualquiera de los anchors es ``None``, devuelve todo ``False``
    (no podemos asegurar nada).
    """
    flags = {
        "title_changed": False,
        "hwnd_changed": False,
        "app_changed": False,
        "url_changed": False,
        "any_changed": False,
    }
    if before is None or after is None:
        return flags
    if before.hwnd is not None and after.hwnd is not None \
            and before.hwnd != after.hwnd:
        flags["hwnd_changed"] = True
    a_title = (before.title or "").strip()
    b_title = (after.title or "").strip()
    if a_title and b_title and a_title != b_title:
        flags["title_changed"] = True
    a_proc = (before.process or "").strip().lower()
    b_proc = (after.process or "").strip().lower()
    if a_proc and b_proc and a_proc != b_proc:
        flags["app_changed"] = True
    a_url = (before.url or "").strip()
    b_url = (after.url or "").strip()
    if a_url and b_url and a_url != b_url:
        flags["url_changed"] = True
    flags["any_changed"] = any(
        v for k, v in flags.items() if k != "any_changed"
    )
    return flags


# Control types that anchor identity even sin huella visual fuerte (shell/list).
_STABLE_PRE_ACTION_CT: frozenset = frozenset({
    "buttoncontrol",
    "splitbuttoncontrol",
    "editcontrol",
    "comboboxcontrol",
    "checkboxcontrol",
    "radiobuttoncontrol",
    "hyperlinkcontrol",
    "listitemcontrol",
    "menuitemcontrol",
    "tabitemcontrol",
    "treeitemcontrol",
    "thumbcontrol",
    "toolbarbuttoncontrol",
})


def _bbox_pathological_for_trust(b: Optional[Dict[str, Any]]) -> bool:
    """Réplica mínima de la regla §pathological del semantic_fusion."""
    if not b:
        return True
    try:
        left = int(b.get("left") or 0)
        top = int(b.get("top") or 0)
        w = int(b.get("width") or 0)
        h = int(b.get("height") or 0)
    except Exception:
        return True
    if w <= 0 or h <= 0:
        return True
    if top < -8192 or left < -8192:
        return True
    if w > 12000 or h > 12000:
        return True
    return False


def _bbox_area(b: Optional[Dict[str, Any]]) -> int:
    if not b:
        return 0
    try:
        return max(0, int(b.get("width") or 0)) * max(
            0, int(b.get("height") or 0)
        )
    except Exception:
        return 0


def _primary_screen_area(metadata: Dict[str, Any]) -> int:
    ts = metadata.get("target_signature") or {}
    if not isinstance(ts, dict):
        return 0
    env = ts.get("environment") or {}
    if not isinstance(env, dict):
        return 0
    scr = env.get("primary_screen_px") or {}
    if not isinstance(scr, dict):
        return 0
    try:
        return max(0, int(scr.get("w") or 0)) * max(0, int(scr.get("h") or 0))
    except Exception:
        return 0


def _click_inside_bbox(
    bbox: Dict[str, Any],
    click_xy: Optional[Tuple[int, int]],
) -> bool:
    if click_xy is None:
        return False
    try:
        cx, cy = int(click_xy[0]), int(click_xy[1])
        left = int(bbox.get("left", 0))
        top = int(bbox.get("top", 0))
        w = int(bbox.get("width", 0))
        h = int(bbox.get("height", 0))
    except Exception:
        return False
    if w <= 0 or h <= 0:
        return False
    return left <= cx <= left + w and top <= cy <= top + h


def _stable_automation_id_for_trust(aid: str) -> bool:
    if not aid:
        return False
    s = aid.strip().lower()
    if len(s) < 2 or s in (
        "0", "-", "_", ".", "*", "##", "##0", "##1",
    ) or s.startswith("generic"):
        return False
    if "_" in s and ("ytd_" in s or "react" in s or "web-" in s) \
            and len(s) >= 18:
        return False
    return True


def _parent_chain_supports_trust(parent_chain: Any) -> bool:
    if not isinstance(parent_chain, dict):
        return False
    for anc in parent_chain.get("ancestors") or []:
        if not isinstance(anc, dict):
            continue
        n = str(anc.get("name") or "").strip()
        if len(n) >= 2:
            return True
    for sib in parent_chain.get("siblings_near_click") or []:
        if not isinstance(sib, dict):
            continue
        n = str(sib.get("name") or "").strip()
        if len(n) >= 2:
            return True
    return False


def _visual_fingerprint_supports_trust(vfp: Any) -> bool:
    if not isinstance(vfp, dict) or not vfp.get("available"):
        return False
    q = str(vfp.get("quality") or "").lower()
    if q in ("strong", "medium"):
        return True
    try:
        if int(vfp.get("orb_keypoint_count") or 0) >= 8:
            return True
    except Exception:
        pass
    return False


def compute_trusted_pre_action_identity(
    metadata: Dict[str, Any],
    *,
    before: Optional[TargetAnchor],
    click_xy: Optional[Tuple[int, int]],
) -> Dict[str, Any]:
    """Evalúa si la evidencia PRE-click es suficientemente fuerte como para
    preservarla como identidad ejecutable aunque el estado posterior
    (hwnd/app/título) cambie — típico de lanzar otra app desde Search.

    Pure-function sobre ``metadata`` (no muta).
    """
    _ = before  # reservado para checks futuros de coherencia hwnd/app
    reasons: List[str] = []
    uia = metadata.get("uia")
    if not isinstance(uia, dict) or not uia:
        return {
            "trusted_pre_action_identity": False,
            "reasons": ["no_uia_dict"],
            "click_inside_bbox": False,
        }
    name = str(uia.get("name") or "").strip()
    aid = str(uia.get("automation_id") or "").strip()
    ct = str(uia.get("control_type") or "").strip().lower()
    if not name and not aid:
        return {
            "trusted_pre_action_identity": False,
            "reasons": ["empty_name_and_aid"],
            "click_inside_bbox": False,
        }
    if not ct:
        return {
            "trusted_pre_action_identity": False,
            "reasons": ["empty_control_type"],
            "click_inside_bbox": False,
        }

    bbox = uia.get("bbox") if isinstance(uia.get("bbox"), dict) else None
    anchor = metadata.get("anchor_bbox")
    if not isinstance(anchor, dict):
        anchor = {}
    ref_bbox = bbox if (bbox and not _bbox_pathological_for_trust(bbox)) else {}
    if not ref_bbox and isinstance(anchor, dict) and anchor \
            and not _bbox_pathological_for_trust(anchor):
        ref_bbox = anchor
    if not ref_bbox or _bbox_pathological_for_trust(ref_bbox):
        return {
            "trusted_pre_action_identity": False,
            "reasons": ["no_reasonable_bbox"],
            "click_inside_bbox": False,
        }

    inside = _click_inside_bbox(ref_bbox, click_xy)
    if not inside:
        return {
            "trusted_pre_action_identity": False,
            "reasons": ["click_outside_bbox"],
            "click_inside_bbox": False,
        }
    reasons.append("click_inside_bbox")

    screen_area = _primary_screen_area(metadata)
    ba = _bbox_area(ref_bbox)
    if screen_area > 0 and ba > int(screen_area * 0.38):
        return {
            "trusted_pre_action_identity": False,
            "reasons": ["bbox_too_large_vs_screen"],
            "click_inside_bbox": True,
        }

    chain = uia.get("parent_chain")
    fp_ok = _visual_fingerprint_supports_trust(
        metadata.get("visual_fingerprint"),
    )
    aid_ok = _stable_automation_id_for_trust(aid)
    chain_ok = _parent_chain_supports_trust(chain)
    ct_ok = ct in _STABLE_PRE_ACTION_CT

    if not (fp_ok or aid_ok or chain_ok or ct_ok):
        return {
            "trusted_pre_action_identity": False,
            "reasons": ["no_supporting_anchor_signal"],
            "click_inside_bbox": True,
        }
    if fp_ok:
        reasons.append("visual_fingerprint")
    if aid_ok:
        reasons.append("stable_automation_id")
    if chain_ok:
        reasons.append("parent_chain")
    if ct_ok:
        reasons.append("stable_control_type")

    reasons.insert(0, "trusted_pre_action_identity")
    return {
        "trusted_pre_action_identity": True,
        "reasons": reasons,
        "click_inside_bbox": True,
    }


def enforce_target_identity_isolation(
    metadata: Dict[str, Any],
    *,
    before: Optional[TargetAnchor],
    after: Optional[TargetAnchor],
    click_xy: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """Si el estado cambió entre el click y la captura UIA/web,
    **migra** las keys contaminadas a ``debug_after_state`` para que
    nunca lleguen a las funciones que arman ``target_identity``.

    Reglas (estructurales, no app-specific):

      * ``hwnd_changed`` / ``title_changed`` / ``app_changed`` ⇒ la
        UIA capturada referencia la VENTANA POSTERIOR. Movemos
        ``metadata["uia"]``, ``metadata["anchor_bbox"]`` y
        ``metadata["target_bbox"]`` a
        ``metadata["debug_after_state"]``. Si ``target_source`` era
        ``"uia"``, lo bajamos a ``"click_fallback"`` (más honesto).

      * **Trusted Pre-Action Identity** (PRD 2026-05-17): si la evidencia
        PRE-click es fuerte (UIA accionable + bbox coherente + click
        dentro + señal de apoyo), entonces un cambio de ventana/app
        **posterior** es tratado como ``successful_state_transition`` —
        la identidad oficial NO se invalida (la UIA no migra).

      * ``url_changed`` ⇒ la web capturada referencia la PÁGINA
        POSTERIOR. Movemos ``metadata["web"]`` a
        ``metadata["debug_after_state"]["web_after"]``. Si la
        ``url_changed`` ocurrió y NO hubo ``hwnd_changed``, las
        identidades UIA pueden seguir siendo válidas (mismo proceso,
        misma ventana — el redirect es DOM-level).

    El audit ``metadata["target_identity_isolation"]`` queda escrito
    siempre — incluso cuando no se migra nada — para que la auditoría
    pueda ver "se verificó la frontera target ↔ outcome".

    Retorna el mismo ``metadata`` (mutado).
    """
    identity_before = resolve_identity_anchor(before, after)
    flags = detect_state_change(before, after)
    contaminated_keys: List[str] = []
    after_block = dict(metadata.get("debug_after_state") or {})

    if before is not None and identity_before is None:
        degradation_reason = "identity_anchor_not_pre_click"
    else:
        degradation_reason = ""

    trust_rep = compute_trusted_pre_action_identity(
        metadata, before=identity_before, click_xy=click_xy,
    )
    trusted = bool(trust_rep.get("trusted_pre_action_identity"))
    window_dirty = (
        flags["hwnd_changed"] or flags["title_changed"] or flags["app_changed"]
    )
    successful_state_transition = bool(
        trusted and window_dirty and flags["any_changed"]
    )
    identity_preserved_after_transition = False

    # Cambio de ventana / título / app ⇒ UIA + bbox serían post-action
    # salvo que la identidad PRE-click sea explícitamente confiable.
    if window_dirty:
        if trusted:
            identity_preserved_after_transition = True
            if degradation_reason != "identity_anchor_not_pre_click":
                degradation_reason = ""
        else:
            if degradation_reason != "identity_anchor_not_pre_click":
                degradation_reason = (
                    "window_transition_without_trusted_pre_action_identity"
                )
            if metadata.get("uia"):
                after_block["uia_after"] = metadata.pop("uia")
                contaminated_keys.append("uia")
            if metadata.get("anchor_bbox"):
                after_block["anchor_bbox_after"] = metadata.pop("anchor_bbox")
                contaminated_keys.append("anchor_bbox")
            if metadata.get("target_bbox"):
                after_block["target_bbox_after"] = metadata.pop("target_bbox")
                contaminated_keys.append("target_bbox")
            ts = str(metadata.get("target_source") or "").strip().lower()
            if ts == "uia":
                metadata["target_source"] = "click_fallback"
                metadata["quality_level"] = "low"
                metadata.setdefault("needs_reinforcement", True)

    # Cambio de URL ⇒ web es post-action (aunque la ventana siga viva).
    if flags["url_changed"]:
        if metadata.get("web"):
            after_block["web_after"] = metadata.pop("web")
            if "web" not in contaminated_keys:
                contaminated_keys.append("web")

    if after_block:
        metadata["debug_after_state"] = after_block

    metadata["target_identity_isolation"] = {
        "before": before.to_dict() if before is not None else None,
        "after": after.to_dict() if after is not None else None,
        "identity_before": (
            resolve_identity_anchor(before, after).to_dict()
            if resolve_identity_anchor(before, after) is not None
            else None
        ),
        "state_change": flags,
        "moved_to_debug_after_state": contaminated_keys,
        "contaminated": bool(contaminated_keys),
        "trusted_pre_action_identity": trusted,
        "trusted_pre_action_reasons": list(trust_rep.get("reasons") or []),
        "successful_state_transition": successful_state_transition,
        "identity_preserved_after_transition": identity_preserved_after_transition,
        "degradation_reason": degradation_reason or None,
    }
    return metadata


# ──────────────────────────────────────────────────────────────────────
# Heurística "UIA débil" + parent_chain + OCR fallback
# ──────────────────────────────────────────────────────────────────────


_GENERIC_CT = {
    "groupcontrol", "panecontrol", "customcontrol",
    "documentcontrol", "windowcontrol", "toolbarcontrol",
    "statusbarcontrol",
}


def is_uia_weak(
    uia: Optional[Dict[str, Any]],
    *,
    target_source: Optional[str] = None,
    quality_level: Optional[str] = None,
    window_area: int = 0,
) -> bool:
    """¿El UIA capturado en este click es estructuralmente débil?

    Reglas estructurales (no app-specific):

      * sin name y control_type genérico (GroupControl, PaneControl…),
      * sin AutomationId rescate,
      * bbox que cubre >50 % del área de la ventana,
      * recorder marcó ``target_source=click_fallback``,
      * ``quality_level in {"low", "red"}``.

    Si CUALQUIERA de las anteriores se cumple ⇒ débil ⇒ vale la pena
    correr OCR sobre la región expandida.
    """
    ts = (target_source or "").lower()
    ql = (quality_level or "").lower()
    if ts == "click_fallback":
        return True
    if ql in ("low", "red", "insufficient"):
        return True
    if not isinstance(uia, dict) or not uia:
        return True
    name = str(uia.get("name") or "").strip()
    aid = str(uia.get("automation_id") or "").strip()
    ct = str(uia.get("control_type") or "").strip().lower()
    bbox = uia.get("bbox") or {}
    if not name and not aid and ct in _GENERIC_CT:
        return True
    if window_area > 0 and isinstance(bbox, dict):
        try:
            w = int(bbox.get("width") or 0)
            h = int(bbox.get("height") or 0)
            if w * h > 0.5 * window_area:
                return True
        except Exception:
            pass
    return False


def capture_uia_parent_chain(
    elem: Any,
    *,
    click_xy: Tuple[int, int],
    max_depth: int = 4,
    max_siblings: int = 6,
) -> Optional[Dict[str, Any]]:
    """Sube el árbol UIA hasta ``max_depth`` niveles, recogiendo nombre
    y control_type de cada antepasado, y los hermanos visibles más
    cercanos al ``click_xy``. ``elem`` es un control de
    ``uiautomation``; si la librería no está disponible o algo falla,
    devuelve ``None``.
    """
    if elem is None:
        return None
    try:
        ancestors: List[Dict[str, Any]] = []
        cursor = elem
        for _ in range(max_depth):
            try:
                parent = cursor.GetParentControl()
            except Exception:
                parent = None
            if parent is None:
                break
            try:
                ancestors.append({
                    "name": str(parent.Name or ""),
                    "control_type": str(parent.ControlTypeName or ""),
                    "automation_id": str(parent.AutomationId or ""),
                })
            except Exception:
                break
            cursor = parent

        siblings: List[Dict[str, Any]] = []
        try:
            top_parent = elem.GetParentControl()
        except Exception:
            top_parent = None
        if top_parent is not None:
            try:
                children = list(top_parent.GetChildren() or [])
            except Exception:
                children = []
            cx, cy = int(click_xy[0]), int(click_xy[1])
            scored: List[Tuple[float, Dict[str, Any]]] = []
            for ch in children:
                try:
                    r = ch.BoundingRectangle
                    nm = str(ch.Name or "").strip()
                    ct = str(ch.ControlTypeName or "")
                    if not nm:
                        continue
                    rx = (int(getattr(r, "left", 0))
                          + int(getattr(r, "right", 0))) // 2
                    ry = (int(getattr(r, "top", 0))
                          + int(getattr(r, "bottom", 0))) // 2
                    dist = ((rx - cx) ** 2 + (ry - cy) ** 2) ** 0.5
                    scored.append((dist, {"name": nm, "control_type": ct}))
                except Exception:
                    continue
            scored.sort(key=lambda t: t[0])
            siblings = [d for _, d in scored[:max_siblings]]

        return {
            "ancestors": ancestors,
            "siblings_near_click": siblings,
        }
    except Exception:
        return None


def maybe_run_ocr(
    *,
    pre_snapshot_full: Optional[str],
    click_xy: Tuple[int, int],
    region_steps_px: Tuple[int, ...] = DEFAULT_OCR_REGION_STEPS_PX,
    ocr_runner: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
) -> Optional[Dict[str, Any]]:
    """Ejecuta OCR sobre crops progresivos del ``pre_snapshot_full``
    centrados en ``click_xy``. Retorna ``None`` si:

      * el snapshot no existe,
      * pytesseract no está disponible y no se inyectó ``ocr_runner``,
      * todos los crops salieron vacíos.

    El resultado tiene la forma::

        {
          "ocr_lines": ["Daniel Chrome", "Daniel Arturo Ramos", ...],
          "ocr_text_around": "Daniel Chrome\\nDaniel Arturo Ramos",
          "ocr_region_bbox": {"left", "top", "width", "height"},
          "ocr_source": "pre_action_expanded_region",
        }

    El ``ocr_runner`` permite a tests inyectar un OCR fake. Su firma:
    ``runner(image_path: str, click_xy, region_bbox) -> Optional[Dict]``.
    """
    if not pre_snapshot_full:
        return None
    runner = ocr_runner or _default_pytesseract_runner
    cx, cy = int(click_xy[0]), int(click_xy[1])
    last_error: Optional[str] = None
    for size in region_steps_px:
        half = max(40, int(size) // 2)
        region = {
            "left": cx - half,
            "top": cy - half,
            "width": int(size),
            "height": int(size),
        }
        try:
            res = runner(pre_snapshot_full, (cx, cy), region)
        except Exception as e:
            last_error = str(e)
            continue
        if not res:
            continue
        lines: List[str] = [str(s).strip() for s in (res.get("lines") or [])
                            if str(s or "").strip()]
        if not lines:
            continue
        text = "\n".join(lines)
        return {
            "ocr_lines": lines,
            "ocr_text_around": text,
            "ocr_region_bbox": region,
            "ocr_source": "pre_action_expanded_region",
        }
    if last_error:
        return None
    return None


def _default_pytesseract_runner(
    image_path: str,
    click_xy: Tuple[int, int],
    region_bbox: Dict[str, int],
) -> Optional[Dict[str, Any]]:
    """Runner por defecto: pytesseract sobre crop. Best-effort."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        with Image.open(image_path) as im:
            left = max(0, int(region_bbox.get("left", 0)))
            top = max(0, int(region_bbox.get("top", 0)))
            right = left + int(region_bbox.get("width", 0))
            bottom = top + int(region_bbox.get("height", 0))
            cropped = im.crop((left, top, right, bottom))
            data = pytesseract.image_to_data(
                cropped, output_type=pytesseract.Output.DICT,
            )
        words = data.get("text") or []
        nums = data.get("line_num") or []
        line_map: Dict[int, List[str]] = {}
        for w, ln in zip(words, nums):
            ws = str(w or "").strip()
            if not ws:
                continue
            line_map.setdefault(int(ln), []).append(ws)
        lines = [" ".join(words) for _, words in sorted(line_map.items())]
        return {"lines": [ln for ln in lines if ln.strip()]}
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# Capture contract builder
# ──────────────────────────────────────────────────────────────────────


_CONTRACT_REQUIRED = (
    "pre_snapshot",
    "post_state",
    "uia",
    "parent_chain",
    "ocr_or_text",
)


def build_capture_contract(
    *,
    pre_snapshot: Optional[PreActionSnapshot],
    post_state: Optional[PostActionState],
    uia: Optional[Dict[str, Any]],
    parent_chain: Optional[Dict[str, Any]],
    ocr: Optional[Dict[str, Any]],
    target_source: Optional[str] = None,
    quality_level: Optional[str] = None,
    target_identity_isolation: Optional[Dict[str, Any]] = None,
    canonical_operational_intent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Declara honestamente qué evidencia llegó al RawEvent.

    ``has_uia`` exige UIA con AT LEAST name no vacío o automation_id —
    un GroupControl pelado no cuenta.

    ``has_ocr_or_text`` se cumple si:
      * llegó OCR no vacío, **o**
      * llegó UIA con texto / accessible name, **o**
      * llegó parent_chain con algún sibling con texto.

    ``capture_complete`` ⇔ no falta ninguno de los 5 campos requeridos.

    ``sufficient_for_execution`` (PRD producto 2026-05-17) — relajado:
    hay identidad UIA accionable **y** al menos una columna de apoyo
    (pre/post/OCR real/parent_chain) **o** un ``automation_id`` no
    trivial (el runtime UIA puede anclar sin las otras 4 piezas).
    No sustituye a ``capture_complete`` para auditoría completa; evita
    degradar agresivamente la truth layer cuando el grabador sí aportó
    señal ejecutable.

    Si ``target_identity_isolation`` declara identidad PRE-acción de
    confianza preservada tras cambio de ventana/app, se fuerza
    ``sufficient_for_execution=True`` (PRD 2026-05-17 Trusted Pre-Action).

  FFEC: si ``canonical_operational_intent.canonical_truth`` está activo,
    el contrato se evalúa con verdad PRE-acción — ``capture_complete`` y
    ``sufficient_for_execution`` quedan cerrados aunque falte post_state
    (YouTube reemplazó la UI original).
    """
    coi_raw = dict(canonical_operational_intent or {})
    if coi_raw.get("canonical_truth") is True:
        ent = coi_raw.get("entity") or {}
        name = str((ent.get("display_name") if isinstance(ent, dict) else "") or "").strip()
        out_ffec: Dict[str, Any] = {
            "has_pre_snapshot": bool(pre_snapshot and pre_snapshot.full_path),
            "has_post_state": bool(post_state),
            "has_uia": bool(name) or bool(uia),
            "has_parent_chain": bool(parent_chain),
            "has_ocr_or_text": bool(ocr) or bool(name),
            "trusted_pre_action_identity": True,
            "sufficient_for_execution": True,
            "capture_complete": True,
            "canonical_truth": True,
            "ffec_pre_action_truth": True,
            "missing": [],
            "execution_missing": [],
            "identity_preserved_after_transition": True,
            "successful_state_transition": True,
            "after_state_role": "outcome_validation_only",
        }
        if target_source:
            out_ffec["target_source"] = str(target_source)
        elif name:
            out_ffec["target_source"] = "semantic_target"
        if quality_level:
            out_ffec["quality_level"] = str(quality_level)
        else:
            out_ffec["quality_level"] = "green"
        return out_ffec

    has_pre = bool(pre_snapshot and pre_snapshot.full_path)
    has_post = bool(post_state)
    name = ""
    aid = ""
    if isinstance(uia, dict):
        name = str(uia.get("name") or "").strip()
        aid = str(uia.get("automation_id") or "").strip()
    has_uia = bool(name or aid)
    has_parent = False
    if isinstance(parent_chain, dict):
        ancestors = parent_chain.get("ancestors") or []
        siblings = parent_chain.get("siblings_near_click") or []
        has_parent = bool(ancestors or siblings)
    ocr_lines = []
    if isinstance(ocr, dict):
        ocr_lines = list(ocr.get("ocr_lines") or [])
    has_ocr_or_text = bool(
        ocr_lines or name or aid
        or (parent_chain and any(
            s.get("name") for s in (parent_chain.get("siblings_near_click") or [])
        ))
    )
    has_real_ocr = bool([ln for ln in ocr_lines if str(ln or "").strip()])
    collateral_execution = (
        has_pre
        or has_post
        or has_parent
        or has_real_ocr
    )
    aid_stable = False
    if aid:
        s = aid.strip().lower()
        if len(s) >= 2 and s not in (
            "0", "-", "_", ".", "*", "##", "##0", "##1",
        ) and not s.startswith("generic"):
            aid_stable = not (
                "_" in s
                and ("ytd_" in s or "react" in s or "web-" in s)
                and len(s) >= 18
            )
    sufficient_for_execution = bool(
        has_uia
        and (
            collateral_execution
            or aid_stable
        )
    )
    iso = dict(target_identity_isolation or {})
    if (
        iso.get("trusted_pre_action_identity") is True
        and iso.get("identity_preserved_after_transition") is True
    ):
        sufficient_for_execution = True

    flags = {
        "has_pre_snapshot": has_pre,
        "has_post_state": has_post,
        "has_uia": has_uia,
        "has_parent_chain": has_parent,
        "has_ocr_or_text": has_ocr_or_text,
        "collateral_execution": collateral_execution,
        "stable_automation_id": aid_stable,
    }
    missing = [
        k.replace("has_", "")
        for k in (
            "has_pre_snapshot", "has_post_state", "has_uia",
            "has_parent_chain", "has_ocr_or_text",
        )
        if not flags[k]
    ]
    execution_missing: List[str] = []
    if not sufficient_for_execution:
        if not has_uia:
            execution_missing.append("uia_anchor")
        elif not collateral_execution and not aid_stable:
            execution_missing.append("collateral_or_stable_aid")

    out: Dict[str, Any] = dict(flags)
    out["missing"] = missing
    out["capture_complete"] = not missing
    out["sufficient_for_execution"] = sufficient_for_execution
    out["execution_missing"] = execution_missing
    out["trusted_pre_action_identity"] = bool(
        iso.get("trusted_pre_action_identity")
    )
    out["successful_state_transition"] = bool(
        iso.get("successful_state_transition")
    )
    out["identity_preserved_after_transition"] = bool(
        iso.get("identity_preserved_after_transition")
    )
    dr = iso.get("degradation_reason")
    out["degradation_reason"] = dr if dr else None
    if target_source:
        out["target_source"] = str(target_source)
    if quality_level:
        out["quality_level"] = str(quality_level)
    return out


def apply_capture_contract_to_metadata(
    metadata: Dict[str, Any],
    *,
    pre_snapshot: Optional[PreActionSnapshot],
    parent_chain: Optional[Dict[str, Any]],
    ocr: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Escribe los campos del contract dentro de ``metadata`` (in place)
    y devuelve la misma referencia. NO toca ``post_action_state`` ni
    ``capture_contract`` — esos los escribe el caller cuando los tenga.

    Convención de keys (estable):

      * ``pre_action_snapshot_full``, ``pre_action_captured_at_ms``,
        ``pre_action_age_ms``  — top-level.
      * ``vision_data.ocr_lines / ocr_text_around / ocr_region_bbox /
        ocr_source``           — bajo ``vision_data`` para que los
                                  consumers existentes (Truth Layer,
                                  Screen Element Reader) lo encuentren.
      * ``uia.parent_chain``   — anidado en el dict ``uia`` que ya
                                  existe.
    """
    if pre_snapshot is not None:
        metadata.update(pre_snapshot.to_metadata())
    if ocr:
        vd = dict(metadata.get("vision_data") or {})
        vd["ocr_lines"] = list(ocr.get("ocr_lines") or [])
        vd["ocr_text_around"] = str(ocr.get("ocr_text_around") or "")
        vd["ocr_region_bbox"] = dict(ocr.get("ocr_region_bbox") or {})
        vd["ocr_source"] = str(
            ocr.get("ocr_source") or "pre_action_expanded_region"
        )
        metadata["vision_data"] = vd
    if parent_chain is not None:
        uia = dict(metadata.get("uia") or {})
        uia["parent_chain"] = {
            "ancestors": list(parent_chain.get("ancestors") or []),
            "siblings_near_click": list(
                parent_chain.get("siblings_near_click") or []
            ),
        }
        metadata["uia"] = uia
    return metadata


def attach_precapture_diagnostics(
    metadata: Dict[str, Any],
    *,
    before_anchor: Optional[TargetAnchor],
    had_pending: bool,
    missing_reason: str = "",
) -> Dict[str, Any]:
    """Telemetría por click: éxito/fallo de pre-click mousedown."""
    diag = dict(metadata.get("precapture_diagnostics") or {})
    preclick_ok = bool(had_pending and is_identity_anchor(before_anchor))
    weak = not preclick_ok
    diag.update({
        "preclick_captured": preclick_ok,
        "had_pending_mousedown": bool(had_pending),
        "target_precapture_weak": weak,
        "missing_reason": (missing_reason or None) if weak else None,
        "before_anchor_phase": str(getattr(before_anchor, "phase", "") or ""),
        "before_anchor_source": str(getattr(before_anchor, "source", "") or ""),
    })
    metadata["precapture_diagnostics"] = diag
    return metadata


def aggregate_precapture_metrics(
    diagnostics: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Agrega métricas de sesión para reportes JSON."""
    total = len(diagnostics)
    if total == 0:
        return {
            "preclick_capture_rate": 1.0,
            "preclick_missing_count": 0,
            "target_precapture_weak_count": 0,
            "click_count": 0,
        }
    captured = sum(1 for d in diagnostics if d.get("preclick_captured"))
    weak = sum(1 for d in diagnostics if d.get("target_precapture_weak"))
    return {
        "preclick_capture_rate": round(captured / total, 4),
        "preclick_missing_count": weak,
        "target_precapture_weak_count": weak,
        "click_count": total,
    }


__all__ = [
    "PreActionSnapshot",
    "PostActionState",
    "TargetAnchor",
    "ScreenshotRingBuffer",
    "PostActionScheduler",
    "is_uia_weak",
    "capture_uia_parent_chain",
    "maybe_run_ocr",
    "build_capture_contract",
    "apply_capture_contract_to_metadata",
    "capture_target_anchor",
    "detect_state_change",
    "enforce_target_identity_isolation",
    "compute_trusted_pre_action_identity",
    "CAPTURE_PHASE_PRE_CLICK",
    "CAPTURE_PHASE_POST_ACTION",
    "TARGET_PRECAPTURE_MISSING",
    "ANCHOR_SOURCE_PYNPUT_MOUSEDOWN",
    "ANCHOR_SOURCE_PYNPUT_POST_CAPTURE",
    "ANCHOR_SOURCE_INTENT_LAYER",
    "is_identity_anchor",
    "resolve_identity_anchor",
    "attach_precapture_diagnostics",
    "aggregate_precapture_metrics",
    "DEFAULT_BUFFER_CAPACITY",
    "DEFAULT_BUFFER_PERIOD_MS",
    "DEFAULT_PRE_SNAPSHOT_MAX_AGE_MS",
    "DEFAULT_POST_DELAYS_MS",
    "DEFAULT_POST_WAIT_MS",
    "DEFAULT_OCR_REGION_STEPS_PX",
]
