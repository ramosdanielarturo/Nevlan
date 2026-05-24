"""
Nevlan — Tests focalizados de mission_review_confirm.confirm_profile
=====================================================================

Cubren el fix PRD 2026-05-09 (canonicalización del perfil):

  * El usuario escribe "Daniel" en Mission Review.
  * Si el alias_store ya conoce "Daniel Arturo Ramos", la
    confirmación debe PROMOVER al canonical completo antes de
    persistirlo en ``semantic_execution_plan.steps[*].params.profile_name``.
  * ``review_confirmations`` debe preservar el ``original_value``
    ("Daniel") y marcar ``source = "user_confirmation_alias_promoted"``.

Estos tests NO tocan UI, NO tocan execution_lifecycle, NO tocan el
intent_collapse_engine. Solo validan el camino quirúrgico de
``confirm_profile`` + ``profile_resolver``.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import Mission, MissionStatus
from app.services.missions import profile_resolver
from app.services.missions.mission_review_confirm import (
    SOURCE_ALIAS_STORE,
    SOURCE_USER,
    SOURCE_USER_ALIAS_PROMOTED,
    confirm_profile,
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


def _build_mission_with_select_profile(
    *, profile_name_in_step: str = "",
) -> Mission:
    """Misión mínima con un único step ``select_profile``.

    Suficiente para que ``confirm_profile`` haga su trabajo sin
    arrastrar todo el pipeline del compiler / ICE.
    """
    mission = Mission(
        name="test_confirm_profile_mission",
        status=MissionStatus.NEEDS_REVIEW,
    )
    mission.semantic_execution_plan = {
        "source": "intent_collapse_engine",
        "version": 1,
        "average_confidence": 0.8,
        "coords_used": False,
        "legacy_graph_ignored": True,
        "intent_status": "NEEDS_REVIEW",
        "not_ready_reasons": ["NEEDS_USER_LABEL_PENDING"],
        "ready_provenance": "unknown",
        "steps": [
            {
                "id": "test:p00:open_app",
                "type": "open_app",
                "params": {"name": "chrome", "method": "windows_search"},
                "preferred_strategy": "open_app:windows_search",
                "fallback_strategies": [],
                "confidence": 0.9,
                "human_label": "Abrir Chrome",
                "needs_user_label": False,
                "label_prompt": "",
                "block_id": None,
            },
            {
                "id": "test:p01:select_profile",
                "type": "select_profile",
                "params": {
                    "profile_name": profile_name_in_step,
                    "app": "chrome",
                },
                "preferred_strategy": "select_profile:uia_text_match",
                "fallback_strategies": [],
                "confidence": 0.5,
                "human_label": "Seleccionar perfil",
                "needs_user_label": True,
                "label_prompt": "¿Qué perfil debo usar?",
                "block_id": None,
            },
        ],
    }
    return mission


# ──────────────────────────────────────────────────────────────────
# Camino feliz: promoción "Daniel" → "Daniel Arturo Ramos"
# ──────────────────────────────────────────────────────────────────


class TestConfirmProfilePromotesShortAliasToCanonical:
    def test_confirm_profile_promotes_daniel_to_daniel_arturo_ramos(self):
        """Cuando el alias_store ya conoce el canonical completo,
        confirmar 'Daniel' debe persistir 'Daniel Arturo Ramos' en
        el plan, no 'Daniel'.
        """
        profile_resolver.learn_profile_alias("Daniel Arturo Ramos")
        mission = _build_mission_with_select_profile()

        plan = confirm_profile(mission, profile_name="Daniel")

        sp = next(s for s in plan.steps if s.type == "select_profile")
        assert sp.params["profile_name"] == "Daniel Arturo Ramos"
        # NO se queda con "Daniel"
        assert sp.params["profile_name"] != "Daniel"
        # human_label refleja el canonical promovido
        assert "Daniel Arturo Ramos" in sp.human_label

    def test_confirm_profile_review_confirmation_preserves_original_value(self):
        """``review_confirmations`` debe registrar:
            original_value == "Daniel"
            value          == "Daniel Arturo Ramos"
            source         == "user_confirmation_alias_promoted"
        """
        profile_resolver.learn_profile_alias("Daniel Arturo Ramos")
        mission = _build_mission_with_select_profile()

        confirm_profile(mission, profile_name="Daniel")

        confs = [
            c for c in mission.review_confirmations
            if c.field == "profile_name"
        ]
        assert len(confs) == 1, confs
        c = confs[0]
        assert c.value == "Daniel Arturo Ramos"
        assert c.original_value == "Daniel"
        assert c.source == SOURCE_USER_ALIAS_PROMOTED


# ──────────────────────────────────────────────────────────────────
# Anti-degradación del alias store al confirmar
# ──────────────────────────────────────────────────────────────────


class TestConfirmProfileDoesNotDegradeAliasStore:
    def test_confirm_daniel_does_not_overwrite_full_canonical_in_store(self):
        """Tras confirmar 'Daniel', el store debe seguir mapeando
        'daniel' a 'Daniel Arturo Ramos' (no a 'Daniel').
        """
        profile_resolver.learn_profile_alias("Daniel Arturo Ramos")
        assert (
            profile_resolver.lookup_profile_by_alias("Daniel")
            == "Daniel Arturo Ramos"
        )

        mission = _build_mission_with_select_profile()
        confirm_profile(mission, profile_name="Daniel")

        # Después de confirm_profile, el store NO debe degradarse.
        assert (
            profile_resolver.lookup_profile_by_alias("Daniel")
            == "Daniel Arturo Ramos"
        )


# ──────────────────────────────────────────────────────────────────
# Camino sin alias previo: comportamiento legacy preservado
# ──────────────────────────────────────────────────────────────────


class TestConfirmProfileWithoutAliasStoreEntry:
    def test_confirm_full_name_without_promotion(self):
        """Sin entrada previa en el store, confirmar el nombre
        completo lo persiste tal cual y NO marca promovido.
        """
        mission = _build_mission_with_select_profile()
        plan = confirm_profile(mission, profile_name="Maria Lopez")
        sp = next(s for s in plan.steps if s.type == "select_profile")
        assert sp.params["profile_name"] == "Maria Lopez"
        confs = [
            c for c in mission.review_confirmations
            if c.field == "profile_name"
        ]
        assert len(confs) == 1
        assert confs[0].source == SOURCE_USER
