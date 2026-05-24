"""FASE 7 — Re-layout: ventana movida/redimensionada entre runs."""

from __future__ import annotations

from app.contracts.mission import Mission

from tests.fixtures.fase7_missions._builder import _plan_step, build_fase7_semantic_mission


def build_relayout_window_mission(*, name: str = "fase7-relayout-window") -> Mission:
    mission = Mission(name=name)
    mid = str(getattr(mission, "id", "") or "fase7-relayout")
    m = build_fase7_semantic_mission(
        name=name,
        scenario_id="relayout_window",
        category="re-layout",
        description="Re-layout: ventana movida/redimensionada entre runs",
        steps=[
            _plan_step(
                mid,
                0,
                "open_app",
                params={"name": "chrome", "method": "windows_search"},
                preferred_strategy="open_app:windows_search",
                human_label="Abrir Chrome",
            ),
            _plan_step(
                mid,
                1,
                "open_new_tab",
                params={"app": "chrome"},
                preferred_strategy="open_new_tab:hotkey_ctrl_t",
                human_label="Nueva pestaña",
            ),
            _plan_step(
                mid,
                2,
                "open_url",
                params={"url": "https://example.com", "site": "example"},
                preferred_strategy="open_url:hotkey_ctrl_l",
                human_label="Abrir sitio",
            ),
            _plan_step(
                mid,
                3,
                "scroll_page",
                params={"direction": "down", "amount": 2, "context": "relayout_probe"},
                preferred_strategy="scroll_page:wheel",
                human_label="Verificar tras re-layout",
            ),
        ],
    )
    tags = list(m.tags or [])
    tags.append("fase7:relayout_between_runs")
    m.tags = tags
    return m


__all__ = ["build_relayout_window_mission"]
