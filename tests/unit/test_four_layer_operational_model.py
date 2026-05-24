"""Tests — Four-Layer Operational Model (4LOM)."""

from __future__ import annotations

import uuid

from app.contracts.mission import Mission
from app.services.missions.four_layer_operational_model import (
    apply_goal_purpose_edit,
    apply_human_view_label_edit,
    apply_machine_step_edit,
    build_four_layer_model,
    ensure_four_layer_model,
    validate_four_layer_invariants,
)
from app.services.missions.mission_review_summary import build_mission_review_summary
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_semantic_plan_to_mission,
)


def _mission_with_plan() -> Mission:
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="s-open",
                type="open_app",
                params={"name": "fictional_app"},
                preferred_strategy="open_app:generic",
                fallback_strategies=[],
                confidence=0.9,
                human_label="Abrir aplicación ficticia",
            ),
            SemanticPlanStep(
                id="s-search",
                type="search_content",
                params={"query": "alpha beta"},
                preferred_strategy="search_content:generic",
                fallback_strategies=[],
                confidence=0.88,
                human_label="Buscar alpha beta",
            ),
        ],
        source="semantic_intent_promotion_engine",
        intent_status="READY",
    )
    m = Mission(
        id=str(uuid.uuid4()),
        name="four-layer-test",
        description="Automatizar búsqueda en app ficticia",
    )
    attach_semantic_plan_to_mission(m, plan=plan, force=True)
    return m


def test_build_four_layers_from_sep():
    m = _mission_with_plan()
    model = build_four_layer_model(m)
    assert model.goal.purpose_statement
    assert len(model.machine_execution.steps) == 2
    assert len(model.human_view.steps) == 2
    assert model.contract.intent_status == (m.semantic_execution_plan or {}).get(
        "intent_status",
    )


def test_human_view_edit_does_not_change_machine_type():
    m = _mission_with_plan()
    ensure_four_layer_model(m)
    apply_human_view_label_edit(
        m,
        "s-search",
        human_label="Buscar contenido (editado)",
        confirmed=True,
    )
    sep = m.semantic_execution_plan or {}
    step = next(s for s in sep["steps"] if s["id"] == "s-search")
    assert step["type"] == "search_content"
    assert step["params"]["query"] == "alpha beta"
    assert step["human_label"] == "Buscar contenido (editado)"
    assert (step["params"] or {}).get("human_view_overlay") is True

    model = ensure_four_layer_model(m)
    assert model.goal.purpose_statement == "Automatizar búsqueda en app ficticia"


def test_machine_edit_does_not_change_goal():
    m = _mission_with_plan()
    goal_before = ensure_four_layer_model(m).goal.purpose_statement
    apply_machine_step_edit(
        m,
        "s-search",
        params_patch={"query": "gamma delta"},
        refresh_contract=False,
    )
    step = next(s for s in (m.semantic_execution_plan or {})["steps"] if s["id"] == "s-search")
    assert step["params"]["query"] == "gamma delta"
    model = ensure_four_layer_model(m)
    assert model.goal.purpose_statement == goal_before


def test_goal_edit_does_not_change_machine_steps():
    m = _mission_with_plan()
    machine_before = [
        (ms.step_id, ms.semantic_type)
        for ms in ensure_four_layer_model(m).machine_execution.steps
    ]
    apply_goal_purpose_edit(m, "Nuevo propósito de alto nivel")
    model = ensure_four_layer_model(m)
    assert model.goal.purpose_statement == "Nuevo propósito de alto nivel"
    after = [(ms.step_id, ms.semantic_type) for ms in model.machine_execution.steps]
    assert after == machine_before


def test_mission_review_uses_human_view_layer():
    m = _mission_with_plan()
    apply_human_view_label_edit(m, "s-open", human_label="Lanzar app ficticia")
    summary = build_mission_review_summary(m, recompute=True)
    labels = [s.human_label for s in summary.steps]
    assert any("Lanzar app ficticia" in lab for lab in labels)


def test_invariants_after_sync():
    m = _mission_with_plan()
    ensure_four_layer_model(m)
    issues = validate_four_layer_invariants(m)
    assert not issues or all("DRIFT" not in i for i in issues)
