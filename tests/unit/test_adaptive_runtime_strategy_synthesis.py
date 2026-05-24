"""Tests — Adaptive Runtime Strategy Synthesis (ARSS)."""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.contracts.mission import (
    AdaptiveExecutionVariant,
    FourLayerOperationalModel,
    HumanViewLayer,
    MachineExecutionLayer,
    MachineExecutionStep,
    Mission,
    OperationalContractLayer,
    OperationalGoalLayer,
    OperationalRuntimeConsistencyReport,
    OperationalRuntimeObservation,
)
from app.services.missions.four_layer_operational_model import ensure_four_layer_model
from app.services.runtime.adaptive_runtime_strategy_synthesis import (
    RuntimeVariantMemoryStore,
    evaluate_variant_with_orce,
    inject_arss_strategies,
    process_arss_after_orce,
    promote_variant_to_machine_layer,
    synthesize_variants_from_divergence,
    validate_promotion_eligibility,
)
from app.services.runtime.operational_runtime_consistency_engine import expert_panel_lines


def _mission_with_4lom(
    *,
    step_id: str = "step_1",
    semantic_type: str = "submit_input",
    preferred: str = "submit_input:keyboard_enter",
    fallbacks: list[str] | None = None,
    purpose: str = "Complete the workflow",
) -> Mission:
    fb = fallbacks or ["submit_input:dom_button"]
    sep = {
        "steps": [
            {
                "id": step_id,
                "type": semantic_type,
                "params": {"query": "test"},
                "preferred_strategy": preferred,
                "fallback_strategies": fb,
            },
        ],
    }
    model = FourLayerOperationalModel(
        goal=OperationalGoalLayer(purpose_statement=purpose, summary=purpose),
        contract=OperationalContractLayer(criteria=[]),
        human_view=HumanViewLayer(steps=[]),
        machine_execution=MachineExecutionLayer(
            steps=[
                MachineExecutionStep(
                    step_id=step_id,
                    semantic_type=semantic_type,
                    params={"query": "test"},
                    preferred_strategy=preferred,
                    fallback_strategies=fb,
                ),
            ],
        ),
    )
    return Mission(
        id=str(uuid.uuid4()),
        name="test_arss",
        semantic_execution_plan=sep,
        four_layer_operational_model=model.model_dump(mode="json"),
    )


def _diverged_report(step_id: str, actual_path: list[str]) -> dict:
    return OperationalRuntimeConsistencyReport(
        consistency_status="DIVERGED",
        consistency_score=0.55,
        divergence_count=1,
        runtime_acceptance="NEEDS_HARDENING",
        step_summaries=[
            {
                "step_id": step_id,
                "expected_path": ["keyboard_enter"],
                "actual_path": actual_path,
                "status": "DIVERGED",
                "consistency_score": 0.45,
                "divergence_reasons": ["RUNTIME_PATH_DIVERGENCE"],
            },
        ],
    ).model_dump(mode="json")


@pytest.fixture
def arss_store(tmp_path: Path) -> RuntimeVariantMemoryStore:
    return RuntimeVariantMemoryStore(db_path=tmp_path / "arss_test.sqlite")


@pytest.fixture(autouse=True)
def enable_arss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARSS_ENABLED", "true")


