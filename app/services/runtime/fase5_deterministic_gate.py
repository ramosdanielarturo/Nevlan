"""FASE 5 — Runtime determinista (gate).

Objetivo: misma misión → mismo path runtime.

Criterios:
  5.1 — SmartExecutor + UOL; MissionPlayer legacy fuera del path prod
  5.2 — smart_route nunca primario
  5.3 — coords nunca primarios (audit coords_used=false)
  5.4 — 3 replays consecutivos: misma secuencia de estrategias por paso
  5.5 — smart_runner_bridge único entrypoint (sin forks legacy silenciosos)
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.services.runtime.nevlan_runtime_convergence import is_forbidden_primary_strategy

FASE5_REQUIRED_REPLAYS = 3
FASE5_RUNTIME_ENTRYPOINT = "smart_runner_bridge"
FASE5_ACCEPTANCE_STATUS = "ACCEPTED"
FASE5_REJECTION_STATUS = "REJECTED"


def is_fase5_deterministic_runtime_active(settings: Any) -> bool:
    """True si el perfil prod/convergencia exige path determinista FASE 5."""
    if bool(getattr(settings, "FASE5_DETERMINISTIC_RUNTIME", True)):
        return True
    import os

    if os.environ.get("NEVLAN_FORCE_CONVERGENCE_PROFILE") == "1":
        return True
    env = str(getattr(settings, "ENV", "") or "").lower()
    return env in ("prod", "production")


def apply_fase5_prod_runtime_policy(
    settings: Any,
    *,
    allow_legacy_fallback: bool,
    use_auto_learning: bool,
    force_deterministic_replay: bool = False,
) -> Tuple[bool, bool]:
    """FASE 5.1/5.5 — prod: sin MissionPlayer legacy en path default."""
    if not is_fase5_deterministic_runtime_active(settings):
        return allow_legacy_fallback, use_auto_learning
    allow_legacy_fallback = False
    if force_deterministic_replay:
        use_auto_learning = False
    return allow_legacy_fallback, use_auto_learning


def extract_strategy_path(
    step_strategies: Sequence[Mapping[str, Any]],
) -> List[Dict[str, str]]:
    """Secuencia normalizada (step_id, kind, strategy_used) por run."""
    path: List[Dict[str, str]] = []
    for step in step_strategies:
        path.append(
            {
                "step_id": str(step.get("step_id") or ""),
                "kind": str(step.get("kind") or ""),
                "strategy_used": str(step.get("strategy_used") or ""),
            }
        )
    return path


def _diff_extracted_paths(
    paths: Sequence[Sequence[Mapping[str, Any]]],
    *,
    label: str,
) -> List[str]:
    if not paths:
        return ["strategy_path_missing"]
    if len(paths) < 2:
        return []

    ref = list(paths[0])
    diffs: List[str] = []
    for run_idx, current in enumerate(paths[1:], start=2):
        current = list(current)
        if len(current) != len(ref):
            diffs.append(
                f"run{run_idx}_length:{len(current)}!={len(ref)}",
            )
        for step_idx, (a, b) in enumerate(zip(ref, current)):
            a_strat = str(a.get("strategy_used") or "")
            b_strat = str(b.get("strategy_used") or "")
            if a_strat != b_strat:
                a_raw = str(a.get("raw_strategy_used") or a_strat)
                b_raw = str(b.get("raw_strategy_used") or b_strat)
                if label == "normalized" and a_raw != b_raw:
                    diffs.append(
                        f"run{run_idx}_step{step_idx}:"
                        f"{a.get('step_id')} "
                        f"{a_strat!r}!={b_strat!r} "
                        f"(raw:{a_raw!r}!={b_raw!r})",
                    )
                else:
                    diffs.append(
                        f"run{run_idx}_step{step_idx}:"
                        f"{a.get('step_id')} "
                        f"{a_strat!r}!={b_strat!r}",
                    )
        if len(current) > len(ref):
            for extra in current[len(ref) :]:
                diffs.append(
                    f"run{run_idx}_extra_step:{extra.get('step_id')}",
                )
    return diffs


def diff_strategy_paths(
    paths: Sequence[Sequence[Mapping[str, Any]]],
) -> List[str]:
    """Diff raw entre replays — informativo; conserva divergencias reales."""
    normalized = [extract_strategy_path(p) for p in paths]
    return _diff_extracted_paths(normalized, label="raw")


def diff_normalized_strategy_paths(
    paths: Sequence[Sequence[Mapping[str, Any]]],
) -> List[str]:
    """Diff normalizado — criterio de gate FASE 5."""
    return _diff_extracted_paths(paths, label="normalized")


def count_forbidden_primary_in_path(
    step_strategies: Sequence[Mapping[str, Any]],
) -> int:
    return sum(
        1
        for step in step_strategies
        if is_forbidden_primary_strategy(str(step.get("strategy_used") or ""))
    )


def count_forbidden_preferred_in_mission(mission: Any) -> int:
    """Cuenta preferred_strategy prohibidas en SEP (5.2/5.3 preflight)."""
    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict):
        return 0
    steps = sep.get("steps") or []
    count = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        pref = str(step.get("preferred_strategy") or "")
        if is_forbidden_primary_strategy(pref):
            count += 1
    return count


def fase5_gate_violations(
    report: Mapping[str, Any],
    *,
    required_replays: int = FASE5_REQUIRED_REPLAYS,
) -> List[str]:
    """Condiciones que hacen fallar ``make fase5-gate``."""
    violations: List[str] = []
    runs = list(report.get("runs") or [])

    if len(runs) < required_replays:
        violations.append(f"insufficient_replays:{len(runs)}<{required_replays}")

    if "strategy_path_diff_normalized" in report:
        path_diff_norm = list(report.get("strategy_path_diff_normalized") or [])
    else:
        path_diff_norm = list(report.get("strategy_path_diff") or [])
    if path_diff_norm:
        violations.append(
            f"strategy_path_diff_normalized:{';'.join(path_diff_norm[:4])}",
        )

    if int(report.get("smart_route_count") or 0) > 0:
        violations.append("smart_route_nonzero")

    if report.get("coords_used"):
        violations.append("coords_used")

    if str(report.get("runtime_entrypoint") or "") != FASE5_RUNTIME_ENTRYPOINT:
        violations.append(
            f"runtime_entrypoint:{report.get('runtime_entrypoint')!r}",
        )

    for run in runs:
        idx = run.get("run_index", "?")
        if str(run.get("player_used") or "") == "legacy":
            violations.append(f"legacy_player_run:{idx}")
        if run.get("legacy_fork"):
            violations.append(f"legacy_fork_run:{idx}")
        if run.get("coords_used"):
            violations.append(f"coords_used_run:{idx}")
        sr = int(run.get("smart_route_count") or 0)
        if sr > 0:
            violations.append(f"smart_route_run:{idx}:{sr}")

    forbidden_pref = int(report.get("forbidden_preferred_count") or 0)
    if forbidden_pref > 0:
        violations.append(f"forbidden_preferred_in_sep:{forbidden_pref}")

    if str(report.get("determinism_status") or "") != FASE5_ACCEPTANCE_STATUS:
        violations.append(
            f"determinism_status:{report.get('determinism_status')!r}",
        )

    reset_failures = int(report.get("environment_reset_failures") or 0)
    if reset_failures > 0:
        violations.append(f"environment_reset_failures:{reset_failures}")

    return violations


__all__ = [
    "FASE5_ACCEPTANCE_STATUS",
    "FASE5_REJECTION_STATUS",
    "FASE5_REQUIRED_REPLAYS",
    "FASE5_RUNTIME_ENTRYPOINT",
    "apply_fase5_prod_runtime_policy",
    "count_forbidden_preferred_in_mission",
    "count_forbidden_primary_in_path",
    "diff_normalized_strategy_paths",
    "diff_strategy_paths",
    "extract_strategy_path",
    "fase5_gate_violations",
    "is_fase5_deterministic_runtime_active",
]
