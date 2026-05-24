"""FASE 7 — baseline certificado congelado (release runtime).

Congelado tras suite default live 5×5 escenarios (25/25 ACCEPTED).
"""

from __future__ import annotations

from pathlib import Path

FASE7_CERTIFIED_RELEASE_ID = "fase7-runtime-v1"
FASE7_CERTIFIED_DATE = "2026-05-24"
FASE7_CERTIFIED_SUITE = "default"
FASE7_CERTIFIED_MODE = "live"
FASE7_CERTIFIED_REQUIRED_RUNS = 5
FASE7_CERTIFIED_SCENARIO_COUNT = 5
FASE7_CERTIFIED_TOTAL_RUNS = 25

CERTIFIED_LIVE_BASELINE_PATH = (
    Path(__file__).resolve().parent
    / "fase7_benchmark"
    / "fase7_certified_live_baseline.json"
)

__all__ = [
    "CERTIFIED_LIVE_BASELINE_PATH",
    "FASE7_CERTIFIED_DATE",
    "FASE7_CERTIFIED_MODE",
    "FASE7_CERTIFIED_RELEASE_ID",
    "FASE7_CERTIFIED_REQUIRED_RUNS",
    "FASE7_CERTIFIED_SCENARIO_COUNT",
    "FASE7_CERTIFIED_SUITE",
    "FASE7_CERTIFIED_TOTAL_RUNS",
]
