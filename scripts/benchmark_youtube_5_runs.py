"""
Benchmark real — 5 corridas de la misión YouTube con AutoLearningExecutor
========================================================================

Corre la **misma** misión cinco veces consecutivas en tu máquina (Chrome
real + redes reales + DOM/UIA reales) y compara las métricas exigidas
por el PRD §15:

    Run 1     vs     Run 5
    -----     vs     -----
    retries          retries
    intervenciones   intervenciones
    tiempo total     tiempo total
    quality_score    quality_score

Diseño:

* Reutilizamos ``scripts.test_smart_executor_youtube.build_example_mission()``
  para tener la misma misión semántica que ya validamos.
* El bridge ``run_mission_smart(...)`` por default usa el
  ``AutoLearningExecutor``, así que cada corrida sucesiva aprovecha la
  memoria persistida en ``var/missions/learning/execution_memory.db``.
* Después de cada corrida imprimimos: status, retries totales, ms,
  quality, intervenciones humanas y resumen humano.
* Al final, imprimimos una tabla comparativa y un veredicto:
      ✓ MEJORA si retries(run5) ≤ retries(run1)
      ✓ MEJORA si quality(run5) ≥ quality(run1)
      ✓ MEJORA si duration(run5) ≤ duration(run1)
* Opcionalmente vuelca el reporte en JSON con ``--save-report``.

Uso (Chrome real, requiere desktop disponible):

    python scripts/benchmark_youtube_5_runs.py
    python scripts/benchmark_youtube_5_runs.py --runs 5 --save-report bench.json
    python scripts/benchmark_youtube_5_runs.py --reset      # borra memoria antes

Importante:

* Si la misión queda *bloqueada* (gate, perfil vacío, etc.) el script
  aborta inmediatamente — eso es un blocker humano, no un fallo del
  benchmark.
* No corre si tu DISPLAY/Chrome no están listos. En ese caso, usa el
  test determinista::

      python -m pytest tests/unit/test_youtube_5_runs_benchmark.py -v -s
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# Añadimos repo root al path para correr el script directamente.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.missions.smart_runner_bridge import (  # noqa: E402
    run_mission_smart,
    RunnerDecision,
)


def _build_mission():
    """Reusa la misma misión semántica que ya validamos en E2E."""
    from scripts.test_smart_executor_youtube import build_example_mission
    return build_example_mission()


def _print_run_header(idx: int) -> None:
    print()
    print("═" * 70)
    print(f"  RUN {idx} / N  ")
    print("═" * 70)


def _interventions_count(decision: RunnerDecision) -> int:
    """Aproxima cuántas veces el executor pidió ayuda humana."""
    if not decision.result:
        return 0
    return sum(
        1 for o in decision.result.outcomes
        if getattr(o, "status", "") == "needs_human"
    )


def _retries_count(decision: RunnerDecision) -> int:
    if not decision.result:
        return 0
    return sum(int(getattr(o, "retries", 0) or 0) for o in decision.result.outcomes)


def _quality_score(decision: RunnerDecision) -> float:
    qr = decision.quality_report
    if qr is None:
        return 0.0
    return float(getattr(qr, "quality_score", 0.0) or 0.0)


def _quality_summary(decision: RunnerDecision) -> str:
    qr = decision.quality_report
    if qr is None:
        return "<sin auto-learning>"
    return getattr(qr, "user_facing_summary", "") or ""


def _print_run_summary(idx: int, decision: RunnerDecision, elapsed_ms: int) -> None:
    print()
    print(f"--- Resumen run {idx} ---")
    print(f"  player_used        = {decision.player_used}")
    print(f"  reason             = {decision.reason}")
    print(f"  auto_learning      = {decision.auto_learning_enabled}")
    print(f"  status             = "
          f"{decision.result.status if decision.result else '<n/a>'}")
    print(f"  retries (total)    = {_retries_count(decision)}")
    print(f"  intervenciones     = {_interventions_count(decision)}")
    print(f"  duration           = {elapsed_ms} ms")
    print(f"  quality            = {_quality_score(decision):.2f}")
    print(f"  quality_summary    = {_quality_summary(decision)}")
    if decision.suggestions:
        print(f"  suggestions        = {len(decision.suggestions)}")
        for s in decision.suggestions[:3]:
            try:
                print(f"     · {s.message}")
            except Exception:
                pass


def _final_table(metrics: List[Dict[str, Any]]) -> None:
    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║   CURVA DE APRENDIZAJE — 5 RUNS                                   ║")
    print("╠═══════╤═════════╤══════════════╤═══════════╤═════════════════════╣")
    print("║  Run  │ Retries │ Intervenciones │ Duración (ms) │  Calidad        ║")
    print("╠═══════╪═════════╪══════════════╪═══════════╪═════════════════════╣")
    for m in metrics:
        print(f"║ {m['run']:>3}   │  {m['retries']:>5}  │   {m['interventions']:>10}   "
              f"│   {m['duration_ms']:>7}    │     {m['quality']:.2f}            ║")
    print("╚═══════╧═════════╧══════════════╧═══════════╧═════════════════════╝")

    first = metrics[0]
    last = metrics[-1]

    def _verdict(label: str, ok: bool, val_a: Any, val_b: Any) -> None:
        sign = "OK" if ok else "WARN"
        print(f"  [{sign}] {label}: run1={val_a}, runN={val_b}")

    print()
    print("Veredicto:")
    _verdict("retries", last["retries"] <= first["retries"],
             first["retries"], last["retries"])
    _verdict("intervenciones", last["interventions"] <= first["interventions"],
             first["interventions"], last["interventions"])
    _verdict("duración (ms)", last["duration_ms"] <= first["duration_ms"],
             first["duration_ms"], last["duration_ms"])
    _verdict("calidad", last["quality"] >= first["quality"],
             round(first["quality"], 2), round(last["quality"], 2))


def _reset_learning() -> None:
    """Borra la memoria de ejecución para empezar limpio."""
    from app.services.missions.execution_memory_store import (
        get_execution_memory_store,
    )
    store = get_execution_memory_store()
    store.forget_all()
    print("[reset] Memoria de ejecución borrada (var/missions/learning/).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs", type=int, default=5,
        help="Cuántas corridas hacer (default 5).",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Borrar memoria de aprendizaje antes de arrancar.",
    )
    parser.add_argument(
        "--save-report", type=str, default="",
        help="Ruta JSON para volcar las métricas detalladas.",
    )
    parser.add_argument(
        "--expert", action="store_true",
        help="Imprime cada step done con strategy/retries.",
    )
    args = parser.parse_args()

    # UTF-8 en stdout/stderr para evitar problemas en Windows.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.reset:
        _reset_learning()

    metrics: List[Dict[str, Any]] = []

    for i in range(1, args.runs + 1):
        _print_run_header(i)
        mission = _build_mission()

        def _on_status(msg: str) -> None:
            print(f"[run {i}] · {msg}")

        def _on_step_done(o):  # type: ignore[no-untyped-def]
            if not args.expert:
                return
            print(
                f"   step={o.kind:<15} "
                f"strategy={o.strategy_used or '?':<15} "
                f"retries={o.retries} "
                f"duration={o.duration_ms}ms "
                f"status={o.status}"
            )

        t0 = time.time()
        decision = run_mission_smart(
            mission,
            on_status=_on_status,
            on_step_done=_on_step_done,
            allow_legacy_fallback=False,
            expert_mode=args.expert,
        )
        elapsed_ms = int((time.time() - t0) * 1000)

        if decision.blocked:
            print()
            print(f"❌ run {i} BLOQUEADO: {decision.reason}")
            for b in decision.blockers:
                print(f"   · [{b.code}] {b.message}")
            print("Aborta benchmark — primero resuelve los blockers humanos.")
            return 2

        _print_run_summary(i, decision, elapsed_ms)

        metrics.append({
            "run": i,
            "status": decision.result.status if decision.result else None,
            "retries": _retries_count(decision),
            "interventions": _interventions_count(decision),
            "duration_ms": elapsed_ms,
            "quality": _quality_score(decision),
            "summary": _quality_summary(decision),
            "suggestions_count": len(decision.suggestions or []),
        })

    _final_table(metrics)

    if args.save_report:
        path = Path(args.save_report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "runs": metrics,
            "first": metrics[0],
            "last": metrics[-1],
            "improvement": {
                "retries_delta": metrics[-1]["retries"] - metrics[0]["retries"],
                "duration_delta_ms": (
                    metrics[-1]["duration_ms"] - metrics[0]["duration_ms"]
                ),
                "quality_delta": (
                    metrics[-1]["quality"] - metrics[0]["quality"]
                ),
            },
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nReporte guardado en {path.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
