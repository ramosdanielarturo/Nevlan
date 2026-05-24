"""Export de paquete de informes beta privada → ``var/reports/beta_private/``."""

from __future__ import annotations

import datetime as _dt
import json
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from app.core.config import PROJECT_ROOT


def beta_private_report_root(*, timestamp: Optional[str] = None) -> Path:
    ts = timestamp or _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = PROJECT_ROOT / "var" / "reports" / "beta_private" / ts
    base.mkdir(parents=True, exist_ok=True)
    (base / "failures").mkdir(exist_ok=True)
    return base


def write_beta_private_bundle(
    *,
    summary_json: Mapping[str, Any],
    markdown_extras: str = "",
    mission_serial: Optional[Mapping[str, Any]] = None,
    recommended_fixes: Optional[List[str]] = None,
    timestamp: Optional[str] = None,
) -> Path:
    """Escribe summary.json / summary.md y misión opcional."""

    root = beta_private_report_root(timestamp=timestamp)
    summ = dict(summary_json)
    if recommended_fixes is not None:
        summ.setdefault("recommended_fixes", recommended_fixes)

    (root / "summary.json").write_text(
        json.dumps(summ, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    oss = summary_json.get("one_shot_reliability_report")
    mh = summary_json.get("mission_health_snapshot")
    lat = summary_json.get("runtime_latency_report")
    telem = summary_json.get("telemetry")
    accept = summary_json.get("beta_private_acceptance_snapshot")

    md_lines = [
        "## Resumen ejecutivo Nevlan Beta Private",
        "",
        f"- **Score aceptación:** `{accept}`",
        f"- **Mission health:** `{mh}`",
        f"- **One-shot reliability:** `{oss}`",
        f"- **Latencia:** `{lat}`",
        f"- **Telemetría:** `{list((telem or {}).keys()) if isinstance(telem, dict) else telem}`",
        "",
        "### Secciones requeridas",
        "",
        "| Sección | Archivo |",
        "|---------|---------|",
        "| summary | summary.md",
        "| summary_json | summary.json",
        "| mission_reports | mission_reports/mission.json (si existe misión)",
        "| latency | resumen dentro de summary.json → runtime_latency_report",
        "| telemetry | resumen dentro de summary.json → telemetry",
        "| failures | failures/ (screenshots opcionales)",
        "| recommended fixes | summary.json.recommended_fixes",
        "",
        "### Escenarios checklist (máquina real)",
        "",
        textwrap.indent(
            textwrap.dedent(
                """
                launcher → navegador → búsqueda → resultados
                abrir / guardar archivo
                completar formulario
                confirmar diálogo
                descarga
                cambio de layout manual (re-layout)
                interrupción por popup emergente
                """
            ).strip(),
            "- ",
        ),
        "",
        "### Notas extras",
        "",
        textwrap.indent(markdown_extras.strip() or "(ninguna)", ""),
    ]
    (root / "summary.md").write_text("\n".join(md_lines), encoding="utf-8")

    if mission_serial:
        mr = root / "mission_reports"
        mr.mkdir(exist_ok=True)
        (mr / "mission.json").write_text(
            json.dumps(mission_serial, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return root


__all__ = ["beta_private_report_root", "write_beta_private_bundle"]
