"""Tests switch_context UOL handler + contrato runtime."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.execution_contracts import MissionStep, build_contract
from app.services.missions.state_detector import StateSnapshot
from app.services.missions.uol_action_handlers import (
    CONTEXT_SWITCH_NOT_CONFIRMED,
    CONTEXT_SURFACE_NOT_READY,
    CONTEXT_TARGET_NOT_FOUND,
    MODAL_NOT_TRIGGERED,
    SWITCH_CONTEXT_RESOLUTION_SUBSTEPS,
    is_uol_strategy,
    try_uol_first_runtime,
    uol_switch_context,
)
from app.services.missions.universal_operational_language import (
    compile_semantic_step_to_uol,
    enrich_mission_step_with_uol,
)
from app.services.runtime.operational_runtime_consistency_engine import (
    build_runtime_expectation_for_machine_step,
    reconcile_step,
    observation_from_uol_record,
    switch_context_substeps_valid,
)
from app.services.runtime.nevlan_runtime_convergence import is_forbidden_primary_strategy
from app.contracts.mission import MachineExecutionStep
from tests.fixtures.fase7_missions.confirmation_dialog import build_confirmation_dialog_mission


def _step(**params) -> MissionStep:
    return MissionStep(id="s:switch", kind="switch_context", params=params)


class TestSwitchContextUolCompilation:
    def test_switch_context_compiles_to_uol_with_preferred_strategy(self):
        action = compile_semantic_step_to_uol(
            {"type": "switch_context", "params": {"action": "close_unsaved"}},
        )
        assert action is not None
        assert action.uol_action.value == "switch_context"
        assert action.location_hint == "close_unsaved"
        assert action.execution_contract.preferred_runtime_strategies[0] == "switch_context:uol_context"

    def test_target_window_params_normalized(self):
        action = compile_semantic_step_to_uol(
            {
                "type": "switch_context",
                "params": {"action": "focus_foreground", "window": "Notepad"},
            },
        )
        assert action is not None
        assert action.target_display_name == "Notepad"

    def test_enrich_mission_step_sets_uol_strategy(self):
        step = enrich_mission_step_with_uol(
            MissionStep(
                id="p01",
                kind="switch_context",
                params={"action": "close_unsaved"},
            ),
        )
        assert step.params.get("uol_action") == "switch_context"
        assert step.params.get("preferred_strategy") == "switch_context:uol_context"
        assert is_uol_strategy(str(step.params.get("preferred_strategy")))

    def test_build_contract_not_unknown_kind(self):
        contract = build_contract(_step(action="close_unsaved"))
        assert not getattr(contract, "is_unknown_kind", False)
        assert contract.preferred_strategy == "switch_context:uol_context"


class TestSwitchContextHandlerSuccess:
    @patch("app.services.missions.uol_action_handlers._detect_modal_or_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._hotkey", return_value=True)
    @patch("app.services.missions.uol_action_handlers._mark_editor_dirty", return_value=True)
    @patch("app.services.missions.uol_action_handlers._ensure_foreground_editor", return_value=True)
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_close_unsaved_triggers_modal(self, _ready, _fg, _mark, _hk, _modal):
        res = uol_switch_context(_step(action="close_unsaved"), StateSnapshot())
        assert res.ok
        assert res.strategy == "switch_context:uol_context"
        assert set(res.extra.get("runtime_resolution_substeps") or []) >= set(
            SWITCH_CONTEXT_RESOLUTION_SUBSTEPS,
        )

    @patch("app.services.missions.uol_action_handlers._focus_foreground_window", return_value=(True, "Notepad"))
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_focus_foreground_success(self, _ready, _focus):
        res = uol_switch_context(_step(action="focus_foreground"), StateSnapshot())
        assert res.ok
        assert res.extra.get("validation_status") == "passed"

    @patch("app.services.missions.uol_action_handlers._activate_window_by_title", return_value=(True, "Notepad"))
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_activate_window_by_target(self, _ready, _act):
        res = uol_switch_context(
            _step(action="focus_foreground", target="Notepad"),
            StateSnapshot(),
        )
        assert res.ok


class TestSwitchContextHandlerFailures:
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(False, CONTEXT_SURFACE_NOT_READY))
    def test_surface_not_ready(self, _ready):
        res = uol_switch_context(_step(action="close_unsaved"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("context_failure_reason") == CONTEXT_SURFACE_NOT_READY

    @patch("app.services.missions.uol_action_handlers._detect_modal_or_dialog", return_value=False)
    @patch("app.services.missions.uol_action_handlers._hotkey", return_value=True)
    @patch("app.services.missions.uol_action_handlers._mark_editor_dirty", return_value=True)
    @patch("app.services.missions.uol_action_handlers._ensure_foreground_editor", return_value=True)
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_modal_not_triggered(self, _ready, _fg, _mark, _hk, _modal):
        res = uol_switch_context(_step(action="close_unsaved"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("context_failure_reason") == MODAL_NOT_TRIGGERED

    @patch("app.services.missions.uol_action_handlers._focus_foreground_window", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_context_target_not_found(self, _ready, _focus):
        res = uol_switch_context(_step(action="focus_foreground"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("context_failure_reason") == CONTEXT_TARGET_NOT_FOUND

    @patch("app.services.missions.uol_action_handlers._activate_window_by_title", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_named_window_not_found(self, _ready, _act):
        res = uol_switch_context(
            _step(action="focus_foreground", target="Missing"),
            StateSnapshot(),
        )
        assert not res.ok
        assert res.extra.get("context_failure_reason") == CONTEXT_TARGET_NOT_FOUND


class TestSwitchContextNoForbiddenStrategies:
    def test_strategy_not_coords_or_smart_route(self):
        assert not is_forbidden_primary_strategy("switch_context:uol_context")
        assert not is_forbidden_primary_strategy("switch_context:focus_foreground")


class TestOrceSwitchContextPath:
    def test_orce_path_compatible_on_success(self):
        exp = build_runtime_expectation_for_machine_step(
            MachineExecutionStep(step_id="p01", semantic_type="switch_context"),
        )
        obs = observation_from_uol_record(
            {
                "step_id": "p01",
                "handler_used": "switch_context:uol_context",
                "validation_passed": True,
                "runtime_resolution_substeps": list(SWITCH_CONTEXT_RESOLUTION_SUBSTEPS),
            },
        )
        reconciled = reconcile_step(exp, obs)
        assert switch_context_substeps_valid(
            list(SWITCH_CONTEXT_RESOLUTION_SUBSTEPS),
            validation_passed=True,
        )
        assert not reconciled.divergence_detected


class TestFase7MissionHasStrategy:
    @patch("app.services.missions.uol_action_handlers.run_uol_strategy")
    def test_confirmation_dialog_switch_step_gets_uol_strategy(self, mock_run):
        mock_run.return_value = MagicMock(
            ok=True,
            strategy="switch_context:uol_context",
            message="ok",
            duration_ms=1,
            extra={
                "validation_status": "passed",
                "runtime_resolution_substeps": list(SWITCH_CONTEXT_RESOLUTION_SUBSTEPS),
            },
        )
        mission = build_confirmation_dialog_mission()
        from app.services.missions.semantic_execution_plan import (
            semantic_plan_to_mission_steps,
            SemanticExecutionPlan,
        )

        steps = semantic_plan_to_mission_steps(
            SemanticExecutionPlan.from_dict(mission.semantic_execution_plan),
        )
        switch = next(s for s in steps if s.kind == "switch_context")
        enriched = enrich_mission_step_with_uol(switch, mission)
        assert enriched.params.get("preferred_strategy") == "switch_context:uol_context"
        hook = try_uol_first_runtime(enriched, StateSnapshot(), mission)
        assert hook.attempted
        assert hook.strategy_used in ("switch_context:uol_context", "switch_context:focus_foreground")
        assert hook.strategy_used != ""
