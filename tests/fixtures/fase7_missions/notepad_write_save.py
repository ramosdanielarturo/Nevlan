"""FASE 7 — Windows Search → Notepad → escribir → guardar."""

from __future__ import annotations

from app.contracts.mission import Mission

from tests.fixtures.fase7_missions._builder import _plan_step, build_fase7_semantic_mission


def build_notepad_write_save_mission(*, name: str = "fase7-notepad-write-save") -> Mission:
    mission = Mission(name=name)
    mid = str(getattr(mission, "id", "") or "fase7-notepad")
    return build_fase7_semantic_mission(
        name=name,
        scenario_id="notepad_write_save",
        category="launcher·editor",
        description="Windows Search → Notepad → escribir → guardar",
        steps=[
            _plan_step(
                mid,
                0,
                "open_app",
                params={"name": "notepad", "method": "windows_search"},
                preferred_strategy="open_app:windows_search",
                human_label="Abrir Notepad",
            ),
            _plan_step(
                mid,
                1,
                "fill_form",
                params={"text": "FASE 7 benchmark line", "target": "editor"},
                preferred_strategy="fill_form:semantic_focus_chain",
                human_label="Escribir texto",
            ),
            _plan_step(
                mid,
                2,
                "submit_form",
                params={"action": "save", "path": "fase7_benchmark.txt"},
                preferred_strategy="submit_form:confirm",
                human_label="Guardar archivo",
            ),
        ],
    )


__all__ = ["build_notepad_write_save_mission"]
