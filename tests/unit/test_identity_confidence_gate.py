"""Unit tests — universal identity confidence gate."""
from __future__ import annotations

from app.services.missions.execution_contracts import MissionStep
from app.services.missions.identity_confidence_gate import (
    NEEDS_REVIEW,
    evaluate_identity_gate,
    filter_strategies_identity_first,
    has_semantic_plan_mandate,
    score_step_identity,
)
from app.services.runtime.nevlan_runtime_convergence import (
    is_forbidden_primary_strategy,
)


def test_missing_identity_returns_needs_review_no_coords():
    step = MissionStep(id="s1", kind="click_named_item", params={})
    gate = evaluate_identity_gate(step)
    assert gate.allowed is False
    assert gate.failure_code == NEEDS_REVIEW
    assert "target_text_or_anchor" in gate.missing

    strategies = filter_strategies_identity_first(
        [
            "click_named_item:uia",
            "click_named_item:relative_coords",
            "click_named_item:vision_coords",
        ],
        step,
    )
    assert not any(is_forbidden_primary_strategy(s) for s in strategies[:1])


def test_semantic_plan_blocks_coords_primary():
    step = MissionStep(
        id="s2",
        kind="click_named_item",
        params={
            "preferred_strategy": "click_named_item:uia_name",
            "fallback_strategies": [
                "click_named_item:ocr",
                "click_named_item:relative_coords",
            ],
            "target_name": "Chrome",
        },
    )
    assert has_semantic_plan_mandate(step)
    gate = evaluate_identity_gate(step)
    assert gate.allowed is True

    ordered = filter_strategies_identity_first(
        [
            "click_named_item:uia_name",
            "click_named_item:ocr",
            "click_named_item:relative_coords",
        ],
        step,
    )
    assert ordered == ["click_named_item:uia_name", "click_named_item:ocr"]
    assert not any(is_forbidden_primary_strategy(s) for s in ordered)


def test_strong_target_identity_passes_gate():
    step = MissionStep(
        id="s3",
        kind="type_text",
        params={
            "text": "hello",
            "target_identity": {"confidence": 0.88, "label": "search box"},
        },
    )
    score, missing, signals = score_step_identity(step)
    assert score >= 0.65
    assert not missing
    assert evaluate_identity_gate(step).allowed is True
    assert "target_identity" in signals
