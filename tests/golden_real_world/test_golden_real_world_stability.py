"""Estabilidad golden real-world — salud y gates deterministas."""
from __future__ import annotations

import pytest

from app.services.missions.mission_truth_gate import gate_decision
from app.services.missions.mission_health_score import compute_mission_health_score
from tests.golden_real_world.builders import REAL_WORLD_SCENARIOS, build_real_world_scenario


@pytest.mark.parametrize("scenario_id", [sid for sid, _ in REAL_WORLD_SCENARIOS])
def test_each_scenario_builds_and_has_health_score(scenario_id: str):
    m = build_real_world_scenario(scenario_id)
    gate_decision(m)
    h = compute_mission_health_score(m, persist=False)
    assert "mission_health_score" in h
    assert 0.0 <= float(h["mission_health_score"]) <= 1.0


def test_launcher_browser_provider_health_stronger_than_language_stub():
    m1 = build_real_world_scenario("launcher_browser_provider_search")
    m2 = build_real_world_scenario("language_variation")
    gate_decision(m1)
    gate_decision(m2)
    h1 = compute_mission_health_score(m1, persist=False)["mission_health_score"]
    h2 = compute_mission_health_score(m2, persist=False)["mission_health_score"]
    assert h1 >= h2
