"""
Nevlan — DOM Probe (PRD 2026-05-10j)
=====================================

Adapter delgado entre el :mod:`intent_interception_layer` y el
``web_recorder`` existente (que ya habla CDP / Playwright). El
``IntentInterceptionLayer`` requiere un callable
``(x, y) -> Optional[dict]``; este módulo se lo ofrece sin acoplar
la capa a Playwright ni a la implementación concreta del bridge.

Por qué un adapter
------------------

El ``web_recorder.capture_web_target`` ya hace:

  * Conexión a la sesión CDP que mantiene ``app.skills.tools.web``
    (puerto 9222 por defecto).
  * Conversión coords pantalla → viewport vía Win32 (sin clamping).
  * ``document.elementFromPoint`` dentro del frame correcto.
  * Cálculo de locators Playwright ordenados por especificidad.

Pero su firma incluye ``process_name`` y ``hwnd`` — datos que la capa
no quiere pasar manualmente. El adapter encapsula ese trabajo y deja
solo la firma ``(x, y) -> dict | None`` para la capa.

Diseño
------

  * **Pure-Python**, sin Qt, sin LLM.
  * **Best-effort**: cualquier fallo de CDP devuelve ``None`` sin
    relanzar. Eso permite que la capa siga adelante con UIA/OCR.
  * **Sin hardcode por app**: el adapter detecta el proceso web bajo
    el cursor con :func:`is_web_process` del web_recorder (que ya
    cubre Chrome/Edge/Firefox/Brave).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from app.core.logger import log


# Firma del callable que resuelve el contexto de ventana bajo (x,y).
# Devuelve un dict mínimo con ``process_name`` y ``hwnd`` (root window
# handle del navegador). Si nada útil, retorna ``{}``.
WindowAtPointFn = Callable[[int, int], Dict[str, Any]]


# ──────────────────────────────────────────────────────────────────────
# Default window-at-point resolver (best-effort sobre Win32 + UIA)
# ──────────────────────────────────────────────────────────────────────


def _default_window_at_point(x: int, y: int) -> Dict[str, Any]:
    """Implementación por defecto: usa el helper del recorder.

    Se importa de forma perezosa para no crear ciclos durante la
    importación del módulo.
    """
    try:
        from app.services.missions.recorder import (
            _get_window_context_at_point,  # type: ignore
        )
    except Exception:
        return {}
    try:
        wc = _get_window_context_at_point(int(x), int(y))
    except Exception:
        return {}
    if wc is None:
        return {}
    return {
        "process_name": getattr(wc, "process_name", "") or "",
        "hwnd": getattr(wc, "hwnd", None),
    }


# ──────────────────────────────────────────────────────────────────────
# DOMProbe
# ──────────────────────────────────────────────────────────────────────


@dataclass
class DOMProbe:
    """Probe DOM para la :class:`IntentInterceptionLayer`.

    Uso típico::

        probe = DOMProbe()
        layer = IntentInterceptionLayer(dom_probe=probe.element_from_point, ...)

    El método :meth:`element_from_point` cumple la firma esperada por
    la capa: ``(x, y) -> Optional[Dict[str, Any]]``.
    """

    window_at_point: WindowAtPointFn = _default_window_at_point
    # Inyectable para tests: caller que hace el trabajo CDP real.
    capture_web_target: Optional[Callable[..., Optional[Dict[str, Any]]]] = None

    def element_from_point(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        """Devuelve el dict de target web bajo el cursor, o ``None``.

        El dict tiene la forma estándar que el web_recorder emite:
        ``{"url", "role", "accessible_name", "locators": [...],
        "bbox": {...}, ...}``.

        Contrato: solo corre si el proceso bajo (x, y) es un navegador
        soportado. La validación se hace SIEMPRE, incluso cuando el
        caller inyectó un ``capture_web_target`` propio — el probe no
        debe llamar a Playwright sobre apps no-web.
        """
        try:
            wc = self.window_at_point(int(x), int(y))
        except Exception:
            wc = {}
        process_name = str((wc or {}).get("process_name") or "")
        hwnd = (wc or {}).get("hwnd")
        if not process_name:
            return None
        try:
            from app.services.missions.web_recorder import is_web_process
        except Exception:
            is_web_process = None  # type: ignore[assignment]
        if is_web_process is not None and not is_web_process(process_name):
            return None
        runner = self.capture_web_target
        if runner is None:
            try:
                from app.services.missions.web_recorder import (
                    capture_web_target as _cw,
                )
            except Exception as e:
                log.debug(f"DOMProbe: web_recorder no disponible: {e}")
                return None
            runner = _cw
        try:
            return runner(int(x), int(y), process_name, hwnd=hwnd)
        except Exception as e:
            log.debug(f"DOMProbe: capture_web_target falló: {e}")
            return None

    # Sugar para usar como callable directo.
    def __call__(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        return self.element_from_point(x, y)


__all__ = ["DOMProbe", "WindowAtPointFn"]
