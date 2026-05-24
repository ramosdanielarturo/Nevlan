"""
Nevlan — Automation Center / Mission Truth Gate UI integration
================================================================

Tests quirúrgicos (máx. 6) que validan la migración del
``automation_center.py`` para que la vista normal consuma
``mission_truth_gate.steps_for_ui`` (y sus helpers UI) en lugar de
leer directamente ``mission.compiled_execution_graph`` /
``mission.interpreted_steps``.

Estos tests:

  * Atacan el contrato del módulo (lectura de fuente) para evitar
    arrancar Qt en CI.
  * Atacan los helpers UI del gate (``step_card_models`` /
    ``ui_blockers`` / ``visible_step_count``) que son pure-Python
    y son la base de la vista normal.
  * Reusan ``mission_1778451248`` como caso real: la UI normal NO
    debe mostrar los 8 pasos legacy con GroupControl/use_coords
    como fuente principal.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.contracts.mission import Mission, MissionStatus
from app.services.missions import profile_resolver
from app.services.missions.mission_truth_gate import (
    SOURCE_COMPILED_GRAPH_DEBUG,
    SOURCE_SEMANTIC_PLAN,
    has_semantic_view,
    reconcile_mission_status,
    step_card_models,
    ui_blockers,
    visible_step_count,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_CENTER_PATH = (
    REPO_ROOT / "app" / "interfaces" / "desktop" / "automation_center.py"
)
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


def _src() -> str:
    return AUTO_CENTER_PATH.read_text(encoding="utf-8")


def _load_real_mission_1778451248() -> Mission:
    if not REAL_MISSION_PATH.exists():
        pytest.skip(f"Misión real no disponible: {REAL_MISSION_PATH}")
    data = json.loads(REAL_MISSION_PATH.read_text(encoding="utf-8"))
    return Mission.model_validate(copy.deepcopy(data))


def _mission_with_sep_ready() -> Mission:
    """SEP READY con 2 pasos limpios + compiled_graph legacy ruidoso.

    Sirve para verificar que la vista normal no mezcla el legacy.
    """
    return Mission.model_validate({
        "id": "syn-ui-1",
        "name": "synthetic ui",
        "raw_trace": [],
        "compiled_execution_graph": [
            {
                "id": "leg-1",
                "action_strategy": "click",
                "target_context": {
                    "uia_data": {"name": "GroupControl", "control_type": "Group"},
                    "fallback_strategy": "use_coords",
                    "fallback_coords": {"x": 100, "y": 200},
                    "confidence": {"capture_score": 0},
                },
                "action_payload": {},
            },
            {
                "id": "leg-2",
                "action_strategy": "click",
                "target_context": {
                    "uia_data": {"name": "GroupControl", "control_type": "Group"},
                    "fallback_strategy": "use_coords",
                    "fallback_coords": {"x": 300, "y": 400},
                    "confidence": {"capture_score": 0},
                },
                "action_payload": {},
            },
        ],
        "interpreted_steps": [
            {"description": "[legacy] paso rojo 1"},
            {"description": "[legacy] paso rojo 2"},
        ],
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
                    "id": "syn-ui-1:p00:open_app",
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
                {
                    "id": "syn-ui-1:p01:open_url",
                    "type": "open_url",
                    "params": {"url": "https://youtube.com"},
                    "preferred_strategy": "open_url:hotkey_ctrl_l",
                    "fallback_strategies": [],
                    "confidence": 0.9,
                    "human_label": "Abrir YouTube",
                    "needs_user_label": False,
                    "label_prompt": "",
                    "block_id": None,
                },
            ],
        },
        "legacy_compiled_graph_purpose": "legacy_debug_only",
    })


# ──────────────────────────────────────────────────────────────────────
# 1. automation_center importa los helpers del gate
# ──────────────────────────────────────────────────────────────────────


def test_automation_center_uses_truth_gate_steps_for_ui() -> None:
    """El módulo debe importar los helpers del gate y usarlos en los
    call-sites de la vista normal. Validamos a nivel de fuente para
    no arrancar Qt en CI.
    """
    src = _src()
    # Import explícito de los helpers del gate.
    assert "from app.services.missions.mission_truth_gate import" in src
    for fn in ("has_semantic_view", "step_card_models",
               "ui_blockers", "visible_step_count"):
        assert fn in src, f"automation_center no importa/usa {fn}"

    # Los call-sites primarios de la vista normal NO deben leer
    # ``len(...compiled_execution_graph)`` directamente para badges.
    # Toleramos lecturas comentadas o dentro de strings de doc.
    code_lines = [
        ln for ln in src.splitlines()
        if "compiled_execution_graph" in ln
        and not ln.lstrip().startswith("#")
        and ".. " not in ln  # docstrings RST
    ]
    # Sidebar tooltip (línea de 'for m in missions:') ya no debe
    # leer compiled_execution_graph: ahora usa visible_step_count.
    sidebar_legacy = [
        ln for ln in code_lines
        if "for m in missions" in ln or "len(m.compiled_execution_graph)" in ln
    ]
    assert not sidebar_legacy, (
        "Sidebar/badge sigue leyendo compiled_execution_graph en lugar "
        f"del gate: {sidebar_legacy}"
    )


# ──────────────────────────────────────────────────────────────────────
# 2. Vista normal NO mezcla pasos del compiled_graph cuando hay SEP
# ──────────────────────────────────────────────────────────────────────


def test_automation_center_does_not_render_compiled_graph_when_sep_exists() -> None:
    """Con SEP READY, ``step_card_models(mission, expert=False)``
    devuelve solo pasos del SEP. El ``compiled_execution_graph``
    legacy (con GroupControl + use_coords) NO entra a la vista
    normal aunque exista en disco.
    """
    m = _mission_with_sep_ready()
    assert has_semantic_view(m) is True
    models = step_card_models(m, expert=False)

    sep_count = len((m.semantic_execution_plan or {}).get("steps") or [])
    compiled_count = len(m.compiled_execution_graph or [])
    assert sep_count == 2 and compiled_count == 2  # fixture sanity

    # Toda la lista visible viene del SEP — ningún ítem etiquetado debug.
    assert len(models) == sep_count
    assert all(mdl["source"] == SOURCE_SEMANTIC_PLAN for mdl in models)
    assert all(mdl["source"] != SOURCE_COMPILED_GRAPH_DEBUG for mdl in models)

    # Y el conteo visible coincide con SEP, no con compiled_graph.
    assert visible_step_count(m) == sep_count


# ──────────────────────────────────────────────────────────────────────
# 3. La vista normal oculta GroupControl / use_coords / capture_score 0
# ──────────────────────────────────────────────────────────────────────


def test_automation_center_hides_groupcontrol_legacy_steps_in_normal_view() -> None:
    """Aunque el compiled_graph tenga ``GroupControl`` /
    ``use_coords`` / ``capture_score=0``, ningún string de esos
    debe aparecer en los modelos de tarjeta normales.
    """
    m = _mission_with_sep_ready()
    models = step_card_models(m, expert=False)

    leaked: list[str] = []
    for mdl in models:
        blob = " ".join([
            str(mdl.get("kind") or ""),
            str(mdl.get("human_label") or ""),
            str(mdl.get("preferred_strategy") or ""),
            str(mdl.get("source") or ""),
        ])
        for forbidden in ("GroupControl", "use_coords", "capture_score=0",
                          "[legacy]"):
            if forbidden in blob:
                leaked.append(f"{forbidden!r} en modelo {mdl}")
    assert not leaked, (
        "La vista normal está exponiendo señales legacy: "
        + "; ".join(leaked)
    )


# ──────────────────────────────────────────────────────────────────────
# 4. mission_1778451248 — UI normal NO muestra los 8 pasos legacy
# ──────────────────────────────────────────────────────────────────────


def test_mission_1778451248_ui_does_not_show_legacy_steps_as_primary() -> None:
    """Caso real: el JSON tiene 8 ``CompiledStep`` rojos y 6 ``SemanticPlanStep``.
    La vista normal debe mostrar 6, NO 8, y ninguno con
    ``preferred_strategy`` ``use_coords``.
    """
    m = _load_real_mission_1778451248()
    reconcile_mission_status(m)  # activa el gate (legacy_debug_only).
    assert has_semantic_view(m) is True

    sep_count = len((m.semantic_execution_plan or {}).get("steps") or [])
    compiled_count = len(m.compiled_execution_graph or [])
    assert compiled_count > sep_count, (
        "Fixture inesperado: el compiled_graph debería tener más pasos "
        "que el SEP (es la fuga legacy)."
    )

    models = step_card_models(m, expert=False)
    assert len(models) == sep_count
    assert len(models) != compiled_count

    # La UI normal nunca debe pintar use_coords como estrategia.
    assert all(
        "use_coords" not in str(mdl.get("preferred_strategy") or "")
        for mdl in models
    )


# ──────────────────────────────────────────────────────────────────────
# 5. NEEDS_REVIEW: blockers visibles, no lista legacy
# ──────────────────────────────────────────────────────────────────────


def test_needs_review_mission_shows_visible_blockers_not_legacy_graph() -> None:
    """Si el gate reduce la misión a NEEDS_REVIEW, ``ui_blockers``
    devuelve los bloqueos legibles y los modelos normales nunca
    incluyen tarjetas legacy con ``capture_score=0``.
    """
    m = _load_real_mission_1778451248()
    reconcile_mission_status(m)
    assert m.status == MissionStatus.NEEDS_REVIEW

    blockers = ui_blockers(m)
    assert blockers, "Misión NEEDS_REVIEW pero el gate no expuso blockers."
    # Los blockers son strings cortos, no objetos pesados.
    assert all(isinstance(b, str) and b for b in blockers)

    # Y los modelos normales no incluyen nada del compiled_graph.
    models = step_card_models(m, expert=False)
    assert all(mdl["source"] == SOURCE_SEMANTIC_PLAN for mdl in models)


# ──────────────────────────────────────────────────────────────────────
# 6. Vista experto puede consultar el debug legacy si se habilita
# ──────────────────────────────────────────────────────────────────────


def test_expert_view_can_still_access_legacy_debug_if_enabled() -> None:
    """Con ``expert=True`` el gate añade los pasos del
    ``compiled_execution_graph`` con ``source = SOURCE_COMPILED_GRAPH_DEBUG``,
    para que una vista de experto/diagnóstico pueda inspeccionarlos
    SIN que se confundan con los pasos canónicos.
    """
    m = _mission_with_sep_ready()

    normal = step_card_models(m, expert=False)
    expert = step_card_models(m, expert=True)

    sep_count = len((m.semantic_execution_plan or {}).get("steps") or [])
    compiled_count = len(m.compiled_execution_graph or [])

    # Vista experto = SEP + legacy debug.
    assert len(normal) == sep_count
    assert len(expert) == sep_count + compiled_count
    debug_items = [m_ for m_ in expert
                   if m_["source"] == SOURCE_COMPILED_GRAPH_DEBUG]
    assert len(debug_items) == compiled_count
    sep_items = [m_ for m_ in expert
                 if m_["source"] == SOURCE_SEMANTIC_PLAN]
    assert len(sep_items) == sep_count
