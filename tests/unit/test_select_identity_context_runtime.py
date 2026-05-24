"""Tests for universal select_identity_context runtime resolver."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime.entity_runtime_resolution import (
    ENTITY_AMBIGUOUS,
    ENTITY_CONTEXT_NOT_VERIFIABLE,
    ENTITY_NOT_FOUND,
)
from app.services.runtime.select_identity_context_runtime import (
    ENTITY_CONTEXT_ALREADY_ACTIVE,
    ENTITY_SELECTOR_NOT_VISIBLE,
    STRATEGY_ALREADY_SATISFIED,
    detect_identity_selector_visible,
    is_identity_selection_step,
    map_entity_failure_for_identity_context,
    resolve_select_identity_context,
    skip_if_active_identity_context,
    verify_active_identity_context,
)


def _step(**params) -> MissionStep:
    return MissionStep(
        id="s1",
        kind="select_profile",
        params=dict(params),
    )


def _state(**kwargs) -> StateSnapshot:
    base = {
        "running_processes": [],
        "uia_visible_names": [],
    }
    base.update(kwargs)
    return StateSnapshot(**base)


# 1. contexto correcto ya activo → skip/success
def test_skip_when_identity_context_already_active():
    step = _step(profile_name="Alice Example", app="browser")
    state = _state(
        running_processes=["browser.exe"],
        browser_url="https://example.com/home",
        browser_title="Alice Example — Browser",
    )
    result = skip_if_active_identity_context(step, state)
    assert result is not None
    assert result.skipped is True
    assert result.strategy == STRATEGY_ALREADY_SATISFIED
    assert result.failure_reason == ENTITY_CONTEXT_ALREADY_ACTIVE
    sr = result.to_strategy_result()
    assert sr.ok is True
    assert sr.strategy == STRATEGY_ALREADY_SATISFIED


# 2. selector visible + entidad única → delegate (no skip, no human yet)
def test_selector_visible_unique_entity_delegates_to_uces_path():
    step = _step(profile_name="Bob Smith", app="browser")
    state = _state(
        active_window_title="Choose your profile",
        uia_targets_available=True,
        uia_visible_names=["Bob Smith", "Add profile"],
    )
    visible, signal = detect_identity_selector_visible(state, step)
    assert visible is True
    assert signal == "window_title_selector"
    resolved = resolve_select_identity_context(step, state)
    assert resolved.needs_human is False
    assert resolved.selector_visible is True
    assert resolved.resolution_path == "delegate_uces_erl_uia"


# 3. selector no visible + contexto no verificable → needs_human exact reason
def test_not_verifiable_when_selector_hidden_and_no_session_signals():
    step = _step(profile_name="Carol Jones", app="browser")
    state = _state(
        active_window_title="IDE — project",
        running_processes=["ide.exe"],
    )
    resolved = resolve_select_identity_context(step, state)
    assert resolved.needs_human is True
    assert resolved.failure_reason == ENTITY_SELECTOR_NOT_VISIBLE
    assert resolved.selector_visible is False


# 4. múltiples identidades visibles → ENTITY_AMBIGUOUS
def test_multiple_visible_identities_are_ambiguous():
    step = _step(profile_name="Dave Lee", app="browser")
    state = _state(
        active_window_title="Select an account",
        uia_targets_available=True,
        uia_visible_names=["Person Alpha", "Person Beta", "Person Gamma"],
    )
    resolved = resolve_select_identity_context(step, state)
    assert resolved.needs_human is True
    assert resolved.failure_reason == ENTITY_AMBIGUOUS


# 5. no coords primary — resolver never invokes coord strategies
def test_resolver_does_not_use_coords_primary():
    import app.services.runtime.select_identity_context_runtime as mod

    source = open(mod.__file__, encoding="utf-8").read().lower()
    assert "relative_coords" not in source
    assert "visual_asset" not in source
    assert "select_profile:relative_coords" not in source


# 6. no hardcode nombres/apps — generic step params drive matching
def test_no_hardcoded_app_or_person_names_in_module():
    import app.services.runtime.select_identity_context_runtime as mod

    source = open(mod.__file__, encoding="utf-8").read()
    assert "Daniel" not in source
    assert "Chrome" not in source
    assert "profile picker" not in source.lower()

    step = _step(profile_name="Zeta User", app="myapp")
    state = _state(
        running_processes=["myapp.exe"],
        browser_url="https://portal.example/session",
        browser_title="Zeta User | Portal",
    )
    assert skip_if_active_identity_context(step, state) is not None


# 7. Fase5 live path determinism — same inputs → same skip strategy
def test_deterministic_skip_strategy_for_identical_state():
    step = _step(profile_name="User One", app="browser")
    state = _state(
        running_processes=["browser.exe"],
        browser_url="https://site.test/",
        browser_title="User One",
    )
    a = skip_if_active_identity_context(step, state)
    b = skip_if_active_identity_context(step, state)
    assert a is not None and b is not None
    assert a.strategy == b.strategy == STRATEGY_ALREADY_SATISFIED
    assert a.resolution_path == b.resolution_path


# 8. Reporte muestra reason específica — map failure + strategy extra
def test_failure_reason_mapped_for_reports_not_generic_not_found():
    verification = verify_active_identity_context(
        _step(profile_name="X"),
        _state(active_window_title="Other App"),
    )
    mapped = map_entity_failure_for_identity_context(
        ENTITY_NOT_FOUND,
        selector_visible=False,
        verification=verification,
    )
    assert mapped == ENTITY_CONTEXT_NOT_VERIFIABLE
    assert mapped != ENTITY_NOT_FOUND

    sr = resolve_select_identity_context(
        _step(profile_name="Y", app="missing"),
        _state(running_processes=[]),
    ).to_strategy_result(wrap_strategy="select_entity_from_collection:uol_entity")
    assert sr.extra.get("entity_failure_reason") in {
        ENTITY_CONTEXT_NOT_VERIFIABLE,
        ENTITY_SELECTOR_NOT_VISIBLE,
    }


def test_is_identity_selection_step_for_collection_entity_types():
    step = MissionStep(
        id="c1",
        kind="select_entity_from_collection",
        params={
            "collection_entity": {
                "display_name": "Account A",
                "entity_type": "user_profile",
            },
        },
    )
    assert is_identity_selection_step(step) is True


def test_app_missing_yields_selector_not_visible():
    step = _step(profile_name="User Z", app="myapp")
    state = _state(running_processes=[], active_window_title="Desktop")
    resolved = resolve_select_identity_context(step, state)
    assert resolved.failure_reason == ENTITY_SELECTOR_NOT_VISIBLE


def test_skip_when_app_running_session_ready_without_picker():
    step = _step(
        profile_name="Session User",
        app="browser",
        pre_transition_confirmed=True,
        canonical_truth=True,
    )
    state = _state(
        running_processes=["browser.exe"],
        active_window_title="IDE focused",
    )
    result = skip_if_active_identity_context(step, state)
    assert result is not None
    assert result.strategy == STRATEGY_ALREADY_SATISFIED


@patch("app.services.runtime.entity_runtime_resolution.try_entity_first_runtime")
@patch("app.services.missions.uol_action_handlers.ar._select_profile_uia_text")
def test_uol_handler_skips_before_erl_when_context_active(mock_uia, mock_erl):
    from app.services.missions.uol_action_handlers import uol_select_entity_from_collection

    mock_erl.return_value = MagicMock(
        failure_reason=ENTITY_NOT_FOUND,
        human_message="missing",
    )
    step = _step(profile_name="Active User", app="browser")
    state = _state(
        running_processes=["browser.exe"],
        browser_url="https://ready.test/",
        browser_title="Active User",
    )
    res = uol_select_entity_from_collection(step, state)
    assert res.ok is True
    assert res.strategy == "select_entity_from_collection:uol_entity"
    mock_erl.assert_not_called()
    mock_uia.assert_not_called()
