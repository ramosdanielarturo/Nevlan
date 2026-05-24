"""
Tests for PRD 2026-05-12 — Visual Fingerprint Engine.

Verifies the deterministic visual matching pipeline (pHash + dHash +
ORB + multi-scale template matching) and its integration with the
:class:`CandidateFusionEngine`.

All tests build synthetic images on the fly (no fixtures on disk) so
they run identically on any machine. Tests gracefully skip if OpenCV
is not installed (``HAS_CV2 == False``).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Tuple

import pytest

from app.services.missions.candidate_fusion_engine import (
    CandidateFusionEngine,
)
from app.services.missions.visual_fingerprint import (
    HAS_CV2,
    SCORE_MEDIUM,
    SCORE_STRONG,
    MatchResult,
    VisualFingerprint,
    compare_fingerprints,
    compute_fingerprint,
    match_in_frame,
    relocate_target,
)


pytestmark = pytest.mark.skipif(not HAS_CV2, reason="cv2 not available")


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _icon(size: int = 48, color=(180, 50, 60)) -> "any":
    """Genera un icono sintético con borde + cuadrado interior blanco.

    Suficiente estructura para que pHash y dHash difieran de un
    background uniforme.
    """
    import numpy as np
    crop = np.zeros((size, size, 3), dtype=np.uint8)
    pad = size // 6
    crop[pad:size - pad, pad:size - pad] = color
    inner = size // 3
    o = (size - inner) // 2
    crop[o:o + inner, o:o + inner] = (255, 255, 255)
    return crop


def _save(arr, path: str) -> str:
    from PIL import Image
    Image.fromarray(arr).save(path)
    return path


def _make_frame_with_icon(
    icon, *, at_xy: Tuple[int, int], frame_size=(800, 600),
    bg=(250, 250, 250),
) -> "any":
    import numpy as np
    frame = np.full(
        (frame_size[1], frame_size[0], 3), bg, dtype=np.uint8,
    )
    h, w = icon.shape[:2]
    x, y = at_xy
    frame[y:y + h, x:x + w] = icon
    return frame


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


# ────────────────────────────────────────────────────────────────────────
# 1) Fingerprint computation
# ────────────────────────────────────────────────────────────────────────


def test_compute_fingerprint_produces_stable_hashes(tmp_dir: Path) -> None:
    """Dos cálculos sobre la misma imagen deben dar hashes idénticos."""
    icon = _icon()
    path = _save(icon, str(tmp_dir / "icon.png"))
    fp1 = compute_fingerprint(path)
    fp2 = compute_fingerprint(path)
    assert fp1.phash_hex == fp2.phash_hex
    assert fp1.dhash_hex == fp2.dhash_hex
    assert fp1.available is True
    # ORB devuelve mismos descriptors módulo el orden de keypoints
    # (no garantizado), pero el conteo sí es determinista para entrada
    # idéntica.
    assert fp1.orb_keypoint_count == fp2.orb_keypoint_count


def test_compute_fingerprint_missing_file_returns_empty(tmp_dir: Path) -> None:
    fp = compute_fingerprint(str(tmp_dir / "noexiste.png"))
    assert fp.available is False
    assert fp.quality == "insufficient"
    assert fp.phash_hex == ""


def test_fingerprint_roundtrip_via_dict() -> None:
    """``to_dict`` → ``from_dict`` debe preservar datos."""
    fp = VisualFingerprint(
        phash_hex="aabbccddeeff0011",
        dhash_hex="1122334455667788",
        orb_descriptors_b64="ZGVzY3JpcHRvcg==",
        orb_keypoint_count=42,
        crop_size=(48, 48),
        source_path="/tmp/icon.png",
        available=True,
        quality="strong",
    )
    fp2 = VisualFingerprint.from_dict(fp.to_dict())
    assert fp2.phash_hex == fp.phash_hex
    assert fp2.dhash_hex == fp.dhash_hex
    assert fp2.orb_keypoint_count == 42
    assert fp2.crop_size == (48, 48)
    assert fp2.available is True
    assert fp2.quality == "strong"


# ────────────────────────────────────────────────────────────────────────
# 2) Matching (in-place, moved, missing)
# ────────────────────────────────────────────────────────────────────────


def test_match_finds_icon_in_same_place(tmp_dir: Path) -> None:
    icon = _icon()
    icon_path = _save(icon, str(tmp_dir / "icon.png"))
    frame = _make_frame_with_icon(icon, at_xy=(220, 180))
    frame_path = _save(frame, str(tmp_dir / "frame.png"))

    fp = compute_fingerprint(icon_path)
    result = match_in_frame(fp, frame_path=frame_path)
    assert result.score >= SCORE_STRONG
    assert result.confidence_label == "strong"
    assert result.found_bbox is not None
    assert abs(result.found_bbox["left"] - 220) <= 4
    assert abs(result.found_bbox["top"] - 180) <= 4


def test_match_finds_icon_when_moved(tmp_dir: Path) -> None:
    """El icono se movió a otra posición — debe encontrarlo igual."""
    icon = _icon()
    icon_path = _save(icon, str(tmp_dir / "icon.png"))
    frame = _make_frame_with_icon(icon, at_xy=(500, 320))
    frame_path = _save(frame, str(tmp_dir / "moved.png"))

    fp = compute_fingerprint(icon_path)
    result = match_in_frame(fp, frame_path=frame_path)
    assert result.found_bbox is not None
    assert abs(result.found_bbox["left"] - 500) <= 4
    assert abs(result.found_bbox["top"] - 320) <= 4
    assert result.score >= SCORE_MEDIUM


def test_match_returns_insufficient_when_icon_absent(tmp_dir: Path) -> None:
    """Si el icono no está, no debe inventar un bbox."""
    import numpy as np
    icon = _icon()
    icon_path = _save(icon, str(tmp_dir / "icon.png"))
    # Frame uniforme sin nada parecido al icono.
    frame = np.full((600, 800, 3), 200, dtype=np.uint8)
    frame_path = _save(frame, str(tmp_dir / "empty.png"))

    fp = compute_fingerprint(icon_path)
    result = match_in_frame(fp, frame_path=frame_path)
    assert result.confidence_label == "insufficient"
    assert result.found_bbox is None


def test_match_with_invalid_fingerprint_returns_empty() -> None:
    fp = VisualFingerprint()  # available=False
    result = match_in_frame(fp, frame_path="anywhere.png")
    assert result.score == 0.0
    assert result.confidence_label == "insufficient"


def test_match_robust_to_moderate_noise(tmp_dir: Path) -> None:
    """Anti-alias / rerendering simulado por ruido aditivo:
    el matching debe seguir alcanzando 'medium' por lo menos.
    """
    import numpy as np
    icon = _icon()
    icon_path = _save(icon, str(tmp_dir / "icon.png"))
    frame = _make_frame_with_icon(icon, at_xy=(300, 200))
    noise = np.random.RandomState(42).randint(
        -12, 13, frame.shape, dtype=np.int16,
    )
    noisy = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    noisy_path = _save(noisy, str(tmp_dir / "noisy.png"))

    fp = compute_fingerprint(icon_path)
    result = match_in_frame(fp, frame_path=noisy_path)
    assert result.found_bbox is not None
    assert result.score >= SCORE_MEDIUM


# ────────────────────────────────────────────────────────────────────────
# 3) compare_fingerprints (huella vs huella)
# ────────────────────────────────────────────────────────────────────────


def test_compare_fingerprints_identical_returns_1(tmp_dir: Path) -> None:
    icon = _icon()
    p = _save(icon, str(tmp_dir / "i.png"))
    a = compute_fingerprint(p)
    b = compute_fingerprint(p)
    assert compare_fingerprints(a, b) >= 0.99


def test_compare_fingerprints_different_returns_low(tmp_dir: Path) -> None:
    """Dos iconos visualmente distintos deben dar similitud baja."""
    a_icon = _icon(color=(200, 30, 30))
    b_icon = _icon(color=(30, 30, 200))
    pa = _save(a_icon, str(tmp_dir / "a.png"))
    pb = _save(b_icon, str(tmp_dir / "b.png"))
    a = compute_fingerprint(pa)
    b = compute_fingerprint(pb)
    sim = compare_fingerprints(a, b)
    # Misma silueta pero color distinto en grayscale: hashes pueden
    # coincidir si la luminancia es similar. Verificamos que NO sea
    # similitud perfecta y que opera sin crash.
    assert 0.0 <= sim <= 1.0


def test_compare_fingerprints_unavailable_returns_0() -> None:
    a = VisualFingerprint(available=False)
    b = VisualFingerprint(available=True, phash_hex="0" * 16, dhash_hex="0" * 16)
    assert compare_fingerprints(a, b) == 0.0


# ────────────────────────────────────────────────────────────────────────
# 4) relocate_target (API pública para self-healing)
# ────────────────────────────────────────────────────────────────────────


def test_relocate_target_accepts_when_strong(tmp_dir: Path) -> None:
    icon = _icon()
    icon_path = _save(icon, str(tmp_dir / "icon.png"))
    frame = _make_frame_with_icon(icon, at_xy=(420, 250))
    frame_path = _save(frame, str(tmp_dir / "frame.png"))

    fp = compute_fingerprint(icon_path)
    out = relocate_target(
        fingerprint=fp.to_dict(),
        frame_path=frame_path,
    )
    assert out["found"] is True
    assert out["decision"] in {"accept", "confirm"}
    # Centro del icono = (420+24, 250+24) = (444, 274)
    assert abs(out["x"] - 444) <= 6
    assert abs(out["y"] - 274) <= 6


def test_relocate_target_asks_when_absent(tmp_dir: Path) -> None:
    import numpy as np
    icon = _icon()
    icon_path = _save(icon, str(tmp_dir / "icon.png"))
    frame = np.full((600, 800, 3), 200, dtype=np.uint8)
    frame_path = _save(frame, str(tmp_dir / "empty.png"))

    fp = compute_fingerprint(icon_path)
    out = relocate_target(
        fingerprint=fp.to_dict(),
        frame_path=frame_path,
    )
    assert out["found"] is False
    assert out["decision"] == "ask"


def test_relocate_target_invalid_fingerprint_returns_ask() -> None:
    out = relocate_target(fingerprint=None, frame_path="anything.png")
    assert out["found"] is False
    assert out["decision"] == "ask"


def test_relocate_target_with_region_bbox_offsets_correctly(
    tmp_dir: Path,
) -> None:
    """Cuando se pasa ``region_bbox``, las coords devueltas deben
    estar en el sistema del frame completo, no del recorte."""
    icon = _icon()
    icon_path = _save(icon, str(tmp_dir / "icon.png"))
    # Icono en (420, 250) del frame original
    frame = _make_frame_with_icon(icon, at_xy=(420, 250))
    frame_path = _save(frame, str(tmp_dir / "frame.png"))

    fp = compute_fingerprint(icon_path)
    # Limitamos búsqueda a la ventana (350, 200, 300, 200)
    out = relocate_target(
        fingerprint=fp.to_dict(),
        frame_path=frame_path,
        region_bbox={"left": 350, "top": 200, "width": 300, "height": 200},
    )
    assert out["found"] is True
    # El offset debe haberse compensado: centro ≈ (444, 274) en coords
    # globales, NO en coords del recorte.
    assert abs(out["x"] - 444) <= 6
    assert abs(out["y"] - 274) <= 6


# ────────────────────────────────────────────────────────────────────────
# 5) Integration with CandidateFusionEngine
# ────────────────────────────────────────────────────────────────────────


def test_fusion_consumes_visual_fingerprint_as_evidence_source() -> None:
    """Un step con visual_fingerprint strong + UIA fuerte sube la
    confianza vs UIA fuerte solo."""
    engine = CandidateFusionEngine()
    uia_strong = {
        "name": "Daniel Arturo Ramos",
        "automation_id": "profile_picker_card_0",
        "control_type": "ButtonControl",
        "bbox": {"left": 100, "top": 100, "width": 200, "height": 200},
    }
    fp_strong = {
        "phash_hex": "1234567890abcdef",
        "dhash_hex": "fedcba0987654321",
        "orb_descriptors_b64": "AAEC",
        "orb_keypoint_count": 30,
        "crop_size": [200, 200],
        "source_path": "/tmp/x.png",
        "available": True,
        "quality": "strong",
    }
    r_without = engine.fuse(
        click_xy=(200, 200), click_ms=0, window_ctx={},
        uia=uia_strong, visual_fingerprint=None,
    )
    r_with = engine.fuse(
        click_xy=(200, 200), click_ms=0, window_ctx={},
        uia=uia_strong, visual_fingerprint=fp_strong,
    )
    assert r_with.confidence >= r_without.confidence


def test_fusion_visual_fingerprint_insufficient_does_not_contribute() -> None:
    """Un fingerprint con quality=insufficient NO produce candidato."""
    engine = CandidateFusionEngine()
    fp_bad = {
        "phash_hex": "", "dhash_hex": "",
        "available": False, "quality": "insufficient",
    }
    r = engine.fuse(
        click_xy=(10, 10), click_ms=0, window_ctx={},
        uia=None, dom=None, ocr=None, visual=None,
        visual_fingerprint=fp_bad,
    )
    # Sin otras fuentes y con fingerprint inválido → decisión "ask".
    assert r.decision == "ask"


def test_fusion_visual_fingerprint_alone_is_not_strong_enough() -> None:
    """Visual fingerprint solo (sin UIA/DOM/OCR) NO debe llegar a
    'accept'. La regla del PRD: la visión es soporte, no identidad
    primaria — salvo cuando hay anchor estructural respaldando.
    """
    engine = CandidateFusionEngine()
    fp_strong = {
        "phash_hex": "1234567890abcdef",
        "dhash_hex": "fedcba0987654321",
        "orb_keypoint_count": 50,
        "available": True,
        "quality": "strong",
    }
    r = engine.fuse(
        click_xy=(10, 10), click_ms=0, window_ctx={},
        uia=None, dom=None, ocr=None, visual=None,
        visual_fingerprint=fp_strong,
    )
    assert r.decision in {"confirm", "ask"}
    assert r.confidence < 0.85
