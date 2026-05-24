"""Beta Runtime Acceptance — reporte unificado OSUE + ORCE + UOL + 4LOM + ARSS.

Agrega telemetría runtime existente y decide si una misión está lista para
uso real en beta privada (sin backends externos).
"""

from __future__ import annotations

import json
import time
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.mission import Mission
from app.core.logger import log
from app.core.paths import VAR_DIR

_BETA_ACCEPTANCE_DIR = VAR_DIR / "runtime" / "beta_acceptance"

_ACCEPTANCE_THRESHOLD = 0.85


class BetaRuntimeAcceptanceStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    NEEDS_HARDENING = "NEEDS_HARDENING"
    REJECTED = "REJECTED"
    NO_DATA = "NO_DATA"


class BetaRuntimeAcceptanceReport(BaseModel):
    """Reporte unificado de aceptación beta runtime."""

    model_config = ConfigDict(extra="ignore")

    mission_id: str = ""
    run_id: str = ""
    status: BetaRuntimeAcceptanceStatus = BetaRuntimeAcceptanceStatus.NO_DATA
    uol_native_success_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    orce_consistency_score: float = Field(default=0.0, ge=0.0, le=1.0)
    osue_state_success_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    arss_variant_usage_count: int = 0
    coords_usage_count: int = 0
    smart_route_usage_count: int = 0
    hidden_fallback_count: int = 0
    blocked_state_count: int = 0
    runtime_timeout_count: int = 0
    needs_human_count: int = 0
    weakest_steps: List[Dict[str, Any]] = Field(default_factory=list)
    hardening_reasons: List[str] = Field(default_factory=list)
    recommended_next_action: str = ""
    recorded_at: float = Field(default_factory=time.time)


def enable_beta_runtime_profile(
    settings: Any,
    *,
    arss_enabled: bool = False,
) -> Any:
    """Activa flags del perfil beta runtime robusto (reversible)."""

    settings.BETA_PRIVATE_RUNTIME_PROFILE = True
    settings.BETA_PRIVATE_RELIABILITY_PROFILE = True
    settings.PCOF_ENABLED = True
    settings.OSUE_ENABLED = True
    settings.OSUE_SHADOW_ENABLED = True
    settings.RELIABILITY_HARDENING_ENABLED = True
    settings.RUNTIME_LATENCY_REPORT_ENABLED = True
    settings.PRODUCTION_TELEMETRY_ENABLED = True
    settings.MAGIC_UX_SANITIZE_RUNNER_STATUS = True
    settings.LOP_SHADOW_ENABLED = True
    settings.LOP_RUNNER_SHADOW_ENABLED = True
    settings.UOOL_ENABLED = True
    settings.UOOL_COORDINATE_SMART_EXECUTOR = True
    settings.UOOL_PREFLIGHT_REQUIRED = False
    settings.OCRP_ENABLED = True
    settings.OCRP_STABILIZATION_ENABLED = True
    settings.LONE_LIVE_BRIDGE_ENABLED = True
    settings.LONE_LOOP_BUDGET_MS = 8000
    settings.ENTITY_RUNTIME_TIMEOUT_MS = 12_000
    settings.ENTITY_MAX_RESOLUTION_ATTEMPTS = 3
    settings.ENTITY_UIA_WALK_BUDGET_MS = 2500
    settings.ENTITY_STEP_HARD_TIMEOUT_MS = 45_000
    settings.SMART_EXECUTOR_STEP_TIMEOUT_MS = 90_000
    settings.BETA_RUN_TIMEOUT_SEC = 600
    settings.ONE_SHOT_RELIABILITY_ENABLED = True
    settings.ONE_SHOT_PREFLIGHT_REQUIRED = True
    settings.ONE_SHOT_USE_LOP = True
    settings.ONE_SHOT_REQUIRE_SAFE_TO_CONTINUE = True
    settings.ARL_ENABLED = True
    settings.ARL_SEP_RECOVERY_MAX_ROUNDS = 1
    settings.ORG_SHADOW_ENABLED = True
    settings.GOOR_SHADOW_ENABLED = True
    settings.OCR_SHADOW_ENABLED = True
    settings.TEL_SHADOW_ENABLED = True
    settings.SLL_ENABLED = True
    settings.OMPC_ENABLED = True
    settings.COB_EXECUTION_PILOT_ENABLED = False
    settings.TEL_SEP_PROMOTION_MODE = "suggest"
    settings.TEL_SEP_PROMOTION_ENABLED = True
    settings.ARSS_ENABLED = bool(arss_enabled)
    return settings