class TestMechanicSynthesis:

    def test_submit_enter_fails_button_submit_variant(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom(semantic_type="submit_input")
        obs = OperationalRuntimeObservation(
            step_id="step_1",
            actual_runtime_path=["keyboard_enter"],
            actual_strategies_used=["submit_input:keyboard_enter"],
            divergence_detected=True,
            consistency_score=0.4,
        )
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            attempt = synthesize_variants_from_divergence(
                mission,
                step_id="step_1",
                observation=obs,
                simulated_replay_success="submit_input:uia_button",
            )
        assert attempt.generated_variants
        strategies = attempt.generated_variants[0].synthesized_strategies
        assert any("button" in s or "dom" in s for s in strategies)
        assert attempt.accepted_variant is not None

    def test_search_input_moved_contextual_variant(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom(
            semantic_type="search_content",
            preferred="search_content:dom_input",
        )
        obs = OperationalRuntimeObservation(
            step_id="step_1",
            actual_runtime_path=["dom_input"],
            divergence_detected=True,
        )
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            attempt = synthesize_variants_from_divergence(
                mission,
                step_id="step_1",
                observation=obs,
                simulated_replay_success="search_content:uia_textbox",
            )
        assert attempt.accepted_variant
        assert "uia_textbox" in attempt.accepted_variant.synthesized_strategies[0]

    def test_new_tab_affordance_alternate_mechanic(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom(
            semantic_type="open_new_tab",
            preferred="open_new_tab:hotkey_ctrl_t",
            fallbacks=["open_new_tab:uia_button"],
        )
        obs = OperationalRuntimeObservation(
            step_id="step_1",
            actual_runtime_path=["hotkey_ctrl_t"],
            divergence_detected=True,
        )
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            attempt = synthesize_variants_from_divergence(
                mission,
                step_id="step_1",
                observation=obs,
                simulated_replay_success="open_new_tab:uia_button",
            )
        assert attempt.generated_variants
        assert any(
            "uia_button" in s or "tab_affordance" in s
            for v in attempt.generated_variants
            for s in v.synthesized_strategies
        )


class TestVariantRejection:

    def test_variant_rejected_for_coords_usage(self):
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="s1",
            synthesized_strategies=["submit_input:relative_coords"],
            forbidden_strategy_used=True,
        )
        evaluated = evaluate_variant_with_orce(variant)
        ok, reasons = validate_promotion_eligibility(evaluated)
        assert not ok
        assert evaluated.consistency_score == 0.0
        assert evaluated.forbidden_strategy_used or "forbidden_strategy" in reasons

    def test_variant_rejected_for_low_consistency(self):
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="s1",
            synthesized_strategies=["submit_input:dom_button"],
            consistency_score=0.55,
            replay_stability_score=0.55,
            deterministic_score=0.55,
            validation_success_ratio=0.80,
            continuity_preserved=True,
            canonical_truth_preserved=True,
            successful_runs=2,
        )
        ok, reasons = validate_promotion_eligibility(variant)
        assert not ok
        assert any("consistency" in r for r in reasons)


class TestPromotion:

    def test_variant_promoted_after_stable_runs(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom()
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="step_1",
            synthesized_strategies=["submit_input:uia_button", "submit_input:dom_button"],
            consistency_score=0.95,
            replay_stability_score=0.93,
            deterministic_score=0.95,
            validation_success_ratio=0.96,
            continuity_preserved=True,
            canonical_truth_preserved=True,
            successful_runs=2,
            accepted_for_runtime=True,
        )
        ok, _ = validate_promotion_eligibility(variant)
        assert ok
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            promoted = promote_variant_to_machine_layer(mission, variant)
        assert promoted
        assert mission.arss_runtime_variants
        sep = mission.semantic_execution_plan or {}
        step = (sep.get("steps") or [])[0]
        assert step.get("preferred_strategy") == "submit_input:uia_button"


class TestLayerPreservation:

    def test_goal_preserved_on_promotion(self, arss_store: RuntimeVariantMemoryStore):
        purpose = "Unique goal statement XYZ"
        mission = _mission_with_4lom(purpose=purpose)
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="step_1",
            synthesized_strategies=["submit_input:dom_button"],
            consistency_score=0.95,
            replay_stability_score=0.93,
            deterministic_score=0.95,
            validation_success_ratio=0.96,
            continuity_preserved=True,
            successful_runs=2,
        )
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            promote_variant_to_machine_layer(mission, variant)
        model = ensure_four_layer_model(mission)
        assert model.goal.purpose_statement == purpose

    def test_contract_preserved(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom()
        contract_before = ensure_four_layer_model(mission).contract
        criteria_before = [c.code for c in contract_before.success_criteria]
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="step_1",
            synthesized_strategies=["submit_input:dom_button"],
            consistency_score=0.95,
            replay_stability_score=0.93,
            deterministic_score=0.95,
            validation_success_ratio=0.96,
            continuity_preserved=True,
            successful_runs=2,
        )
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            promote_variant_to_machine_layer(mission, variant)
        contract_after = ensure_four_layer_model(mission).contract
        criteria_after = [c.code for c in contract_after.success_criteria]
        assert criteria_before == criteria_after

    def test_human_view_unchanged(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom()
        human_before = ensure_four_layer_model(mission).human_view.model_dump()
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="step_1",
            synthesized_strategies=["submit_input:dom_button"],
            consistency_score=0.95,
            replay_stability_score=0.93,
            deterministic_score=0.95,
            validation_success_ratio=0.96,
            continuity_preserved=True,
            successful_runs=2,
        )
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            promote_variant_to_machine_layer(mission, variant)
        human_after = ensure_four_layer_model(mission).human_view.model_dump()
        assert human_before == human_after

    def test_machine_layer_updated_only_after_promotion(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom(preferred="submit_input:keyboard_enter")
        before = ensure_four_layer_model(mission).machine_execution.steps[0].preferred_strategy
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="step_1",
            synthesized_strategies=["submit_input:dom_button"],
            consistency_score=0.95,
            replay_stability_score=0.93,
            deterministic_score=0.95,
            validation_success_ratio=0.96,
            continuity_preserved=True,
            successful_runs=2,
            accepted_for_runtime=True,
        )
        assert before == "submit_input:keyboard_enter"
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            promote_variant_to_machine_layer(mission, variant)
        after = ensure_four_layer_model(mission).machine_execution.steps[0].preferred_strategy
        assert after == "submit_input:dom_button"


