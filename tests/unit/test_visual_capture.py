"""Unit tests for ``visual_capture`` — bbox-first asset selection.

These tests are pure-Python (no display required) — they exercise the
selection logic and metadata pipeline without writing PNGs. The
``capture_files=False`` flag disables ImageGrab calls so the tests run
in CI/headless.
"""
from __future__ import annotations

import pytest

from app.services.missions.visual_capture import (
    CapturedVisual,
    TargetCandidate,
    SRC_UIA, SRC_DOM, SRC_OCR, SRC_VISION, SRC_CLICK_FALLBACK,
    MIN_MARGIN_PX, MAX_MARGIN_PX, MAX_TARGET_AREA_RATIO,
    select_target_bbox,
    compute_asset_bbox,
    capture_visual_target,
    validate_visual_capture,
)


# ──────────────────────────────────────────────────────────────────────
# select_target_bbox — fuente UIA
# ──────────────────────────────────────────────────────────────────────

class TestSelectTargetBboxUIA:
    def test_uia_meaningful_button_wins(self):
        uia = {
            "name": "Save", "automation_id": "btnSave",
            "control_type": "ButtonControl",
            "bbox": {"left": 100, "top": 200, "width": 80, "height": 32},
        }
        cand = select_target_bbox(
            click_xy=(140, 215),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
        )
        assert cand.source == SRC_UIA
        assert cand.bbox["width"] == 80
        assert cand.label == "Save"

    def test_uia_giant_container_rejected_by_25_pct_rule(self):
        # Window 1000x1000 → window_area=1_000_000.
        # bbox 800x800 → 640_000 → 64% > 25% → debe rechazarse.
        uia = {
            "name": "Document", "automation_id": "",
            "control_type": "DocumentControl",
            "bbox": {"left": 50, "top": 50, "width": 800, "height": 800},
        }
        cand = select_target_bbox(
            click_xy=(400, 400),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1000, "height": 1000},
            screen_size=(1920, 1080),
        )
        # Cae a click_fallback al no haber DOM/OCR.
        assert cand.source == SRC_CLICK_FALLBACK

    def test_uia_generic_control_type_rejected(self):
        uia = {
            "name": "main", "automation_id": "",
            "control_type": "GroupControl",
            "bbox": {"left": 100, "top": 100, "width": 200, "height": 100},
        }
        cand = select_target_bbox(
            click_xy=(150, 150),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
        )
        assert cand.source == SRC_CLICK_FALLBACK

    def test_uia_generic_ct_rescued_by_automation_id(self):
        # GroupControl + automation_id="my_panel" → tiene rescate, se acepta.
        uia = {
            "name": "main", "automation_id": "my_panel",
            "control_type": "GroupControl",
            "bbox": {"left": 100, "top": 100, "width": 200, "height": 100},
        }
        cand = select_target_bbox(
            click_xy=(150, 150),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
        )
        assert cand.source == SRC_UIA

    @pytest.mark.parametrize("name", [
        "guide-service", "ytd-app", "ytd-search-box",
        "video-stream", "app-root", "react-root",
    ])
    def test_uia_technical_label_rejected(self, name):
        uia = {
            "name": name, "automation_id": "",
            "control_type": "CustomControl",
            "bbox": {"left": 100, "top": 100, "width": 200, "height": 50},
        }
        cand = select_target_bbox(
            click_xy=(150, 120),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
        )
        assert cand.source == SRC_CLICK_FALLBACK, (
            f"UIA name={name!r} debe rechazarse como contenedor técnico"
        )

    def test_uia_click_outside_bbox_rejected(self):
        uia = {
            "name": "Save", "automation_id": "btnSave",
            "control_type": "ButtonControl",
            "bbox": {"left": 100, "top": 200, "width": 80, "height": 32},
        }
        # Click fuera del bbox → no se acepta.
        cand = select_target_bbox(
            click_xy=(500, 500),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
        )
        assert cand.source == SRC_CLICK_FALLBACK


# ──────────────────────────────────────────────────────────────────────
# select_target_bbox — DOM/OCR/vision fallbacks
# ──────────────────────────────────────────────────────────────────────