def _has_telemetry(mission: Mission) -> bool:
    if list(getattr(mission, "uol_runtime_audit", None) or []):
        return True
    if getattr(mission, "operational_runtime_consistency_report", None):
        return True
    if list(getattr(mission, "osue_runtime_wait_audit", None) or []):
        return True
    if getattr(mission, "uol_runtime_acceptance_report", None):
        return True
    return False


def _osue_metrics(mission: Mission) -> tuple[float, int, int, int, List[Dict[str, Any]]]:
    rows = list(getattr(mission, "osue_runtime_wait_audit", None) or [])
    if not rows:
        return 0.0, 0, 0, 0, []

    matched = sum(1 for r in rows if isinstance(r, dict) and r.get("matched"))
    total = len(rows)
    ratio = matched / total if total else 0.0
    timeouts = sum(1 for r in rows if isinstance(r, dict) and r.get("state_wait_timeout"))
    blocked = sum(
        1 for r in rows
        if isinstance(r, dict) and (r.get("blocked_state_detected") or r.get("needs_human"))
    )
    weak: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if not r.get("matched") or r.get("state_wait_timeout") or r.get("blocked_state_detected"):
            weak.append(
                {
                    "step_id": r.get("step_id", ""),
                    "source": "osue",
                    "reason": r.get("osue_wait_reason") or r.get("reason") or "",
                    "expected_state": r.get("expected_state", ""),
                    "actual_state": r.get("actual_state", ""),
                },
            )
    return round(ratio, 4), timeouts, blocked, total, weak[:6]


def _arss_variant_usage(mission: Mission) -> int:
    ct = 0
    for raw in getattr(mission, "arss_runtime_variants", None) or []:
        if isinstance(raw, dict) and int(raw.get("usage_count") or 0) > 0:
            ct += 1
    return ct


def _needs_human_from_result(mission_result: Any) -> int:
    if mission_result is None:
        return 0
    outcomes = getattr(mission_result, "outcomes", None) or []
    return sum(1 for o in outcomes if getattr(o, "status", "") == "needs_human")


def _destructive_or_unsafe(mission: Mission, mission_result: Any) -> bool:
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    if isinstance(sep, dict):
        for reason in sep.get("not_ready_reasons") or []:
            rs = str(reason).lower()
            if "destructive" in rs or "unsafe" in rs or "ambiguity" in rs:
                return True
    if mission_result is not None:
        for o in getattr(mission_result, "outcomes", None) or []:
            err = str(getattr(o, "error", "") or getattr(o, "human_message", "")).lower()
            if "destructive" in err or "unsafe" in err:
                return True
    return False


