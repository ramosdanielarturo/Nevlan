"""
Nevlan — Tests reales de mission_1778303937 (PRD 2026-05-08d §A)
=================================================================

Caso real reportado por el usuario:

* Click en búsqueda Windows → escribir "Chrome" → click Chrome →
  click perfil "Daniel Chrome D Daniel Arturo Ramos" → nueva pestaña
  → bookmark YouTube → buscar canción → scroll.
* El recorder captura el evento del PROFILE picker, pero la captura
  UIA llega *tarde* (el picker se cierra demasiado rápido) y termina
  reflejando el contenido de YouTube:
  ``automation_id='rendering-content'``,
  ``class_name='style-scope ytd-in-feed-ad-layout-renderer'``.
* Resultado original: el plan quedaba con
  ``profile_name='Seleccionar perfil'`` y ``needs_user_label=False``,
  permitiendo que la misión llegara a ejecución sin perfil real.

Estos tests fijan las garantías nuevas:

  1. Tras recompilar, ``select_profile`` queda con
     ``profile_name=""`` y ``needs_user_label=True``.
  2. ``EMPTY_PROFILE_NAME_IN_SELECT_PROFILE`` aparece en
     ``not_ready_reasons`` de forma estable.
  3. La evidencia YouTube (ytd-*, rendering-content, style-scope)
     NUNCA se acepta como nombre de perfil ni alias del store.
  4. Tras ``confirm_profile`` con un nombre válido, la misión pasa
     a ``READY`` y queda idempotente para futuras corridas.
  5. El alias_store recuerda al perfil para próximas misiones del
     mismo usuario.
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
from app.services.missions.mission_review_confirm import confirm_profile
from app.services.missions import profile_resolver


FIXTURES_DIR = Path("tests/fixtures/real_missions")
RAW_FIXTURE = FIXTURES_DIR / "mission_1778303937_raw.json"
CONFIRMED_FIXTURE = FIXTURES_DIR / "mission_1778303937_confirmed.json"


@pytest.fixture(autouse=True)
def _isolate_alias_store(tmp_path, monkeypatch):
    fake = tmp_path / "profile_aliases.json"
    monkeypatch.setattr(
        profile_resolver, "_alias_store_path", lambda: fake,
    )
    profile_resolver.reset_alias_store()
    yield
    profile_resolver.reset_alias_store()


def _load_raw_mission() -> Mission:
    """Devuelve la misión real recompilada desde raw_trace.

    Importante: NO arrastramos el plan persistido — partimos del
    raw para que cualquier cambio en el ICE se refleje.
    """
    data = json.loads(RAW_FIXTURE.read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    data["review_confirmations"] = []
    data["tags"] = [
        t for t in (data.get("tags") or [])
        if not str(t).startswith("review_confirmed:")
    ]
    m = Mission(**data)
    apply_collapse_to_mission(m)
    return m


# ─────────────────────────────────────────────────────────────────────
# §A.1 — el plan deja profile_name="" + needs_user_label=True
# ─────────────────────────────────────────────────────────────────────


class TestProfileEmptyBeforeConfirmation:

    def test_real_mission_1778303937_profile_empty_before_confirmation(self):
        m = _load_raw_mission()
        plan = m.semantic_execution_plan
        select_steps = [
            s for s in plan["steps"] if s["type"] == "select_profile"
        ]
        assert len(select_steps) == 1
        sp = select_steps[0]

        assert (sp["params"].get("profile_name") or "") == "", (
            "El profile_name jamás debe quedar como literal "
            "('Seleccionar perfil', 'rendering-content', etc.) en "
            "una misión sin confirmación."
        )
        assert sp.get("needs_user_label") is True, (
            "El plan debe pedir confirmación humana (UI muestra "
            "'Confirmar perfil') si el profile no se resolvió."
        )
        assert sp.get("label_prompt"), (
            "Debe existir un prompt humano para la confirmación."
        )

    def test_blocker_empty_profile_name_present(self):
        m = _load_raw_mission()
        reasons = list(m.semantic_execution_plan.get("not_ready_reasons") or [])
        assert "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE" in reasons
        assert m.semantic_execution_plan["intent_status"] == "NEEDS_REVIEW"

    def test_no_executable_plan_with_empty_profile_name(self):
        m = _load_raw_mission()
        plan = m.semantic_execution_plan
        assert plan["intent_status"] != "READY", (
            "Una misión con profile vacío JAMÁS puede estar READY."
        )


# ─────────────────────────────────────────────────────────────────────
# §A.2 — evidencia YouTube/Polymer rechazada
# ─────────────────────────────────────────────────────────────────────


class TestProfileResolverRejectsYouTubeEvidence:

    @pytest.mark.parametrize("bad_value", [
        "rendering-content",
        "style-scope ytd-in-feed-ad-layout-renderer",
        "ytd-app",
        "ytd-rich-grid-renderer",
        "guide-service",
        "style-scope",
        "html5-main-video",
        "video-stream",
        "YouTube",
        "Nueva pestaña - Google Chrome",
        "YouTube - Google Chrome",
    ])
    def test_extract_canonical_rejects_youtube_polymer(self, bad_value):
        canonical = profile_resolver.extract_canonical_profile_name(bad_value)
        assert canonical == "", (
            f"extract_canonical_profile_name aceptó evidencia inválida: {bad_value!r} → {canonical!r}"
        )

    @pytest.mark.parametrize("bad_value", [
        "rendering-content",
        "style-scope ytd-in-feed-ad-layout-renderer",
        "ytd-app",
        "guide-service",
    ])
    def test_resolve_profile_neutralizes_invalid_observation(self, bad_value):
        res = profile_resolver.resolve_profile(
            human_observation=bad_value,
            ocr_observation=bad_value,
            window_title=bad_value,
        )
        assert not res.is_resolved
        assert res.profile_name == ""
        assert res.needs_user_confirmation is True
        # Estos valores tampoco deben reaparecer en candidate_aliases.
        for cand in res.candidate_aliases:
            assert not profile_resolver.is_invalid_profile_evidence(cand)

    def test_profile_resolver_rejects_youtube_ad_groupcontrol_as_profile(self):
        """Reproduce exactamente el caso del recorder real:

        El UIA dice ``automation_id='rendering-content',
        class_name='style-scope ytd-in-feed-ad-layout-renderer',
        name=''``. El resolver no debe construir un perfil con eso.
        """
        observation = "rendering-content style-scope ytd-in-feed-ad-layout-renderer"
        res = profile_resolver.resolve_profile(
            human_observation=observation,
            raw_profile=observation,
        )
        assert res.profile_name == ""
        assert res.needs_user_confirmation is True

    def test_profile_resolver_does_not_persist_invalid_alias(self):
        """Aunque el caller pase basura, el alias_store debe
        permanecer limpio tras un resolve_profile fallido.
        """
        before = profile_resolver._load_alias_store()
        res = profile_resolver.resolve_profile(
            human_observation="ytd-app guide-service",
        )
        # resolve_profile NO debe persistir nada por sí mismo.
        assert profile_resolver._load_alias_store() == before
        # Y los candidate_aliases visibles tampoco deben venir
        # con la basura (filtramos antes de _record).
        for c in res.candidate_aliases:
            assert not profile_resolver.is_invalid_profile_evidence(c)


# ─────────────────────────────────────────────────────────────────────
# §A.3 — confirmación pone READY + alias persistente
# ─────────────────────────────────────────────────────────────────────


class TestProfileConfirmationFlow:

    def test_real_mission_1778303937_profile_confirmation_sets_ready_candidate(self):
        m = _load_raw_mission()
        # Confirmamos también la query (es el otro blocker pendiente
        # — el PRD nos dice no tocar query fidelity, pero aquí solo
        # ejercitamos el verbo existente para llevar la misión a
        # READY y comprobar que el confirm_profile no rompe el plan).
        from app.services.missions.mission_review_confirm import confirm_query
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")

        plan = confirm_profile(m, profile_name="Daniel Arturo Ramos")
        assert plan.intent_status == "READY", (
            f"Tras confirmar perfil + query la misión debe estar "
            f"READY; quedó {plan.intent_status} con "
            f"reasons={plan.not_ready_reasons}"
        )
        # La entrada del audit trail existe y refleja el origen
        # ("user_confirmation").
        rcs = m.review_confirmations
        prof_entries = [c for c in rcs if c.field == "profile_name"]
        assert len(prof_entries) == 1
        assert prof_entries[0].value == "Daniel Arturo Ramos"
        assert prof_entries[0].source == "user_confirmation"

    def test_profile_alias_store_reuses_daniel_arturo_ramos(self):
        """Al confirmar el nombre, el alias_store queda poblado con
        'Daniel Arturo Ramos' + aliases derivados. Una próxima
        observación humana parecida ("Daniel Arturo") debe resolver
        directo al canonical sin pedir confirmación.
        """
        m = _load_raw_mission()
        confirm_profile(m, profile_name="Daniel Arturo Ramos")

        # Próxima misión: el resolver debe encontrar el canonical
        # via alias_store usando solo "Daniel Arturo".
        res = profile_resolver.resolve_profile(
            human_observation="Daniel Arturo",
        )
        assert res.profile_name == "Daniel Arturo Ramos"
        assert res.is_resolved is True
        # source puede ser human_observation (extractor canónico)
        # o alias_store (lookup), ambos valen.
        assert res.source in ("human_observation", "alias_store")

    def test_profile_resolver_uses_previous_profile_picker_evidence(self):
        """Si el alias_store tiene "Daniel Arturo Ramos" aprendido y
        vemos una observación humana razonable ("Daniel Chrome D
        Daniel Arturo Ramos"), debemos resolver SIN pedir confirmación.
        """
        # Aprendemos primero (simulando misión previa).
        profile_resolver.learn_profile_alias(
            "Daniel Arturo Ramos",
            aliases=["Daniel Chrome D Daniel Arturo Ramos", "Daniel Arturo"],
        )
        res = profile_resolver.resolve_profile(
            human_observation="Daniel Chrome D Daniel Arturo Ramos",
        )
        assert res.is_resolved
        assert res.profile_name == "Daniel Arturo Ramos"

    def test_confirmed_fixture_is_ready(self):
        """El fixture _confirmed.json debe estar en READY (es el
        snapshot post-confirmación que documenta el estado final
        esperado de la UI tras Mission Review)."""
        data = json.loads(CONFIRMED_FIXTURE.read_text(encoding="utf-8"))
        plan = data["semantic_execution_plan"]
        assert plan["intent_status"] == "READY"
        assert not plan.get("not_ready_reasons")
        # Los reviewers ven 2 confirmaciones: profile + query.
        rcs = data.get("review_confirmations") or []
        assert any(c.get("field") == "profile_name" for c in rcs)
        assert any(c.get("field") == "query" for c in rcs)


# ─────────────────────────────────────────────────────────────────────
# §A.3 — _heal_stale_select_profile cura planes viejos
# ─────────────────────────────────────────────────────────────────────


class TestHealStaleSelectProfile:

    def test_heal_recompiles_when_profile_name_is_generic_label(self):
        from app.interfaces.desktop.mission_review import (
            _heal_stale_select_profile,
        )
        m = _load_raw_mission()
        # Forzamos un plan stale: profile_name='Seleccionar perfil'
        # y needs_user_label=False (el bug del JSON live original).
        plan = m.semantic_execution_plan
        for s in plan["steps"]:
            if s["type"] == "select_profile":
                s["params"]["profile_name"] = "Seleccionar perfil"
                s["needs_user_label"] = False
                break
        m.semantic_execution_plan = plan

        _heal_stale_select_profile(m)

        for s in m.semantic_execution_plan["steps"]:
            if s["type"] == "select_profile":
                assert s["params"].get("profile_name", "") == ""
                assert s.get("needs_user_label") is True

    def test_heal_recompiles_when_profile_name_is_polymer_garbage(self):
        from app.interfaces.desktop.mission_review import (
            _heal_stale_select_profile,
        )
        m = _load_raw_mission()
        plan = m.semantic_execution_plan
        for s in plan["steps"]:
            if s["type"] == "select_profile":
                s["params"]["profile_name"] = "rendering-content style-scope"
                s["needs_user_label"] = False
                break
        m.semantic_execution_plan = plan
        _heal_stale_select_profile(m)
        for s in m.semantic_execution_plan["steps"]:
            if s["type"] == "select_profile":
                # El nombre tóxico fue purgado.
                assert "rendering-content" not in (s["params"].get("profile_name") or "")
                assert "style-scope" not in (s["params"].get("profile_name") or "")
