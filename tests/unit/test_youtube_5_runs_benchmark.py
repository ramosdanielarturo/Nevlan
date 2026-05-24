"""
Benchmark determinista — la misión "abrir YouTube y buscar" mejora a lo
largo de 5 ejecuciones consecutivas.

Esto NO sustituye una prueba E2E con Chrome real (que se ejerce con
``scripts/benchmark_youtube.py``), pero verifica que el **algoritmo**
del Auto-Learning Executor produce la curva esperada por el PRD §15:

  Run 1:  más reintentos, menos calidad, con intervención humana posible.
  Run 5:  cero reintentos, cero intervenciones, calidad alta.

Para hacerlo determinista simulamos un ``SmartMissionExecutor`` cuyo
comportamiento depende del aprendizaje acumulado:

  * En run 1, el strategy ranker devuelve ``[dom_input, uia_searchbox, ocr_search]``
    porque no hay historia → el simulador hace que ``dom_input`` falle
    (selector estale), ``uia_searchbox`` también falla, y finalmente
    ``ocr_search`` triunfa con 1 retry y duración alta.
  * En runs sucesivos, el ranker (basado en los datos persistidos)
    debería reordenar para que ``ocr_search`` o ``uia_searchbox`` queden
    primero — el simulador respeta el orden propuesto y por eso los
    siguientes runs son más rápidos y sin retries.

El test asserta:
  * runs[0].retries > runs[4].retries
  * runs[0].duration_ms > runs[4].duration_ms
  * runs[0].quality_score < runs[4].quality_score
  * runs[4].quality_score >= 0.85
  * runs[4].retries == 0
  * runs[4].interventions == 0
"""
from __future__ import annotations

import time
from typing import Dict, List
from unittest.mock import patch

import pytest


def _step_youtube_open():
    from app.services.missions.execution_contracts import MissionStep
    return MissionStep(
        id="s_open", kind="open_url",
        params={"alias": "youtube", "domain": "youtube.com",
                "app": "chrome.exe"},
    )


def _step_youtube_search():
    from app.services.missions.execution_contracts import MissionStep
    return MissionStep(
        id="s_search", kind="search_youtube",
        params={"query": "luis miguel", "domain": "youtube.com",
                "app": "chrome.exe", "semantic_target": "youtube_search_box"},
    )


class _AdaptiveSmartFake:
    """SmartMissionExecutor sintético cuyo comportamiento depende del
    historial conocido por el LearningLogger inyectado.

    Reglas:
      - Para ``open_url``:
          * 1ª vez: 1 fail (bookmark_click) + 1 success (omnibox_url) = 1 retry
          * Siguientes: la ranker pondrá omnibox_url primero (más score) → 0 retries
      - Para ``search_*`` YouTube (``search_content`` / ``search_site`` /
        ``search_youtube`` legacy):
          * 1ª vez: dom_input falla (stale selector) + uia_searchbox falla + ocr_search OK
          * Siguientes: el TargetMemory/ranker pondrá ocr_search o uia_searchbox al frente → 0 retries
    """

    def __init__(self, run_id, learning, on_status, on_human_help_needed,
                 on_step_done, expert_mode, **_kw):
        self.run_id = run_id
        self.learning = learning
        self.on_step_done = on_step_done
        self.on_status = on_status

    def run_mission(self, steps, **_kwargs):
        from app.services.missions.smart_executor import (
            MissionResult, StepOutcome,
        )
        result = MissionResult(run_id=self.run_id, status="success")
        for step in steps:
            outcome = self._simulate_step(step)
            if self.on_step_done:
                self.on_step_done(outcome)
            result.outcomes.append(outcome)
        result.finished_at = time.time()
        return result

    def _simulate_step(self, step):
        """Devuelve un StepOutcome consistente con el ranker actual."""
        from app.services.missions.smart_executor import StepOutcome
        kind = step.kind

        if kind == "open_url":
            ranked = self.learning.rank_strategies(
                "open_url", ["bookmark_click", "omnibox_url"],
            )
            winner_strategy, retries = self._first_success(
                ranked,
                success_set={"omnibox_url"},
                fail_set={"bookmark_click"},
            )
            base_duration = 200 if winner_strategy == "omnibox_url" else 1500
            duration = base_duration + (retries * 1500)
            return StepOutcome(
                step_id=step.id, kind=kind, status="success",
                strategy_used=winner_strategy,
                retries=retries, duration_ms=duration,
                state_after={
                    "active_app": "chrome.exe",
                    "browser_url": "https://www.youtube.com/",
                    "browser_domain": "youtube.com",
                },
            )

        if kind in ("search_youtube", "search_content", "search_site"):
            ranked = self.learning.rank_strategies(
                "search_youtube", ["dom_input", "uia_searchbox", "ocr_search"],
            )
            winner_strategy, retries = self._first_success(
                ranked,
                success_set={"ocr_search", "uia_searchbox"},
                fail_set={"dom_input"},  # dom_input fallará por "stale selector"
            )
            base_duration = (
                200 if winner_strategy == "uia_searchbox"
                else (300 if winner_strategy == "ocr_search" else 1800)
            )
            duration = base_duration + (retries * 2000)
            return StepOutcome(
                step_id=step.id, kind=kind, status="success",
                strategy_used=winner_strategy,
                retries=retries, duration_ms=duration,
                state_after={
                    "active_app": "chrome.exe",
                    "browser_url": "https://www.youtube.com/results?search_query=x",
                    "browser_domain": "youtube.com",
                },
            )

        # Default: success directo.
        return StepOutcome(
            step_id=step.id, kind=kind, status="success",
            strategy_used="default", retries=0, duration_ms=100,
            state_after={"active_app": "chrome.exe"},
        )

    def _first_success(self, ranked, *, success_set, fail_set):
        """Recorre el ranking; cuenta como retry cada estrategia que
        está en fail_set y devuelve la primera de success_set."""
        retries = 0
        for s in ranked:
            if s in fail_set:
                retries += 1
                continue
            if s in success_set:
                return s, retries
        # Si nada matchea (debug), devolvemos la última.
        return ranked[-1], retries


