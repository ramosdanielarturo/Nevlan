"""FASE 7 — Formulario web simple (3 campos + submit)."""

from __future__ import annotations

from pathlib import Path

from app.contracts.mission import Mission

from tests.fixtures.fase7_missions._builder import _plan_step, build_fase7_semantic_mission


def fase7_simple_form_url() -> str:
    """URL local ``file://`` con formulario name/email/message para benchmark live."""
    html = Path(__file__).resolve().parent / "assets" / "simple_form.html"
    return html.as_uri()


def build_web_form_simple_mission(*, name: str = "fase7-web-form-simple") -> Mission:
    mission = Mission(name=name)
    mid = str(getattr(mission, "id", "") or "fase7-form")
    form_url = fase7_simple_form_url()
    return build_fase7_semantic_mission(
        name=name,
        scenario_id="web_form_simple",
        category="browser·form",
        description="Formulario web simple (3 campos + submit)",
        steps=[
            _plan_step(
                mid,
                0,
                "open_url",
                params={"url": form_url, "site": "simple_form"},
                preferred_strategy="open_url:hotkey_ctrl_l",
                human_label="Abrir formulario",
            ),
            _plan_step(
                mid,
                1,
                "fill_form",
                params={"field": "name", "value": "Arthur"},
                preferred_strategy="fill_form:semantic_focus_chain",
                human_label="Campo nombre",
            ),
            _plan_step(
                mid,
                2,
                "fill_form",
                params={"field": "email", "value": "bench@example.com"},
                preferred_strategy="fill_form:semantic_focus_chain",
                human_label="Campo email",
            ),
            _plan_step(
                mid,
                3,
                "fill_form",
                params={"field": "message", "value": "FASE 7"},
                preferred_strategy="fill_form:semantic_focus_chain",
                human_label="Campo mensaje",
            ),
            _plan_step(
                mid,
                4,
                "submit_form",
                params={"method": "primary_button"},
                preferred_strategy="submit_form:confirm",
                human_label="Enviar formulario",
            ),
        ],
    )


__all__ = ["build_web_form_simple_mission", "fase7_simple_form_url"]
