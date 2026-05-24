"""Tests confirm_dialog UOL handler + contrato runtime."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.execution_contracts import MissionStep, build_contract
from app.services.missions.state_detector import StateSnapshot
from app.services.missions.uol_action_handlers import (
    CONFIRM_DIALOG_RESOLUTION_SUBSTEPS,
    DIALOG_CHOICE_NOT_FOUND,
    DIALOG_DISMISS_NOT_CONFIRMED,
    DIALOG_NOT_FOUND,
    DIALOG_SURFACE_NOT_READY,
    is_uol_strategy,
    try_uol_first_runtime,
    uol_confirm_dialog,
)
from app.services.missions.universal_operational_language import (
    compile_semantic_step_to_uol,
    enrich_mission_step_with_uol,
)
from app.services.runtime.operational_runtime_consistency_engine import (
    build_runtime_expectation_for_machine_step,
    confirm_dialog_substeps_valid,
    reconcile_step,
    observation_from_uol_record,
)
from app.services.runtime.nevlan_runtime_convergence import is_forbidden_primary_strategy
from app.contracts.mission import MachineExecutionStep
from tests.fixtures.fase7_missions.confirmation_dialog import build_confirmation_dialog_mission


def _step(**params) -> MissionStep:
    return MissionStep(id="s:confirm", kind="confirm_dialog", params=params)


class TestConfirmDialogUolCompilation:
    def test_confirm_dialog_compiles_to_uol_with_preferred_strategy(self):
        action = compile_semantic_step_to_uol(
            {"type": "confirm_dialog", "params": {"choice": "accept"}},
        )
        assert action is not None
        assert action.uol_action.value == "confirm_action"
        assert action.location_hint == "accept"
        assert action.execution_contract.preferred_runtime_strategies[0] == "confirm_dialog:uol_dialog"

    def test_choice_answer_params_normalized(self):
        action = compile_semantic_step_to_uol(
            {"type": "confirm_dialog", "params": {"answer": "cancel", "label": "Cancel"}},
        )
        assert action is not None
        assert action.location_hint == "cancel"
        assert action.target_display_name == "Cancel"

    def test_enrich_mission_step_sets_uol_strategy(self):
        step = enrich_mission_step_with_uol(
            MissionStep(
                id="p02",
                kind="confirm_dialog",
                params={"choice": "accept"},
            ),
        )
        assert step.params.get("uol_action") == "confirm_action"
        assert step.params.get("preferred_strategy") == "confirm_dialog:uol_dialog"
        assert is_uol_strategy(str(step.params.get("preferred_strategy")))

    def test_build_contract_not_unknown_kind(self):
        contract = build_contract(_step(choice="accept"))
        assert not getattr(contract, "is_unknown_kind", False)
        assert contract.preferred_strategy == "confirm_dialog:uol_dialog"


class TestConfirmDialogHandlerSuccess:
    @patch("app.services.missions.uol_action_handlers._validate_dialog_dismissed", return_value=True)
    @patch("app.services.missions.uol_action_handlers._click_dialog_button", return_value=(True, "No guardar"))
    @patch("app.services.missions.uol_action_handlers._ensure_dialog_foreground", return_value=True)
    @patch("app.services.missions.uol_action_handlers._scan_visible_windows_for_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._dialog_surface_ready", return_value=(True, ""))
    def test_accept_choice_dismisses_dialog(self, _ready, _scan, _fg, _click, _val):
        res = uol_confirm_dialog(_step(choice="accept"), StateSnapshot())
        assert res.ok
        assert res.strategy == "confirm_dialog:uol_dialog"
        assert set(res.extra.get("runtime_resolution_substeps") or []) >= set(
            CONFIRM_DIALOG_RESOLUTION_SUBSTEPS,
        )
        _click.assert_called_once()

    @patch("app.services.missions.uol_action_handlers._validate_dialog_dismissed", return_value=True)
    @patch("app.services.missions.uol_action_handlers._click_dialog_button", return_value=(True, "Guardar"))
    @patch("app.services.missions.uol_action_handlers._ensure_dialog_foreground", return_value=True)
    @patch("app.services.missions.uol_action_handlers._scan_visible_windows_for_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._dialog_surface_ready", return_value=(True, ""))
    def test_save_choice_clicks_save(self, _ready, _scan, _fg, _click, _val):
        res = uol_confirm_dialog(_step(choice="save"), StateSnapshot())
        assert res.ok
        args = _click.call_args[0][0]
        assert "Guardar" in args or "Save" in args


class TestConfirmDialogHandlerFailures:
    @patch("app.services.missions.uol_action_handlers._dialog_surface_ready", return_value=(False, DIALOG_SURFACE_NOT_READY))
    def test_surface_not_ready(self, _ready):
        res = uol_confirm_dialog(_step(choice="accept"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("dialog_failure_reason") == DIALOG_SURFACE_NOT_READY

    @patch("app.services.missions.uol_action_handlers._click_dialog_button", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._press", return_value=False)
    @patch("app.services.missions.uol_action_handlers._ensure_dialog_foreground", return_value=True)
    @patch("app.services.missions.uol_action_handlers._scan_visible_windows_for_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._dialog_surface_ready", return_value=(True, ""))
    def test_choice_not_found(self, _ready, _scan, _fg, _press, _click):
        res = uol_confirm_dialog(_step(choice="accept"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("dialog_failure_reason") == DIALOG_CHOICE_NOT_FOUND

    @patch("app.services.missions.uol_action_handlers._validate_dialog_dismissed", return_value=False)
    @patch("app.services.missions.uol_action_handlers._click_dialog_button", return_value=(True, "No guardar"))
    @patch("app.services.missions.uol_action_handlers._ensure_dialog_foreground", return_value=True)
    @patch("app.services.missions.uol_action_handlers._scan_visible_windows_for_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._dialog_surface_ready", return_value=(True, ""))
    def test_dismiss_not_confirmed(self, _ready, _scan, _fg, _click, _val):
        res = uol_confirm_dialog(_step(choice="accept"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("dialog_failure_reason") == DIALOG_DISMISS_NOT_CONFIRMED

    @patch("app.services.missions.uol_action_handlers._ensure_dialog_foreground", return_value=False)
    @patch("app.services.missions.uol_action_handlers._detect_modal_or_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._scan_visible_windows_for_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._dialog_surface_ready", return_value=(True, ""))
    def test_dialog_not_found_when_cannot_focus(self, _ready, _scan, _detect, _fg):
        res = uol_confirm_dialog(_step(choice="accept"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("dialog_failure_reason") == DIALOG_NOT_FOUND


class TestConfirmDialogNoForbiddenStrategies:
    def test_strategy_not_coords_or_smart_route(self):
        assert not is_forbidden_primary_strategy("confirm_dialog:uol_dialog")
        assert not is_forbidden_primary_strategy("confirm_dialog:dismiss_positive")


class TestOrceConfirmDialogPath:
    def test_orce_path_compatible_on_success(self):
        exp = build_runtime_expectation_for_machine_step(
            MachineExecutionStep(step_id="p02", semantic_type="confirm_dialog"),
        )
        obs = observation_from_uol_record(
            {
                "step_id": "p02",
                "handler_used": "confirm_dialog:uol_dialog",
                "validation_passed": True,
                "runtime_resolution_substeps": list(CONFIRM_DIALOG_RESOLUTION_SUBSTEPS),
            },
        )
        reconciled = reconcile_step(exp, obs)
        assert confirm_dialog_substeps_valid(
            list(CONFIRM_DIALOG_RESOLUTION_SUBSTEPS),
            validation_passed=True,
        )
        assert not reconciled.divergence_detected


class TestFase7MissionHasStrategy:
    @patch("app.services.missions.uol_action_handlers.run_uol_strategy")
    def test_confirmation_dialog_confirm_step_gets_uol_strategy(self, mock_run):
        mock_run.return_value = MagicMock(
            ok=True,
            strategy="confirm_dialog:uol_dialog",
            message="ok",
            duration_ms=1,
            extra={
                "validation_status": "passed",
                "runtime_resolution_substeps": list(CONFIRM_DIALOG_RESOLUTION_SUBSTEPS),
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
        confirm = next(s for s in steps if s.kind == "confirm_dialog")
        enriched = enrich_mission_step_with_uol(confirm, mission)
        assert enriched.params.get("preferred_strategy") == "confirm_dialog:uol_dialog"
        hook = try_uol_first_runtime(enriched, StateSnapshot(), mission)
        assert hook.attempted
        assert hook.strategy_used in (
            "confirm_dialog:uol_dialog",
            "confirm_dialog:dismiss_positive",
            "submit_input:uol_submit",
        )
        assert hook.strategy_used != ""
