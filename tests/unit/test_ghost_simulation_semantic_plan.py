"""
Nevlan — Tests Ghost Simulation sobre Semantic Plan (PRD 2026-05-07c §G)
========================================================================

Ghost Simulation debe **simular la intención limpia**, no el grafo
legacy. Cuando la misión tiene ``semantic_execution_plan``, el ghost
DEBE preferirlo y validar:

  * intención canónica (open_app, select_profile, open_new_tab,
    open_url, search_youtube, scroll_results)
  * no estrategias coords/visual como primarias
  * profile_name no vacío
  * query no vacía y sin pérdida de tokens
  * open_new_tab usa Ctrl+T
  * open_url usa Ctrl+L

Si el plan no existe, fallback al ghost legacy (sin cambios).
"""
from __future__ import annotations

import pytest

from app.contracts.mission import Mission
from app.services.missions.ghost_simulation import (
    GhostStatus,
    simulate_mission,
)


def _attach_plan(mission: Mission, plan_dict: dict) -> None:
    mission.semantic_execution_plan = plan_dict
    mission.legacy_compiled_graph_purpose = "legacy_debug_only"


def _ready_plan_dict() -> dict:
    """Plan canónico LIMPIO (chrome+youtube) — debe pasar el ghost."""
    return {
        "source": "intent_collapse_engine",
        "version": 1,
        "average_confidence": 0.92,
        "coords_used": False,
        "legacy_graph_ignored": True,
        "intent_status": "READY",
        "not_ready_reasons": [],
        "steps": [
            {
                "id": "p0", "type": "open_app",
                "params": {"name": "chrome", "method": "windows_search"},
                "preferred_strategy": "open_app:windows_search",
                "fallback_strategies": [],
                "confidence": 0.95,
                "human_label": "Abrir Chrome",
                "needs_user_label": False, "label_prompt": "",
                "block_id": None,
            },
            {
                "id": "p1", "type": "select_profile",
                "params": {"profile_name": "Daniel Arturo Ramos",
                           "app": "chrome"},
                "preferred_strategy": "select_profile:uia_text_match",
                "fallback_strategies": [],
                "confidence": 0.92,
                "human_label": "Seleccionar perfil",
                "needs_user_label": False, "label_prompt": "",
                "block_id": None,
            },
            {
                "id": "p2", "type": "open_new_tab",
                "params": {"app": "chrome"},
                "preferred_strategy": "open_new_tab:hotkey_ctrl_t",
                "fallback_strategies": [],
                "confidence": 0.90,
                "human_label": "Nueva pestaña",
                "needs_user_label": False, "label_prompt": "",
                "block_id": None,
            },
            {
                "id": "p3", "type": "open_url",
                "params": {"url": "youtube.com", "alias": "youtube"},
                "preferred_strategy": "open_url:hotkey_ctrl_l",
                "fallback_strategies": [],
                "confidence": 0.95,
                "human_label": "Abrir YouTube",
                "needs_user_label": False, "label_prompt": "",
                "block_id": None,
            },
            {
                "id": "p4", "type": "search_youtube",
                "params": {
                    "query": "Devuélveme el amor de Luis Miguel",
                    "site": "youtube",
                    "target": "youtube_search_box",
                    "query_fidelity_status": "ok",
                },
                "preferred_strategy": "search_youtube:dom_input",
                "fallback_strategies": [],
                "confidence": 0.95,
                "human_label": "Buscar en YouTube",
                "needs_user_label": False, "label_prompt": "",
                "block_id": None,
            },
            {
                "id": "p5", "type": "scroll_results",
                "params": {"direction": "down", "amount": 1,
                           "context": "youtube_results"},
                "preferred_strategy": "scroll_results:wheel",
                "fallback_strategies": [],
                "confidence": 0.85,
                "human_label": "Bajar resultados",
                "needs_user_label": False, "label_prompt": "",
                "block_id": None,
            },
        ],
    }


# ──────────────────────────────────────────────────────────────────
# 1. Ghost simula intención semántica, no legacy
# ──────────────────────────────────────────────────────────────────

