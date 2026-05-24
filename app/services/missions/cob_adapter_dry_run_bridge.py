"""COB Adapter Dry-Run Bridge — valida filas del adapter contra UIC/SEP sin ejecutar."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import Mission
from app.core.logger import log
from app.services.missions.canonical_operational_blocks import COB_TO_SEP_TYPES
from app.services.missions.cob_runtime_adapter import (
    _SEP_CAP_RELATED_ADAPTER_CAPS,
    build_cob_runtime_adapter_report,
)
from app.services.missions.uic_capability_registry import (
    get_capability,
    validate_step_against_registry,
)
from app.services.missions.uic_contract_catalog import UIC_CONTRACT_ROWS

_COORD_MARKERS: Tuple[str, ...] = (
    "coord",
    "coordinate",
    "absolute_xy",
    "pixels",
    "coords_absolute",
)


def _catalog_cap_for_sep_type(sep_type: str) -> Optional[str]:
    t = str(sep_type or "").strip().lower()
    for row in UIC_CONTRACT_ROWS:
        if row.sep_type.lower() == t:
            return row.capability_id
    return None


def _strategy_has_coord_token(s: Optional[str]) -> bool:
    if not s:
        return False
    sl = s.lower()
    return any(m in sl for m in _COORD_MARKERS)


def _sep_steps_list(mission: Mission) -> List[Dict[str, Any]]:
    sep = mission.semantic_execution_plan or {}
    return [s for s in (sep.get("steps") or []) if isinstance(s, dict)]


def find_matching_sep_step(
    row: Dict[str, Any],
    sep_steps: List[Dict[str, Any]],
) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Empareja una fila adapter con el primer paso SEP relacionado."""
    ca = str(row.get("canonical_action") or "")
    cap_id = row.get("uic_capability_id")
    bucket = COB_TO_SEP_TYPES.get(ca, set())

    for i, step in enumerate(sep_steps):
        t = str(step.get("type") or "").lower()
        if t in bucket:
            return i, step

    if cap_id:
        for i, step in enumerate(sep_steps):
            t = str(step.get("type") or "").lower()
            sc = _catalog_cap_for_sep_type(t)
            if not sc:
                continue
            related = _SEP_CAP_RELATED_ADAPTER_CAPS.get(sc, frozenset())
            if str(cap_id) == sc or str(cap_id) in related:
                return i, step

    return None, None


def _effective_required_param_names(cap: Any, row: Dict[str, Any]) -> Tuple[str, ...]:
    """Ajusta required_params del registry a fases COB (p.ej. superficie launcher)."""
    pid = str(row.get("phase_id") or "")
    cid = str(cap.capability_id or "")
    if cid == "open_app" and pid == "open_system_launch_surface":
        return ()
    return tuple(cap.required_params or ())


def _params_complete(cap: Optional[Any], row: Dict[str, Any]) -> bool:
    if cap is None:
        return False
    req = _effective_required_param_names(cap, row)
    if not req:
        return True
    params = dict(row.get("required_params") or {})
    for name in req:
        v = params.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            return False
    return True


def _safety_passed(cap: Optional[Any], row: Dict[str, Any]) -> Tuple[bool, str]:
    strat = row.get("strategy") or {}
    primary = strat.get("primary")
    if _strategy_has_coord_token(primary):
        return False, "SAFETY_PRIMARY_STRATEGY_COORDS"

    fb = row.get("fallback_policy") or {}
    rejected = fb.get("rejected_coord_fallbacks") or []
    if rejected:
        # Fallbacks coord rechazados: OK para shadow; no fallan safety si primary limpio.
        pass

    safe_fb = fb.get("safe_fallbacks") or []
    for s in safe_fb:
        if _strategy_has_coord_token(str(s)):
            return False, "SAFETY_ACCEPTED_FALLBACK_COORDS"

    if cap is not None and getattr(cap, "allows_coords_emergency", False):
        return False, "SAFETY_CAPABILITY_ALLOWS_COORD_EMERGENCY_SHADOW_REJECT"

    if "coord_fallbacks_rejected" in (row.get("safety_flags") or []):
        # Intencionalmente rechazamos coords en adapter — no es blocker de safety.
        pass

    return True, ""


def _pseudo_sep_step(cap_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": cap_id,
        "params": dict(params or {}),
        "needs_user_label": False,
    }