def compute_beta_runtime_acceptance_report(
    mission: Mission,
    *,
    run_id: str = "",
    mission_result: Optional[Any] = None,
) -> BetaRuntimeAcceptanceReport:
    """Calcula reporte unificado desde telemetría ya persistida en la misión."""

    mid = str(getattr(mission, "id", "") or "")
    hardening: List[str] = []
    weakest: List[Dict[str, Any]] = []

    if not _has_telemetry(mission):
        return BetaRuntimeAcceptanceReport(
            mission_id=mid,
            run_id=run_id,
            status=BetaRuntimeAcceptanceStatus.NO_DATA,
            hardening_reasons=["sin telemetría runtime (ejecutar con SmartExecutor beta)"],
            recommended_next_action="Ejecutar la misión con perfil beta y volver a evaluar.",
        )

    from app.services.missions.uol_runtime_telemetry import (
        build_uol_runtime_acceptance_report,
        mission_has_unresolved_ask,
    )
    from app.services.runtime.operational_runtime_consistency_engine import (
        OrceRuntimeAcceptance,
        reconcile_mission,
    )

    uol = build_uol_runtime_acceptance_report(mission)
    orce = reconcile_mission(mission)
    osue_ratio, osue_timeouts, osue_blocked, _osue_total, osue_weak = _osue_metrics(mission)

    uol_ratio = float(uol.uol_native_success_ratio or 0.0)
    orce_score = float(orce.consistency_score or 0.0)
    coords_ct = int(uol.coords_usage_count or 0)
    smart_ct = int(uol.smart_route_usage_count or 0)
    hidden_fb = int(orce.hidden_fallbacks or 0)
    needs_human = _needs_human_from_result(mission_result)
    if mission_has_unresolved_ask(mission):
        needs_human = max(needs_human, 1)

    arss_usage = _arss_variant_usage(mission)

    weakest.extend(osue_weak)
    for w in uol.weakest_uol_steps or []:
        if isinstance(w, dict):
            weakest.append({**w, "source": "uol"})
    for w in orce.weakest_runtime_steps or []:
        if isinstance(w, dict):
            weakest.append({**w, "source": "orce"})

    seen_ids: set = set()
    deduped: List[Dict[str, Any]] = []
    for w in weakest:
        key = (w.get("step_id"), w.get("source"), w.get("reason"))
        if key in seen_ids:
            continue
        seen_ids.add(key)
        deduped.append(w)
    weakest = deduped[:8]

    smart_on_canonical = any(
        "smart_route" in str(r).lower() and "canonical" in str(r).lower()
        for r in (uol.acceptance_reasons or [])
    )
    canon_viol = int(orce.canonical_truth_violations or 0)
    forbidden = int(orce.forbidden_strategy_usage or 0)
    orce_accept = str(orce.runtime_acceptance or "")

    status = BetaRuntimeAcceptanceStatus.ACCEPTED

    if _destructive_or_unsafe(mission, mission_result):
        status = BetaRuntimeAcceptanceStatus.REJECTED
        hardening.append("destructive_or_unsafe_ambiguity")

    if coords_ct > 0 and (canon_viol > 0 or uol.canonical_truth_usage_count > 0):
        status = BetaRuntimeAcceptanceStatus.REJECTED
        hardening.append("coords_forbidden_on_canonical_truth")

    if smart_on_canonical or (
        smart_ct > 0 and uol.canonical_truth_usage_count > 0 and uol_ratio < _ACCEPTANCE_THRESHOLD
    ):
        if status != BetaRuntimeAcceptanceStatus.REJECTED:
            status = BetaRuntimeAcceptanceStatus.REJECTED
        hardening.append("smart_route_primary_on_canonical_truth")

    if canon_viol > 0:
        status = BetaRuntimeAcceptanceStatus.REJECTED
        hardening.append(f"canonical_truth_violations={canon_viol}")

    if forbidden > 0 and coords_ct > 0:
        status = BetaRuntimeAcceptanceStatus.REJECTED
        hardening.append("forbidden_coords_runtime")

    if orce_accept == OrceRuntimeAcceptance.REJECTED.value:
        status = BetaRuntimeAcceptanceStatus.REJECTED
        hardening.append("orce_runtime_rejected")

    if orce.divergence_count > 0 and status == BetaRuntimeAcceptanceStatus.ACCEPTED:
        hardening.append(f"orce_divergence_count={orce.divergence_count}")

    if uol_ratio < _ACCEPTANCE_THRESHOLD:
        if status == BetaRuntimeAcceptanceStatus.ACCEPTED:
            status = BetaRuntimeAcceptanceStatus.NEEDS_HARDENING
        hardening.append(f"uol_native_success_ratio={uol_ratio:.2f} < {_ACCEPTANCE_THRESHOLD}")

    if orce_score < _ACCEPTANCE_THRESHOLD:
        if status == BetaRuntimeAcceptanceStatus.ACCEPTED:
            status = BetaRuntimeAcceptanceStatus.NEEDS_HARDENING
        hardening.append(f"orce_consistency_score={orce_score:.2f} < {_ACCEPTANCE_THRESHOLD}")

    if _osue_total > 0 and osue_ratio < _ACCEPTANCE_THRESHOLD:
        if status == BetaRuntimeAcceptanceStatus.ACCEPTED:
            status = BetaRuntimeAcceptanceStatus.NEEDS_HARDENING
        hardening.append(f"osue_state_success_ratio={osue_ratio:.2f} < {_ACCEPTANCE_THRESHOLD}")

    if hidden_fb > 0:
        if status == BetaRuntimeAcceptanceStatus.ACCEPTED:
            status = BetaRuntimeAcceptanceStatus.NEEDS_HARDENING
        hardening.append(f"hidden_fallback_count={hidden_fb}")

    if osue_timeouts > 0:
        if status == BetaRuntimeAcceptanceStatus.ACCEPTED:
            status = BetaRuntimeAcceptanceStatus.NEEDS_HARDENING
        hardening.append(f"runtime_timeout_count={osue_timeouts}")

    if osue_blocked > 0:
        if status == BetaRuntimeAcceptanceStatus.ACCEPTED:
            status = BetaRuntimeAcceptanceStatus.NEEDS_HARDENING
        hardening.append(f"blocked_state_count={osue_blocked}")

    if needs_human > 0:
        if status == BetaRuntimeAcceptanceStatus.ACCEPTED:
            status = BetaRuntimeAcceptanceStatus.NEEDS_HARDENING
        hardening.append(f"needs_human_count={needs_human}")

    if coords_ct > 0 and status == BetaRuntimeAcceptanceStatus.ACCEPTED:
        status = BetaRuntimeAcceptanceStatus.NEEDS_HARDENING
        hardening.append(f"coords_usage_count={coords_ct}")

    if (
        status == BetaRuntimeAcceptanceStatus.ACCEPTED
        and uol_ratio >= _ACCEPTANCE_THRESHOLD
        and orce_score >= _ACCEPTANCE_THRESHOLD
        and (_osue_total == 0 or osue_ratio >= _ACCEPTANCE_THRESHOLD)
        and coords_ct == 0
        and hidden_fb == 0
        and needs_human == 0
        and osue_blocked == 0
    ):
        hardening.append("criterios beta runtime cumplidos")

    next_action = _recommended_next_action(status, hardening, weakest)

    return BetaRuntimeAcceptanceReport(
        mission_id=mid,
        run_id=run_id,
        status=status,
        uol_native_success_ratio=round(uol_ratio, 4),
        orce_consistency_score=round(orce_score, 4),
        osue_state_success_ratio=osue_ratio,
        arss_variant_usage_count=arss_usage,
        coords_usage_count=coords_ct,
        smart_route_usage_count=smart_ct,
        hidden_fallback_count=hidden_fb,
        blocked_state_count=osue_blocked,
        runtime_timeout_count=osue_timeouts,
        needs_human_count=needs_human,
        weakest_steps=weakest,
        hardening_reasons=hardening[:16],
        recommended_next_action=next_action,
    )