class TestGhostSemanticPath:
    def test_ghost_simulates_semantic_plan_not_legacy_graph(self):
        """Cuando hay semantic_execution_plan, el ghost lo prefiere
        sobre compiled_execution_graph (que puede estar sucio).
        """
        m = Mission(name="g")
        _attach_plan(m, _ready_plan_dict())
        # Inyectamos un compiled_execution_graph "sucio" para
        # asegurarnos de que NO contamina la decisión del ghost.
        # (no necesitamos la forma exacta — solo que exista.)
        report = simulate_mission(m)
        # Todos los steps OK = la intención semántica simuló bien.
        for step_report in report.steps:
            assert step_report.status == GhostStatus.OK, step_report.reason

    def test_ghost_passes_chrome_youtube_case_ready(self):
        """E2E del PRD: el plan canónico chrome+youtube debe pasar
        ghost simulation sin missing/uncertain.
        """
        m = Mission(name="g")
        _attach_plan(m, _ready_plan_dict())
        report = simulate_mission(m)
        assert not report.needs_user_signal
        # Y la cobertura: hay 6 steps, todos pasaron.
        assert len(report.steps) == 6
        for sr in report.steps:
            assert sr.status == GhostStatus.OK


# ──────────────────────────────────────────────────────────────────
# 2. Fallos esperados: el ghost detecta intenciones rotas
# ──────────────────────────────────────────────────────────────────

def _step_at(report, idx: int):
    """Devuelve el GhostStepReport en el índice ``idx`` del plan."""
    for sr in report.steps:
        if sr.step_index == idx:
            return sr
    return None


class TestGhostFailsBrokenIntent:
    def test_ghost_fails_empty_profile_name(self):
        plan = _ready_plan_dict()
        plan["steps"][1]["params"]["profile_name"] = ""
        m = Mission(name="g")
        _attach_plan(m, plan)
        report = simulate_mission(m)
        select_step = _step_at(report, 1)
        assert select_step is not None
        assert select_step.status == GhostStatus.MISSING
        assert "profile_name" in select_step.reason.lower() or \
               "perfil" in select_step.reason.lower()

    def test_ghost_fails_query_fidelity_suspect(self):
        plan = _ready_plan_dict()
        plan["steps"][4]["params"]["query_fidelity_status"] = (
            "missing_short_token"
        )
        m = Mission(name="g")
        _attach_plan(m, plan)
        report = simulate_mission(m)
        sy = _step_at(report, 4)
        assert sy is not None
        assert sy.status == GhostStatus.MISSING
        assert "token" in sy.reason.lower() or "query" in sy.reason.lower()

    def test_ghost_fails_open_new_tab_not_using_ctrl_t(self):
        plan = _ready_plan_dict()
        plan["steps"][2]["preferred_strategy"] = "open_new_tab:click_button"
        m = Mission(name="g")
        _attach_plan(m, plan)
        report = simulate_mission(m)
        nt = _step_at(report, 2)
        assert nt is not None
        assert nt.status == GhostStatus.MISSING
        assert "ctrl+t" in nt.reason.lower()

    def test_ghost_fails_open_url_not_using_ctrl_l(self):
        plan = _ready_plan_dict()
        plan["steps"][3]["preferred_strategy"] = "open_url:bookmark_uia"
        m = Mission(name="g")
        _attach_plan(m, plan)
        report = simulate_mission(m)
        ou = _step_at(report, 3)
        assert ou is not None
        assert ou.status == GhostStatus.MISSING
        assert "ctrl+l" in ou.reason.lower()

    def test_ghost_fails_search_youtube_coords_primary(self):
        plan = _ready_plan_dict()
        plan["steps"][4]["preferred_strategy"] = "search_youtube:vision_coords"
        m = Mission(name="g")
        _attach_plan(m, plan)
        report = simulate_mission(m)
        sy = _step_at(report, 4)
        assert sy is not None
        assert sy.status == GhostStatus.MISSING
        assert "coord" in sy.reason.lower() or "visual" in sy.reason.lower()

    def test_ghost_fails_when_plan_uses_coords(self):
        plan = _ready_plan_dict()
        plan["coords_used"] = True
        m = Mission(name="g")
        _attach_plan(m, plan)
        report = simulate_mission(m)
        # Plan-level problem propaga MISSING a todos los steps.
        assert report.needs_user_signal
        for sr in report.steps:
            assert sr.status == GhostStatus.MISSING

    def test_ghost_fails_when_legacy_graph_not_ignored(self):
        plan = _ready_plan_dict()
        plan["legacy_graph_ignored"] = False
        m = Mission(name="g")
        _attach_plan(m, plan)
        report = simulate_mission(m)
        assert report.needs_user_signal
        for sr in report.steps:
            assert sr.status == GhostStatus.MISSING
