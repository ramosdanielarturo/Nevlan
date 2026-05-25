"""Pre-click frames + OCR collection resolver (FakeDetector, no OCR real)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.missions.ocr_collection_resolver import ocr_collection_resolve
from app.services.missions.recorder import MouseListener
from app.services.missions.recorder_capture_contract import (
    CAPTURE_PHASE_PRE_CLICK,
    ScreenshotRingBuffer,
    attach_precapture_frames,
)
from app.services.missions.runtime_collection_errors import (
    COLLECTION_AMBIGUOUS,
    COLLECTION_ITEM_NOT_FOUND,
    OCR_DETECTOR_UNAVAILABLE,
)
from app.services.missions.vision.text_detector import FakeDetector, NullDetector


def test_preclick_frames_attached_on_mousedown(tmp_path) -> None:
    """Ring buffer → attach_precapture_frames escribe metadata.precapture (3 frames)."""
    clock = {"t": 5000}
    injected: list[str] = []

    for i in range(3):
        clock["t"] = 4600 + i * 100
        p = tmp_path / f"inj_{i}.png"
        p.write_bytes(b"png")
        injected.append(str(p))

    buf = ScreenshotRingBuffer(
        tmp_path,
        capacity=5,
        period_ms=100,
        grabber=lambda d: None,
        clock_ms=lambda: clock["t"],
    )
    for i, path in enumerate(injected):
        buf.push(path, 4600 + i * 100)

    frames = buf.get_last_n_before(5000, 3)
    assert len(frames) == 3

    meta: dict = {}
    attach_precapture_frames(
        meta,
        frames,
        collection_candidates=[
            {
                "text": "Daniel Arturo Ramos",
                "bbox": {"left": 1, "top": 2, "width": 10, "height": 8},
            },
        ],
    )
    precap = meta["precapture"]
    assert precap["phase"] == CAPTURE_PHASE_PRE_CLICK
    assert len(precap["frames"]) == 3
    assert len(precap["ts"]) == 3
    assert precap["candidates_digest"]

    # Mousedown path stores preclick_frames in _pending_click
    listener = MouseListener(callback=MagicMock(), mission_id="m-test", pre_buffer=buf)
    with listener._pending_click_lock:
        listener._pending_click = {
            "x": 10, "y": 20, "button": "left", "t_down": 5.0,
            "window_ctx": MagicMock(),
            "before_anchor": MagicMock(phase=CAPTURE_PHASE_PRE_CLICK),
            "preclick_frames": frames,
        }
    got = listener._pending_click
    assert got is not None
    assert len(got["preclick_frames"]) == 3


def test_ocr_collection_resolve_unique_match() -> None:
    img = "/fake/screen.png"
    det = FakeDetector({
        img: [
            {"text": "Guest", "bbox": {"left": 0, "top": 0, "width": 50, "height": 20}},
            {"text": "Daniel Arturo Ramos", "bbox": {"left": 100, "top": 200, "width": 180, "height": 40}},
        ],
    })
    result = ocr_collection_resolve(
        "Daniel Arturo Ramos",
        [img],
        detector=det,
    )
    assert result.ok is True
    assert result.bbox == {"left": 100, "top": 200, "width": 180, "height": 40}
    assert result.click_xy == (190, 220)


def test_ocr_collection_ambiguous_returns_typed_error() -> None:
    img = "/fake/screen.png"
    det = FakeDetector({
        img: [
            {"text": "Daniel Arturo Ramos", "bbox": {"left": 10, "top": 10, "width": 100, "height": 30}},
            {"text": "Daniel Arturo Ramos Work", "bbox": {"left": 10, "top": 60, "width": 100, "height": 30}},
        ],
    })
    result = ocr_collection_resolve(
        "Daniel Arturo Ramos",
        [img],
        detector=det,
    )
    assert result.ok is False
    assert result.error_code == COLLECTION_AMBIGUOUS
    assert len(result.matches) >= 2


def test_ocr_collection_not_found_returns_typed_error() -> None:
    img = "/fake/screen.png"
    det = FakeDetector({
        img: [{"text": "Other User", "bbox": {"left": 0, "top": 0, "width": 50, "height": 20}}],
    })
    result = ocr_collection_resolve(
        "Daniel Arturo Ramos",
        [img],
        detector=det,
    )
    assert result.ok is False
    assert result.error_code == COLLECTION_ITEM_NOT_FOUND


def test_ocr_collection_null_detector_without_stored_candidates() -> None:
    result = ocr_collection_resolve(
        "Daniel Arturo Ramos",
        [],
        detector=NullDetector(),
    )
    assert result.ok is False
    assert result.error_code == OCR_DETECTOR_UNAVAILABLE
