"""
Nevlan — Mission Truth Gate (PRD 2026-05-10d)
==============================================

Tests quirúrgicos (máx. 8) del gate único que decide:

  * si una misión es ejecutable,
  * qué pasos muestra la UI,
  * de dónde viene la fuente runtime,
  * cuándo bajar ``mission.status`` a ``NEEDS_REVIEW``.

El gate NO crea detectores nuevos: consolida lo que ya existe
(``semantic_execution_plan``, ``approval_gate``, recorder truth
layer, dropped semantic events). Estos tests verifican el
comportamiento agregado, no las piezas internas.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    Mission,
    MissionStatus,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions import profile_resolver
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.mission_truth_gate import (
    SOURCE_NONE,
    SOURCE_SEMANTIC_PLAN,
    TruthGateCode,
    gate_decision,
    is_executable,
    reconcile_mission_status,
    steps_for_ui,
)
from app.services.missions.semantic_execution_plan import (
    ReadyBlockerCode,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MISSION_PATH = (
    REPO_ROOT / "var" / "missions"
    / "cd87af59-6a2a-42c4-92a1-94dd2c03fc56.json"
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


def _load_real_mission_1778451248() -> Mission:
    if not REAL_MISSION_PATH.exists():
        pytest.skip(f"Misión real no disponible: {REAL_MISSION_PATH}")
    data = json.loads(REAL_MISSION_PATH.read_text(encoding="utf-8"))
    return Mission.model_validate(copy.deepcopy(data))


def _make_synthetic_executable_mission() -> Mission:
    """Misión mínima con un SEP READY (camino feliz, sin contaminación)."""
    return Mission.model_validate({
        "id": "syn-1",
        "name": "synthetic",
        "raw_trace": [],
        "compiled_execution_graph": [],
        "interpreted_steps": [],
        "action_groups": [],
        "status": "compiled",
        "semantic_execution_plan": {
            "source": "intent_collapse_engine",
            "version": 1,
            "average_confidence": 0.95,
            "coords_used": False,
            "legacy_graph_ignored": True,
            "intent_status": "READY",
            "not_ready_reasons": [],
            "ready_provenance": "raw_evidence",
            "steps": [
                {
                    "id": "syn-1:p00:open_app",
                    "type": "open_app",
                    "params": {"name": "chrome", "method": "windows_search"},
                    "preferred_strategy": "open_app:windows_search",
                    "fallback_strategies": ["open_app:os_startfile"],
                    "confidence": 0.95,
                    "human_label": "Abrir Chrome",
                    "needs_user_label": False,
                    "label_prompt": "",
                    "block_id": None,
                },
            ],
        },
        "legacy_compiled_graph_purpose": "legacy_debug_only",
    })


# ──────────────────────────────────────────────────────────────────────
# 1. mission_1778451248 NO ejecutable con perfil GroupControl legacy
# ──────────────────────────────────────────────────────────────────────


def test_mission_1778451248_not_executable_with_legacy_groupcontrol_profile() -> None:
    """La misión real tiene SEP=NEEDS_REVIEW pero quedó persistida con
    ``status="compiled"``. El gate debe corregir eso y dejarla
    explícitamente NO ejecutable.
    """
    m = _load_real_mission_1778451248()
    # Estado persistido en disco — la fuga.
    assert m.status in (
        MissionStatus.COMPILED, MissionStatus.EXECUTABLE,
        MissionStatus.APPROVED,
    )
    new_status = reconcile_mission_status(m)
    assert new_status == MissionStatus.NEEDS_REVIEW.value
    assert m.status == MissionStatus.NEEDS_REVIEW
    assert is_executable(m) is False
    decision = gate_decision(m)
    assert TruthGateCode.PLAN_NOT_READY in decision.blockers
    # Y los pasos para la UI son los del plan semántico, NO los
    # legacy contaminados con use_coords + GroupControl.
    ui_steps = steps_for_ui(m)
    assert len(ui_steps) == 6
    assert all("preferred_strategy" in s for s in ui_steps)
    # Ningún preferred_strategy con "use_coords".
    assert all(
        "use_coords" not in str(s.get("preferred_strategy") or "")
        for s in ui_steps
    )


# ──────────────────────────────────────────────────────────────────────
# 2. compiled_execution_graph queda como debug, nunca runtime
# ──────────────────────────────────────────────────────────────────────


def test_compiled_execution_graph_debug_only_never_runtime_source() -> None:
    """Tras pasar por el gate, ``legacy_compiled_graph_purpose`` queda
    en ``"legacy_debug_only"`` cuando coexiste con un SEP. La fuente
    runtime que el gate publica es ``semantic_execution_plan``.
    """
    m = _load_real_mission_1778451248()
    reconcile_mission_status(m)
    assert m.legacy_compiled_graph_purpose == "legacy_debug_only"
    decision = gate_decision(m)
    assert decision.source == SOURCE_SEMANTIC_PLAN


# ──────────────────────────────────────────────────────────────────────
# 3. UI consume SEP, no el grafo compilado / interpreted_steps
# ──────────────────────────────────────────────────────────────────────


def test_ui_steps_come_from_semantic_plan_not_compiled_graph() -> None:
    """``steps_for_ui`` retorna los steps del SEP, NUNCA los del
    ``compiled_execution_graph``. Aunque el grafo legacy tenga 8
    pasos sucios, la UI se basa en los 6 del plan semántico.
    """
    m = _load_real_mission_1778451248()
    compiled_count = len(m.compiled_execution_graph or [])
    assert compiled_count > 0, "fixture inesperado (sin compiled_graph)"
    sep_count = len((m.semantic_execution_plan or {}).get("steps") or [])
    ui = steps_for_ui(m)
    assert len(ui) == sep_count
    assert len(ui) != compiled_count
    # Los ids vienen del SEP (formato "<mission_id>:pNN:<type>").
    for s in ui:
        assert ":p" in str(s.get("id") or "")


# ──────────────────────────────────────────────────────────────────────
# 4. Executor NO usa compiled_graph cuando SEP es inválido
# ──────────────────────────────────────────────────────────────────────


def test_executor_refuses_compiled_graph_when_semantic_plan_invalid() -> None:
    """Si SEP existe pero está en NEEDS_REVIEW (caso real), el gate
    decide NO ejecutar. Reusamos la misma señal que ``run_mission_smart``
    consume vía ``check_mission_approvable`` / ``classify_status``:
    ``is_executable(mission) == False`` es contractual.
    """
    m = _load_real_mission_1778451248()
    reconcile_mission_status(m)
    assert is_executable(m) is False
    # Y el approval_gate debe coincidir — coherencia entre ambos
    # gates evita que un caller bypase el truth gate.
    from app.services.missions.approval_gate import (
        is_executable as approval_is_executable,
    )
    assert approval_is_executable(m) is False


# ──────────────────────────────────────────────────────────────────────
# 5. use_coords en compiled_graph NO desbloquea ejecución
# ──────────────────────────────────────────────────────────────────────


def test_use_coords_in_compiled_graph_does_not_make_mission_executable() -> None:
    """La misión real tiene múltiples ``fallback_strategy="use_coords"``
    en ``compiled_execution_graph``. Eso NO debe traducirse en
    ejecución posible — el gate ignora el grafo legacy y solo
    pregunta al SEP.
    """
    m = _load_real_mission_1778451248()
    # Confirmamos que el compiled_graph tiene la basura legacy.
    has_use_coords_fallback = any(
        getattr(cs, "fallback_strategy", "") == "use_coords"
        or (isinstance(cs, dict)
            and cs.get("fallback_strategy") == "use_coords")
        for cs in (m.compiled_execution_graph or [])
    )
    assert has_use_coords_fallback, "fixture inesperado (sin use_coords)"
    reconcile_mission_status(m)
    assert is_executable(m) is False


# ──────────────────────────────────────────────────────────────────────
# 6. Truth Layer weak/insufficient ⇒ no high-confidence step
# ──────────────────────────────────────────────────────────────────────


def test_truth_layer_weak_event_cannot_be_high_confidence_step() -> None:
    """Construimos un raw_trace donde el único click del profile
    picker tiene UIA débil (GroupControl gigante). El Recorder
    Truth Layer (vía ``intent_collapse_engine``) lo bajará a
    ``select_profile`` con ``needs_user_label=True``. El gate
    debe respetar ese veredicto: NO ejecutable.
    """
    raw_trace = [
        RawEvent(
            event_type=EventType.WINDOW_ACTIVATE,
            timestamp=datetime.now(timezone.utc),
            window_context=WindowContext(
                hwnd=10, title="Google Chrome", process_name="chrome.exe",
            ),
        ),
        RawEvent(
            event_type=EventType.MOUSE_CLICK,
            timestamp=datetime.now(timezone.utc),
            offset_ms=10,
            mouse_action=MouseAction(x=100, y=100, button="left"),
            window_context=WindowContext(
                hwnd=10, title="Google Chrome", process_name="chrome.exe",
            ),
            metadata={
                "uia": {
                    "name": "",
                    "control_type": "GroupControl",
                    "automation_id": "",
                    "bbox": {"left": 0, "top": 0, "width": 1500, "height": 800},
                },
                "anchor_bbox": {"left": 0, "top": 0, "width": 1500, "height": 800},
            },
        ),
    ]
    m = Mission(name="weak_truth", raw_trace=raw_trace)
    apply_collapse_to_mission(m)
    reconcile_mission_status(m)
    sep = m.semantic_execution_plan or {}
    # El SEP debe quedar en NEEDS_REVIEW por evidencia débil.
    assert sep.get("intent_status") == "NEEDS_REVIEW"
    assert is_executable(m) is False
    # Y existe al menos un step con needs_user_label=True (no se
    # promovió a alta confianza).
    needs = [
        s for s in (sep.get("steps") or [])
        if s.get("needs_user_label") is True
    ]
    assert needs, sep.get("steps")


# ──────────────────────────────────────────────────────────────────────
# 7. SEP inválido ⇒ mission.status = NEEDS_REVIEW
# ──────────────────────────────────────────────────────────────────────


def test_mission_status_needs_review_when_semantic_plan_invalid() -> None:
    """Si el plan está vacío o ausente, el gate baja la misión a
    NEEDS_REVIEW (no la deja como COMPILED ejecutable).
    """
    m = Mission.model_validate({
        "id": "syn-empty",
        "name": "no_plan",
        "raw_trace": [],
        "compiled_execution_graph": [],
        "interpreted_steps": [],
        "action_groups": [],
        "status": "compiled",
        "semantic_execution_plan": None,
    })
    new_status = reconcile_mission_status(m)
    assert new_status == MissionStatus.NEEDS_REVIEW.value
    assert m.status == MissionStatus.NEEDS_REVIEW
    decision = gate_decision(m)
    assert TruthGateCode.NO_SEMANTIC_PLAN in decision.blockers
    assert decision.source == SOURCE_NONE
    assert steps_for_ui(m) == []


# ──────────────────────────────────────────────────────────────────────
# 8. Layer mismatch (raw vs SEP) bloquea ejecución
# ──────────────────────────────────────────────────────────────────────


def test_mission_truth_gate_blocks_layer_mismatch() -> None:
    """Si la misión tiene un SEP "READY" sintético pero
    ``_dropped_semantic_events`` con eventos relevantes (capa de
    verdad detectó algo que el plan no representa), el gate marca
    ``LAYER_MISMATCH`` y baja la misión a NEEDS_REVIEW. Esto cierra
    el caso "raw_trace dice una cosa, SEP dice otra".
    """
    m = _make_synthetic_executable_mission()
    # Inyectamos el dropped event como ya hace el ICE en producción.
    m._dropped_semantic_events = [{
        "event_id": "evt-dropped-1",
        "event_type": "mouse_scroll",
        "reason": "scroll relevante sin step semántico que lo cubra",
        "suggested_semantic_type": "scroll_page",
    }]
    decision = gate_decision(m)
    assert TruthGateCode.LAYER_MISMATCH in decision.blockers
    assert decision.mismatches and any(
        mm.get("code") == "DROPPED_RELEVANT_EVENTS"
        for mm in decision.mismatches
    )
    new_status = reconcile_mission_status(m)
    assert new_status == MissionStatus.NEEDS_REVIEW.value
    assert is_executable(m) is False
