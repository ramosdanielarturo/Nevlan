"""FASE 7 — Diálogo confirmación (aceptar/cancelar)."""

from __future__ import annotations

from app.contracts.mission import Mission

from tests.fixtures.fase7_missions._builder import _plan_step, build_fase7_semantic_mission


def build_confirmation_dialog_mission(*, name: str = "fase7-confirmation-dialog") -> Mission:
    mission = Mission(name=name)
    mid = str(getattr(mission, "id", "") or "fase7-dialog")
    return build_fase7_semantic_mission(
        name=name,
        scenario_id="confirmation_dialog",
        category="dialog",
        description="Diálogo confirmación (aceptar/cancelar)",
        steps=[
            _plan_step(
                mid,
                0,
                "open_app",
                params={"name": "notepad", "method": "windows_search"},
                preferred_strategy="open_app:windows_search",
                human_label="Abrir app con diálogo",
            ),
            _plan_step(
                mid,
                1,
                "switch_context",
                params={"action": "close_unsaved"},
                preferred_strategy="switch_context:focus_foreground",
                human_label="Disparar diálogo",
            ),
            _plan_step(
                mid,
                2,
                "confirm_dialog",
                params={"choice": "accept"},
                preferred_strategy="confirm_dialog:dismiss_positive",
                human_label="Aceptar diálogo",
            ),
        ],
    )


__all__ = ["build_confirmation_dialog_mission"]
