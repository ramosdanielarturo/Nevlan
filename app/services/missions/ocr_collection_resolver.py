"""
Nevlan — OCR Collection Resolver
================================

Resuelve clicks en listas/tiles (Chrome profile picker, grids) buscando
``label_text`` en detecciones OCR con bbox — sin coordenadas como
estrategia primaria.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.missions.runtime_collection_errors import (
    COLLECTION_AMBIGUOUS,
    COLLECTION_ITEM_NOT_FOUND,
    OCR_DETECTOR_UNAVAILABLE,
    REPAIR_SUGGESTIONS,
)
from app.services.missions.vision.text_detector import (
    NullDetector,
    TextDetection,
    TextDetector,
    get_default_detector,
)

# Minimum score to count as a match (token overlap ratio).
DEFAULT_MATCH_THRESHOLD: float = 0.55

# If top two scores differ by less than this, report ambiguous.
AMBIGUITY_SCORE_DELTA: float = 0.08


@dataclass
class CollectionResolveResult:
    ok: bool
    bbox: Optional[Dict[str, int]] = None
    click_xy: Optional[Tuple[int, int]] = None
    error_code: str = ""
    message: str = ""
    suggestion: str = ""
    matches: List[Dict[str, Any]] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "bbox": self.bbox,
            "click_xy": self.click_xy,
            "error_code": self.error_code or None,
            "message": self.message,
            "suggestion": self.suggestion or None,
            "matches": list(self.matches),
            "score": self.score,
        }


def normalize_text(text: str) -> str:
    """Lower, strip accents, collapse whitespace."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    collapsed = re.sub(r"\s+", " ", stripped.lower().strip())
    return collapsed


def _token_overlap_score(label: str, candidate: str) -> float:
    """Score in [0, 1] via token overlap + substring bonus."""
    nl = normalize_text(label)
    nc = normalize_text(candidate)
    if not nl or not nc:
        return 0.0
    if nl == nc:
        return 1.0
    if nl in nc or nc in nl:
        return 0.92
    lt = set(nl.split())
    ct = set(nc.split())
    if not lt:
        return 0.0
    overlap = len(lt & ct) / len(lt)
    return float(overlap)


def _bbox_center(bbox: Dict[str, int]) -> Tuple[int, int]:
    left = int(bbox.get("left") or 0)
    top = int(bbox.get("top") or 0)
    w = int(bbox.get("width") or 0)
    h = int(bbox.get("height") or 0)
    return left + w // 2, top + h // 2


def _collect_detections(
    image_paths: Sequence[str],
    detector: TextDetector,
) -> List[TextDetection]:
    seen: set = set()
    out: List[TextDetection] = []
    for path in image_paths:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            out.extend(detector.detect(path))
        except Exception:
            continue
    return out


def ocr_collection_resolve(
    label_text: str,
    image_paths: Sequence[str],
    *,
    detector: Optional[TextDetector] = None,
    match_threshold: float = DEFAULT_MATCH_THRESHOLD,
    stored_candidates: Optional[Sequence[Dict[str, Any]]] = None,
) -> CollectionResolveResult:
    """Find ``label_text`` in OCR detections and return click bbox.

    Priority:
      1. Run ``detector`` on ``image_paths`` (live screen).
      2. Fall back to ``stored_candidates`` from recording metadata.

    Never returns coords-only fallback — typed errors on failure.
    """
    label = (label_text or "").strip()
    if not label:
        return CollectionResolveResult(
            ok=False,
            error_code=COLLECTION_ITEM_NOT_FOUND,
            message="label_text vacío",
            suggestion=REPAIR_SUGGESTIONS[COLLECTION_ITEM_NOT_FOUND],
        )

    det = detector if detector is not None else get_default_detector()
    if isinstance(det, NullDetector) and not stored_candidates:
        return CollectionResolveResult(
            ok=False,
            error_code=OCR_DETECTOR_UNAVAILABLE,
            message="OCR detector no disponible (NullDetector)",
            suggestion=REPAIR_SUGGESTIONS[OCR_DETECTOR_UNAVAILABLE],
        )

    detections: List[TextDetection] = []
    if image_paths:
        detections = _collect_detections(image_paths, det)

    if not detections and stored_candidates:
        for c in stored_candidates:
            text = str(c.get("text") or c.get("label_text") or "").strip()
            bbox = dict(c.get("bbox") or c.get("bbox_hint") or {})
            if text and bbox:
                detections.append(TextDetection(
                    text=text,
                    bbox=bbox,
                    confidence=float(c.get("confidence") or 1.0),
                ))

    if not detections:
        return CollectionResolveResult(
            ok=False,
            error_code=OCR_DETECTOR_UNAVAILABLE
            if isinstance(det, NullDetector)
            else COLLECTION_ITEM_NOT_FOUND,
            message="Sin detecciones OCR en frames",
            suggestion=REPAIR_SUGGESTIONS.get(
                OCR_DETECTOR_UNAVAILABLE
                if isinstance(det, NullDetector)
                else COLLECTION_ITEM_NOT_FOUND,
                "",
            ),
        )

    scored: List[Tuple[float, TextDetection]] = []
    for d in detections:
        sc = _token_overlap_score(label, d.text)
        if sc >= match_threshold:
            scored.append((sc, d))

    if not scored:
        return CollectionResolveResult(
            ok=False,
            error_code=COLLECTION_ITEM_NOT_FOUND,
            message=f"No se encontró '{label}' en pantalla",
            suggestion=REPAIR_SUGGESTIONS[COLLECTION_ITEM_NOT_FOUND],
            matches=[d.to_dict() for d in detections[:8]],
        )

    scored.sort(key=lambda t: (-t[0], -t[1].confidence))
    top_score, top_det = scored[0]

    if len(scored) >= 2:
        second_score = scored[1][0]
        if (top_score - second_score) < AMBIGUITY_SCORE_DELTA:
            return CollectionResolveResult(
                ok=False,
                error_code=COLLECTION_AMBIGUOUS,
                message=(
                    f"Ambiguo: '{label}' coincide con "
                    f"{len(scored)} elementos"
                ),
                suggestion=REPAIR_SUGGESTIONS[COLLECTION_AMBIGUOUS],
                matches=[
                    {"text": d.text, "bbox": d.bbox, "score": sc}
                    for sc, d in scored[:5]
                ],
                score=top_score,
            )

    cx, cy = _bbox_center(top_det.bbox)
    return CollectionResolveResult(
        ok=True,
        bbox=dict(top_det.bbox),
        click_xy=(cx, cy),
        score=top_score,
        matches=[{"text": top_det.text, "bbox": top_det.bbox, "score": top_score}],
    )


__all__ = [
    "CollectionResolveResult",
    "ocr_collection_resolve",
    "normalize_text",
    "DEFAULT_MATCH_THRESHOLD",
]
