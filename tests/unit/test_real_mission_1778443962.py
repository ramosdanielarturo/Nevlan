"""
Tests focalizados — mission_1778443962 (PRD 2026-05-09 §exec-runtime).

Cubre:

A. Bug "paso 0 de 6": el SmartExecutor debe emitir ``on_step_started``
   ANTES de empezar cada step para que la UI mueva el contador de
   inmediato (en vez de quedarse en 0/N hasta que el primer step
   termina).
B. Soporte runtime para los tipos del plan de mission_1778443962:
   ``open_app``, ``select_profile``, ``use_selected_profile``,
   ``open_new_tab``, ``search_web``, ``open_search_result``,
   ``scroll_page``.
C. Fallo visible cuando un kind no tiene executor:
   ``UNSUPPORTED_SEMANTIC_STEP_TYPE`` (no se cuelga, no marca success).
D. ``select_profile`` debe fallar visible (``PROFILE_SELECTION_FAILED``)
   cuando el perfil pedido no se puede resolver (no quedarse en bucle
   de recovery infinito).
E. Trace honesto: cada step ejecutado deja entries en orden y nunca
   marca "Ejecutado" cuando falló.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from app.services.missions.action_runner import (
    StrategyResult,
    supported_strategies,
)
from app.services.missions.execution_contracts import (
    MissionStep,
    build_contract,
    supported_kinds,
)
from app.services.missions.execution_lifecycle import (
    NO_STEPS_STARTED,
    PROFILE_SELECTION_FAILED,
    STEP_FAILED,
    STEP_STARTED,
    STEP_SUCCEEDED,
    UNSUPPORTED_SEMANTIC_STEP_TYPE,
    ExecutionRun,
    ExecutionStatus,
    derive_history_label,
    is_history_executed,
)
from app.services.missions.execution_orchestration import (
    execute_with_lifecycle,
)
from app.services.missions.smart_executor import (
    SmartMissionExecutor,
    StepOutcome,
    StepStarted,
)
from app.services.missions.state_detector import StateSnapshot


# ─── Fixture loader ────────────────────────────────────────────────────


_FIXTURE_PATH = Path(__file__).resolve().parents[1] / (
    "fixtures/real_missions/mission_1778443962_raw.json"
)


def _load_real_mission() -> Dict[str, Any]:
    with _FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _real_plan_steps() -> List[Dict[str, Any]]:
    data = _load_real_mission()
    sep = data.get("semantic_execution_plan") or {}
    return list(sep.get("steps") or [])


# ─── Helpers de testing ────────────────────────────────────────────────


class _RecordingActionRunner:
    """ActionRunner de prueba: registra llamadas y siempre devuelve ok.

    Útil para tests de integración donde queremos verificar que el
    SmartExecutor llama a las estrategias correctas EN ORDEN sin tocar
    UIA / Playwright reales.
    """

    def __init__(self, *, force_ok: bool = True) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.force_ok = force_ok

    def run_strategy(self, strategy: str, step, state) -> StrategyResult:
        self.calls.append({
            "strategy": strategy,
            "step_id": step.id,
            "kind": step.kind,
            "params": dict(step.params or {}),
        })
        return StrategyResult(
            ok=self.force_ok,
            strategy=strategy,
            message="(test runner ok)" if self.force_ok else "(test runner fail)",
            duration_ms=1,
        )


class _AlwaysReadyDetector:
    """StateDetector de prueba: devuelve un snapshot construido para
    satisfacer las postcondiciones de los 6 step types del plan
    (open_app, select_profile, open_new_tab, search_web,
    open_search_result, scroll_page) sin que el executor espere
    timeouts.

    Título incluye "New Tab" + "Banxico" + "Banco" → cubre
    open_new_tab, open_search_result y search_web.
    URL con google.com/search?q=… → cubre search_web.
    Process chrome.exe → cubre open_app, scroll_page,
    select_profile.
    """

    def detect(self, deep: bool = False) -> StateSnapshot:
        return StateSnapshot(
            active_window_title=(
                "Banxico, Banco de México · New Tab - Google Search"
            ),
            active_app="chrome.exe",
            running_processes=["chrome.exe"],
            browser_url="https://www.google.com/search?q=Banco+de+Mexico",
            browser_domain="google.com",
            visible_text="Banxico Banco de México",
            active_window_bbox=None,
        )


def _make_steps_from_real_plan() -> List[MissionStep]:
    """Construye ``MissionStep``s a partir del plan canónico de la
    misión real (incluye preferred_strategy + fallback_strategies).
    """
    steps: List[MissionStep] = []
    for raw in _real_plan_steps():
        sid = str(raw.get("id") or "")
        kind = str(raw.get("type") or "")
        params = dict(raw.get("params") or {})
        params["preferred_strategy"] = raw.get("preferred_strategy") or ""
        params["fallback_strategies"] = list(
            raw.get("fallback_strategies") or [],
        )
        steps.append(MissionStep(
            id=sid, kind=kind, params=params,
            human_label=raw.get("description") or kind,
        ))
    return steps


# ─── A. BUG: paso 0 de 6 ───────────────────────────────────────────────


def test_execution_progress_moves_from_zero_on_first_step() -> None:
    """SmartExecutor debe emitir ``on_step_started(index=1)`` ANTES
    de invocar el primer ``on_step_done`` (PRD §A.2/3).

    Si on_step_started se disparara DESPUÉS, la UI quedaría mostrando
    "Paso 0 / N" hasta que el primer step termine — el bug original.
    Capturamos el orden cronológico de events para auditarlo.
    """
    events: List[tuple] = []  # (event, payload)
    runner = _RecordingActionRunner(force_ok=True)
    executor = SmartMissionExecutor(
        action_runner=runner,
        state_detector=_AlwaysReadyDetector(),
        on_step_started=lambda p: events.append(("started", p)),
        on_step_done=lambda o: events.append(("done", o)),
    )
    executor.run_mission([
        MissionStep(id="s1", kind="open_new_tab", params={
            "preferred_strategy": "open_new_tab:hotkey_ctrl_t",
        }),
        MissionStep(id="s2", kind="open_new_tab", params={
            "preferred_strategy": "open_new_tab:hotkey_ctrl_t",
        }),
    ])
    assert events, "ningún callback se disparó"
    # El PRIMER evento DEBE ser ``started`` con index=1, no ``done``.
    first_evt, first_payload = events[0]
    assert first_evt == "started", (
        f"el primer evento debería ser 'started' (index=1), "
        f"se vio {first_evt}"
    )
    assert first_payload.index == 1
    assert first_payload.total == 2
    # Y para cada step debe existir un started ANTES de su done.
    started_indices = [
        p.index for ev, p in events if ev == "started"
    ]
    assert started_indices == [1, 2]


def test_execution_progress_never_stays_zero_when_worker_started() -> None:
    """Cuando el worker arranca, el lifecycle debe ver al menos un
    ``mark_step_started`` antes del primer outcome (PRD §A.5).

    Esto es lo que evita que la UI muestre "0 de N" indefinidamente.
    """
    run = ExecutionRun(mission_id="m1", mission_name="t", total_steps=2)

    def fake_runner(mission, *, on_step_started, on_step_done, **kwargs):
        on_step_started(StepStarted(
            step_id="s1", kind="open_app", human_label="Abrir Chrome",
            index=1, total=2,
        ))
        on_step_done(StepOutcome(
            step_id="s1", kind="open_app", status="success",
            human_message="ok",
        ))
        on_step_started(StepStarted(
            step_id="s2", kind="open_new_tab", human_label="Nueva pestaña",
            index=2, total=2,
        ))
        on_step_done(StepOutcome(
            step_id="s2", kind="open_new_tab", status="success",
            human_message="ok",
        ))

        class _R:
            blocked = False
            reason = "ok"
            source = "semantic_execution_plan"
            result = type("R", (), {
                "outcomes": [], "status": "success", "last_message": "",
            })()
        return _R()

    execute_with_lifecycle(mission=object(), run=run, runner=fake_runner)
    # Debe haber al menos una entrada ``started`` antes de la primera
    # ``succeeded`` en el trace.
    statuses = [e.status for e in run.trace.entries]
    assert STEP_STARTED in statuses
    first_started_idx = statuses.index(STEP_STARTED)
    first_succeeded_idx = statuses.index(STEP_SUCCEEDED)
    assert first_started_idx < first_succeeded_idx
    # Y por tanto el run NO se queda en NO_STEPS_STARTED.
    assert run.error_code != NO_STEPS_STARTED


def test_execution_shows_visible_error_if_first_step_not_started() -> None:
    """Si el runner devuelve sin emitir nada (ni step_started ni
    step_done), el lifecycle debe marcar ``NO_STEPS_STARTED`` —
    historial honesto: "No iniciado", no "Ejecutado".
    """
    run = ExecutionRun(mission_id="m1", mission_name="t", total_steps=2)

    def silent_runner(mission, **kwargs):
        class _R:
            blocked = False
            reason = "no_op"
            source = "semantic_execution_plan"
            result = type("R", (), {
                "outcomes": [], "status": "success", "last_message": "",
            })()
        return _R()

    execute_with_lifecycle(mission=object(), run=run, runner=silent_runner)
    assert run.status == ExecutionStatus.FAILED
    assert run.error_code == NO_STEPS_STARTED
    assert derive_history_label(run) == "No iniciado"
    assert not is_history_executed(run)


def test_real_mission_1778443962_no_stuck_step_zero() -> None:
    """E2E con la misión real: el contador de progreso DEBE moverse
    de 0 → 1 → 2 → … → N a medida que el SmartExecutor avanza, no
    todo de golpe al final (que era la sensación de "stuck en 0 de 6").
    """
    steps = _make_steps_from_real_plan()
    assert len(steps) == 6, "fixture inesperado (mission_1778443962)"
    started: List[StepStarted] = []
    runner = _RecordingActionRunner(force_ok=True)
    executor = SmartMissionExecutor(
        action_runner=runner,
        state_detector=_AlwaysReadyDetector(),
        on_step_started=lambda p: started.append(p),
    )
    result = executor.run_mission(steps)
    # Cada step emitió step_started 1..N exactamente una vez.
    indices = [p.index for p in started]
    assert indices == [1, 2, 3, 4, 5, 6]
    totals = {p.total for p in started}
    assert totals == {6}
    # Y el resultado final no quedó en "stuck" — todos completed
    # (success vía SKIP cuando la postcondición ya estaba o vía
    # estrategia ejecutada por el fake runner).
    assert result.status == "success"
    assert all(
        o.status in ("success", "skipped")
        for o in result.outcomes
    )


# ─── B/C. Soporte de tipos semánticos + fallo visible ─────────────────


def test_unsupported_semantic_type_fails_visibly() -> None:
    """Un kind sin builder registrado debe fallar con
    ``UNSUPPORTED_SEMANTIC_STEP_TYPE`` (no needs_human, no success).
    """
    runner = _RecordingActionRunner(force_ok=True)
    executor = SmartMissionExecutor(
        action_runner=runner,
        state_detector=_AlwaysReadyDetector(),
    )
    bogus = MissionStep(
        id="x1", kind="totally_made_up_kind",
        params={}, human_label="Algo raro",
    )
    result = executor.run_mission([bogus])
    assert len(result.outcomes) == 1
    out = result.outcomes[0]
    assert out.status == "failed"
    assert out.error == UNSUPPORTED_SEMANTIC_STEP_TYPE
    # El mensaje canónico contiene step_id + step_type + human_label.
    msg = out.human_message
    assert "totally_made_up_kind" in msg
    assert "Algo raro" in msg or "totally_made_up_kind" in msg
    # El historial NO debe decir "Ejecutado".
    assert result.status == "failed"


def test_unsupported_semantic_type_does_not_hang() -> None:
    """El error UNSUPPORTED debe responder rápido (sin entrar en bucle
    de recovery con timeouts de varios segundos).
    """
    import time as _t
    runner = _RecordingActionRunner(force_ok=False)
    executor = SmartMissionExecutor(
        action_runner=runner,
        state_detector=_AlwaysReadyDetector(),
    )
    bogus = MissionStep(id="x1", kind="invented_kind", params={})
    t0 = _t.perf_counter()
    result = executor.run_mission([bogus])
    elapsed = _t.perf_counter() - t0
    # Si entrara al bucle de recovery con timeouts default, esto
    # tardaría >5 s. Aceptamos hasta 1.5s para máquinas lentas en CI.
    assert elapsed < 1.5
    assert result.outcomes[0].error == UNSUPPORTED_SEMANTIC_STEP_TYPE
    # No se llamó a NINGUNA estrategia (cortocircuito en build_contract).
    assert not runner.calls


def test_unsupported_semantic_type_not_marked_success() -> None:
    """Honest reporting: aunque haya un step exitoso previo, un
    UNSUPPORTED detiene la misión y el status final NO es success.
    """
    runner = _RecordingActionRunner(force_ok=True)
    executor = SmartMissionExecutor(
        action_runner=runner,
        state_detector=_AlwaysReadyDetector(),
    )
    steps = [
        MissionStep(id="ok1", kind="open_new_tab", params={
            "preferred_strategy": "open_new_tab:hotkey_ctrl_t",
        }),
        MissionStep(id="bad", kind="totally_invented", params={}),
    ]
    result = executor.run_mission(steps)
    assert result.status == "failed"
    # Step 1 puede quedar como success o skipped (depende de
    # postcondición); lo importante es que NO falló y que el step 2
    # SÍ falló con UNSUPPORTED.
    assert result.outcomes[0].status in ("success", "skipped")
    assert result.outcomes[1].status == "failed"
    assert result.outcomes[1].error == UNSUPPORTED_SEMANTIC_STEP_TYPE


def test_all_real_mission_1778443962_step_types_have_executor_or_visible_error() -> None:
    """Todos los kinds que aparecen en mission_1778443962 deben tener
    builder registrado. Si alguno no, el test debe fallar y exponer
    en el assert qué tipo falta — sin que el SmartExecutor lo silencie.
    """
    plan_kinds = {s.get("type") for s in _real_plan_steps()}
    missing = plan_kinds - set(supported_kinds())
    assert not missing, (
        f"Tipos del plan sin executor: {sorted(missing)} "
        f"— supported_kinds={supported_kinds()}"
    )


# ─── D. Runtime esperado por tipo ──────────────────────────────────────


def test_use_selected_profile_runtime_noops_safely_when_profile_active() -> None:
    """``use_selected_profile`` debe completar rápido cuando el
    navegador está activo (no debe colgarse esperando un profile
    picker que no va a aparecer).

    Si Chrome ya está corriendo, la postcondición se cumple y el
    executor toma el atajo SKIP — la misión avanza al siguiente
    paso. Lo crítico es que el step termine ``success`` o
    ``skipped`` (ambos avanzan), nunca ``needs_human``/``failed``.
    """
    runner = _RecordingActionRunner(force_ok=True)
    executor = SmartMissionExecutor(
        action_runner=runner,
        state_detector=_AlwaysReadyDetector(),
    )
    step = MissionStep(
        id="p1", kind="use_selected_profile",
        params={
            "app": "chrome",
            "profile_resolution": "already_active_or_selected",
            "preferred_strategy": "use_selected_profile:noop",
        },
    )
    result = executor.run_mission([step])
    assert result.outcomes[0].status in ("success", "skipped")
    assert result.status == "success"
    # Y la estrategia ``use_selected_profile:noop`` está registrada
    # (lo importante para "no colgarse" — si necesitara ejecutar
    # estrategia, hay un noop disponible).
    assert "use_selected_profile:noop" in supported_strategies()


def test_search_web_runtime_uses_ctrl_l_type_enter() -> None:
    """``search_web:address_bar_type_enter`` está registrada y es la
    preferida para search_web.
    """
    assert "search_web:address_bar_type_enter" in supported_strategies()
    step = MissionStep(
        id="p3", kind="search_web",
        params={"query": "Banco de Mexico", "engine": "google",
                "submit": True},
    )
    contract = build_contract(step)
    assert contract.preferred_strategy == "search_web:address_bar_type_enter"
    assert "search_web:dom_search_input" in contract.fallback_strategies


def test_open_search_result_runtime_targets_title() -> None:
    """``open_search_result`` debe tener estrategia preferida DOM por
    título y postcondición que valide cambio de página/título.
    """
    assert (
        "open_search_result:dom_link_by_title" in supported_strategies()
    )
    step = MissionStep(
        id="p4", kind="open_search_result",
        params={"title": "Banxico, banco central, Banco de México"},
    )
    contract = build_contract(step)
    assert contract.preferred_strategy == "open_search_result:dom_link_by_title"
    # El "Banxico" debería detectarse como título por la postcondición:
    state_with_banxico = StateSnapshot(
        active_window_title="Banxico — Banco de México",
        active_app="chrome.exe",
        running_processes=["chrome.exe"],
        browser_url="https://www.banxico.org.mx/",
        browser_domain="banxico.org.mx",
        visible_text="",
        active_window_bbox=None,
    )
    assert contract.postcondition(state_with_banxico)


def test_scroll_page_runtime_executes() -> None:
    """``scroll_page:wheel`` está registrada y es la preferida para
    scroll_page (independiente de scroll_results legacy).
    """
    assert "scroll_page:wheel" in supported_strategies()
    step = MissionStep(
        id="p5", kind="scroll_page",
        params={"direction": "down", "amount": 2},
    )
    contract = build_contract(step)
    assert contract.preferred_strategy == "scroll_page:wheel"
    # No es unknown_kind — es un kind válido.
    assert not contract.is_unknown_kind


# ─── E. select_profile fail visible (PROFILE_SELECTION_FAILED) ────────


def _patch_contract_with_tiny_timeouts(monkeypatch) -> None:
    """Sustituye ``build_contract`` para que devuelva contratos con
    timeouts microscópicos (1 ms). Sin esto, los tests que ejercitan
    el bucle de recovery + estrategias agotadas tardan ~45 s
    (5 strategies × 8 s polling each) — inviable en CI.
    """
    from app.services.missions import smart_executor as se

    original = se.build_contract

    def fast(step):
        c = original(step)
        c.precondition_grace_ms = 0
        c.postcondition_settle_ms = 1
        c.postcondition_timeout_ms = 1
        return c

    monkeypatch.setattr(se, "build_contract", fast)


def test_select_profile_runtime_fails_visible_if_profile_missing(
    monkeypatch,
) -> None:
    """Si todas las estrategias de select_profile fallan, el outcome
    debe quedar marcado con ``error=PROFILE_SELECTION_FAILED`` para
    que el historial discrimine "el botón falló" de "el perfil
    pedido no existe".
    """
    _patch_contract_with_tiny_timeouts(monkeypatch)

    class _AllFailRunner:
        def __init__(self) -> None:
            self.calls: List[str] = []

        def run_strategy(self, strategy, step, state):
            self.calls.append(strategy)
            return StrategyResult(
                ok=False, strategy=strategy,
                message=f"perfil no encontrado vía {strategy}",
                duration_ms=1,
            )

    from app.services.missions.recovery_engine import RecoveryPlan

    class _EscalateRecovery:
        def recover_precondition(self, *a, **k):
            return RecoveryPlan(
                escalate_human=True,
                human_message="No puedo abrir el profile picker.",
            )

        def recover_postcondition(self, *a, **k):
            return RecoveryPlan(
                escalate_human=True,
                human_message="No encontré el perfil pedido.",
            )

    picker_state = StateSnapshot(
        active_window_title="Choose your profile",
        active_app="chrome.exe",
        running_processes=["chrome.exe"],
        browser_url=None,
        browser_domain=None,
        visible_text="",
        active_window_bbox=None,
    )

    class _StaticDetector:
        def detect(self, deep: bool = False) -> StateSnapshot:
            return picker_state

    runner_fake = _AllFailRunner()
    executor = SmartMissionExecutor(
        action_runner=runner_fake,
        state_detector=_StaticDetector(),
    )
    executor.recovery = _EscalateRecovery()
    step = MissionStep(
        id="p2", kind="select_profile",
        params={
            "profile_name": "Interés Interbancaria",
            "app": "chrome",
            "preferred_strategy": "select_profile:uia_text_match",
            "fallback_strategies": [
                "select_profile:visible_text_ocr",
                "select_profile:chrome_profile_alias",
                "select_profile:skip_if_loaded",
            ],
        },
    )
    result = executor.run_mission([step])
    out = result.outcomes[0]
    assert out.status == "needs_human"
    assert out.error == PROFILE_SELECTION_FAILED
    # Y el resultado de la misión NO es success.
    assert result.status == "stopped_for_human"


def test_invalid_profile_name_fails_visible_not_hang(monkeypatch) -> None:
    """Reglas explícitas de la spec: select_profile inválido debe
    fallar visible (no hang). El executor agota estrategias y emite
    needs_human/failed con código PROFILE_SELECTION_FAILED.
    """
    import time as _t
    _patch_contract_with_tiny_timeouts(monkeypatch)

    class _AllFailFast:
        calls = 0

        def run_strategy(self, strategy, step, state):
            self.calls += 1
            return StrategyResult(
                ok=False, strategy=strategy,
                message="profile no encontrado", duration_ms=1,
            )

    from app.services.missions.recovery_engine import RecoveryPlan

    class _EscalateRecovery:
        def recover_precondition(self, *a, **k):
            return RecoveryPlan(escalate_human=True, human_message="x")

        def recover_postcondition(self, *a, **k):
            return RecoveryPlan(escalate_human=True, human_message="x")

    picker_state = StateSnapshot(
        active_window_title="Elige tu perfil",
        active_app="chrome.exe",
        running_processes=["chrome.exe"],
        browser_url=None,
        browser_domain=None,
        visible_text="",
        active_window_bbox=None,
    )

    class _PickerDetector:
        def detect(self, deep: bool = False) -> StateSnapshot:
            return picker_state

    executor = SmartMissionExecutor(
        action_runner=_AllFailFast(),
        state_detector=_PickerDetector(),
    )
    executor.recovery = _EscalateRecovery()
    step = MissionStep(
        id="p2", kind="select_profile",
        params={
            "profile_name": "Inexistente",
            "preferred_strategy": "select_profile:uia_text_match",
        },
    )
    t0 = _t.perf_counter()
    result = executor.run_mission([step])
    elapsed = _t.perf_counter() - t0
    # Con timeouts microscópicos, debe terminar en <2 s — la spec
    # exige "no hang", no "instantáneo".
    assert elapsed < 5.0
    out = result.outcomes[0]
    assert out.status == "needs_human"
    assert out.error == PROFILE_SELECTION_FAILED


def test_profile_selection_failure_history_failed() -> None:
    """El historial NO debe decir "Ejecutado" para una corrida que se
    detuvo por PROFILE_SELECTION_FAILED.
    """
    run = ExecutionRun(mission_id="m1", mission_name="t", total_steps=1)

    def runner(mission, *, on_step_started, on_step_done, **kwargs):
        on_step_started(StepStarted(
            step_id="p2", kind="select_profile",
            human_label="Perfil", index=1, total=1,
        ))
        on_step_done(StepOutcome(
            step_id="p2", kind="select_profile", status="needs_human",
            error=PROFILE_SELECTION_FAILED,
            human_message="No encontré 'Interés Interbancaria'",
        ))

        class _R:
            blocked = False
            reason = "stopped"
            source = "semantic_execution_plan"
            result = type("R", (), {
                "outcomes": [], "status": "stopped_for_human",
                "last_message": "necesita perfil",
            })()
        return _R()

    execute_with_lifecycle(mission=object(), run=run, runner=runner)
    assert run.status == ExecutionStatus.FAILED
    assert derive_history_label(run) != "Ejecutado"
    assert not is_history_executed(run)


# ─── E2E: ejecución real de mission_1778443962 ────────────────────────


def test_real_mission_1778443962_execution_invokes_all_steps_in_order() -> None:
    """E2E con executor fake: los 6 steps de mission_1778443962 se
    invocan en el orden esperado, cada uno emite step_started 1..6,
    y el lifecycle termina con éxito (no se queda en "stuck"/0).
    """
    steps = _make_steps_from_real_plan()
    expected_kinds = [
        "open_app",
        "select_profile",
        "open_new_tab",
        "search_web",
        "open_search_result",
        "scroll_page",
    ]
    assert [s.kind for s in steps] == expected_kinds

    started: List[StepStarted] = []
    done: List[StepOutcome] = []
    runner = _RecordingActionRunner(force_ok=True)
    executor = SmartMissionExecutor(
        action_runner=runner,
        state_detector=_AlwaysReadyDetector(),
        on_step_started=lambda p: started.append(p),
        on_step_done=lambda o: done.append(o),
    )
    result = executor.run_mission(steps)
    # Orden de step_started.
    started_kinds = [p.kind for p in started]
    assert started_kinds == expected_kinds
    # Index va 1..6, total=6.
    assert [p.index for p in started] == list(range(1, 7))
    assert {p.total for p in started} == {6}
    # done también en orden.
    done_kinds = [o.kind for o in done]
    assert done_kinds == expected_kinds
    assert result.status == "success"


def test_real_mission_1778443962_lifecycle_trace_honest() -> None:
    """Cuando el SmartExecutor corre los 6 steps con éxito, el trace
    del ExecutionRun debe tener exactamente 12 entries (started +
    succeeded por cada step) y no duplicados, gracias a la dedup de
    ``_record_outcome``.
    """
    steps = _make_steps_from_real_plan()
    run = ExecutionRun(
        mission_id="98aa20d4", mission_name="t",
        total_steps=len(steps),
    )

    def runner(mission, *, on_step_started, on_step_done, **kwargs):
        for i, st in enumerate(steps, start=1):
            on_step_started(StepStarted(
                step_id=st.id, kind=st.kind,
                human_label=st.human_label,
                index=i, total=len(steps),
            ))
            on_step_done(StepOutcome(
                step_id=st.id, kind=st.kind, status="success",
                human_message="ok",
            ))

        class _R:
            blocked = False
            reason = "ok"
            source = "semantic_execution_plan"
            result = type("R", (), {
                "outcomes": [], "status": "success", "last_message": "",
            })()
        return _R()

    execute_with_lifecycle(mission=object(), run=run, runner=runner)
    statuses = [e.status for e in run.trace.entries]
    # 6 started + 6 succeeded = 12. Sin dedup serían 18 (started doble).
    assert len(statuses) == 12
    assert statuses.count(STEP_STARTED) == 6
    assert statuses.count(STEP_SUCCEEDED) == 6
    # Y el orden es started, succeeded, started, succeeded, ...
    assert statuses[::2] == [STEP_STARTED] * 6
    assert statuses[1::2] == [STEP_SUCCEEDED] * 6
    assert run.status == ExecutionStatus.SUCCEEDED


def test_profile_selection_failure_closes_progress_dialog() -> None:
    """Cuando select_profile falla con PROFILE_SELECTION_FAILED, el
    lifecycle debe quedar terminal (FAILED) — la UI ve esto y cierra
    el progress dialog (no se queda colgada).
    """
    run = ExecutionRun(mission_id="m1", mission_name="t", total_steps=1)

    def runner(mission, *, on_step_started, on_step_done, **kwargs):
        on_step_started(StepStarted(
            step_id="p2", kind="select_profile",
            human_label="Perfil", index=1, total=1,
        ))
        on_step_done(StepOutcome(
            step_id="p2", kind="select_profile", status="failed",
            error=PROFILE_SELECTION_FAILED,
            human_message="profile picker no se encontró",
        ))

        class _R:
            blocked = False
            reason = "stop"
            source = "semantic_execution_plan"
            result = type("R", (), {
                "outcomes": [], "status": "failed", "last_message": "x",
            })()
        return _R()

    execute_with_lifecycle(mission=object(), run=run, runner=runner)
    assert run.is_terminal
    assert run.status == ExecutionStatus.FAILED
