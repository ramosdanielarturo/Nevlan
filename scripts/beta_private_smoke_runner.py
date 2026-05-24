#!/usr/bin/env python3
"""Checklist guiado smoke beta Nevlan — empaquetado de evidencia sin UI.

Este script NO sustituye E2E con PyQt ni Win32 grabado en vivo:
documenta checklist y exporta snapshots/dictos para auditoría reproducible.

Uso típico (golden mission reproducible)::

    python scripts/beta_private_smoke_runner.py --golden --export

Checklist sólo texto::

    python scripts/beta_private_smoke_runner.py --checklist-only
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from typing import Any, Dict

from app.services.missions.mission_review_summary import build_mission_review_summary
from app.services.missions.mission_truth_gate import gate_decision
from app.services.runtime.beta_private_acceptance_score import compute_beta_private_acceptance_score
from app.services.runtime.beta_private_report_export import write_beta_private_bundle
from app.services.runtime.full_rerecord_guard import (
    mission_corruption_severe_for_full_rerecord,
    should_block_full_rerecord,
)
from app.services.runtime.runtime_production_telemetry import (
    record_runtime_event,
    summarize_telemetry,
    telemetry_snapshot,
)


def _print_checklist() -> None:
    print(
        """\
Nevlan — Beta Privada Smoke (máquina real)
=========================================

1. Grabar misión nueva (launcher → navegador → buscar → resultados como mínimo).
2. Abrir Mission Review normal — confirmar estados sólo-producto sin tokens técnicos.
3. Abrir Mission Review experto — auditoría SEP/LOP/ARL visible.
4. Ejecutar misión hasta fin o parada NEEDS_HUMAN esperada.
5. Verificar archivo misión tiene one_shot_reliability_report.
6. Verificar runtime_latency_report (RUNTIME_LATENCY_REPORT_ENABLED).
7. Verificar mission_health_snapshot (RELIABILITY_HARDENING_ENABLED).
8. Capturar production_telemetry desde mission.runtime_production_telemetry.

Escenarios recomendados:
- lanzador → navegador → búsqueda → resultados
- abrir / guardar archivo
- rellenar formulario
- confirmación de diálogo
- descarga
- cambio de layout manual (re-layout deliberado)
- popup interrumpiendo el flujo
"""
    )


def run_golden_export():
    from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
        build_golden_search_chrome_youtube_mission,
    )

    mission = build_golden_search_chrome_youtube_mission(name="beta-smoke")
    gate_decision(mission)

    summary_canonical = asdict(build_mission_review_summary(mission))
    expert_signals = {
        "adaptive_runtime_audit": getattr(mission, "adaptive_runtime_audit", None),
        "lop_last_snapshot": getattr(mission, "lop_last_snapshot", None),
        "one_shot_reliability_report": getattr(mission, "one_shot_reliability_report", None),
    }

    from app.services.missions.mission_health_score import compute_mission_health_score

    mission.one_shot_reliability_report = {
        "one_shot_reliability_score": 0.74,
        "preflight_outcome": "READY_TO_EXECUTE",
        "runtime_mode": "shadow_safe",
        "plan_source": "SEP_SIPE_CANONICAL",
    }
    h = compute_mission_health_score(mission, persist=True)
    corrupted, _why = mission_corruption_severe_for_full_rerecord(mission)
    block_full, _bz = should_block_full_rerecord(mission, reliability_hardening=True)

    record_runtime_event(mission, kind="self_healing_phase", payload={"phase": "semantic_relocation", "order": 1})
    record_runtime_event(mission, kind="self_healing_phase", payload={"phase": "micro_repair", "order": 9})
    record_runtime_event(mission, kind="repair_suggestion_offer", payload={"stub": True})

    from app.services.runtime.runtime_latency_budget import RuntimeLatencyCollector, build_runtime_latency_report

    lc = RuntimeLatencyCollector()
    lc.mark_start("one_shot_preflight")
    lc.mark_end("one_shot_preflight")
    lc.mark_start("lop_refresh")
    lc.mark_end("lop_refresh")
    lc.mark_start("smart_executor_run")
    lc.mark_end("smart_executor_run")
    latency = build_runtime_latency_report(lc, semantic_step_count=8)

    telem_snap = summarize_telemetry(telemetry_snapshot(mission))

    acceptance = compute_beta_private_acceptance_score(mission, mission_result=None)

    fixes = []
    if not latency.get("budget_ok"):
        fixes.append("Revisar runtime_latency_report.budget_exceeded")
    if corrupted:
        fixes.append("Corrupción severa detectada por heurística: sólo ahí amplía re-record")
    elif block_full:
        fixes.append("Política: preferir micro-re-record de bloque (ver full_rerecord_guard.py)")

    blob: Dict[str, Any] = {
        "scenario": "golden_canonical_bundle",
        "mission_review_summary": summary_canonical,
        "mission_review_expert_signals": expert_signals,
        "mission_health_snapshot": h,
        "one_shot_reliability_report": mission.one_shot_reliability_report,
        "runtime_latency_report": latency,
        "telemetry": telem_snap,
        "beta_private_acceptance_snapshot": acceptance,
        "corruption_audit": {"corrupted_severe_for_full_rerecord": corrupted},
        "rerecord_policy": {"block_full_when_hardened": block_full},
    }

    md_extra = "**Mission ID:** `{}` · visible_blockers(norm)={}".format(
        getattr(mission, "id", "?"),
        len((summary_canonical.get("visible_blockers") or [])),
    )

    serial = mission.model_dump(mode="json")
    root = write_beta_private_bundle(
        summary_json=blob,
        markdown_extras=md_extra,
        mission_serial=serial,
        recommended_fixes=fixes or ["Ningún fix automático (smoke nominal)"],
    )
    return root


def main() -> None:
    p = argparse.ArgumentParser(description="Nevlan beta private smoke exporter")
    p.add_argument("--checklist-only", action="store_true")
    p.add_argument("--golden", action="store_true", help="Usa golden mission + export reproducible")
    p.add_argument("--export", action="store_true")
    ns = p.parse_args()

    if ns.checklist_only:
        _print_checklist()
        return

    if ns.golden and ns.export:
        path = run_golden_export()
        print(f"exported to {path}")
        return

    _print_checklist()


if __name__ == "__main__":
    main()
