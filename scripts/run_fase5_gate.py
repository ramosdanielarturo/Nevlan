#!/usr/bin/env python3
"""FASE 5 — Runtime determinista (gate).

Gate: 3× replay golden dry-run; diff estrategias por paso = vacío;
coords_used=false; smart_route_count=0.

Uso::

    python scripts/run_fase5_gate.py
    python scripts/run_fase5_gate.py --replays 3 --report json

Exit codes:
    0 — gate FASE 5 OK
    1 — determinismo REJECTED
    2 — violación gate FASE 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.runtime.fase5_deterministic_gate import (
    FASE5_REQUIRED_REPLAYS,
    fase5_gate_violations,
)
from app.services.runtime.fase5_replay_runner import (
    default_export_path,
    latest_export_path,
    run_fase5_golden_replay,
    write_fase5_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="FASE 5 deterministic runtime gate")
    parser.add_argument(
        "--replays",
        type=int,
        default=FASE5_REQUIRED_REPLAYS,
        help="Replays consecutivos (default: 3)",
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run", "live"),
        default="dry-run",
        help="dry-run=CI sin I/O; live=escritorio real",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Timeout por replay en segundos (live; 0=BETA_RUN_TIMEOUT_SEC)",
    )
    parser.add_argument(
        "--report",
        choices=("json", "none"),
        default="json",
    )
    parser.add_argument(
        "--export",
        nargs="?",
        const="auto",
        default="auto",
    )
    parser.add_argument(
        "--no-prod-profile",
        action="store_true",
    )
    parser.add_argument(
        "--skip-gate-checks",
        action="store_true",
    )
    args = parser.parse_args()

    export_path = latest_export_path() if args.export == "auto" else Path(args.export)

    report = run_fase5_golden_replay(
        mode=args.mode,
        replays=args.replays,
        prod_profile=not args.no_prod_profile,
        export_path=None,
        timeout_sec=args.timeout,
    )
    blob = report.to_dict()

    if args.report == "json":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(json.dumps(blob, ensure_ascii=False, indent=2))

    if args.export:
        write_fase5_report(report, export_path)
        ts_path = default_export_path()
        write_fase5_report(report, ts_path)
        print(f"\nexported: {export_path}", file=sys.stderr)
        print(f"timestamped: {ts_path}", file=sys.stderr)

    if report.determinism_status != "ACCEPTED":
        return 1 if args.skip_gate_checks else 1

    if args.skip_gate_checks:
        return 0

    violations = list(fase5_gate_violations(blob, required_replays=args.replays))
    if violations:
        print("\nFASE5 GATE VIOLATIONS:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