def _build_executor(mission_id):
    from app.services.missions.auto_learning_executor import AutoLearningExecutor
    return AutoLearningExecutor(
        mission_id=mission_id,
        # Modo privado garantiza que cada test parte limpio.
        # El learning logger efímero NO persiste entre invocaciones.
        # Por eso debemos reusar la MISMA instancia entre runs.
    )


def test_5_consecutive_runs_show_improvement_curve(tmp_path):
    """PRD §15: 5 ejecuciones de la misma misión convergen a un óptimo.

    Se reutiliza la MISMA instancia del executor para que el aprendizaje
    persista en el LearningLogger interno del SmartMissionExecutor fake.
    """
    from app.services.missions.auto_learning_executor import (
        AutoLearningExecutor, _make_ephemeral_learning_logger,
    )
    from app.services.missions import auto_learning_executor as ale_mod
    from app.services.missions.execution_memory_store import ExecutionMemoryStore

    # Store persistente en tmp_path (no usamos private_mode porque el
    # espejo al LearningLogger se desactiva en ese modo, y necesitamos
    # que cada run actualice las stats que consumirá el siguiente).
    shared_store = ExecutionMemoryStore(
        db_path=tmp_path / "bench.db",
        audit_jsonl=tmp_path / "bench.jsonl",
    )
    # LearningLogger efímero compartido entre los 5 runs. Garantiza que
    # el run 1 parta de stats vacías (para que el ranker no esté
    # contaminado con datos de otros tests del proceso).
    shared_learning = _make_ephemeral_learning_logger()
    # Conectamos el store al logger efímero para que el espejo escriba
    # ahí (no al global).
    shared_store._learning = shared_learning

    metrics = []
    with patch.object(ale_mod, "SmartMissionExecutor", _AdaptiveSmartFake):
        for i in range(5):
            executor = AutoLearningExecutor(
                mission_id="mission_youtube_search",
                memory_store=shared_store,
                learning=shared_learning,
                run_id=f"run_{i + 1}",
            )
            steps = [_step_youtube_open(), _step_youtube_search()]
            result = executor.run_mission(steps)
            quality = (
                result.quality_report.quality_score
                if result.quality_report else 0.0
            )
            total_retries = sum(o.retries for o in result.mission_result.outcomes)
            interventions = sum(
                1 for o in result.mission_result.outcomes
                if o.status == "needs_human"
            )
            duration_ms = sum(o.duration_ms for o in result.mission_result.outcomes)
            metrics.append({
                "run_index": i + 1,
                "retries": total_retries,
                "interventions": interventions,
                "duration_ms": duration_ms,
                "quality": quality,
                "summary": (
                    result.quality_report.user_facing_summary
                    if result.quality_report else ""
                ),
            })

    # Imprimimos para que aparezca en la salida del test (fácil debug).
    print("\n=== Curva de aprendizaje (PRD §15) ===")
    for m in metrics:
        print(
            f"  run {m['run_index']}: "
            f"retries={m['retries']}, "
            f"intervenciones={m['interventions']}, "
            f"duration={m['duration_ms']}ms, "
            f"quality={m['quality']:.2f}"
        )
        print(f"           {m['summary']}")

    # ── Asserts del PRD §15 ────────────────────────────────────
    first = metrics[0]
    last = metrics[-1]

    # Run 1 tiene MÁS retries que run 5
    assert first["retries"] > last["retries"], (
        f"esperaba retries(run1)>retries(run5); "
        f"run1={first['retries']} run5={last['retries']}"
    )
    # Run 1 tarda MÁS que run 5
    assert first["duration_ms"] > last["duration_ms"], (
        f"esperaba duration(run1)>duration(run5); "
        f"run1={first['duration_ms']}ms run5={last['duration_ms']}ms"
    )
    # Run 1 tiene calidad PEOR que run 5
    assert first["quality"] < last["quality"], (
        f"esperaba quality(run1)<quality(run5); "
        f"run1={first['quality']:.2f} run5={last['quality']:.2f}"
    )
    # Run 5: 0 retries y 0 intervenciones
    assert last["retries"] == 0, (
        f"esperaba retries(run5)==0, fue {last['retries']}"
    )
    assert last["interventions"] == 0, (
        f"esperaba interventions(run5)==0, fue {last['interventions']}"
    )
    # Run 5: calidad >= 0.85
    assert last["quality"] >= 0.85, (
        f"esperaba quality(run5)>=0.85, fue {last['quality']:.2f}"
    )

    # Y la promoción de estrategia: en el último run, la ranker debe
    # poner las estrategias ganadoras al frente.
    final_search_ranking = executor.scoring.rank_strategies(
        "search_youtube",
        ["dom_input", "uia_searchbox", "ocr_search"],
        scope_app="chrome.exe", scope_domain="youtube.com",
        scope_target="youtube_search_box",
    )
    assert final_search_ranking[0] in ("uia_searchbox", "ocr_search"), (
        f"el ranker no promovió la estrategia ganadora; "
        f"final ranking = {final_search_ranking}"
    )

    final_open_ranking = executor.scoring.rank_strategies(
        "open_url", ["bookmark_click", "omnibox_url"],
    )
    assert final_open_ranking[0] == "omnibox_url", (
        f"esperaba omnibox_url primero; final ranking = {final_open_ranking}"
    )
