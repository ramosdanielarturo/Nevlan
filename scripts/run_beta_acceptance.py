#!/usr/bin/env python3
"""Ejecuta benchmark beta runtime y genera BetaRuntimeAcceptanceReport.

Uso::

    python scripts/run_beta_acceptance.py --mission var/missions/<id>.json
    python scripts/run_beta_acceptance.py --mission var/missions/<id>.json --runs 5
    python scripts/run_beta_acceptance.py --mission var/missions/<id>.json --evaluate-only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.contracts.mission import mission_from_dict
from app.core.config import Settings, invalidate_settings_cache
from app.services.runtime.beta_runtime_acceptance import (
    BetaRuntimeAcceptanceStatus,
    build_and_persist_beta_runtime_acceptance,
    compact_summary,
    compute_beta_runtime_acceptance_report,
    enable_beta_runtime_profile,
    persist_beta_runtime_acceptance_report,
)


def _load_mission(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return mission_from_dict(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Beta runtime acceptance benchmark")
    parser.add_argument(
        "--mission",
        required=True,
        help="Ruta al JSON de misión (var/missions/...)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Repeticiones consecutivas (meta: 5 para execute-always)",
    )
    parser.add_argument(
        "--evaluate-only",
        action="store_true",
        help="Sólo evaluar telemetría existente; no ejecutar SmartExecutor",
    )
    parser.add_argument(
        "--arss",
        action="store_true",
        help="Activar ARSS en perfil beta para esta ejecución",
    )
    parser.add_argument(
        "--run-timeout-sec",
        type=int,
        default=0,
        help="Timeout por run (0 = usar BETA_RUN_TIMEOUT_SEC del settings)",
    )
    args = parser.parse_args()

    mission_path = Path(args.mission)
    if not mission_path.is_file():
        print(f"ERROR: misión no encontrada: {mission_path}")
        return 2

    settings = Settings(BETA_PRIVATE_RUNTIME_PROFILE=True)
    enable_beta_runtime_profile(settings, arss_enabled=bool(args.arss))
    invalidate_settings_cache()

    mission = _load_mission(mission_path)
    reports = []

    if not args.evaluate_only:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        from app.services.missions.smart_runner_bridge import run_mission_smart

        run_timeout = int(args.run_timeout_sec or getattr(settings, "BETA_RUN_TIMEOUT_SEC", 600))

        for i in range(max(1, args.runs)):
            print(f"--- Run {i + 1}/{args.runs} mission={mission.id} ---")
            decision = None
            timed_out = False
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(
                    run_mission_smart,
                    mission,
                    expert_mode=True,
                    allow_legacy_fallback=False,
                    use_auto_learning=False,
                )
                try:
                    decision = fut.result(timeout=run_timeout)
                except FuturesTimeout:
                    timed_out = True
                    print(
                        f"[RUN_TIMEOUT] run excedió {run_timeout}s — "
                        "marcando NEEDS_HARDENING (ENTITY_RUNTIME_TIMEOUT)."
                    )
                    try:
                        from app.services.missions.uol_runtime_telemetry import (
                            UolStepTelemetryRecord,
                            append_uol_runtime_record,
                        )

                        append_uol_runtime_record(
                            mission,
                            UolStepTelemetryRecord(
                                step_id="benchmark:run_timeout",
                                uol_action="entity_runtime",
                                failure_reason="ENTITY_RUNTIME_TIMEOUT",
                                handler_success=False,
                                validation_passed=False,
                                latency_ms=run_timeout * 1000,
                            ),
                        )
                    except Exception:
                        pass

            if timed_out:
                report = build_and_persist_beta_runtime_acceptance_report(
                    mission,
                    run_id=f"beta-run-timeout-{i + 1}",
                    mission_result=None,
                )
                reports.append(report)
                print(compact_summary(report))
                continue

            run_id = getattr(decision.result, "run_id", "") if decision.result else ""
            report = build_and_persist_beta_runtime_acceptance(
                mission,
                run_id=str(run_id or f"beta-run-{i + 1}"),
                mission_result=decision.result,
            )
            reports.append(report)
            print(compact_summary(report))
            if report.status == BetaRuntimeAcceptanceStatus.REJECTED:
                print("REJECTED — deteniendo benchmark.")
                break
    else:
        report = compute_beta_runtime_acceptance_report(mission)
        report = persist_beta_runtime_acceptance_report(mission, report)
        reports.append(report)
        print(compact_summary(report))

    final = reports[-1] if reports else compute_beta_runtime_acceptance_report(mission)
    accepted_runs = sum(1 for r in reports if r.status == BetaRuntimeAcceptanceStatus.ACCEPTED)
    print("")
    print(f"Final: {final.status.value}")
    print(f"Accepted runs: {accepted_runs}/{len(reports)}")
    print(f"Next: {final.recommended_next_action}")
    if final.weakest_steps:
        w = final.weakest_steps[0]
        print(f"Weakest: {w.get('step_id')} ({w.get('source')})")

    try:
        out_path = (
            _ROOT / "var" / "runtime" / "beta_acceptance" / f"{mission.id}_latest_summary.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(final.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Report: {out_path}")
    except Exception as exc:
        print(f"WARN: no se pudo escribir resumen: {exc}")

    if final.status == BetaRuntimeAcceptanceStatus.ACCEPTED and accepted_runs >= min(5, args.runs):
        return 0
    if final.status == BetaRuntimeAcceptanceStatus.NEEDS_HARDENING:
        return 1
    if final.status == BetaRuntimeAcceptanceStatus.REJECTED:
        return 2
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
