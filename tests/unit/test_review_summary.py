"""
Nevlan — Tests del summary de Mission Review (PRD 2026-05-08 rev §4)
=====================================================================

La política visual:

  * Vista normal — limpia, sin tecnicismos. Solo lista de pasos +
    "Lista para ejecutar" (cuando aplique).
  * Vista experto — la misma lista + sección "Confirmado por
    usuario" + nota de trazabilidad ("READY por confirmación
    humana, no por captura perfecta").

Estos tests blindan:

  1. ``ready_provenance`` se computa correctamente desde el plan
     y las confirmaciones registradas.
  2. ``build_mission_review_summary`` produce el modelo de
     presentación esperado.
  3. ``render_normal_view`` NO menciona confirmaciones.
  4. ``render_expert_view`` SÍ las muestra con el origen de cada
     dato y la trazabilidad.
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
    confirm_profile, confirm_query,
)
from app.services.missions.mission_review_summary import (
    MissionReviewSummary,
    build_mission_review_summary,
    get_visible_blockers_for_user,
    render_expert_view,
    render_normal_view,
)
from app.services.missions.semantic_execution_plan import (
    READY_PROVENANCE_RAW,
    READY_PROVENANCE_UNKNOWN,
    READY_PROVENANCE_USER,
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_intent_status_to_plan,
    compute_ready_provenance,
)
from app.services.missions import profile_resolver

RAW_FIXTURE = Path(
    "tests/fixtures/real_missions/mission_1778221896_raw.json"
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


def _build_mission_for_replay() -> Mission:
    raw = json.loads(RAW_FIXTURE.read_text(encoding="utf-8"))
    data = copy.deepcopy(raw)
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    data["review_confirmations"] = []
    return Mission(**data)


# ──────────────────────────────────────────────────────────────────
# Provenance core
# ──────────────────────────────────────────────────────────────────


def _clean_ready_plan() -> SemanticExecutionPlan:
    """Plan mínimo que ya está READY (sin necesidad de mission)."""
    return SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="m:p00:open_app",
                type="open_app",
                params={"name": "chrome", "method": "windows_search"},
                preferred_strategy="open_app:windows_search",
                fallback_strategies=[
                    "open_app:os_startfile", "open_app:start_command",
                ],
                confidence=0.95,
                human_label="Abrir Chrome",
            ),
        ],
        intent_status="READY",
    )


class TestReadyProvenance:

    def test_unknown_when_plan_not_ready(self):
        plan = SemanticExecutionPlan(intent_status="NEEDS_REVIEW")
        assert compute_ready_provenance(plan, mission=None) == (
            READY_PROVENANCE_UNKNOWN
        )

    def test_raw_evidence_when_no_confirmations(self):
        m = Mission(name="x")
        plan = _clean_ready_plan()
        assert compute_ready_provenance(plan, mission=m) == (
            READY_PROVENANCE_RAW
        )

    def test_user_confirmation_when_review_present(self):
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        sep = m.semantic_execution_plan
        assert sep["intent_status"] == "READY"
        assert sep["ready_provenance"] == READY_PROVENANCE_USER

    def test_provenance_persists_in_to_dict(self):
        plan = _clean_ready_plan()
        plan.ready_provenance = READY_PROVENANCE_USER
        d = plan.to_dict()
        assert d["ready_provenance"] == READY_PROVENANCE_USER

    def test_provenance_round_trips_from_dict(self):
        plan = _clean_ready_plan()
        plan.ready_provenance = READY_PROVENANCE_USER
        plan2 = SemanticExecutionPlan.from_dict(plan.to_dict())
        assert plan2.ready_provenance == READY_PROVENANCE_USER

    def test_attach_intent_status_recomputes_provenance(self):
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        # Pre-confirm: NEEDS_REVIEW → unknown
        sep = m.semantic_execution_plan
        assert sep["ready_provenance"] == READY_PROVENANCE_UNKNOWN
        # Post-confirm: READY → user_confirmation
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        sep = m.semantic_execution_plan
        assert sep["ready_provenance"] == READY_PROVENANCE_USER


# ──────────────────────────────────────────────────────────────────
# Summary builder
# ──────────────────────────────────────────────────────────────────


class TestMissionReviewSummary:

    def test_summary_for_raw_mission_is_pending(self):
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        s = build_mission_review_summary(m)
        assert isinstance(s, MissionReviewSummary)
        assert s.status_ready is False
        assert "revisión" in s.status_label.lower() or s.blockers
        assert s.confirmations == []
        assert any(step.needs_review for step in s.steps)

    def test_summary_after_confirm_is_ready_with_user_provenance(self):
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        s = build_mission_review_summary(m)
        assert s.status_ready is True
        # Vista normal: SOLO "Lista para ejecutar", limpio.
        assert s.status_label == "Lista para ejecutar"
        # Vista experto: añade procedencia entre paréntesis.
        assert "confirmación" in s.status_label_expert.lower()
        assert s.provenance_key == READY_PROVENANCE_USER

    def test_normal_label_is_clean_for_all_ready_provenances(self):
        """Vista normal NO debe mostrar la procedencia en el label —
        el usuario común solo ve "Lista para ejecutar"."""
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        s = build_mission_review_summary(m)
        assert "confirmación" not in s.status_label.lower()
        assert "(" not in s.status_label
        assert s.status_label == "Lista para ejecutar"

    def test_summary_marks_steps_as_confirmed(self):
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        s = build_mission_review_summary(m)
        confirmed_indices = [step.index for step in s.steps if step.confirmed]
        # Confirmamos perfil (paso 2) y query (paso 5).
        assert 2 in confirmed_indices
        assert 5 in confirmed_indices
        # Los demás pasos no deben aparecer como confirmados.
        assert 1 not in confirmed_indices  # Abrir Chrome
        assert 3 not in confirmed_indices  # Nueva pestaña

    def test_summary_confirmations_have_human_labels(self):
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        s = build_mission_review_summary(m)
        labels = {c.field_label for c in s.confirmations}
        assert "Perfil" in labels
        assert "Búsqueda" in labels

    def test_summary_confirmations_keep_original_value(self):
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        s = build_mission_review_summary(m)
        qry = next(c for c in s.confirmations if c.field_key == "query")
        assert qry.value == "Devuélveme el amor de Luis Miguel"
        assert qry.original_value == "Devuelveme el amor Luis Miguel"


# ──────────────────────────────────────────────────────────────────
# Render normal vs experto
# ──────────────────────────────────────────────────────────────────


class TestRenderViews:

    @pytest.fixture
    def confirmed_mission(self) -> Mission:
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        return m

    def test_normal_view_lists_steps_and_state(self, confirmed_mission):
        s = build_mission_review_summary(confirmed_mission)
        out = render_normal_view(s)
        assert "Entendido" in out
        assert "Abrir Chrome" in out
        assert "Seleccionar perfil Daniel Arturo Ramos" in out
        assert "Devuélveme el amor de Luis Miguel" in out
        assert "Lista para ejecutar" in out

    def test_normal_view_does_not_mention_confirmations(
        self, confirmed_mission,
    ):
        """En vista normal el usuario común no debe enterarse de
        que hubo confirmaciones — solo le importa que está READY."""
        s = build_mission_review_summary(confirmed_mission)
        out = render_normal_view(s)
        assert "Confirmado" not in out
        assert "Trazabilidad" not in out
        assert "antes:" not in out

    def test_expert_view_shows_confirmation_panel(self, confirmed_mission):
        s = build_mission_review_summary(confirmed_mission)
        out = render_expert_view(s)
        assert "Confirmado por el usuario:" in out
        assert "Perfil:" in out
        assert "Daniel Arturo Ramos" in out
        assert "Búsqueda:" in out
        assert "Devuélveme el amor de Luis Miguel" in out

    def test_expert_view_shows_traceability_note(self, confirmed_mission):
        s = build_mission_review_summary(confirmed_mission)
        out = render_expert_view(s)
        assert "Trazabilidad:" in out
        assert "confirmación humana" in out
        assert "no por captura perfecta" in out

    def test_expert_view_shows_original_value_for_query(
        self, confirmed_mission,
    ):
        s = build_mission_review_summary(confirmed_mission)
        out = render_expert_view(s)
        assert "(antes:" in out
        assert "Devuelveme el amor Luis Miguel" in out

    def test_expert_view_marks_confirmed_steps_inline(
        self, confirmed_mission,
    ):
        s = build_mission_review_summary(confirmed_mission)
        out = render_expert_view(s)
        lines = out.splitlines()
        # El step "Usar el perfil Daniel Arturo Ramos" debe aparecer
        # con marca de confirmación en su línea.
        confirmed_lines = [
            ln for ln in lines if "[confirmado]" in ln
        ]
        assert any("Daniel Arturo Ramos" in ln for ln in confirmed_lines)
        assert any("Devuélveme el amor de Luis Miguel" in ln
                   for ln in confirmed_lines)

    def test_no_confirmations_means_no_panel_in_expert(self):
        """Si la grabación fue limpia (READY directo), el panel
        de confirmaciones no aparece — no añade ruido."""
        m = Mission(name="clean")
        plan = _clean_ready_plan()
        m.semantic_execution_plan = plan.to_dict()
        attach_intent_status_to_plan(plan, mission=m)
        m.semantic_execution_plan = plan.to_dict()
        s = build_mission_review_summary(m)
        out = render_expert_view(s)
        assert "Confirmado por el usuario:" not in out
        # La trazabilidad sí aparece, pero diciendo "sin
        # intervención humana".
        assert "sin intervención humana" in out

    def test_heuristic_acceptance_attributed_separately(self):
        """Si el usuario aceptó la sugerencia heurística, la
        confirmación debe registrarse con source distinto al de
        ``user_confirmation`` puro — eso permite analizar la
        calidad de las sugerencias."""
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        # La sugerencia heurística para esta query es exactamente
        # esta cadena.
        confirm_query(m, query="Devuelveme el amor de Luis Miguel")
        s = build_mission_review_summary(m)
        qry = next(c for c in s.confirmations if c.field_key == "query")
        assert qry.source_key == SOURCE_HEURISTIC_ACCEPTED


# ──────────────────────────────────────────────────────────────────
# Visible blockers (PRD 2026-05-08c §G)
# ──────────────────────────────────────────────────────────────────


class TestVisibleBlockers:
    """``get_visible_blockers_for_user`` es la única API que la UI
    consume para decidir si mostrar el banner de bloqueos. Contrato:

      1. NUNCA incluye blockers debug-only / técnicos.
      2. NUNCA incluye ``LEGACY_RUNTIME_CONTAMINATION`` cuando no
         hay contaminación de ejecución real.
      3. Prioriza causas materiales (:class:`semantic_ambiguity_engine`)
         por encima del rastro ``NEEDS_USER_LABEL_PENDING``.
      4. Recompute fresca: descarta ``not_ready_reasons`` stale.
    """

    def test_real_mission_visible_blockers_match_marked_steps(self):
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        visible = get_visible_blockers_for_user(m)
        # Hay perfil vacío y query suspect → blockers reales.
        assert "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE" in visible
        assert "QUERY_FIDELITY_SUSPECT" in visible
        # No debe haber LEGACY contamination espuria.
        assert "LEGACY_RUNTIME_CONTAMINATION" not in visible
        sep = m.semantic_execution_plan
        assert any(
            s.get("needs_user_label") for s in sep["steps"]
        ), "Esta misión raw debe tener al menos un step con needs_user_label"
        assert "NEEDS_USER_LABEL_PENDING" not in visible

    def test_visible_blockers_empty_after_full_review(self):
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        assert get_visible_blockers_for_user(m) == []

    def test_visible_blockers_filters_stale_legacy_contamination(self):
        """Si el JSON persistido trae LEGACY_RUNTIME_CONTAMINATION
        pero la lógica actual no detecta contaminación afectando
        ejecución, la UI NO debe mostrarlo (filtro defensivo).
        """
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        # Inyectamos un blocker stale en el JSON persistido.
        sep = dict(m.semantic_execution_plan or {})
        sep["not_ready_reasons"] = ["LEGACY_RUNTIME_CONTAMINATION"]
        sep["intent_status"] = "NEEDS_REVIEW"
        m.semantic_execution_plan = sep
        # El builder DEBE recomputar y descartar el blocker stale.
        s = build_mission_review_summary(m)
        assert "LEGACY_RUNTIME_CONTAMINATION" not in s.visible_blockers
        # Tras recompute, no debería haber contaminación afecta-ejecución.
        assert s.status_ready is True

    def test_visible_blockers_filters_needs_user_label_when_no_marked_step(
        self,
    ):
        """Si nadie tiene ``needs_user_label=True`` en steps, el
        blocker ``NEEDS_USER_LABEL_PENDING`` no se muestra (evita
        el error 'Arregla los puntos marcados…' sin puntos).
        """
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        confirm_profile(m, profile_name="Daniel Arturo Ramos")
        confirm_query(m, query="Devuélveme el amor de Luis Miguel")
        sep = dict(m.semantic_execution_plan or {})
        # Inyectamos blocker huérfano.
        sep["not_ready_reasons"] = ["NEEDS_USER_LABEL_PENDING"]
        sep["intent_status"] = "NEEDS_REVIEW"
        m.semantic_execution_plan = sep
        s = build_mission_review_summary(m)
        assert "NEEDS_USER_LABEL_PENDING" not in s.visible_blockers

    def test_visible_blockers_keeps_blocker_when_step_actually_marked(self):
        """Si un step real está marcado ``needs_user_label=True`` pero la
        ambigüedad ya proyectó códigos concretos, no hace falta duplicar
        ``NEEDS_USER_LABEL_PENDING`` en la vista."""
        m = _build_mission_for_replay()
        apply_collapse_to_mission(m)
        # Sin confirmar: hay steps con needs_user_label=True.
        s = build_mission_review_summary(m)
        assert s.visible_blockers
        assert "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE" in s.visible_blockers
