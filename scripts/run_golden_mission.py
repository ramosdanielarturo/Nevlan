#!/usr/bin/env python3
"""FASE 0 — Golden mission runner (gate CI/local).

Ejecuta la misión golden canónica y exporta JSON con coords_used,
weakest_step, status, duration_ms y runtime_flags activos.

Uso::

    python scripts/run_golden_mission.py
    python scripts/run_golden_mission.py --mode dry-run
    python scripts/run_golden_mission.py --mode live

Exit codes:
    0 — gate FASE 0 OK
    1 — run bloqueado / gate no ejecutable
    2 — violación gate FASE 0 (drift, SEP, telemetry, smart_route, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.runtime.execution_run_audit import EXECUTION_RUN_AUDIT_JSONL
from app.services.runtime.golden_mission_runner import (
    default_export_path,
    latest_export_path,
    run_golden_mission,
)
from app.services.runtime.nevlan_runtime_convergence import fase0_gate_violations


def main() -> int:
    parser = argparse.ArgumentParser(description="FASE 0 golden mission runner")
    parser.add_argument(
        "--mode",
        choices=("dry-run", "stub", "gate", "live"),
        default="dry-run",
        help="dry-run=replay+bridge sin I/O (default gate); stub=debug; live=escritorio",
    )
    parser.add_argument(
        "--export",
        nargs="?",
        const="auto",
        default="auto",
        help="Ruta JSON de salida (default: var/runtime/golden_runner/golden_run_latest.json)",
    )
    parser.add_argument(
        "--no-prod-profile",
        action="store_true",
        help="No forzar perfil convergencia prod (sólo debug)",
    )
    parser.add_argument(
        "--allow-drift",
        action="store_true",
        help="No fallar si hay drift de flags vs canónico",
    )
    parser.add_argument(
        "--skip-gate-checks",
        action="store_true",
        help="Sólo exportar reporte; no aplicar fase0_gate_violations",
    )
    args = parser.parse_args()

    export_path = None
    if args.export:
        export_path = (
            latest_export_path()
            if args.export == "auto"
            else Path(args.export)
        )

    report = run_golden_mission(
        mode=args.mode,
        prod_profile=not args.no_prod_profile,
        export_path=export_path,
    )

    blob = report.to_dict()
    print(json.dumps(blob, ensure_ascii=False, indent=2))

    if export_path:
        ts_path = default_export_path()
        ts_path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nexported: {export_path}", file=sys.stderr)
        print(f"timestamped: {ts_path}", file=sys.stderr)

    if report.blocked or report.status in ("gate_blocked", "failed", "stopped_for_human"):
        return 1

    if args.mode == "gate" and not report.gate_executable:
        return 1

    if args.skip_gate_checks:
        return 0

    violations = list(fase0_gate_violations(
        blob,
        audit_jsonl=EXECUTION_RUN_AUDIT_JSONL,
        run_id=str(report.run_id or ""),
        require_representative_mode=(args.mode != "gate"),
    ))

    if args.allow_drift:
        violations = [v for v in violations if not v.startswith("profile_drift")]

    if violations:
        print("\nFASE0 GATE VIOLATIONS:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
