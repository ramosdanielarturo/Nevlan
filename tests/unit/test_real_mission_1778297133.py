"""
Nevlan — E2E con la grabación REAL ``mission_1778297133`` (PRD 2026-05-08c §exec-lifecycle)
=============================================================================================

Esta misión documenta el bug del **ciclo de vida de ejecución**:

    1. El usuario presionó Ejecutar.
    2. La misión NO ejecutó pasos visibles.
    3. La ventana de ejecución NO se cerró.
    4. El historial mostró "Ejecutado" igualmente.

La fixture es ``mission_1778297133_raw.json`` y representa el estado
honesto de la grabación tras ``apply_collapse_to_mission``: cae
naturalmente en READY (``ready_provenance="raw_evidence"``) con 6
pasos. El plan canónico es:

    1. open_app          (Chrome)
    2. select_profile    (Daniel Arturo Ramos)
    3. open_new_tab
    4. open_url          (YouTube — bookmark)
    5. search_content    (provider=youtube, "devuelveme el amor luis miguel")
    6. scroll_results

Casos cubiertos:

  * El plan se compone correctamente y queda en READY sin
    ``query_fidelity_suspect`` (el usuario confirma que escribió
    así la query).
  * Si el orquestador NO arranca pasos (bug observado), el
    lifecycle NUNCA queda en SUCCEEDED — siempre marca
    ``failed/NO_STEPS_STARTED``.
  * El historial muestra "No iniciado", no "Ejecutado".
  * El bridge debe usar ``semantic_execution_plan`` como fuente.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contracts.mission import Mission
from app.services.missions.execution_lifecycle import (
    EXECUTOR_NOT_INVOKED,
    NO_STEPS_STARTED,
    ExecutionRun,
    ExecutionStatus,
    derive_history_label,
)
from app.services.missions.execution_orchestration import (
    evaluate_decision,
    execute_with_lifecycle,
)
from app.services.missions.mission_review_summary import (
    build_mission_review_summary,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "real_missions" / "mission_1778297133_raw.json"
)


def _load() -> Mission:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return Mission(**data)


# ─── Plan honesto + query respeted ───────────────────────────────────


def test_mission_1778297133_plan_is_ready_from_raw_evidence() -> None:
    m = _load()
    sep = m.semantic_execution_plan or {}
    assert sep.get("intent_status") == "READY"
    assert sep.get("ready_provenance") == "raw_evidence"
    assert sep.get("not_ready_reasons") == []


def test_mission_1778297133_query_devuelveme_amor_is_accepted() -> None:
    """PRD: la query 'devuelveme el amor luis miguel' es lo que el
    usuario escribió y NO debe marcarse como sospechosa."""
    m = _load()
    sep = m.semantic_execution_plan or {}
    search_steps = [
        s for s in sep.get("steps") or []
        if s.get("type") == "search_content"
        and str((s.get("params") or {}).get("provider") or "").lower()
        == "youtube"
    ]
    assert len(search_steps) == 1
    params = search_steps[0].get("params") or {}
    assert params.get("query") == "devuelveme el amor luis miguel"
    assert params.get("query_fidelity_status") == "ok"


def test_mission_1778297133_full_step_kinds() -> None:
    m = _load()
    sep = m.semantic_execution_plan or {}
    kinds = [s.get("type") for s in sep.get("steps") or []]
    assert kinds == [
        "open_app", "select_profile", "open_new_tab",
        "open_url", "search_content", "scroll_results",
    ]


def test_mission_1778297133_review_summary_is_ready() -> None:
    m = _load()
    summary = build_mission_review_summary(m)
    assert summary.status_ready is True
    assert summary.visible_blockers == []
    assert len(summary.steps) == 6


# ─── Ciclo de vida: el bug original ───────────────────────────────────


def test_mission_1778297133_no_false_success_without_steps() -> None:
    """REGRESIÓN: antes el bridge devolvía ``decision.result=None`` y la
    UI marcaba "Ejecutado". Ahora el lifecycle FALLA correctamente."""
    m = _load()
    run = ExecutionRun(
        mission_id=m.id, mission_name=m.name,
        total_steps=len(m.semantic_execution_plan["steps"]),
        executor_used="smart",
        plan_source="semantic_execution_plan",
    )
    run.mark_started()

    class _Decision:
        blocked = False
        reason = ""
        result = None
        source = "semantic_execution_plan"

    evaluate_decision(run, _Decision(), consume_outcomes=True)
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == EXECUTOR_NOT_INVOKED
    assert derive_history_label(run) != "Ejecutado"


def test_mission_1778297133_executor_invoked_but_zero_outcomes_marks_no_steps_started() -> None:
    """Si el SmartExecutor termina con ``status="success"`` y
    ``outcomes=[]``, lifecycle => failed/NO_STEPS_STARTED."""
    m = _load()
    run = ExecutionRun(
        mission_id=m.id, mission_name=m.name,
        total_steps=6, executor_used="smart",
    )
    run.mark_started()

    class _Result:
        status = "success"
        outcomes = []
        last_message = ""

    class _Decision:
        blocked = False
        reason = ""
        result = _Result()
        source = "semantic_execution_plan"

    evaluate_decision(run, _Decision(), consume_outcomes=True)
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == NO_STEPS_STARTED
    assert derive_history_label(run) == "No iniciado"


def test_mission_1778297133_execution_uses_semantic_plan() -> None:
    """El bridge debe consumir ``mission.semantic_execution_plan`` —
    no el ``compiled_execution_graph`` legacy."""
    m = _load()
    sep = m.semantic_execution_plan or {}
    assert sep.get("steps"), "El plan semántico no puede estar vacío"
    # El compiled_execution_graph está vacío en el fixture (PRD §2026-05-07
    # §10: legacy debug-only). El bridge no debe intentar usarlo.
    assert m.compiled_execution_graph in ([], None) or (
        len(m.compiled_execution_graph) == 0
    )

    captured: dict = {}

    def fake_runner(mission, *, on_status, on_step_done,
                    on_human_help_needed, expert_mode,
                    allow_legacy_fallback, **kwargs):
        # Verificación: el bridge sí pudo leer steps del plan.
        captured["plan_steps"] = len(
            (mission.semantic_execution_plan or {}).get("steps") or []
        )

        class _Outcome:
            step_id = "open_app"
            kind = "open_app"
            status = "success"
            strategy_used = "windows_search"
            duration_ms = 100
            error = ""
            state_after = {}

        class _Result:
            status = "success"
            outcomes = [_Outcome()]
            last_message = ""

        class _Decision:
            blocked = False
            reason = ""
            result = _Result()
            source = "semantic_execution_plan"

        on_step_done(_Outcome())
        return _Decision()

    run = ExecutionRun(
        mission_id=m.id, mission_name=m.name,
        total_steps=6, executor_used="smart",
        plan_source="semantic_execution_plan",
    )
    decision = execute_with_lifecycle(
        mission=m, run=run, runner=fake_runner,
    )
    assert decision is not None
    assert captured.get("plan_steps") == 6
    # Con un solo outcome y total_steps=6 falta evidencia de éxito
    # global — pero al menos no se marca como NO_STEPS_STARTED.
    assert run.error_code != NO_STEPS_STARTED


def test_mission_1778297133_legacy_graph_ignored_telemetry() -> None:
    """``ready_provenance=raw_evidence`` viene con
    ``legacy_graph_ignored=True`` — fix histórico §2026-05-07."""
    m = _load()
    sep = m.semantic_execution_plan or {}
    assert sep.get("legacy_graph_ignored") is True
    assert sep.get("coords_used") is False
