"""Adaptive Runtime Layer (ARL) — Fase 1: tolerancia SAFE a cambios de entorno."""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.logger import log
from app.services.runtime.arl_metrics import get_arl_metrics

SAFE_DEVIATION_TYPES = frozenset(
    {
        "text_variation",
        "label_translation",
        "layout_shift",
        "relative_position_shift",
        "minor_dom_change",
        "scroll_offset_change",
        "resolution_change",
        "zoom_change",
        "cosmetic_container_change",
        "none",
    },
)
UNSAFE_DEVIATION_TYPES = frozenset(
    {
        "unknown_target",
        "multiple_possible_targets",
        "destructive_action_uncertain",
        "auth_flow_change",
        "modal_blocking_unknown",
        "payment_flow_change",
    },
)


class ArlDecision(str, Enum):
    CONTINUE = "CONTINUE"
    ADAPT = "ADAPT"
    REVALIDATE = "REVALIDATE"
    FALLBACK_TO_SEP = "FALLBACK_TO_SEP"
    REQUEST_HUMAN = "REQUEST_HUMAN"


_SEM_GROUPS: Tuple[frozenset[str], ...] = (
    frozenset({"buscar", "search", "find", "buscador", "finder"}),
    frozenset({"enviar", "send", "submit", "enter"}),
    frozenset({"siguiente", "next", "continuar", "continue"}),
    frozenset({"atras", "back", "volver"}),
)
_STOP = frozenset(
    {"el", "la", "los", "las", "un", "una", "de", "del", "y", "o", "a",
     "the", "of", "to", "for", "and", "in", "is"},
)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _toks(s: str) -> List[str]:
    return [t for t in _norm(s).split() if t and t not in _STOP]


def lightweight_token_overlap_similarity(a: str, b: str) -> float:
    sa, sb = set(_toks(a)), set(_toks(b))
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    u = len(sa | sb) or 1
    return round(len(sa & sb) / u, 6)


def lightweight_semantic_table_match(expected: str, observed: str) -> Tuple[bool, float]:
    ne, no = _norm(expected), _norm(observed)
    if not ne or not no:
        return False, 0.0
    if ne == no:
        return True, 1.0
    te, to = _toks(ne), _toks(no)
    for g in _SEM_GROUPS:
        if any(t in g for t in te) and any(t in g for t in to):
            return True, 0.92
    j = lightweight_token_overlap_similarity(ne, no)
    return j >= 0.45, round(j, 6)


