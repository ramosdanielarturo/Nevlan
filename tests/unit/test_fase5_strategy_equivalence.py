"""Tests FASE 5 — equivalencia controlada de strategy paths."""
from __future__ import annotations

from app.services.runtime.fase5_deterministic_gate import (
    FASE5_ACCEPTANCE_STATUS,
    diff_normalized_strategy_paths,
    diff_strategy_paths,
    fase5_gate_violations,
)
from app.services.runtime.fase5_strategy_equivalence import (
    CONTRACT_SATISFIED_NO_OP,
    extract_normalized_strategy_path,
    normalize_step_strategy,
)


def _identity_skip_step(strategy: str, **extra) -> dict:
    return {
        "step_id": "m:p01",
        "kind": "select_profile",
        "status": "skipped",
        "strategy_used": strategy,
        "validation_passed": True,
        "contract_satisfied": True,
        "orce_consistency_score": 0.95,
        "failure_reason": "",
        "coords_used_step": False,
        "smart_route_used": False,
        **extra,
    }


def _browse_step(strategy: str, **extra) -> dict:
    return {
        "step_id": "m:p05",
        "kind": "scroll_results",
        "status": "skipped" if strategy == "skip:already_satisfied" else "success",
        "strategy_used": strategy,
        "validation_passed": extra.pop("validation_passed", True),
        "contract_satisfied": extra.pop("contract_satisfied", True),
        "orce_consistency_score": extra.pop("orce_consistency_score", 0.95),
        "failure_reason": "",
        "coords_used_step": False,
        "smart_route_used": False,
        **extra,
    }


class TestAlreadySatisfiedEquivalence:
    def test_skip_and_identity_context_equivalent_when_contract_satisfied(self):
        skip = _identity_skip_step("skip:already_satisfied")
        identity = _identity_skip_step("select_identity_context:already_satisfied")
        assert normalize_step_strategy(skip)[0] == CONTRACT_SATISFIED_NO_OP
        assert normalize_step_strategy(identity)[0] == CONTRACT_SATISFIED_NO_OP

    def test_skip_without_validation_not_equivalent(self):
        step = _identity_skip_step(
            "skip:already_satisfied",
            validation_passed=False,
            contract_satisfied=False,
            status="skipped",
        )
        normalized, reason = normalize_step_strategy(step)
        assert normalized == "skip:already_satisfied"
        assert reason == ""


class TestBrowseScrollEquivalence:
    def test_browse_skip_equivalent_with_validated_contract(self):
        step = _browse_step("skip:already_satisfied")
        normalized, reason = normalize_step_strategy(step)
        assert normalized == CONTRACT_SATISFIED_NO_OP
        assert reason == "browse_scroll_skip_contract_validated"

    def test_browse_skip_without_evidence_stays_divergent(self):
        step = _browse_step(
            "skip:already_satisfied",
            validation_passed=False,
            contract_satisfied=False,
            orce_consistency_score=0.5,
        )
        normalized, _ = normalize_step_strategy(step)
        assert normalized == "skip:already_satisfied"

    def test_browse_executed_success_normalizes_to_no_op(self):
        step = _browse_step("browse_results:uol_scroll_surface", status="success")
        normalized, reason = normalize_step_strategy(step)
        assert normalized == CONTRACT_SATISFIED_NO_OP
        assert reason == "browse_scroll_executed_contract_satisfied"


class TestForbiddenDegradationBreaksEquivalence:
    def test_coords_break_equivalence(self):
        step = _identity_skip_step("skip:already_satisfied", coords_used_step=True)
        normalized, _ = normalize_step_strategy(step)
        assert normalized == "skip:already_satisfied"

    def test_smart_route_breaks_equivalence(self):
        step = _identity_skip_step("skip:already_satisfied", smart_route_used=True)
        normalized, _ = normalize_step_strategy(step)
        assert normalized == "skip:already_satisfied"


class TestNormalizedDiffReporting:
    def test_normalized_diff_empty_when_raw_differs_legitimately(self):
        raw_paths = [
            [
                {"step_id": "m:p01", "kind": "select_profile", "strategy_used": "select_identity_context:already_satisfied"},
                {"step_id": "m:p05", "kind": "scroll_results", "strategy_used": "browse_results:uol_scroll_surface"},
            ],
            [
                {"step_id": "m:p01", "kind": "select_profile", "strategy_used": "skip:already_satisfied"},
                {"step_id": "m:p05", "kind": "scroll_results", "strategy_used": "skip:already_satisfied"},
            ],
        ]
        steps_runs = [
            [
                _identity_skip_step("select_identity_context:already_satisfied"),
                _browse_step("browse_results:uol_scroll_surface", status="success"),
            ],
            [
                _identity_skip_step("skip:already_satisfied"),
                _browse_step("skip:already_satisfied"),
            ],
        ]
        normalized_paths = [
            extract_normalized_strategy_path(run) for run in steps_runs
        ]
        assert diff_strategy_paths(steps_runs)
        assert diff_normalized_strategy_paths(normalized_paths) == []

    def test_raw_diff_preserved_in_report_shape(self):
        steps_runs = [
            [_identity_skip_step("select_identity_context:already_satisfied")],
            [_identity_skip_step("skip:already_satisfied")],
        ]
        raw_diff = diff_strategy_paths(steps_runs)
        assert raw_diff
        assert "select_identity_context:already_satisfied" in raw_diff[0]
        assert "skip:already_satisfied" in raw_diff[0]


class TestFase5GateUsesNormalizedDiff:
    def test_gate_passes_on_normalized_empty_raw_informative(self):
        report = {
            "runs": [
                {"run_index": 1, "player_used": "smart", "coords_used": False, "smart_route_count": 0, "legacy_fork": False},
                {"run_index": 2, "player_used": "smart", "coords_used": False, "smart_route_count": 0, "legacy_fork": False},
                {"run_index": 3, "player_used": "smart", "coords_used": False, "smart_route_count": 0, "legacy_fork": False},
            ],
            "strategy_path_diff": [
                "run2_step1:m:p01 'select_identity_context:already_satisfied'!='skip:already_satisfied'",
            ],
            "strategy_path_diff_normalized": [],
            "coords_used": False,
            "smart_route_count": 0,
            "runtime_entrypoint": "smart_runner_bridge",
            "forbidden_preferred_count": 0,
            "environment_reset_failures": 0,
            "determinism_status": FASE5_ACCEPTANCE_STATUS,
        }
        violations = fase5_gate_violations(report)
        assert not any("strategy_path_diff" in v for v in violations)

    def test_gate_fails_when_normalized_diff_non_empty(self):
        report = {
            "runs": [],
            "strategy_path_diff": [],
            "strategy_path_diff_normalized": [
                "run2_step0:m:p05 'browse_results:uol_scroll_surface'!='skip:already_satisfied'",
            ],
            "coords_used": False,
            "smart_route_count": 0,
            "runtime_entrypoint": "smart_runner_bridge",
            "forbidden_preferred_count": 0,
            "environment_reset_failures": 0,
            "determinism_status": "REJECTED",
        }
        violations = fase5_gate_violations(report)
        assert any("strategy_path_diff_normalized" in v for v in violations)
