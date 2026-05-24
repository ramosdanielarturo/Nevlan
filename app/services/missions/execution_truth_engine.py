"""EXECUTION TRUTH — fuente única de «¿puedo ejecutar esto con confianza?».

Prioridad producto (PRD 2026-05-17b):

    EXECUTION_TRUTH > semantic gate > heurísticas de captura > mission.status

No decide ambigüedad humana real (perfil/query): eso sigue en
:class:`~app.services.missions.semantic_execution_plan.SemanticPlanStep`
y en ``compute_ready_status``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_ALLOWED_TARGET_SOURCES = frozenset({
    "uia",
    "semantic_target",
    "uia_control",
    "dom",
    "uia_control_legacy",
})


def _as_cc_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return {}


def _merge_iso_cc(iso: Any, cc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cc)
    if not isinstance(iso, dict):
        return out
    for k in (
        "trusted_pre_action_identity",
        "identity_preserved_after_transition",
        "successful_state_transition",
    ):
        if iso.get(k) is True:
            out[k] = True
    if iso.get("contamination") is False:
        out["contamination"] = False
    elif iso.get("contamination") is True:
        out["contamination"] = True
    # Alineado con ``build_capture_contract``: aislamiento fuerte → ejecutable.
    if iso.get("trusted_pre_action_identity") and iso.get(
        "identity_preserved_after_transition",
    ):
        out["trusted_pre_action_identity"] = True
        out["sufficient_for_execution"] = bool(
            out.get("sufficient_for_execution") or True,
        )
    return out


def _evaluate_evidence_bundle(
    cc: Dict[str, Any],
    *,
    iso: Optional[Dict[str, Any]] = None,
    sig: Optional[Dict[str, Any]] = None,
    where: str,
) -> Tuple[bool, List[str], List[str]]:
    """Evalúa una evidencia concreta (contrato + aislamiento + firma compilada)."""
    merged = _merge_iso_cc(iso, cc)
    reasons: List[str] = []
    suppressed: List[str] = []

    trusted = bool(merged.get("trusted_pre_action_identity"))
    sufficient = bool(merged.get("sufficient_for_execution"))
    capture_complete = merged.get("capture_complete")

    # Contaminación explícita en contrato / auditoría de aislamiento.
    contamination_raw = merged.get("contamination")
    contamination_ok = contamination_raw is not True

    ts_cc = str(merged.get("target_source") or "").strip().lower()
    ts_sig = str((sig or {}).get("target_source") or "").strip().lower()
    ts = ts_cc or ts_sig

    promoted_raw = str(
        (sig or {}).get("identity_promoted_from")
        or merged.get("identity_promoted_from")
        or "",
    ).strip().lower()
    promoted_cf = promoted_raw == "click_fallback"

    preserved = bool(
        merged.get("identity_preserved_after_transition")
        or (sig or {}).get("identity_preserved_after_transition")
    )

    strong_capture_contract = bool(
        capture_complete is True and trusted and sufficient,
    )

    source_ok = (
        ts in _ALLOWED_TARGET_SOURCES
        or promoted_cf
        or preserved
        or strong_capture_contract
    )

    if strong_capture_contract:
        reasons.append(f"{where}:strong_capture_contract_fallback")

    if trusted:
        reasons.append(f"{where}:trusted_pre_action_identity")
    if sufficient:
        reasons.append(f"{where}:sufficient_for_execution")
    if capture_complete is True:
        reasons.append(f"{where}:capture_complete")
    if preserved:
        reasons.append(f"{where}:identity_preserved_after_transition")
    if promoted_cf:
        reasons.append(f"{where}:identity_promoted_from_click_fallback")
    if ts in _ALLOWED_TARGET_SOURCES:
        reasons.append(f"{where}:target_source={ts}")

    confirmed = bool(
        trusted
        and sufficient
        and contamination_ok
        and source_ok
    )

    if not contamination_ok:
        confirmed = False
        reasons.append(f"{where}:contamination_true")

    if confirmed and capture_complete is False and not (preserved or promoted_cf):
        confirmed = False
        reasons.append(f"{where}:capture_complete_false_blocks")

    if confirmed:
        if preserved:
            suppressed.extend(
                ["hwnd_changed", "app_changed", "title_changed",
                 "legacy_transition_noise"],
            )
        if promoted_cf:
            suppressed.extend(
                ["click_fallback_historical", "quality_level_legacy"],
            )
        ql = str(merged.get("quality_level") or "").strip().lower()
        if ql in ("low", "red"):
            suppressed.append("quality_level_legacy")
        if ts_sig == "click_fallback" or ts_cc == "click_fallback":
            if preserved or promoted_cf:
                suppressed.append("click_fallback_historical")
        if merged.get("capture_complete") is False and preserved:
            suppressed.append("capture_complete_relaxed_by_transition")
        suppressed.extend(
            ["zero_capture", "visual_missing", "needs_review_artificial"],
        )

    return confirmed, reasons, suppressed


@dataclass
class ExecutionTruthResult:
    """Resultado de auditar la misión contra EXECUTION_TRUTH."""

    confirmed: bool
    reasons: List[str] = field(default_factory=list)
    suppressed_legacy_flags: List[str] = field(default_factory=list)
    promoted_to_executable: bool = False
    where: str = ""

    def to_audit_dict(self) -> Dict[str, Any]:
        return {
            "execution_truth": {
                "confirmed": self.confirmed,
                "reasons": list(self.reasons),
                "suppressed_legacy_flags": list(self.suppressed_legacy_flags),
                "promoted_to_executable": self.promoted_to_executable,
                "where": self.where,
            }
        }


def compute_execution_truth(mission: Any) -> ExecutionTruthResult:
    """Barrido de ``raw_trace`` + ``compiled_execution_graph`` (firmas)."""
    best: Optional[ExecutionTruthResult] = None

    for coi in getattr(mission, "canonical_operational_intents", None) or []:
        if getattr(coi, "canonical_truth", False):
            return ExecutionTruthResult(
                confirmed=True,
                reasons=["ffec:canonical_operational_intent"],
                suppressed_legacy_flags=[
                    "hwnd_changed",
                    "app_changed",
                    "title_changed",
                    "legacy_transition_noise",
                    "click_fallback_historical",
                    "capture_incomplete",
                    "low_confidence",
                    "ambiguous_profile_picker",
                ],
                promoted_to_executable=True,
                where="canonical_operational_intents",
            )

    for idx, ev in enumerate(getattr(mission, "raw_trace", None) or []):
        meta = getattr(ev, "metadata", None) or {}
        if isinstance(meta, dict):
            coi_raw = meta.get("canonical_operational_intent")
            if isinstance(coi_raw, dict) and coi_raw.get("canonical_truth"):
                return ExecutionTruthResult(
                    confirmed=True,
                    reasons=["ffec:raw_trace_canonical_operational_intent"],
                    suppressed_legacy_flags=[
                        "hwnd_changed",
                        "app_changed",
                        "title_changed",
                        "legacy_transition_noise",
                        "click_fallback_historical",
                        "capture_incomplete",
                        "low_confidence",
                    ],
                    promoted_to_executable=True,
                    where=f"raw_trace[{idx}]",
                )

    for idx, ev in enumerate(getattr(mission, "raw_trace", None) or []):
        meta = getattr(ev, "metadata", None) or {}
        if not isinstance(meta, dict):
            continue
        cc = _as_cc_dict(meta.get("capture_contract"))
        iso = meta.get("target_identity_isolation")
        ok, reasons, suppressed = _evaluate_evidence_bundle(
            cc,
            iso=iso if isinstance(iso, dict) else None,
            where=f"raw_trace[{idx}]",
        )
        if ok:
            return ExecutionTruthResult(
                confirmed=True,
                reasons=reasons,
                suppressed_legacy_flags=suppressed,
                promoted_to_executable=True,
                where=f"raw_trace[{idx}]",
            )
        if best is None or len(reasons) > len(best.reasons):
            best = ExecutionTruthResult(
                confirmed=False,
                reasons=reasons,
                suppressed_legacy_flags=suppressed,
                promoted_to_executable=False,
                where=f"raw_trace[{idx}]",
            )

    for idx, cs in enumerate(getattr(mission, "compiled_execution_graph", None) or []):
        tc = getattr(cs, "target_context", None)
        sig = getattr(tc, "signature", None) if tc is not None else None
        if not isinstance(sig, dict):
            continue
        cc = _as_cc_dict(sig.get("capture_contract"))
        # Firma compilada a veces lleva campos top-level duplicados.
        if not cc:
            cc = {
                "trusted_pre_action_identity": sig.get("trusted_pre_action_identity"),
                "sufficient_for_execution": sig.get("sufficient_for_execution"),
                "capture_complete": sig.get("capture_complete"),
                "target_source": sig.get("target_source"),
                "quality_level": sig.get("quality_level"),
                "identity_preserved_after_transition": sig.get(
                    "identity_preserved_after_transition",
                ),
                "contamination": sig.get("contamination"),
                "identity_promoted_from": sig.get("identity_promoted_from"),
            }
        ok, reasons, suppressed = _evaluate_evidence_bundle(
            cc,
            iso=None,
            sig=sig,
            where=f"compiled[{idx}]",
        )
        if ok:
            return ExecutionTruthResult(
                confirmed=True,
                reasons=reasons,
                suppressed_legacy_flags=suppressed,
                promoted_to_executable=True,
                where=f"compiled[{idx}]",
            )
        if best is None or len(reasons) > len(best.reasons):
            best = ExecutionTruthResult(
                confirmed=False,
                reasons=reasons,
                suppressed_legacy_flags=suppressed,
                promoted_to_executable=False,
                where=f"compiled[{idx}]",
            )

    if best is None:
        return ExecutionTruthResult(confirmed=False, reasons=["no_evidence_bundle"])
    return best


def is_execution_truth_confirmed(mission: Any) -> bool:
    return compute_execution_truth(mission).confirmed


def should_suppress_legacy_capture_noise(mission: Any) -> bool:
    """UI + badges vista normal: ocultar ruido legacy si la verdad ya cerró."""
    return is_execution_truth_confirmed(mission)


def should_promote_to_executable(mission: Any) -> bool:
    """¿Hay ancla fuerte suficiente para tratar el camino como ejecutable
    salvo bloqueos humanos obligatorios (perfil/query)?"""
    return is_execution_truth_confirmed(mission)


def attach_execution_truth_audit(mission: Any) -> Optional[Dict[str, Any]]:
    """Persiste auditoría en ``mission._execution_truth_audit`` (best-effort)."""
    et = compute_execution_truth(mission)
    audit = et.to_audit_dict()
    try:
        setattr(mission, "_execution_truth_audit", audit)
    except Exception:
        pass
    return audit


__all__ = [
    "ExecutionTruthResult",
    "compute_execution_truth",
    "is_execution_truth_confirmed",
    "should_suppress_legacy_capture_noise",
    "should_promote_to_executable",
    "attach_execution_truth_audit",
]
