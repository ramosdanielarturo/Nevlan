from types import SimpleNamespace
from unittest.mock import patch

from app.contracts.mission import CompiledStep, TargetContext, TargetResolutionStrategy
from app.services.missions.player import MissionPlayer
from app.contracts.mission import Mission, MissionStatus


def _step_with_icon_bundle():
    cs = CompiledStep(
        target_resolution_strategy=TargetResolutionStrategy.COORDINATE_ICON_VALIDATED_CLICK,
        target_context=TargetContext(
            image_ref_mid="/tmp/mid.png",
            image_ref="/tmp/small.png",
            anchor_bbox={"left": 100, "top": 100, "width": 32, "height": 32},
            fallback_coords={"x": 200, "y": 200},
        ),
    )
    return cs


def test_resolve_icon_returns_region_and_score_meta():
    m = Mission(name="t", status=MissionStatus.APPROVED)
    m.compiled_execution_graph = []
    player = MissionPlayer(m)
    step = _step_with_icon_bundle()

    fake_box = SimpleNamespace(left=110, top=110, width=32, height=32)

    with patch(
        "pyautogui.locateOnScreen", return_value=fake_box,
    ):
        coords, meta = player._resolve_icon_validated_coords(step)

    assert coords is not None and "x" in coords
    assert meta.get("strategy_used") == "coordinate_icon_validated_click"
    assert meta.get("region_used") is not None
    assert meta.get("visual_match_score") == 0.92
    assert meta.get("full_screen_search") is False


def test_resolve_icon_no_anchor_skips_fullscreen():
    m = Mission(name="t", status=MissionStatus.APPROVED)
    player = MissionPlayer(m)
    step = CompiledStep(
        target_context=TargetContext(
            image_ref_mid="x.png",
        ),
    )
    c, meta = player._resolve_icon_validated_coords(step)
    assert c is None and meta.get("fallback_used") == "no_anchor_bbox"

