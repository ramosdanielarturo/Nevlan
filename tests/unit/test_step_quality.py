from app.contracts.mission import (
    CompiledStep, InterpretedStep, TargetContext, ActionStrategy, ValidationStrategy,
)
from app.services.missions.step_quality import compute_step_quality, count_weak_steps


def _minimal_click_step() -> CompiledStep:
    return CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=TargetContext(confidence={
            "capture_score": 30.0,
            "capture_reasons": [],
        }),
    )


def test_abs_only_fallback_is_red_even_if_score_not_zero():
    cs = CompiledStep(action_strategy=ActionStrategy.CLICK, target_context=TargetContext())
    tc = cs.target_context
    tc.confidence = {"capture_score": 55.0, "capture_reasons": ["test"]}
    tc.fallback_coords = {"x": 10, "y": 20}
    assert compute_step_quality(cs, None).level == "red"


def test_strong_signals_push_green_bundle():
    cs = CompiledStep(action_strategy=ActionStrategy.CLICK, target_context=TargetContext())
    tc = cs.target_context
    tc.web_data = {
        "test_id": "ok",
        "locators": [{"kind": "test_id", "value": "x"}],
    }
    tc.uia_data = {"automation_id": "aid"}
    tc.anchor_bbox = {"left": 0, "top": 0, "width": 60, "height": 22}
    tc.image_ref_mid = "mid.png"
    tc.fallback_coords_relative_to_window = {"fx": 0.1, "fy": 0.2}
    tc.confidence = {"capture_score": 76.0, "capture_reasons": ["mixed"]}
    cs.validation_strategy = ValidationStrategy.REQUIRE_ELEMENT_EXISTS
    assert compute_step_quality(cs, InterpretedStep(description="")).level in (
        "green", "yellow",
    )


def test_interpreted_level_combined_worst():
    cs = _minimal_click_step()
    cs.target_context.confidence = {"capture_score": 90.0, "capture_reasons": []}
    i = InterpretedStep(description="", quality_level="yellow")
    assert compute_step_quality(cs, i).level == "yellow"


def test_count_weak_matches_red_levels():
    w1 = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=TargetContext(
            confidence={"capture_score": 12.0},
            fallback_coords={"x": 1, "y": 2},
        ),
    )
    w2 = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=TargetContext(
            confidence={"capture_score": 20.0},
            fallback_coords={"x": 3, "y": 4},
        ),
    )
    xs = count_weak_steps(
        [w1, w2],
        [InterpretedStep(description=""), InterpretedStep(description="")],
    )
    assert xs == 2
