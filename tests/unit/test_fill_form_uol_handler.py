"""Tests fill_form UOL handler + contrato runtime."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.execution_contracts import MissionStep, build_contract
from app.services.missions.state_detector import StateSnapshot
from app.services.missions.uol_action_handlers import (
    FILL_FIELD_NOT_FOUND,
    FILL_FORM_RESOLUTION_SUBSTEPS,
    FILL_VALUE_NOT_CONFIRMED,
    INPUT_SURFACE_NOT_READY,
    is_uol_strategy,
    try_uol_first_runtime,
    uol_fill_form,
)
from app.services.missions.universal_operational_language import (
    compile_semantic_step_to_uol,
    enrich_mission_step_with_uol,
)
from app.services.runtime.operational_runtime_consistency_engine import (
    build_runtime_expectation_for_machine_step,
    fill_form_substeps_valid,
    reconcile_step,
    observation_from_uol_record,
)
from app.services.runtime.nevlan_runtime_convergence import is_forbidden_primary_strategy
from app.contracts.mission import MachineExecutionStep
from tests.fixtures.fase7_missions.notepad_write_save import build_notepad_write_save_mission
from tests.fixtures.fase7_missions.web_form_simple import build_web_form_simple_mission


def _step(**params) -> MissionStep:
    return MissionStep(id="s:fill", kind="fill_form", params=params)


class TestFillFormUolCompilation:
    def test_fill_form_compiles_to_uol_with_preferred_strategy(self):
        action = compile_semantic_step_to_uol(
            {"type": "fill_form", "params": {"text": "hello", "target": "editor"}},
        )
        assert action is not None
        assert action.uol_action.value == "fill_field"
        assert action.input_text == "hello"
        assert action.execution_contract.preferred_runtime_strategies[0] == "fill_form:uol_fields"

    def test_params_text_value_content_normalized(self):
        for key in ("text", "value", "content", "multiline_text"):
            action = compile_semantic_step_to_uol(
                {"type": "fill_form", "params": {key: "payload"}},
            )
            assert action is not None
            assert action.input_text == "payload"

    def test_enrich_mission_step_sets_uol_strategy(self):
        step = enrich_mission_step_with_uol(
            MissionStep(
                id="p01",
                kind="fill_form",
                params={"text": "line", "target": "editor"},
            ),
        )
        assert step.params.get("uol_action") == "fill_field"
        assert step.params.get("preferred_strategy") == "fill_form:uol_fields"
        assert is_uol_strategy(str(step.params.get("preferred_strategy")))

    def test_build_contract_not_unknown_kind(self):
        contract = build_contract(_step(text="x"))
        assert not getattr(contract, "is_unknown_kind", False)
        assert contract.preferred_strategy == "fill_form:uol_fields"


class TestFillFormHandlerSuccess:
    @patch("app.services.missions.uol_action_handlers._fill_uia_editable", return_value=(True, "hello"))
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_active_editor_text_success(self, _ready, _uia):
        res = uol_fill_form(_step(text="hello", target="editor"), StateSnapshot())
        assert res.ok
        assert res.strategy == "fill_form:uol_fields"
        assert res.extra.get("validation_status") == "passed"
        assert set(res.extra.get("runtime_resolution_substeps") or []) >= set(FILL_FORM_RESOLUTION_SUBSTEPS)

    @patch("app.services.missions.uol_action_handlers._fill_dom_field")
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_web_form_three_fields_via_dom(self, _ready, mock_dom):
        mock_dom.side_effect = [
            (True, "Arthur"),
            (True, "bench@example.com"),
            (True, "FASE 7"),
        ]
        step = _step(
            fields=[
                {"name": "name", "value": "Arthur"},
                {"name": "email", "value": "bench@example.com"},
                {"name": "message", "value": "FASE 7"},
            ],
        )
        res = uol_fill_form(step, StateSnapshot(running_processes=["chrome.exe"]))
        assert res.ok
        assert mock_dom.call_count == 3


class TestFillFormHandlerFailures:
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_no_payload_field_not_found(self, _ready):
        res = uol_fill_form(_step(), StateSnapshot())
        assert not res.ok
        assert res.extra.get("fill_failure_reason") == FILL_FIELD_NOT_FOUND
        assert res.strategy == "fill_form:uol_fields"

    @patch("app.services.missions.uol_action_handlers._fill_dom_field", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._fill_uia_editable", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._fill_dom_active", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._inject_text_into_focus", return_value=False)
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_field_not_found_when_no_surface(self, _ready, _inj, _dom_active, _uia, _dom):
        res = uol_fill_form(_step(text="x", field="name"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("fill_failure_reason") == FILL_FIELD_NOT_FOUND

    @patch("app.services.missions.uol_action_handlers._fill_uia_editable", return_value=(True, "wrong"))
    @patch("app.services.missions.uol_action_handlers._fill_dom_field", return_value=(False, ""))
    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(True, ""))
    def test_value_not_confirmed(self, _ready, _dom, _uia):
        st = StateSnapshot(active_window_title="other")
        res = uol_fill_form(_step(text="expected", field="name"), st)
        assert not res.ok
        assert res.extra.get("fill_failure_reason") == FILL_VALUE_NOT_CONFIRMED

    @patch("app.services.missions.uol_action_handlers._fill_input_surface_ready", return_value=(False, INPUT_SURFACE_NOT_READY))
    def test_input_surface_not_ready(self, _ready):
        res = uol_fill_form(_step(text="x"), StateSnapshot())
        assert not res.ok
        assert res.extra.get("fill_failure_reason") == INPUT_SURFACE_NOT_READY


class TestFillFormNoForbiddenStrategies:
    def test_strategy_not_coords_or_smart_route(self):
        assert not is_forbidden_primary_strategy("fill_form:uol_fields")
        assert not is_forbidden_primary_strategy("fill_field:uol_field")


class TestOrceFillFormPath:
    def test_orce_path_compatible_on_success(self):
        exp = build_runtime_expectation_for_machine_step(
            MachineExecutionStep(step_id="p01", semantic_type="fill_form"),
        )
        obs = observation_from_uol_record(
            {
                "step_id": "p01",
                "handler_used": "fill_form:uol_fields",
                "validation_passed": True,
                "runtime_resolution_substeps": list(FILL_FORM_RESOLUTION_SUBSTEPS),
            },
        )
        reconciled = reconcile_step(exp, obs)
        assert fill_form_substeps_valid(
            list(FILL_FORM_RESOLUTION_SUBSTEPS),
            validation_passed=True,
        )
        assert not reconciled.divergence_detected


class TestFase7MissionsHaveStrategy:
    @patch("app.services.missions.uol_action_handlers.run_uol_strategy")
    def test_notepad_fill_step_gets_uol_strategy(self, mock_run):
        mock_run.return_value = MagicMock(
            ok=True,
            strategy="fill_form:uol_fields",
            message="ok",
            duration_ms=1,
            extra={"validation_status": "passed", "runtime_resolution_substeps": list(FILL_FORM_RESOLUTION_SUBSTEPS)},
        )
        mission = build_notepad_write_save_mission()
        from app.services.missions.semantic_execution_plan import semantic_plan_to_mission_steps, SemanticExecutionPlan

        steps = semantic_plan_to_mission_steps(
            SemanticExecutionPlan.from_dict(mission.semantic_execution_plan),
        )
        fill = next(s for s in steps if s.kind == "fill_form")
        enriched = enrich_mission_step_with_uol(fill, mission)
        assert enriched.params.get("preferred_strategy") == "fill_form:uol_fields"
        hook = try_uol_first_runtime(enriched, StateSnapshot(), mission)
        assert hook.attempted
        assert hook.strategy_used in ("fill_form:uol_fields", "fill_field:uol_field")
        assert hook.strategy_used != ""

    @patch("app.services.missions.uol_action_handlers.run_uol_strategy")
    def test_web_form_step_attempts_uol_handler(self, mock_run):
        mock_run.return_value = MagicMock(
            ok=False,
            strategy="fill_form:uol_fields",
            message="fail",
            duration_ms=1,
            extra={"fill_failure_reason": FILL_FIELD_NOT_FOUND, "validation_status": "failed"},
        )
        mission = build_web_form_simple_mission()
        from app.services.missions.semantic_execution_plan import semantic_plan_to_mission_steps, SemanticExecutionPlan

        steps = semantic_plan_to_mission_steps(
            SemanticExecutionPlan.from_dict(mission.semantic_execution_plan),
        )
        fill = next(s for s in steps if s.kind == "fill_form")
        enriched = enrich_mission_step_with_uol(fill, mission)
        hook = try_uol_first_runtime(enriched, StateSnapshot(), mission)
        assert hook.strategy_used in ("fill_form:uol_fields", "fill_field:uol_field")
        assert hook.strategy_used != ""
