"""
Nevlan — E2E con la grabación REAL ``mission_1778244908`` (PRD 2026-05-08c)
============================================================================

Caso PRD 2026-05-08c **cierre de LEGACY_RUNTIME_CONTAMINATION**.

La grabación real (Chrome → perfil → YouTube → buscar canción → scroll)
producía un JSON live que contenía ``LEGACY_RUNTIME_CONTAMINATION`` en
``not_ready_reasons`` aunque:

  * ``coords_used == False``
  * ``legacy_graph_ignored == True``
  * ``legacy_compiled_graph_purpose == "legacy_debug_only"``

Causa raíz: ``compute_ready_status`` emitía el blocker para CUALQUIER
detalle de contaminación detectado, incluso para casos puramente
debug/auditoría (grafo legacy presente pero marcado debug-only). El
fix introduce el flag ``affects_execution`` por detalle: solo cuando
algún detalle declara ``affects_execution=True`` el blocker se emite.

Estos tests usan dos fixtures:

  * ``mission_1778244908_raw.json``      — estado natural NEEDS_REVIEW
  * ``mission_1778244908_confirmed.json`` — estado tras confirmaciones

Las fixtures se generan con
``python scripts/build_real_mission_fixtures.py``.
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
    SOURCE_USER, confirm_profile, confirm_query,
)
from app.services.missions.mission_review_summary import (
    build_mission_review_summary, get_visible_blockers_for_user,
    render_expert_view, render_normal_view,
)
from app.services.missions import profile_resolver

FIXTURES_DIR = Path("tests/fixtures/real_missions")
RAW_FIXTURE = FIXTURES_DIR / "mission_1778244908_raw.json"
CONFIRMED_FIXTURE = FIXTURES_DIR / "mission_1778244908_confirmed.json"
LIVE_PATH = Path("var/missions/2d1fec16-aafd-41bd-980b-3e40e91d4ee4.json")
EXPECTED_NAME = "mission_1778244908"


@pytest.fixture(autouse=True)
def _isolate_alias_store(tmp_path, monkeypatch):
    fake = tmp_path / "profile_aliases.json"
    monkeypatch.setattr(
        profile_resolver, "_alias_store_path", lambda: fake,
    )
    profile_resolver.reset_alias_store()
    yield
    profile_resolver.reset_alias_store()


@pytest.fixture
def real_mission_dict() -> dict:
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


def _build_mission_for_replay(raw: dict) -> Mission:
    """Recrea la misión desde el raw_trace forzando recompute."""
    data = copy.deepcopy(raw)
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
    return Mission(**data)


# ============================================================
# Parte 1 — Cierre del bug LEGACY_RUNTIME_CONTAMINATION
# ============================================================


class TestRealMission1778244908NoFalseLegacyContamination:
    """Reglas que cierran la causa raíz del bug PRD 2026-05-08c §A."""

    def test_real_mission_uses_raw_json_not_idealized_fixture(
        self, real_mission_dict,
    ):
        """La fixture es derivada del JSON real (raw_trace presente)."""
        assert real_mission_dict["raw_trace"], (
            "La fixture no tiene raw_trace; sería un fixture idealizado "
            "y no representa la grabación real."
        )
        assert len(real_mission_dict["raw_trace"]) >= 8, (
            f"raw_trace solo tiene "
            f"{len(real_mission_dict['raw_trace'])} eventos; la grabación "
            "real tenía ≥ 10 (incluye apertura Chrome, perfil, búsqueda)."
        )

    def test_real_mission_1778244908_no_false_legacy_contamination_after_confirmations(
        self, real_mission_dict,
    ):
        """**El test del PRD 2026-05-08c §M**:
        tras confirm_profile + confirm_query el plan debe pasar a READY
        sin LEGACY_RUNTIME_CONTAMINATION.
        """
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)

        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")

        sep = m.semantic_execution_plan
        assert "LEGACY_RUNTIME_CONTAMINATION" not in (
            sep["not_ready_reasons"]
        ), (
            f"Tras confirmar perfil + query NO debe quedar "
            f"LEGACY_RUNTIME_CONTAMINATION. reasons="
            f"{sep['not_ready_reasons']}"
        )
        assert sep["intent_status"] == "READY"
        assert sep["not_ready_reasons"] == []
        assert sep["ready_provenance"] == "user_confirmation"
        assert sep["coords_used"] is False
        assert sep["legacy_graph_ignored"] is True
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"

    def test_no_false_legacy_contamination_BEFORE_confirmations(
        self, real_mission_dict,
    ):
        """Aún antes de confirmar, ``LEGACY_RUNTIME_CONTAMINATION`` NO
        debe aparecer: el grafo legacy es debug-only y no afecta
        ejecución. Los blockers reales son los datos faltantes.
        """
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert sep["intent_status"] == "NEEDS_REVIEW"
        assert "LEGACY_RUNTIME_CONTAMINATION" not in (
            sep["not_ready_reasons"]
        ), (
            f"NEEDS_REVIEW debe deberse a perfil/query, NO a "
            f"contaminación falsa. reasons={sep['not_ready_reasons']}"
        )
        # Los blockers correctos son los reparables sin regrabar:
        assert "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE" in (
            sep["not_ready_reasons"]
        )
        assert "QUERY_FIDELITY_SUSPECT" in sep["not_ready_reasons"]


# ============================================================
# Parte 2 — Visible blockers (PRD §G)
# ============================================================


class TestRealMissionVisibleBlockers:

    def test_visible_blockers_match_real_blockers(
        self, real_mission_dict,
    ):
        """``get_visible_blockers_for_user`` devuelve los blockers
        accionables; nunca incluye ``LEGACY_RUNTIME_CONTAMINATION``
        cuando no hay contaminación de ejecución real."""
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        visible = get_visible_blockers_for_user(m)
        assert "LEGACY_RUNTIME_CONTAMINATION" not in visible
        assert "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE" in visible
        assert "QUERY_FIDELITY_SUSPECT" in visible

    def test_visible_blockers_empty_after_full_review(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        assert get_visible_blockers_for_user(m) == [], (
            "Tras confirmar todo, no debe quedar NINGÚN blocker visible."
        )


# ============================================================
# Parte 3 — Vista normal vs. experto (PRD §L)
# ============================================================


class TestRealMissionReviewViews:

    def test_normal_view_matches_prd_textual_capture_after_review(
        self, real_mission_dict,
    ):
        """PRD 2026-05-08c §M: tras confirmaciones, la vista normal
        debe verse exactamente así.
        """
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        summary = build_mission_review_summary(m)
        text = render_normal_view(summary)
        # Encabezado y estado correctos.
        assert text.startswith("Entendido"), text
        assert "Estado: Lista para ejecutar" in text
        # Vista normal NO debe mencionar términos técnicos.
        forbidden_terms = [
            "GroupControl", "use_coords", "confidence", "raw_trace",
            "/temp/", "fallback_coords", "compiled_execution_graph",
            "LEGACY_RUNTIME_CONTAMINATION", "target_signature",
        ]
        for tok in forbidden_terms:
            assert tok not in text, (
                f"vista normal contiene término técnico {tok!r}: {text!r}"
            )
        # Pasos en orden semántico canónico.
        assert "1. Abrir Chrome" in text
        assert "2. Usar el perfil Daniel Arturo Ramos" in text
        assert "3. Abrir nueva pestaña" in text
        assert "4. Abrir YouTube" in text
        assert "5. Buscar en YouTube: Devuélveme el amor de Luis Miguel" in text
        assert "6. Bajar resultados" in text

    def test_expert_view_shows_provenance_and_confirmations(
        self, real_mission_dict,
    ):
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        summary = build_mission_review_summary(m)
        text = render_expert_view(summary)
        assert "con confirmación del usuario" in text
        assert "Daniel Arturo Ramos" in text
        assert "Devuélveme el amor de Luis Miguel" in text
        assert "(antes: Devuelveme el amor Luis Miguel)" in text

    def test_normal_view_does_not_show_fix_marked_points_when_clean(
        self, real_mission_dict,
    ):
        """Tras confirmar todo, no debe haber blockers visibles, así
        que la UI nunca puede mostrar 'Arregla los puntos marcados'.
        """
        m = _build_mission_for_replay(real_mission_dict)
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        summary = build_mission_review_summary(m)
        assert summary.visible_blockers == []


# ============================================================
# Parte 4 — Defensa: live JSON nunca afirma READY engañoso
# ============================================================


class TestRealMissionLiveJsonHonesty:

    def test_live_json_never_misleadingly_ready(self):
        """El JSON live (var/missions/<uuid>.json) no puede afirmar
        READY sin tener ``review_confirmations`` (PRD revisión §2).
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

    def test_live_json_does_not_contain_stale_legacy_contamination(self):
        """Tras la limpieza del PRD 2026-05-08c, el JSON live ya no
        debe persistir ``LEGACY_RUNTIME_CONTAMINATION`` falso.
        """
        if not LIVE_PATH.exists():
            pytest.skip("no hay misión live")
        data = json.loads(LIVE_PATH.read_text(encoding="utf-8"))
        sep = data.get("semantic_execution_plan") or {}
        reasons = sep.get("not_ready_reasons") or []
        assert "LEGACY_RUNTIME_CONTAMINATION" not in reasons, (
            f"El JSON live aún persiste LEGACY_RUNTIME_CONTAMINATION; "
            f"corre scripts/build_real_mission_fixtures.py para limpiarlo. "
            f"reasons={reasons}"
        )