class TestORCEAndReplay:

    def test_orce_validates_variant(self):
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="s1",
            synthesized_strategies=["submit_input:dom_button"],
        )
        obs = OperationalRuntimeObservation(
            step_id="s1",
            coords_used=False,
            smart_route_used=False,
            divergence_detected=False,
        )
        evaluated = evaluate_variant_with_orce(variant, observation=obs, simulated_success=True)
        assert evaluated.consistency_score >= 0.90

    def test_replay_deterministic(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom()
        variant = AdaptiveExecutionVariant(
            variant_id="det_var_1",
            parent_machine_step_id="step_1",
            synthesized_strategies=["submit_input:dom_button"],
            consistency_score=0.95,
            replay_stability_score=0.93,
            deterministic_score=0.95,
            validation_success_ratio=0.96,
            continuity_preserved=True,
            accepted_for_runtime=True,
            successful_runs=2,
        )
        arss_store.save_variant(variant, mission_id=mission.id, semantic_type="submit_input")

        class _Step:
            id = "step_1"
            params = {"preferred_strategy": "submit_input:keyboard_enter"}

        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.arss_enabled",
            return_value=True,
        ), patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            injected = inject_arss_strategies(_Step(), ["submit_input:keyboard_enter"], mission)
        assert injected[0] == "submit_input:keyboard_enter"
        assert "submit_input:dom_button" in injected


class TestMemoryAndLineage:

    def test_runtime_variant_memory_persists(self, arss_store: RuntimeVariantMemoryStore):
        variant = AdaptiveExecutionVariant(
            variant_id="mem_var_1",
            parent_machine_step_id="step_x",
            synthesized_strategies=["search_content:dom_input"],
            accepted_for_runtime=True,
            consistency_score=0.92,
        )
        arss_store.save_variant(variant, mission_id="m1", semantic_type="search_content")
        loaded = arss_store.get_accepted_for_step("step_x")
        assert len(loaded) == 1
        assert loaded[0].variant_id == "mem_var_1"

    def test_variant_lineage_visible(self):
        mission = _mission_with_4lom()
        mission.arss_runtime_variants = [
            AdaptiveExecutionVariant(
                parent_machine_step_id="step_1",
                synthesized_strategies=["submit_input:dom_button"],
                synthesis_reason="alternative_mechanic_1:submit_input",
                promoted_to_machine_layer=True,
                consistency_score=0.95,
                replay_stability_score=0.93,
            ).model_dump(mode="json"),
        ]
        lines = expert_panel_lines(mission)
        assert any("ARSS" in ln for ln in lines)


