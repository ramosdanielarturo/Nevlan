"""COB Execution Pilot — Fase 1 (SAFE aggregates + SEP fallback).


Modo opcional cuando ``COB_EXECUTION_PILOT_ENABLED`` es True. NO sustituye
SmartExecutor cuando el piloto está desactivado o devuelve ``None``.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.contracts.mission import CanonicalOperationalBlock, Mission
from app.core.config import Settings, get_settings
from app.core.logger import log
from app.services.missions.action_runner import ActionRunner
from app.services.missions.cob_adapter_dry_run_bridge import (
    find_matching_sep_step,
    run_cob_adapter_dry_run_bridge,
)
from app.services.missions.cob_pilot_metrics import get_cob_pilot_metrics
from app.services.missions.cob_runtime_adapter import build_cob_runtime_adapter_report
from app.services.missions.canonical_operational_blocks import execution_truth_confirmed
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.smart_executor import MissionResult, StepOutcome
from app.services.missions.state_detector import StateDetector
from app.services.missions.uic_capability_registry import get_capability, validate_step_against_registry


PILOT_SAFE_CANONICAL_ACTIONS = frozenset(
    {
        "open_application",
        "open_new_tab",
        "navigate_to_site",
        "search_content_and_browse_results",
    },
)

_MICRO_CAP_SCROLL = "scroll_results"
_MICRO_CAP_REJECT = frozenset(
    {"select_visible_option", "self_heal_target", "click", "double_click"},
)

_COORD_HINTS = (
    "coord",
    "coordinate",
    "absolute_xy",
    "pixels",
    "coords_absolute",
    "vision_coords",
    "relative_coords",
    "visual_asset",
)

StatusCb = Optional[Callable[[str], None]]


def _strategy_has_coords_or_vision(strategy: Optional[str]) -> bool:
    o = "" if strategy is None else str(strategy).lower()
    return any(h in o for h in _COORD_HINTS)


def _norm_params(cob: CanonicalOperationalBlock) -> Dict[str, Any]:
    p = dict(cob.params or {})
    out = dict(p)
    qp = str(p.get("query_preview") or "").strip()
    if qp and not str(out.get("query") or "").strip():
        out["query"] = qp
    up = str(p.get("url_preview") or "").strip()
    if up and not str(out.get("url") or "").strip():
        out["url"] = up
    nh = str(p.get("navigation_url_hint") or "").strip()
    if nh and not out.get("url"):
        out["url"] = nh
    if qp and not str(out.get("app") or "").strip():
        out["app"] = qp
    return out


def _sep_steps(mission: Mission) -> List[Dict[str, Any]]:
    sep = mission.semantic_execution_plan or {}
    return [s for s in (sep.get("steps") or []) if isinstance(s, dict)]


def _adapter_flat(mission: Mission) -> List[Dict[str, Any]]:
    adapter = mission.cob_runtime_adapter_shadow or {}
    return [dict(r) for r in (adapter.get("flat_translated_steps") or []) if isinstance(r, dict)]


def _dry_evaluations(mission: Mission) -> List[Dict[str, Any]]:
    dry = mission.cob_dry_run_shadow or {}
    return [dict(r) for r in (dry.get("rows") or []) if isinstance(r, dict)]


def paired_adapter_and_dry(
    mission: Mission,
    *,
    rebuild: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if rebuild or not isinstance(mission.cob_dry_run_shadow, dict):
        if not (mission.cob_runtime_adapter_shadow or {}).get("flat_translated_steps"):
            rs = mission.cob_replay_shadow if isinstance(mission.cob_replay_shadow, dict) else None
            build_cob_runtime_adapter_report(mission, replay_snapshot=rs, persist=True)
        run_cob_adapter_dry_run_bridge(mission, persist=True, rebuild_adapter=False)
    flat = _adapter_flat(mission)
    drows = _dry_evaluations(mission)
    if len(drows) != len(flat):
        run_cob_adapter_dry_run_bridge(mission, persist=True, rebuild_adapter=False)
        drows = _dry_evaluations(mission)
    return flat, drows


def _rows_for_cob(
    flat: List[Dict[str, Any]],
    drows: List[Dict[str, Any]],
    cob_id: str,
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    n = min(len(flat), len(drows))
    for i in range(n):
        if str(flat[i].get("cob_id") or "") != cob_id:
            continue
        pairs.append((flat[i], drows[i]))
    return pairs


def _sep_range_for_cob(mission: Mission, cob_id: str) -> Tuple[Optional[int], Optional[int]]:
    flat, _ = paired_adapter_and_dry(mission)
    sep_steps_list = _sep_steps(mission)
    min_si: Optional[int] = None
    max_si: Optional[int] = None
    for row in flat:
        if str(row.get("cob_id") or "") != cob_id:
            continue
        si, _ = find_matching_sep_step(row, sep_steps_list)
        if si is not None:
            min_si = si if min_si is None else min(min_si, si)
            max_si = si if max_si is None else max(max_si, si)
    return min_si, max_si


def _sep_exclusive_upper_after_cob(mission: Mission, cob_id: str) -> Optional[int]:
    _mn, mx = _sep_range_for_cob(mission, cob_id)
    if mx is None:
        return None
    return int(mx) + 1


def _requires_human_from_cob_ambiguity(cob: CanonicalOperationalBlock) -> bool:
    if bool((cob.semantic_ambiguity_snapshot or {}).get("requires_human_confirmation")):
        return True
    if bool((cob.ambiguity or {}).get("requires_human_confirmation")):
        return True
    return False


def _multiple_targets_hypothesis(cob: CanonicalOperationalBlock) -> bool:
    if int((cob.ambiguity or {}).get("hypothesis_type_count") or 0) > 1:
        return True
    if int((cob.semantic_ambiguity_snapshot or {}).get("hypothesis_type_count") or 0) > 1:
        return True
    return False


def _replay_strategy_considered_valid(cob: CanonicalOperationalBlock) -> bool:
    rs = cob.replay_strategy or {}
    mode = str(rs.get("mode") or "").strip().lower()
    primary = str(rs.get("primary") or "").strip()
    if primary and mode in {"", "semantic", "hybrid"}:
        mode = mode or ("hybrid" if primary else "")
    return mode in {"semantic", "hybrid"} and bool(primary)


def evaluate_cob_execution_eligibility(
    mission: Mission,
    cob: CanonicalOperationalBlock,
    *,
    paired: Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]] = None,
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    cfg = settings or get_settings()
    failures: List[str] = []

    paired_rows = paired if paired is not None else paired_adapter_and_dry(mission, rebuild=False)
    flat, drows = paired_rows
    rows_cd = _rows_for_cob(flat, drows, cob.id)
    rp = dict(cob.replay_strategy or {})

    scores: Dict[str, float] = {
        "execution_truth_score": 0.0,
        "ambiguity_score": 0.0,
        "dry_run_supported_score": 0.0,
        "stability_score": float(cob.stability_score or 0.0),
        "portability_score": float(cob.portability_score or 0.0),
        "validator_quality": 0.0,
        "strategy_quality": 0.0,
        "target_identity_quality": 0.0,
    }

    truth_snap = cob.execution_truth_snapshot or {}
    truth_ok_flag = execution_truth_confirmed(dict(truth_snap))
    scores["execution_truth_score"] = 1.0 if truth_ok_flag else 0.0

    ambiguity_human = float(_requires_human_from_cob_ambiguity(cob))
    scores["ambiguity_score"] = 1.0 if not ambiguity_human else 0.0

    if _multiple_targets_hypothesis(cob):
        ambiguity_multi = True
        scores["ambiguity_score"] *= 0.65
        failures.append("MULTIPLE_TARGETS_HYPOTHESIS")
    else:
        ambiguity_multi = False

    if ambiguity_human:
        failures.append("SEMANTIC_REQUIRES_HUMAN_CONFIRMATION")

    if getattr(cfg, "COB_EXECUTION_PILOT_SAFE_ONLY", True) and (
        cob.canonical_action not in PILOT_SAFE_CANONICAL_ACTIONS
    ):
        failures.append("NOT_SAFE_WHITELIST_ACTION")

    if not cfg.COB_EXECUTION_PILOT_ENABLED:
        failures.append("PILOT_DISABLED")

    if not truth_ok_flag:
        failures.append("EXECUTION_TRUTH_UNCONFIRMED")

    dry_ok_rows = True
    if not rows_cd:
        dry_ok_rows = False
        failures.append("MISSING_ADAPTER_DRY_ROWS")
    else:
        for _row, dev in rows_cd:
            supported = bool(dev.get("supported"))
            vv = bool(dev.get("validator_available"))
            spass = bool(dev.get("safety_passed"))
            if not vv:
                failures.append("VALIDATOR_UNAVAILABLE_FOR_ROW")
            if not spass:
                failures.append(f"ROW_SAFETY_FAIL:{dev.get('blocker_reason')}")
            if not supported:
                dry_ok_rows = False
                br = dev.get("blocker_reason") or "DRY_RUN_UNSUPPORTED"
                failures.append(f"SUPPORTED_ROW_FALSE:{br}")
            cap_s = str(_row.get("uic_capability_id") or "").strip()
            if cap_s in _MICRO_CAP_REJECT:
                dry_ok_rows = False
                failures.append(f"COB_REJECT_CAP:{cap_s}")
            if cob.canonical_action in PILOT_SAFE_CANONICAL_ACTIONS and cap_s:
                cap_o = get_capability(cap_s)
                if cap_o and not callable(cap_o.outcome_validator):
                    failures.append(f"NO_CALLABLE_VALIDATOR:{cap_s}")
                    dry_ok_rows = False
            prim = ((_row.get("strategy") or {}) or {}).get("primary")
            if _strategy_has_coords_or_vision(prim):
                failures.append(f"PRIMARY_COORD_PRIMARY:{prim}")
                dry_ok_rows = False
            fb = (_row.get("fallback_policy") or {})
            safe_fb = fb.get("safe_fallbacks") or []
            for sfb in safe_fb:
                if _strategy_has_coords_or_vision(str(sfb)):
                    failures.append(f"SAFE_FB_COORD:{sfb}")
                    dry_ok_rows = False

    scores["dry_run_supported_score"] = 1.0 if dry_ok_rows else 0.0

    strat_ok = _replay_strategy_considered_valid(cob)
    scores["strategy_quality"] = 1.0 if strat_ok else 0.0
    if not strat_ok and bool(rp):
        failures.append("REPLAY_STRATEGY_WEAK_OR_EMPTY_PRIMARY")

    denom = max(len(rows_cd), 1)
    vf_ok_n = sum(
        1
        for _r, dev in rows_cd
        if bool(dev.get("validator_available")) and bool(dev.get("validator_passed"))
    )
    scores["validator_quality"] = round(vf_ok_n / denom, 4)

    intents = getattr(mission, "operational_intents", []) or []
    linked = sum(
        1 for it in intents if getattr(it, "session_id", "") == getattr(cob, "session_id", "")
    )
    scores["target_identity_quality"] = 1.0 if linked > 0 else 0.62

    if getattr(cfg, "COB_EXECUTION_REQUIRE_DRY_RUN", True) and mission.cob_dry_run_shadow is None:
        failures.append("DRY_RUN_REQUIRED_MISSING_SHADOW")

    overall = round(
        0.26 * scores["execution_truth_score"]
        + 0.18 * scores["ambiguity_score"]
        + 0.26 * scores["dry_run_supported_score"]
        + 0.10 * scores["stability_score"]
        + 0.08 * scores["portability_score"]
        + 0.06 * scores["validator_quality"]
        + 0.06 * scores["strategy_quality"],
        6,
    )

    eligible_gates = (
        (not ambiguity_human)
        and (not ambiguity_multi)
        and dry_ok_rows
        and truth_ok_flag
        and strat_ok
        and cfg.COB_EXECUTION_PILOT_ENABLED
        and (
            cob.canonical_action in PILOT_SAFE_CANONICAL_ACTIONS
            or not getattr(cfg, "COB_EXECUTION_PILOT_SAFE_ONLY", True)
        )
    )
    if getattr(cfg, "COB_EXECUTION_REQUIRE_DRY_RUN", True):
        eligible_gates = eligible_gates and mission.cob_dry_run_shadow is not None

    failures = sorted(set(str(f) for f in failures if f))
    return {
        "eligible": eligible_gates,
        "cob_id": cob.id,
        "canonical_action": cob.canonical_action,
        "failures": failures,
        "scores": scores,
        "overall_score": overall,
        "dry_run_micro_rows_checked": len(rows_cd),
        "paired_row_count_expectation": sum(
            1 for r in flat if str(r.get("cob_id") or "") == cob.id
        ),
    }


def build_cob_execution_plan(
    mission: Mission,
    cob: CanonicalOperationalBlock,
    *,
    paired: Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    paired_rows = paired if paired is not None else paired_adapter_and_dry(mission, rebuild=False)
    flat, drows = paired_rows
    rows_cd = _rows_for_cob(flat, drows, cob.id)
    sep_slice = _sep_steps(mission)
    return {
        "version": 1,
        "cob_id": cob.id,
        "canonical_action": cob.canonical_action,
        "session_id": getattr(cob, "session_id", ""),
        "norm_params_preview": {
            _k: _norm_params(cob).get(_k) for _k in ("app", "url", "query", "site")
        },
        "paired_rows": [
            {"adapter": r, "dry": d, "sep_match_index": find_matching_sep_step(r, sep_slice)[0]}
            for r, d in rows_cd
        ],
    }


def _estimate_sep_row_duration_ms(canonical_action_for_row: str) -> float:
    return {
        "open_application": 1400.0,
        "open_new_tab": 300.0,
        "navigate_to_site": 900.0,
        "search_content_and_browse_results": 950.0,
    }.get(canonical_action_for_row, 800.0)


def compare_cob_vs_sep_runtime(
    pilot_actual_ms_per_cob_block: Dict[str, float],
    *,
    mission: Mission,
    cob_sequence: List[CanonicalOperationalBlock],
) -> Dict[str, Any]:
    deltas: List[float] = []
    lines: List[str] = []
    _ = mission
    for cob in cob_sequence:
        actual = pilot_actual_ms_per_cob_block.get(cob.id)
        estimate = _estimate_sep_row_duration_ms(str(cob.canonical_action))
        if actual is None:
            continue
        delta = float(actual) - float(estimate)
        deltas.append(delta)
        lines.append(f"{cob.id}:{delta:.2f}ms vs SEP~{estimate:.0f}")
    return {
        "per_cob_deltas_summary": "; ".join(lines[:16]),
        "avg_delta_estimate_ms": (sum(deltas) / len(deltas)) if deltas else None,
    }


def _pseudo_sep_step(cap_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": cap_id, "params": dict(params), "needs_user_label": False}


def fallback_to_sep_runtime(
    *,
    reason: str,
    semantic_resume_index: int,
    cob_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "action": "sep_resume",
        "reason": reason,
        "semantic_start_step_index": max(0, int(semantic_resume_index)),
        "cob_id": cob_id,
    }


def _run_open_application_aggregate(
    cob: CanonicalOperationalBlock,
    runner: ActionRunner,
    detector: StateDetector,
) -> Tuple[bool, float, Optional[str]]:
    par = _norm_params(cob)
    name = str(par.get("app") or par.get("name") or "").strip()
    if not name:
        return False, 0.0, "MISSING_APP_NAME_FOR_OPEN_APPLICATION"
    t0 = time.time()
    st = detector.detect(deep=False, want_url=False)
    step = MissionStep(
        id=f"cobpit-open-app-{uuid.uuid4().hex[:8]}",
        kind="open_app",
        params={"name": name, "preferred_strategy": "open_app:windows_search"},
        human_label="COB Pilot: lanzar aplicación",
    )
    res = runner.run_strategy("open_app:windows_search", step, st)
    dt = time.time() - t0
    return res.ok, dt * 1000.0, None if res.ok else res.message


def _run_navigate_aggregate(
    cob: CanonicalOperationalBlock,
    runner: ActionRunner,
    detector: StateDetector,
) -> Tuple[bool, float, Optional[str]]:
    par = _norm_params(cob)
    url = str(par.get("url") or "").strip()
    if not url:
        return False, 0.0, "MISSING_URL"
    if validate_step_against_registry(_pseudo_sep_step("open_url", {"url": url})):
        return False, 0.0, "PRE_VALIDATOR_BLOCKED_OPEN_URL"
    t0 = time.time()
    step = MissionStep(
        id=f"cobpit-nav-{uuid.uuid4().hex[:8]}",
        kind="open_url",
        params={"url": url, "preferred_strategy": "open_url:hotkey_ctrl_l"},
        human_label="COB Pilot: navegar",
    )
    st = detector.detect(deep=False, want_url=True)
    res = runner.run_strategy("open_url:hotkey_ctrl_l", step, st)
    dt = time.time() - t0
    return res.ok, dt * 1000.0, None if res.ok else res.message


def _run_open_tab_aggregate(
    runner: ActionRunner,
    detector: StateDetector,
) -> Tuple[bool, float, Optional[str]]:
    t0 = time.time()
    step = MissionStep(
        id=f"cobpit-tab-{uuid.uuid4().hex[:8]}",
        kind="open_new_tab",
        params={"preferred_strategy": "open_new_tab:hotkey_ctrl_t"},
        human_label="COB Pilot: nueva pestaña",
    )
    st = detector.detect(deep=False, want_url=False)
    res = runner.run_strategy("open_new_tab:hotkey_ctrl_t", step, st)
    dt = time.time() - t0
    return res.ok, dt * 1000.0, None if res.ok else res.message


def _run_search_aggregate(
    cob: CanonicalOperationalBlock,
    runner: ActionRunner,
    detector: StateDetector,
) -> Tuple[bool, float, Optional[str]]:
    par = _norm_params(cob)
    q = str(par.get("query") or "").strip()
    site = str(par.get("site") or par.get("search_site") or par.get("provider") or "").strip()
    params: Dict[str, Any] = {"query": q, "preferred_strategy": "search_content:smart_route"}
    if site:
        params["site"] = site
    vr = validate_step_against_registry(_pseudo_sep_step("search_content", params))
    if vr:
        return False, 0.0, f"SEARCH_VALIDATOR_BLOCKED:{vr}"
    t0 = time.time()
    step = MissionStep(
        id=f"cobpit-search-{uuid.uuid4().hex[:8]}",
        kind="search_content",
        params=params,
        human_label="COB Pilot: búsqueda",
    )
    st = detector.detect(deep=False, want_url=True)
    res = runner.run_strategy("search_content:smart_route", step, st)
    dt = time.time() - t0
    return res.ok, dt * 1000.0, None if res.ok else res.message


def _run_scroll_aggregate(runner: ActionRunner, detector: StateDetector) -> Tuple[bool, float, Optional[str]]:
    t0 = time.time()
    step = MissionStep(
        id=f"cobpit-scroll-{uuid.uuid4().hex[:8]}",
        kind="scroll_page",
        params={
            "direction": "down",
            "amount_clicks": 1,
            "preferred_strategy": "scroll_page:wheel",
        },
        human_label="COB Pilot: scroll resultados",
    )
    st = detector.detect(deep=False, want_url=True)
    res = runner.run_strategy("scroll_page:wheel", step, st)
    dt = time.time() - t0
    return res.ok, dt * 1000.0, None if res.ok else res.message


def execute_cob_pilot_step(
    mission: Mission,
    cob: CanonicalOperationalBlock,
    *,
    runner: Optional[ActionRunner] = None,
    detector: Optional[StateDetector] = None,
) -> Dict[str, Any]:
    r = runner or ActionRunner()
    d = detector or StateDetector()
    timings: Dict[str, float] = {}
    errs: List[str] = []

    paired_rows = paired_adapter_and_dry(mission)
    flat, dro = paired_rows
    cob_rows = _rows_for_cob(flat, dro, cob.id)

    eligibility = evaluate_cob_execution_eligibility(mission, cob, paired=paired_rows)
    approx_ops_used = len(cob_rows) + 12

    if not eligibility["eligible"]:
        fb_i = (_sep_range_for_cob(mission, cob.id)[0] if _sep_range_for_cob(mission, cob.id)[0] is not None else 0)
        return {
            "ok": False,
            "fallback": fallback_to_sep_runtime(
                reason="ELIGIBILITY_FAIL",
                semantic_resume_index=fb_i,
                cob_id=cob.id,
            ),
            "eligibility": eligibility,
            "ops_used": approx_ops_used,
        }

    seq_start = time.time()
    action = cob.canonical_action or ""
    ops_executed = 0
    ok_total = True

    try:
        if action == "open_application":
            ok, ms, em = _run_open_application_aggregate(cob, r, d)
            timings["open_application_aggregate"] = ms
            ops_executed += 6
            if not ok:
                errs.append(em or "OPEN_APP_FAIL")
                ok_total = False
        elif action == "open_new_tab":
            ok, ms, em = _run_open_tab_aggregate(r, d)
            timings["open_new_tab_aggregate"] = ms
            ops_executed += 4
            if not ok:
                errs.append(em or "OPEN_TAB_FAIL")
                ok_total = False
        elif action == "navigate_to_site":
            ok, ms, em = _run_navigate_aggregate(cob, r, d)
            timings["navigate_aggregate"] = ms
            ops_executed += 6
            if not ok:
                errs.append(em or "NAVIGATE_FAIL")
                ok_total = False
        elif action == "search_content_and_browse_results":
            ok, ms, em = _run_search_aggregate(cob, r, d)
            timings["search_aggregate"] = ms
            ops_executed += 6
            if not ok:
                errs.append(em or "SEARCH_FAIL")
                ok_total = False
            else:
                want_scroll = any(
                    str(rr.get("uic_capability_id") or "").strip().lower() == _MICRO_CAP_SCROLL
                    for rr, _ in cob_rows
                )
                if want_scroll:
                    ok2, ms2, em2 = _run_scroll_aggregate(r, d)
                    timings["scroll_results_micro"] = ms2
                    ops_executed += 4
                    if not ok2:
                        errs.append(em2 or "SCROLL_FAIL")
                        ok_total = False
        else:
            ok_total = False
            errs.append(f"UNSUPPORTED_AGGREGATE_FOR:{action}")
    except Exception as ex:
        ok_total = False
        errs.append(str(ex))

    elapsed = (time.time() - seq_start) * 1000.0
    mn_si, mx_si = _sep_range_for_cob(mission, cob.id)
    nx = mx_si + 1 if mx_si is not None else None

    if not ok_total:
        fb_idx = mn_si if mn_si is not None else 0
        get_cob_pilot_metrics().record_fallback()
        out_payload = {
            "ok": False,
            "fallback": fallback_to_sep_runtime(
                reason="EXEC_FAIL:" + ";".join(errs),
                semantic_resume_index=fb_idx,
                cob_id=cob.id,
            ),
            "elapsed_ms_total": elapsed,
            "micro_timings": timings,
            "errors": errs,
            "eligible": eligibility,
            "ops_used": ops_executed or approx_ops_used,
            "exclusive_sep_upper_seen": nx,
        }
        try:
            _cfg_fb = get_settings()
            if getattr(_cfg_fb, "ARL_ENABLED", False):
                from app.services.runtime.adaptive_runtime_layer import (
                    record_cob_aggregate_failure_audit,
                )

                record_cob_aggregate_failure_audit(
                    mission,
                    canonical_action=str(action),
                    cob_params=_norm_params(cob),
                    detector=d,
                    execution_errors=errs,
                    retry_budget_hint=int(getattr(_cfg_fb, "ARL_MAX_RETRIES", 1) or 1),
                )
        except Exception as ex:
            log.debug("ARL audit (COB aggregate failure): {}", ex)
        try:
            if getattr(get_settings(), "SLL_ENABLED", False):
                from app.services.runtime.strategy_learning_layer import (
                    record_cob_pilot_observation,
                )

                record_cob_pilot_observation(
                    mission,
                    cob,
                    ok=False,
                    elapsed_ms=float(elapsed),
                    fell_back=True,
                )
        except Exception as ex2:
            log.debug("SLL COB pilot record (fail): {}", ex2)
        try:
            if getattr(get_settings(), "OMPC_ENABLED", False):
                from app.services.runtime.operational_memory import observe_cob_pilot_chain

                observe_cob_pilot_chain(
                    mission,
                    cob,
                    ok=False,
                    elapsed_ms=float(elapsed),
                    fell_back=True,
                )
        except Exception as ex3:
            log.debug("OMPC cob chain (fail): {}", ex3)
        return out_payload

    get_cob_pilot_metrics().record_primitive_op(ops_executed)
    sep_est = _estimate_sep_row_duration_ms(str(cob.canonical_action))
    get_cob_pilot_metrics().record_execution_delta_ms(elapsed, sep_est)
    try:
        if getattr(get_settings(), "SLL_ENABLED", False):
            from app.services.runtime.strategy_learning_layer import (
                record_cob_pilot_observation,
            )

            record_cob_pilot_observation(
                mission,
                cob,
                ok=True,
                elapsed_ms=float(elapsed),
                fell_back=False,
            )
    except Exception as ex:
        log.debug("SLL COB pilot record: {}", ex)
    try:
        if getattr(get_settings(), "OMPC_ENABLED", False):
            from app.services.runtime.operational_memory import observe_cob_pilot_chain

            observe_cob_pilot_chain(
                mission,
                cob,
                ok=True,
                elapsed_ms=float(elapsed),
                fell_back=False,
            )
    except Exception as ex3:
        log.debug("OMPC cob chain (ok): {}", ex3)
    return {
        "ok": True,
        "elapsed_ms_total": elapsed,
        "micro_timings": timings,
        "eligible_before_run": eligibility,
        "exclusive_sep_upper": nx,
        "ops_used": ops_executed,
    }


def build_cob_pilot_audit(
    mission: Mission,
    *,
    events: List[Dict[str, Any]],
    comparisons: Dict[str, Any],
) -> Dict[str, Any]:
    m = get_cob_pilot_metrics().dump_to_dict()
    eligible_blocks: List[str] = []
    pilot_exec: List[str] = []
    fallbacks: List[Dict[str, Any]] = []
    last_elig: Dict[str, Any] = {}
    for evt in events:
        if evt.get("type") == "eligibility_checked":
            cid = str(evt["cob_id"])
            last_elig[cid] = evt
            if evt.get("eligible"):
                eligible_blocks.append(cid)
        elif evt.get("type") == "pilot_execute_ok":
            pilot_exec.append(str(evt["cob_id"]))
        elif evt.get("type") == "fallback":
            fallbacks.append(dict(evt))
    return {
        "version": 2,
        "generated_by": "cob_execution_pilot",
        "events": events,
        "comparison": comparisons,
        "eligible_block_ids_seen": sorted(set(eligible_blocks)),
        "pilot_executed_ids": pilot_exec,
        "fallback_records": fallbacks,
        "last_eligibility_snapshots_by_cob_id": last_elig,
        "metrics_snapshot_global": m,
    }


def _synth_step_out(sid: str, label: str, ok: bool) -> StepOutcome:
    return StepOutcome(
        step_id=sid,
        kind="cob_execution_pilot",
        status="success" if ok else "failed",
        strategy_used="cob_pilot_aggregate_safe",
        duration_ms=0,
        human_message=label,
    )


def cob_pilot_try_prefix_or_fallback(
    mission: Mission,
    *,
    semantic_step_count: int,
    start_step_index: int = 0,
    on_status: StatusCb = None,
    paired: Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]] = None,
) -> Optional[Dict[str, Any]]:
    cfg = get_settings()
    audit_events: List[Dict[str, Any]] = []

    def _tell(msg: str) -> None:
        if callable(on_status):
            try:
                on_status(msg)
            except Exception:
                pass

    if not getattr(cfg, "COB_EXECUTION_PILOT_ENABLED", False):
        return None
    if start_step_index > 0:
        return None
    cobs = mission.canonical_operational_blocks or []
    if not cobs:
        return None

    paired_rows = paired if paired is not None else paired_adapter_and_dry(mission, rebuild=True)
    if getattr(cfg, "COB_EXECUTION_REQUIRE_DRY_RUN", True):
        flat_chk, _ = paired_rows
        dry_chk = mission.cob_dry_run_shadow
        if not flat_chk or dry_chk is None or not dry_chk.get("rows"):
            return None

    mts = get_cob_pilot_metrics()
    mts.record_attempt()

    runner = ActionRunner()
    detector = StateDetector()
    outcomes: List[StepOutcome] = []
    timings_by_cob: Dict[str, float] = {}

    sep_cursor_exclusive = max(0, int(start_step_index))
    cob_sequence: List[CanonicalOperationalBlock] = []
    ops_budget_used = 0
    cfg_max = int(getattr(cfg, "COB_EXECUTION_MAX_STEPS", 24) or 24)
    pilot_run_id = f"cob-pilot-{getattr(mission, 'id', 'mission')}-{uuid.uuid4().hex[:8]}"
    wall_t0 = time.time()

    ordered = sorted(list(cobs), key=lambda c: int(getattr(c, "execution_priority", 50) or 50))

    for cob in ordered:
        if semantic_step_count > 0 and sep_cursor_exclusive >= semantic_step_count:
            break

        if getattr(cfg, "COB_EXECUTION_PILOT_SAFE_ONLY", True) and (
            cob.canonical_action not in PILOT_SAFE_CANONICAL_ACTIONS
        ):
            audit_events.append({"type": "cob_skipped_not_whitelisted", "cob_id": cob.id})
            continue

        eligibility = evaluate_cob_execution_eligibility(mission, cob, paired=paired_rows, settings=cfg)
        projected = max(
            8,
            int(eligibility.get("dry_run_micro_rows_checked") or 0) + 6,
        )
        if ops_budget_used + projected > cfg_max:
            fb = fallback_to_sep_runtime(
                reason="MAX_STEPS_BY_BUDGET",
                semantic_resume_index=sep_cursor_exclusive,
                cob_id=cob.id,
            )
            audit_events.append({"type": "fallback", "fallback": fb})
            mts.record_fallback()
            comp = compare_cob_vs_sep_runtime(timings_by_cob, mission=mission, cob_sequence=cob_sequence)
            audit = build_cob_pilot_audit(mission, events=audit_events, comparisons=comp)
            mission.cob_execution_pilot_audit = audit
            return {
                "completed_all_via_pilot": False,
                "next_sep_step_index": fb["semantic_start_step_index"],
                "mission_result": None,
                "audit_final": audit,
                "pilot_wall_ms": (time.time() - wall_t0) * 1000.0,
                "fallback_at": fb,
            }

        audit_events.append({
            "type": "eligibility_checked",
            "cob_id": cob.id,
            "eligible": bool(eligibility.get("eligible")),
            "scores": eligibility.get("scores"),
            "failures": list(eligibility.get("failures") or []),
        })

        if not eligibility.get("eligible"):
            fb = fallback_to_sep_runtime(
                reason="ELIGIBILITY_FAIL_ORCHESTRATION",
                semantic_resume_index=sep_cursor_exclusive,
                cob_id=cob.id,
            )
            audit_events.append({"type": "fallback", "fallback": fb})
            mts.record_fallback()
            comp = compare_cob_vs_sep_runtime(timings_by_cob, mission=mission, cob_sequence=cob_sequence)
            audit = build_cob_pilot_audit(mission, events=audit_events, comparisons=comp)
            mission.cob_execution_pilot_audit = audit
            return {
                "completed_all_via_pilot": False,
                "next_sep_step_index": fb["semantic_start_step_index"],
                "mission_result": None,
                "audit_final": audit,
                "pilot_wall_ms": (time.time() - wall_t0) * 1000.0,
                "fallback_at": fb,
            }

        cob_sequence.append(cob)
        _tell(f"COB Pilot: «{cob.canonical_action}»")

        exe = execute_cob_pilot_step(mission, cob, runner=runner, detector=detector)
        ops_budget_used += int(exe.get("ops_used") or 0)

        if not exe.get("ok"):
            fb = exe.get("fallback") or fallback_to_sep_runtime(
                reason="EXEC_FAIL",
                semantic_resume_index=sep_cursor_exclusive,
                cob_id=cob.id,
            )
            audit_events.append({"type": "fallback", "fallback": fb})
            comp = compare_cob_vs_sep_runtime(timings_by_cob, mission=mission, cob_sequence=cob_sequence)
            audit = build_cob_pilot_audit(mission, events=audit_events, comparisons=comp)
            mission.cob_execution_pilot_audit = audit
            mts.record_fallback()
            return {
                "completed_all_via_pilot": False,
                "next_sep_step_index": fb["semantic_start_step_index"],
                "mission_result": None,
                "audit_final": audit,
                "pilot_wall_ms": (time.time() - wall_t0) * 1000.0,
                "fallback_at": fb,
            }

        timings_by_cob[cob.id] = float(exe.get("elapsed_ms_total") or 0.0)
        audit_events.append({
            "type": "pilot_execute_ok",
            "cob_id": cob.id,
            "elapsed_ms": timings_by_cob[cob.id],
        })
        outcomes.append(_synth_step_out(cob.id, f"COB Pilot «{cob.canonical_action}» OK", True))

        up = exe.get("exclusive_sep_upper")
        if up is not None:
            sep_cursor_exclusive = max(sep_cursor_exclusive, int(up))
        else:
            ux = _sep_exclusive_upper_after_cob(mission, cob.id)
            if ux is not None:
                sep_cursor_exclusive = max(sep_cursor_exclusive, int(ux))

    comp_f = compare_cob_vs_sep_runtime(timings_by_cob, mission=mission, cob_sequence=cob_sequence)
    audit = build_cob_pilot_audit(mission, events=audit_events, comparisons=comp_f)
    mission.cob_execution_pilot_audit = audit

    completed_all = semantic_step_count > 0 and sep_cursor_exclusive >= semantic_step_count

    mr: Optional[MissionResult]
    if completed_all:
        mts.record_success()
        mr = MissionResult(
            run_id=pilot_run_id,
            status="success",
            outcomes=outcomes,
            finished_at=time.time(),
            last_message="Ejecución vía COB Execution Pilot",
        )
    else:
        mr = None

    return {
        "completed_all_via_pilot": completed_all,
        "next_sep_step_index": sep_cursor_exclusive,
        "mission_result": mr,
        "audit_final": audit,
        "pilot_wall_ms": (time.time() - wall_t0) * 1000.0,
        "cob_sequence_order": [c.id for c in cob_sequence],
    }


__all__ = [
    "evaluate_cob_execution_eligibility",
    "build_cob_execution_plan",
    "execute_cob_pilot_step",
    "fallback_to_sep_runtime",
    "compare_cob_vs_sep_runtime",
    "build_cob_pilot_audit",
    "paired_adapter_and_dry",
    "cob_pilot_try_prefix_or_fallback",
    "PILOT_SAFE_CANONICAL_ACTIONS",
]
