"""Reforzar imagen: copia a assets internos y actualiza metadatos."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    TargetContext,
    TargetResolutionStrategy,
    ValidationStrategy,
)
from app.services.missions.image_asset_repair import reinforce_step_from_capture_bundle


@pytest.fixture
def fake_var(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "VAR_PATH", tmp_path)
    return tmp_path


def test_reinforce_copies_images(fake_var, tmp_path):
    small = tmp_path / "a.png"
    mid = tmp_path / "b.png"
    small.write_bytes(b"fakepng")
    mid.write_bytes(b"fakepng")
    step = CompiledStep(
        id="step-xyz",
        action_strategy=ActionStrategy.CLICK,
        action_payload={},
        target_resolution_strategy=TargetResolutionStrategy.UIA_THEN_VISION,
        target_context=TargetContext(),
        validation_strategy=ValidationStrategy.NONE,
    )
    bundle = {
        "image_ref": str(small),
        "image_ref_mid": str(mid),
        "fallback_coords": {"x": 1, "y": 2},
    }
    r = reinforce_step_from_capture_bundle(step, "mid-1", bundle)
    assert r.copied_small and Path(r.copied_small).is_file()
    assert "missions" in str(r.copied_small).replace("\\", "/") or "missions" in str(
        Path(r.copied_small)
    )


def test_friendly_resolver_coordinate_icon():
    from app.services.missions.execution_ui import friendly_resolver_summary

    n, x = friendly_resolver_summary(
        "coordinate_icon_validated_click",
        {"visual_match_score": 0.88, "region_used": "(0,0,100,100)"},
    )
    assert "ícono" in n.lower()
    assert "0.88" in x or "score=" in x
