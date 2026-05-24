"""
Unit tests para ``app.services.missions.asset_finalizer``.

Verifica el contrato del Asset Finalizer (PRD 2026-05-03 §D):

  * Mueve PNGs desde ``var/missions/assets/temp/`` a la carpeta
    permanente ``var/missions/assets/{mission_id}/``.
  * Reescribe TODAS las referencias en ``raw_trace[i].snapshot_ref``,
    ``raw_trace[i].metadata['snapshot_*']``, ``target_signature.vision.*``
    y ``compiled_execution_graph[i].target_context.image_ref{,_mid}``.
  * Idempotente: re-ejecutar no rompe.
  * Devuelve ``ok=False`` y entradas en ``temp_remaining`` si algún path
    queda apuntando a ``/temp/``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    InterpretedStep,
    Mission,
    RawEvent,
    EventType,
    MouseAction,
    TargetContext,
    ValidationStrategy,
)
from app.services.missions.asset_finalizer import (
    enrich_resolution_independent_layout,
    finalize_mission_assets,
    has_temp_paths,
)


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_PNG_TAIL = (
    # IHDR chunk minimal + IDAT empty + IEND. Así pasa la firma + tamaño
    # mínimo que pide ``asset_validation.is_safe_png_path``.
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x0aIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n\x2d\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_PNG_HEADER + _PNG_TAIL)


@pytest.fixture
def mission_with_temp_assets(tmp_path, monkeypatch):
    """Misión con un step compilado que apunta a 3 PNGs en ``temp/``."""
    # Forzar VAR_PATH a tmp_path para no tocar el filesystem real.
    monkeypatch.setattr(
        "app.services.missions.asset_finalizer.settings.VAR_PATH",
        tmp_path,
    )
    temp_dir = tmp_path / "missions" / "assets" / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    raw_path = temp_dir / "step_raw_small.png"
    mid_path = temp_dir / "step_raw_mid.png"
    full_path = temp_dir / "step_raw_full.png"
    _write_png(raw_path)
    _write_png(mid_path)
    _write_png(full_path)

    raw_event = RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=10, y=20, button="left"),
        snapshot_ref=str(raw_path),
        metadata={
            "snapshot_small": str(raw_path),
            "snapshot_mid": str(mid_path),
            "snapshot_full": str(full_path),
            "target_signature": {
                "vision": {
                    "ref_small": str(raw_path),
                    "ref_mid": str(mid_path),
                },
            },
        },
    )

    tc = TargetContext(
        image_ref=str(raw_path),
        image_ref_mid=str(mid_path),
    )
    cs = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=tc,
        validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    )
    interp = InterpretedStep(
        description="x", raw_event_ids=[raw_event.id],
    )

    m = Mission(name="t", id="abc1234")
    m.raw_trace = [raw_event]
    m.compiled_execution_graph = [cs]
    m.interpreted_steps = [interp]
    return m, tmp_path, raw_path, mid_path, full_path


class TestFinalizeMissionAssets:
    def test_copies_temp_assets_to_canonical_path(self, mission_with_temp_assets):
        m, root, raw, mid, full = mission_with_temp_assets
        report = finalize_mission_assets(m)
        assert report.ok, f"errors={report.errors} temp={report.temp_remaining}"
        assert report.copied >= 1
        # Destino canónico existe
        dest_dir = root / "missions" / "assets" / m.id
        assert dest_dir.exists()
        png_files = list(dest_dir.glob("*.png"))
        assert len(png_files) >= 1

    def test_rewrites_all_references(self, mission_with_temp_assets):
        m, root, *_ = mission_with_temp_assets
        finalize_mission_assets(m)
        # Ningún path en la misión debe contener "/temp/" o "\\temp\\".
        ev = m.raw_trace[0]
        assert "/temp/" not in str(ev.snapshot_ref).replace("\\", "/")
        for k in ("snapshot_small", "snapshot_mid", "snapshot_full"):
            v = ev.metadata.get(k, "")
            assert "/temp/" not in str(v).replace("\\", "/")
        sig = ev.metadata["target_signature"]["vision"]
        for k in ("ref_small", "ref_mid"):
            assert "/temp/" not in str(sig[k]).replace("\\", "/")
        cs = m.compiled_execution_graph[0]
        assert "/temp/" not in str(cs.target_context.image_ref).replace("\\", "/")
        assert "/temp/" not in str(cs.target_context.image_ref_mid).replace("\\", "/")

    def test_canonical_destination_contains_step_id(self, mission_with_temp_assets):
        m, *_ = mission_with_temp_assets
        finalize_mission_assets(m)
        cs = m.compiled_execution_graph[0]
        assert cs.id in str(cs.target_context.image_ref)
        assert cs.id in str(cs.target_context.image_ref_mid)

    def test_idempotent(self, mission_with_temp_assets):
        m, *_ = mission_with_temp_assets
        r1 = finalize_mission_assets(m)
        # Segunda ejecución: ya no hay nada en temp, todo debe seguir ok.
        r2 = finalize_mission_assets(m)
        assert r1.ok and r2.ok
        assert r2.copied == 0  # nada nuevo
        assert r2.temp_remaining == []

    def test_has_temp_paths_helper(self, mission_with_temp_assets):
        m, *_ = mission_with_temp_assets
        assert has_temp_paths(m) is True
        finalize_mission_assets(m)
        assert has_temp_paths(m) is False

    def test_rewrites_visual_assets_recursively(self, tmp_path, monkeypatch):
        """PRD 2026-05-03b §4: la reescritura debe entrar a CUALQUIER
        path bajo ``target_signature`` o ``target_context.signature``,
        no sólo a ``vision.ref_*``.
        """
        monkeypatch.setattr(
            "app.services.missions.asset_finalizer.settings.VAR_PATH",
            tmp_path,
        )
        temp_dir = tmp_path / "missions" / "assets" / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        small = temp_dir / "abc_small.png"
        mid = temp_dir / "abc_mid.png"
        _write_png(small)
        _write_png(mid)

        ev = RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=10, y=10, button="left"),
            metadata={
                # ``visual_assets`` (key real del recorder) en vez de
                # ``vision``: el finalizer debe encontrarlo igual.
                "target_signature": {
                    "visual_assets": {
                        "ref_small": str(small),
                        "ref_mid": str(mid),
                    },
                    "environment": {"primary_screen_px": {"w": 1920, "h": 1080}},
                },
            },
        )
        tc = TargetContext(
            signature={
                "visual_assets": {
                    "ref_small": str(small),
                    "ref_mid": str(mid),
                },
            },
        )
        cs = CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            target_context=tc,
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
        )
        m = Mission(name="t", id="recwalk")
        m.raw_trace = [ev]
        m.compiled_execution_graph = [cs]
        m.interpreted_steps = [InterpretedStep(description="x", raw_event_ids=[ev.id])]

        report = finalize_mission_assets(m)
        assert report.ok, f"errors={report.errors} temp={report.temp_remaining}"

        # NINGÚN path en visual_assets puede seguir apuntando a /assets/temp/.
        sig_ev = m.raw_trace[0].metadata["target_signature"]["visual_assets"]
        for k in ("ref_small", "ref_mid"):
            v = str(sig_ev[k]).replace("\\", "/")
            assert "/assets/temp/" not in v, f"raw_trace visual_assets.{k}={v}"
        sig_cs = m.compiled_execution_graph[0].target_context.signature["visual_assets"]
        for k in ("ref_small", "ref_mid"):
            v = str(sig_cs[k]).replace("\\", "/")
            assert "/assets/temp/" not in v, f"compiled visual_assets.{k}={v}"

    def test_missing_file_records_in_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.services.missions.asset_finalizer.settings.VAR_PATH",
            tmp_path,
        )
        ev = RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=1, y=1, button="left"),
            snapshot_ref=str(
                tmp_path / "missions" / "assets" / "temp" / "ghost_small.png"
            ),
        )
        m = Mission(name="t", id="m")
        m.raw_trace = [ev]
        report = finalize_mission_assets(m)
        # Falta el archivo → en `missing` y `snapshot_ref` se limpia.
        assert any("snapshot_ref" in x for x in report.missing)
        assert m.raw_trace[0].snapshot_ref is None


class TestLayoutEnrichment:
    """``enrich_resolution_independent_layout`` (PRD 2026-05-03c §1)."""

    def test_computes_click_offset_and_layout_ratios(self):
        ev = RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=160, y=240, button="left"),
            metadata={
                "click_xy": [160, 240],
                "anchor_bbox": {
                    "left": 100, "top": 200, "width": 200, "height": 100,
                },
                "window_bbox": {
                    "left": 0, "top": 0, "width": 1000, "height": 800,
                },
                "screen_size": {"w": 1920, "h": 1080},
                "dpi_scale": 1.5,
            },
        )
        cs = CompiledStep(
            id=ev.id,  # truco para emparejar raw → compiled vía id
            action_strategy=ActionStrategy.CLICK,
            target_context=TargetContext(),
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
        )
        m = Mission(name="t", id="layout1")
        m.raw_trace = [ev]
        m.compiled_execution_graph = [cs]
        m.interpreted_steps = [InterpretedStep(description="x", raw_event_ids=[ev.id])]

        n = enrich_resolution_independent_layout(m)
        assert n == 1
        layout = cs.target_context.signature["layout"]
        assert layout["dpi_scale"] == 1.5
        assert layout["capture_screen_size"] == {"w": 1920, "h": 1080}
        assert layout["target_bbox"] == {
            "left": 100, "top": 200, "width": 200, "height": 100,
        }
        assert layout["click_xy"] == [160, 240]
        # click center fuera del top-left por (60, 40); ratios = (.30, .40)
        assert abs(layout["click_offset_ratio"]["x_ratio"] - 0.30) < 1e-6
        assert abs(layout["click_offset_ratio"]["y_ratio"] - 0.40) < 1e-6
        # target en window: top-left (0.10, 0.25), tamaño (0.20, 0.125)
        ely = layout["element_layout_in_window"]
        assert abs(ely["left_ratio"] - 0.10) < 1e-6
        assert abs(ely["top_ratio"] - 0.25) < 1e-6
        assert abs(ely["width_ratio"] - 0.20) < 1e-6
        assert abs(ely["height_ratio"] - 0.125) < 1e-6
        # Estrategia de resolución prioritaria
        assert layout["resolution_strategy"][0] == "uia_dom"
        assert layout["resolution_strategy"][-1] == "absolute_emergency"

    def test_no_data_means_no_layout_entry(self):
        cs = CompiledStep(
            action_strategy=ActionStrategy.LAUNCH_APP,
            action_payload={"app_name": "Chrome"},
            target_context=TargetContext(),
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
        )
        m = Mission(name="t", id="layout2")
        m.compiled_execution_graph = [cs]
        n = enrich_resolution_independent_layout(m)
        # Sin raw_event ni metadata → no podemos calcular nada concreto;
        # sólo se inyecta resolution_strategy (default).
        layout = (cs.target_context.signature or {}).get("layout") or {}
        assert "resolution_strategy" in layout
        assert "click_offset_ratio" not in layout
