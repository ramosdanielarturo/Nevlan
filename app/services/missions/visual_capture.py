"""
Nevlan Visual Target Capture — bbox-first, click-as-fallback
=============================================================

Para cada click (o selección equivalente) generamos los assets visuales a
partir del **bbox del target**, no del punto exacto del click. Así el
replay visual abarca el elemento completo y aguanta cambios de layout.

Pipeline
--------

    click(x, y) + uia/web/window context
        │
        ▼  select_target_bbox  ──────────────────────────────┐
        target_bbox + source ∈ {uia, dom, ocr, vision,         │
                                 click_fallback}              │
        │                                                     │
        ▼  compute_asset_bbox (margin = max(40, 20%·dim),      │
        asset_bbox          cap 160, clamp a screen)          │
        │                                                     │
        ▼  capture_visual_assets                               │
        small.png  =  exact target_bbox                        │
        mid.png    =  asset_bbox (target + margen)             │
        full.png   =  ventana activa (o pantalla)              │
        │                                                     │
        ▼  validate_visual_capture                             │
        click_xy ∈ target_bbox ⊆ asset_bbox ⊆ screen           │
        archivos abren sin error                               │
        │                                                     │
        ▼                                                     │
        CapturedVisual (paths + metadata + quality_level) ◄────┘

Reglas de selección de target_bbox (en orden de preferencia)
------------------------------------------------------------

    1. UIA bbox de un elemento *meaningful* (no genérico, no técnico,
       y < 25% del área de la ventana).
    2. DOM bbox (web_metadata.bbox) cuando es web y satisface las mismas
       restricciones de tamaño.
    3. OCR bbox del texto cercano al click (best-effort, hookable).
    4. Vision/icon bbox (best-effort, hookable).
    5. ``click_fallback`` — bbox sintético alrededor del click. Marca
       ``quality_level = low`` y ``needs_reinforcement = True`` para
       que Mission Review pida refuerzo visual al usuario.

Rechazos automáticos de contenedores
------------------------------------

* ControlType genéricos: ``GroupControl``, ``PaneControl``,
  ``DocumentControl``, ``WindowControl``, ``ToolBarControl``,
  ``StatusBarControl`` (sin AutomationId que rescate).
* Nombres técnicos: ``guide-service``, ``ytd-app``, ``ytd-search``,
  ``ytd-*``, ``video-stream``, ``app-root``, ``app-shell``,
  ``react-root``, ``container``, ``wrapper``.
* Tamaño: ``bbox_area > 0.25 · window_area`` (o screen si no hay window).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import sys
import uuid


# ──────────────────────────────────────────────────────────────────────
# Constantes de la spec
# ──────────────────────────────────────────────────────────────────────

#: margen contextual mínimo en píxeles
MIN_MARGIN_PX: int = 40

#: margen contextual máximo en píxeles
MAX_MARGIN_PX: int = 160

#: ratio respecto a la dimensión del bbox (20%)
MARGIN_RATIO: float = 0.20

#: si el bbox cubre más de este ratio del área de la ventana / pantalla,
#: lo consideramos contenedor inválido (regla #9 del PRD).
MAX_TARGET_AREA_RATIO: float = 0.25

#: tamaño mínimo de un bbox aceptable; debajo de esto consideramos
#: que algo está mal y caemos a ``click_fallback``.
MIN_TARGET_DIM_PX: int = 4

#: tamaño del cuadro sintético de ``click_fallback`` (lado en píxeles)
CLICK_FALLBACK_SIZE_PX: int = 80

#: ControlTypes UIA que jamás son target válido por sí solos.
GENERIC_UIA_CONTROL_TYPES = frozenset({
    "groupcontrol", "panecontrol", "customcontrol",
    "documentcontrol", "windowcontrol", "toolbarcontrol",
    "statusbarcontrol",
})

#: Patrones de nombre/automation_id que indican contenedor técnico
#: (web modernas, frameworks SPA). Si el ÚNICO descriptor del target
#: matchea uno, lo descartamos como contenedor.
TECHNICAL_NAME_PATTERNS = (
    "guide-service",
    "ytd-app", "ytd-search", "ytd-",
    "video-stream",
    "app-root", "app-shell", "react-root",
    "container", "wrapper", "outer", "inner",
)


# ──────────────────────────────────────────────────────────────────────
# Tipos de dominio
# ──────────────────────────────────────────────────────────────────────

# Etiquetas de fuente — fijas para que tests, telemetría y UI las usen
# sin string typos.
SRC_UIA = "uia"
SRC_DOM = "dom"
SRC_OCR = "ocr"
SRC_VISION = "vision"
SRC_CLICK_FALLBACK = "click_fallback"


@dataclass
class TargetCandidate:
    """Bbox candidato producido por una de las fuentes (UIA/DOM/OCR/...)."""
    bbox: Dict[str, int]   # left, top, width, height (coords absolutas pantalla)
    source: str
    label: str = ""        # nombre humano (para diagnóstico/UI)
    role: str = ""         # control_type / aria role
    confidence: float = 1.0
    is_canvas_action: bool = False  # bypass al check 25% si el usuario
                                    # explícitamente clickeó fondo/lienzo


@dataclass
class CapturedVisual:
    """Resultado del pipeline visual de un step.

    Todos los paths son ``str`` para ser JSON-serializable; ``None`` si la
    captura del archivo falló (no rompemos la grabación por eso).
    """
    target_bbox: Dict[str, int]
    asset_bbox: Dict[str, int]
    click_xy: Tuple[int, int]
    click_offset_px: Tuple[int, int]      # (dx, dy) respecto al centro del bbox
    click_offset_ratio: Tuple[float, float]  # (fx, fy) ∈ [0,1] dentro del bbox
    source: str                            # uia | dom | ocr | vision | click_fallback
    quality_level: str                     # high | medium | low
    needs_reinforcement: bool              # señal a Mission Review

    small_path: Optional[str] = None       # target exacto
    mid_path: Optional[str] = None         # target + margen contextual
    full_path: Optional[str] = None        # ventana activa (o pantalla)

    dpi_scale: Optional[float] = None
    screen_size: Optional[Tuple[int, int]] = None  # (w, h)
    window_bbox: Optional[Dict[str, int]] = None
    validation_errors: List[str] = field(default_factory=list)

    def to_metadata(self) -> Dict[str, Any]:
        """Serializa los campos que el recorder inyecta en ``RawEvent.metadata``.

        Conserva keys cortas para no inflar tokens en la traza.
        """
        out: Dict[str, Any] = {
            "target_bbox": dict(self.target_bbox),
            "asset_bbox": dict(self.asset_bbox),
            "click_xy": [int(self.click_xy[0]), int(self.click_xy[1])],
            "click_offset_px": {
                "dx": int(self.click_offset_px[0]),
                "dy": int(self.click_offset_px[1]),
            },
            "click_offset_ratio": {
                "fx": round(float(self.click_offset_ratio[0]), 4),
                "fy": round(float(self.click_offset_ratio[1]), 4),
            },
            "target_source": self.source,
            "quality_level": self.quality_level,
            "needs_reinforcement": bool(self.needs_reinforcement),
        }
        if self.dpi_scale is not None:
            out["dpi_scale"] = float(self.dpi_scale)
        if self.screen_size is not None:
            out["screen_size"] = [int(self.screen_size[0]), int(self.screen_size[1])]
        if self.window_bbox is not None:
            out["window_bbox"] = dict(self.window_bbox)
        if self.small_path:
            out["snapshot_small"] = self.small_path
        if self.mid_path:
            out["snapshot_mid"] = self.mid_path
        if self.full_path:
            out["snapshot_full"] = self.full_path
        if self.validation_errors:
            out["validation_errors"] = list(self.validation_errors)
        return out


# ──────────────────────────────────────────────────────────────────────
# Helpers geométricos
# ──────────────────────────────────────────────────────────────────────

def _bbox_area(bbox: Optional[Dict[str, int]]) -> int:
    if not bbox:
        return 0
    w = max(0, int(bbox.get("width") or 0))
    h = max(0, int(bbox.get("height") or 0))
    return w * h


def _bbox_contains_point(bbox: Dict[str, int], x: int, y: int) -> bool:
    if not bbox:
        return False
    left = int(bbox.get("left", 0))
    top = int(bbox.get("top", 0))
    right = left + int(bbox.get("width", 0))
    bottom = top + int(bbox.get("height", 0))
    return left <= int(x) <= right and top <= int(y) <= bottom


def _bbox_contains_bbox(outer: Dict[str, int], inner: Dict[str, int]) -> bool:
    if not outer or not inner:
        return False
    ol = int(outer.get("left", 0))
    ot = int(outer.get("top", 0))
    or_ = ol + int(outer.get("width", 0))
    ob = ot + int(outer.get("height", 0))
    il = int(inner.get("left", 0))
    it = int(inner.get("top", 0))
    ir = il + int(inner.get("width", 0))
    ib = it + int(inner.get("height", 0))
    return ol <= il and ot <= it and or_ >= ir and ob >= ib


def _clamp_bbox_to_screen(
    bbox: Dict[str, int], screen: Tuple[int, int],
) -> Dict[str, int]:
    """Recorta ``bbox`` para que caiga dentro de ``[0, screen_w] × [0, screen_h]``."""
    sw, sh = int(screen[0]), int(screen[1])
    left = max(0, int(bbox.get("left", 0)))
    top = max(0, int(bbox.get("top", 0)))
    right = min(sw, int(bbox.get("left", 0)) + int(bbox.get("width", 0)))
    bottom = min(sh, int(bbox.get("top", 0)) + int(bbox.get("height", 0)))
    return {
        "left": left,
        "top": top,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
    }


def _is_technical_label(label: str) -> bool:
    if not label:
        return False
    lo = label.strip().lower()
    for pat in TECHNICAL_NAME_PATTERNS:
        if pat in lo:
            return True
    # Heurística: id slug autogenerado (sin espacios + con guiones + ≥10 chars)
    if " " not in lo and "-" in lo and len(lo) >= 10:
        # No descartar si tiene punto (hostname real).
        if "." not in lo:
            return True
    return False


def _is_generic_uia_ct(control_type: str) -> bool:
    if not control_type:
        return False
    return control_type.strip().lower() in GENERIC_UIA_CONTROL_TYPES


# ──────────────────────────────────────────────────────────────────────
# Selección del target_bbox
# ──────────────────────────────────────────────────────────────────────

def _bbox_from_uia(uia: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    """Extrae bbox absoluto desde ``uia`` (admite ``bbox`` o el ``rect`` legacy)."""
    if not uia:
        return None
    bb = uia.get("bbox")
    if isinstance(bb, dict) and bb.get("width") and bb.get("height"):
        return {
            "left": int(bb.get("left", 0)),
            "top": int(bb.get("top", 0)),
            "width": int(bb.get("width", 0)),
            "height": int(bb.get("height", 0)),
        }
    return None


def _bbox_from_web(web: Optional[Dict[str, Any]],
                   *, window_bbox: Optional[Dict[str, int]]) -> Optional[Dict[str, int]]:
    """Convierte ``web.bbox`` (coords absolutas DOM, scrollX/Y inclusive)
    a coords absolutas de pantalla usando ``window_bbox`` y los offsets de scroll.

    El web_recorder guarda ``bbox`` con scroll incluido (líneas 289-292 del JS):
        {left: rect.left + window.scrollX, ...}.

    Para volver al viewport: restamos scroll. Para volver a coords de pantalla:
    sumamos el origen de la viewport del navegador (≈ window_bbox.left/top).

    No tenemos viewport offset exacto (chrome también tiene barras), pero el
    error suele ser <30px y el margen contextual lo absorbe. Si no hay
    ``window_bbox`` o las llaves no están bien, devolvemos None.
    """
    if not web or not isinstance(web, dict):
        return None
    bb = web.get("bbox")
    if not isinstance(bb, dict):
        return None
    w = int(bb.get("width") or 0)
    h = int(bb.get("height") or 0)
    if w <= 0 or h <= 0:
        return None
    sx = int(web.get("scroll_x") or 0)
    sy = int(web.get("scroll_y") or 0)
    # bbox en viewport
    viewport_left = int(bb.get("left", 0)) - sx
    viewport_top = int(bb.get("top", 0)) - sy
    # offset de la ventana del navegador
    win_left = int((window_bbox or {}).get("left", 0))
    win_top = int((window_bbox or {}).get("top", 0))
    return {
        "left": viewport_left + win_left,
        "top": viewport_top + win_top,
        "width": w,
        "height": h,
    }


def _is_bbox_acceptable(
    bbox: Dict[str, int],
    *,
    window_bbox: Optional[Dict[str, int]],
    screen_size: Tuple[int, int],
    is_canvas_action: bool = False,
) -> Tuple[bool, str]:
    """¿Es ``bbox`` un target válido? Returns (ok, reason_if_not)."""
    if not bbox:
        return False, "no_bbox"
    w = int(bbox.get("width") or 0)
    h = int(bbox.get("height") or 0)
    if w < MIN_TARGET_DIM_PX or h < MIN_TARGET_DIM_PX:
        return False, "too_small"

    if is_canvas_action:
        return True, ""  # bypass del check de área

    area = w * h
    # Comparamos con window si está; si no, con screen.
    if window_bbox:
        ref_area = max(1, _bbox_area(window_bbox))
    else:
        ref_area = max(1, int(screen_size[0]) * int(screen_size[1]))
    if area > MAX_TARGET_AREA_RATIO * ref_area:
        return False, "too_large_container"

    return True, ""


def select_target_bbox(
    *,
    click_xy: Tuple[int, int],
    uia_metadata: Optional[Dict[str, Any]] = None,
    web_metadata: Optional[Dict[str, Any]] = None,
    ocr_candidates: Optional[List[TargetCandidate]] = None,
    vision_candidates: Optional[List[TargetCandidate]] = None,
    window_bbox: Optional[Dict[str, int]] = None,
    screen_size: Tuple[int, int] = (1920, 1080),
    is_canvas_action: bool = False,
) -> TargetCandidate:
    """Devuelve el mejor ``TargetCandidate`` aplicando la prioridad UIA → DOM
    → OCR → vision → click_fallback y rechazando contenedores inválidos.

    Nunca devuelve ``None``: si nada califica, sintetiza un ``click_fallback``
    cuadrado alrededor del click. El caller decide qué hacer con el
    ``quality_level`` resultante.
    """
    cx, cy = int(click_xy[0]), int(click_xy[1])

    # ── 1. UIA ────────────────────────────────────────────────────
    if uia_metadata:
        ct = str(uia_metadata.get("control_type", "")).strip()
        name = str(uia_metadata.get("name", "")).strip()
        aid = str(uia_metadata.get("automation_id", "")).strip()
        bb = _bbox_from_uia(uia_metadata)
        # Generic CT es solo descalificador si NO hay AutomationId rescate.
        is_generic = _is_generic_uia_ct(ct) and not aid
        is_technical_only = (
            _is_technical_label(name)
            and _is_technical_label(aid)  # ambos técnicos = sin rescate
        ) or (_is_technical_label(name) and not aid)
        ok, reason = (False, "no_bbox") if bb is None else _is_bbox_acceptable(
            bb, window_bbox=window_bbox, screen_size=screen_size,
            is_canvas_action=is_canvas_action,
        )
        if bb is not None and ok and not is_generic and not is_technical_only \
                and _bbox_contains_point(bb, cx, cy):
            return TargetCandidate(
                bbox=bb, source=SRC_UIA,
                label=name or aid,
                role=ct.lower(),
                confidence=0.95,
            )

    # ── 2. DOM (web) ──────────────────────────────────────────────
    if web_metadata:
        bb = _bbox_from_web(web_metadata, window_bbox=window_bbox)
        ok, reason = (False, "no_bbox") if bb is None else _is_bbox_acceptable(
            bb, window_bbox=window_bbox, screen_size=screen_size,
            is_canvas_action=is_canvas_action,
        )
        # El bbox web suele caer ligeramente fuera por chrome bars; toleramos
        # 24px de slack para el contains-point.
        contains = bb is not None and (
            _bbox_contains_point(bb, cx, cy)
            or _bbox_contains_point(_inflate(bb, 24), cx, cy)
        )
        label = str(
            (web_metadata.get("accessible_name")
             or web_metadata.get("label")
             or web_metadata.get("placeholder")
             or web_metadata.get("test_id") or "")
        ).strip()
        if bb is not None and ok and contains \
                and not _is_technical_label(label):
            return TargetCandidate(
                bbox=bb, source=SRC_DOM,
                label=label,
                role=str(web_metadata.get("role", "")).lower(),
                confidence=0.9,
            )

    # ── 3. OCR ────────────────────────────────────────────────────
    for cand in (ocr_candidates or []):
        ok, _ = _is_bbox_acceptable(
            cand.bbox, window_bbox=window_bbox, screen_size=screen_size,
            is_canvas_action=is_canvas_action,
        )
        if ok and _bbox_contains_point(cand.bbox, cx, cy) \
                and not _is_technical_label(cand.label):
            return TargetCandidate(
                bbox=cand.bbox, source=SRC_OCR,
                label=cand.label, role=cand.role,
                confidence=cand.confidence or 0.75,
            )

    # ── 4. Vision/icon ────────────────────────────────────────────
    for cand in (vision_candidates or []):
        ok, _ = _is_bbox_acceptable(
            cand.bbox, window_bbox=window_bbox, screen_size=screen_size,
            is_canvas_action=is_canvas_action,
        )
        if ok and _bbox_contains_point(cand.bbox, cx, cy):
            return TargetCandidate(
                bbox=cand.bbox, source=SRC_VISION,
                label=cand.label, role=cand.role,
                confidence=cand.confidence or 0.6,
            )

    # ── 5. click_fallback ─────────────────────────────────────────
    half = CLICK_FALLBACK_SIZE_PX // 2
    fb_bbox = {
        "left": cx - half,
        "top": cy - half,
        "width": CLICK_FALLBACK_SIZE_PX,
        "height": CLICK_FALLBACK_SIZE_PX,
    }
    fb_bbox = _clamp_bbox_to_screen(fb_bbox, screen_size)
    return TargetCandidate(
        bbox=fb_bbox, source=SRC_CLICK_FALLBACK,
        label="", role="",
        confidence=0.4,
    )


def _inflate(bbox: Dict[str, int], px: int) -> Dict[str, int]:
    return {
        "left": int(bbox.get("left", 0)) - int(px),
        "top": int(bbox.get("top", 0)) - int(px),
        "width": int(bbox.get("width", 0)) + 2 * int(px),
        "height": int(bbox.get("height", 0)) + 2 * int(px),
    }


# ──────────────────────────────────────────────────────────────────────
# Cálculo del asset_bbox (bbox + margen contextual)
# ──────────────────────────────────────────────────────────────────────

def compute_asset_bbox(
    target_bbox: Dict[str, int],
    *,
    screen_size: Tuple[int, int],
    window_bbox: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """``target_bbox`` + ``margin = max(40, 20%·dim)``, capped 160, clamped."""
    w = int(target_bbox.get("width") or 0)
    h = int(target_bbox.get("height") or 0)
    margin_x = max(MIN_MARGIN_PX, int(MARGIN_RATIO * w))
    margin_y = max(MIN_MARGIN_PX, int(MARGIN_RATIO * h))
    margin_x = min(margin_x, MAX_MARGIN_PX)
    margin_y = min(margin_y, MAX_MARGIN_PX)
    asset = _inflate(target_bbox, max(margin_x, margin_y))
    # Recorte primero a window (si la hay), luego a screen.
    if window_bbox:
        asset = _intersect_with_window(asset, window_bbox)
    asset = _clamp_bbox_to_screen(asset, screen_size)
    return asset


def _intersect_with_window(
    bbox: Dict[str, int], window_bbox: Dict[str, int],
) -> Dict[str, int]:
    wl = int(window_bbox.get("left", 0))
    wt = int(window_bbox.get("top", 0))
    wr = wl + int(window_bbox.get("width", 0))
    wb = wt + int(window_bbox.get("height", 0))
    bl = int(bbox.get("left", 0))
    bt = int(bbox.get("top", 0))
    br = bl + int(bbox.get("width", 0))
    bb = bt + int(bbox.get("height", 0))
    left = max(wl, bl)
    top = max(wt, bt)
    right = min(wr, br)
    bottom = min(wb, bb)
    return {
        "left": left,
        "top": top,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
    }


# ──────────────────────────────────────────────────────────────────────
# Captura de las 3 imágenes
# ──────────────────────────────────────────────────────────────────────

def capture_visual_assets(
    *,
    target_bbox: Dict[str, int],
    asset_bbox: Dict[str, int],
    full_bbox: Dict[str, int],
    assets_dir: Path,
    base_name: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Guarda 3 PNGs en ``assets_dir`` y devuelve sus paths como str.

    - ``small`` = ``target_bbox`` exacto.
    - ``mid``   = ``asset_bbox`` (target + margen contextual).
    - ``full``  = ``full_bbox``  (ventana / pantalla completa).

    Si una captura individual falla, devuelve ``None`` para esa entrada
    pero NO aborta las otras (mejor 2 imágenes que ninguna).
    """
    try:
        from PIL import ImageGrab  # type: ignore
    except Exception:
        return None, None, None

    assets_dir.mkdir(parents=True, exist_ok=True)
    base = base_name or f"step_{uuid.uuid4().hex[:8]}"

    def _grab(bbox: Dict[str, int], suffix: str) -> Optional[str]:
        try:
            w = int(bbox.get("width") or 0)
            h = int(bbox.get("height") or 0)
            if w <= 0 or h <= 0:
                return None
            left = int(bbox.get("left", 0))
            top = int(bbox.get("top", 0))
            right = left + w
            bottom = top + h
            try:
                img = ImageGrab.grab(
                    bbox=(left, top, right, bottom), all_screens=True,
                )
            except TypeError:  # PIL < 8 sin all_screens
                img = ImageGrab.grab(bbox=(left, top, right, bottom))
            path = assets_dir / f"{base}_{suffix}.png"
            img.save(path)
            return str(path)
        except Exception:
            return None

    small_path = _grab(target_bbox, "small")
    mid_path = _grab(asset_bbox, "mid")
    full_path = _grab(full_bbox, "full")
    return small_path, mid_path, full_path


