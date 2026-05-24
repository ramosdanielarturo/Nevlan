"""Contrato mínimo para ``open_site`` (SIPE promueve ``open_url`` → ``open_site``)."""

from __future__ import annotations

from app.services.missions.action_runner import supported_strategies
from app.services.missions.execution_contracts import MissionStep, build_contract
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    semantic_plan_to_mission_steps,
)
from app.services.runtime.operational_runtime_consistency_engine import (
    expected_uol_path_for_semantic_type,
    observation_from_uol_record,
    reconcile_step,
    build_runtime_expectation_for_machine_step,
)
from app.contracts.mission import MachineExecutionStep


def test_open_site_build_contract_not_unknown_kind():
    """Evita UNSUPPORTED_SEMANTIC_STEP_TYPE tras promoción SIPE open_url→open_site."""
    step = MissionStep(
        id="92f621eb:p02:open_url",
        kind="open_site",
        params={
            "site": "youtube",
            "url": "https://www.youtube.com",
            "preferred_strategy": "open_site:smart_route",
        },
        human_label="Abrir Youtube",
    )
    contract = build_contract(step)
    assert not getattr(contract, "is_unknown_kind", False)
    assert contract.preferred_strategy == "open_site:smart_route"
    assert "open_site:smart_route" in supported_strategies()


def test_semantic_plan_open_site_maps_to_known_contract():
    plan = SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="p02",
                type="open_site",
                params={"site": "youtube", "url": "https://www.youtube.com"},
                preferred_strategy="open_site:smart_route",
                human_label="Abrir Youtube",
            ),
        ],
        source="test",
    )
    steps = semantic_plan_to_mission_steps(plan)
    assert len(steps) == 1
    assert steps[0].kind == "open_site"
    contract = build_contract(steps[0])
    assert not getattr(contract, "is_unknown_kind", False)


def test_orce_open_new_tab_uol_hotkey_aligned():
    """UOL ``open_tab:uol_hotkey`` debe alinearse con ruta esperada open_new_tab."""
    assert expected_uol_path_for_semantic_type("open_new_tab") == ["open_tab"]
    exp = build_runtime_expectation_for_machine_step(
        MachineExecutionStep(step_id="p01", semantic_type="open_new_tab"),
    )
    obs = observation_from_uol_record(
        {
            "step_id": "p01",
            "handler_used": "open_tab:uol_hotkey",
            "strategies_attempted": ["open_tab:uol_hotkey"],
            "validation_passed": True,
        },
    )
    reconciled = reconcile_step(exp, obs)
    assert not reconciled.divergence_detected
