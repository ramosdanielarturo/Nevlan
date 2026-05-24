"""
Microbenchmarks opcionales Nevlan (manual; no endurecer CI con umbrales fijos).

Uso::
  python scripts/nevlan_microbench.py

Salida::
  Consola + ``var/performance_summary.txt`` + ``var/performance_summary.json``
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


WARN_COMPILE_200_MS = 900.0
WARN_GROUP_SEMANTIC_200_MS = 120.0
WARN_QUALITY_LOOP_200_MS = 250.0
WARN_SYNTH_EXEC_MS = 400.0


def _ensure_repo_path() -> Path:
    repo = Path(__file__).resolve().parents[1]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    return repo


def _fake_trace(n: int):
    from app.contracts.mission import (
        EventType,
        Mission,
        MissionStatus,
        MouseAction,
        RawEvent,
    )

    m = Mission(name="bench", status=MissionStatus.DRAFT)
    m.raw_trace = [
        RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=i % 500, y=i % 500, button="left"),
        )
        for i in range(n)
    ]
    return m


def _p95_ms(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(0.95 * (len(s) - 1))))
    return float(s[idx])


def _run() -> Dict[str, Any]:
    _ensure_repo_path()
    from app.services.missions.compiler import MissionCompiler
    from app.services.missions.rerecord_display import build_rerecord_display
    from app.services.missions.step_quality import compute_step_quality

    rows: Dict[str, Any] = {}

    for n in (50, 200):
        m = _fake_trace(n)
        t0 = time.perf_counter()
        MissionCompiler.compile_mission(m)
        ms = (time.perf_counter() - t0) * 1000.0
        key = f"compile_{n}"
        entry: Dict[str, Any] = {
            "total_ms": round(ms, 2),
            "ms_per_step": round(ms / max(1, n), 4),
        }
        if n == 200 and ms > WARN_COMPILE_200_MS:
            entry["warn"] = f"compile 200 > {WARN_COMPILE_200_MS} ms"
        rows[key] = entry

    m200 = _fake_trace(200)
    MissionCompiler.compile_mission(m200)
    t0 = time.perf_counter()
    _ = MissionCompiler._build_semantic_action_groups(  # noqa: SLF001
        m200.compiled_execution_graph,
        m200.interpreted_steps,
    )
    gms = (time.perf_counter() - t0) * 1000.0
    g_ent: Dict[str, Any] = {"total_ms": round(gms, 2)}
    if gms > WARN_GROUP_SEMANTIC_200_MS:
        g_ent["warn"] = f"grouping 200 > {WARN_GROUP_SEMANTIC_200_MS} ms"
    rows["group_semantic_200"] = g_ent

    steps200 = m200.compiled_execution_graph
    interp200 = m200.interpreted_steps
    t0 = time.perf_counter()
    for c, i in zip(steps200, interp200):
        compute_step_quality(c, i)
    qms = (time.perf_counter() - t0) * 1000.0
    q_ent: Dict[str, Any] = {
        "total_ms": round(qms, 2),
        "ms_per_step": round(qms / max(1, len(steps200)), 4),
    }
    if qms > WARN_QUALITY_LOOP_200_MS:
        q_ent["warn"] = f"quality loop 200 > {WARN_QUALITY_LOOP_200_MS} ms"
    rows["quality_scoring_200"] = q_ent

    evs = (_fake_trace(200).raw_trace) or []
    t0 = time.perf_counter()
    build_rerecord_display(evs, expert=False)
    build_rerecord_display(evs, expert=True)
    rms = (time.perf_counter() - t0) * 1000.0
    rows["rerecord_display_200_twice_normal_expert"] = {"total_ms": round(rms, 2)}

    latencies: List[float] = []
    t_loop = time.perf_counter()
    for i in range(200):
        t1 = time.perf_counter()
        _ = bool(steps200[i % len(steps200)].target_context.fallback_coords)
        latencies.append((time.perf_counter() - t1) * 1000.0)
    loop_ms = (time.perf_counter() - t_loop) * 1000.0
    syn: Dict[str, Any] = {
        "total_ms": round(loop_ms, 2),
        "p50_ms": round(statistics.median(latencies), 6) if latencies else 0.0,
        "p95_ms": round(_p95_ms(latencies), 6),
    }
    if loop_ms > WARN_SYNTH_EXEC_MS:
        syn["warn"] = (
            f"synthetic loop overhead high > {WARN_SYNTH_EXEC_MS} ms "
            "(CPU/carga local)"
        )
    rows["synthetic_exec_loop_200"] = syn

    try:
        from app.services.missions.execution_ui import friendly_resolver_summary

        n, x = friendly_resolver_summary(
            "coordinate_icon_validated_click",
            {"visual_match_score": 0.91, "region_used": "bbox"},
        )
        rows["coordinate_icon_label_sanity"] = {"normal": n, "expert_sample": x}
    except Exception as e:
        rows["coordinate_icon_label_sanity"] = {"error": str(e)[:120]}

    rows["_thresholds_hint"] = {
        "warn_compile_200_ms": WARN_COMPILE_200_MS,
        "warn_group_semantic_200_ms": WARN_GROUP_SEMANTIC_200_MS,
        "warn_quality_loop_200_ms": WARN_QUALITY_LOOP_200_MS,
        "warn_synthetic_exec_ms": WARN_SYNTH_EXEC_MS,
    }
    return rows


def main() -> None:
    import os

    ap = argparse.ArgumentParser(description="Nevlan microbench (rendimiento local)")
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Muestra logs técnicos del store/comtypes (por defecto silenciados)",
    )
    args = ap.parse_args()
    if args.verbose:
        os.environ.pop("NEVLAN_QUIET_BENCH", None)
    else:
        os.environ["NEVLAN_QUIET_BENCH"] = "1"
        logging.disable(logging.CRITICAL)

    data = _run()
    for k, v in data.items():
        if not k.startswith("_"):
            print(f"{k}: {v}")
    out_txt = Path("var") / "performance_summary.txt"
    out_json = Path("var") / "performance_summary.json"
    try:
        out_txt.parent.mkdir(parents=True, exist_ok=True)
        rendered = []
        for k, v in sorted(data.items()):
            rendered.append(f"{k} = {json.dumps(v, ensure_ascii=False)}")
        out_txt.write_text("\n".join(rendered) + "\n", encoding="utf-8")
        out_json.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("", flush=True)
        print(f"Wrote {out_txt} and {out_json}", flush=True)
    except OSError:
        print("(No se pudo crear var/, resultados sólo en consola.)")


if __name__ == "__main__":
    main()
