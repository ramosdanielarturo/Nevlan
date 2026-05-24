"""
Nevlan — Execution Vision Pipeline (PRD 2026-05-12)
=====================================================

Pipeline de visión en tiempo de **ejecución** que refleja el orden de
captura del grabador, pero con escalones progresivos para gastar el
mínimo de recursos:

    1. **Coord grabada** — primero confiamos en la coord
       relativa/absoluta del grabador. Si UIA/DOM responden en ese
       punto, terminamos sin tomar ninguna foto.

    2. **Captura LOCAL** — si la coord falló, tomamos un recorte
       pequeño (200×200 px por defecto) alrededor del punto grabado
       y corremos :func:`match_in_frame` con el visual_fingerprint.
       Esta es la situación normal cuando el elemento se desplazó
       unos píxeles por re-layout.

    3. **Captura de VENTANA** — si el match local falla, tomamos toda
       la ventana activa del proceso (o un rect ~800×600 si no
       conocemos hwnd). El target se movió dentro de su contenedor
       pero sigue en la ventana.

    4. **Captura FULL-SCREEN** — último escalón antes de rendirse.
       Solo aquí pagamos el costo de una foto completa.

    5. **ASK** — si ninguna escala dio score ≥ medio, devolvemos
       ``decision="ask"``. El sistema NUNCA debe inventar coordenadas
       (regla PRD §10).

Diseño zero-cost
----------------

  * **CPU**: cada nivel cuesta ~10–80 ms según tamaño. Si nivel 1
    funciona, no tomamos ninguna foto (caso típico).
  * **Sin LLM, sin red, sin GPU**. Solo OpenCV + PIL ya instalados.
  * **Sin estado global**. La función es pura: entrada → resultado.
  * **Cacheable**: el caller puede memoizar el frame full-screen para
    reusar entre varios steps si así lo desea (no lo hacemos aquí
    para no asumir nada sobre la longevidad del frame).

Garantía de no-regresión
------------------------

Si OpenCV/PIL faltan o el fingerprint no está disponible, el pipeline
devuelve ``decision="ask"`` sin crashear — el ejecutor sigue su flujo
normal con UIA/DOM/rel_window/coords. La adopción es **opt-in**: solo
se activa cuando hay un fingerprint válido en la metadata del step.
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.logger import log
from app.services.missions.visual_fingerprint import (
    HAS_CV2,
    SCORE_MEDIUM,
    SCORE_STRONG,
    VisualFingerprint,
    relocate_target,
)


# ──────────────────────────────────────────────────────────────────────
# Constantes calibradas
# ──────────────────────────────────────────────────────────────────────

#: Lado del recorte local alrededor de la coord grabada. 200 px es un
#: buen punto medio: cabe la mayoría de los re-layouts típicos sin
#: capturar pantalla entera.
DEFAULT_LOCAL_HALF_SIDE_PX: int = 200

#: Tamaño máximo del recorte de "ventana" cuando no conocemos el HWND.
#: ~800×600 cubre toolbar + área principal de la mayoría de apps.
DEFAULT_WINDOW_FALLBACK_W: int = 800
DEFAULT_WINDOW_FALLBACK_H: int = 600


# ──────────────────────────────────────────────────────────────────────
# Resultado
# ──────────────────────────────────────────────────────────────────────


@dataclass
class VisionLookupResult:
    """Resultado de un intento de localización por visión."""

    found: bool = False
    decision: str = "ask"  # "accept" | "confirm" | "ask"
    x: int = 0
    y: int = 0
    score: float = 0.0
    label: str = "insufficient"
    bbox: Optional[Dict[str, int]] = None
    level: str = "none"  # "coord" | "local" | "window" | "fullscreen" | "none"
    components: Dict[str, float] = field(default_factory=dict)
    captures_taken: int = 0
    elapsed_ms: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "found": bool(self.found),
            "decision": str(self.decision),
            "x": int(self.x),
            "y": int(self.y),
            "score": round(float(self.score), 4),
            "label": str(self.label),
            "bbox": dict(self.bbox) if self.bbox else None,
            "level": str(self.level),
            "components": {
                k: round(float(v), 4) for k, v in self.components.items()
            },
            "captures_taken": int(self.captures_taken),
            "elapsed_ms": int(self.elapsed_ms),
            "reason": str(self.reason or ""),
        }


# ──────────────────────────────────────────────────────────────────────
# Captura progresiva
# ──────────────────────────────────────────────────────────────────────


def _grab_region(
    bbox: Tuple[int, int, int, int],
    out_path: Optional[str] = None,
) -> Optional[str]:
    """Captura PIL de la región dada. Devuelve path al PNG o None.

    Si ``out_path`` no se especifica, escribimos un temporal único.
    Usamos ``ImageGrab.grab(bbox=...)`` que es ~10× más rápido que un
    full-screen-and-crop.
    """
    try:
        from PIL import ImageGrab  # type: ignore
    except Exception:
        return None
    try:
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        except TypeError:  # PIL < 8 sin all_screens
            img = ImageGrab.grab(bbox=bbox)
        if out_path is None:
            out_path = str(
                Path(tempfile.gettempdir())
                / f"nevlan_exec_vision_{uuid.uuid4().hex[:10]}.png"
            )
        img.save(out_path)
        return out_path
    except Exception:
        return None


def _grab_fullscreen(out_path: Optional[str] = None) -> Optional[str]:
    try:
        from PIL import ImageGrab  # type: ignore
    except Exception:
        return None
    try:
        try:
            img = ImageGrab.grab(all_screens=True)
        except TypeError:
            img = ImageGrab.grab()
        if out_path is None:
            out_path = str(
                Path(tempfile.gettempdir())
                / f"nevlan_exec_vision_full_{uuid.uuid4().hex[:10]}.png"
            )
        img.save(out_path)
        return out_path
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────


def locate_with_progressive_vision(
    *,
    fingerprint: Any,
    recorded_xy: Optional[Tuple[int, int]] = None,
    window_bbox: Optional[Dict[str, int]] = None,
    accept_threshold: float = SCORE_STRONG,
    medium_threshold: float = SCORE_MEDIUM,
    local_half_side: int = DEFAULT_LOCAL_HALF_SIDE_PX,
    coord_verifier: Optional[Callable[[int, int], bool]] = None,
    capture_region: Optional[Callable[[Tuple[int, int, int, int]], Optional[str]]] = None,
    capture_fullscreen: Optional[Callable[[], Optional[str]]] = None,
    cleanup_temp: bool = True,
) -> VisionLookupResult:
    """Pipeline progresivo de localización por visión.

    Args:
        fingerprint: ``VisualFingerprint`` o ``dict`` (lo que el
            grabador guardó en ``metadata["visual_fingerprint"]``).
        recorded_xy: las coords originales del grabador (paso 1).
        window_bbox: bbox de la ventana del proceso destino (paso 3).
        accept_threshold / medium_threshold: umbrales del score.
        local_half_side: tamaño del recorte local en cada lado.
        coord_verifier: callable opcional ``(x, y) -> bool``. Si
            devuelve True para ``recorded_xy``, terminamos en nivel 1
            sin tomar foto. La idea: que UIA/DOM verifiquen
            estructuralmente que el elemento sigue ahí.
        capture_region / capture_fullscreen: inyectables para tests.

    Returns:
        :class:`VisionLookupResult` con coords + decisión + el nivel
        de pipeline donde se resolvió.
    """
    t0 = time.time()
    res = VisionLookupResult()

    # ── Validaciones tempranas ──────────────────────────────────
    if not HAS_CV2:
        res.reason = "cv2_unavailable"
        res.decision = "ask"
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res

    if isinstance(fingerprint, dict):
        fp = VisualFingerprint.from_dict(fingerprint)
    elif isinstance(fingerprint, VisualFingerprint):
        fp = fingerprint
    else:
        fp = VisualFingerprint()

    if not fp.available:
        res.reason = "fingerprint_not_available"
        res.decision = "ask"
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res

    grab_region = capture_region or (
        lambda bbox: _grab_region(bbox)
    )
    grab_full = capture_fullscreen or _grab_fullscreen
    temp_paths: List[str] = []

    try:
        # ── Nivel 1: confiar en la coord grabada si UIA/DOM la valida ──
        if recorded_xy is not None and coord_verifier is not None:
            rx, ry = int(recorded_xy[0]), int(recorded_xy[1])
            try:
                if coord_verifier(rx, ry):
                    res.found = True
                    res.decision = "accept"
                    res.x = rx
                    res.y = ry
                    res.level = "coord"
                    res.label = "strong"
                    res.score = 1.0
                    res.reason = "coord_verifier_passed"
                    return res
            except Exception:
                pass

        # ── Nivel 2: captura LOCAL alrededor de la coord ───────────
        if recorded_xy is not None:
            rx, ry = int(recorded_xy[0]), int(recorded_xy[1])
            half = max(60, int(local_half_side))
            local_bbox = (
                rx - half, ry - half,
                rx + half, ry + half,
            )
            local_path = grab_region(local_bbox)
            if local_path:
                res.captures_taken += 1
                temp_paths.append(local_path)
                # Bbox del crop dentro del frame full-screen para
                # devolver coords absolutas.
                offset = (local_bbox[0], local_bbox[1])
                out = relocate_target(
                    fingerprint=fp,
                    frame_path=local_path,
                    accept_threshold=accept_threshold,
                    medium_threshold=medium_threshold,
                )
                if out.get("found"):
                    bb = out.get("bbox") or {}
                    res.found = True
                    res.decision = str(out.get("decision") or "confirm")
                    res.x = int(out.get("x", 0)) + offset[0]
                    res.y = int(out.get("y", 0)) + offset[1]
                    res.score = float(out.get("score", 0.0))
                    res.label = str(out.get("label", "insufficient"))
                    bb["left"] = int(bb.get("left", 0)) + offset[0]
                    bb["top"] = int(bb.get("top", 0)) + offset[1]
                    res.bbox = bb
                    res.level = "local"
                    res.components = dict(out.get("components") or {})
                    return res

        # ── Nivel 3: captura de VENTANA ────────────────────────────
        if window_bbox and isinstance(window_bbox, dict):
            l = int(window_bbox.get("left", 0))
            t = int(window_bbox.get("top", 0))
            w = int(window_bbox.get("width", 0))
            h = int(window_bbox.get("height", 0))
        else:
            # Sin hwnd: usamos un recorte centrado en la coord o (0,0).
            if recorded_xy is not None:
                rx, ry = int(recorded_xy[0]), int(recorded_xy[1])
                l = max(0, rx - DEFAULT_WINDOW_FALLBACK_W // 2)
                t = max(0, ry - DEFAULT_WINDOW_FALLBACK_H // 2)
            else:
                l, t = 0, 0
            w = DEFAULT_WINDOW_FALLBACK_W
            h = DEFAULT_WINDOW_FALLBACK_H

        if w > 0 and h > 0:
            win_bbox_t = (l, t, l + w, t + h)
            win_path = grab_region(win_bbox_t)
            if win_path:
                res.captures_taken += 1
                temp_paths.append(win_path)
                out = relocate_target(
                    fingerprint=fp,
                    frame_path=win_path,
                    accept_threshold=accept_threshold,
                    medium_threshold=medium_threshold,
                )
                if out.get("found"):
                    bb = out.get("bbox") or {}
                    res.found = True
                    res.decision = str(out.get("decision") or "confirm")
                    res.x = int(out.get("x", 0)) + l
                    res.y = int(out.get("y", 0)) + t
                    res.score = float(out.get("score", 0.0))
                    res.label = str(out.get("label", "insufficient"))
                    bb["left"] = int(bb.get("left", 0)) + l
                    bb["top"] = int(bb.get("top", 0)) + t
                    res.bbox = bb
                    res.level = "window"
                    res.components = dict(out.get("components") or {})
                    return res

        # ── Nivel 4: captura FULL-SCREEN ───────────────────────────
        full_path = grab_full()
        if full_path:
            res.captures_taken += 1
            temp_paths.append(full_path)
            out = relocate_target(
                fingerprint=fp,
                frame_path=full_path,
                accept_threshold=accept_threshold,
                medium_threshold=medium_threshold,
            )
            if out.get("found"):
                res.found = True
                res.decision = str(out.get("decision") or "confirm")
                res.x = int(out.get("x", 0))
                res.y = int(out.get("y", 0))
                res.score = float(out.get("score", 0.0))
                res.label = str(out.get("label", "insufficient"))
                res.bbox = dict(out.get("bbox") or {}) or None
                res.level = "fullscreen"
                res.components = dict(out.get("components") or {})
                return res

        # ── Nivel 5: ASK ───────────────────────────────────────────
        res.found = False
        res.decision = "ask"
        res.level = "none"
        res.reason = "exhausted_all_levels"
        return res
    finally:
        res.elapsed_ms = int((time.time() - t0) * 1000)
        # Borramos los temporales para no llenar el disco.
        if cleanup_temp:
            for p in temp_paths:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass


__all__ = [
    "VisionLookupResult",
    "locate_with_progressive_vision",
    "DEFAULT_LOCAL_HALF_SIDE_PX",
]
