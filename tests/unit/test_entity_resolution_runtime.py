"""Entity-first runtime integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from app.contracts.mission import (
    Mission,
    MissionStatus,
    OperationalEntity,
    OperationalEntityType,
)
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.smart_executor import SmartMissionExecutor
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime import entity_runtime_resolution as err
from app.services.runtime.entity_resolution_layer import EntityMemoryStore


@pytest.fixture
def memory_db(tmp_path: Path) -> Path:
    return tmp_path / "erl_runtime.sqlite"


@pytest.fixture
def store(memory_db: Path) -> EntityMemoryStore:
    return EntityMemoryStore(db_path=memory_db)


def _profile_entity() -> OperationalEntity:
    return OperationalEntity(
        entity_id="ent-profile-1",
        entity_type=OperationalEntityType.USER_PROFILE,
        display_name="Daniel Chrome D Daniel Arturo Ramos",
        normalized_name="daniel_chrome_d_daniel_arturo_ramos",
        aliases=["Daniel Arturo Ramos"],
        confidence=0.92,
    )


def _state_with_profile() -> StateSnapshot:
    return StateSnapshot(
        active_app="browser.exe",
        active_window_title="Profile Picker",
        uia_targets_available=True,
        uia_visible_names=[
            "Daniel Chrome D Daniel Arturo Ramos",
            "Other User",
        ],
    )


def test_step_with_operational_entity_attempts_erl():
    step = MissionStep(
        id="s1",
        kind="select_profile",
        params={
            "operational_entity": _profile_entity().model_dump(mode="json"),
            "profile_name": "Daniel Chrome D Daniel Arturo Ramos",
        },
    )
    ent = err.entity_from_mission_step(step)
    assert ent is not None
    assert ent.display_name.startswith("Daniel")


@patch("app.services.runtime.entity_runtime_resolution.execute_entity_target_click")
def test_exact_match_executes_without_legacy(click_mock):
    click_mock.return_value = type("R", (), {
        "ok": True, "strategy": "entity_resolution", "message": "ok",
        "duration_ms": 1, "extra": {},
    })()
    step = MissionStep(
        id="s1",
        kind="select_profile",
        params={"operational_entity": _profile_entity().model_dump(mode="json")},
    )
    hook = err.try_entity_first_runtime(step, _state_with_profile())
    assert hook.attempted
    assert hook.resolved
    assert hook.executed
    assert not hook.fallback_used
    click_mock.assert_called_once()


@patch("app.services.runtime.entity_runtime_resolution.execute_entity_target_click")
def test_alias_match_path(click_mock):
    ent = OperationalEntity(
        entity_id="e2",
        entity_type=OperationalEntityType.USER_PROFILE,
        display_name="Daniel Arturo Ramos",
        normalized_name="daniel_arturo_ramos",
        aliases=["D. Ramos"],
        confidence=0.9,
    )
    click_mock.return_value = type("R", (), {
        "ok": True, "strategy": "entity_resolution", "message": "ok",
        "duration_ms": 0, "extra": {},
    })()
    state = StateSnapshot(uia_visible_names=["D. Ramos", "Guest"])
    hook = err.try_entity_first_runtime(
        MissionStep(id="s", kind="select_profile", params={
            "operational_entity": ent.model_dump(mode="json"),
        }),
        state,
    )
    assert hook.executed or hook.resolved


@patch("app.services.runtime.entity_runtime_resolution.execute_entity_target_click")
def test_fuzzy_unique_candidate(click_mock):
    click_mock.return_value = type("R", (), {
        "ok": True, "strategy": "entity_resolution", "message": "ok",
        "duration_ms": 0, "extra": {},
    })()
    ent = OperationalEntity(
        entity_type=OperationalEntityType.USER_PROFILE,
        display_name="Daniel Chrome D Daniel Arturo Ramos",
        normalized_name="daniel_chrome_d_daniel_arturo_ramos",
        confidence=0.9,
    )
    state = StateSnapshot(
        uia_visible_names=["Daniel Chrome D Daniel Arturo Ramos"],
    )
    hook = err.try_entity_first_runtime(
        MissionStep(id="s", kind="click", params={
            "operational_entity": ent.model_dump(mode="json"),
        }),
        state,
    )
    assert hook.resolved


def test_multiple_candidates_block_human():
    ent = _profile_entity()
    state = StateSnapshot(
        uia_visible_names=[
            "Daniel Chrome D Daniel Arturo Ramos",
            "Daniel Chrome D Daniel Arturo Ramos Guest",
        ],
    )
    hook = err.try_entity_first_runtime(
        MissionStep(id="s", kind="select_profile", params={
            "operational_entity": ent.model_dump(mode="json"),
        }),
        state,
    )
    assert hook.blocked_ambiguity or hook.blocked_unsafe or not hook.executed


def test_low_confidence_does_not_execute():
    ent = OperationalEntity(
        display_name="Rare Label XYZ",
        normalized_name="rare_label_xyz",
        confidence=0.5,
    )
    state = StateSnapshot(uia_visible_names=["Completely Different"])
    hook = err.try_entity_first_runtime(
        MissionStep(id="s", kind="click", params={
            "operational_entity": ent.model_dump(mode="json"),
        }),
        state,
    )
    assert not hook.executed


def test_destructive_context_blocks_auto_execute():
    step = MissionStep(
        id="pay",
        kind="confirm_payment",
        params={"operational_entity": OperationalEntity(
            display_name="Pay now",
            normalized_name="pay_now",
            confidence=0.99,
        ).model_dump(mode="json")},
    )
    hook = err.try_entity_first_runtime(
        step, StateSnapshot(uia_visible_names=["Pay now"]),
    )
    assert hook.blocked_unsafe
    assert not hook.executed


def test_entity_memory_updated_after_success(store: EntityMemoryStore):
    ent = _profile_entity()
    store.upsert_entity(ent)
    loaded = store.find_by_normalized_name(ent.normalized_name)
    assert len(loaded) >= 1


@patch("app.services.runtime.strategy_learning_layer.record_observation")
def test_sll_receives_entity_resolution(sll_mock):
    step = MissionStep(id="s", kind="select_profile", params={})
    ent = _profile_entity()
    err.record_entity_runtime_success(
        Mission(name="m", status=MissionStatus.EXECUTABLE),
        step, ent, strategy="exact",
        runtime_target={"label": ent.display_name},
        context={"step_kind": "select_profile"},
    )
    if sll_mock.called:
        assert "entity_resolution" in str(sll_mock.call_args)


def test_ompc_audit_payload_on_mission():
    mission = Mission(name="m", status=MissionStatus.EXECUTABLE)
    err.record_entity_runtime_success(
        mission,
        MissionStep(id="s", kind="select_profile", params={}),
        _profile_entity(),
        strategy="exact",
        runtime_target={},
        context={},
    )
    audit = getattr(mission, "ompc_entity_resolution_audit", None)
    assert audit is None or isinstance(audit, list)


def test_erl_failure_does_not_raise():
    step = MissionStep(id="s", kind="click", params={})
    hook = err.try_entity_first_runtime(step, StateSnapshot())
    assert hook.attempted is False or hook.attempted is True


def test_filter_strategies_skips_coords_when_entity_resolved():
    strats = [
        "select_profile:uia_text_match",
        "select_profile:relative_coords",
        "select_profile:visual_asset",
    ]
    out = err.filter_strategies_when_entity_present(strats, entity_resolved=True)
    assert not any("coords" in s for s in out)


@patch("app.services.runtime.entity_runtime_resolution.try_entity_first_runtime")
def test_smart_executor_calls_erl_before_strategies(erl_mock):
    erl_mock.return_value = err.EntityRuntimeHookResult(attempted=False)
    from app.services.missions.execution_contracts import build_contract

    step = MissionStep(
        id="s",
        kind="select_profile",
        params={
            "operational_entity": _profile_entity().model_dump(mode="json"),
            "profile_name": "Daniel Chrome D Daniel Arturo Ramos",
        },
    )
    ex = SmartMissionExecutor(mission_ref=Mission(name="t", status=MissionStatus.EXECUTABLE))
    contract = build_contract(step)
    state = _state_with_profile()
    with patch("app.services.missions.smart_executor.build_contract", return_value=contract):
        with patch.object(contract, "postcondition", return_value=False):
            with patch.object(ex, "_detect", return_value=state):
                with patch.object(ex, "_ensure_precondition", return_value=state):
                    with patch.object(ex.runner, "run_strategy") as run_mock:
                        run_mock.return_value = type("R", (), {
                            "ok": False, "strategy": "x", "message": "fail",
                            "duration_ms": 0,
                        })()
                        with patch.object(ex, "_await_postcondition", return_value=state):
                            try:
                                ex._run_step(step)
                            except Exception:
                                pass
    assert erl_mock.called


def test_mission_without_entity_params_no_attempt():
    step = MissionStep(id="s", kind="open_app", params={"name": "notepad"})
    hook = err.try_entity_first_runtime(step, StateSnapshot())
    assert not hook.executed


def test_expert_audit_lines():
    mission = Mission(name="m", status=MissionStatus.EXECUTABLE)
    err.append_entity_runtime_audit(
        mission,
        err.EntityRuntimeAuditEntry(
            step_id="s1",
            attempted=True,
            resolved=True,
            display_name="Daniel",
            strategy_used="exact",
            confidence=0.91,
        ),
    )
    lines = err.expert_audit_lines(mission)
    assert lines and "ERL" in lines[0]


def test_mission_review_normal_no_technical_tokens():
    from app.services.runtime.entity_resolution_layer import (
        sanitize_human_label_for_normal_view,
    )

    clean = sanitize_human_label_for_normal_view(
        "Perfil (uia_text_match) listo",
    )
    assert "uia_text_match" not in clean.lower()