class TestTruthAndContinuity:

    def test_canonical_truth_preserved(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom()
        obs = OperationalRuntimeObservation(step_id="step_1", divergence_detected=True)
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            attempt = synthesize_variants_from_divergence(
                mission, step_id="step_1", observation=obs,
            )
        for v in attempt.generated_variants:
            assert v.canonical_truth_preserved

    def test_continuity_preserved(self):
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="s1",
            synthesized_strategies=["submit_input:dom_button"],
            continuity_preserved=True,
            consistency_score=0.95,
            replay_stability_score=0.93,
            deterministic_score=0.95,
            validation_success_ratio=0.96,
            successful_runs=2,
        )
        ok, reasons = validate_promotion_eligibility(variant)
        assert ok
        assert "continuity_break" not in reasons


class TestForbiddenAndQuarantine:

    def test_forbidden_strategies_rejected(self):
        variant = AdaptiveExecutionVariant(
            parent_machine_step_id="s1",
            synthesized_strategies=["search_content:smart_route"],
            forbidden_strategy_used=True,
            consistency_score=0.0,
        )
        ok, _ = validate_promotion_eligibility(variant)
        assert not ok

    def test_weak_variants_quarantined(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom()
        obs = OperationalRuntimeObservation(step_id="step_1", divergence_detected=True)
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            attempt = synthesize_variants_from_divergence(
                mission, step_id="step_1", observation=obs,
            )
        weak = [v for v in attempt.generated_variants if v.consistency_score < 0.90]
        for v in weak:
            ok, _ = validate_promotion_eligibility(v)
            assert not ok

    def test_runtime_prefers_stable_variants(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom()
        stable = AdaptiveExecutionVariant(
            variant_id="stable_v",
            parent_machine_step_id="step_1",
            synthesized_strategies=["submit_input:dom_button"],
            accepted_for_runtime=True,
            consistency_score=0.95,
            successful_runs=3,
        )
        arss_store.save_variant(stable, mission_id=mission.id, semantic_type="submit_input")

        class _Step:
            id = "step_1"
            params = {}

        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.arss_enabled",
            return_value=True,
        ), patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            result = inject_arss_strategies(
                _Step(),
                ["submit_input:keyboard_enter", "legacy:fallback"],
                mission,
            )
        assert result.index("submit_input:dom_button") < result.index("legacy:fallback")


class TestNoHardcoding:

    def test_no_hardcoded_apps_in_module(self):
        from app.services.runtime import adaptive_runtime_strategy_synthesis as arss

        source = inspect.getsource(arss)
        forbidden = ["chrome", "youtube", "windows", "microsoft", "google"]
        low = source.lower()
        for word in forbidden:
            assert word not in low, f"hardcoded: {word}"


class TestProcessAfterOrce:

    def test_process_arss_after_orce(self, arss_store: RuntimeVariantMemoryStore):
        mission = _mission_with_4lom(semantic_type="submit_input")
        mission.operational_runtime_consistency_report = _diverged_report(
            "step_1", ["keyboard_enter"],
        )
        with patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.arss_enabled",
            return_value=True,
        ), patch(
            "app.services.runtime.adaptive_runtime_strategy_synthesis.get_runtime_variant_memory_store",
            return_value=arss_store,
        ):
            attempts = process_arss_after_orce(mission)
        assert attempts
        assert mission.arss_synthesis_audit