def _recommended_next_action(
    status: BetaRuntimeAcceptanceStatus,
    reasons: Sequence[str],
    weakest: Sequence[Dict[str, Any]],
) -> str:
    if status == BetaRuntimeAcceptanceStatus.NO_DATA:
        return "Ejecutar la misión con perfil beta activo y generar telemetría."
    if status == BetaRuntimeAcceptanceStatus.ACCEPTED:
        return "Repetir 5 ejecuciones consecutivas; si todas ACCEPTED, lista para uso real."
    if status == BetaRuntimeAcceptanceStatus.REJECTED:
        if any("coords" in r for r in reasons):
            return "Eliminar coords/smart_route en pasos canonical; re-grabar identidad con PCOF."
        if any("canonical" in r for r in reasons):
            return "Revisar contrato 4LOM y reforzar canonical truth antes de re-ejecutar."
        return "Corregir violaciones de contrato runtime antes de continuar."
    step_hint = ""
    if weakest:
        step_hint = str(weakest[0].get("step_id") or "")
    if any("timeout" in r.lower() for r in reasons):
        return f"Fortalecer waits OSUE en paso {step_hint or 'débil'}; revisar loading/blockers."
    if any("blocked" in r.lower() for r in reasons):
        return f"Resolver bloqueos operacionales (modal/auth) en paso {step_hint or 'débil'}."
    if step_hint:
        return f"Fortalecer paso {step_hint} y re-ejecutar benchmark beta."
    return "Re-ejecutar misión tras hardening de pasos débiles."