def detect_runtime_deviation(
    *,
    recorded_signature: Dict[str, Any],
    observed_state: Dict[str, Any],
    session_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sess = dict(session_context or {})
    kinds: List[str] = []
    mag = 0.0

    rt = str(recorded_signature.get("window_title_fragment") or "")
    ot = str(observed_state.get("active_window_title") or "")
    if rt and ot:
        ts = lightweight_token_overlap_similarity(rt, ot)
        if ts < 0.35:
            kinds.append("layout_shift")
            mag = max(mag, 1.0 - ts)

    rh = str(recorded_signature.get("url_host_fragment") or "")
    ou = str(observed_state.get("browser_url") or "")
    if rh and ou and rh.lower() not in ou.lower():
        kinds.append("unknown_target")
        mag = max(mag, 0.85)

    rc0, rc1 = recorded_signature.get("resolution_class"), observed_state.get("resolution_class")
    if rc0 is not None and rc1 is not None and str(rc0) != str(rc1):
        kinds.append("resolution_change")
        mag = max(mag, 0.35)

    z0, z1 = recorded_signature.get("zoom_bucket"), observed_state.get("zoom_bucket")
    if z0 is not None and z1 is not None and z0 != z1:
        kinds.append("zoom_change")
        mag = max(mag, 0.3)

    rf, of = recorded_signature.get("dom_fingerprint_weak"), observed_state.get("dom_fingerprint_weak")
    if isinstance(rf, str) and isinstance(of, str) and rf and of and rf != of:
        sim = lightweight_token_overlap_similarity(rf, of)
        kinds.append("minor_dom_change")
        mag = max(mag, max(1.0 - sim, 0.25))

    if sess.get("scroll_offset_bucket_changed"):
        kinds.append("scroll_offset_change")

    rlab = recorded_signature.get("target_label_exact")
    ocand = observed_state.get("visible_label_candidates") or []
    if rlab and isinstance(ocand, (list, tuple)):
        if not any(lightweight_semantic_table_match(str(rlab), str(x))[0] for x in ocand):
            kinds.append("text_variation")

    eb = recorded_signature.get("expected_search_button_text") or ""
    ob = observed_state.get("observed_search_button_text") or ""
    if eb and ob and lightweight_semantic_table_match(str(eb), str(ob))[0]:
        kinds.append("label_translation")

    if sess.get("extra_sidebar_detected"):
        kinds.append("cosmetic_container_change")

    if recorded_signature.get("relative_anchor_shift_hint"):
        kinds.append("relative_position_shift")

    if sess.get("validator_partial_retry"):
        kinds.append("minor_dom_change")
        mag = max(mag, 0.28)

    amb = int(sess.get("ambiguous_target_count") or 0)

    out: Dict[str, Any]
    if not kinds:
        out = {"deviation_detected": False, "deviation_types": [], "magnitude_estimate": 0.0}
    else:
        uq = sorted(set(kinds))
        out = {
            "deviation_detected": True,
            "deviation_types": uq,
            "magnitude_estimate": round(min(1.0, mag), 4),
            "signals": sess,
        }
        if amb >= 2:
            out["risk_flags"] = ["multiple_ambiguous_signals"]
            out["ambiguous_target_count"] = amb

    log.debug("ARL deviation %s", out)
    return out


def classify_deviation(deviation_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    kinds = list(deviation_snapshot.get("deviation_types") or [])
    if not deviation_snapshot.get("deviation_detected"):
        return {"risk": "SAFE", "primary_type": "none", "kinds": []}
    rf = deviation_snapshot.get("risk_flags") or []
    if "multiple_ambiguous_signals" in rf:
        return {"risk": "NO_SAFE", "primary_type": "multiple_possible_targets", "kinds": kinds}
    if "unknown_target" in kinds:
        return {"risk": "NO_SAFE", "primary_type": "unknown_target", "kinds": kinds}
    for u in UNSAFE_DEVIATION_TYPES.intersection(set(kinds)):
        return {"risk": "NO_SAFE", "primary_type": u, "kinds": kinds}
    prim = ""
    for s in SAFE_DEVIATION_TYPES:
        if s in kinds:
            prim = s
            break
    if not prim:
        prim = kinds[0] if kinds else "none"
    return {"risk": "SAFE", "primary_type": prim, "kinds": kinds}


def _resolution_class(screen_size: Optional[Tuple[int, int]]) -> Optional[str]:
    if not screen_size or len(screen_size) < 2:
        return None
    h = int(screen_size[1])
    if h >= 1440:
        return "xlarge"
    if h >= 1080:
        return "1080p_plus"
    if h >= 900:
        return "900p"
    return "below_900"


def _zoom_bucket(dpi_scale: Optional[float]) -> Optional[str]:
    if dpi_scale is None:
        return None
    step = round(float(dpi_scale) * 4) / 4.0
    return f"z{step:.2f}"


def state_snapshot_to_observed_state(snapshot: Any) -> Dict[str, Any]:
    """Convierte un :class:`~app.services.missions.state_detector.StateSnapshot` en dict Observado."""
    if snapshot is None:
        return {}

    def _pick(*names: str) -> Optional[Any]:
        for n in names:
            if hasattr(snapshot, n):
                return getattr(snapshot, n)
        return None

    ss = _pick("screen_size")
    dpi = _pick("dpi_scale")
    cand: List[str] = []
    uia_names = _pick("uia_visible_names") or ()
    dom_names = getattr(snapshot, "dom_visible_names", None)
    vt = _pick("visible_text")
    if isinstance(uia_names, (list, tuple)):
        cand.extend(str(x) for x in uia_names[:24] if x)
    if isinstance(dom_names, (list, tuple)):
        cand.extend(str(x) for x in dom_names[:24] if x)
    if vt and isinstance(vt, str) and vt.strip():
        dom_weak = vt.strip()[:400]
    else:
        dom_weak = " ".join(sorted(set(_toks(" ".join(cand))))[:32])[:200] if cand else ""

    observed: Dict[str, Any] = {
        "active_window_title": (_pick("active_window_title") or _pick("browser_title") or "") or "",
        "browser_url": _pick("browser_url") or "",
        "resolution_class": _resolution_class(tuple(ss) if isinstance(ss, tuple) else None),
        "zoom_bucket": _zoom_bucket(float(dpi) if dpi is not None else None),
        "dom_fingerprint_weak": dom_weak,
        "visible_label_candidates": cand[:48],
        "detect_ms": _pick("detect_ms"),
        "detect_errors": list(_pick("errors") or ()),
    }
    return observed


def build_recorded_operational_hints(
    *,
    canonical_action: str,
    cob_params: Dict[str, Any],
    mission_home_url_hint: Optional[str] = None,
) -> Dict[str, Any]:
    p = dict(cob_params or {})
    qp = str(p.get("query_preview") or "").strip()
    query = str(p.get("query") or qp).strip()
    url = str(p.get("url") or str(p.get("navigation_url_hint") or "")).strip()
    app = str(p.get("app") or p.get("name") or query).strip()
    frag_host = ""
    if url:
        try:
            from urllib.parse import urlparse

            frag_host = (urlparse(url).hostname or "").lower()
        except Exception:
            frag_host = url.lower().split("/")[0].replace(":", "")
    mh = mission_home_url_hint or ""
    if not frag_host and mh:
        try:
            from urllib.parse import urlparse

            frag_host = (urlparse(mh).hostname or "").lower()
        except Exception:
            frag_host = ""

    eb = ""
    search_btn = ""
    if canonical_action == "search_content_and_browse_results":
        eb = query
        search_btn = str(p.get("expected_search_control_label") or "search")
    return {
        "window_title_fragment": app or eb or frag_host or "mission",
        "url_host_fragment": frag_host,
        "target_label_exact": eb or app,
        "expected_search_button_text": search_btn or "",
        "dom_fingerprint_weak": f"{canonical_action}:{query}:{url}:{app}"[:280],
        "canonical_action": canonical_action,
        "resolution_class": p.get("recorded_resolution_class"),
        "zoom_bucket": p.get("recorded_zoom_bucket"),
    }


def attempt_semantic_relocation(
    *,
    visible_label_candidates: Sequence[str],
    recorded_target_fragment: str,
    classification: Dict[str, Any],
) -> Dict[str, Any]:
    meters = get_arl_metrics()
    kinds = frozenset(classification.get("kinds") or [])
    if classification.get("risk") != "SAFE" or "text_variation" not in kinds:
        meters.record_relocation_attempt(success=False)
        return {"attempted": True, "success": False, "reason": "not_applicable"}

    best: Tuple[str, float] = ("", 0.0)
    rt = recorded_target_fragment or ""
    for c in visible_label_candidates:
        cs = str(c)
        ok, sc = lightweight_semantic_table_match(rt, cs)
        if ok and sc > best[1]:
            best = (cs, sc)

    ok = bool(best[0]) and best[1] >= 0.45
    meters.record_relocation_attempt(success=ok)
    if ok:
        meters.record_fallback_avoidance()
    return {
        "attempted": True,
        "success": ok,
        "relocated_label": best[0] or None,
        "confidence": best[1],
    }


def attempt_structural_relocation(
    *,
    recorded_dom_weak: Optional[str],
    observed_dom_weak: Optional[str],
    classification: Dict[str, Any],
) -> Dict[str, Any]:
    meters = get_arl_metrics()
    kinds = frozenset(classification.get("kinds") or [])
    if classification.get("risk") != "SAFE" or "minor_dom_change" not in kinds:
        meters.record_relocation_attempt(success=False)
        return {"attempted": False, "success": False, "reason": "not_applicable"}

    rdom = recorded_dom_weak or ""
    odom = observed_dom_weak or ""
    if not rdom or not odom:
        meters.record_relocation_attempt(success=False)
        return {"attempted": True, "success": False, "reason": "missing_fingerprint"}

    sim = lightweight_token_overlap_similarity(rdom, odom)
    ok = sim >= 0.45
    meters.record_relocation_attempt(success=ok)
    if ok:
        meters.record_fallback_avoidance()
    return {"attempted": True, "success": ok, "structural_similarity": sim}


def attempt_contextual_revalidation(
    *,
    truth_signals: Dict[str, Any],
    ambiguity: Dict[str, Any],
    deviation_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    amb_c = int(ambiguity.get("count") or 0)
    if amb_c >= 2:
        return {"attempted": True, "ok": False, "reason": "high_ambiguity"}
    confirmed = bool(truth_signals.get("execution_truth_confirmed"))
    align = float(truth_signals.get("sep_alignment_score") or truth_signals.get("alignment") or 0.0)
    if deviation_snapshot.get("deviation_detected") and confirmed and align >= 0.55:
        return {"attempted": True, "ok": True, "reason": "truth_and_alignment"}

    if not deviation_snapshot.get("deviation_detected"):
        return {"attempted": True, "ok": True, "reason": "no_deviation"}

    if align >= 0.72:
        return {"attempted": True, "ok": True, "reason": "alignment_only"}

    return {"attempted": True, "ok": False, "reason": "insufficient_signals"}


def validate_operational_continuity(
    *,
    mission_plan_slice: Dict[str, Any],
    classifier_primary_type: str,
    deviation_kinds: Sequence[str],
) -> Dict[str, Any]:
    invariant_host = str(mission_plan_slice.get("invariant_host") or "").lower()
    current_host = str(mission_plan_slice.get("browser_host_observed") or "").lower()

    dk = frozenset(deviation_kinds)
    if invariant_host and current_host and current_host != invariant_host:
        if "unknown_target" in dk:
            return {"ok": False, "reason": "host_mismatch_with_unknown"}
        # SAFE cosmetic / layout deviations still allow continuity si el host coincide;
        # si no coincide pero el tipo SAFE es layout-ish, marca riesgo bajo pero no rompe.
        if classifier_primary_type in {"layout_shift", "resolution_change"}:
            return {"ok": True, "reason": "ignored_for_layout"}

    exp_q = mission_plan_slice.get("expected_queries") or ()
    typed = mission_plan_slice.get("last_typed_fragment") or ""
    if isinstance(exp_q, (list, tuple)) and typed:
        tnorm = _norm(str(typed))
        if any(_norm(str(x)) in tnorm or tnorm in _norm(str(x)) for x in exp_q):
            return {"ok": True, "reason": "query_trail_present"}

    if mission_plan_slice.get("cob_action") == "open_application":
        return {"ok": True, "reason": "launch_step_lenient"}

    if not deviation_kinds or deviation_kinds == ["none"]:
        return {"ok": True, "reason": "no_deviation"}

    return {"ok": True, "reason": "default_safe_continuity"}


def adapt_validator_expectations(
    validator_spec: Dict[str, Any],
    deviation_types: Sequence[str],
) -> Dict[str, Any]:
    out = dict(validator_spec or {})
    kinds = frozenset(deviation_types)
    adapters: List[str] = []
    if "label_translation" in kinds or "text_variation" in kinds:
        out["text_match_mode"] = "semantic_loose_jaccard"
        out["accepted_similarity_floor"] = min(float(out.get("accepted_similarity_floor") or 0.85), 0.62)
        adapters.append("relax_text_match")
    if {"resolution_change", "zoom_change"} & kinds:
        out["bounds_slack_px"] = int(out.get("bounds_slack_px") or 0) + 12
        adapters.append("increase_bounds_slack")
    if "minor_dom_change" in kinds:
        out["structure_check"] = str(out.get("structure_check") or "weak_topology")
        adapters.append("weaken_topology")

    meters = get_arl_metrics()
    if adapters:
        meters.record_validator_adaptation()

    out["adapted_for_deviations"] = sorted(kinds)
    out["adaptations_applied"] = adapters
    return out


def decide_continue_vs_fallback(
    *,
    classification: Dict[str, Any],
    semantic_relocation: Dict[str, Any],
    structural_relocation: Dict[str, Any],
    continuity: Dict[str, Any],
    revalidation: Dict[str, Any],
    ambiguity: Dict[str, Any],
    retry_budget: int,
    human_escalation_preferred: bool = False,
) -> Dict[str, Any]:
    meters = get_arl_metrics()
    risk = classification.get("risk")
    pt = classification.get("primary_type") or "none"
    amb_c = int(ambiguity.get("count") or 0)

    if risk == "NO_SAFE" or pt in UNSAFE_DEVIATION_TYPES:
        if human_escalation_preferred or amb_c >= 2:
            meters.record_human_request_hint()
            meters.record_fallback()
            return {"decision": ArlDecision.REQUEST_HUMAN.value, "rationale": "unsafe_or_human_gate"}
        meters.record_fallback()
        return {"decision": ArlDecision.FALLBACK_TO_SEP.value, "rationale": "no_safe_deviation"}

    if amb_c >= 3:
        meters.record_human_request_hint()
        meters.record_fallback()
        return {"decision": ArlDecision.REQUEST_HUMAN.value, "rationale": "ambiguity_excess"}

    if retry_budget <= 0:
        meters.record_fallback()
        return {"decision": ArlDecision.FALLBACK_TO_SEP.value, "rationale": "retry_budget_empty"}

    sem_ok = bool(semantic_relocation.get("success"))
    struct_ok = bool(structural_relocation.get("success"))
    cont_ok = bool(continuity.get("ok"))
    rev_ok = bool(revalidation.get("ok"))

    if sem_ok or struct_ok:
        meters.record_adaptation(success=True)
        meters.record_fallback_avoidance()
        meters.record_continuity_preserved()
        return {"decision": ArlDecision.ADAPT.value, "rationale": "relocation_success"}

    if pt in {"none"} or pt is None:
        return {"decision": ArlDecision.CONTINUE.value, "rationale": "aligned"}

    if rev_ok and cont_ok:
        meters.record_continuity_preserved()
        return {"decision": ArlDecision.CONTINUE.value, "rationale": "revalidate_ok"}

    if rev_ok and not cont_ok:
        meters.record_adaptation(success=False)
        return {"decision": ArlDecision.REVALIDATE.value, "rationale": "needs_continuity_check"}

    meters.record_fallback()
    return {"decision": ArlDecision.FALLBACK_TO_SEP.value, "rationale": "default_fallback"}


def build_arl_audit(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    ev_list = [dict(e) for e in events]
    rollup = {"event_count": len(ev_list)}
    if ev_list:
        lev = ev_list[-1]
        d_pkg = lev.get("decision")
        if isinstance(d_pkg, dict) and "decision" in d_pkg:
            rollup["last_decision"] = d_pkg.get("decision")
        b = lev.get("bundle") or {}
        bd = b.get("decision") if isinstance(b, dict) else None
        if isinstance(bd, dict) and bd.get("decision") is not None:
            rollup["last_decision"] = bd.get("decision")

    audit = {"version": 1, "events": ev_list, "rollup": rollup}
    get_arl_metrics().merge_into_summary(audit)
    return audit


def arl_append_audit_event(mission: Any, event: Dict[str, Any]) -> Dict[str, Any]:
    prev = getattr(mission, "adaptive_runtime_audit", None)
    base = dict(prev) if isinstance(prev, dict) else {"version": 1, "events": []}
    evs = list(base.get("events") or [])
    evs.append(dict(event))
    base["events"] = evs
    base.setdefault("rollup", {})
    base["rollup"]["event_count"] = len(evs)
    setattr(mission, "adaptive_runtime_audit", base)
    get_arl_metrics().merge_into_summary(base)
    return base


def run_arl_safe_bundle(
    *,
    recorded_signature: Dict[str, Any],
    observed_state: Dict[str, Any],
    session_context: Optional[Dict[str, Any]],
    truth_signals: Dict[str, Any],
    ambiguity: Dict[str, Any],
    mission_plan_slice: Dict[str, Any],
    validator_spec: Dict[str, Any],
    retry_budget: int,
    human_escalation_preferred: bool = False,
) -> Dict[str, Any]:
    dev = detect_runtime_deviation(
        recorded_signature=recorded_signature,
        observed_state=observed_state,
        session_context=session_context,
    )
    cls = classify_deviation(dev)

    relocate_sem = attempt_semantic_relocation(
        visible_label_candidates=list(observed_state.get("visible_label_candidates") or ()),
        recorded_target_fragment=str(recorded_signature.get("target_label_exact") or ""),
        classification=cls,
    )
    relocate_struct = attempt_structural_relocation(
        recorded_dom_weak=str(recorded_signature.get("dom_fingerprint_weak") or ""),
        observed_dom_weak=str(observed_state.get("dom_fingerprint_weak") or ""),
        classification=cls,
    )
    continuity = validate_operational_continuity(
        mission_plan_slice=mission_plan_slice,
        classifier_primary_type=str(cls.get("primary_type") or ""),
        deviation_kinds=list(dev.get("deviation_types") or ()),
    )
    adapted_val = adapt_validator_expectations(validator_spec, list(dev.get("deviation_types") or ()))

    rev = attempt_contextual_revalidation(
        truth_signals=truth_signals,
        ambiguity=ambiguity,
        deviation_snapshot=dev,
    )

    decision_pkg = decide_continue_vs_fallback(
        classification=cls,
        semantic_relocation=relocate_sem,
        structural_relocation=relocate_struct,
        continuity=continuity,
        revalidation=rev,
        ambiguity=ambiguity,
        retry_budget=retry_budget,
        human_escalation_preferred=human_escalation_preferred,
    )

    return {
        "deviation": dev,
        "classification": cls,
        "semantic_relocation": relocate_sem,
        "structural_relocation": relocate_struct,
        "continuity": continuity,
        "revalidation": rev,
        "adapted_validator_expectations": adapted_val,
        "decision": decision_pkg,
        "session_context_echo": dict(session_context or {}),
    }


def record_cob_aggregate_failure_audit(
    mission: Any,
    *,
    canonical_action: str,
    cob_params: Dict[str, Any],
    detector: Optional[Any],
    execution_errors: Sequence[str],
    retry_budget_hint: Optional[int] = None,
) -> Dict[str, Any]:
    cfg_max = retry_budget_hint
    if cfg_max is None:
        try:
            from app.core.config import get_settings

            cfg_max = int(getattr(get_settings(), "ARL_MAX_RETRIES", 1) or 1)
        except Exception:
            cfg_max = 1

    recorded = build_recorded_operational_hints(
        canonical_action=canonical_action,
        cob_params=cob_params,
        mission_home_url_hint=getattr(mission, "description", "") or "",
    )

    snapshot = detector.detect(deep=False, want_url=True) if detector else None
    observed = state_snapshot_to_observed_state(snapshot)

    mh = ""
    bu = observed.get("browser_url") or ""
    if bu:
        try:
            from urllib.parse import urlparse

            mh = (urlparse(str(bu)).hostname or "").lower()
        except Exception:
            mh = ""

    qp = str(cob_params.get("query") or cob_params.get("query_preview") or "").strip()
    plan_slice = {
        "invariant_host": recorded.get("url_host_fragment") or mh,
        "browser_host_observed": mh,
        "expected_queries": [qp] if qp else [],
        "cob_action": canonical_action,
        "last_typed_fragment": str(cob_params.get("query") or ""),
    }
    truths = execution_truth_signals_for_cob(
        getattr(mission, "cob_dry_run_shadow", None),
        canonical_action=str(canonical_action),
    )
    amb = ambiguity_hint_from_shadow(getattr(mission, "semantic_shadow_audit", None))
    sess = {"pilot_aggregate_errors": [str(e) for e in execution_errors]}

    bundle = run_arl_safe_bundle(
        recorded_signature=recorded,
        observed_state=observed,
        session_context=sess,
        truth_signals=truths,
        ambiguity=amb,
        mission_plan_slice=plan_slice,
        validator_spec={"source": "cob_aggregate"},
        retry_budget=max(0, int(cfg_max)),
        human_escalation_preferred=any("HUMAN" in str(e).upper() for e in execution_errors),
    )

    evt = {
        "type": "cob_aggregate_failure",
        "canonical_action": canonical_action,
        "bundle": bundle,
        "observed_trim": {
            "active_window_title": (observed.get("active_window_title") or "")[:120],
            "browser_url_prefix": str(observed.get("browser_url") or "")[:140],
        },
    }
    arl_append_audit_event(mission, evt)
    return evt


def execution_truth_signals_for_cob(
    cob_dry_run_shadow: Optional[Dict[str, Any]],
    *,
    canonical_action: str,
) -> Dict[str, Any]:
    _ = canonical_action
    if not isinstance(cob_dry_run_shadow, dict):
        return {"execution_truth_confirmed": False, "sep_alignment_score": 0.0}

    summary = cob_dry_run_shadow.get("summary") or {}
    ratio = summary.get("supported_ratio")
    try:
        score = float(ratio)
    except (TypeError, ValueError):
        sc_n = summary.get("supported_count")
        tot_n = summary.get("total_rows")
        try:
            score = float(sc_n) / max(1.0, float(tot_n))
        except (TypeError, ValueError, ZeroDivisionError):
            score = 0.0

    confirmed = score >= 0.95

    return {"execution_truth_confirmed": confirmed, "sep_alignment_score": float(score)}


def ambiguity_hint_from_shadow(semantic_shadow_audit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(semantic_shadow_audit, dict):
        return {"count": 0}
    mm = semantic_shadow_audit.get("top_mismatches") or []
    cnt = len(mm) if isinstance(mm, list) else 0
    return {"count": cnt}


__all__ = [
    "SAFE_DEVIATION_TYPES",
    "UNSAFE_DEVIATION_TYPES",
    "ArlDecision",
    "lightweight_semantic_table_match",
    "lightweight_token_overlap_similarity",
    "detect_runtime_deviation",
    "classify_deviation",
    "state_snapshot_to_observed_state",
    "build_recorded_operational_hints",
    "attempt_semantic_relocation",
    "attempt_structural_relocation",
    "attempt_contextual_revalidation",
    "validate_operational_continuity",
    "adapt_validator_expectations",
    "decide_continue_vs_fallback",
    "build_arl_audit",
    "arl_append_audit_event",
    "run_arl_safe_bundle",
    "record_cob_aggregate_failure_audit",
]
