"""
TargetSignature — firma portable para reproducir clic en el mismo objetivo humano.

Se construye durante la grabación (MouseListener + window context + vision refs)
y se serializa dentro de CompiledStep.target_context (dict en ``signature``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import sys
from datetime import datetime, timezone

def _pct(score: float) -> float:
    return max(0.0, min(100.0, score))


def gather_capture_environment_hints(
    *,
    hwnd_root: Optional[int] = None,
) -> Dict[str, Any]:
    """DPI, pantalla principal, timestamp — evita depender solo de píxeles absolutos."""
    out: Dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "dpi_scale": None,
        "monitor_id": None,
        "primary_screen_px": None,
    }
    try:
        import pyautogui  # type: ignore

        sz = pyautogui.size()
        out["primary_screen_px"] = {"w": int(sz.width), "h": int(sz.height)}
    except Exception:
        pass
    if sys.platform == "win32" and hwnd_root:
        try:
            import ctypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            dpi_scale = user32.GetDpiForWindow(int(hwnd_root))
            if dpi_scale:
                out["dpi_scale"] = round(float(dpi_scale) / 96.0, 3)
        except Exception:
            pass
    try:
        from PyQt6.QtWidgets import QApplication  # type: ignore

        app = QApplication.instance()
        scr = None
        if app:
            scr = app.primaryScreen()
        if scr:
            geo = scr.geometry()
            pix = geo.width() * geo.height()
            out["logical_screen_px"] = {"w": geo.width(), "h": geo.height()}
            out["dpi_scale"] = out["dpi_scale"] or round(
                scr.logicalDotsPerInchX() / 96.0, 3)
    except Exception:
        pass
    return out


def bbox_ratio_within_window(window_bbox: Dict[str, int], elem_bbox: Dict[str, int]) -> Dict[str, float]:
    """Fracciones 0–1 del bbox del elemento dentro de la ventana."""
    try:
        wl = window_bbox.get("left", 0)
        wt = window_bbox.get("top", 0)
        ww = max(1, int(window_bbox.get("width") or 1))
        wh = max(1, int(window_bbox.get("height") or 1))
        el = elem_bbox.get("left", wl)
        et = elem_bbox.get("top", wt)
        ew = max(1, int(elem_bbox.get("width") or 1))
        eh = max(1, int(elem_bbox.get("height") or 1))
        cx = (el + ew / 2 - wl) / ww
        cy = (et + eh / 2 - wt) / wh
        return {
            "center_fx_win": round(max(0.0, min(1.0, cx)), 4),
            "center_fy_win": round(max(0.0, min(1.0, cy)), 4),
            "area_ratio": round((ew * eh) / max(1.0, float(ww * wh)), 5),
        }
    except Exception:
        return {}


def build_target_signature(
    *,
    x: int,
    y: int,
    metadata: Dict[str, Any],
    window_ctx: Optional[Any],
    small_snapshot: Optional[str] = None,
    mid_snapshot: Optional[str] = None,
    hwnd_root: Optional[int] = None,
    small_px: int = 100,
    mid_px: int = 600,
) -> Dict[str, Any]:
    """Produce un dict serializable listo para target_context.signature."""
    reasons: List[str] = []

    uia = dict(metadata.get("uia") or {})
    bbox = metadata.get("anchor_bbox") or uia.get("bbox")
    ct = (uia.get("control_type") or "").strip()
    aid = (uia.get("automation_id") or "").strip()
    name = (uia.get("name") or "").strip()

    score = 0.0
    if aid:
        score += 30
        reasons.append("uia.automation_id")
    elif name:
        score += 15
        reasons.append("uia.name")
    if ct and ct.lower() not in ("groupcontrol", "panecontrol", "documentcontrol"):
        score += 10
        reasons.append("uia.control_type")
    if bbox and isinstance(bbox, dict) and bbox.get("width", 0) > 8:
        score += 10
        reasons.append("uia.bounding_box")

    web = metadata.get("web") or {}
    locs = web.get("locators") or []
    if isinstance(locs, list) and locs:
        score += 12
        reasons.append("web.locators")

    rel = metadata.get("rel_win") or {}
    has_rel = isinstance(rel, dict) and ("fx" in rel or "fy" in rel)
    if has_rel:
        score += 8
        reasons.append("coords.relative_window")

    if mid_snapshot or small_snapshot:
        score += 15
        reasons.append("vision.crops")

    off = metadata.get("click_offset")
    if isinstance(off, dict):
        reasons.append("click_offset_within_element")

    if not uia and not web and not (mid_snapshot or small_snapshot):
        score = max(score * 0.4, 5.0)
        reasons.append("weak:no_structured_target")

    if not any(r.startswith("coords") or r.startswith("vision") or r.startswith("uia.") for r in reasons):
        score -= 15
        reasons.append("-no_anchor_signal")

    quality = "red"
    if score >= 75:
        quality = "green"
    elif score >= 45:
        quality = "yellow"

    env = gather_capture_environment_hints(
        hwnd_root=hwnd_root or (getattr(window_ctx, "hwnd", None) if window_ctx else None),
    )
    win_bb: Optional[Dict[str, int]] = None
    try:
        if window_ctx and getattr(window_ctx, "bounding_box", None):
            win_bb = dict(window_ctx.bounding_box or {})  # type: ignore[arg-type]
    except Exception:
        win_bb = None
    elem_bb = bbox if isinstance(bbox, dict) else None
    layout_ratios = {}
    if win_bb and elem_bb:
        layout_ratios = bbox_ratio_within_window(win_bb, elem_bb)

    off = metadata.get("click_offset") if isinstance(metadata.get("click_offset"), dict) else {}
    ew = max(8, int((elem_bb or {}).get("width") or 0))
    eh = max(8, int((elem_bb or {}).get("height") or 0))
    ratio_off = {}
    try:
        if off:
            ratio_off["dx_frac_elem"] = round(float(off.get("dx", 0)) / ew, 4)
            ratio_off["dy_frac_elem"] = round(float(off.get("dy", 0)) / eh, 4)
    except Exception:
        ratio_off = {}

    snap_sz: Dict[str, Any] = {}
    theme_hint = ""
    if small_px:
        snap_sz["image_size_small_px"] = int(small_px)
    if mid_px:
        snap_sz["image_size_mid_px"] = int(mid_px)

    sig: Dict[str, Any] = {
        "uia": {
            "name": uia.get("name"),
            "automation_id": uia.get("automation_id"),
            "control_type": uia.get("control_type"),
            "class_name": uia.get("class_name"),
            "bbox": bbox if isinstance(bbox, dict) else None,
        },
        "text_context": {
            "near_visible": (web.get("accessible_name") or web.get("label")
                              or web.get("placeholder")),
            "ocr_anchor": metadata.get("ocr_anchor"),
            "associated_label": web.get("label"),
            "nearby_dom": web.get("nearby_text"),
        },
        "visual_assets": {
            "ref_small": small_snapshot,
            "ref_mid": mid_snapshot or metadata.get("snapshot_mid"),
        },
        "coordinates": {
            "absolute": {"x": int(x), "y": int(y)},
            "relative_window": metadata.get("rel_win"),
            "bbox_element": bbox if isinstance(bbox, dict) else None,
            "click_offset_in_bbox": metadata.get("click_offset"),
            "click_offset_ratio_in_element": ratio_off or None,
            "element_layout_in_window": layout_ratios or None,
        },
        "capture_environment": env,
        "image_sizes": snap_sz or None,
        "theme_hint": theme_hint or None,
        "window_process": {},
        "confidence": {
            "confidence_score": round(_pct(score), 1),
            "confidence_reasons": reasons,
            "target_quality": quality,
        },
    }

    try:
        if window_ctx is not None:
            sig["window_process"] = {
                "hwnd": getattr(window_ctx, "hwnd", None),
                "title": getattr(window_ctx, "title", None),
                "process_name": getattr(window_ctx, "process_name", None),
                "window_bbox": getattr(window_ctx, "bounding_box", None),
            }
    except Exception:
        pass

    return sig


@dataclass
class TargetSignatureEnvelope:
    """Referencia opcional para tests y futura promoción a tipado estricto."""

    blob: Dict[str, Any] = field(default_factory=dict)
