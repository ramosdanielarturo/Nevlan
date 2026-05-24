"""
Nevlan — E2E con la grabación REAL ``mission_1778221896`` (PRD 2026-05-08)
============================================================================

Este test usa DOS fixtures derivadas de la grabación humana real
(persistidas en ``tests/fixtures/real_missions/``):

  * ``mission_1778221896_raw.json``
        Estado natural del raw_trace tras el ICE actual.
        ``intent_status == NEEDS_REVIEW``, sin confirmaciones.
        Es la pieza forense crítica: cualquier regresión que
        permita READY sin confirmar humano debe romper aquí.

  * ``mission_1778221896_confirmed.json``
        Estado tras Mission Review (confirm_profile + confirm_query).
        ``intent_status == READY`` con ``review_confirmations``
        estructuradas. Demuestra el caso exitoso end-to-end.

Razones para mantener AMBAS fixtures:

  1. Necesitamos seguir probando el bug original (raw incompleto
     ⇒ NEEDS_REVIEW correcto) sin depender del JSON live (que se
     muta cuando el usuario interactúa con la UI).
  2. El JSON live NUNCA debe afirmar READY si la grabación venía
     incompleta — el archivo persistente no debe ser engañoso.
  3. Si en el futuro el ICE mejora y extrae el perfil/query desde
     el raw_trace SOLO, el fixture ``raw`` lo detectará y forzará
     a actualizar la fixture (con justificación).

Diseño:

  * La fixture ``raw`` es el reflejo natural del raw_trace tras el
    ICE — NO se mutan campos para ayudar al colapsador.
  * La confirmación de Mission Review se aplica desde el módulo
    ``mission_review_confirm`` — exactamente lo que la UI invoca.
  * El alias persistido se aísla con un alias store temporal para
    no contaminar el del usuario.
  * El ``raw_trace`` JAMÁS se modifica en ninguna ruta — propiedad
    forense verificada por test explícito.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.mission_review_confirm import (
    SOURCE_HEURISTIC_ACCEPTED,
    SOURCE_USER,
    confirm_profile, confirm_query,
)
from app.services.missions import profile_resolver

FIXTURES_DIR = Path("tests/fixtures/real_missions")
RAW_FIXTURE = FIXTURES_DIR / "mission_1778221896_raw.json"
CONFIRMED_FIXTURE = FIXTURES_DIR / "mission_1778221896_confirmed.json"
LIVE_PATH = Path(
    "var/missions/82bec5fd-cefb-45db-a82d-4bf074bf4180.json"
)
EXPECTED_NAME = "mission_1778221896"


@pytest.fixture(autouse=True)
def _isolate_alias_store(tmp_path, monkeypatch):
    """No tocar el alias store real del usuario."""
    fake = tmp_path / "profile_aliases.json"
    monkeypatch.setattr(
        profile_resolver, "_alias_store_path", lambda: fake,
    )
    profile_resolver.reset_alias_store()
    yield
    profile_resolver.reset_alias_store()


@pytest.fixture
def real_mission_dict() -> dict:
    """JSON tal cual está persistido en la fixture RAW.

    NO carga el archivo live (``var/missions/<id>.json``) porque la
    UI puede haberlo mutado. La fixture vive en
    ``tests/fixtures/real_missions/`` y es la fuente de verdad
    para los tests.
    """
    assert RAW_FIXTURE.exists(), (
        f"Falta la fixture RAW {RAW_FIXTURE}. "
        "Regenérala con: python scripts/build_real_mission_fixtures.py"
    )
    raw = json.loads(RAW_FIXTURE.read_text(encoding="utf-8"))
    assert raw.get("name") == EXPECTED_NAME, (
        f"La fixture cambió: name={raw.get('name')!r} esperado "
        f"{EXPECTED_NAME!r}."
    )
    return raw


@pytest.fixture
def confirmed_mission_dict() -> dict:
    """Fixture del estado tras Mission Review (READY)."""
    assert CONFIRMED_FIXTURE.exists(), (
        f"Falta la fixture CONFIRMED {CONFIRMED_FIXTURE}. "
        "Regenérala con: python scripts/build_real_mission_fixtures.py"
    )
    return json.loads(CONFIRMED_FIXTURE.read_text(encoding="utf-8"))


def _build_mission_for_replay(raw: dict) -> Mission:
    """Recrea la misión desde el raw_trace forzando recompute.

    Limpiamos:
      * ``semantic_execution_plan`` (lo regeneramos con el ICE actual)
      * ``compiled_execution_graph`` (legacy_debug_only — no lo usamos
        para decidir READY)
      * ``interpreted_steps`` / ``action_groups`` (derivados)
      * ``status`` → "draft" (vamos a recomputar)

    Lo que NO tocamos:
      * ``raw_trace`` (es la evidencia humana real)
      * ``aliases`` / ``triggers`` / ``policy``
    """
    data = copy.deepcopy(raw)
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    data["review_confirmations"] = []
    return Mission(**data)


# ============================================================
# Parte 1 — replay del recorder (BEFORE)
# ============================================================


class TestRealMissionBeforeReview:
    """Estado tras el ICE, sin intervención de Mission Review."""

    def test_real_mission_loads(self, real_mission_dict):
        assert real_mission_dict["name"] == EXPECTED_NAME
        assert real_mission_dict.get("raw_trace"), (
            "raw_trace vacío en el JSON real."
        )

    def test_replay_produces_six_semantic_steps(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        types = [s["type"] for s in sep["steps"]]
        assert types == [
            "open_app",
            "select_profile",
            "open_new_tab",
            "open_url",
            "search_content",
            "scroll_results",
        ], (
            f"La estructura del plan cambió: {types}. "
            "El usuario pidió EXACTAMENTE estos 6 pasos."
        )

    def test_replay_produces_needs_review_status(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert sep["intent_status"] == "NEEDS_REVIEW", (
            f"Sin confirmación humana, la evidencia REAL no permite "
            f"READY. Status actual: {sep['intent_status']!r}, "
            f"reasons={sep['not_ready_reasons']}"
        )

    def test_replay_blocker_empty_profile(self, real_mission_dict):
        """El raw_trace real no tiene UIA name del perfil. El sistema
        debe reconocerlo y bloquear ``READY`` hasta confirmar."""
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE" in (
            sep["not_ready_reasons"]
        )
        sp = sep["steps"][1]
        assert sp["type"] == "select_profile"
        assert sp["params"]["profile_name"] == "", (
            f"profile_name debe quedar VACÍO (no inventado) si la "
            f"evidencia es insuficiente. Actual: "
            f"{sp['params']['profile_name']!r}"
        )
        assert sp["needs_user_label"] is True
        # El prompt debe ser explícito.
        assert sp["label_prompt"]

    def test_replay_blocker_query_suspect(self, real_mission_dict):
        """El usuario tipeó "Devuelveme el amor Luis Miguel" — sin
        "de". La heurística DEBE marcarlo como suspect (genérica:
        "<contenido> + <Nombre Apellido>" sin conector)."""
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert "QUERY_FIDELITY_SUSPECT" in sep["not_ready_reasons"], (
            f"El raw_trace NO contiene 'de' entre 'amor' y 'Luis "
            f"Miguel'. La heurística debe detectarlo. reasons="
            f"{sep['not_ready_reasons']}"
        )
        sp = sep["steps"][4]
        assert sp["type"] == "search_content"
        assert str((sp.get("params") or {}).get("provider") or "").lower() \
            == "youtube"
        # La query NO se inventa — quedamos con lo que el usuario
        # escribió.
        assert sp["params"]["query"] == "Devuelveme el amor Luis Miguel"
        assert sp["params"]["query_fidelity_status"] == "suspect"
        # Pero ofrecemos la sugerencia preseleccionada para confirmar.
        assert (
            sp["params"]["suggested_query"]
            == "Devuelveme el amor de Luis Miguel"
        )
        assert sp["params"]["suspected_missing_connector"] == "de"
        assert sp["needs_user_label"] is True
        assert "quisiste decir" in sp["label_prompt"].lower()

    def test_replay_no_temp_paths_in_plan(self, real_mission_dict):
        """Esta misión real NO tiene assets /temp/ — el blocker
        ``TEMP_ASSET_IN_APPROVED_PLAN`` no debe aparecer."""
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert "TEMP_ASSET_IN_APPROVED_PLAN" not in (
            sep["not_ready_reasons"]
        )

    def test_replay_no_legacy_runtime_contamination(
        self, real_mission_dict,
    ):
        """Ningún param del semantic plan debe arrastrar campos
        legacy del compiled_graph (``execution_strategy``,
        ``locator_strategy``, etc.)."""
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert "LEGACY_RUNTIME_CONTAMINATION" not in (
            sep["not_ready_reasons"]
        )

    def test_replay_legacy_graph_ignored_and_coords_false(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert sep["coords_used"] is False
        assert sep["legacy_graph_ignored"] is True
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"


# ============================================================
# Parte 2 — flujo de Mission Review (AFTER)
# ============================================================


class TestRealMissionAfterReview:
    """Tras la confirmación humana, la misión real DEBE pasar a
    READY sin regrabar."""

    def test_confirm_profile_alone_does_not_reach_ready(
        self, real_mission_dict,
    ):
        """Confirmar perfil resuelve un blocker pero la query sigue
        suspect — debe seguir NEEDS_REVIEW."""
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        sep = m.semantic_execution_plan
        assert "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE" not in (
            sep["not_ready_reasons"]
        )
        assert sep["intent_status"] == "NEEDS_REVIEW"
        assert "QUERY_FIDELITY_SUSPECT" in sep["not_ready_reasons"]

    def test_confirm_query_alone_does_not_reach_ready(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_query(
            m, query="Devuélveme el amor de Luis Miguel",
        )
        sep = m.semantic_execution_plan
        assert "QUERY_FIDELITY_SUSPECT" not in (
            sep["not_ready_reasons"]
        )
        assert sep["intent_status"] == "NEEDS_REVIEW"
        assert "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE" in (
            sep["not_ready_reasons"]
        )

    def test_full_review_brings_mission_to_ready(
        self, real_mission_dict,
    ):
        """El test crítico del PRD 2026-05-08 §H. Tras confirmar
        ambos datos, el plan REAL debe pasar a READY con cero
        blockers."""
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)

        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")

        sep = m.semantic_execution_plan
        assert sep["not_ready_reasons"] == [], (
            f"Tras confirmar perfil + query, NO deben quedar "
            f"blockers. Reasons restantes: {sep['not_ready_reasons']}"
        )
        assert sep["intent_status"] == "READY", (
            f"intent_status debe ser READY, fue "
            f"{sep['intent_status']!r}"
        )
        assert sep["coords_used"] is False
        assert sep["legacy_graph_ignored"] is True

    def test_full_review_yields_correct_profile_name(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        sep = m.semantic_execution_plan
        sp = sep["steps"][1]
        assert sp["type"] == "select_profile"
        assert sp["params"]["profile_name"] == "Daniel Arturo Ramos"
        assert sp["needs_user_label"] is False
        assert sp["params"].get("profile_confirmed_via") == (
            "mission_review"
        )

    def test_full_review_yields_correct_query(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        sep = m.semantic_execution_plan
        sp = sep["steps"][4]
        assert sp["type"] == "search_content"
        assert str((sp.get("params") or {}).get("provider") or "").lower() \
            == "youtube"
        assert sp["params"]["query"] == (
            "Devuélveme el amor de Luis Miguel"
        )
        assert sp["params"]["query_fidelity_status"] == "ok"
        assert sp["params"].get("query_confirmed_via") == (
            "mission_review"
        )
        assert sp["needs_user_label"] is False
        # Sugerencia heurística limpiada — ya quedó aceptada.
        assert "suggested_query" not in sp["params"]
        assert "suspected_missing_connector" not in sp["params"]

    def test_review_persists_alias_for_future_runs(
        self, real_mission_dict,
    ):
        """Tras confirmar el perfil, la próxima misión con
        evidencia alias-compatible debe autorrepararse SIN preguntar.
        """
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")

        # Lookup directo desde el alias store: cualquier observación
        # canónica debe devolver el nombre.
        for obs in (
            "Daniel Arturo Ramos",
            "daniel arturo ramos",
            "daniel.arturo.ramos",
        ):
            assert (
                profile_resolver.lookup_profile_by_alias(obs)
                == "Daniel Arturo Ramos"
            ), f"alias '{obs}' no resolvió a canonical."

    def test_review_strategy_chain_unchanged_after_confirm(
        self, real_mission_dict,
    ):
        """Confirmar datos NO debe alterar el chain de estrategias —
        solo limpia params + needs_label."""
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        before = [
            (s["type"], s["preferred_strategy"], list(s["fallback_strategies"]))
            for s in m.semantic_execution_plan["steps"]
        ]
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        after = [
            (s["type"], s["preferred_strategy"], list(s["fallback_strategies"]))
            for s in m.semantic_execution_plan["steps"]
        ]
        assert before == after, (
            "El chain de estrategias cambió tras la confirmación; "
            "no debería."
        )

    def test_audit_tags_recorded(self, real_mission_dict):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        assert (
            "review_confirmed:profile:Daniel Arturo Ramos" in m.tags
        )
        assert (
            "review_confirmed:query:"
            "Devuélveme el amor de Luis Miguel" in m.tags
        )


# ============================================================
# Parte 3 — defensas contra regresión
# ============================================================


class TestRealMissionGuardrails:
    """Garantías generales de que la solución NO está hardcodeada
    a este caso particular."""

    def test_confirm_profile_rejects_empty(self, real_mission_dict):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        with pytest.raises(ValueError):
            confirm_profile(m, profile_name="")
        with pytest.raises(ValueError):
            confirm_profile(m, profile_name="   ")

    def test_confirm_profile_rejects_generic_label(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        with pytest.raises(ValueError):
            confirm_profile(m, profile_name="Seleccionar perfil")

    def test_confirm_query_rejects_empty(self, real_mission_dict):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        with pytest.raises(ValueError):
            confirm_query(m, query="")

    def test_idempotent_double_confirm(self, real_mission_dict):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        sep = m.semantic_execution_plan
        assert sep["intent_status"] == "READY"
        # No tags duplicados
        prof_tags = [
            t for t in m.tags
            if t.startswith("review_confirmed:profile:")
        ]
        assert len(prof_tags) == 1


# ============================================================
# Parte 4 — propiedad forense: raw_trace inmutable
#                              + review_confirmations estructuradas
# ============================================================


class TestForensicAuditTrail:
    """PRD 2026-05-08 (revisión §2): el JSON real no debe ser
    engañoso. ``raw_trace`` jamás se modifica; ``review_confirmations``
    documenta cada intervención humana."""

    def test_raw_trace_not_mutated_by_collapse(self, real_mission_dict):
        original = copy.deepcopy(real_mission_dict["raw_trace"])
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        after = json.loads(m.model_dump_json())["raw_trace"]
        assert after == original, (
            "El ICE modificó raw_trace; debe ser INMUTABLE."
        )

    def test_raw_trace_not_mutated_by_confirm_profile(
        self, real_mission_dict,
    ):
        original = copy.deepcopy(real_mission_dict["raw_trace"])
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        after = json.loads(m.model_dump_json())["raw_trace"]
        assert after == original, (
            "confirm_profile modificó raw_trace; NO debe."
        )

    def test_raw_trace_not_mutated_by_confirm_query(
        self, real_mission_dict,
    ):
        original = copy.deepcopy(real_mission_dict["raw_trace"])
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        after = json.loads(m.model_dump_json())["raw_trace"]
        assert after == original, (
            "confirm_query modificó raw_trace; NO debe."
        )

    def test_raw_fixture_starts_with_no_confirmations(
        self, real_mission_dict,
    ):
        """Fixture forense: la grabación raw NO tiene confirmaciones
        humanas — debe quedar evidente que el READY (si aparece) NO
        viene del raw_trace, sino de la intervención del usuario.
        """
        assert real_mission_dict.get("review_confirmations") == []

    def test_confirmed_fixture_persists_two_structured_entries(
        self, confirmed_mission_dict,
    ):
        """La fixture CONFIRMED debe leerse como un JSON honesto:
        cualquier persona puede ver QUÉ se confirmó y POR QUIÉN."""
        confs = confirmed_mission_dict.get("review_confirmations") or []
        assert len(confs) == 2, (
            f"Esperaba 2 confirmaciones (perfil + query), "
            f"hay {len(confs)}."
        )

        by_field = {c["field"]: c for c in confs}
        assert "profile_name" in by_field
        assert "query" in by_field

        prof = by_field["profile_name"]
        assert prof["value"] == "Daniel Arturo Ramos"
        assert prof["source"] == "user_confirmation"
        assert prof["step_type"] == "select_profile"
        assert prof["step_id"]  # apunta a un step concreto

        qry = by_field["query"]
        assert qry["value"] == "Devuélveme el amor de Luis Miguel"
        assert qry["source"] == "user_confirmation"
        assert qry["step_type"] == "search_content"
        # original_value debe preservar lo que el ICE había puesto
        assert qry["original_value"] == "Devuelveme el amor Luis Miguel"

    def test_confirmation_records_original_value_for_query(
        self, real_mission_dict,
    ):
        """Cuando el usuario corrige, debe quedar registro de QUÉ
        valor existía antes (auditoría de intervención)."""
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        confs = [
            c for c in (m.review_confirmations or [])
            if c.field == "query"
        ]
        assert len(confs) == 1
        assert confs[0].original_value == "Devuelveme el amor Luis Miguel"
        assert confs[0].value == "Devuélveme el amor de Luis Miguel"

    def test_accepting_heuristic_suggestion_is_attributed_correctly(
        self, real_mission_dict,
    ):
        """Si el usuario acepta tal cual la sugerencia del ICE, el
        source debe quedar como ``heuristic_suggestion_accepted``,
        NO como ``user_confirmation`` puro — esto permite analizar
        qué tan buenas son nuestras sugerencias.
        """
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        # La sugerencia heurística era exactamente esa cadena —
        # el usuario aceptó "Aplicar sugerencia".
        suggested = "Devuelveme el amor de Luis Miguel"
        confirm_query(m, query=suggested)
        confs = [
            c for c in (m.review_confirmations or [])
            if c.field == "query"
        ]
        assert len(confs) == 1
        assert confs[0].source == SOURCE_HEURISTIC_ACCEPTED, (
            f"source debe ser SOURCE_HEURISTIC_ACCEPTED, fue "
            f"{confs[0].source!r}"
        )

    def test_double_confirm_does_not_duplicate_audit_entries(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        confs = list(m.review_confirmations or [])
        assert len(confs) == 2, (
            f"Esperaba 2 entradas, tengo {len(confs)} — "
            "double-confirm no debe duplicar audit trail."
        )

    def test_correcting_a_previous_confirmation_replaces_entry(
        self, real_mission_dict,
    ):
        """Si el usuario primero escribe X y luego corrige a Y, la
        audit trail debe mostrar Y (lo último aprobado), no acumular
        ambos como historia parcial.
        """
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Ramos")
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        prof_confs = [
            c for c in (m.review_confirmations or [])
            if c.field == "profile_name"
        ]
        assert len(prof_confs) == 1
        assert prof_confs[0].value == "Daniel Arturo Ramos"

    def test_live_json_is_not_misleadingly_ready(self):
        """El JSON live no debe afirmar READY si la grabación venía
        incompleta (PRD revisión §2: el archivo persistido no debe
        ser engañoso). Si en el futuro alguien lo persiste como
        READY sin ``review_confirmations``, este test falla.
        """
        if not LIVE_PATH.exists():
            pytest.skip("no hay misión live; nada que validar")
        data = json.loads(LIVE_PATH.read_text(encoding="utf-8"))
        sep = data.get("semantic_execution_plan") or {}
        confs = data.get("review_confirmations") or []
        if sep.get("intent_status") == "READY":
            assert confs, (
                "El JSON live afirma READY pero no tiene "
                "review_confirmations — eso sería engañoso."
            )
