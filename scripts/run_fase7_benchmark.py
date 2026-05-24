#!/usr/bin/env python3
"""FASE 7 — Benchmark real (gate de release).

Uso::

    python scripts/run_fase7_benchmark.py --suite default --runs 5 --mode dry-run
    python scripts/run_fase7_benchmark.py --mission chrome_youtube_launcher --runs 5
    python scripts/run_fase7_benchmark.py --mission var/missions/foo.json --runs 5 --timeout 600

Exit codes:
    0 — gate FASE 7 OK (5/5 ACCEPTED, coords=0, smart_route=0)
    1 — benchmark incompleto / escenario REJECTED
    2 — violación gate FASE 7
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.runtime.fase7_benchmark_runner import (
    default_export_path,
    latest_export_path,
    run_fase7_benchmark,
    run_fase7_suite,
    write_fase7_report,
)
from app.services.runtime.fase7_release_gate import (
    FASE7_REQUIRED_CONSECUTIVE_RUNS,
    fase7_batch_gate_violations,
    fase7_gate_violations,
)


def _resolve_mission(scenario_or_mission: str):
    from tests.fixtures.fase7_missions import resolve_fase7_mission

    return resolve_fase7_mission(scenario_or_mission)


def _scenario_index():
    from tests.fixtures.fase7_missions import FASE7_SCENARIO_BY_ID

    return FASE7_SCENARIO_BY_ID


def main() -> int:
    parser = argparse.ArgumentParser(description="FASE 7 benchmark runner (release gate)")
    parser.add_argument(
        "--mission",
        help="Id de escenario FASE 7 o ruta JSON de misión",
    )
    parser.add_argument(
        "--suite",
        default="",
        help="Suite de producto (default = 5 escenarios si no hay --mission)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=FASE7_REQUIRED_CONSECUTIVE_RUNS,
        help="Corridas consecutivas por escenario (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Timeout por run en segundos (0 = BETA_RUN_TIMEOUT_SEC)",
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run", "live"),
        default="dry-run",
        help="dry-run=infra CI sin I/O; live=escritorio real",
    )
    parser.add_argument(
        "--report",
        choices=("json", "none"),
        default="json",
        help="Formato de salida (default: json stdout)",
    )
    parser.add_argument(
        "--export",
        nargs="?",
        const="auto",
        default="auto",
        help="Ruta JSON (default: var/runtime/fase7_benchmark/fase7_run_latest.json)",
    )
    parser.add_argument(
        "--no-prod-profile",
        action="store_true",
        help="No forzar perfil convergencia prod",
    )
    parser.add_argument(
        "--skip-gate-checks",
        action="store_true",
        help="Sólo exportar reporte; no aplicar gate FASE 7",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="Lista escenarios de la suite default",
    )
    args = parser.parse_args()

    if args.list_scenarios:
        for sid, spec in _scenario_index().items():
            print(f"{sid}\t{spec.category}\t{spec.description}")
        return 0

    prod = not args.no_prod_profile
    export_path = latest_export_path() if args.export == "auto" else Path(args.export)

    if args.mission:
        mission = _resolve_mission(args.mission)
        spec = _scenario_index().get(args.mission.strip())
        report = run_fase7_benchmark(
            mission,
            scenario_id=spec.scenario_id if spec else args.mission.strip(),
            category=spec.category if spec else "",
            description=spec.description if spec else "",
            mode=args.mode,
            runs=args.runs,
            timeout_sec=args.timeout,
            prod_profile=prod,
        )
        blob = report.to_dict()
        gate_fn = fase7_gate_violations
    else:
        suite_name = args.suite or "default"
        batch = run_fase7_suite(
            mode=args.mode,
            runs=args.runs,
            timeout_sec=args.timeout,
            prod_profile=prod,
            suite_name=suite_name,
        )
        blob = batch.to_dict()
        gate_fn = fase7_batch_gate_violations

    if args.report == "json":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(json.dumps(blob, ensure_ascii=False, indent=2))

    if args.export:
        write_fase7_report(blob, export_path)
        ts_path = default_export_path()
        write_fase7_report(blob, ts_path)
        print(f"\nexported: {export_path}", file=sys.stderr)
        print(f"timestamped: {ts_path}", file=sys.stderr)

    release_status = str(blob.get("release_status") or "")
    if release_status != "ACCEPTED":
        if args.skip_gate_checks:
            return 0
        return 1

    if args.skip_gate_checks:
        return 0

    violations = list(gate_fn(blob, required_runs=args.runs))
    if violations:
        print("\nFASE7 GATE VIOLATIONS:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
