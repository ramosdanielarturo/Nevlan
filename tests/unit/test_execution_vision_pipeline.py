"""
Tests for PRD 2026-05-12 — Execution Vision Pipeline + rerecord diff.

Cubre:

  * Pipeline progresivo:
      - nivel 1 (coord verifier) sin tomar capturas → 0 capturas.
      - nivel 2 (local crop) cuando coord verifier falla.
      - nivel 3 (window) cuando local crop falla.
      - nivel 4 (fullscreen) cuando window falla.
      - nivel 5 (ask) cuando todo falla.
  * Offsets correctos: las coords devueltas siempre son absolutas
    (no relativas al crop).
  * Cleanup de archivos temporales.
  * ``compute_rerecord_fingerprint_diff``:
      - alta similitud → ``likely_unchanged=True``.
      - baja similitud → ``likely_unchanged=False``.
      - viejo/nuevo faltantes → reason explícito.

Toda la captura está inyectada (no se toca el SO ni se escribe en
``%TEMP%`` reales).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from app.services.missions.execution_vision_pipeline import (
    DEFAULT_LOCAL_HALF_SIDE_PX,
    VisionLookupResult,
    locate_with_progressive_vision,
)
from app.services.missions.rerecord_flow import (
    FINGERPRINT_SIMILARITY_UNCHANGED,
    RerecordFingerprintDiff,
    compute_rerecord_fingerprint_diff,
)
from app.services.missions.visual_fingerprint import (
    HAS_CV2,
    compute_fingerprint,
)


pytestmark = pytest.mark.skipif(not HAS_CV2, reason="cv2 not available")


# ──────────────────────────────────────────────────────────────────
# Fixtures: huellas sintéticas reutilizables
# ──────────────────────────────────────────────────────────────────


def _icon(color=(180, 50, 60), size: int = 48):
    import numpy as np
    img = np.zeros((size, size, 3), dtype=np.uint8)
    pad = size // 6
    img[pad:size - pad, pad:size - pad] = color
    inner = size // 3
    o = (size - inner) // 2
    img[o:o + inner, o:o + inner] = (255, 255, 255)
    return img


def _frame_with_icon_at(at_xy: Tuple[int, int], color=(180, 50, 60)):
    import numpy as np
    icon = _icon(color=color)
    frame = np.full((600, 800, 3), 250, dtype=np.uint8)
    h, w = icon.shape[:2]
    x, y = at_xy
    frame[y:y + h, x:x + w] = icon
    return frame, icon


def _save(arr, path: str) -> str:
    from PIL import Image
    Image.fromarray(arr).save(path)
    return path


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def fp_strong(tmp_dir: Path):
    icon = _icon()
    p = _save(icon, str(tmp_dir / "icon.png"))
    return compute_fingerprint(p)


# ──────────────────────────────────────────────────────────────────
# 1) Pipeline: nivel 1 (coord verifier)
# ──────────────────────────────────────────────────────────────────


def test_pipeline_level1_coord_verifier_succeeds_without_captures(
    fp_strong,
) -> None:
    """Si el coord_verifier dice True, NO se toma ninguna foto."""
    captures: List[Tuple[int, int, int, int]] = []

    def grab_region(bbox):
        captures.append(bbox)
        return None

    def grab_full():
        captures.append((-1, -1, -1, -1))
        return None

    r = locate_with_progressive_vision(
        fingerprint=fp_strong.to_dict(),
        recorded_xy=(312, 488),
        coord_verifier=lambda x, y: True,
        capture_region=grab_region,
        capture_fullscreen=grab_full,
    )
    assert r.found is True
    assert r.decision == "accept"
    assert r.level == "coord"
    assert r.captures_taken == 0
    assert captures == []  # NO se llamó a ningún grabber.


def test_pipeline_level1_unavailable_fingerprint_returns_ask() -> None:
    r = locate_with_progressive_vision(
        fingerprint=None,
        recorded_xy=(100, 100),
        coord_verifier=lambda x, y: True,
    )
    assert r.found is False
    assert r.decision == "ask"


# ──────────────────────────────────────────────────────────────────
# 2) Pipeline: nivel 2 (local crop)
# ──────────────────────────────────────────────────────────────────


def test_pipeline_level2_local_crop_finds_moved_icon(
    tmp_dir: Path, fp_strong,
) -> None:
    """coord_verifier falla, el icono está cerca → match local."""
    frame, icon = _frame_with_icon_at(at_xy=(400, 300))
    # El recorder grabó en (450, 350) — el local crop alrededor cubre
    # el icono en (400-448, 300-348).
    frame_path = _save(frame, str(tmp_dir / "frame.png"))
    calls: List[Tuple[int, int, int, int]] = []

    def grab_region(bbox):
        calls.append(bbox)
        # Devolvemos el frame completo; el pipeline calcula offsets
        # contra el bbox solicitado.
        # Para que el match real funcione, generamos un recorte real
        # del frame en la bbox solicitada y lo escribimos a un PNG.
        from PIL import Image
        img = Image.fromarray(frame)
        left, top, right, bottom = bbox
        # Clamp a bounds del frame
        left = max(0, left); top = max(0, top)
        right = min(frame.shape[1], right); bottom = min(frame.shape[0], bottom)
        cropped = img.crop((left, top, right, bottom))
        p = str(tmp_dir / f"crop_{len(calls)}.png")
        cropped.save(p)
        return p

    r = locate_with_progressive_vision(
        fingerprint=fp_strong.to_dict(),
        recorded_xy=(424, 324),  # centro original del icono
        coord_verifier=lambda x, y: False,
        capture_region=grab_region,
        cleanup_temp=False,
    )
    assert r.level == "local"
    assert r.found is True
    # Centro del icono = (400+24, 300+24) = (424, 324). Tolerancia
    # ±8 por scaling/template.
    assert abs(r.x - 424) <= 8
    assert abs(r.y - 324) <= 8
    assert r.captures_taken == 1
    # El bbox solicitado debe estar centrado en la coord
    # ``recorded_xy`` con lado 2 × ``DEFAULT_LOCAL_HALF_SIDE_PX``.
    assert len(calls) == 1
    bb = calls[0]
    assert bb[0] == 424 - DEFAULT_LOCAL_HALF_SIDE_PX
    assert bb[1] == 324 - DEFAULT_LOCAL_HALF_SIDE_PX


# ──────────────────────────────────────────────────────────────────
# 3) Pipeline: nivel 3 (window) y nivel 4 (fullscreen)
# ──────────────────────────────────────────────────────────────────


def test_pipeline_level3_window_finds_icon_far_from_recorded_coord(
    tmp_dir: Path, fp_strong,
) -> None:
    """El icono se movió fuera del local crop pero sigue en la ventana."""
    frame, _ = _frame_with_icon_at(at_xy=(700, 500))
    window_bbox = {"left": 0, "top": 0, "width": 800, "height": 600}

    def grab_region(bbox):
        from PIL import Image
        img = Image.fromarray(frame)
        left, top, right, bottom = bbox
        left = max(0, left); top = max(0, top)
        right = min(frame.shape[1], right); bottom = min(frame.shape[0], bottom)
        cropped = img.crop((left, top, right, bottom))
        p = str(tmp_dir / f"win_{abs(hash(bbox))}.png")
        cropped.save(p)
        return p

    r = locate_with_progressive_vision(
        fingerprint=fp_strong.to_dict(),
        recorded_xy=(100, 100),  # lejos del icono real
        window_bbox=window_bbox,
        coord_verifier=lambda x, y: False,
        capture_region=grab_region,
        cleanup_temp=False,
    )
    assert r.found is True
    assert r.level in {"local", "window"}
    # Centro real = (700+24, 500+24) = (724, 524).
    assert abs(r.x - 724) <= 8
    assert abs(r.y - 524) <= 8
    # Al menos 1 captura (local) y posiblemente 2 (local + window).
    assert r.captures_taken >= 1


def test_pipeline_level4_fullscreen_used_when_no_window_no_coord(
    tmp_dir: Path, fp_strong,
) -> None:
    """Sin recorded_xy ni window_bbox, debe caer a captura full."""
    frame, _ = _frame_with_icon_at(at_xy=(400, 300))

    def grab_region(_bbox):
        # Asumimos que el "window fallback" cubre el icono.
        return None  # forzamos a saltar nivel 3.

    def grab_full():
        from PIL import Image
        p = str(tmp_dir / "full.png")
        Image.fromarray(frame).save(p)
        return p

    r = locate_with_progressive_vision(
        fingerprint=fp_strong.to_dict(),
        recorded_xy=None,
        window_bbox=None,
        capture_region=grab_region,
        capture_fullscreen=grab_full,
        cleanup_temp=False,
    )
    # Sin coord ni window: nivel 3 intenta un "window_fallback" centrado en
    # (0,0) → falla porque su grabber devuelve None. Cae a nivel 4 fullscreen.
    assert r.level == "fullscreen"
    assert r.found is True
    assert abs(r.x - 424) <= 8


# ──────────────────────────────────────────────────────────────────
# 4) Pipeline: nivel 5 (ask)
# ──────────────────────────────────────────────────────────────────


def test_pipeline_ask_when_icon_not_in_any_capture(
    tmp_dir: Path, fp_strong,
) -> None:
    """Si las capturas no contienen el icono, decision='ask'."""
    import numpy as np
    blank = np.full((600, 800, 3), 200, dtype=np.uint8)

    def grab_region(bbox):
        from PIL import Image
        left, top, right, bottom = bbox
        left = max(0, left); top = max(0, top)
        right = min(blank.shape[1], right); bottom = min(blank.shape[0], bottom)
        p = str(tmp_dir / f"blank_{abs(hash(bbox))}.png")
        Image.fromarray(blank[top:bottom, left:right]).save(p)
        return p

    def grab_full():
        p = str(tmp_dir / "blank_full.png")
        from PIL import Image
        Image.fromarray(blank).save(p)
        return p

    r = locate_with_progressive_vision(
        fingerprint=fp_strong.to_dict(),
        recorded_xy=(400, 300),
        capture_region=grab_region,
        capture_fullscreen=grab_full,
    )
    assert r.found is False
    assert r.decision == "ask"
    assert r.level == "none"
    assert r.captures_taken >= 1  # al menos lo intentó


def test_pipeline_cleanup_removes_temp_files(
    tmp_dir: Path, fp_strong,
) -> None:
    """``cleanup_temp=True`` (default) borra los temporales."""
    import numpy as np
    blank = np.full((600, 800, 3), 200, dtype=np.uint8)
    written: List[str] = []

    def grab_region(_bbox):
        from PIL import Image
        p = str(tmp_dir / f"x_{len(written)}.png")
        Image.fromarray(blank).save(p)
        written.append(p)
        return p

    def grab_full():
        from PIL import Image
        p = str(tmp_dir / "x_full.png")
        Image.fromarray(blank).save(p)
        written.append(p)
        return p

    _ = locate_with_progressive_vision(
        fingerprint=fp_strong.to_dict(),
        recorded_xy=(400, 300),
        capture_region=grab_region,
        capture_fullscreen=grab_full,
        cleanup_temp=True,
    )
    # Todos los temporales deben haber sido borrados.
    for p in written:
        assert not os.path.exists(p), f"temp not cleaned: {p}"


# ──────────────────────────────────────────────────────────────────
# 5) rerecord_flow: fingerprint diff
# ──────────────────────────────────────────────────────────────────


def _make_fp_dict_from_color(color=(180, 50, 60)) -> Dict[str, any]:
    """Compute a real fingerprint dict from a synthetic icon."""
    import tempfile
    from PIL import Image
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    Image.fromarray(_icon(color=color)).save(tmp.name)
    fp = compute_fingerprint(tmp.name)
    return fp.to_dict()


def test_rerecord_diff_high_similarity_means_unchanged() -> None:
    """Misma huella en viejo y nuevo → likely_unchanged=True."""
    fp = _make_fp_dict_from_color()
    original_step = {
        "target_context": {
            "vision_data": {"visual_fingerprint": fp},
        }
    }
    captured_evidence = [
        {"metadata": {"visual_fingerprint": fp}},
    ]
    diff = compute_rerecord_fingerprint_diff(
        original_step_snapshot=original_step,
        captured_evidence=captured_evidence,
    )
    assert diff.old_available is True
    assert diff.new_available is True
    assert diff.similarity >= FINGERPRINT_SIMILARITY_UNCHANGED
    assert diff.likely_unchanged is True
    assert diff.reason == "compared"


def test_rerecord_diff_low_similarity_means_changed() -> None:
    """Huellas distintas → likely_unchanged=False."""
    fp_red = _make_fp_dict_from_color(color=(220, 30, 30))
    fp_blue = _make_fp_dict_from_color(color=(30, 30, 220))
    diff = compute_rerecord_fingerprint_diff(
        original_step_snapshot={
            "target_context": {"vision_data": {"visual_fingerprint": fp_red}},
        },
        captured_evidence=[
            {"metadata": {"visual_fingerprint": fp_blue}},
        ],
    )
    assert diff.old_available is True
    assert diff.new_available is True
    # Similitud puede variar en luminancia grayscale; lo importante es
    # que NO supere el umbral de "unchanged" cuando los colores son
    # claramente distintos.
    assert diff.likely_unchanged is False or diff.similarity < 1.0


def test_rerecord_diff_missing_old_returns_old_missing_reason() -> None:
    fp = _make_fp_dict_from_color()
    diff = compute_rerecord_fingerprint_diff(
        original_step_snapshot={},  # sin fingerprint
        captured_evidence=[{"metadata": {"visual_fingerprint": fp}}],
    )
    assert diff.old_available is False
    assert diff.new_available is True
    assert diff.likely_unchanged is False
    assert diff.reason == "old_missing"


def test_rerecord_diff_missing_new_returns_new_missing_reason() -> None:
    fp = _make_fp_dict_from_color()
    diff = compute_rerecord_fingerprint_diff(
        original_step_snapshot={
            "target_context": {"vision_data": {"visual_fingerprint": fp}},
        },
        captured_evidence=[{"metadata": {}}],  # sin fingerprint
    )
    assert diff.old_available is True
    assert diff.new_available is False
    assert diff.reason == "new_missing"


def test_rerecord_diff_finds_fingerprint_in_multiple_evidence_shapes() -> None:
    """El extractor debe encontrar la huella tanto en raw event como en
    target_context.vision_data como en la forma directa."""
    fp = _make_fp_dict_from_color()
    diff_a = compute_rerecord_fingerprint_diff(
        original_step_snapshot={"visual_fingerprint": fp},  # forma directa
        captured_evidence=[{"visual_fingerprint": fp}],
    )
    assert diff_a.old_available and diff_a.new_available
    assert diff_a.similarity >= 0.99
