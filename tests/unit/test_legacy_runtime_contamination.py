"""
Nevlan — LEGACY_RUNTIME_CONTAMINATION (PRD 2026-05-08c §A)
============================================================

Estos tests blindan la separación entre:

  * "El runtime LEGACY participa en ejecución/decisión" → contamina.
  * "Hay residuo legacy presente pero solo como evidencia/debug" → NO contamina.

Cada test es la respuesta concreta a un caso del PRD §A.5.
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
    confirm_profile, confirm_query,
)
from app.services.missions.mission_review_summary import (
    build_mission_review_summary,
    get_visible_blockers_for_user,
)
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    compute_ready_status,
)
from app.services.missions import profile_resolver
from app.services.missions.approval_gate import check_mission_approvable


FIXTURES_DIR = Path("tests/fixtures/real_missions")
RAW_FIXTURE = FIXTURES_DIR / "mission_1778244908_raw.json"


@pytest.fixture(autouse=True)
def _isolate_alias_store(tmp_path, monkeypatch):
    fake = tmp_path / "profile_aliases.json"
    monkeypatch.setattr(
        profile_resolver, "_alias_store_path", lambda: fake,
    )
    profile_resolver.reset_alias_store()
    yield
    profile_resolver.reset_alias_store()


def _real_mission() -> Mission:
    raw = json.loads(RAW_FIXTURE.read_text(encoding="utf-8"))
    data = copy.deepcopy(raw)
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    data["review_confirmations"] = []
    m = Mission(**data)
    apply_collapse_to_mission(m)
    return m


def _confirmed_mission() -> Mission:
    m = _real_mission()
    confirm_profile(m, profile_name="Daniel Arturo Ramos")
    confirm_query(m, query="Devu\u00e9lveme el amor de Luis Miguel")
    return m


def _detail_kinds(mission: Mission) -> list:
    return [
        d.get("kind")
        for d in (getattr(mission, "_contamination_details", None) or [])
    ]


def _affecting_kinds(mission: Mission) -> list:
    return [
        d.get("kind")
        for d in (getattr(mission, "_contamination_details", None) or [])
        if d.get("affects_execution")
    ]


# ════════════════════════════════════════════════════════════════════
# §A.5 — casos que NO deben disparar LEGACY_RUNTIME_CONTAMINATION
# ════════════════════════════════════════════════════════════════════


class TestNonContaminatingCases:
    """Cosas que la evidencia puede arrastrar pero NO afectan ejecución
    porque la fuente única de verdad es el ``semantic_execution_plan``.
    """

    def test_dirty_compiled_graph_debug_only_does_not_trigger_legacy_runtime_contamination(
        self,
    ):
        """Aunque el ``compiled_execution_graph`` traiga steps legacy,
        si está marcado ``legacy_debug_only`` NO contamina READY."""
        m = _confirmed_mission()
        # Aseguramos que SÍ hay grafo legacy (debe haberlo, capturado).
        # Si por algún motivo el compilador no lo emitió, simulamos uno.
        if not m.compiled_execution_graph:
            from app.contracts.mission import (
                ActionStrategy, CompiledStep, ValidationStrategy,
                TargetContext,
            )
            cs = CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                validation_strategy=ValidationStrategy.NONE,
                target_context=TargetContext(),
                action_payload={"x": 100, "y": 200},
            )
            m.compiled_execution_graph = [cs]
        m.legacy_compiled_graph_purpose = "legacy_debug_only"

        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert "LEGACY_RUNTIME_CONTAMINATION" not in blockers
        # Sí queda registrada como debug-only.
        assert "compiled_graph_debug_only" in _detail_kinds(m)

    def test_raw_trace_coords_do_not_trigger_legacy_runtime_contamination(
        self,
    ):
        """``raw_trace`` con clicks/coords/scrolls es EVIDENCIA forense.
        El runtime jamás la consume — no contamina."""
        m = _confirmed_mission()
        # El raw_trace de la misión real ya tiene mouse_clicks con
        # x/y; verificamos que el plan sigue READY.
        def _evtype(ev):
            t = getattr(ev, "event_type", None)
            return getattr(t, "value", None) or str(t or "")
        assert m.raw_trace and any(
            _evtype(ev) == "mouse_click" for ev in m.raw_trace
        )
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert "LEGACY_RUNTIME_CONTAMINATION" not in blockers
        assert status == "READY"

    def test_target_signature_coords_do_not_trigger_legacy_runtime_contamination(
        self,
    ):
        """Las ``target_signature.coordinates`` son evidencia visual
        capturada, no decisión activa de ejecución."""
        m = _confirmed_mission()
        # raw_trace en la misión real trae target_signature con
        # coordinates en cada metadata — está OK.
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert "LEGACY_RUNTIME_CONTAMINATION" not in blockers


# ════════════════════════════════════════════════════════════════════
# §A.5 — casos que SÍ deben disparar LEGACY_RUNTIME_CONTAMINATION
# ════════════════════════════════════════════════════════════════════


class TestContaminatingCases:
    """Estos sí afectan ejecución y, por tanto, SÍ bloquean READY."""

    def test_run_legacy_player_called_triggers_legacy_runtime_contamination(
        self,
    ):
        """Si en algún momento ``run_legacy_player`` corrió, contamina.
        Lo emulamos seteando la flag que el bridge debería escribir."""
        m = _confirmed_mission()
        m._run_legacy_player_called = True
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert "LEGACY_RUNTIME_CONTAMINATION" in blockers
        assert "run_legacy_player_called" in _affecting_kinds(m)
        assert status == "NEEDS_REVIEW"

    def test_decision_source_compiled_graph_triggers_legacy_runtime_contamination(
        self,
    ):
        """Una decisión con ``source == "compiled_execution_graph"``
        es la prueba más directa: el bridge resolvió usando el grafo
        legacy en vez del semantic plan."""
        m = _confirmed_mission()
        m._last_decision = {
            "source": "compiled_execution_graph",
            "step_id": "x",
        }
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        _, blockers = compute_ready_status(plan, mission=m)
        assert "LEGACY_RUNTIME_CONTAMINATION" in blockers
        assert "decision_source_legacy" in _affecting_kinds(m)

    def test_ghost_simulating_legacy_graph_triggers_legacy_runtime_contamination(
        self,
    ):
        """Ghost Simulation debe simular el plan semántico, jamás el
        grafo legacy."""
        m = _confirmed_mission()
        m._ghost_simulation_meta = {
            "simulating": "compiled_execution_graph",
        }
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        _, blockers = compute_ready_status(plan, mission=m)
        assert "LEGACY_RUNTIME_CONTAMINATION" in blockers
        assert "ghost_simulating_legacy" in _affecting_kinds(m)

    def test_compiled_graph_executable_purpose_triggers_legacy_runtime_contamination(
        self,
    ):
        """Si alguien marca ``legacy_compiled_graph_purpose`` como
        ejecutable y hay grafo, hay riesgo de doble ejecución."""
        m = _confirmed_mission()
        # Aseguramos un grafo legacy presente.
        if not m.compiled_execution_graph:
            from app.contracts.mission import (
                ActionStrategy, CompiledStep, ValidationStrategy,
                TargetContext,
            )
            cs = CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                validation_strategy=ValidationStrategy.NONE,
                target_context=TargetContext(),
                action_payload={"x": 1, "y": 1},
            )
            m.compiled_execution_graph = [cs]
        m.legacy_compiled_graph_purpose = "execution"
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        _, blockers = compute_ready_status(plan, mission=m)
        assert "LEGACY_RUNTIME_CONTAMINATION" in blockers

    def test_coords_used_true_triggers_legacy_runtime_contamination(self):
        m = _confirmed_mission()
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        plan.coords_used = True
        _, blockers = compute_ready_status(plan, mission=m)
        assert "LEGACY_RUNTIME_CONTAMINATION" in blockers


# ════════════════════════════════════════════════════════════════════
# §A.4 — trazabilidad estructurada
# ════════════════════════════════════════════════════════════════════


class TestContaminationDetailsStructure:
    """Cada entrada de contamination_details debe traer el contrato:
    source_module, field_path, contaminating_value, reason,
    affects_execution."""

    def test_debug_only_entry_has_complete_metadata(self):
        m = _confirmed_mission()
        # Forzamos la presencia del detail debug-only inyectando un
        # grafo legacy y marcando debug-only.
        from app.contracts.mission import (
            ActionStrategy, CompiledStep, ValidationStrategy,
            TargetContext,
        )
        m.compiled_execution_graph = [
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                validation_strategy=ValidationStrategy.NONE,
                target_context=TargetContext(),
                action_payload={"x": 1, "y": 1},
            )
        ]
        m.legacy_compiled_graph_purpose = "legacy_debug_only"
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        compute_ready_status(plan, mission=m)
        details = getattr(m, "_contamination_details", None) or []
        target = next(
            (d for d in details if d["kind"] == "compiled_graph_debug_only"),
            None,
        )
        assert target is not None
        for k in (
            "source_module", "field_path", "contaminating_value",
            "reason", "affects_execution",
        ):
            assert k in target, f"falta {k} en detail debug-only"
        assert target["affects_execution"] is False

    def test_affecting_entry_has_complete_metadata(self):
        m = _confirmed_mission()
        m._run_legacy_player_called = True
        plan = SemanticExecutionPlan.from_dict(m.semantic_execution_plan)
        compute_ready_status(plan, mission=m)
        details = getattr(m, "_contamination_details", None) or []
        target = next(
            (d for d in details if d["kind"] == "run_legacy_player_called"),
            None,
        )
        assert target is not None
        for k in (
            "source_module", "field_path", "contaminating_value",
            "reason", "affects_execution",
        ):
            assert k in target
        assert target["affects_execution"] is True


# ════════════════════════════════════════════════════════════════════
# §A.5 — Approval Gate convive con grafo legacy debug-only
# ════════════════════════════════════════════════════════════════════


class TestApprovalGateIgnoresLegacyDebugWhenSemanticPlanExists:
    def test_approval_gate_ignores_legacy_debug_graph_when_semantic_plan_exists(
        self,
    ):
        """Aunque haya un compiled_execution_graph "sucio", si existe
        plan semántico válido y el grafo está marcado debug-only,
        el Approval Gate NO debe emitir contamination."""
        m = _confirmed_mission()
        from app.contracts.mission import (
            ActionStrategy, CompiledStep, ValidationStrategy,
            TargetContext,
        )
        m.compiled_execution_graph = [
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                validation_strategy=ValidationStrategy.NONE,
                target_context=TargetContext(),
                action_payload={"x": 100, "y": 200},
            )
        ]
        m.legacy_compiled_graph_purpose = "legacy_debug_only"
        report = check_mission_approvable(m)
        codes = {v.code for v in report.violations}
        assert "LEGACY_RUNTIME_CONTAMINATION" not in codes


# ════════════════════════════════════════════════════════════════════
# §M — caso real mission_1778244908 sin falsa contaminación
# ════════════════════════════════════════════════════════════════════


class TestRealMission1778244908:
    def test_real_mission_1778244908_no_false_legacy_contamination_after_confirmations(
        self,
    ):
        """La misión real debe llegar a READY cero blockers tras
        confirmar perfil + query."""
        m = _confirmed_mission()
        sep = m.semantic_execution_plan
        assert sep["intent_status"] == "READY"
        assert sep["not_ready_reasons"] == []
        assert sep["coords_used"] is False
        assert sep["legacy_graph_ignored"] is True
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"
        # Ningún detalle de contaminación afecta ejecución.
        affecting = _affecting_kinds(m)
        assert affecting == [], (
            f"Detalles afectan ejecución inesperadamente: {affecting}"
        )
