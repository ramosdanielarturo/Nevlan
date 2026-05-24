"""UOL Runtime Telemetry & Acceptance Report.

Mide qué porcentaje del runtime ejecuta vía handlers UOL-native vs legacy
(``smart_route``, coords, fallbacks).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.core.logger import log
from app.core.paths import VAR_DIR
from app.services.missions.execution_contracts import MissionStep

_UOL_JSONL = VAR_DIR / "runtime" / "uol_execution_memory_audit.jsonl"

_COORD_MARKERS = (
    "relative_coords",
    "absolute_coords",
    "use_coords",
    "vision_coords",
    ":coords",
    "coords_emergency",
)
_SMART_ROUTE_MARKER = "smart_route"
_UOL_MARKER = ":uol_"


class UolAcceptanceStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    NEEDS_HARDENING = "NEEDS_HARDENING"
    NO_UOL_DATA = "NO_UOL_DATA"


class UolStepTelemetryRecord(BaseModel):
    """Telemetría por step (una fila en ``mission.uol_runtime_audit``)."""

    model_config = ConfigDict(extra="ignore")

    step_id: str = ""
    uol_action: str = ""
    primary_strategy: str = ""
    handler_used: str = ""
    handler_success: bool = False
    fallback_used: bool = False
    fallback_strategy: str = ""
    legacy_used: bool = False
    coords_used: bool = False
    smart_route_used: bool = False
    validation_passed: bool = False
    failure_reason: str = ""
    latency_ms: int = 0
    canonical_truth_used: bool = False
    coi_intent_id: str = ""
    runtime_resolution_path: str = ""
    runtime_resolution_substeps: List[str] = Field(default_factory=list)
    execution_latency_ms: int = 0
    run_id: str = ""
    recorded_at: float = Field(default_factory=time.time)
    strategies_attempted: List[str] = Field(default_factory=list)
    continuity_preserved: bool = False
    continuity_bridge_used: str = ""
    operational_thread_id: str = ""
    surface_transition_type: str = ""
    continuity_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class UolRuntimeAcceptanceReport(BaseModel):
    """Resumen agregado para Mission Review experto."""

    model_config = ConfigDict(extra="ignore")

    total_uol_steps: int = 0
    uol_native_success_count: int = 0
    uol_native_success_ratio: float = 0.0
    legacy_fallback_ratio: float = 0.0
    smart_route_usage_count: int = 0
    coords_usage_count: int = 0
    canonical_truth_usage_count: int = 0
    validation_passed_count: int = 0
    validation_passed_ratio: float = 0.0
    average_latency_ms: float = 0.0
    weakest_uol_steps: List[Dict[str, Any]] = Field(default_factory=list)
    last_failure_reason: str = ""
    unresolved_ask: bool = False
    acceptance_status: UolAcceptanceStatus = UolAcceptanceStatus.NO_UOL_DATA
    acceptance_reasons: List[str] = Field(default_factory=list)


def _strategy_uses_coords(strategy: str) -> bool:
    sl = (strategy or "").lower()
    return any(m in sl for m in _COORD_MARKERS)


def _strategy_uses_smart_route(strategy: str) -> bool:
    return _SMART_ROUTE_MARKER in (strategy or "").lower()


def _is_uol_strategy(strategy: str) -> bool:
    return _UOL_MARKER in (strategy or "")


def step_has_uol_telemetry(step: MissionStep) -> bool:
    """True si el step participa en telemetría UOL."""
    if str(step.params.get("uol_action") or "").strip():
        return True
    pref = str(step.params.get("preferred_strategy") or "")
    return _is_uol_strategy(pref)


def _coi_context_from_step(step: MissionStep) -> tuple[bool, str]:
    if step.params.get("uol_canonical_truth") or step.params.get("canonical_truth"):
        coi_raw = step.params.get("canonical_operational_intent")
        if isinstance(coi_raw, dict):
            return True, str(coi_raw.get("intent_id") or "")
        return True, ""
    coi_raw = step.params.get("canonical_operational_intent")
    if isinstance(coi_raw, dict) and coi_raw.get("canonical_truth"):
        return True, str(coi_raw.get("intent_id") or "")
    return False, ""


def append_uol_runtime_record(mission: Any, record: UolStepTelemetryRecord) -> None:
    """Persiste en ``mission.uol_runtime_audit`` y JSONL opcional."""
    if mission is None:
        return
    try:
        audit = list(getattr(mission, "uol_runtime_audit", None) or [])
        audit.append(record.model_dump(mode="json"))
        mission.uol_runtime_audit = audit[-500:]
    except Exception as exc:
        log.debug("[UOL telemetry] mission audit: %s", exc)

    try:
        _UOL_JSONL.parent.mkdir(parents=True, exist_ok=True)
        row = record.model_dump(mode="json")
        row["mission_id"] = str(getattr(mission, "id", "") or "")
        with _UOL_JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.debug("[UOL telemetry] jsonl: %s", exc)


def mission_has_unresolved_ask(mission: Any) -> bool:
    """True si el plan o blockers indican aclaración pendiente."""
    if mission is None:
        return False
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    if isinstance(sep, dict):
        blockers = list(sep.get("not_ready_reasons") or [])
        if any("NEEDS_USER_LABEL" in str(b) for b in blockers):
            return True
        if any("clarification" in str(b).lower() for b in blockers):
            return True
        for sp in sep.get("steps") or []:
            if isinstance(sp, dict) and sp.get("needs_user_label"):
                return True
    return False


def build_uol_runtime_acceptance_report(mission: Any) -> UolRuntimeAcceptanceReport:
    """Calcula métricas de aceptación UOL desde ``uol_runtime_audit``."""
    raw = list(getattr(mission, "uol_runtime_audit", None) or []) if mission else []
    if not raw:
        return UolRuntimeAcceptanceReport(
            acceptance_status=UolAcceptanceStatus.NO_UOL_DATA,
            acceptance_reasons=["sin telemetría UOL en la misión"],
            unresolved_ask=mission_has_unresolved_ask(mission),
        )

    records: List[UolStepTelemetryRecord] = []
    for row in raw:
        try:
            records.append(UolStepTelemetryRecord.model_validate(row))
        except Exception:
            continue

    total = len(records)
    native_ok = sum(
        1 for r in records
        if r.handler_success and _is_uol_strategy(r.handler_used or r.primary_strategy)
        and not r.fallback_used
    )
    legacy_fb = sum(1 for r in records if r.fallback_used or r.legacy_used)
    smart_ct = sum(1 for r in records if r.smart_route_used)
    coords_ct = sum(1 for r in records if r.coords_used)
    canon_ct = sum(1 for r in records if r.canonical_truth_used)
    val_ok = sum(1 for r in records if r.validation_passed)
    latencies = [int(r.latency_ms) for r in records if r.latency_ms > 0]
    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0

    smart_on_canonical = sum(
        1 for r in records
        if r.canonical_truth_used and r.smart_route_used
    )

    ratio_native = native_ok / total if total else 0.0
    ratio_legacy = legacy_fb / total if total else 0.0
    ratio_val = val_ok / total if total else 0.0

    weakest = sorted(
        records,
        key=lambda r: (
            0 if (r.handler_success and r.validation_passed) else 1,
            0 if str(r.failure_reason or "").startswith("ENTITY_") or r.failure_reason == "NO_STATE_PROGRESS" else 1,
            -r.latency_ms,
        ),
    )[:5]
    weakest_out = [
        {
            "step_id": r.step_id,
            "uol_action": r.uol_action,
            "handler_used": r.handler_used,
            "failure_reason": r.failure_reason,
            "latency_ms": r.latency_ms,
        }
        for r in weakest
        if not r.handler_success or not r.validation_passed
    ]
    if not weakest_out and records:
        weakest_out = [
            {
                "step_id": records[-1].step_id,
                "uol_action": records[-1].uol_action,
                "handler_used": records[-1].handler_used,
                "failure_reason": records[-1].failure_reason or "ok",
                "latency_ms": records[-1].latency_ms,
            },
        ]

    last_fail = ""
    for r in reversed(records):
        if r.failure_reason:
            last_fail = r.failure_reason
            break

    unresolved = mission_has_unresolved_ask(mission)
    reasons: List[str] = []
    status = UolAcceptanceStatus.ACCEPTED

    if ratio_native < 0.85:
        status = UolAcceptanceStatus.NEEDS_HARDENING
        reasons.append(f"uol_native_success_ratio={ratio_native:.2f} < 0.85")
    if coords_ct > 0:
        status = UolAcceptanceStatus.NEEDS_HARDENING
        reasons.append(f"coords_usage_count={coords_ct}")
    if smart_on_canonical > 0:
        status = UolAcceptanceStatus.NEEDS_HARDENING
        reasons.append(f"smart_route en pasos canonical_truth={smart_on_canonical}")
    if unresolved:
        status = UolAcceptanceStatus.NEEDS_HARDENING
        reasons.append("unresolved_ask")
    if ratio_val < 0.85:
        status = UolAcceptanceStatus.NEEDS_HARDENING
        reasons.append(f"validation_passed_ratio={ratio_val:.2f} < 0.85")

    if status == UolAcceptanceStatus.ACCEPTED and not reasons:
        reasons.append("criterios UOL cumplidos")

    return UolRuntimeAcceptanceReport(
        total_uol_steps=total,
        uol_native_success_count=native_ok,
        uol_native_success_ratio=round(ratio_native, 4),
        legacy_fallback_ratio=round(ratio_legacy, 4),
        smart_route_usage_count=smart_ct,
        coords_usage_count=coords_ct,
        canonical_truth_usage_count=canon_ct,
        validation_passed_count=val_ok,
        validation_passed_ratio=round(ratio_val, 4),
        average_latency_ms=round(avg_lat, 1),
        weakest_uol_steps=weakest_out,
        last_failure_reason=last_fail,
        unresolved_ask=unresolved,
        acceptance_status=status,
        acceptance_reasons=reasons,
    )


@dataclass
class UolStepTelemetryTracker:
    """Acumula telemetría de un step durante ``SmartMissionExecutor._run_step``."""

    mission: Any
    step: MissionStep
    run_id: str = ""
    active: bool = False
    record: UolStepTelemetryRecord = field(default_factory=UolStepTelemetryRecord)
    _t0: float = field(default_factory=time.time)
    _uol_native_success: bool = False
    _finalized: bool = False

    def __post_init__(self) -> None:
        self.active = step_has_uol_telemetry(self.step)
        if not self.active:
            return
        canon, coi_id = _coi_context_from_step(self.step)
        self.record = UolStepTelemetryRecord(
            step_id=str(self.step.id or ""),
            uol_action=str(self.step.params.get("uol_action") or ""),
            primary_strategy=str(self.step.params.get("preferred_strategy") or ""),
            canonical_truth_used=canon,
            coi_intent_id=coi_id,
            run_id=self.run_id,
        )
        try:
            from app.services.missions.operational_continuity_reconstruction import (
                enrich_uol_telemetry_record,
            )

            enrich_uol_telemetry_record(self.record, self.step, self.mission)
        except Exception:
            pass

    def _apply_result_extra(self, extra: Dict[str, Any], *, validation_passed: bool = False) -> None:
        if extra.get("uol_handler"):
            self.record.handler_used = str(extra.get("uol_handler"))
        if extra.get("resolution_path"):
            self.record.runtime_resolution_path = str(extra.get("resolution_path"))
        substeps = extra.get("runtime_resolution_substeps")
        if isinstance(substeps, list) and substeps:
            self.record.runtime_resolution_substeps = [str(s) for s in substeps if str(s).strip()]
        if extra.get("execution_latency_ms") is not None:
            try:
                self.record.execution_latency_ms = max(0, int(extra.get("execution_latency_ms") or 0))
            except Exception:
                pass
        if extra.get("validation_status") == "passed" or validation_passed:
            self.record.validation_passed = True

    def record_uol_hook_attempt(
        self,
        *,
        strategy: str,
        success: bool,
        message: str = "",
        resolution_path: str = "",
        runtime_resolution_substeps: Optional[List[str]] = None,
        execution_latency_ms: int = 0,
        blocked_ambiguity: bool = False,
    ) -> None:
        if not self.active:
            return
        self.record.handler_used = strategy
        self.record.handler_success = bool(success)
        if resolution_path:
            self.record.runtime_resolution_path = resolution_path
        elif success:
            self.record.runtime_resolution_path = "uol_hook"
        if runtime_resolution_substeps:
            self.record.runtime_resolution_substeps = list(runtime_resolution_substeps)
        if execution_latency_ms > 0:
            self.record.execution_latency_ms = execution_latency_ms
        if blocked_ambiguity:
            self.record.failure_reason = message or "ambiguity_blocked"
        if success and _is_uol_strategy(strategy):
            self._uol_native_success = True

    def record_strategy_attempt(
        self,
        strategy: str,
        result: Any,
        *,
        validation_passed: bool = False,
    ) -> None:
        """Registra cada ``run_strategy`` (UOL o legacy)."""
        if not self.active:
            return
        if strategy not in self.record.strategies_attempted:
            self.record.strategies_attempted.append(strategy)

        extra = getattr(result, "extra", None) or {}
        if isinstance(extra, dict):
            self._apply_result_extra(extra, validation_passed=validation_passed)

        ok = bool(getattr(result, "ok", False))
        is_uol = _is_uol_strategy(strategy)

        if is_uol and ok and not self.record.fallback_used:
            self.record.handler_used = strategy
            self.record.handler_success = True
            self._uol_native_success = True
            if validation_passed:
                self.record.validation_passed = True
            return

        if self.record.handler_success and _is_uol_strategy(self.record.handler_used):
            # Ya hubo éxito UOL-native; intentos posteriores son redundantes.
            return

        if is_uol and not ok and not self.record.handler_used:
            self.record.handler_used = strategy

        if not is_uol:
            self.record.legacy_used = True
            if self.record.handler_used and _is_uol_strategy(self.record.handler_used):
                self.record.fallback_used = True
                self.record.fallback_strategy = strategy
            elif not self.record.handler_success:
                self.record.fallback_used = True
                self.record.fallback_strategy = strategy

        if _strategy_uses_coords(strategy):
            self.record.coords_used = True
        if _strategy_uses_smart_route(strategy):
            self.record.smart_route_used = True

        if not ok and not self.record.failure_reason:
            self.record.failure_reason = str(getattr(result, "message", "") or "")

        if validation_passed:
            self.record.validation_passed = True

    def finalize(
        self,
        *,
        success: bool,
        validation_passed: bool = False,
        failure_reason: str = "",
        strategy_used: Optional[str] = None,
    ) -> None:
        if not self.active or self._finalized:
            return
        self._finalized = True
        if self.record.execution_latency_ms <= 0:
            self.record.latency_ms = int((time.time() - self._t0) * 1000)
        else:
            self.record.latency_ms = self.record.execution_latency_ms
        if strategy_used:
            if _is_uol_strategy(strategy_used):
                self.record.handler_used = strategy_used
            elif success:
                self.record.fallback_strategy = strategy_used
        if validation_passed:
            self.record.validation_passed = True
        if success and self._uol_native_success:
            self.record.handler_success = True
        if failure_reason:
            self.record.failure_reason = failure_reason
        elif not success and not self.record.failure_reason:
            self.record.failure_reason = "step_failed"

        append_uol_runtime_record(self.mission, self.record)


def start_step_telemetry(
    mission: Any,
    step: MissionStep,
    *,
    run_id: str = "",
) -> UolStepTelemetryTracker:
    return UolStepTelemetryTracker(mission=mission, step=step, run_id=run_id)


def expert_panel_lines(mission: Any) -> List[str]:
    """Líneas para panel Mission Review experto."""
    report = build_uol_runtime_acceptance_report(mission)
    if report.acceptance_status == UolAcceptanceStatus.NO_UOL_DATA:
        return ["UOL Runtime: sin telemetría de ejecución (corre la misión con Smart Executor)."]

    pct = int(report.uol_native_success_ratio * 100)
    lines = [
        "UOL Runtime Acceptance",
        f"  estado: {report.acceptance_status.value}",
        f"  UOL-native: {pct}% ({report.uol_native_success_count}/{report.total_uol_steps})",
        f"  legacy fallbacks: {int(report.legacy_fallback_ratio * 100)}%",
        f"  coords: {report.coords_usage_count} · smart_route: {report.smart_route_usage_count}",
        f"  validación OK: {int(report.validation_passed_ratio * 100)}%",
        f"  latencia media: {report.average_latency_ms:.0f} ms",
    ]
    if report.weakest_uol_steps:
        w = report.weakest_uol_steps[0]
        lines.append(
            f"  paso más débil: {w.get('step_id')} ({w.get('uol_action')}) "
            f"— {w.get('failure_reason', '')[:80]}",
        )
    if report.last_failure_reason:
        lines.append(f"  último fallo: {report.last_failure_reason[:120]}")
    if report.acceptance_reasons:
        lines.append(f"  · {'; '.join(report.acceptance_reasons[:3])}")
    return lines


def persist_acceptance_report_on_mission(mission: Any) -> UolRuntimeAcceptanceReport:
    """Calcula y guarda snapshot en misión (campo opcional)."""
    report = build_uol_runtime_acceptance_report(mission)
    if mission is not None:
        try:
            mission.uol_runtime_acceptance_report = report.model_dump(mode="json")
        except Exception:
            pass
    return report


__all__ = [
    "UolAcceptanceStatus",
    "UolRuntimeAcceptanceReport",
    "UolStepTelemetryRecord",
    "UolStepTelemetryTracker",
    "append_uol_runtime_record",
    "build_uol_runtime_acceptance_report",
    "expert_panel_lines",
    "mission_has_unresolved_ask",
    "persist_acceptance_report_on_mission",
    "start_step_telemetry",
    "step_has_uol_telemetry",
]
