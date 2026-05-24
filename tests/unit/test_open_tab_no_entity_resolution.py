"""open_new_tab must not route through ERL / ENTITY_NOT_FOUND."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.missions import uol_action_handlers as uol_h
from app.services.runtime.entity_runtime_resolution import (
    ENTITY_NOT_FOUND,
    OPEN_TAB_NOT_CONFIRMED,
    is_context_creation_step,
    sanitize_context_creation_failure_reason,
    try_entity_first_runtime,
)
from app.services.runtime.operational_runtime_consistency_engine import (
    _normalize_strategy_to_path_token,
)


def _open_tab_step(**params) -> MissionStep:
    return MissionStep(
        id="p02",
        kind="open_new_tab",
        params={
            "app": "browser",
            "uol_action": "open_tab",
            "preferred_strategy": "open_tab:uol_hotkey",
            **params,
        },
    )


def _state(**kwargs) -> StateSnapshot:
    base = {"running_processes": ["chrome.exe"]}
    base.update(kwargs)
    return StateSnapshot(**base)


def test_open_new_tab_is_context_creation_step():
    step = _open_tab_step()
    assert is_context_creation_step(step) is True


def test_try_entity_first_runtime_skips_open_new_tab():
    step = _open_tab_step()
    state = _state()
    hook = try_entity_first_runtime(step, state)
    assert hook.attempted is False
    assert hook.failure_reason == ""


@patch("app.services.missions.state_detector.StateDetector")
@patch("app.services.missions.uol_action_handlers.ar._open_new_tab_hotkey")
def test_uol_open_tab_hotkey_success_with_context_validation(mock_hotkey, mock_det_cls):
    mock_hotkey.return_value = uol_h._ok("open_new_tab:hotkey_ctrl_t", "Ctrl+T")
    before = _state(
        active_window_title="Old Tab",
        browser_url="https://example.com/old",
    )
    after = _state(
        active_window_title="New Tab",
        browser_url="https://example.com/new",
    )
    mock_det_cls.return_value.detect.return_value = after

    res = uol_h.uol_open_tab_hotkey(_open_tab_step(), before)
    assert res.ok is True
    assert res.extra.get("validation_status") == "passed"
    assert res.extra.get("resolution_path") == "open_tab"
    assert "ENTITY" not in str(res.extra.get("open_tab_failure_reason") or "")


@patch("app.services.missions.state_detector.StateDetector")
@patch("app.services.missions.uol_action_handlers.ar._open_new_tab_uia")
@patch("app.services.missions.uol_action_handlers.ar._open_new_tab_hotkey")
def test_uol_open_tab_failure_uses_open_tab_reason_not_entity(
    mock_hotkey, mock_uia, mock_det_cls,
):
    mock_hotkey.return_value = uol_h._ok("open_new_tab:hotkey_ctrl_t", "Ctrl+T")
    mock_uia.return_value = uol_h._fail("open_new_tab:uia_button", "no button")
    before = _state(active_window_title="Same", browser_url="")
    after = _state(active_window_title="Same", browser_url="")
    mock_det_cls.return_value.detect.return_value = after

    res = uol_h.uol_open_tab_hotkey(_open_tab_step(), before)
    assert res.ok is False
    assert res.extra.get("open_tab_failure_reason") == OPEN_TAB_NOT_CONFIRMED
    assert res.extra.get("open_tab_failure_reason") != ENTITY_NOT_FOUND


def test_sanitize_maps_entity_not_found_for_open_tab():
    step = _open_tab_step()
    assert sanitize_context_creation_failure_reason(step, ENTITY_NOT_FOUND) == OPEN_TAB_NOT_CONFIRMED


def test_orce_maps_uol_hotkey_to_open_tab_path():
    tok = _normalize_strategy_to_path_token("open_tab:uol_hotkey")
    assert tok == "open_tab"


def test_fase5_path_open_new_tab_never_entity_not_found_in_sanitize():
    """Simula outcome Fase5: ERL no debe contaminar open_new_tab."""
    step = _open_tab_step()
    for raw in (ENTITY_NOT_FOUND, "ENTITY_AMBIGUOUS", "NO_STATE_PROGRESS"):
        cleaned = sanitize_context_creation_failure_reason(step, raw)
        assert cleaned == OPEN_TAB_NOT_CONFIRMED
        assert "ENTITY_NOT_FOUND" not in cleaned


def test_no_coords_or_smart_route_in_open_tab_handler_source():
    import app.services.missions.uol_action_handlers as mod

    src = open(mod.__file__, encoding="utf-8").read().lower()
    start = src.index("def uol_open_tab_hotkey")
    end = src.index("def uol_navigate_to_location_address")
    block = src[start:end]
    assert "relative_coords" not in block
    assert "smart_route" not in block


def test_smart_executor_skips_erl_via_is_context_creation_gate():
    """Gate is_context_creation_step evita hook ERL en open_new_tab."""
    step = _open_tab_step()
    assert is_context_creation_step(step) is True
    hook = try_entity_first_runtime(step, _state())
    assert hook.attempted is False
    assert hook.failure_reason != ENTITY_NOT_FOUND
