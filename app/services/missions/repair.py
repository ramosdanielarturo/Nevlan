"""
ArthurOS Services - Repair / autocuración
-----------------------------------------
Cuando un paso falla o tiene baja confianza, el sistema entra en
"modo reparación":

   "No encontré {target}. Haz clic en el elemento correcto."

El usuario hace UN solo click sobre el elemento real. Capturamos un
TargetBundle nuevo y lo agregamos como **descriptor alternativo** del
mismo paso (no reemplaza el original — coleccionamos descriptores y
elegimos el mejor por scoring/historial).

Este módulo provee:

- ``OneClickCapture``: un mini-listener que espera un único click y
  captura un TargetBundle (web_data + uia + visual + coords).
- ``apply_repair_to_step``: integra el bundle nuevo en
  ``CompiledStep.target_context.alternates`` y registra métricas.
- ``record_method_outcome``: API simple para que el player aprenda de
  cada replay.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Callable

from app.contracts.mission import CompiledStep
from app.core.logger import log


class OneClickCapture:
    """Espera un único click del usuario y captura el TargetBundle
    completo del elemento debajo del cursor.

    Uso:
        cap = OneClickCapture(on_done=lambda bundle: ...)
        cap.start()
    """

    def __init__(self, on_done: Callable[[Optional[Dict[str, Any]]], None]):
        self._on_done = on_done
        self._listener = None
        self._lock = threading.Lock()
        self._done = False

    def start(self) -> None:
        try:
            from pynput.mouse import Listener as PyMouseListener

            def on_click(x, y, button, pressed):
                if pressed:
                    return
                with self._lock:
                    if self._done:
                        return
                    self._done = True
                # Captura asíncrona para no bloquear pynput.
                threading.Thread(target=self._capture, args=(x, y),
                                 daemon=True).start()
                return False  # detiene el listener

            self._listener = PyMouseListener(on_click=on_click)
            self._listener.start()
        except Exception as e:
            log.error(f"OneClickCapture start: {e}")
            self._on_done(None)

    def stop(self) -> None:
        with self._lock:
            self._done = True
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def _capture(self, x: int, y: int) -> None:
        try:
            bundle = capture_target_bundle(x, y)
        except Exception as e:
            log.error(f"capture_target_bundle: {e}")
            bundle = None
        try:
            self._on_done(bundle)
        except Exception as e:
            log.error(f"OneClickCapture on_done: {e}")


def capture_target_bundle(x: int, y: int) -> Optional[Dict[str, Any]]:
    """Reusa el flujo del MouseListener pero sin grabar un evento — solo
    extraemos el TargetBundle (UIA + web + snapshots + window context)
    para que sea consumido como ``alternate``.
    """
    from app.services.missions.recorder import (
        MouseListener, _get_window_context_at_point,
    )
    from app.services.missions.web_recorder import capture_web_target, is_web_process

    window_ctx = _get_window_context_at_point(x, y)
    # Reusamos el mismo capturador interno: instancia ad-hoc.
    ml = MouseListener(callback=lambda *_: None)
    uia = ml._capture_uia(x, y)  # noqa: SLF001
    small = ml._capture_snapshot(x, y, size=100)  # noqa: SLF001
    mid = ml._capture_snapshot(x, y, size=600, suffix="mid")  # noqa: SLF001
    web = None
    if window_ctx and is_web_process(getattr(window_ctx, "process_name", None)):
        try:
            web = capture_web_target(x, y, window_ctx.process_name)
        except Exception as e:
            log.debug(f"repair web: {e}")

    bundle: Dict[str, Any] = {
        "captured_at": time.time(),
        "fallback_coords": {"x": int(x), "y": int(y)},
        "uia_data": uia.get("uia") if uia else None,
        "anchor_bbox": uia.get("anchor_bbox") if uia else None,
        "click_offset_within_bbox": uia.get("click_offset") if uia else None,
        "image_ref": str(small) if small else None,
        "image_ref_mid": str(mid) if mid else None,
        "process_name": getattr(window_ctx, "process_name", None) if window_ctx else None,
        "window_title": getattr(window_ctx, "title", None) if window_ctx else None,
        "hwnd": getattr(window_ctx, "hwnd", None) if window_ctx else None,
        "web_data": web,
    }
    if window_ctx and getattr(window_ctx, "bounding_box", None):
        try:
            bb = window_ctx.bounding_box or {}
            ww = max(1, int(bb.get("width") or 0))
            hh = max(1, int(bb.get("height") or 0))
            bundle["fallback_coords_relative_to_window"] = {
                "fx": round(max(0.0, min(1.0,
                    (x - int(bb.get("left") or 0)) / ww)), 6),
                "fy": round(max(0.0, min(1.0,
                    (y - int(bb.get("top") or 0)) / hh)), 6),
            }
        except Exception:
            pass
    return bundle


def apply_repair_to_step(step: CompiledStep,
                          bundle: Dict[str, Any],
                          replace_primary: bool = False) -> None:
    """Agrega `bundle` como descriptor alternativo del paso. Si
    ``replace_primary=True``, también promueve los campos clave (uia,
    fallback_coords, web_data) al primer nivel para que el resolver lo
    intente primero. Por defecto, los alternates se acumulan y el
    resolver elegirá por scoring/historial.
    """
    tc = step.target_context
    alts = list(tc.alternates or [])
    alts.append(bundle)
    # Mantenemos solo los últimos 5 — más es ruido.
    if len(alts) > 5:
        alts = alts[-5:]
    tc.alternates = alts

    if replace_primary:
        if bundle.get("uia_data"):
            tc.uia_data = dict(bundle["uia_data"])
        if bundle.get("anchor_bbox"):
            tc.anchor_bbox = dict(bundle["anchor_bbox"])
        if bundle.get("click_offset_within_bbox"):
            tc.click_offset_within_bbox = dict(bundle["click_offset_within_bbox"])
        if bundle.get("fallback_coords"):
            tc.fallback_coords = dict(bundle["fallback_coords"])
        if bundle.get("fallback_coords_relative_to_window"):
            tc.fallback_coords_relative_to_window = dict(
                bundle["fallback_coords_relative_to_window"]
            )
        if bundle.get("image_ref"):
            tc.image_ref = bundle["image_ref"]
        if bundle.get("image_ref_mid"):
            tc.image_ref_mid = bundle["image_ref_mid"]
        if bundle.get("web_data"):
            tc.web_data = dict(bundle["web_data"])
        if bundle.get("process_name"):
            tc.process_name = bundle["process_name"]
        if bundle.get("window_title"):
            tc.window_title = bundle["window_title"]


def record_method_outcome(step: CompiledStep, method: str, ok: bool) -> None:
    """Incrementa el contador ok/fail por método para futuros scoring."""
    try:
        conf = dict(step.target_context.confidence or {})
        stats = dict(conf.get("success_by_method") or {})
        entry = dict(stats.get(method) or {"ok": 0, "fail": 0})
        if ok:
            entry["ok"] = int(entry.get("ok", 0)) + 1
        else:
            entry["fail"] = int(entry.get("fail", 0)) + 1
        stats[method] = entry
        conf["success_by_method"] = stats
        if ok:
            conf["last_success_method"] = method
        step.target_context.confidence = conf
    except Exception as e:
        log.debug(f"record_method_outcome: {e}")


# ──────────────────────────────────────────────────────────────────────
# Phase 8: bundle-level success tracking + promotion
# ──────────────────────────────────────────────────────────────────────
# Cuando TargetResolverV2 resuelve usando un alternate (no el primary),
# acumulamos racha de éxitos. Tras 3 éxitos consecutivos promovemos el
# alternate a primary (el primary anterior pasa a ser un alternate más
# para no perder lo aprendido).
#
# Estructura en ``target_context.confidence``:
#   alternate_streaks: {"alternate:0": {"ok_streak": int, "total_ok": int,
#                                         "total_fail": int}}
#   primary_total_ok: int
#   primary_total_fail: int

PROMOTION_STREAK = 3


def record_bundle_success(step: CompiledStep, bundle_label: str,
                           ok: bool) -> None:
    """Registra éxito/fracaso a nivel de bundle (primary vs alternate:N)
    y dispara promoción si el alternate alcanza ``PROMOTION_STREAK``
    éxitos consecutivos.
    """
    try:
        tc = step.target_context
        conf = dict(tc.confidence or {})
        if bundle_label == "primary":
            if ok:
                conf["primary_total_ok"] = int(conf.get("primary_total_ok", 0)) + 1
            else:
                conf["primary_total_fail"] = int(conf.get("primary_total_fail", 0)) + 1
            tc.confidence = conf
            return

        # alternate:N
        streaks = dict(conf.get("alternate_streaks") or {})
        entry = dict(streaks.get(bundle_label) or {
            "ok_streak": 0, "total_ok": 0, "total_fail": 0,
        })
        if ok:
            entry["ok_streak"] = int(entry.get("ok_streak", 0)) + 1
            entry["total_ok"] = int(entry.get("total_ok", 0)) + 1
        else:
            entry["ok_streak"] = 0
            entry["total_fail"] = int(entry.get("total_fail", 0)) + 1
        streaks[bundle_label] = entry
        conf["alternate_streaks"] = streaks
        tc.confidence = conf

        # Promoción: 3 éxitos seguidos ⇒ este alternate pasa a primary.
        if ok and int(entry.get("ok_streak", 0)) >= PROMOTION_STREAK:
            try:
                idx = int(bundle_label.split(":", 1)[1])
            except Exception:
                return
            _promote_alternate_to_primary(step, idx)
    except Exception as e:
        log.debug(f"record_bundle_success: {e}")


def _promote_alternate_to_primary(step: CompiledStep, alt_idx: int) -> None:
    """Mueve ``alternates[alt_idx]`` al primary y manda el primary anterior
    al final de ``alternates`` (no se pierde lo aprendido).
    """
    try:
        tc = step.target_context
        alts = list(tc.alternates or [])
        if not (0 <= alt_idx < len(alts)):
            return
        winner = alts.pop(alt_idx)

        # Snapshot del primary actual antes de pisar.
        old_primary = {
            "uia_data": tc.uia_data,
            "anchor_bbox": tc.anchor_bbox,
            "click_offset_within_bbox": tc.click_offset_within_bbox,
            "image_ref": tc.image_ref,
            "image_ref_mid": tc.image_ref_mid,
            "fallback_coords": tc.fallback_coords,
            "fallback_coords_relative_to_window": tc.fallback_coords_relative_to_window,
            "web_data": tc.web_data,
            "process_name": tc.process_name,
            "window_title": tc.window_title,
            "hwnd": tc.hwnd,
            "demoted_at": time.time(),
        }
        # Si el primary anterior tenía algo útil, lo conservamos.
        has_old = any(old_primary.get(k) for k in (
            "uia_data", "fallback_coords", "web_data", "image_ref", "image_ref_mid",
        ))

        # Aplicamos el ganador como nuevo primary.
        for key in (
            "uia_data", "anchor_bbox", "click_offset_within_bbox",
            "image_ref", "image_ref_mid", "fallback_coords",
            "fallback_coords_relative_to_window", "web_data",
            "process_name", "window_title", "hwnd",
        ):
            v = winner.get(key)
            if v is not None:
                try:
                    setattr(tc, key, v)
                except Exception:
                    continue

        # Reordenamos alternates: viejo primary al final, resetear streaks.
        if has_old:
            alts.append(old_primary)
        if len(alts) > 5:
            alts = alts[-5:]
        tc.alternates = alts

        conf = dict(tc.confidence or {})
        conf["alternate_streaks"] = {}  # se reinicia tras la promoción
        conf["last_promoted_at"] = time.time()
        conf["last_promoted_from"] = f"alternate:{alt_idx}"
        tc.confidence = conf

        log.info(
            f"repair: alternate#{alt_idx} promovido a primary "
            f"tras {PROMOTION_STREAK} éxitos consecutivos"
        )
    except Exception as e:
        log.debug(f"_promote_alternate_to_primary: {e}")


__all__ = [
    "OneClickCapture", "capture_target_bundle",
    "apply_repair_to_step", "record_method_outcome",
    "record_bundle_success", "PROMOTION_STREAK",
]