class TestSelectTargetBboxFallbacks:
    def test_dom_used_when_uia_rejected(self):
        # UIA gigante + DOM bbox razonable → DOM gana.
        uia = {
            "name": "ytd-app", "automation_id": "",
            "control_type": "PaneControl",
            "bbox": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        }
        web = {
            "role": "searchbox",
            "accessible_name": "Buscar",
            "label": "Buscar",
            "bbox": {"left": 800, "top": 50, "width": 400, "height": 40},
            "scroll_x": 0, "scroll_y": 0,
        }
        window = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        cand = select_target_bbox(
            click_xy=(900, 70),
            uia_metadata=uia,
            web_metadata=web,
            window_bbox=window,
            screen_size=(1920, 1080),
        )
        assert cand.source == SRC_DOM
        assert cand.bbox["width"] == 400
        assert cand.label == "Buscar"

    def test_ocr_used_when_no_uia_no_dom(self):
        ocr = [TargetCandidate(
            bbox={"left": 100, "top": 100, "width": 80, "height": 30},
            source=SRC_OCR, label="Submit", role="button",
            confidence=0.8,
        )]
        cand = select_target_bbox(
            click_xy=(140, 115),
            ocr_candidates=ocr,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
        )
        assert cand.source == SRC_OCR
        assert cand.label == "Submit"

    def test_vision_after_ocr(self):
        vision = [TargetCandidate(
            bbox={"left": 200, "top": 300, "width": 64, "height": 64},
            source=SRC_VISION, label="Icon", role="image",
            confidence=0.7,
        )]
        cand = select_target_bbox(
            click_xy=(220, 320),
            vision_candidates=vision,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
        )
        assert cand.source == SRC_VISION

    def test_click_fallback_when_nothing_matches(self):
        cand = select_target_bbox(
            click_xy=(500, 500),
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
        )
        assert cand.source == SRC_CLICK_FALLBACK
        # Bbox sintético cuadrado alrededor del click.
        assert cand.bbox["width"] == 80
        assert cand.bbox["height"] == 80
        cx = cand.bbox["left"] + cand.bbox["width"] // 2
        cy = cand.bbox["top"] + cand.bbox["height"] // 2
        assert abs(cx - 500) <= 1 and abs(cy - 500) <= 1

    def test_canvas_action_bypasses_25_pct(self):
        # En modo "canvas" un bbox enorme SÍ es válido (ej. lienzo de Paint).
        uia = {
            "name": "Canvas", "automation_id": "draw_area",
            "control_type": "CustomControl",
            "bbox": {"left": 0, "top": 0, "width": 1900, "height": 1000},
        }
        cand = select_target_bbox(
            click_xy=(500, 500),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
            is_canvas_action=True,
        )
        assert cand.source == SRC_UIA


# ──────────────────────────────────────────────────────────────────────
# compute_asset_bbox — margen contextual
# ──────────────────────────────────────────────────────────────────────

class TestComputeAssetBbox:
    def test_min_margin_for_small_target(self):
        # 50x20 → 20% = 10/4 → min margin 40 → asset crece 80px en cada eje.
        asset = compute_asset_bbox(
            {"left": 500, "top": 500, "width": 50, "height": 20},
            screen_size=(1920, 1080),
        )
        # Margen real aplicado = max(MIN_MARGIN_PX, 0.2*max_dim) = 40.
        assert asset["left"] == 460
        assert asset["top"] == 460
        assert asset["width"] == 50 + 80  # +40 left, +40 right
        assert asset["height"] == 20 + 80

    def test_20_pct_margin_for_medium_target(self):
        # 400x100 → 0.20*400 = 80 (>40 mínimo), <160 cap → margin 80.
        asset = compute_asset_bbox(
            {"left": 500, "top": 500, "width": 400, "height": 100},
            screen_size=(1920, 1080),
        )
        # max(margin_x, margin_y) = max(80, 40)=80.
        assert asset["width"] == 400 + 2 * 80
        assert asset["height"] == 100 + 2 * 80

    def test_margin_capped_at_max(self):
        # 1000x1000 → 0.20*1000 = 200, capped at MAX_MARGIN_PX=160.
        asset = compute_asset_bbox(
            {"left": 200, "top": 200, "width": 1000, "height": 1000},
            screen_size=(2000, 2000),
        )
        # Inflado por max(160, 160) = 160.
        assert asset["width"] == 1000 + 2 * MAX_MARGIN_PX
        assert asset["height"] == 1000 + 2 * MAX_MARGIN_PX

    def test_clamped_to_screen(self):
        # Target en esquina superior-izquierda; asset NUNCA puede ser negativo.
        asset = compute_asset_bbox(
            {"left": 5, "top": 5, "width": 50, "height": 50},
            screen_size=(1920, 1080),
        )
        assert asset["left"] >= 0
        assert asset["top"] >= 0

    def test_clamped_to_window_when_provided(self):
        # Window estrecha de 200x200 — el asset no debe salir de ahí.
        asset = compute_asset_bbox(
            {"left": 50, "top": 50, "width": 100, "height": 100},
            screen_size=(1920, 1080),
            window_bbox={"left": 0, "top": 0, "width": 200, "height": 200},
        )
        assert asset["left"] >= 0
        assert asset["top"] >= 0
        assert asset["left"] + asset["width"] <= 200
        assert asset["top"] + asset["height"] <= 200


# ──────────────────────────────────────────────────────────────────────
# capture_visual_target — orquestación
# ──────────────────────────────────────────────────────────────────────

