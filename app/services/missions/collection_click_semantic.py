"""
Map weak-UIA collection clicks to semantic steps with ocr_collection strategy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.missions.ocr_collection_resolver import normalize_text
from app.services.missions.recorder_capture_contract import is_uia_weak
from app.services.missions.vision.text_detector import build_candidates_digest

STRATEGY_OCR_COLLECTION = "ocr_collection"

_PROFILE_PICKER_TOKENS = frozenset({
    "perfil", "perfiles", "profile", "profiles",
    "seleccionar perfil", "select profile", "choose profile",
    "elige un perfil", "chrome",
})


def _best_label_from_candidates(
    candidates: List[Dict[str, Any]],
    click_xy: Optional[tuple] = None,
) -> str:
    """Pick the longest meaningful candidate near click (if coords known)."""
    scored: List[tuple] = []
    for c in candidates:
        text = str(c.get("text") or "").strip()
        if len(text) < 2:
            continue
        norm = normalize_text(text)
        if norm in _PROFILE_PICKER_TOKENS:
            continue
        if any(tok in norm for tok in ("add profile", "agregar perfil", "guest")):
            continue
        score = len(text)
        if click_xy and isinstance(c.get("bbox"), dict):
            bb = c["bbox"]
            cx = int(bb.get("left", 0)) + int(bb.get("width", 0)) // 2
            cy = int(bb.get("top", 0)) + int(bb.get("height", 0)) // 2
            dist = ((cx - click_xy[0]) ** 2 + (cy - click_xy[1]) ** 2) ** 0.5
            score += max(0, 200 - dist) / 10
        scored.append((score, text, c.get("bbox")))
    if not scored:
        return ""
    scored.sort(key=lambda t: -t[0])
    return scored[0][1]


def is_collection_click(metadata: Dict[str, Any]) -> bool:
    """True when click looks like a list/tile item (weak UIA + OCR candidates)."""
    precap = metadata.get("precapture") or {}
    candidates = precap.get("collection_candidates") or []
    if not candidates:
        return False
    return is_uia_weak(
        metadata.get("uia"),
        target_source=metadata.get("target_source"),
        quality_level=metadata.get("quality_level"),
    )


def build_collection_semantic_step(
    metadata: Dict[str, Any],
    *,
    click_xy: Optional[tuple] = None,
    default_app: str = "chrome",
) -> Optional[Dict[str, Any]]:
    """Build select_profile / click_collection_item args from precapture."""
    if not is_collection_click(metadata):
        return None

    precap = metadata.get("precapture") or {}
    candidates: List[Dict[str, Any]] = list(
        precap.get("collection_candidates") or []
    )
    label = _best_label_from_candidates(candidates, click_xy)
    if not label:
        ocr_lines = (
            (metadata.get("vision_data") or {}).get("ocr_lines") or []
        )
        for ln in ocr_lines:
            s = str(ln or "").strip()
            if len(s) >= 3 and normalize_text(s) not in _PROFILE_PICKER_TOKENS:
                label = s
                break
    if not label:
        return None

    bbox_hint: Optional[Dict[str, int]] = None
    for c in candidates:
        if normalize_text(str(c.get("text") or "")) == normalize_text(label):
            bb = c.get("bbox")
            if isinstance(bb, dict):
                bbox_hint = dict(bb)
                break

    digest = precap.get("candidates_digest") or build_candidates_digest(candidates)
    frames = list(precap.get("frames") or [])

    # Profile picker heuristic
    title = str((metadata.get("target_identity") or {}).get("title") or "")
    proc = str((metadata.get("target_identity") or {}).get("process") or "")
    is_profile = (
        "profile" in normalize_text(title)
        or "perfil" in normalize_text(title)
        or "chrome" in normalize_text(proc)
        or any("perfil" in normalize_text(str(c.get("text") or "")) for c in candidates)
    )

    step_type = "select_profile" if is_profile else "click_collection_item"
    params: Dict[str, Any] = {
        "strategy": STRATEGY_OCR_COLLECTION,
        "label_text": label,
        "candidates_digest": digest,
        "precapture_frames": frames,
        "collection_candidates": candidates,
    }
    if bbox_hint:
        params["bbox_hint"] = bbox_hint
    if step_type == "select_profile":
        params["profile"] = label
        params["app"] = default_app

    return {
        "type": step_type,
        "params": params,
        "confidence": 0.88,
        "human_label": f"Seleccionar {label}",
        "evidence": {"strategy": STRATEGY_OCR_COLLECTION, "candidates": len(candidates)},
    }


__all__ = [
    "STRATEGY_OCR_COLLECTION",
    "is_collection_click",
    "build_collection_semantic_step",
]