def persist_beta_runtime_acceptance_report(
    mission: Mission,
    report: BetaRuntimeAcceptanceReport,
) -> BetaRuntimeAcceptanceReport:
    mission.beta_runtime_acceptance_report = report.model_dump(mode="json")
    try:
        _BETA_ACCEPTANCE_DIR.mkdir(parents=True, exist_ok=True)
        path = _BETA_ACCEPTANCE_DIR / f"{report.mission_id}_{int(report.recorded_at)}.json"
        path.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("[BetaAcceptance] persist json: %s", exc)
    return report


def build_and_persist_beta_runtime_acceptance(
    mission: Mission,
    *,
    run_id: str = "",
    mission_result: Optional[Any] = None,
) -> BetaRuntimeAcceptanceReport:
    report = compute_beta_runtime_acceptance_report(
        mission,
        run_id=run_id,
        mission_result=mission_result,
    )
    return persist_beta_runtime_acceptance_report(mission, report)


def beta_runtime_normal_label(mission: Mission) -> str:
    """Texto simple para Mission Review modo normal."""

    raw = getattr(mission, "beta_runtime_acceptance_report", None)
    if not isinstance(raw, dict):
        return ""
    st = str(raw.get("status") or "")
    if st == BetaRuntimeAcceptanceStatus.ACCEPTED.value:
        return "Lista para ejecutar"
    if st in {
        BetaRuntimeAcceptanceStatus.NEEDS_HARDENING.value,
        BetaRuntimeAcceptanceStatus.REJECTED.value,
        BetaRuntimeAcceptanceStatus.NO_DATA.value,
    }:
        return "Necesita fortalecerse"
    return ""


def expert_panel_lines(mission: Mission, *, expert_mode: bool = True) -> List[str]:
    if not expert_mode:
        return []

    raw = getattr(mission, "beta_runtime_acceptance_report", None)
    if not isinstance(raw, dict):
        report = compute_beta_runtime_acceptance_report(mission)
        if report.status == BetaRuntimeAcceptanceStatus.NO_DATA:
            return []
        raw = report.model_dump(mode="json")

    lines = [
        "Beta Runtime Acceptance",
        f"  estado: {raw.get('status', 'NO_DATA')}",
        (
            f"  UOL: {float(raw.get('uol_native_success_ratio') or 0):.0%} · "
            f"ORCE: {float(raw.get('orce_consistency_score') or 0):.2f} · "
            f"OSUE: {float(raw.get('osue_state_success_ratio') or 0):.0%}"
        ),
        (
            f"  fallbacks ocultos: {int(raw.get('hidden_fallback_count') or 0)} · "
            f"blocked: {int(raw.get('blocked_state_count') or 0)} · "
            f"timeouts: {int(raw.get('runtime_timeout_count') or 0)}"
        ),
    ]
    weak = raw.get("weakest_steps") or []
    if weak and isinstance(weak[0], dict):
        w0 = weak[0]
        lines.append(
            f"  paso débil: {w0.get('step_id', '—')} ({w0.get('source', '')}) "
            f"{str(w0.get('reason') or w0.get('failure_reason') or '')[:60]}",
        )
    action = str(raw.get("recommended_next_action") or "")
    if action:
        lines.append(f"  → {action[:120]}")
    return lines


def compact_summary(report: BetaRuntimeAcceptanceReport) -> str:
    return (
        f"[{report.status.value}] UOL={report.uol_native_success_ratio:.0%} "
        f"ORCE={report.orce_consistency_score:.2f} OSUE={report.osue_state_success_ratio:.0%} "
        f"| {report.recommended_next_action[:80]}"
    )


__all__ = [
    "BetaRuntimeAcceptanceReport",
    "BetaRuntimeAcceptanceStatus",
    "beta_runtime_normal_label",
    "build_and_persist_beta_runtime_acceptance",
    "compact_summary",
    "compute_beta_runtime_acceptance_report",
    "enable_beta_runtime_profile",
    "expert_panel_lines",
    "persist_beta_runtime_acceptance_report",
]