class TestCaptureVisualTarget:
    def test_metadata_shape(self):
        uia = {
            "name": "Save", "automation_id": "btnSave",
            "control_type": "ButtonControl",
            "bbox": {"left": 100, "top": 200, "width": 80, "height": 32},
        }
        cap = capture_visual_target(
            click_xy=(140, 215),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
            capture_files=False,
        )
        meta = cap.to_metadata()
        # Llaves obligatorias del PRD §5.
        for k in (
            "target_bbox", "asset_bbox", "click_xy",
            "click_offset_px", "click_offset_ratio",
            "target_source", "quality_level",
            "needs_reinforcement", "screen_size", "window_bbox",
        ):
            assert k in meta, f"falta clave {k!r} en metadata"
        # click_xy formato compacto [x, y].
        assert meta["click_xy"] == [140, 215]
        # quality_level high si UIA aceptado y sin errores.
        assert meta["quality_level"] in {"high", "medium"}
        assert meta["target_source"] == SRC_UIA

    def test_click_fallback_marks_low_quality(self):
        cap = capture_visual_target(
            click_xy=(500, 500),
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
            capture_files=False,
        )
        assert cap.source == SRC_CLICK_FALLBACK
        assert cap.quality_level == "low"
        assert cap.needs_reinforcement is True

    def test_click_offset_ratio_in_unit_range(self):
        # Click en (140, 215) y bbox 100..180 / 200..232 → fx=0.5, fy~=0.47.
        uia = {
            "name": "Save", "automation_id": "btnSave",
            "control_type": "ButtonControl",
            "bbox": {"left": 100, "top": 200, "width": 80, "height": 32},
        }
        cap = capture_visual_target(
            click_xy=(140, 215),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
            capture_files=False,
        )
        fx, fy = cap.click_offset_ratio
        assert 0.0 <= fx <= 1.0
        assert 0.0 <= fy <= 1.0
        assert abs(fx - 0.5) < 0.01
        assert abs(fy - (15 / 32)) < 0.01

    def test_click_offset_px_from_bbox_center(self):
        uia = {
            "name": "Save", "automation_id": "btnSave",
            "control_type": "ButtonControl",
            "bbox": {"left": 100, "top": 200, "width": 80, "height": 32},
        }
        cap = capture_visual_target(
            click_xy=(140, 215),
            uia_metadata=uia,
            window_bbox={"left": 0, "top": 0, "width": 1920, "height": 1080},
            screen_size=(1920, 1080),
            capture_files=False,
        )
        # Centro del bbox = (140, 216). Click (140, 215) → offset (0, -1).
        assert cap.click_offset_px == (0, -1)


# ──────────────────────────────────────────────────────────────────────
# validate_visual_capture
# ──────────────────────────────────────────────────────────────────────

class TestValidate:
    def test_valid_capture_no_errors(self):
        captured = CapturedVisual(
            target_bbox={"left": 100, "top": 200, "width": 80, "height": 32},
            asset_bbox={"left": 60, "top": 160, "width": 160, "height": 112},
            click_xy=(140, 215),
            click_offset_px=(0, -1),
            click_offset_ratio=(0.5, 0.47),
            source=SRC_UIA,
            quality_level="high",
            needs_reinforcement=False,
            screen_size=(1920, 1080),
        )
        assert validate_visual_capture(captured) == []

    def test_click_outside_target_flagged(self):
        captured = CapturedVisual(
            target_bbox={"left": 100, "top": 200, "width": 80, "height": 32},
            asset_bbox={"left": 60, "top": 160, "width": 160, "height": 112},
            click_xy=(500, 500),  # ¡fuera!
            click_offset_px=(0, 0),
            click_offset_ratio=(0.0, 0.0),
            source=SRC_UIA,
            quality_level="high",
            needs_reinforcement=False,
            screen_size=(1920, 1080),
        )
        errors = validate_visual_capture(captured)
        assert "click_xy_outside_target_bbox" in errors

    def test_asset_outside_screen_flagged(self):
        captured = CapturedVisual(
            target_bbox={"left": 100, "top": 200, "width": 80, "height": 32},
            asset_bbox={"left": -50, "top": 0, "width": 160, "height": 112},
            click_xy=(140, 215),
            click_offset_px=(0, -1),
            click_offset_ratio=(0.5, 0.47),
            source=SRC_UIA,
            quality_level="high",
            needs_reinforcement=False,
            screen_size=(1920, 1080),
        )
        errors = validate_visual_capture(captured)
        assert "asset_bbox_outside_screen" in errors

    def test_click_fallback_skips_click_in_target_check(self):
        # Aunque el click esté fuera del bbox sintético, si la fuente es
        # ``click_fallback`` no marcamos error (el bbox es por construcción
        # alrededor del click).
        captured = CapturedVisual(
            target_bbox={"left": 460, "top": 460, "width": 80, "height": 80},
            asset_bbox={"left": 420, "top": 420, "width": 160, "height": 160},
            click_xy=(500, 500),
            click_offset_px=(0, 0),
            click_offset_ratio=(0.5, 0.5),
            source=SRC_CLICK_FALLBACK,
            quality_level="low",
            needs_reinforcement=True,
            screen_size=(1920, 1080),
        )
        assert validate_visual_capture(captured) == []
