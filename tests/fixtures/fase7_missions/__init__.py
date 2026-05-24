"""Suite mínima FASE 7 — ≥5 escenarios de producto."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List

from app.contracts.mission import Mission

from tests.fixtures.fase7_missions.chrome_youtube_launcher import (
    build_chrome_youtube_launcher_mission,
)
from tests.fixtures.fase7_missions.confirmation_dialog import (
    build_confirmation_dialog_mission,
)
from tests.fixtures.fase7_missions.notepad_write_save import (
    build_notepad_write_save_mission,
)
from tests.fixtures.fase7_missions.relayout_window import build_relayout_window_mission
from tests.fixtures.fase7_missions.web_form_simple import build_web_form_simple_mission


@dataclass(frozen=True)
class Fase7ScenarioSpec:
    scenario_id: str
    category: str
    description: str
    builder: Callable[[], Mission]
    maturity_note: str = "depends_on_phases_1_6"


FASE7_DEFAULT_SUITE: List[Fase7ScenarioSpec] = [
    Fase7ScenarioSpec(
        scenario_id="chrome_youtube_launcher",
        category="launcher·browser·search",
        description="Chrome → perfil → pestaña → YouTube → búsqueda → scroll",
        builder=build_chrome_youtube_launcher_mission,
    ),
    Fase7ScenarioSpec(
        scenario_id="notepad_write_save",
        category="launcher·editor",
        description="Windows Search → Notepad → escribir → guardar",
        builder=build_notepad_write_save_mission,
    ),
    Fase7ScenarioSpec(
        scenario_id="web_form_simple",
        category="browser·form",
        description="Formulario web simple (3 campos + submit)",
        builder=build_web_form_simple_mission,
    ),
    Fase7ScenarioSpec(
        scenario_id="confirmation_dialog",
        category="dialog",
        description="Diálogo confirmación (aceptar/cancelar)",
        builder=build_confirmation_dialog_mission,
    ),
    Fase7ScenarioSpec(
        scenario_id="relayout_window",
        category="re-layout",
        description="Re-layout: ventana movida/redimensionada entre runs",
        builder=build_relayout_window_mission,
        maturity_note="live_only_relayout_between_runs",
    ),
]

FASE7_SCENARIO_BY_ID: Dict[str, Fase7ScenarioSpec] = {
    s.scenario_id: s for s in FASE7_DEFAULT_SUITE
}


def resolve_fase7_mission(scenario_or_mission: str) -> Mission:
    """Resuelve id de escenario o ruta JSON de misión."""
    from pathlib import Path
    import json
    from app.contracts.mission import mission_from_dict

    key = scenario_or_mission.strip()
    if key in FASE7_SCENARIO_BY_ID:
        return FASE7_SCENARIO_BY_ID[key].builder()

    path = Path(key)
    if path.is_file():
        return mission_from_dict(json.loads(path.read_text(encoding="utf-8")))

    raise KeyError(f"escenario o misión desconocida: {scenario_or_mission!r}")


__all__ = [
    "FASE7_DEFAULT_SUITE",
    "FASE7_SCENARIO_BY_ID",
    "Fase7ScenarioSpec",
    "resolve_fase7_mission",
]
