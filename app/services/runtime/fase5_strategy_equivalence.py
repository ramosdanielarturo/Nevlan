"""FASE 5 — equivalencia controlada de strategy paths.

Dos estrategias raw pueden normalizarse a ``contract_satisfied:no_op`` solo
cuando el resultado contractual es equivalente y está validado.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.services.runtime.nevlan_runtime_convergence import is_forbidden_primary_strategy

CONTRACT_SATISFIED_NO_OP = "contract_satisfied:no_op"
ORCE_MIN_CONSISTENCY = 0.90

_BROWSE_SCROLL_KINDS = frozenset({"scroll_results", "browse_results"})
_OK_STEP_STATUSES = frozenset({"success", "skipped"})
_ALREADY_SATISFIED_RE = re.compile(r"(^skip:already_satisfied$|^.+:already_satisfied$)")


def is_already_satisfied_strategy(strategy: str) -> bool:
    s = (strategy or "").strip()
    return bool(s and _ALREADY_SATISFIED_RE.match(s))


def _step_validation_ok(step: Mapping[str, Any]) -> bool:
    if bool(step.get("validation_passed")):
        return True
    score = float(step.get("orce_consistency_score") or 0.0)
    if score >= ORCE_MIN_CONSISTENCY:
        return True
    status = str(step.get("status") or "")
    strategy = str(step.get("strategy_used") or "")
    if status == "success":
        return True
    if status == "skipped" and is_already_satisfied_strategy(strategy):
        return bool(step.get("contract_satisfied"))
    return False


def _step_has_forbidden_degradation(
    step: Mapping[str, Any],
    *,
    run_coords_used: bool = False,
) -> bool:
    if run_coords_used or bool(step.get("coords_used_step")):
        return True
    if bool(step.get("smart_route_used")):
        return True
    strategy = str(step.get("strategy_used") or "")
    return is_forbidden_primary_strategy(strategy)


def _browse_scroll_skip_allowed(step: Mapping[str, Any]) -> bool:
    if str(step.get("kind") or "") not in _BROWSE_SCROLL_KINDS:
        return False
    if not is_already_satisfied_strategy(str(step.get("strategy_used") or "")):
        return False
    if not bool(step.get("contract_satisfied")):
        return False
    if str(step.get("failure_reason") or "").strip():
        return False
    if not _step_validation_ok(step):
        return False
    score = float(step.get("orce_consistency_score") or 0.0)
    if score >= ORCE_MIN_CONSISTENCY:
        return True
    if bool(step.get("validation_passed")):
        return True
    if str(step.get("status") or "") == "skipped":
        return True
    return False


def normalize_step_strategy(
    step: Mapping[str, Any],
    *,
    run_coords_used: bool = False,
) -> Tuple[str, str]:
    """Devuelve ``(normalized_strategy, equivalence_reason)``."""
    raw = str(step.get("strategy_used") or "")
    kind = str(step.get("kind") or "")
    status = str(step.get("status") or "")
    failure = str(step.get("failure_reason") or "").strip()

    if status not in _OK_STEP_STATUSES:
        return raw, ""
    if failure:
        return raw, ""
    if _step_has_forbidden_degradation(step, run_coords_used=run_coords_used):
        return raw, ""
    if not bool(step.get("contract_satisfied", False)):
        return raw, ""
    if not _step_validation_ok(step):
        return raw, ""

    if is_already_satisfied_strategy(raw):
        if kind in _BROWSE_SCROLL_KINDS:
            if not _browse_scroll_skip_allowed(step):
                return raw, ""
            return CONTRACT_SATISFIED_NO_OP, "browse_scroll_skip_contract_validated"
        return CONTRACT_SATISFIED_NO_OP, "already_satisfied_contract_validated"

    if kind in _BROWSE_SCROLL_KINDS and status == "success":
        return CONTRACT_SATISFIED_NO_OP, "browse_scroll_executed_contract_satisfied"

    return raw, ""


def extract_normalized_strategy_path(
    step_strategies: Sequence[Mapping[str, Any]],
    *,
    run_coords_used: bool = False,
) -> List[Dict[str, str]]:
    path: List[Dict[str, str]] = []
    for step in step_strategies:
        normalized, reason = normalize_step_strategy(
            step,
            run_coords_used=run_coords_used,
        )
        entry: Dict[str, str] = {
            "step_id": str(step.get("step_id") or ""),
            "kind": str(step.get("kind") or ""),
            "strategy_used": normalized,
            "raw_strategy_used": str(step.get("strategy_used") or ""),
        }
        if reason:
            entry["equivalence_reason"] = reason
        path.append(entry)
    return path


def enrich_step_strategies_for_equivalence(
    step_strategies: Sequence[Mapping[str, Any]],
    *,
    mission: Any = None,
    decision: Any = None,
) -> List[Dict[str, Any]]:
    """Fusiona telemetría UOL/ORCE y outcomes para validar equivalencia."""
    uol_by_id: Dict[str, Dict[str, Any]] = {}
    for row in getattr(mission, "uol_runtime_audit", None) or []:
        if isinstance(row, dict) and row.get("step_id"):
            uol_by_id[str(row["step_id"])] = row

    orce_by_id: Dict[str, Dict[str, Any]] = {}
    orce_report = getattr(mission, "operational_runtime_consistency_report", None) or {}
    if isinstance(orce_report, dict):
        for summary in orce_report.get("step_summaries") or []:
            if isinstance(summary, dict) and summary.get("step_id"):
                orce_by_id[str(summary["step_id"])] = summary

    outcomes_by_id: Dict[str, Any] = {}
    result = getattr(decision, "result", None) if decision is not None else None
    for outcome in getattr(result, "outcomes", None) or []:
        sid = str(getattr(outcome, "step_id", "") or "")
        if sid:
            outcomes_by_id[sid] = outcome

    enriched: List[Dict[str, Any]] = []
    for step in step_strategies:
        sd = dict(step)
        sid = str(sd.get("step_id") or "")
        uol = uol_by_id.get(sid) or {}
        orce = orce_by_id.get(sid) or {}
        outcome = outcomes_by_id.get(sid)

        sd["validation_passed"] = bool(uol.get("validation_passed"))
        sd["orce_consistency_score"] = float(orce.get("consistency_score") or 0.0)
        sd["failure_reason"] = str(
            uol.get("failure_reason")
            or (getattr(outcome, "error", "") if outcome is not None else "")
            or "",
        )
        sd["coords_used_step"] = bool(uol.get("coords_used"))
        sd["smart_route_used"] = bool(uol.get("smart_route_used"))

        status = str(sd.get("status") or "")
        strategy = str(sd.get("strategy_used") or "")
        if status == "success" and not sd["validation_passed"]:
            sd["validation_passed"] = True
        if status in _OK_STEP_STATUSES and not sd["failure_reason"]:
            if is_already_satisfied_strategy(strategy) or status == "success":
                sd["contract_satisfied"] = True
            else:
                sd["contract_satisfied"] = bool(sd["validation_passed"])
        else:
            sd["contract_satisfied"] = False

        enriched.append(sd)
    return enriched


__all__ = [
    "CONTRACT_SATISFIED_NO_OP",
    "ORCE_MIN_CONSISTENCY",
    "enrich_step_strategies_for_equivalence",
    "extract_normalized_strategy_path",
    "is_already_satisfied_strategy",
    "normalize_step_strategy",
]