def evaluate_adapter_row_dry_run(
    mission: Mission,
    row: Dict[str, Any],
) -> Dict[str, Any]:
    """Evalúa una fila de ``flat_translated_steps`` (shadow-only)."""
    sep_steps = _sep_steps_list(mission)
    executor = str(row.get("executor_action") or "")
    cap_id_raw = row.get("uic_capability_id")
    cap_id = str(cap_id_raw).strip() if cap_id_raw else ""

    ca = str(row.get("canonical_action") or "")
    order = row.get("plan_step_order")
    phase_id = str(row.get("phase_id") or "")

    human_readable_diff = (
        f"COB[{ca}]#{order}:{phase_id} executor={executor} cap={cap_id or '∅'}"
    )

    if executor in {"UNSUPPORTED_SHADOW", "UNSUPPORTED_SHADOW_SCAN"}:
        reason = str(row.get("unsupported_reason") or "UNSUPPORTED_SHADOW_ROW")
        sep_i, sep_step = find_matching_sep_step(row, sep_steps)
        if sep_step is not None:
            sp = sep_step.get("params") or {}
            human_readable_diff += (
                f" vs SEP[{sep_i}] type={sep_step.get('type')} params_keys={list(sp.keys())}"
            )
        return {
            "cob_id": row.get("cob_id"),
            "plan_step_order": order,
            "phase_id": phase_id,
            "supported": False,
            "registry_valid": bool(cap_id and get_capability(cap_id)),
            "validator_available": bool(cap_id and get_capability(cap_id)),
            "params_complete": False,
            "safety_passed": False,
            "sep_alignment": sep_i is not None or not sep_steps,
            "validator_passed": False,
            "registry_message": None,
            "blocker_reason": reason,
            "human_readable_diff": human_readable_diff + f" | BLOCKED:{reason}",
        }

    cap = get_capability(cap_id) if cap_id else None
    registry_valid = cap is not None
    validator_available = (
        cap is not None and callable(getattr(cap, "outcome_validator", None))
    )

    if not registry_valid:
        sep_i, sep_step = find_matching_sep_step(row, sep_steps)
        diff_tail = ""
        if sep_step is not None:
            diff_tail = f" vs SEP[{sep_i}] type={sep_step.get('type')}"
        return {
            "cob_id": row.get("cob_id"),
            "plan_step_order": order,
            "phase_id": phase_id,
            "supported": False,
            "registry_valid": False,
            "validator_available": False,
            "params_complete": False,
            "safety_passed": False,
            "sep_alignment": sep_i is not None or not sep_steps,
            "validator_passed": False,
            "registry_message": "UNKNOWN_UIC_CAPABILITY",
            "blocker_reason": "UNKNOWN_CAPABILITY",
            "human_readable_diff": human_readable_diff + diff_tail + " | UNKNOWN_CAPABILITY",
        }

    params = dict(row.get("required_params") or {})
    pseudo = _pseudo_sep_step(cap_id, params)
    reg_msg = validate_step_against_registry(pseudo)
    validator_passed = reg_msg is None

    params_complete = _params_complete(cap, row)
    safety_ok, safety_blocker = _safety_passed(cap, row)

    sep_i, sep_step = find_matching_sep_step(row, sep_steps)
    sep_alignment = (sep_i is not None) or (not sep_steps)

    sparams = {}
    if sep_step is not None:
        sparams = sep_step.get("params") or {}
        human_readable_diff += (
            f" ↔ SEP[{sep_i}] type={sep_step.get('type')} "
            f"keys_cob={sorted(params.keys())} keys_sep={sorted(sparams.keys())}"
        )

    blocker_reason = ""
    if not params_complete:
        blocker_reason = "INCOMPLETE_PARAMS"
    elif not safety_ok:
        blocker_reason = safety_blocker
    elif not validator_passed:
        blocker_reason = f"VALIDATOR_FAILED:{reg_msg or 'outcome_validator'}"
    elif not sep_alignment and sep_steps:
        blocker_reason = "SEP_MISMATCH"

    supported = (
        registry_valid
        and validator_available
        and validator_passed
        and params_complete
        and safety_ok
        and sep_alignment
    )

    if not supported and not blocker_reason:
        blocker_reason = "DRY_RUN_FAILED"

    if supported:
        human_readable_diff += " | OK dry-run"

    return {
        "cob_id": row.get("cob_id"),
        "plan_step_order": order,
        "phase_id": phase_id,
        "supported": supported,
        "registry_valid": registry_valid,
        "validator_available": validator_available,
        "params_complete": params_complete,
        "safety_passed": safety_ok,
        "sep_alignment": sep_alignment,
        "validator_passed": validator_passed,
        "registry_message": reg_msg,
        "blocker_reason": blocker_reason,
        "human_readable_diff": human_readable_diff,
    }


def run_cob_adapter_dry_run_bridge(
    mission: Mission,
    *,
    persist: bool = True,
    rebuild_adapter: bool = False,
) -> Dict[str, Any]:
    """Valida ``cob_runtime_adapter_shadow`` completo (sin ejecutar runtime real)."""
    adapter = mission.cob_runtime_adapter_shadow
    if adapter is None or rebuild_adapter:
        try:
            rs = mission.cob_replay_shadow if isinstance(mission.cob_replay_shadow, dict) else None
            build_cob_runtime_adapter_report(mission, replay_snapshot=rs, persist=True)
            adapter = mission.cob_runtime_adapter_shadow
        except Exception as e:
            log.debug(f"dry-run rebuild adapter: {e}")
            adapter = None

    flat: List[Dict[str, Any]] = []
    if isinstance(adapter, dict):
        flat = list(adapter.get("flat_translated_steps") or [])

    evaluations: List[Dict[str, Any]] = []
    for row in flat:
        evaluations.append(evaluate_adapter_row_dry_run(mission, row))

    supported_n = sum(1 for e in evaluations if e.get("supported"))
    hist: Dict[str, int] = {}
    for e in evaluations:
        br = str(e.get("blocker_reason") or "").strip() or ("OK" if e.get("supported") else "UNKNOWN")
        hist[br] = hist.get(br, 0) + 1

    compact_diff_lines = [e["human_readable_diff"] for e in evaluations[:24]]

    report = {
        "version": 1,
        "generated_by": "cob_adapter_dry_run_bridge",
        "shadow_only": True,
        "rows": evaluations,
        "summary": {
            "total_rows": len(evaluations),
            "supported_count": supported_n,
            "supported_ratio": round(supported_n / max(len(evaluations), 1), 4)
            if evaluations
            else None,
            "blocker_histogram": hist,
        },
        "compact_diff_preview": compact_diff_lines,
    }
    if persist:
        mission.cob_dry_run_shadow = report
    return report


def rebuild_cob_dry_run_shadow_only(mission: Mission) -> None:
    if not (mission.cob_runtime_adapter_shadow or {}).get("flat_translated_steps"):
        mission.cob_dry_run_shadow = None
        return
    try:
        run_cob_adapter_dry_run_bridge(mission, persist=True, rebuild_adapter=False)
    except Exception as e:
        log.debug(f"rebuild_cob_dry_run_shadow_only: {e}")
