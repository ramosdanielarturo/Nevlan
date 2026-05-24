#!/usr/bin/env python3
"""Agrega resultados del CSV de apps reales (manual) a resúmenes JSON/TXT.

Uso::
  python scripts/summarize_real_app_results.py [--csv path]

Escribe ``var/real_app_results_summary.json`` y ``.txt`` si ``var/`` existe.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _score_cell(v: str) -> float:
    v = (v or "").strip().lower()
    mapping = {
        "0": 0, "1": 1, "2": 2, "3": 3,
        "falla": 0, "parcial": 1, "ok": 2, "excelente": 3,
        "sí": 2, "si": 2, "no": 0, "": -1,
    }
    if v in mapping:
        return float(mapping[v])
    try:
        return float(v)
    except ValueError:
        return -1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--csv",
        default=str(_REPO / "scripts" / "results_template.csv"),
        help="CSV con columnas del checklist",
    )
    args = ap.parse_args()
    src = Path(args.csv)
    if not src.is_file():
        print(f"No existe {src}", file=sys.stderr)
        sys.exit(1)

    rows = []
    with src.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(dict(row))

    by_app: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        app = (row.get("app") or "").strip()
        if not app:
            continue
        g = _score_cell(row.get("grabó_bien") or row.get("grabó_correctamente") or "")
        a = _score_cell(row.get("agrupó_bien") or row.get("agrupó_correctamente") or "")
        e = _score_cell(row.get("ejecutó_bien") or row.get("ejecutó_correctamente") or "")
        vals = [x for x in (g, a, e) if x >= 0]
        if vals:
            by_app[app].append(sum(vals) / len(vals))

    summary = {
        "source_csv": str(src),
        "rows_read": len(rows),
        "precision_by_app_avg": {
            k: round(sum(v) / max(1, len(v)), 3) for k, v in by_app.items()
        },
        "notes": "Scoring 0–3 por celda cuando la columna está rellenada.",
    }

    out_dir = _REPO / "var"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "real_app_results_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"app: avg_score = {summary['precision_by_app_avg'].get(app)}"
        for app in sorted(summary["precision_by_app_avg"])
    ]
    (out_dir / "real_app_results_summary.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    print("\n".join(lines))
    print(f"\nWrote {out_dir / 'real_app_results_summary.json'}")


if __name__ == "__main__":
    main()
