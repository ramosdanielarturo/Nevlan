"""Golden «real world» — perfiles de misión para contratos de estabilidad.

No sustituyen E2E Win32: son **plantillas semánticas + golden traza** para CI."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

from app.contracts.mission import Mission, MissionStatus

from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)


def _minimal_sep(name: str, steps: List[Dict[str, Any]], **extra: Any) -> Mission:
    m = Mission(name=name, status=MissionStatus.EXECUTABLE)
    blob = {
        "source": "semantic_intent_promotion_engine",
        "intent_status": "READY",
        "ready_provenance": "user_confirmation",
        "coords_used": False,
        "steps": steps,
    }
    blob.update(extra)
    m.semantic_execution_plan = blob
    return m


def scenario_launcher_browser_provider_search() -> Mission:
    return build_golden_search_chrome_youtube_mission(
        name="golden-rw-launcher-browser-provider-search",
    )


def scenario_file_open_edit_save() -> Mission:
    return _minimal_sep(
        "golden-rw-file-open-edit-save",
        [
            {"type": "open_app", "human_label": "Abrir editor", "params": {"name": "notepad"}, "preferred_strategy": "start_menu"},
            {"type": "open_url", "human_label": "Abrir documento", "params": {"url": "file:///tmp/doc.txt"}, "preferred_strategy": "ctrl_o"},
            {"type": "search_content", "human_label": "Buscar texto", "params": {"query": "revision"}, "preferred_strategy": "ctrl_f"},
        ],
    )


def scenario_download_flow() -> Mission:
    return _minimal_sep(
        "golden-rw-download",
        [
            {"type": "open_new_tab", "human_label": "Nueva pestaña", "params": {}, "preferred_strategy": "ctrl_t"},
            {"type": "open_url", "human_label": "Descargas", "params": {"url": "https://example.invalid/download"}, "preferred_strategy": "address_bar"},
        ],
    )


def scenario_dialog_confirmation() -> Mission:
    return _minimal_sep(
        "golden-rw-dialog-confirm",
        [
            {"type": "search_content", "human_label": "Confirmar diálogo", "params": {"query": "OK"}, "preferred_strategy": "semantic"},
        ],
    )


def scenario_form_fill_submit() -> Mission:
    return _minimal_sep(
        "golden-rw-form-submit",
        [
            {"type": "open_url", "human_label": "Formulario", "params": {"url": "https://forms.example.invalid/"}, "preferred_strategy": "address_bar"},
            {"type": "search_content", "human_label": "Enviar", "params": {"query": "submit"}, "preferred_strategy": "semantic"},
        ],
    )


def scenario_search_pagination() -> Mission:
    return _minimal_sep(
        "golden-rw-search-pagination",
        [
            {"type": "search_content", "human_label": "Buscar", "params": {"query": "report"}, "preferred_strategy": "semantic"},
            {"type": "scroll_results", "human_label": "Página siguiente", "params": {"amount": 6}, "preferred_strategy": "keyboard"},
        ],
    )


def scenario_multi_tab_navigation() -> Mission:
    return _minimal_sep(
        "golden-rw-multi-tab",
        [
            {"type": "open_new_tab", "human_label": "Tab 2", "params": {}, "preferred_strategy": "ctrl_t"},
            {"type": "open_url", "human_label": "Sitio A", "params": {"url": "https://a.example.invalid"}, "preferred_strategy": "address_bar"},
            {"type": "open_new_tab", "human_label": "Tab 3", "params": {}, "preferred_strategy": "ctrl_t"},
            {"type": "open_url", "human_label": "Sitio B", "params": {"url": "https://b.example.invalid"}, "preferred_strategy": "address_bar"},
        ],
    )


def scenario_app_switch() -> Mission:
    return _minimal_sep(
        "golden-rw-app-switch",
        [
            {"type": "open_app", "human_label": "App A", "params": {"name": "app-a"}, "preferred_strategy": "launcher"},
            {"type": "open_app", "human_label": "App B", "params": {"name": "app-b"}, "preferred_strategy": "launcher"},
        ],
    )


def scenario_layout_shift() -> Mission:
    return build_golden_search_chrome_youtube_mission(name="golden-rw-layout-shift")


def scenario_language_variation() -> Mission:
    m = _minimal_sep(
        "golden-rw-language-variation",
        [
            {"type": "search_content", "human_label": "Chercher", "params": {"query": "données"}, "preferred_strategy": "semantic"},
        ],
    )
    return m


def scenario_slow_loading() -> Mission:
    m = scenario_download_flow()
    m.name = "golden-rw-slow-loading"
    return m


def scenario_popup_interruption() -> Mission:
    return _minimal_sep(
        "golden-rw-popup",
        [{"type": "scroll_page", "human_label": "Cerrar overlay", "params": {"amount": 1}, "preferred_strategy": "escape_first"}],
    )


def scenario_missing_target_recovery() -> Mission:
    return _minimal_sep(
        "golden-rw-missing-target",
        [{"type": "search_content", "human_label": "Recuperar cuadro", "params": {"query": "find box"}, "preferred_strategy": "semantic"}],
    )


def scenario_browser_restore_session() -> Mission:
    return _minimal_sep(
        "golden-rw-browser-restore",
        [
            {"type": "open_app", "human_label": "Navegador", "params": {"name": "browser"}, "preferred_strategy": "launcher"},
            {"type": "open_url", "human_label": "Restaurar sesión", "params": {"url": "about:sessionrestore"}, "preferred_strategy": "address_bar"},
        ],
    )


def scenario_table_dashboard_navigation() -> Mission:
    return _minimal_sep(
        "golden-rw-table-dashboard",
        [
            {"type": "open_url", "human_label": "Dashboard", "params": {"url": "https://dash.example.invalid"}, "preferred_strategy": "address_bar"},
            {"type": "scroll_results", "human_label": "Filas", "params": {"amount": 8}, "preferred_strategy": "keyboard"},
        ],
    )


REAL_WORLD_SCENARIOS: Tuple[Tuple[str, Callable[[], Mission]], ...] = (
    ("launcher_browser_provider_search", scenario_launcher_browser_provider_search),
    ("file_open_edit_save", scenario_file_open_edit_save),
    ("download_flow", scenario_download_flow),
    ("dialog_confirmation", scenario_dialog_confirmation),
    ("form_fill_submit", scenario_form_fill_submit),
    ("search_pagination", scenario_search_pagination),
    ("multi_tab_navigation", scenario_multi_tab_navigation),
    ("app_switch", scenario_app_switch),
    ("layout_shift", scenario_layout_shift),
    ("language_variation", scenario_language_variation),
    ("slow_loading", scenario_slow_loading),
    ("popup_interruption", scenario_popup_interruption),
    ("missing_target_recovery", scenario_missing_target_recovery),
    ("browser_restore_session", scenario_browser_restore_session),
    ("table_dashboard_navigation", scenario_table_dashboard_navigation),
)


def build_real_world_scenario(scenario_id: str) -> Mission:
    for sid, fn in REAL_WORLD_SCENARIOS:
        if sid == scenario_id:
            return fn()
    raise KeyError(scenario_id)


__all__ = [
    "REAL_WORLD_SCENARIOS",
    "build_real_world_scenario",
    "scenario_launcher_browser_provider_search",
]
