"""Mission health score — compuesto determinista sobre señales existentes."""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.services.missions.execution_truth_engine import compute_execution_truth
from app.services.missions.mission_truth_gate import gate_decision
from app.services.missions.semantic_ambiguity_engine import (
    aggregate_plan_ambiguity,
    evaluate_plan_semantic_ambiguity,
)
from app.services.missions.semantic_execution_plan import SemanticExecutionPlan
from app.services.missions.semantic_review_display import plan_declares_coords_used


def compute_mission_health_score(
    mission: Any,
    *,
    persist: bool = False,
) -> Dict[str, Any]:
    """Devuelve ``mission_health_score`` 0–1 y factores auditables."""
    factors: Dict[str, float] = {}

    gd = gate_decision(mission)
    factors["gate_executable"] = 1.0 if gd.is_executable else 0.25

    et = compute_execution_truth(mission)
    factors["execution_truth"] = 1.0 if et.confirmed else 0.35

    sep = getattr(mission, "semantic_execution_plan", None) or {}
    steps = [s for s in (sep.get("steps") or []) if isinstance(s, dict)]
    coords_used = plan_declares_coords_used(mission)
    if coords_used is True:
        factors["coords_dependency"] = 0.2
    elif coords_used is False:
        factors["coords_dependency"] = 1.0
    else:
        factors["coords_dependency"] = 0.85

    amb_score = 0.75
    try:
        if steps:
            plan = SemanticExecutionPlan.from_dict(sep if isinstance(sep, dict) else {})
            agg = aggregate_plan_ambiguity(evaluate_plan_semantic_ambiguity(plan, mission))
            amb_score = max(0.0, 1.0 - float(agg.ambiguity_score or 0.0))
            if agg.requires_human_confirmation:
                amb_score *= 0.65
    except Exception:
        amb_score = 0.5
    factors["ambiguity_inverse"] = amb_score

    lop_c = 0.55
    snap = getattr(mission, "lop_last_snapshot", None)
    if isinstance(snap, dict) and snap:
        try:
            lop_c = float(snap.get("confidence") or 0.55)
            if snap.get("safe_to_continue") is False:
                lop_c *= 0.7
        except Exception:
            lop_c = 0.45
    factors["lop_confidence"] = max(0.0, min(1.0, lop_c))

    org = getattr(mission, "operational_runtime_graph", None)
    org_edges = 0
    if isinstance(org, dict):
        org_edges = len(org.get("edges") or org.get("transitions") or []) if isinstance(
            org.get("edges") or org.get("transitions"),
            list,
        ) else 0
    factors["recovery_coverage"] = min(1.0, 0.35 + 0.05 * min(org_edges, 12))

    arl_a = getattr(mission, "adaptive_runtime_audit", None) or {}
    ev = arl_a.get("events") if isinstance(arl_a, dict) else None
    arl_n = len(ev) if isinstance(ev, list) else 0
    factors["arl_noise_inverse"] = max(0.35, 1.0 - min(0.5, 0.03 * arl_n))

    oss = getattr(mission, "one_shot_reliability_report", None) or {}
    oss_s = 0.65
    if isinstance(oss, dict) and oss.get("one_shot_reliability_score") is not None:
        try:
            oss_s = float(oss["one_shot_reliability_score"])
        except Exception:
            oss_s = 0.65
    factors["one_shot_score"] = oss_s

    weights = {
        "gate_executable": 0.14,
        "execution_truth": 0.16,
        "coords_dependency": 0.12,
        "ambiguity_inverse": 0.14,
        "lop_confidence": 0.1,
        "recovery_coverage": 0.1,
        "arl_noise_inverse": 0.08,
        "one_shot_score": 0.16,
    }
    score = sum(weights[k] * factors[k] for k in weights)

    out: Dict[str, Any] = {
        "version": 1,
        "mission_health_score": round(max(0.0, min(1.0, score)), 4),
        "factors": factors,
        "weights": weights,
        "is_executable": bool(gd.is_executable),
    }
    if persist:
        try:
            mission.mission_health_snapshot = dict(out)
        except Exception:
            pass
    return out


__all__ = ["compute_mission_health_score"]
