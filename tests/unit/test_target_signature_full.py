"""Tests para TargetSignature (PRD §4).

Verifica que la firma incluya:
  - UIA: name, control_type, automation_id, bbox
  - Visual: refs small/mid + image_sizes
  - Coordenadas: absoluta + relativa a ventana + relativa a elemento
  - click_offset (px y porcentual)
  - Contexto: window title / process / hwnd / window bbox / dpi
  - Confianza: score, reasons, target_quality
"""
from __future__ import annotations

from app.contracts.mission import WindowContext
from app.services.missions.target_signature import (
    build_target_signature, bbox_ratio_within_window,
)


def _wctx(hwnd=99, title="App", proc="app.exe",
          left=0, top=0, w=1920, h=1080) -> WindowContext:
    return WindowContext(
        hwnd=hwnd,
        title=title,
        process_name=proc,
        bounding_box={"left": left, "top": top, "width": w, "height": h},
    )


class TestSignatureCompleteness:
    def test_signature_contains_all_required_blocks_for_strong_target(self):
        sig = build_target_signature(
            x=480, y=320,
            metadata={
                "uia": {
                    "name": "Buscar",
                    "control_type": "EditControl",
                    "automation_id": "searchInput",
                    "class_name": "Edit",
                },
                "anchor_bbox": {
                    "left": 460, "top": 300, "width": 120, "height": 40,
                },
                "click_offset": {"dx": 4, "dy": 0},
                "rel_win": {"fx": 0.25, "fy": 0.30},
                "snapshot_mid": "/tmp/mid.png",
                "web": {
                    "locators": [{"kind": "test_id", "value": "search-x"}],
                    "accessible_name": "Buscar",
                    "label": "Buscar",
                    "nearby_text": "Buscar productos",
                },
                "ocr_anchor": "Buscar",
            },
            window_ctx=_wctx(),
            small_snapshot="/tmp/small.png",
            mid_snapshot="/tmp/mid.png",
        )

        # ── UIA ────────────────────────────────────────────
        uia = sig["uia"]
        assert uia["name"] == "Buscar"
        assert uia["control_type"] == "EditControl"
        assert uia["automation_id"] == "searchInput"
        assert uia["class_name"] == "Edit"
        assert uia["bbox"] == {
            "left": 460, "top": 300, "width": 120, "height": 40,
        }

        # ── Visual ────────────────────────────────────────
        va = sig["visual_assets"]
        assert va["ref_small"] == "/tmp/small.png"
        assert va["ref_mid"] == "/tmp/mid.png"
        sz = sig["image_sizes"]
        assert sz["image_size_small_px"] == 100
        assert sz["image_size_mid_px"] == 600

        # ── Coordenadas ────────────────────────────────────
        coords = sig["coordinates"]
        assert coords["absolute"] == {"x": 480, "y": 320}
        assert coords["relative_window"] == {"fx": 0.25, "fy": 0.30}
        assert coords["bbox_element"]["width"] == 120
        assert coords["click_offset_in_bbox"] == {"dx": 4, "dy": 0}
        # offset relativo (porcentaje del bbox)
        ratio = coords["click_offset_ratio_in_element"]
        assert "dx_frac_elem" in ratio
        assert "dy_frac_elem" in ratio
        assert abs(ratio["dx_frac_elem"] - (4 / 120)) < 1e-3

        # Layout dentro de la ventana
        layout = coords["element_layout_in_window"]
        assert "center_fx_win" in layout
        assert "center_fy_win" in layout
        assert "area_ratio" in layout

        # ── Contexto ───────────────────────────────────────
        wp = sig["window_process"]
        assert wp["hwnd"] == 99
        assert wp["title"] == "App"
        assert wp["process_name"] == "app.exe"
        assert wp["window_bbox"]["width"] == 1920

        env = sig["capture_environment"]
        assert "captured_at" in env
        # En Windows + PyQt6 puede haber dpi_scale; en otros entornos,
        # al menos primary_screen_px o el campo None.
        assert "primary_screen_px" in env

        # ── Texto contextual ───────────────────────────────
        tx = sig["text_context"]
        assert tx["near_visible"] in ("Buscar", "Buscar productos")
        assert tx["nearby_dom"] == "Buscar productos"

        # ── Confianza ──────────────────────────────────────
        conf = sig["confidence"]
        assert isinstance(conf["confidence_score"], float)
        assert 0.0 <= conf["confidence_score"] <= 100.0
        assert conf["target_quality"] in ("green", "yellow", "red")
        # Razones: deben aparecer las señales fuertes presentes.
        reasons = conf["confidence_reasons"]
        assert any("uia.automation_id" in r for r in reasons)
        assert any("vision.crops" in r for r in reasons)
        assert any("web.locators" in r for r in reasons)


class TestSignatureWeakTarget:
    def test_only_coords_marked_low_quality(self):
        sig = build_target_signature(
            x=10, y=10,
            metadata={},
            window_ctx=_wctx(),
        )
        conf = sig["confidence"]
        # Sin UIA, web, vision → quality red y score bajo.
        assert conf["target_quality"] == "red"
        assert conf["confidence_score"] <= 30


class TestBboxRatioHelper:
    def test_basic_centering(self):
        out = bbox_ratio_within_window(
            window_bbox={"left": 0, "top": 0, "width": 1000, "height": 500},
            elem_bbox={"left": 450, "top": 240, "width": 100, "height": 20},
        )
        assert abs(out["center_fx_win"] - 0.5) < 0.01
        assert abs(out["center_fy_win"] - 0.5) < 0.01
        # area_ratio = 100*20 / 1000*500 = 0.004
        assert abs(out["area_ratio"] - 0.004) < 1e-4
