"""Optional vision plugins (OCR, detectors). No hard deps for CI."""

from app.services.missions.vision.text_detector import (
    FakeDetector,
    NullDetector,
    TextDetection,
    TextDetector,
    get_default_detector,
)

__all__ = [
    "FakeDetector",
    "NullDetector",
    "TextDetection",
    "TextDetector",
    "get_default_detector",
]