# ──────────────────────────────────────────────────────────────────────
# Validación
# ──────────────────────────────────────────────────────────────────────

def validate_visual_capture(captured: CapturedVisual) -> List[str]:
    """Devuelve lista de errores. Vacía = todo bien.

    Reglas (PRD §6):
        - click_xy ∈ target_bbox (con tolerancia para click_fallback)
        - target_bbox ⊆ asset_bbox
        - asset_bbox dentro de la pantalla
        - imagen abre (si hay path)
    """
    errors: List[str] = []

    cx, cy = captured.click_xy
    if captured.source != SRC_CLICK_FALLBACK:
        if not _bbox_contains_point(captured.target_bbox, cx, cy):
            errors.append("click_xy_outside_target_bbox")

    if not _bbox_contains_bbox(captured.asset_bbox, captured.target_bbox):
        # El asset_bbox puede haberse recortado a la pantalla y eso ES ok
        # mientras el bbox que SÍ entra en la pantalla siga conteniendo al
        # target visible. Solo marcamos error si no se solape lo suficiente.
        if not _bbox_overlaps_significantly(
            captured.asset_bbox, captured.target_bbox, ratio=0.7,
        ):
            errors.append("asset_bbox_does_not_contain_target")

    if captured.screen_size:
        sw, sh = captured.screen_size
        ab = captured.asset_bbox
        if (int(ab.get("left", 0)) < 0
                or int(ab.get("top", 0)) < 0
                or int(ab.get("left", 0)) + int(ab.get("width", 0)) > sw
                or int(ab.get("top", 0)) + int(ab.get("height", 0)) > sh):
            errors.append("asset_bbox_outside_screen")

    for label, path in (
        ("small", captured.small_path),
        ("mid", captured.mid_path),
        ("full", captured.full_path),
    ):
        if not path:
            continue
        try:
            from PIL import Image  # type: ignore
            with Image.open(path) as im:
                im.verify()
        except Exception:
            errors.append(f"asset_{label}_not_readable")

    return errors


