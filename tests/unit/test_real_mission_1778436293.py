"""
Nevlan — Tests reales de mission_1778436293 (PRD 2026-05-10)
=============================================================

Misión real reportada por el usuario:

    id: 725ee8dc-306d-4b02-afef-3b22fefc0b24
    name: mission_1778436293

Acciones humanas reales:

  1. Click en búsqueda Windows → escribir "Chrome" → click Chrome.
  2. (Chrome ya estaba con un perfil activo — no apareció picker.)
  3. Click en "Nueva pestaña".
  4. Click en barra del navegador → escribir "Banco de Mexico" → Enter.
  5. Click en resultado "Banxico, banco central, Banco de México".
  6. Scroll dentro de la página de Banxico.

Bug original:

  * El plan colapsado quedaba como READY con solo
    ``[open_app, open_new_tab, open_url youtube.com]``.
  * Se perdía la búsqueda, el click al resultado y el scroll.
  * Aparecía YouTube por contaminación de un tab heredado de la
    sesión anterior.
  * "Banco de" / " de Mexico" se quedaban como dos pasos partidos
    por el field_commit asíncrono del recorder.

Estos tests fijan las garantías nuevas (PRD 2026-05-10
secciones A, B, C, D, E, F, G, H).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.contracts.mission import Mission
from app.services.missions import profile_resolver
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
    collapse_intent,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "real_missions"
    / "mission_1778436293_raw.json"
)


@pytest.fixture(autouse=True)
def _isolate_alias_store(tmp_path, monkeypatch):
    """No queremos que el alias_store del repo contamine este test."""
    fake = tmp_path / "profile_aliases.json"
    monkeypatch.setattr(
        profile_resolver, "_alias_store_path", lambda: fake,
    )
    profile_resolver.reset_alias_store()
    yield
    profile_resolver.reset_alias_store()


def _load_raw_mission() -> Mission:
    """Recompila la misión real desde el raw_trace fixture."""
    if not FIXTURE.exists():
        pytest.skip(f"Fixture no disponible: {FIXTURE}")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    data["review_confirmations"] = []
    data["tags"] = []
    return Mission.model_validate(data)


def _types_in_plan(mission: Mission):
    sep = mission.semantic_execution_plan or {}
    return [s.get("type") for s in (sep.get("steps") or [])]


def _step_by_type(mission: Mission, kind: str):
    sep = mission.semantic_execution_plan or {}
    for s in (sep.get("steps") or []):
        if s.get("type") == kind:
            return s
    return None


# ──────────────────────────────────────────────────────────────────
# 1. No falso READY cuando se pierden eventos relevantes (§A)
# ──────────────────────────────────────────────────────────────────


def test_real_mission_1778436293_not_false_ready_with_dropped_steps():
    """READY/NEEDS_REVIEW debe reflejar la realidad: si el engine
    cubre todos los eventos relevantes con steps semánticos, READY
    es válido. Si NO los cubre, debe estar en NEEDS_REVIEW con el
    blocker SEMANTIC_EVENT_DROPPED_FROM_PLAN.
    """
    mission = _load_raw_mission()
    apply_collapse_to_mission(mission)
    sep = mission.semantic_execution_plan or {}
    status = sep.get("intent_status")
    blockers = list(sep.get("not_ready_reasons") or [])
    dropped = list(getattr(mission, "_dropped_semantic_events", None) or [])
    if dropped:
        assert status == "NEEDS_REVIEW", (status, blockers, dropped)
        assert "SEMANTIC_EVENT_DROPPED_FROM_PLAN" in blockers, blockers
    else:
        # Cobertura completa: la única forma de NO estar en READY
        # sería tener algún otro blocker; verificamos que NO sea el
        # falso READY antiguo de 3 pasos.
        types = _types_in_plan(mission)
        # Mínimo el cuerpo de la misión: search_web + open_search_result.
        assert "search_web" in types, types
        assert "open_search_result" in types, types


# ──────────────────────────────────────────────────────────────────
# 2-4. La query, el click al resultado y el scroll deben quedar (§B-E)
# ──────────────────────────────────────────────────────────────────


def test_real_mission_1778436293_plan_has_banco_de_mexico_search():
    mission = _load_raw_mission()
    apply_collapse_to_mission(mission)
    sw = _step_by_type(mission, "search_web")
    assert sw is not None, _types_in_plan(mission)
    params = sw.get("params") or {}
    assert params.get("query") == "Banco de Mexico", params
    assert params.get("target") == "browser_address_bar", params
    assert (params.get("engine") or "").lower() == "google", params


def test_real_mission_1778436293_plan_has_banxico_result_click():
    mission = _load_raw_mission()
    apply_collapse_to_mission(mission)
    osr = _step_by_type(mission, "open_search_result")
    assert osr is not None, _types_in_plan(mission)
    title = (osr.get("params") or {}).get("title", "")
    assert "Banxico" in title, title


def test_real_mission_1778436293_plan_has_scroll():
    mission = _load_raw_mission()
    apply_collapse_to_mission(mission)
    sp = _step_by_type(mission, "scroll_page")
    assert sp is not None, _types_in_plan(mission)
    params = sp.get("params") or {}
    assert (params.get("direction") or "").lower() == "down"
    assert int(params.get("amount") or 0) >= 1


# ──────────────────────────────────────────────────────────────────
# 5. NO debe arrastrar YouTube (§C)
# ──────────────────────────────────────────────────────────────────


def test_real_mission_1778436293_plan_does_not_have_youtube():
    """El raw_trace incluye un click en una ventana cuyo título es
    'Devuelveme el amor Luis Miguel - YouTube - Google Chrome'
    (tab heredado). El engine NO debe convertir eso en
    open_url youtube.com porque YouTube no fue el destino real
    de esta misión.
    """
    mission = _load_raw_mission()
    apply_collapse_to_mission(mission)
    sep = mission.semantic_execution_plan or {}
    for step in (sep.get("steps") or []):
        params = step.get("params") or {}
        url = str(params.get("url") or "").lower()
        alias = str(params.get("alias") or "").lower()
        engine = str(params.get("engine") or "").lower()
        assert "youtube" not in url, step
        assert alias != "youtube", step
        assert engine != "youtube", step
    # Tampoco como tipo de step.
    assert "search_youtube" not in _types_in_plan(mission)


# ──────────────────────────────────────────────────────────────────
# 6. Field commit: "Banco de" + " de Mexico" → "Banco de Mexico" (§B)
# ──────────────────────────────────────────────────────────────────


def test_real_mission_1778436293_field_commit_merges_query():
    """El recorder produjo dos eventos keyboard_type_text:
    "Banco de" y " de Mexico" con field_session=True. El engine
    debe fusionarlos en una única búsqueda "Banco de Mexico"
    (no debe quedar partido en dos steps ejecutables ni meter
    Enter en medio).
    """
    mission = _load_raw_mission()
    plan = collapse_intent(mission=mission)
    sw_steps = [s for s in plan.semantic_steps if s.type == "search_web"]
    assert len(sw_steps) == 1, [s.type for s in plan.semantic_steps]
    assert sw_steps[0].params.get("query") == "Banco de Mexico"


# ──────────────────────────────────────────────────────────────────
# 7. Eventos relevantes contabilizados (§A)
# ──────────────────────────────────────────────────────────────────


def test_real_mission_1778436293_relevant_raw_events_accounted_for():
    """Para esta misión específica, los eventos relevantes
    (typed_text "Banco de", Enter, typed_text " de Mexico", click
    Banxico, dos scrolls) deben quedar TODOS cubiertos por algún
    step semántico — la lista de dropped events debe estar vacía.
    """
    mission = _load_raw_mission()
    apply_collapse_to_mission(mission)
    dropped = list(getattr(mission, "_dropped_semantic_events", None) or [])
    assert dropped == [], dropped
    sep = mission.semantic_execution_plan or {}
    blockers = list(sep.get("not_ready_reasons") or [])
    assert "SEMANTIC_EVENT_DROPPED_FROM_PLAN" not in blockers, blockers


# ──────────────────────────────────────────────────────────────────
# 8. Perfil seleccionado representado o bloqueado honestamente (§G)
# ──────────────────────────────────────────────────────────────────


def test_real_mission_1778436293_profile_selected_represented_or_blocks():
    """Para esta misión Chrome ya estaba abierto/logueado. El
    engine debe representar ese estado de forma segura (con
    use_selected_profile) y NO inventar "Daniel Arturo Ramos"
    ni promover "Daniel" automáticamente.
    """
    mission = _load_raw_mission()
    apply_collapse_to_mission(mission)
    types = _types_in_plan(mission)

    # Debe haber al menos UNA representación del perfil.
    has_profile_step = (
        "select_profile" in types
        or "use_selected_profile" in types
    )
    assert has_profile_step, types

    # Y SI emite use_selected_profile (caso esperado para esta
    # misión), NO debe contener un profile_name inventado.
    usp = _step_by_type(mission, "use_selected_profile")
    if usp is not None:
        params = usp.get("params") or {}
        assert "profile_name" not in params, params
        assert params.get("profile_resolution") == (
            "already_active_or_selected"
        )

    # Si por alguna razón emite select_profile, verifica que NO
    # haya un Daniel inventado.
    sp = _step_by_type(mission, "select_profile")
    if sp is not None:
        params = sp.get("params") or {}
        # No debe traer "Daniel Arturo Ramos" — no hay evidencia.
        assert (
            params.get("profile_name") != "Daniel Arturo Ramos"
        ), params


# ──────────────────────────────────────────────────────────────────
# 9. Plan completo esperado (§H)
# ──────────────────────────────────────────────────────────────────


def test_real_mission_1778436293_plan_minimum_shape():
    """El plan final debe contener al menos:
        open_app, (use_)select(ed)_profile, open_new_tab,
        search_web, open_search_result, scroll_page.
    """
    mission = _load_raw_mission()
    apply_collapse_to_mission(mission)
    types = _types_in_plan(mission)
    must_have = (
        "open_app",
        "open_new_tab",
        "search_web",
        "open_search_result",
        "scroll_page",
    )
    for kind in must_have:
        assert kind in types, (kind, types)
    # Y un representante del perfil.
    assert (
        "select_profile" in types or "use_selected_profile" in types
    ), types


# ──────────────────────────────────────────────────────────────────
# 10. Helpers — pruebas focalizadas y portátiles (§B / §F)
# ──────────────────────────────────────────────────────────────────


def test_smart_merge_handles_overlap_and_no_overlap_cases():
    """El smart_merge debe:
       * Detectar overlap "Banco de" + " de Mexico" → "Banco de Mexico".
       * Concatenar con espacio cuando no hay overlap.
       * No inventar texto.
    """
    from app.services.missions.intent_collapse_engine import (
        _smart_merge_text_fragments,
    )
    assert _smart_merge_text_fragments(["Banco de", " de Mexico"]) == (
        "Banco de Mexico"
    )
    assert _smart_merge_text_fragments(["Banco", " de Mexico"]) == (
        "Banco de Mexico"
    )
    assert _smart_merge_text_fragments(["foo", "bar"]) == "foo bar"
    assert _smart_merge_text_fragments(["foo", " foo bar"]) == "foo bar"
    assert _smart_merge_text_fragments(["unico"]) == "unico"
    assert _smart_merge_text_fragments([]) == ""


def test_event_order_prefers_sequence_id_over_timestamp_when_available():
    """Aunque dos eventos del recorder vengan con offset_ms invertido
    (caso real del field_commit asíncrono), el orden estable los pone
    por sequence_id ascendente.
    """
    from app.contracts.mission import RawEvent, EventType
    from app.services.missions.intent_collapse_engine import (
        _stable_sort_raw_trace,
    )
    e_late_ts_low_off = RawEvent(
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        sequence_id=1729,
        offset_ms=19923,
        timestamp="2026-05-10T01:00:00.001Z",
        metadata={"final_text": " de Mexico"},
    )
    e_enter = RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        sequence_id=1728,
        offset_ms=19924,
        timestamp="2026-05-10T01:00:00.002Z",
        keyboard_action={"keys": ["enter"], "modifiers": []},
    )
    sorted_evs = _stable_sort_raw_trace([e_late_ts_low_off, e_enter])
    assert sorted_evs[0].sequence_id == 1728
    assert sorted_evs[1].sequence_id == 1729
