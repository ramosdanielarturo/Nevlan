"""
Nevlan — Text Detector plugin interface (optional OCR).

All real OCR backends are lazy-imported. CI uses :class:`NullDetector`
or :class:`FakeDetector` in tests — no pytesseract/tesseract required.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class TextDetection:
    """Single text region detected in an image."""

    text: str
    bbox: Dict[str, int]
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "bbox": dict(self.bbox),
            "confidence": float(self.confidence),
        }


@runtime_checkable
class TextDetector(Protocol):
    """Detect text regions in a screenshot path."""

    def detect(self, image_path: str) -> List[TextDetection]:
        ...


class NullDetector:
    """Default no-op detector — safe for CI."""

    def detect(self, image_path: str) -> List[TextDetection]:
        _ = image_path
        return []


class FakeDetector:
    """Deterministic detector for unit tests.

    ``fixtures`` maps image_path -> list of detections (dict or TextDetection).
    """

    def __init__(
        self,
        fixtures: Optional[Dict[str, List[Any]]] = None,
    ) -> None:
        self._fixtures = dict(fixtures or {})

    def detect(self, image_path: str) -> List[TextDetection]:
        raw = self._fixtures.get(image_path, [])
        out: List[TextDetection] = []
        for item in raw:
            if isinstance(item, TextDetection):
                out.append(item)
            elif isinstance(item, dict):
                out.append(TextDetection(
                    text=str(item.get("text") or ""),
                    bbox=dict(item.get("bbox") or {}),
                    confidence=float(item.get("confidence") or 1.0),
                ))
        return out


def _lazy_tesseract_detector() -> Optional[TextDetector]:
    try:
        from app.services.missions.vision.tesseract_detector import (  # noqa: WPS433
            TesseractDetector,
        )
        return TesseractDetector()
    except Exception:
        return None


_DEFAULT: Optional[TextDetector] = None


def get_default_detector() -> TextDetector:
    """Return configured detector or NullDetector."""
    global _DEFAULT  # noqa: PLW0603
    if _DEFAULT is not None:
        return _DEFAULT
    det = _lazy_tesseract_detector()
    _DEFAULT = det if det is not None else NullDetector()
    return _DEFAULT


def set_default_detector(detector: Optional[TextDetector]) -> None:
    """Override default (tests / desktop bootstrap)."""
    global _DEFAULT  # noqa: PLW0603
    _DEFAULT = detector


def build_candidates_digest(candidates: List[Dict[str, Any]]) -> str:
    """Stable short digest of collection OCR candidates."""
    payload = [
        {
            "text": str(c.get("text") or ""),
            "bbox": dict(c.get("bbox") or {}),
        }
        for c in candidates
    ]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "TextDetection",
    "TextDetector",
    "NullDetector",
    "FakeDetector",
    "get_default_detector",
    "set_default_detector",
    "build_candidates_digest",
]
