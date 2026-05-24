"""
Nevlan — Tests reales de mission_1778307670 (fix PRD 2026-05-09)
==================================================================

Misión real reportada por el usuario:

    id: 815bc407-795e-4ac9-a71c-fc778e28d70c
    name: mission_1778307670

Bug observado:

    El usuario confirmó "Daniel" desde Mission Review.
    El sistema persistió en
    ``semantic_execution_plan.steps[p01].params.profile_name = "Daniel"``,
    cuando el canonical correcto es "Daniel Arturo Ramos".

    El alias_store quedó contaminado con
    ``"daniel": "Daniel"``, degradando el canónico para futuras
    misiones.

Estos tests fijan las garantías:

  1. Cargar la misión persistida no debe afirmar nada sobre el
     campo profile_name (puede traer "Daniel" del estado pre-fix).
  2. Tras llamar ``confirm_profile(mission, profile_name="Daniel")``
     con un alias_store que ya conoce "Daniel Arturo Ramos", el
     plan debe quedar con
     ``params.profile_name == "Daniel Arturo Ramos"``, NUNCA con
     ``"Daniel"``.
  3. El audit ``review_confirmations`` debe registrar:
        original_value == "Daniel"
        value          == "Daniel Arturo Ramos"
        source         in {"user_confirmation_alias_promoted",
                            "alias_store"}
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.contracts.mission import Mission
from app.services.missions import profile_resolver
from app.services.missions.mission_review_confirm import (
    SOURCE_ALIAS_STORE,
    SOURCE_USER_ALIAS_PROMOTED,
    confirm_profile,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MISSION_PATH = (
    REPO_ROOT / "var" / "missions"
    / "815bc407-795e-4ac9-a71c-fc778e28d70c.json"
)


@pytest.fixture(autouse=True)
def _isolate_alias_store(tmp_path, monkeypatch):
    fake = tmp_path / "profile_aliases.json"
    monkeypatch.setattr(
        profile_resolver, "_alias_store_path", lambda: fake,
    )
    profile_resolver.reset_alias_store()
    yield
    profile_resolver.reset_alias_store()


def _load_mission_fresh() -> Mission:
    """Carga la misión del disco SIN tocar campos forenses.

    Limpiamos solo ``review_confirmations`` y reseteamos el step
    ``select_profile`` a su estado pre-confirmación, para simular
    el momento exacto en que el usuario va a confirmar "Daniel".
    """
    if not MISSION_PATH.exists():
        pytest.skip(
            f"Mission fixture no disponible: {MISSION_PATH}"
        )
    raw = json.loads(MISSION_PATH.read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    # Limpiamos los campos que cambiaron por confirmaciones previas
    # para reproducir el momento "antes de confirmar".
    raw["review_confirmations"] = []
    raw["tags"] = [
        t for t in (raw.get("tags") or [])
        if not str(t).startswith("review_confirmed:profile:")
    ]
    plan = raw.get("semantic_execution_plan") or {}
    for step in plan.get("steps") or []:
        if step.get("type") == "select_profile":
            params = step.setdefault("params", {})
            params["profile_name"] = ""
            params.pop("profile_confirmed_via", None)
            step["needs_user_label"] = True
            step["label_prompt"] = "¿Qué perfil debo usar?"
            step["human_label"] = "Seleccionar perfil"
    return Mission.model_validate(raw)


def _select_profile_step(mission: Mission) -> dict:
    plan = mission.semantic_execution_plan or {}
    for step in plan.get("steps") or []:
        if step.get("type") == "select_profile":
            return step
    raise AssertionError("La misión real no tiene step select_profile")


# ──────────────────────────────────────────────────────────────────
# Tests del bug exacto
# ──────────────────────────────────────────────────────────────────


def test_real_mission_1778307670_profile_not_daniel_only():
    """Tras canonicalizar con el alias_store conociendo el canónico
    completo, el plan NO debe quedar con profile_name == "Daniel".
    """
    profile_resolver.learn_profile_alias("Daniel Arturo Ramos")
    mission = _load_mission_fresh()

    confirm_profile(mission, profile_name="Daniel")

    step = _select_profile_step(mission)
    profile_name = step["params"].get("profile_name")
    assert profile_name == "Daniel Arturo Ramos", profile_name
    assert profile_name != "Daniel"


def test_real_mission_1778307670_confirm_profile_daniel_becomes_daniel_arturo_ramos():
    """Auditoría completa: el plan, los review_confirmations y el
    alias store quedan en el estado correcto tras confirmar "Daniel".
    """
    profile_resolver.learn_profile_alias("Daniel Arturo Ramos")
    mission = _load_mission_fresh()

    confirm_profile(mission, profile_name="Daniel")

    # 1) plan
    step = _select_profile_step(mission)
    assert step["params"]["profile_name"] == "Daniel Arturo Ramos"

    # 2) review_confirmations
    confs = [
        c for c in mission.review_confirmations
        if c.field == "profile_name"
    ]
    assert len(confs) == 1, confs
    c = confs[0]
    assert c.value == "Daniel Arturo Ramos"
    assert c.original_value == "Daniel"
    assert c.source in (SOURCE_USER_ALIAS_PROMOTED, SOURCE_ALIAS_STORE)

    # 3) alias store: NO debe haber quedado degradado a "Daniel".
    assert (
        profile_resolver.lookup_profile_by_alias("Daniel")
        == "Daniel Arturo Ramos"
    )
    assert (
        profile_resolver.lookup_profile_by_alias("daniel")
        == "Daniel Arturo Ramos"
    )
