"""p04 search_site/search_content must not route through ERL / ENTITY_NOT_FOUND."""
from __future__ import annotations

from unittest.mock import patch

from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.missions import uol_action_handlers as uol_h
from app.services.runtime.entity_runtime_resolution import (
    ENTITY_NOT_FOUND,
    SEARCH_RESULTS_NOT_CONFIRMED,
    is_surface_operation_step,
    sanitize_non_entity_step_failure_reason,
    sanitize_surface_operation_failure_reason,
    try_entity_first_runtime,
)
from app.services.runtime.operational_runtime_consistency_engine import (
    _normalize_strategy_to_path_token,
    expected_uol_path_for_semantic_type,
)
from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)
from app.services.missions.semantic_execution_plan import SemanticExecutionPlan
from app.services.missions.universal_operational_language import enrich_mission_step_with_uol


def _p04_step(**params) -> MissionStep:
    base = {
        "site": "youtube",
        "query": "Devuélveme el amor de Luis Miguel",
        "submit": True,
        "uol_action": "search_content",
        "preferred_strategy": "search_content:uol_surface",
    }
    base.update(params)
    return MissionStep(
        id="3541b37a-e18c-4ce7-83df-cc7bc3e6e324:p04:search_content",
        kind="search_site",
        params=base,
        human_label='Buscar "Devuélveme el amor de Luis Miguel" en Youtube',
    )


def _state(**kwargs) -> StateSnapshot:
    base = {
        "running_processes": ["chrome.exe"],
        "browser_url": "https://www.youtube.com/",
        "active_window_title": "YouTube",
    }
    base.update(kwargs)
    return StateSnapshot(**base)


def test_golden_p04_is_search_site_not_entity():
    m = build_golden_search_chrome_youtube_mission()
    sep = SemanticExecutionPlan.from_dict(m.semantic_execution_plan or {})
    sp = sep.steps[4]
    assert sp.type == "search_site"
    assert "search_content" in (sp.preferred_strategy or "")
    ms = enrich_mission_step_with_uol(
        MissionStep(
            id=sp.id,
            kind=sp.type,
            params=dict(sp.params),
            human_label=sp.human_label,
        ),
        m,
    )
    assert ms.params.get("uol_action") == "search_content"
    assert expected_uol_path_for_semantic_type(sp.type) == [
        "focus_input",
        "inject_text",
        "submit_input",
    ]


def test_search_site_is_surface_operation_step():
    step = _p04_step()
    assert is_surface_operation_step(step) is True


def test_try_entity_first_runtime_skips_search_site():
    step = _p04_step()
    hook = try_entity_first_runtime(step, _state())
    assert hook.attempted is False
    assert hook.failure_reason == ""


def test_sanitize_maps_entity_not_found_for_search_site():
    step = _p04_step()
    assert (
        sanitize_surface_operation_failure_reason(step, ENTITY_NOT_FOUND)
        == SEARCH_RESULTS_NOT_CONFIRMED
    )
    assert (
        sanitize_non_entity_step_failure_reason(step, ENTITY_NOT_FOUND)
        == SEARCH_RESULTS_NOT_CONFIRMED
    )


def test_fase5_p04_never_entity_not_found_in_sanitize():
    step = _p04_step()
    for raw in (ENTITY_NOT_FOUND, "ENTITY_AMBIGUOUS", "NO_STATE_PROGRESS"):
        cleaned = sanitize_non_entity_step_failure_reason(step, raw)
        assert cleaned == SEARCH_RESULTS_NOT_CONFIRMED
        assert "ENTITY_NOT_FOUND" not in cleaned


def test_orce_maps_search_content_uol_surface_path():
    tok = _normalize_strategy_to_path_token("search_content:uol_surface")
    assert tok == "surface"


@patch(
    "app.services.missions.uol_action_handlers._validate_search_outcome_after_action",
    return_value=(True, {"reason": "RESULTS_CONFIRMED", "osue_wait_started": True}),
)
@patch("app.services.missions.uol_action_handlers.ar._ensure_browser_foreground")
@patch("app.services.missions.uol_action_handlers._get_playwright_page", return_value=None)
@patch("app.services.missions.uol_action_handlers.ar._search_youtube_dom")
@patch("app.services.missions.uol_action_handlers.ar._search_web_address_bar")
def test_uol_search_surface_success_sets_validation_status(
    mock_omnibox, mock_yt_dom, _mock_page, _mock_focus, _mock_validate,
):
    mock_yt_dom.return_value = uol_h._fail("search_youtube:dom", "no dom")
    mock_omnibox.return_value = uol_h._ok(
        "search_web:address_bar_type_enter",
        "omnibox typed",
    )
    step = _p04_step()
    state = _state()
    res = uol_h.uol_search_content_surface(step, state)
    assert res.ok is True
    assert res.extra.get("validation_status") == "passed"
    assert res.extra.get("resolution_path") == "omnibox"
    substeps = res.extra.get("runtime_resolution_substeps") or []
    assert "focus_input" in substeps
    assert "submit_input" in substeps
    assert "ENTITY" not in str(res.extra.get("search_failure_reason") or "")


def test_smart_executor_erl_gate_skips_surface_ops():
    """Gate is_surface_operation_step evita hook ERL en search_site."""
    step = _p04_step()
    assert is_surface_operation_step(step) is True
    hook = try_entity_first_runtime(step, _state())
    assert hook.attempted is False
    assert hook.failure_reason != ENTITY_NOT_FOUND
