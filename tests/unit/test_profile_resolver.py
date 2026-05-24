"""
Nevlan — Tests del Profile Resolver (PRD 2026-05-07c §A)
=========================================================

El ``profile_resolver`` decide el ``profile_name`` real de un
``select_profile`` semántico. Su contrato:

  1. Nunca aprueba un ``profile_name`` vacío (siempre devuelve algo
     o pide confirmación mínima).
  2. Extrae canonical desde ruido humano:
     "Daniel Chrome D Daniel Arturo Ramos" → "Daniel Arturo Ramos".
  3. Persiste aliases en disco, así futuras misiones no preguntan
     dos veces.
  4. NUNCA pide regrabar — solo confirmar.

Estos tests son los que el PRD 2026-05-07c §A.8 exige, traducidos
1:1 al lenguaje del código.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.missions.profile_resolver import (
    ProfileResolution,
    extract_canonical_profile_name,
    generate_aliases,
    is_generic_profile_label,
    learn_profile_alias,
    lookup_profile_by_alias,
    reset_alias_store,
    resolve_canonical_profile_name,
    resolve_profile,
)


@pytest.fixture(autouse=True)
def _isolated_alias_store(tmp_path, monkeypatch):
    """Aísla el alias store entre tests para que la persistencia
    aprendida en un test no contamine al siguiente.
    """
    store_dir = tmp_path / "var"
    store_dir.mkdir()
    from app.core.config import settings
    monkeypatch.setattr(settings, "VAR_PATH", store_dir)
    reset_alias_store()
    yield
    reset_alias_store()


# ──────────────────────────────────────────────────────────────────
# 1. extract_canonical_profile_name
# ──────────────────────────────────────────────────────────────────

class TestExtractCanonical:
    """Casos directos del extractor — núcleo de toda la resolución."""

    def test_extracts_daniel_arturo_ramos_from_chrome_picker_label(self):
        """Caso real reportado: el UIA del item del Chrome profile
        picker viene como "Daniel Chrome D Daniel Arturo Ramos".
        Debe quedar limpio.
        """
        out = extract_canonical_profile_name(
            "Daniel Chrome D Daniel Arturo Ramos"
        )
        assert out == "Daniel Arturo Ramos"

    def test_extracts_from_chrome_window_title(self):
        out = extract_canonical_profile_name(
            "Daniel Arturo Ramos - Google Chrome"
        )
        assert out == "Daniel Arturo Ramos"

    def test_extracts_from_edge_window_title(self):
        out = extract_canonical_profile_name(
            "Maria Lopez - Microsoft Edge"
        )
        assert out == "Maria Lopez"

    def test_returns_empty_for_generic_label(self):
        for s in ("perfil", "perfil del navegador", "default", "user",
                  "PROFILE", " usuario "):
            assert extract_canonical_profile_name(s) == "", s

    def test_returns_empty_for_site_title(self):
        """``YouTube``, ``Google``, etc. no son perfiles aunque vengan
        capitalizados en el title del browser tras seleccionar perfil.
        """
        for s in ("YouTube", "Google", "YouTube - Google Chrome",
                  "Nueva pestaña - Google Chrome"):
            assert extract_canonical_profile_name(s) == "", s

    def test_accepts_single_token_real_name(self):
        """Si la observación es un único nombre propio razonable
        (``"Daniel"``), lo aceptamos — no es genérico ni de sitio.
        """
        assert extract_canonical_profile_name("Daniel") == "Daniel"

    def test_accepts_accented_names(self):
        out = extract_canonical_profile_name("María José Pérez")
        assert out == "María José Pérez"

    def test_accepts_compound_surnames_with_hyphen(self):
        out = extract_canonical_profile_name("Ana García-López")
        assert out == "Ana García-López"

    def test_strips_signin_prefix(self):
        """``"Sign in as Maria Lopez"`` debe degradar a la corrida
        más larga ("Maria Lopez"), aunque el extractor NO entiende
        "as" — solo se queda con la corrida de tokens-nombre.
        """
        out = extract_canonical_profile_name("Sign in as Maria Lopez")
        assert out == "Maria Lopez"

    def test_returns_empty_for_garbage_uia(self):
        """Targets UIA basura (GroupControl, video-stream…) NO
        producen un nombre canonical.
        """
        for s in ("GroupControl", "video-stream", "html5-main-video",
                  "guide-service", ""):
            assert extract_canonical_profile_name(s) == "", s


# ──────────────────────────────────────────────────────────────────
# 2. generate_aliases / alias store
# ──────────────────────────────────────────────────────────────────

class TestAliases:
    def test_generate_aliases_contains_expected_variants(self):
        out = generate_aliases("Daniel Arturo Ramos")
        assert "Daniel Arturo Ramos" in out  # canonical
        assert "Daniel" in out                # primer nombre
        assert "Daniel Arturo" in out         # nombre + medio
        assert "Daniel Ramos" in out          # primer + último
        assert "Arturo Ramos" in out          # suffix de 2

    def test_generate_aliases_includes_human_observation(self):
        """Si pasamos la observación cruda del flow, debe quedar
        guardada como alias adicional para que el alias store la
        reconozca en la próxima misión.
        """
        out = generate_aliases(
            "Daniel Arturo Ramos",
            observations=["Daniel Chrome D Daniel Arturo Ramos"],
        )
        assert "Daniel Chrome D Daniel Arturo Ramos" in out

    def test_learn_and_lookup_roundtrip(self):
        added = learn_profile_alias(
            "Daniel Arturo Ramos",
            aliases=["Daniel Chrome D Daniel Arturo Ramos",
                     "Daniel Chrome"],
        )
        assert added > 0
        for q in ("Daniel Arturo Ramos",
                  "daniel arturo ramos",          # case-insensitive
                  "  Daniel  Arturo  Ramos  ",    # whitespace
                  "Daniel Chrome D Daniel Arturo Ramos",
                  "Daniel"):                       # alias parcial
            got = lookup_profile_by_alias(q)
            assert got == "Daniel Arturo Ramos", (q, got)

    def test_learn_rejects_generic_canonical(self):
        """No queremos persistir aliases para "default" o "perfil del
        navegador" — eso pollute el store y degrada la calidad.
        """
        for canon in ("default", "user", "perfil del navegador",
                      "", "  "):
            assert learn_profile_alias(canon, aliases=["whatever"]) == 0
            assert lookup_profile_by_alias("whatever") is None

    def test_lookup_returns_none_for_unknown(self):
        assert lookup_profile_by_alias("Persona Que No Existe") is None


# ──────────────────────────────────────────────────────────────────
# 3. is_generic_profile_label
# ──────────────────────────────────────────────────────────────────

class TestIsGeneric:
    def test_treats_blank_as_generic(self):
        assert is_generic_profile_label("")
        assert is_generic_profile_label(None)
        assert is_generic_profile_label("   ")

    def test_treats_known_generics(self):
        for s in ("perfil del navegador", "el perfil",
                  "default", "profile", "USER", "  Perfil  "):
            assert is_generic_profile_label(s), s

    def test_rejects_real_names(self):
        for s in ("Daniel Arturo Ramos", "María", "Ana García"):
            assert not is_generic_profile_label(s), s


# ──────────────────────────────────────────────────────────────────
# 4. resolve_profile pipeline (PRD §A.2)
# ──────────────────────────────────────────────────────────────────

class TestResolveProfile:
    def test_select_profile_never_approved_with_empty_profile_name(self):
        """Sin ninguna fuente, devuelve needs_user_confirmation y
        el caller NUNCA debe aprobar profile_name vacío.
        """
        res = resolve_profile()
        assert res.needs_user_confirmation
        assert res.source in ("none", "needs_confirmation")
        # is_resolved es estricto: needs_confirmation → no resuelto.
        assert not res.is_resolved

    def test_select_profile_extracts_daniel_arturo_ramos_from_human_label(
        self,
    ):
        """El observador humano del flow trae el label completo —
        el resolver lo limpia a canonical sin pedir nada.
        """
        res = resolve_profile(
            human_observation="Daniel Chrome D Daniel Arturo Ramos",
        )
        assert res.profile_name == "Daniel Arturo Ramos"
        assert res.source == "human_observation"
        assert not res.needs_user_confirmation
        assert res.is_resolved

    def test_select_profile_uses_alias_when_uia_is_groupcontrol(self):
        """Si el UIA solo trae basura ("GroupControl") pero el alias
        store ya aprendió de un flow previo, recuperamos el canonical
        sin molestar al usuario.
        """
        learn_profile_alias(
            "Daniel Arturo Ramos",
            aliases=["Daniel Chrome D Daniel Arturo Ramos"],
        )
        res = resolve_profile(
            raw_profile="GroupControl",   # basura UIA
            human_observation="Daniel Chrome D Daniel Arturo Ramos",
        )
        # human_observation gana antes de necesitar el alias store,
        # pero igual debe resolver al canonical correcto.
        assert res.profile_name == "Daniel Arturo Ramos"
        assert res.is_resolved

    def test_select_profile_uses_alias_store_only_path(self):
        """Si nada en el evento actual da canonical pero el alias
        store sí lo aprendió, recuperamos del store.
        """
        learn_profile_alias(
            "Daniel Arturo Ramos",
            aliases=["mi pc", "perfil de daniel"],
        )
        res = resolve_profile(
            human_observation="mi pc",   # alias aprendido pero no
                                          # extraíble como canonical
        )
        assert res.profile_name == "Daniel Arturo Ramos"
        assert res.source == "alias_store"
        assert res.is_resolved

    def test_select_profile_does_not_request_rerecord_when_profile_missing(
        self,
    ):
        """El protocolo es: confirmación mínima, jamás regrabar.
        Verificamos que el resolver NO emite ningún flag de
        "regrabar"; solo needs_user_confirmation.
        """
        res = resolve_profile(
            raw_profile="GroupControl",
            ocr_observation="",
        )
        assert res.needs_user_confirmation
        # No hay un campo "needs_rerecord" — solo confirmación.
        for k in res.to_dict().keys():
            assert "rerecord" not in k.lower()
        assert "confirm" in res.confirmation_prompt.lower() or \
               "perfil" in res.confirmation_prompt.lower()

    def test_select_profile_saves_confirmed_alias_for_future_runs(self):
        """Tras una confirmación, learn_profile_alias debe persistir
        el alias para que la siguiente grabación no vuelva a preguntar.
        """
        # Primera misión: el resolver no pudo (UIA basura, sin OCR).
        first = resolve_profile(raw_profile="GroupControl")
        assert first.needs_user_confirmation

        # El usuario confirma "Daniel Arturo Ramos". Mission Review
        # llama a learn_profile_alias con el canonical confirmado +
        # los candidatos vistos en el flow.
        learn_profile_alias(
            "Daniel Arturo Ramos",
            aliases=first.candidate_aliases + ["GroupControl",
                                                "Daniel Chrome"],
        )

        # Segunda misión: con la misma evidencia "GroupControl",
        # el resolver SÍ recupera el canonical sin volver a preguntar.
        second = resolve_profile(raw_profile="GroupControl")
        assert second.is_resolved
        assert second.profile_name == "Daniel Arturo Ramos"
        assert not second.needs_user_confirmation

    def test_select_profile_skip_if_loaded_when_chrome_already_in_profile(
        self,
    ):
        """Si Chrome ya está abierto y activo en el perfil correcto,
        el resolver responde sin tocar el picker (skip_if_loaded).
        """
        res = resolve_profile(
            chrome_already_loaded=True,
            chrome_active_profile="Daniel Arturo Ramos - Google Chrome",
        )
        assert res.is_resolved
        assert res.source == "already_loaded"
        assert res.profile_name == "Daniel Arturo Ramos"

    def test_window_title_youtube_does_not_become_profile(self):
        """Bug regresión: si tras seleccionar perfil la siguiente
        ventana es "YouTube - Google Chrome", el resolver NO debe
        confundir "YouTube" con un nombre de perfil.
        """
        res = resolve_profile(
            window_title="YouTube - Google Chrome",
        )
        assert not res.is_resolved
        assert res.profile_name in ("", res.profile_name)
        # Si hay un best_candidate, debe NO ser un site name.
        assert res.profile_name.lower() not in (
            "youtube", "google", "facebook"
        )


# ──────────────────────────────────────────────────────────────────
# 5. resolve_canonical_profile_name + anti-degradación (fix 2026-05-09)
# ──────────────────────────────────────────────────────────────────

class TestResolveCanonicalProfileName:
    """Garantías del punto único de canonicalización usado por
    Mission Review. Cierra el bug del perfil "Daniel" guardado en
    lugar de "Daniel Arturo Ramos".
    """

    def test_lookup_daniel_returns_daniel_arturo_ramos_when_canonical_exists(
        self,
    ):
        """Si el alias_store ya conoce 'Daniel Arturo Ramos', pedir
        canonicalizar 'Daniel' debe promover al canonical completo.
        """
        learn_profile_alias("Daniel Arturo Ramos")
        out = resolve_canonical_profile_name("Daniel")
        assert out["canonical"] == "Daniel Arturo Ramos"
        assert out["original"] == "Daniel"
        assert out["promoted"] is True
        assert out["source"] == "alias_store"

    def test_resolve_profile_single_token_prefers_known_full_canonical(self):
        """Aunque el extractor canonicaliza 'Daniel' → 'Daniel', si
        el store conoce un canonical más rico que contenga ese
        token, lo promovemos.
        """
        learn_profile_alias("Daniel Arturo Ramos")
        out = resolve_canonical_profile_name("Daniel")
        assert out["canonical"] == "Daniel Arturo Ramos"
        assert out["promoted"] is True

    def test_profile_resolver_rejects_generic_profile_labels(self):
        """Etiquetas como 'Seleccionar perfil', 'Perfil', 'profile',
        'Chrome', 'Google Chrome', 'GroupControl' NUNCA deben quedar
        como canonical final.
        """
        for s in (
            "Seleccionar perfil",
            "Perfil",
            "profile",
            "default",
            "Chrome",
            "Google Chrome",
            "GroupControl",
            "ButtonControl",
        ):
            out = resolve_canonical_profile_name(s)
            assert out["canonical"] == "", (s, out)
            assert out["source"] == "rejected", (s, out)

    def test_resolve_canonical_no_alias_returns_extracted_unchanged(self):
        """Sin alias store y con un nombre completo legítimo, el
        canonical es el mismo que entró (no promueve, no degrada).
        """
        out = resolve_canonical_profile_name("Maria Lopez")
        assert out["canonical"] == "Maria Lopez"
        assert out["promoted"] is False


class TestLearnAliasAntiDegradation:
    """Regla anti-degradación: nunca sobrescribir un canonical más
    completo con uno más corto que sea subconjunto suyo.
    """

    def test_learn_profile_alias_does_not_degrade_full_canonical_to_daniel(
        self,
    ):
        """Bug exacto reportado en mission_1778307670:
        si el store ya tenía 'daniel' → 'Daniel Arturo Ramos',
        llamar learn_profile_alias('Daniel') NO debe dejarlo en
        'daniel' → 'Daniel'.
        """
        learn_profile_alias("Daniel Arturo Ramos")
        assert lookup_profile_by_alias("Daniel") == "Daniel Arturo Ramos"

        # Llamada peligrosa: aprender el alias corto.
        learn_profile_alias("Daniel")

        # El canonical aprendido NO debe degradarse.
        assert lookup_profile_by_alias("Daniel") == "Daniel Arturo Ramos"
        assert lookup_profile_by_alias("daniel") == "Daniel Arturo Ramos"

    def test_learn_alias_overwrites_with_more_specific_canonical(self):
        """Camino feliz: el upgrade SÍ debe ocurrir."""
        learn_profile_alias("Daniel")
        assert lookup_profile_by_alias("Daniel") == "Daniel"

        learn_profile_alias("Daniel Arturo Ramos")
        assert lookup_profile_by_alias("Daniel") == "Daniel Arturo Ramos"
