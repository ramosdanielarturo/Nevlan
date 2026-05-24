"""Tests submit_form UOL handler + contrato runtime."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.execution_contracts import MissionStep, build_contract
from app.services.missions.state_detector import StateSnapshot
from app.services.missions.uol_action_handlers import (
    SAVE_DIALOG_NOT_READY,
    SUBMIT_CONTROL_NOT_FOUND,
    SUBMIT_FORM_RESOLUTION_SUBSTEPS,
    SUBMIT_OUTCOME_NOT_CONFIRMED,
    SUBMIT_SURFACE_NOT_READY,
    is_uol_strategy,
    try_uol_first_runtime,
    uol_submit_form,
)
from app.services.missions.universal_operational_language import (
    compile_semantic_step_to_uol,
    enrich_mission_step_with_uol,
)
from app.services.runtime.operational_runtime_consistency_engine import (
    build_runtime_expectation_for_machine_step,
    reconcile_step,
    observation_from_uol_record,
    submit_form_substeps_valid,
)
from app.services.runtime.nevlan_runtime_convergence import is_forbidden_primary_strategy
from app.contracts.mission import MachineExecutionStep
from tests.fixtures.fase7_missions.notepad_write_save import build_notepad_write_save_mission
from tests.fixtures.fase7_missions.web_form_simple import build_web_form_simple_mission


def _step(**params) -> MissionStep:
    return MissionStep(id="s:submit", kind="submit_form", params=params)


class TestSubmitFormUolCompilation:
    def test_submit_form_compiles_to_uol_with_preferred_strategy(self):
        action = compile_semantic_step_to_uol(
            {
                "type": "submit_form",
                "params": {"action": "save", "path": "out.txt"},
            },
        )
        assert action is not None
        assert action.uol_action.value == "submit_input"
        assert action.expected_outcome.extra.get("path") == "out.txt"
        assert action.execution_contract.preferred_runtime_strategies[0] == "submit_form:uol_submit"

    def test_method_primary_button_maps_to_submit(self):
        action = compile_semantic_step_to_uol(
            {"type": "submit_form", "params": {"method": "primary_button"}},
        )
        assert action is not None
        assert action.location_hint == "primary_button"

    def test_enrich_mission_step_sets_uol_strategy(self):
        step = enrich_mission_step_with_uol(
            MissionStep(
                id="p02",
                kind="submit_form",
                params={"action": "save", "path": "fase7_benchmark.txt"},
            ),
        )
        assert step.params.get("uol_action") == "submit_input"
        assert step.params.get("preferred_strategy") == "submit_form:uol_submit"
        assert is_uol_strategy(str(step.params.get("preferred_strategy")))

    def test_build_contract_not_unknown_kind(self):
        contract = build_contract(_step(action="save", path="x.txt"))
        assert not getattr(contract, "is_unknown_kind", False)
        assert contract.preferred_strategy == "submit_form:uol_submit"


class TestSubmitFormHandlerSuccess:
    @patch("app.services.missions.uol_action_handlers._ensure_dialog_foreground", return_value=True)
    @patch("app.services.missions.uol_action_handlers._scan_visible_windows_for_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._validate_save_outcome", return_value=True)
    @patch("app.services.missions.uol_action_handlers._fill_save_dialog_path", return_value=True)
    @patch("app.services.missions.uol_action_handlers._hotkey", return_value=True)
    @patch("app.services.missions.uol_action_handlers._submit_input_surface_ready", return_value=(True, ""))
    def test_save_with_path_success(self, _ready, _hk, _dlg, _val, _scan, _fg):
        res = uol_submit_form(
            _step(action="save", path="fase7_benchmark.txt"),
            StateSnapshot(active_window_title="fase7_benchmark.txt - Notepad"),
        )
        assert res.ok
        assert res.strategy == "submit_form:uol_submit"
        assert set(res.extra.get("runtime_resolution_substeps") or []) >= set(
            SUBMIT_FORM_RESOLUTION_SUBSTEPS,
        )

    @patch("app.services.missions.uol_action_handlers._submit_uia_button", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._submit_dom_form", return_value=(True, "button[type='submit']"))
    @patch("app.services.missions.uol_action_handlers._submit_input_surface_ready", return_value=(True, ""))
    def test_web_form_primary_button_via_dom(self, _ready, _dom, _uia):
        res = uol_submit_form(
            _step(method="primary_button"),
            StateSnapshot(running_processes=["chrome.exe"]),
        )
        assert res.ok
        assert res.extra.get("validation_status") == "passed"


class TestSubmitFormHandlerFailures:
    @patch("app.services.missions.uol_action_handlers._submit_input_surface_ready", return_value=(False, SUBMIT_SURFACE_NOT_READY))
    def test_surface_not_ready(self, _ready):
        res = uol_submit_form(_step(method="primary_button"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("submit_failure_reason") == SUBMIT_SURFACE_NOT_READY

    @patch("app.services.missions.uol_action_handlers._submit_uia_button", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._submit_dom_form", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._press", return_value=False)
    @patch("app.services.missions.uol_action_handlers._submit_input_surface_ready", return_value=(True, ""))
    def test_submit_control_not_found(self, _ready, _press, _dom, _uia):
        res = uol_submit_form(_step(method="primary_button"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("submit_failure_reason") == SUBMIT_CONTROL_NOT_FOUND

    @patch("app.services.missions.uol_action_handlers._ensure_dialog_foreground", return_value=True)
    @patch("app.services.missions.uol_action_handlers._scan_visible_windows_for_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._validate_save_outcome", return_value=False)
    @patch("app.services.missions.uol_action_handlers._fill_save_dialog_path", return_value=True)
    @patch("app.services.missions.uol_action_handlers._hotkey", return_value=True)
    @patch("app.services.missions.uol_action_handlers._submit_input_surface_ready", return_value=(True, ""))
    def test_save_outcome_not_confirmed(self, _ready, _hk, _dlg, _val, _scan, _fg):
        res = uol_submit_form(_step(action="save", path="out.txt"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("submit_failure_reason") == SUBMIT_OUTCOME_NOT_CONFIRMED

    @patch("app.services.missions.uol_action_handlers._ensure_dialog_foreground", return_value=True)
    @patch("app.services.missions.uol_action_handlers._scan_visible_windows_for_dialog", return_value=True)
    @patch("app.services.missions.uol_action_handlers._fill_save_dialog_path", return_value=False)
    @patch("app.services.missions.uol_action_handlers._hotkey", return_value=True)
    @patch("app.services.missions.uol_action_handlers._submit_input_surface_ready", return_value=(True, ""))
    def test_save_dialog_not_ready(self, _ready, _hk, _dlg, _scan, _fg):
        res = uol_submit_form(_step(action="save", path="out.txt"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("submit_failure_reason") == SAVE_DIALOG_NOT_READY


class TestSaveDialogHelpers:
    def test_uia_markers_detect_filename_field(self):
        from app.services.missions.uol_action_handlers import _uia_control_has_save_dialog_markers

        root = MagicMock()
        root.Exists.return_value = True
        root.EditControl.return_value.Exists.return_value = True
        root.ComboBoxControl.return_value.Exists.return_value = False
        root.ButtonControl.return_value.Exists.return_value = False
        assert _uia_control_has_save_dialog_markers(root)

    def test_uia_markers_false_without_controls(self):
        from app.services.missions.uol_action_handlers import _uia_control_has_save_dialog_markers

        root = MagicMock()
        root.Exists.return_value = True
        root.EditControl.return_value.Exists.return_value = False
        root.ComboBoxControl.return_value.Exists.return_value = False
        root.ButtonControl.return_value.Exists.return_value = False
        assert not _uia_control_has_save_dialog_markers(root)

    @patch("app.services.missions.uol_action_handlers._click_save_dialog_button", return_value=True)
    @patch("app.services.missions.uol_action_handlers._find_save_filename_edit")
    @patch("app.services.missions.uol_action_handlers._locate_save_dialog_uia")
    @patch("app.services.missions.uol_action_handlers._hotkey", return_value=True)
    @patch("app.services.missions.uol_action_handlers._type_text", return_value=True)
    def test_fill_save_dialog_path_success(self, _type, _hk, locate, find_edit, _btn):
        from app.services.missions.uol_action_handlers import _fill_save_dialog_path

        dlg = MagicMock()
        edit = MagicMock()
        locate.return_value = dlg
        find_edit.return_value = edit
        assert _fill_save_dialog_path("out.txt") is True
        _btn.assert_called_once_with(dlg)


class TestSubmitFormNoForbiddenStrategies:
    def test_strategy_not_coords_or_smart_route(self):
        assert not is_forbidden_primary_strategy("submit_form:uol_submit")
        assert not is_forbidden_primary_strategy("submit_input:uol_submit")


class TestOrceSubmitFormPath:
    def test_orce_path_compatible_on_success(self):
        exp = build_runtime_expectation_for_machine_step(
            MachineExecutionStep(step_id="p02", semantic_type="submit_form"),
        )
        obs = observation_from_uol_record(
            {
                "step_id": "p02",
                "handler_used": "submit_form:uol_submit",
                "validation_passed": True,
                "runtime_resolution_substeps": list(SUBMIT_FORM_RESOLUTION_SUBSTEPS),
            },
        )
        reconciled = reconcile_step(exp, obs)
        assert submit_form_substeps_valid(
            list(SUBMIT_FORM_RESOLUTION_SUBSTEPS),
            validation_passed=True,
        )
        assert not reconciled.divergence_detected


class TestFase7MissionsHaveStrategy:
    @patch("app.services.missions.uol_action_handlers.run_uol_strategy")
    def test_notepad_submit_step_gets_uol_strategy(self, mock_run):
        mock_run.return_value = MagicMock(
            ok=True,
            strategy="submit_form:uol_submit",
            message="ok",
            duration_ms=1,
            extra={
                "validation_status": "passed",
                "runtime_resolution_substeps": list(SUBMIT_FORM_RESOLUTION_SUBSTEPS),
            },
        )
        mission = build_notepad_write_save_mission()
        from app.services.missions.semantic_execution_plan import (
            semantic_plan_to_mission_steps,
            SemanticExecutionPlan,
        )

        steps = semantic_plan_to_mission_steps(
            SemanticExecutionPlan.from_dict(mission.semantic_execution_plan),
        )
        submit = next(s for s in steps if s.kind == "submit_form")
        enriched = enrich_mission_step_with_uol(submit, mission)
        assert enriched.params.get("preferred_strategy") == "submit_form:uol_submit"
        hook = try_uol_first_runtime(enriched, StateSnapshot(), mission)
        assert hook.attempted
        assert hook.strategy_used in ("submit_form:uol_submit", "submit_input:uol_submit")
        assert hook.strategy_used != ""

    @patch("app.services.missions.uol_action_handlers.run_uol_strategy")
    def test_web_form_submit_attempts_uol_handler(self, mock_run):
        mock_run.return_value = MagicMock(
            ok=False,
            strategy="submit_form:uol_submit",
            message="fail",
            duration_ms=1,
            extra={
                "submit_failure_reason": SUBMIT_CONTROL_NOT_FOUND,
                "validation_status": "failed",
            },
        )
        mission = build_web_form_simple_mission()
        from app.services.missions.semantic_execution_plan import (
            semantic_plan_to_mission_steps,
            SemanticExecutionPlan,
        )

        steps = semantic_plan_to_mission_steps(
            SemanticExecutionPlan.from_dict(mission.semantic_execution_plan),
        )
        submit = next(s for s in steps if s.kind == "submit_form")
        enriched = enrich_mission_step_with_uol(submit, mission)
        hook = try_uol_first_runtime(enriched, StateSnapshot(), mission)
        assert hook.attempted
        assert hook.strategy_used in ("submit_form:uol_submit", "submit_input:uol_submit")
        assert hook.strategy_used != ""
