"""
ArthurOS Unit Tests — Mission Pipeline
---------------------------------------
Verifica la integridad del pipeline: contrato → compilación → store → edición.
"""
import json
import tempfile
import shutil
from pathlib import Path

import pytest

from app.contracts.mission import (
    Mission, RawEvent, EventType, MouseAction, KeyboardAction,
    MissionStatus, CompiledStep, InterpretedStep, MissionExecution,
    ReplayStatus, Annotation, AnnotationType,
)


# ============================================================================
# Helpers
# ============================================================================

def _make_raw_click(x=100, y=200, button="left", uia_name="Botón OK"):
    """Crea un RawEvent de mouse click con UIA metadata."""
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=x, y=y, button=button),
        metadata={"uia": {"name": uia_name, "control_type": "ButtonControl", "automation_id": "btn_ok", "class_name": "Button"}} if uia_name else {},
    )

def _make_raw_keypress(key="a"):
    """Crea un RawEvent de tecla."""
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=[key]),
    )

def _make_mission_with_events():
    """Crea una misión con eventos de prueba."""
    m = Mission(name="test_mission")
    m.raw_trace = [
        _make_raw_click(100, 200, "left", "Botón OK"),
        _make_raw_keypress("H"),
        _make_raw_keypress("o"),
        _make_raw_keypress("l"),
        _make_raw_keypress("a"),
        _make_raw_keypress("Key.enter"),
        _make_raw_click(300, 400, "left", "Guardar"),
    ]
    return m


# ============================================================================
# Test 1: Compilación produce execution graph no vacío
# ============================================================================

class TestCompiler:
    def test_compile_produces_compiled_steps(self):
        from app.services.missions.compiler import MissionCompiler
        m = _make_mission_with_events()
        result = MissionCompiler.compile_mission(m)

        assert len(result.compiled_execution_graph) > 0, "compiled_execution_graph debe tener pasos"
        assert len(result.interpreted_steps) > 0, "interpreted_steps debe tener pasos"
        # PRD 2026-05-10d (Mission Truth Gate §3 + §5): tras compilar,
        # el gate baja la misión a ``NEEDS_REVIEW`` si el plan
        # semántico no llegó a READY (síntesis pobre sin window
        # context ni UIA). Antes el compiler dejaba ``COMPILED`` —
        # eso era la fuga legacy. Aceptamos cualquier estado no
        # ejecutable: ``COMPILED`` (sin handoff), ``NEEDS_REVIEW``
        # (gate bajó), o ``COMPILED_DRAFT`` (residuos).
        assert result.status in {
            MissionStatus.COMPILED,
            MissionStatus.NEEDS_REVIEW,
            MissionStatus.COMPILED_DRAFT,
        }
        # Y nunca ejecutable de golpe sin pasar por el gate completo.
        assert result.status not in {
            MissionStatus.EXECUTABLE,
            MissionStatus.APPROVED,
        }

    def test_compile_groups_keyboard_events(self):
        from app.services.missions.compiler import MissionCompiler
        m = _make_mission_with_events()
        result = MissionCompiler.compile_mission(m)

        # Antes (PRD 2026-05-03b): el compiler emitía 4 steps:
        #   click, type_text("Hola"), send_hotkey(enter), click
        # Tras el ``search_finalizer`` (PRD 2026-05-03c §3) text+enter
        # se fusiona OBLIGATORIAMENTE en un único step con
        # ``submit_after=True``. Resultado:
        #   click, type_text("Hola", submit_after=True), click  → 3
        steps = result.compiled_execution_graph
        assert len(steps) == 3, \
            f"Expected 3 steps tras search_finalizer, got {len(steps)}"
        # El step central debe ser el TYPE_TEXT con submit_after=True.
        from app.contracts.mission import ActionStrategy
        mid = steps[1]
        assert mid.action_strategy in (
            ActionStrategy.TYPE_TEXT, ActionStrategy.SET_FIELD_VALUE,
        ), f"middle step is {mid.action_strategy}"
        assert (mid.action_payload or {}).get("submit_after") is True, \
            "submit_after should be True after search_finalizer fused Enter"

    def test_compile_links_execution_graph(self):
        from app.services.missions.compiler import MissionCompiler
        m = _make_mission_with_events()
        result = MissionCompiler.compile_mission(m)
        
        steps = result.compiled_execution_graph
        for i in range(len(steps) - 1):
            assert steps[i].next_step_id_on_success == steps[i+1].id