def _bbox_overlaps_significantly(
    outer: Dict[str, int], inner: Dict[str, int], *, ratio: float = 0.7,
) -> bool:
    """¿Más del ``ratio`` del área del ``inner`` está dentro de ``outer``?"""
    inner_area = max(1, _bbox_area(inner))
    inter = _bbox_intersection(outer, inner)
    return _bbox_area(inter) >= ratio * inner_area


def _bbox_intersection(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    al = int(a.get("left", 0))
    at = int(a.get("top", 0))
    ar = al + int(a.get("width", 0))
    ab = at + int(a.get("height", 0))
    bl = int(b.get("left", 0))
    bt = int(b.get("top", 0))
    br = bl + int(b.get("width", 0))
    bb = bt + int(b.get("height", 0))
    left = max(al, bl)
    top = max(at, bt)
    right = min(ar, br)
    bottom = min(ab, bb)
    return {
        "left": left, "top": top,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
    }


# ──────────────────────────────────────────────────────────────────────
# Quality level
# ──────────────────────────────────────────────────────────────────────

def assess_quality(
    *, source: str, errors: List[str], have_small: bool, have_mid: bool,
) -> Tuple[str, bool]:
    """Devuelve (quality_level, needs_reinforcement) según fuente y errores."""
    if source == SRC_CLICK_FALLBACK:
        return "low", True
    if errors:
        return "medium", False
    if not have_small or not have_mid:
        return "medium", False
    if source in (SRC_UIA, SRC_DOM):
        return "high", False
    return "medium", False


# ──────────────────────────────────────────────────────────────────────
# Orquestador público
# ──────────────────────────────────────────────────────────────────────

def capture_visual_target(
    *,
    click_xy: Tuple[int, int],
    uia_metadata: Optional[Dict[str, Any]] = None,
    web_metadata: Optional[Dict[str, Any]] = None,
    ocr_candidates: Optional[List[TargetCandidate]] = None,
    vision_candidates: Optional[List[TargetCandidate]] = None,
    window_bbox: Optional[Dict[str, int]] = None,
    screen_size: Optional[Tuple[int, int]] = None,
    dpi_scale: Optional[float] = None,
    is_canvas_action: bool = False,
    assets_dir: Optional[Path] = None,
    base_name: Optional[str] = None,
    capture_files: bool = True,
) -> CapturedVisual:
    """Pipeline completo: selección → margen → captura → validación.

    Si ``capture_files`` es ``False`` (tests, dry-run), se omite la
    escritura de PNGs y se devuelven solo metadatos. La validación
    también se relaja para no marcar archivos faltantes como error.
    """
    cx, cy = int(click_xy[0]), int(click_xy[1])
    sz = screen_size or _detect_screen_size()

    # 1. seleccionar target_bbox
    cand = select_target_bbox(
        click_xy=(cx, cy),
        uia_metadata=uia_metadata,
        web_metadata=web_metadata,
        ocr_candidates=ocr_candidates,
        vision_candidates=vision_candidates,
        window_bbox=window_bbox,
        screen_size=sz,
        is_canvas_action=is_canvas_action,
    )

    target_bbox = dict(cand.bbox)
    asset_bbox = compute_asset_bbox(
        target_bbox, screen_size=sz, window_bbox=window_bbox,
    )

    # 2. full bbox = ventana activa, o pantalla si no se conoce
    full_bbox = dict(window_bbox) if window_bbox \
        else {"left": 0, "top": 0, "width": int(sz[0]), "height": int(sz[1])}
    full_bbox = _clamp_bbox_to_screen(full_bbox, sz)

    # 3. capturar imágenes
    small_path = mid_path = full_path = None
    if capture_files and assets_dir is not None:
        small_path, mid_path, full_path = capture_visual_assets(
            target_bbox=target_bbox,
            asset_bbox=asset_bbox,
            full_bbox=full_bbox,
            assets_dir=assets_dir,
            base_name=base_name,
        )

    # 4. offsets
    cx_box = int(target_bbox.get("left", 0)) + int(target_bbox.get("width", 0)) // 2
    cy_box = int(target_bbox.get("top", 0)) + int(target_bbox.get("height", 0)) // 2
    offset_px = (cx - cx_box, cy - cy_box)
    tw = max(1, int(target_bbox.get("width", 1)))
    th = max(1, int(target_bbox.get("height", 1)))
    fx = (cx - int(target_bbox.get("left", 0))) / tw
    fy = (cy - int(target_bbox.get("top", 0))) / th
    fx = max(0.0, min(1.0, fx))
    fy = max(0.0, min(1.0, fy))

    captured = CapturedVisual(
        target_bbox=target_bbox,
        asset_bbox=asset_bbox,
        click_xy=(cx, cy),
        click_offset_px=offset_px,
        click_offset_ratio=(fx, fy),
        source=cand.source,
        quality_level="high",          # provisional, se ajusta tras validar
        needs_reinforcement=False,
        small_path=small_path,
        mid_path=mid_path,
        full_path=full_path,
        dpi_scale=dpi_scale,
        screen_size=sz,
        window_bbox=window_bbox,
    )

    # 5. validar
    errors = validate_visual_capture(captured) if capture_files else []
    captured.validation_errors = errors
    quality, needs = assess_quality(
        source=cand.source,
        errors=errors,
        have_small=bool(small_path) or not capture_files,
        have_mid=bool(mid_path) or not capture_files,
    )
    captured.quality_level = quality
    captured.needs_reinforcement = needs
    return captured


# ──────────────────────────────────────────────────────────────────────
# Detección de pantalla
# ──────────────────────────────────────────────────────────────────────

def _detect_screen_size() -> Tuple[int, int]:
    """Best-effort: pyautogui → PyQt6 → fallback 1920×1080."""
    try:
        import pyautogui  # type: ignore
        sz = pyautogui.size()
        return int(sz.width), int(sz.height)
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            return (
                int(user32.GetSystemMetrics(0)),
                int(user32.GetSystemMetrics(1)),
            )
        except Exception:
            pass
    return (1920, 1080)


__all__ = [
    "CapturedVisual",
    "TargetCandidate",
    "SRC_UIA", "SRC_DOM", "SRC_OCR", "SRC_VISION", "SRC_CLICK_FALLBACK",
    "MIN_MARGIN_PX", "MAX_MARGIN_PX", "MARGIN_RATIO", "MAX_TARGET_AREA_RATIO",
    "GENERIC_UIA_CONTROL_TYPES", "TECHNICAL_NAME_PATTERNS",
    "select_target_bbox",
    "compute_asset_bbox",
    "capture_visual_assets",
    "validate_visual_capture",
    "assess_quality",
    "capture_visual_target",
]
