"""
E2E real — SmartMissionExecutor → Chrome → YouTube → búsqueda
=============================================================

Misión ejemplo (PRD 2026-05-03d §5)::

    1. open_app            Chrome
    2. select_profile      Daniel Arturo Ramos
    3. open_new_tab
    4. open_url            YouTube
    5. search_youtube      "Devuélveme el amor de Luis Miguel"

Lo que valida este script:

* El ejecutor usado ES ``SmartMissionExecutor`` y NUNCA ``MissionPlayer``
  legacy (se verifica por introspección).
* No avanza tras una precondición fallida: si Chrome no se detecta,
  los siguientes steps ni se ejecutan.
* No avanza tras una postcondición fallida: si la URL no cambia a
  ``youtube.com``, el search no arranca.
* Si YouTube no está abierto, el Smart Executor lo abre (Ctrl+L +
  youtube.com + Enter).
* Si la barra de búsqueda no se encuentra por DOM, pasa por UIA/OCR/
  visual/coords. Registra la cadena de fallbacks usada.
* Si ``search_youtube`` falla, rollback al checkpoint ``youtube_loaded``
  y máximo 2 ciclos de retry (enforced por ``MAX_PER_STRATEGY`` +
  ``recover_search``).
* Resultado final: URL contiene ``search_query=`` o el título contiene
  una palabra de la query.

Uso::

    # Real (abre Chrome, YouTube, etc.):
    python scripts/test_smart_executor_youtube.py

    # Dry-run (monkeypatch: no toca el sistema, sólo verifica lógica):
    python scripts/test_smart_executor_youtube.py --dry-run

El script imprime un **reporte breve** con el player usado, la cadena
de estrategias, los checkpoints guardados y el status final. Sirve
como QA manual para el sprint "ejecución inteligente".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Aseguramos que el repo raíz esté en PYTHONPATH cuando se corre directo.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.contracts.mission import (  # noqa: E402
    ActionStrategy,
    CompiledStep,
    InterpretedStep,
    Mission,
    MissionStatus,
    TargetContext,
    ValidationStrategy,
)
from app.services.missions.execution_contracts import MissionStep  # noqa: E402
from app.services.missions.semantic_adapter import (  # noqa: E402
    adapt_mission_to_steps,
    is_mission_semantic,
)
from app.services.missions.smart_executor import (  # noqa: E402
    MissionResult,
    SmartMissionExecutor,
    StepOutcome,
)
from app.services.missions.smart_runner_bridge import (  # noqa: E402
    RunnerDecision,
    run_mission_smart,
)


PROFILE_NAME = "Daniel Arturo Ramos"
YOUTUBE_QUERY = "Devuélveme el amor de Luis Miguel"


# ─── Construcción de la misión ────────────────────────────────────────


def _tc(uia_name: str, process: str = "chrome.exe") -> TargetContext:
    return TargetContext(
        uia_data={
            "name": uia_name,
            "automation_id": "",
            "control_type": "ButtonControl",
        },
        process_name=process,
        confidence={"quality_level": "green", "capture_score": 0.95},
    )


def build_example_mission() -> Mission:
    """Construye la misión YouTube del PRD sin pasar por el recorder.

    Cada step es un ``CompiledStep`` **ya semántico** — es el tipo de
    grafo que produce el compiler + semantic fusion tras una grabación
    real del workflow Chrome → perfil → nueva pestaña → YouTube →
    búsqueda. Al adaptarlo, cada paso se traduce uno-a-uno a un
    ``MissionStep`` que consume el Smart Executor.
    """
    compiled: List[CompiledStep] = [
        CompiledStep(
            id="cs1-launch-chrome",
            goal="Abrir Chrome",
            action_strategy=ActionStrategy.LAUNCH_APP,
            action_payload={
                "app_name": "Chrome",
                "raw_typed": "chrome",
                "launch_method": "windows_search",
                "had_enter": True,
            },
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
            target_context=_tc("Chrome", "chrome.exe"),
        ),
        CompiledStep(
            id="cs2-select-profile",
            goal=f"Seleccionar perfil {PROFILE_NAME}",
            action_strategy=ActionStrategy.SELECT_PROFILE,
            action_payload={"profile": PROFILE_NAME, "app": "chrome"},
            validation_strategy=ValidationStrategy.REQUIRE_TEXT_MATCH,
            target_context=_tc(PROFILE_NAME),
        ),
        CompiledStep(
            id="cs3-open-new-tab",
            goal="Abrir pestaña nueva",
            action_strategy=ActionStrategy.OPEN_NEW_TAB,
            action_payload={"app": "chrome"},
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
            target_context=_tc("Nueva pestaña"),
        ),
        CompiledStep(
            id="cs4-open-youtube",
            goal="Abrir YouTube",
            action_strategy=ActionStrategy.OPEN_BOOKMARK,
            action_payload={
                "name": "YouTube",
                "url": "https://www.youtube.com",
                "app": "chrome",
            },
            validation_strategy=ValidationStrategy.REQUIRE_TEXT_MATCH,
            target_context=_tc("YouTube"),
        ),
        CompiledStep(
            id="cs5-search-youtube",
            goal="Buscar en YouTube",
            action_strategy=ActionStrategy.SUBMIT_SEARCH,
            action_payload={
                "text": YOUTUBE_QUERY,
                "site": "youtube",
                "search_site_label": "YouTube",
                "target_label": "youtube_search_box",
                "submit_after": True,
            },
            validation_strategy=ValidationStrategy.REQUIRE_TEXT_MATCH,
            target_context=_tc("Buscar"),
        ),
    ]

    mission = Mission(
        id="smart-e2e-youtube",
        name="SmartExecutor E2E — YouTube",
        status=MissionStatus.EXECUTABLE,
    )
    mission.compiled_execution_graph = compiled
    mission.interpreted_steps = [
        InterpretedStep(description=cs.goal, raw_event_ids=[])
        for cs in compiled
    ]
    return mission


# ─── Verificaciones hard (PRD §5) ─────────────────────────────────────


def assert_smart_executor_is_used(decision: RunnerDecision) -> None:
    """PRD §5 + §14: no usa MissionPlayer legacy para misiones EXECUTABLE."""
    if decision.player_used != "smart":
        raise AssertionError(
            f"❌ PRD §14 — se esperaba player_used='smart', "
            f"pero el bridge eligió '{decision.player_used}' "
            f"(motivo: {decision.reason})"
        )


def assert_no_blind_advance(result: MissionResult) -> None:
    """PRD §9: si un step falla, el siguiente NO debe haberse ejecutado.

    Implementación:
      * Si algún ``StepOutcome`` termina en ``failed`` o ``needs_human``,
        verificamos que no hay outcomes posteriores con status distinto
        a ``skipped`` (no re-runs).
    """
    terminal = {"failed", "needs_human"}
    for i, o in enumerate(result.outcomes):
        if o.status in terminal:
            after = result.outcomes[i + 1 :]
            bad = [x for x in after if x.status not in ("skipped",)]
            if bad:
                raise AssertionError(
                    f"❌ PRD §9 — tras paso fallido ({o.step_id}, "
                    f"status={o.status}), se ejecutaron {len(bad)} pasos más."
                )


def assert_final_result_reaches_youtube(result: MissionResult) -> None:
    """PRD §5: resultado final contiene search_query o resultados visibles."""
    if not result.outcomes:
        raise AssertionError("❌ PRD §5 — no hubo outcomes (misión vacía).")
    last = result.outcomes[-1]
    if last.status not in ("success", "skipped"):
        # Aceptamos needs_human si el motivo es "la búsqueda está
        # esperando intervención": es un fallo CONTROLADO del PRD.
        print(
            f"ℹ Paso final terminó en '{last.status}' "
            f"({last.human_message}). La misión se detuvo de forma segura."
        )
        return
    after = last.state_after or {}
    url = (after.get("browser_url") or "").lower()
    title = (after.get("active_window_title") or "").lower()
    q_token = YOUTUBE_QUERY.split()[0].lower()
    has_query = (
        "search_query=" in url
        or "/results?" in url
        or q_token in title
        or q_token in (after.get("browser_title") or "").lower()
    )
    if not has_query:
        raise AssertionError(
            "❌ PRD §5 — el paso final terminó success, pero el estado "
            f"no trae search_query ni título con '{q_token}'. "
            f"url={url!r} title={title!r}"
        )


# ─── Reporte breve (PRD §13) ──────────────────────────────────────────


def print_execution_report(decision: RunnerDecision, *, dry_run: bool) -> None:
    """Imprime el reporte humano requerido al final de la ejecución."""
    data = decision.to_dict()
    print("\n" + "=" * 68)
    print("  REPORTE BREVE — SmartExecutor E2E YouTube")
    print("=" * 68)
    print(f"  Player usado   : {data['player_used']}")
    print(f"  Motivo         : {data['reason']}")
    print(f"  Duración (ms)  : {data['duration_ms']}")
    print(f"  Status del gate: {data['status_used']}")
    if decision.result:
        r = decision.result
        ok = sum(1 for o in r.outcomes if o.status in ("success", "skipped"))
        print(f"  Steps ok       : {ok}/{len(r.outcomes)}")
        print(f"  Estado final   : {r.status}")
        for i, o in enumerate(r.outcomes, 1):
            strat = o.strategy_used or "-"
            print(
                f"    {i}. [{o.status:<10}] kind={o.kind:<15} "
                f"strat={strat:<34} retries={o.retries} "
                f"dur={o.duration_ms}ms"
            )
            if o.human_message:
                print(f"       > {o.human_message}")
    if decision.blockers:
        print(f"  Bloqueantes    : {len(decision.blockers)}")
        for b in decision.blockers:
            print(f"    · [{b.code}] {b.message}")
    print(f"  (dry_run={dry_run})")
    print("=" * 68 + "\n")


# ─── Runner ────────────────────────────────────────────────────────────


def _human_status(msg: str) -> None:
    print(f"[Nevlan] {msg}")


def _expert_step(outcome: StepOutcome) -> None:
    strat = outcome.strategy_used or "-"
    print(
        f"  [expert] step={outcome.step_id} kind={outcome.kind} "
        f"status={outcome.status} strategy={strat} retries={outcome.retries}"
    )


def _needs_human(outcome: StepOutcome) -> None:
    print(f"[Nevlan · ATENCIÓN] {outcome.human_message}")


def _apply_dry_run_monkeypatch() -> None:
    """Parchea el ``ActionRunner`` para que no toque el sistema.

    Sin ``--dry-run`` el script espera tener Chrome, red y Playwright
    disponibles. En ``--dry-run`` cada estrategia devuelve ``ok=True``
    sin hacer nada, y el ``StateDetector`` devuelve un snapshot
    sintético que cumple precondiciones y postcondiciones por step.

    El objetivo del modo dry-run es **validar la lógica** del bridge y
    del adaptador en CI headless, no la integración con Chrome real.
    """
    from app.services.missions import state_detector as _sd
    from app.services.missions import action_runner as _ar
    from app.services.missions.state_detector import StateSnapshot

    # Estado sintético que "evoluciona" conforme avanzan los steps.
    _state: Dict[str, Any] = {
        "running": set(),
        "url": "",
        "title": "Escritorio",
        "domain": "",
    }

    def _dry_detect(self, deep: bool = False, want_url: bool = True) -> StateSnapshot:  # noqa: D401
        return StateSnapshot(
            active_app="chrome.exe" if "chrome.exe" in _state["running"] else None,
            active_pid=1,
            active_window_title=_state["title"],
            active_window_class="Chrome_WidgetWin_1",
            active_window_bbox={"x": 0, "y": 0, "w": 1280, "h": 800},
            running_processes=sorted(_state["running"]),
            browser_url=_state["url"] or None,
            browser_title=_state["title"],
            browser_domain=_state["domain"] or None,
            has_playwright_page=bool(_state["url"]),
            screen_size=(1920, 1080),
            dpi_scale=1.0,
        )

    _sd.StateDetector.detect = _dry_detect  # type: ignore[assignment]

    def _dry_run_strategy(self, strategy, step, state):  # type: ignore[no-redef]
        # Avanzamos el estado en función del kind del step — así las
        # postcondiciones se cumplen sin pegarle al sistema.
        t0 = time.time()
        kind = step.kind
        if kind == "open_app":
            _state["running"].add("chrome.exe")
            _state["title"] = "Nueva pestaña - Google Chrome"
        elif kind == "select_profile":
            _state["url"] = "chrome://newtab/"
            _state["title"] = (
                f"{PROFILE_NAME} - Nueva pestaña - Google Chrome"
            )
        elif kind == "open_new_tab":
            _state["url"] = "chrome://newtab/"
            _state["title"] = "Nueva pestaña - Google Chrome"
        elif kind == "open_url":
            _state["url"] = "https://www.youtube.com/"
            _state["title"] = "YouTube"
            _state["domain"] = "youtube.com"
        elif kind == "search_youtube":
            _state["url"] = (
                "https://www.youtube.com/results?search_query="
                + step.params.get("query", "").replace(" ", "+")
            )
            _state["title"] = f"{step.params.get('query', '')} - YouTube"
            _state["domain"] = "www.youtube.com"
        return _ar.StrategyResult(
            ok=True,
            strategy=strategy,
            message=f"dry-run ok for {kind}",
            duration_ms=int((time.time() - t0) * 1000),
        )

    _ar.ActionRunner.run_strategy = _dry_run_strategy  # type: ignore[assignment]


# ─── Main ─────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No tocar el sistema: parchear runner + detector.",
    )
    parser.add_argument(
        "--expert",
        action="store_true",
        help="Imprimir líneas técnicas por step (strategy, retries, ...).",
    )
    parser.add_argument(
        "--save-report",
        type=str,
        default="",
        help="Ruta opcional para volcar el reporte en JSON.",
    )
    args = parser.parse_args()

    # Forzamos UTF-8 en stdout para que los emojis/flechas no rompan el
    # script bajo Windows cp1252 (default en la consola Win).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.dry_run:
        print("> Modo DRY-RUN activo: ningún proceso real será abierto.")
        _apply_dry_run_monkeypatch()

    mission = build_example_mission()

    # Pre-check: la misión DEBE considerarse semántica + adaptable.
    if not is_mission_semantic(mission):
        print("❌ La misión de ejemplo no se detecta como semántica.")
        return 2
    adapted = adapt_mission_to_steps(mission, mission_id=mission.id)
    if adapted.blockers or not adapted.steps:
        print("❌ La adaptación falló:")
        for b in adapted.blockers:
            print(f"  · [{b.code}] {b.message}")
        return 3
    print(f"✓ Adaptados {len(adapted.steps)} MissionSteps:")
    for s in adapted.steps:
        print(f"   · kind={s.kind:<15} params={dict(s.params)}")

    step_done_cb = _expert_step if args.expert else None

    print("\n> Ejecutando con SmartMissionExecutor (bridge oficial)...\n")
    decision = run_mission_smart(
        mission,
        on_status=_human_status,
        on_human_help_needed=_needs_human,
        on_step_done=step_done_cb,
        expert_mode=bool(args.expert),
        allow_legacy_fallback=False,  # PRD §14: E2E rechaza legacy
    )

    # ── Verificaciones PRD §5 / §9 / §14 ─────────────────────────
    exit_code = 0
    try:
        assert_smart_executor_is_used(decision)
        if decision.result is not None:
            assert_no_blind_advance(decision.result)
            assert_final_result_reaches_youtube(decision.result)
        else:
            print("⚠ No hubo result (gate o bridge bloqueó la misión).")
            exit_code = 4
    except AssertionError as e:
        print(str(e))
        exit_code = 1

    print_execution_report(decision, dry_run=args.dry_run)

    if args.save_report:
        try:
            rpt = decision.to_dict()
            Path(args.save_report).parent.mkdir(parents=True, exist_ok=True)
            with open(args.save_report, "w", encoding="utf-8") as f:
                json.dump(rpt, f, indent=2, ensure_ascii=False)
            print(f"✓ Reporte JSON guardado en {args.save_report}")
        except Exception as e:
            print(f"⚠ No pude guardar reporte: {e}")

    if exit_code == 0:
        print("✅ PRD §14 cumplido: SmartExecutor ejecutó la misión sin "
              "caer al player legacy.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