# ============================================================================
# Test 2: Serialización roundtrip
# ============================================================================

class TestSerialization:
    def test_mission_roundtrip(self):
        from app.contracts.mission import mission_to_dict, mission_from_dict
        from app.services.missions.compiler import MissionCompiler
        
        m = _make_mission_with_events()
        m = MissionCompiler.compile_mission(m)
        
        data = mission_to_dict(m)
        json_str = json.dumps(data, default=str, ensure_ascii=False)
        loaded_data = json.loads(json_str)
        m2 = mission_from_dict(loaded_data)
        
        assert m2.name == m.name
        assert len(m2.compiled_execution_graph) == len(m.compiled_execution_graph)
        assert len(m2.interpreted_steps) == len(m.interpreted_steps)
        assert len(m2.raw_trace) == len(m.raw_trace)
        assert m2.status == m.status

    def test_mission_with_aliases_roundtrip(self):
        from app.contracts.mission import mission_to_dict, mission_from_dict
        m = Mission(name="test", aliases=["alias1", "alias2"])
        data = mission_to_dict(m)
        m2 = mission_from_dict(data)
        assert m2.aliases == ["alias1", "alias2"]

    def test_execution_has_verification_summary(self):
        e = MissionExecution(mission_id="test", mission_version=1)
        assert hasattr(e, 'verification_summary')
        assert e.verification_summary == ""


# ============================================================================
# Test 3: Store CRUD
# ============================================================================

class TestStore:
    @pytest.fixture
    def temp_store(self):
        from app.services.missions.store import MissionStore
        tmp = Path(tempfile.mkdtemp())
        store = MissionStore(base_path=tmp)
        yield store
        shutil.rmtree(tmp, ignore_errors=True)

    def test_save_and_load(self, temp_store):
        m = Mission(name="Store Test")
        temp_store.save(m)
        loaded = temp_store.load(m.id)
        assert loaded is not None
        assert loaded.name == "Store Test"

    def test_find_by_name_case_insensitive(self, temp_store):
        m = Mission(name="Prueba 15")
        temp_store.save(m)
        
        found = temp_store.find_by_name("prueba 15")
        assert found is not None
        assert found.id == m.id

    def test_delete(self, temp_store):
        m = Mission(name="To Delete")
        temp_store.save(m)
        result = temp_store.delete(m.id)
        assert result is True
        assert temp_store.load(m.id) is None

    def test_edit_mission_step(self, temp_store):
        from app.services.missions.compiler import MissionCompiler
        m = _make_mission_with_events()
        m = MissionCompiler.compile_mission(m)
        temp_store.save(m)
        
        step_id = m.compiled_execution_graph[0].id
        result = temp_store.edit_mission_step(m.id, step_id, {"goal": "Updated Goal"})
        assert result is True
        
        loaded = temp_store.load(m.id)
        assert loaded.compiled_execution_graph[0].goal == "Updated Goal"
        assert loaded.version == m.version + 1

    def test_get_stats_no_crash(self, temp_store):
        m = _make_mission_with_events()
        from app.services.missions.compiler import MissionCompiler
        m = MissionCompiler.compile_mission(m)
        temp_store.save(m)
        
        stats = temp_store.get_stats()
        assert stats["total_missions"] == 1
        assert "total_compiled_steps" in stats
        assert stats["total_compiled_steps"] > 0


# ============================================================================
# Test 4: No ghost fields
# ============================================================================

class TestNoGhostFields:
    def test_mission_has_user_annotations_not_annotations(self):
        m = Mission(name="test")
        assert hasattr(m, 'user_annotations')
        # 'annotations' should not be an independent attribute
        # (with extra=ignore it won't crash, but we verify the canonical name)
        assert 'user_annotations' in Mission.model_fields

    def test_mission_has_aliases(self):
        m = Mission(name="test")
        assert hasattr(m, 'aliases')
        assert m.aliases == []

    def test_compiled_step_no_tags(self):
        """CompiledStep should not have a 'tags' field."""
        c = CompiledStep()
        assert 'tags' not in CompiledStep.model_fields

    def test_mission_execution_has_verification_summary(self):
        e = MissionExecution(mission_id="x", mission_version=1)
        assert hasattr(e, 'verification_summary')
