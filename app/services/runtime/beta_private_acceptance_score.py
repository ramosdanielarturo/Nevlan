"""Puntuación de aceptación beta privada Nevlan — sólo evidencia local (sin backends)."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from app.services.runtime.runtime_production_telemetry import summarize_telemetry, telemetry_snapshot

BetaAcceptState = Literal["READY_FOR_PRIVATE_BETA", "NEEDS_HARDENING", "UNSAFE_FOR_BETA"]


def compute_beta_private_acceptance_score(
    mission: Any,
    *,
    mission_result: Optional[Any] = None,
) -> Dict[str, Any]:
    """Agrega factores existentes para decidir READY / NEEDS / UNSAFE."""

    mh = getattr(mission, "mission_health_snapshot", None) or {}
    oss = getattr(mission, "one_shot_reliability_report", None) or {}
    lat = getattr(mission, "runtime_latency_report", None) or {}
    tail = telemetry_snapshot(mission)
    telem = summarize_telemetry(tail)

    mission_health = float(mh.get("mission_health_score") or 0.0)
    one_shot = float(oss.get("one_shot_reliability_score") or 0.65)

    success_rate = 1.0
    repair_rate = 0.0
    if mission_result is not None:
        st = str(getattr(mission_result, "status", "") or "")
        success_rate = 1.0 if st == "success" else (0.5 if st == "stopped_for_human" else 0.0)

    tel_kinds = dict(telem.get("kinds") or {})
    heal_n = int(tel_kinds.get("self_healing_phase", 0))
    repair_n = int(telem.get("repair_signals") or 0)

    fallback_pressure = float(
        tel_kinds.get("fallback_pressure_hint", 0)
        + tel_kinds.get("smart_route_fallback", 0),
    )

    clarification_false_proxy = float(tel_kinds.get("possible_false_clarification", 0))
    latency_score = 1.0 if bool(lat.get("budget_ok", True)) else 0.45
    latency_exceeded: List[str] = []
    if isinstance(lat, dict):
        latency_exceeded = list(lat.get("budget_exceeded") or [])

    unsafe_stop_correct = float(
        1.0
        if (not bool(mh.get("is_executable")) and clarification_false_proxy == 0)
        else 0.75
    )

    self_healing_success = min(1.0, 0.55 + 0.08 * min(heal_n, 5))
    if repair_n > 0:
        self_healing_success *= max(0.35, 1.0 - 0.12 * min(repair_n, 6))

    repair_rate = min(1.0, 0.15 * float(repair_n))

    user_visible_noise = max(
        0.0,
        min(
            1.0,
            0.04 * len(latency_exceeded)
            + 0.02 * int(telem.get("arl_signals") or 0)
            + 0.03 * float(telem.get("unsafe_signals") or 0),
        ),
    )

    composite = (
        0.18 * mission_health
        + 0.16 * one_shot
        + 0.14 * success_rate
        + 0.10 * (1.0 - min(1.0, repair_rate * 3.0))
        + 0.10 * unsafe_stop_correct
        + 0.10 * latency_score
        + 0.08 * (1.0 - min(1.0, clarification_false_proxy * 0.5))
        + 0.06 * (1.0 - min(1.0, fallback_pressure * 0.2))
        + 0.05 * self_healing_success
        + 0.03 * (1.0 - user_visible_noise)
    )
    composite = max(0.0, min(1.0, float(composite)))

    preflight_o = str(oss.get("preflight_outcome") or "").upper()
    state: BetaAcceptState
    unsupported_rt = (
        mh.get("is_executable") is False
        and str(oss.get("runtime_mode") or "").upper().startswith("UNSUPPORTED")
    )
    if (
        preflight_o in ("UNSAFE_STOP", "UNSUPPORTED_PLAN_SOURCE")
        or mission_health < 0.22
        or unsupported_rt
    ):
        state = "UNSAFE_FOR_BETA"
    elif composite >= 0.78 and latency_score >= 0.9 and mission_health >= 0.62:
        state = "READY_FOR_PRIVATE_BETA"
    else:
        state = "NEEDS_HARDENING"

    factors = {
        "mission_health_score": mission_health,
        "one_shot_reliability_score": one_shot,
        "success_rate": success_rate,
        "repair_rate": repair_rate,
        "unsafe_stop_correctness": unsafe_stop_correct,
        "latency_score": latency_score,
        "latency_budget_items": latency_exceeded,
        "false_clarification_signals": clarification_false_proxy,
        "fallback_pressure": fallback_pressure,
        "self_healing_success": self_healing_success,
        "user_visible_noise_score": user_visible_noise,
        "telemetry_event_count": float(telem.get("event_count") or 0),
    }

    return {
        "version": 1,
        "beta_private_acceptance_score": round(composite, 4),
        "state": state,
        "factors": factors,
    }


__all__ = ["compute_beta_private_acceptance_score"]
