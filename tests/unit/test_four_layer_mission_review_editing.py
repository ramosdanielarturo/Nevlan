"""Tests — Mission Review editing via Four-Layer Operational Model."""

from __future__ import annotations

import uuid

import pytest

from app.contracts.mission import Mission
from app.services.missions.four_layer_operational_model import (
    ensure_four_layer_model,
    machine_steps_for_runtime,
    validate_four_layer_invariants,
)
from app.services.missions.mission_review_four_layer_editing import (
    FourLayerEditError,
    edit_step_params,
    move_step_down,
    rename_step_label,
    reorder_steps,
    save_mission_edits,
    set_step_disabled,
)
from app.services.missions.mission_review_summary import build_mission_review_summary
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_semantic_plan_to_mission,
    semantic_plan_to_mission_steps,
)


def _mission() -> Mission:
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="s-launch",
                type="open_app",
                params={"name": "fictional"},
                preferred_strategy="open_app:generic",
                fallback_strategies=[],
                confidence=0.9,
                human_label="Abrir app ficticia",
            ),
            SemanticPlanStep(
                id="s-nav",
                type="open_site",
                params={"url": "https://example.test"},
                preferred_strategy="open_site:bar",
                fallback_strategies=[],
                confidence=0.9,
                human_label="Ir al sitio",
            ),
            SemanticPlanStep(
                id="s-search",
                type="search_content",
                params={"query": "alpha"},
                preferred_strategy="search_content:generic",
                fallback_strategies=[],
                confidence=0.88,
                human_label="Buscar alpha",
            ),
            SemanticPlanStep(
                id="s-refine",
                type="fill_field",
                params={"field": "q", "value": "refine"},
                preferred_strategy="fill_field:generic",
                fallback_strategies=[],
                confidence=0.86,
                human_label="Refinar consulta",
            ),
            SemanticPlanStep(
                id="s-submit",
                type="submit_input",
                params={},
                preferred_strategy="submit_input:enter",
                fallback_strategies=[],
                confidence=0.85,
                human_label="Confirmar búsqueda",
            ),
        ],
        source="semantic_intent_promotion_engine",
        intent_status="READY",
    )
    m = Mission(
        id=str(uuid.uuid4()),
        name="edit-test",
        description="Objetivo original de prueba",
    )
    m.raw_trace = [object(), object()]
    attach_semantic_plan_to_mission(m, plan=plan, force=True)
    return m


def test_rename_does_not_change_machine():
    m = _mission()
    trace_len = len(m.raw_trace)
    model_before = ensure_four_layer_model(m)
    ms = next(x for x in model_before.machine_execution.steps if x.step_id == "s-search")
    goal_before = model_before.goal.purpose_statement
    type_before = ms.semantic_type
    query_before = ms.params.get("query")

    rename_step_label(m, "s-search", "Buscar contenido renombrado")
    save_mission_edits(m)

    model_after = ensure_four_layer_model(m)
    ms2 = next(x for x in model_after.machine_execution.steps if x.step_id == "s-search")
    assert ms2.semantic_type == type_before
    assert ms2.params.get("query") == query_before
    assert model_after.goal.purpose_statement == goal_before
    assert len(m.raw_trace) == trace_len
    hv = next(h for h in model_after.human_view.steps if h.machine_step_id == "s-search")
    assert "renombrado" in hv.human_label


def test_change_query_changes_machine_not_goal():
    m = _mission()
    goal_before = ensure_four_layer_model(m).goal.purpose_statement
    edit_step_params(m, "s-search", {"query": "beta gamma"})
    save_mission_edits(m)
    ms = next(
        x for x in ensure_four_layer_model(m).machine_execution.steps
        if x.step_id == "s-search"
    )
    assert ms.params.get("query") == "beta gamma"
    assert ensure_four_layer_model(m).goal.purpose_statement == goal_before


def test_change_url_changes_machine():
    m = _mission()
    edit_step_params(m, "s-nav", {"url": "https://other.test/path"})
    save_mission_edits(m)
    ms = next(x for x in ensure_four_layer_model(m).machine_execution.steps if x.step_id == "s-nav")
    assert ms.params.get("url") == "https://other.test/path"


def test_invalid_reorder_blocked():
    m = _mission()
    with pytest.raises(FourLayerEditError):
        reorder_steps(
            m,
            ["s-search", "s-submit", "s-launch", "s-nav"],
        )


def test_disabled_step_omitted_from_runtime():
    m = _mission()
    set_step_disabled(m, "s-search", disabled=True)
    save_mission_edits(m)
    plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan or {})
    steps = semantic_plan_to_mission_steps(plan)
    assert all(s.id != "s-search" for s in steps)


def test_raw_trace_intact_after_edits():
    m = _mission()
    n = len(m.raw_trace)
    rename_step_label(m, "s-launch", "Lanzar")
    edit_step_params(m, "s-search", {"query": "z"})
    save_mission_edits(m)
    assert len(m.raw_trace) == n


def test_invariants_after_save():
    m = _mission()
    rename_step_label(m, "s-nav", "Navegar")
    issues = save_mission_edits(m)
    assert not issues or all("DRIFT" not in i for i in issues)
    assert validate_four_layer_invariants(m) == issues or not validate_four_layer_invariants(m)


def test_mission_review_uses_human_view():
    m = _mission()
    rename_step_label(m, "s-launch", "Etiqueta human view única")
    save_mission_edits(m)
    summary = build_mission_review_summary(m, recompute=True)
    assert any("Etiqueta human view única" in s.human_label for s in summary.steps)
    assert summary.steps[0].machine_step_id == "s-launch"


def test_runtime_uses_machine_layer_order():
    m = _mission()
    machine_ids = [ms.step_id for ms in machine_steps_for_runtime(m)]
    plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan or {})
    runtime_ids = [s.id for s in semantic_plan_to_mission_steps(plan)]
    assert runtime_ids == machine_ids
    sep_ids = [s["id"] for s in (m.semantic_execution_plan or {}).get("steps") or []]
    assert sep_ids == machine_ids


def test_move_down_valid_when_same_phase():
    m = _mission()
    before = [ms.step_id for ms in ensure_four_layer_model(m).machine_execution.steps]
    assert before.index("s-search") < before.index("s-refine")
    move_step_down(m, "s-search")
    save_mission_edits(m)
    after = [ms.step_id for ms in ensure_four_layer_model(m).machine_execution.steps]
    assert after.index("s-refine") < after.index("s-search")
    assert validate_four_layer_invariants(m) == [] or not validate_four_layer_invariants(m)
