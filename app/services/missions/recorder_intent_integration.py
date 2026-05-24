"""
Nevlan — Recorder ↔ IntentInterceptionLayer Integration (PRD 2026-05-10j)
============================================================================

Glue code que conecta el ``MissionRecorder`` con la
:class:`IntentInterceptionLayer`. Su tarea: cada click de grabación
pasa por la capa **o** la grabación falla en modo seguro.

Por qué un módulo aparte
------------------------

El ``recorder.py`` ya tiene 99 KB y un pipeline maduro. Tocar su
interior con la lógica del intent layer mezclaría dos responsabilidades
y haría imposible revertir el cambio. Este módulo:

  1. Construye la capa con los probes correctos (UIA, DOM via CDP,
     OCR, visión, ventana, screenshot).
  2. Instala el ``Win32MouseHookAdapter`` cuando es posible para
     capturar ``mouse_down`` ANTES del cambio de pantalla.
  3. Expone dos puntos de inyección al recorder:
       * :func:`notify_mouse_down(layer, x, y)` — llamarlo desde el
         hook o desde ``MouseListener.on_click(pressed=True)``.
       * :func:`process_click_through_layer(layer, x, y, click_ms,
         metadata)` — llamarlo desde ``MouseListener._process_click``
         AL FINAL del pipeline existente para enriquecer la metadata.
  4. Garantiza ``fail-safe``: si la capa no está activa,
     ``ensure_layer_or_fail(strict=True)`` levanta
     :class:`RecorderUnsafeError` en lugar de regresar al flujo legacy.

Diseño deliberado
-----------------

  * **Pure-Python**, sin Qt, sin LLM, sin red.
  * **Idempotente y reentrante**: se puede usar desde varios threads
    del recorder; el ``IntentInterceptionLayer`` ya gestiona sus
    propios locks.
  * **Sin app-specific**: ningún literal "Chrome" / "YouTube".
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from app.core.logger import log
from app.services.missions.candidate_fusion_engine import CandidateFusionEngine
from app.services.missions.dom_probe import DOMProbe
from app.services.missions.intent_interception_layer import (
    IntentInterceptionLayer,
    InterceptedClick,
)
from app.services.missions.recorder_capture_contract import (
    PostActionScheduler,
    ScreenshotRingBuffer,
)


class RecorderUnsafeError(RuntimeError):
    """Se levanta cuando la grabación no puede operar de forma segura.

    Reglas (PRD §10 — políticas de no-regresión):

      * Si ``strict_intent_interception=True`` y la capa no se pudo
        construir → grabación rechazada.
      * Si la capa NO está activa y el caller invoca
        :func:`process_click_through_layer` → click rechazado.

    La política es "fail-safe, NO regresión al legacy".
    """


# ──────────────────────────────────────────────────────────────────────
# Construcción de la capa con todos sus probes
# ──────────────────────────────────────────────────────────────────────


def _make_uia_probe() -> Callable[[int, int], Optional[Dict[str, Any]]]:
    """Wrapper sobre la captura UIA del recorder.

    El recorder ya tiene ``MouseListener._capture_uia`` que devuelve un
    dict con ``{"uia": {...}, "anchor_bbox": ...}``. Para la capa
    necesitamos solo el sub-dict ``uia``. Esta función adapta.
    """
    def probe(x: int, y: int) -> Optional[Dict[str, Any]]:
        try:
            from app.services.missions.recorder import (
                _refine_to_meaningful,  # type: ignore
            )
        except Exception:
            return None
        try:
            import uiautomation as auto  # type: ignore
        except Exception:
            return None
        try:
            with auto.UIAutomationInitializerInThread():
                elem = auto.ControlFromPoint(int(x), int(y))
                elem = _refine_to_meaningful(elem, int(x), int(y))
                if not elem:
                    return None
                bbox = None
                try:
                    r = elem.BoundingRectangle
                    left = int(getattr(r, "left", 0))
                    top = int(getattr(r, "top", 0))
                    w = int(getattr(r, "right", 0)) - left
                    h = int(getattr(r, "bottom", 0)) - top
                    if w > 0 and h > 0:
                        bbox = {"left": left, "top": top, "width": w, "height": h}
                except Exception:
                    pass
                return {
                    "name": str(getattr(elem, "Name", "") or ""),
                    "automation_id": str(getattr(elem, "AutomationId", "") or ""),
                    "control_type": str(getattr(elem, "ControlTypeName", "") or ""),
                    "class_name": str(getattr(elem, "ClassName", "") or ""),
                    "bbox": bbox,
                }
        except Exception:
            return None
    return probe


def _make_window_ctx_probe() -> Callable[[], Dict[str, Any]]:
    """Devuelve el window_ctx actual (proceso/título/url si hay)."""
    def probe() -> Dict[str, Any]:
        try:
            from app.services.missions.recorder import (
                _capture_post_state_default,  # type: ignore
            )
        except Exception:
            return {}
        try:
            return dict(_capture_post_state_default() or {})
        except Exception:
            return {}
    return probe


def _make_ocr_probe() -> Callable[
    [Optional[str], Tuple[int, int]], Optional[Dict[str, Any]]
]:
    """Wrapper sobre :func:`maybe_run_ocr` que la capa puede consumir."""
    from app.services.missions.recorder_capture_contract import (
        maybe_run_ocr,
    )

    def probe(
        pre_path: Optional[str], xy: Tuple[int, int],
    ) -> Optional[Dict[str, Any]]:
        if not pre_path:
            return None
        try:
            return maybe_run_ocr(pre_snapshot_full=pre_path, click_xy=xy)
        except Exception:
            return None
    return probe


# ──────────────────────────────────────────────────────────────────────
# Builder + fail-safe gate
# ──────────────────────────────────────────────────────────────────────


@dataclass
class LayerHandle:
    """Handle público que el ``MissionRecorder`` guarda en ``start()``.

    Lleva la capa + el hook Win32 (si arrancó). El ``stop()`` del
    recorder llama a :meth:`shutdown` para que ambos se detengan
    cooperativamente.
    """

    layer: IntentInterceptionLayer
    win32_hook: Any = None  # Win32MouseHookAdapter | None
    last_intercept: Optional[InterceptedClick] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def remember_intercept(self, ic: InterceptedClick) -> None:
        with self._lock:
            self.last_intercept = ic

    def shutdown(self) -> None:
        h = self.win32_hook
        if h is not None:
            try:
                h.stop()
            except Exception as e:
                log.debug(f"[intent_integration] win32_hook.stop fail: {e}")


def build_layer_for_recording(
    *,
    pre_buffer: Optional[ScreenshotRingBuffer] = None,
    post_scheduler: Optional[PostActionScheduler] = None,
    dom_probe: Optional[DOMProbe] = None,
    install_win32_hook: bool = True,
    is_own_hwnd: Optional[Callable[[int], bool]] = None,
) -> LayerHandle:
    """Construye :class:`IntentInterceptionLayer` listo para grabación.

    Si ``install_win32_hook`` está activo y la plataforma lo soporta,
    también arranca el :class:`Win32MouseHookAdapter` que dispara
    ``layer.on_mouse_down`` cuando el SO emite ``WM_LBUTTONDOWN``.

    En modo grabación, la capa **no debe ejecutar** clicks
    (``clicker=None``): el SO sigue siendo quien procesa el click real
    — Nevlan solo observa, captura y decide. Eso es seguro: incluso
    si la capa o el hook fallan, el usuario nunca pierde el control.
    """
    dom_probe = dom_probe or DOMProbe()

    layer = IntentInterceptionLayer(
        clicker=None,  # ← grabación: NO ejecutamos el click; lo hace el OS.
        uia_probe=_make_uia_probe(),
        dom_probe=dom_probe,
        ocr_runner=_make_ocr_probe(),
        visual_probe=None,  # visión la maneja recorder._process_click directo.
        parent_chain_probe=None,
        window_ctx_probe=_make_window_ctx_probe(),
        state_probe=_make_window_ctx_probe(),
        screenshot_probe=None,
        pre_buffer=pre_buffer,
        post_scheduler=post_scheduler,
        fusion=CandidateFusionEngine(),
        clock_ms=lambda: int(time.time() * 1000),
    )

    handle = LayerHandle(layer=layer)
    if install_win32_hook:
        try:
            from app.services.missions.win32_mouse_hook import (
                Win32MouseHookAdapter, is_available,
            )
            if is_available():
                def _on_hook_event(ev) -> None:
                    if ev.kind == "down":
                        try:
                            layer.on_mouse_down(int(ev.x), int(ev.y))
                        except Exception as e:
                            log.debug(
                                f"[intent_integration] layer.on_mouse_down "
                                f"failed: {e}"
                            )
                hook = Win32MouseHookAdapter(
                    on_event=_on_hook_event,
                    is_own_hwnd_at=is_own_hwnd,
                )
                if hook.start():
                    handle.win32_hook = hook
                    log.debug("[intent_integration] win32_hook started")
                else:
                    log.debug("[intent_integration] win32_hook failed to start")
        except Exception as e:
            log.debug(f"[intent_integration] win32_hook install failed: {e}")
    return handle


def ensure_layer_or_fail(
    handle: Optional[LayerHandle], *, strict: bool,
) -> LayerHandle:
    """Gate fail-safe. Levanta :class:`RecorderUnsafeError` cuando
    ``strict=True`` y no hay capa activa.

    En modo non-strict (legacy / tests), devuelve un handle sintético
    con una capa "vacía" (sin probes, sin clicker) para que el
    código de integración no se rompa — pero no es el camino feliz.
    """
    if handle is not None:
        return handle
    if strict:
        raise RecorderUnsafeError(
            "IntentInterceptionLayer no inicializada — grabación bloqueada "
            "en modo seguro (PRD §10 no-regresión: 'no volver al legacy')."
        )
    # Non-strict: handle vacío.
    return LayerHandle(layer=IntentInterceptionLayer())


# ──────────────────────────────────────────────────────────────────────
# Hooks que el MouseListener invoca
# ──────────────────────────────────────────────────────────────────────


def notify_mouse_down(handle: LayerHandle, x: int, y: int) -> None:
    """Disparar la fase 1 del layer (sin bloquear pynput).

    Llamable desde:
      * Win32 hook ``on_event(kind="down")``.
      * ``MouseListener.on_click(pressed=True)`` como red de
        seguridad cuando el hook Win32 no arrancó.

    Idempotente: si la capa ya tiene un pending no lo sobreescribe
    (se respeta la captura más temprana).
    """
    if handle is None or handle.layer is None:
        return
    if handle.layer.has_pending:
        return
    try:
        handle.layer.on_mouse_down(int(x), int(y))
    except Exception as e:
        log.debug(f"[intent_integration] on_mouse_down failed: {e}")


def process_click_through_layer(
    handle: Optional[LayerHandle],
    *,
    x: int,
    y: int,
    click_ms: int,
    uia: Optional[Dict[str, Any]] = None,
    dom: Optional[Dict[str, Any]] = None,
    ocr: Optional[Dict[str, Any]] = None,
    visual: Optional[Dict[str, Any]] = None,
    parent_chain: Optional[Dict[str, Any]] = None,
    window_ctx: Optional[Dict[str, Any]] = None,
    visual_fingerprint: Optional[Dict[str, Any]] = None,
    strict: bool = True,
) -> Dict[str, Any]:
    """Reusa el resultado del pipeline existente del recorder para
    armar el :class:`InterceptedClick` final y devolver la metadata
    enriquecida (``target_identity``, ``intent_decision``,
    ``evidence_sources``, ``fusion_audit``) lista para anidar en el
    ``RawEvent.metadata``.

    Si ``strict=True`` y ``handle`` es ``None``, levanta
    :class:`RecorderUnsafeError`. En non-strict devuelve ``{}`` y el
    caller decide qué hacer (compatibilidad con tests viejos).
    """
    if handle is None:
        if strict:
            raise RecorderUnsafeError(
                "process_click_through_layer llamado sin layer activa."
            )
        return {}

    layer = handle.layer
    # Usamos la capa para la decisión final (fusion + isolation + outcome).
    # Pero le pasamos los datos YA capturados por el pipeline existente
    # — no queremos correr UIA/DOM/OCR dos veces. La capa los recibe
    # como dependencias inyectadas vía la fusion engine.
    fusion = layer.fusion
    result = fusion.fuse(
        click_xy=(int(x), int(y)),
        click_ms=int(click_ms),
        window_ctx=dict(window_ctx or {}),
        uia=uia,
        dom=dom,
        ocr=ocr,
        visual=visual,
        parent_chain=parent_chain,
        outcome_confirmed=False,
        capture_ms=int(time.time() * 1000),
        visual_fingerprint=visual_fingerprint,
    )
    identity = result.chosen
    audit = result.to_dict()
    intent_meta: Dict[str, Any] = {
        "intent_decision": result.decision,
        "intent_confidence": round(float(result.confidence), 4),
        "intent_confidence_label": identity.confidence_label,
        "target_identity": identity.to_dict(),
        "evidence_sources": list(result.evidence_sources),
        "fusion_audit": {
            "decision": audit["decision"],
            "confidence": audit["confidence"],
            "candidates": audit["candidates"],
            "reasons": audit["reasons"],
            "ambiguity": audit["ambiguity"],
        },
    }
    return intent_meta


__all__ = [
    "LayerHandle",
    "RecorderUnsafeError",
    "build_layer_for_recording",
    "ensure_layer_or_fail",
    "notify_mouse_down",
    "process_click_through_layer",
]
