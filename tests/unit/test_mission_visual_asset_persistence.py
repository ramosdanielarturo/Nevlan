"""
Misión con click visual: los PNG referenciados en raw_trace deben existir
en disco tras promoción/finalize (criterio: JSON sin rutas falsas).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.contracts.mission import (
    EventType,
    Mission,
    MouseAction,
    RawEvent,
)
from app.services.missions.asset_finalizer import (
    finalize_mission_assets,
    has_temp_paths,
    promote_raw_event_snapshots,
)
from app.services.missions.asset_validation import (
    is_safe_png_path,
    sanitize_mission_image_refs,
)
from app.services.missions.compiler import MissionCompiler

_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_PNG_TAIL = (
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x0aIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n\x2d\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_PNG_HEADER + _PNG_TAIL)


def _click_raw_event(
    *,
    mission_id: str,
    temp_dir: Path,
    step_id: str = "ev-click-1",
) -> RawEvent:
    small = temp_dir / f"{step_id}_small.png"
    mid = temp_dir / f"{step_id}_mid.png"
    full = temp_dir / f"{step_id}_full.png"
    _write_png(small)
    _write_png(mid)
    _write_png(full)
    return RawEvent(
        id=step_id,
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=10, y=20, button="left"),
        snapshot_ref=str(small),
        metadata={
            "snapshot_small": str(small),
            "snapshot_mid": str(mid),
            "snapshot_full": str(full),
            "target_signature": {
                "vision": {"ref_mid": str(mid), "ref_small": str(small)},
            },
        },
    )


def _assert_all_snapshot_paths_exist(ev: RawEvent) -> None:
    assert ev.snapshot_ref and is_safe_png_path(ev.snapshot_ref)
    md = ev.metadata or {}
    for key in ("snapshot_small", "snapshot_mid", "snapshot_full"):
        p = md.get(key)
        assert p, f"missing metadata[{key}]"
        assert is_safe_png_path(p), f"invalid or missing file: {p}"
    assert "/assets/temp/" not in str(ev.snapshot_ref).replace("\\", "/").lower()
    for key in ("snapshot_small", "snapshot_mid", "snapshot_full"):
        assert "/assets/temp/" not in str(md[key]).replace("\\", "/").lower()


@pytest.fixture
def var_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.missions.asset_finalizer.settings.VAR_PATH",
        tmp_path,
    )
    return tmp_path


class TestPromoteRawEventSnapshots:
    def test_promotes_snapshot_ref_mid_full_from_temp(self, var_path):
        mission_id = "mission-visual-1"
        temp_dir = var_path / "missions" / "assets" / "temp"
        ev = _click_raw_event(mission_id=mission_id, temp_dir=temp_dir)

        n = promote_raw_event_snapshots(ev, mission_id)
        assert n >= 3
        _assert_all_snapshot_paths_exist(ev)
        dest = var_path / "missions" / "assets" / mission_id
        assert dest.is_dir()
        assert list(dest.glob("*.png"))


class TestCompileMissionFinalizesAssets:
    def test_compile_mission_moves_temp_snapshots_and_files_exist(self, var_path):
        mission_id = "mission-compile-visual"
        temp_dir = var_path / "missions" / "assets" / "temp"
        ev = _click_raw_event(mission_id=mission_id, temp_dir=temp_dir)
        m = Mission(id=mission_id, name="visual click")
        m.raw_trace = [ev]

        compiled = MissionCompiler.compile_mission(m)
        assert compiled.raw_trace
        _assert_all_snapshot_paths_exist(compiled.raw_trace[0])
        assert not has_temp_paths(compiled)


class TestSanitizeRawTraceImageRefs:
    def test_sanitize_blanks_orphan_snapshot_mid_in_raw_trace(self, var_path):
        orphan = str(var_path / "missions" / "assets" / "temp" / "gone_mid.png")
        m = Mission(name="t")
        m.raw_trace = [
            RawEvent(
                event_type=EventType.MOUSE_CLICK,
                mouse_action=MouseAction(x=1, y=2, button="left"),
                metadata={"snapshot_mid": orphan},
            )
        ]
        n = sanitize_mission_image_refs(m)
        assert n == 1
        assert m.raw_trace[0].metadata.get("snapshot_mid") is None
