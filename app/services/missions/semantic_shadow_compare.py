"""Compara hipótesis semantic-first (grabación) vs SEP post-collapse/SIPE."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import Mission, SemanticHypothesis


# Buckets normalizados para alinear SEP (tipos collapse) con hipótesis shadow.
SEP_BUCKET: Dict[str, str] = {
    "open_app": "launcher",
    "select_profile": "pick",
    "use_selected_profile": "pick",
    "open_new_tab": "new_tab",
    "open_url": "nav_url",
    "search_youtube": "search",
    "search_site": "search",
    "search_web": "search",
    "open_search_result": "nav_click",
    "scroll_results": "scroll",
    "scroll_page": "scroll",
}

HYP_BUCKET: Dict[str, str] = {
    "open_app_candidate": "launcher",
    "select_visible_option": "pick",
    "click_button_by_label": "click",
    "focus_input_field": "focus",
    "open_url_candidate": "nav_url",
    "open_new_tab": "new_tab",
    "focus_browser_address_bar": "addr",
    "type_field_value": "type",
    "search_content_candidate": "search",
    "confirm_semantic_value": "confirm",
    "submit_field": "confirm",
    "confirm_input": "confirm",
    "cancel_input": "cancel",
    "focus_next_field": "focus_nav",
    "scroll_content_candidate": "scroll",
}


def _sep_buckets(mission: Mission) -> List[Tuple[str, str, float]]:
    """Lista (step_id_or_idx, bucket, confidence)."""
    sep = mission.semantic_execution_plan or {}
    steps = sep.get("steps") or []
    out: List[Tuple[str, str, float]] = []
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            continue
        typ = str(st.get("type") or "").strip().lower()
        b = SEP_BUCKET.get(typ, typ or "unknown")
        conf = float(st.get("confidence") or 0.75)
        sid = str(st.get("id") or f"sep_{i}")
        out.append((sid, b, conf))
    return out


def _hyp_buckets(hyps: Sequence[SemanticHypothesis]) -> List[Tuple[str, str, float]]:
    out: List[Tuple[str, str, float]] = []
    for h in hyps:
        b = HYP_BUCKET.get(h.candidate_intent_type, h.candidate_intent_type)
        out.append((h.id, b, float(h.confidence)))
    return out


_COMP_MATRIX = {
    ("launcher", "launcher"),
    ("launcher", "focus"),
    ("pick", "pick"),
    ("pick", "click"),
    ("new_tab", "new_tab"),
    ("nav_url", "nav_url"),
    ("nav_url", "click"),
    ("search", "search"),
    ("search", "type"),
    ("search", "confirm"),
    ("scroll", "scroll"),
    ("confirm", "confirm"),
    ("confirm", "search"),
    ("focus", "focus"),
    ("focus", "type"),
    ("click", "click"),
    ("click", "pick"),
    ("nav_click", "click"),
    ("nav_click", "nav_url"),
}


def _compatible(sep_b: str, hyp_b: str) -> bool:
    if sep_b == hyp_b:
        return True
    return (sep_b, hyp_b) in _COMP_MATRIX


def compare_shadow_to_sep(mission: Mission) -> Dict[str, Any]:
    """Métricas de alineación shadow vs SEP (no bloqueante)."""
    s_buckets = _sep_buckets(mission)
    h_buckets = _hyp_buckets(mission.intent_timeline or [])

    pairs: List[Dict[str, Any]] = []
    mismatched_params = 0
    matched_hyp_indices: set = set()

    for sid, sb, sc in s_buckets:
        for hj, (hid, hb, hc) in enumerate(h_buckets):
            if hj in matched_hyp_indices:
                continue
            if _compatible(sb, hb):
                matched_hyp_indices.add(hj)
                pairs.append({
                    "sep": sid,
                    "hyp": hid,
                    "sep_bucket": sb,
                    "hyp_bucket": hb,
                })
                break

    matched = len(pairs)
    missed_by_shadow = max(0, len(s_buckets) - matched)
    invented_by_shadow = max(0, len(h_buckets) - len(matched_hyp_indices))

    sep_conf_mean = (
        sum(x[2] for x in s_buckets) / len(s_buckets) if s_buckets else 0.0
    )
    hyp_conf_mean = (
        sum(x[2] for x in h_buckets) / len(h_buckets) if h_buckets else 0.0
    )
    confidence_delta = round(sep_conf_mean - hyp_conf_mean, 4)

    denom = len(s_buckets) if s_buckets else 1
    collapse_agreement_rate = round(matched / denom, 4)

    top_mismatches: List[str] = []
    # SEP sin pareja (simple): últimos buckets SEP no consumidos
    consumed_sep = {p["sep"] for p in pairs}
    for sid, sb, _ in s_buckets:
        if sid not in consumed_sep and len(top_mismatches) < 8:
            top_mismatches.append(f"sep_unmatched:{sb}:{sid}")

    return {
        "matched_intents": matched,
        "missed_by_shadow": missed_by_shadow,
        "invented_by_shadow": invented_by_shadow,
        "mismatched_params": mismatched_params,
        "confidence_delta": confidence_delta,
        "collapse_agreement_rate": collapse_agreement_rate,
        "pairs": pairs,
        "top_mismatches": top_mismatches[:10],
    }


@dataclass
class ShadowCompareResult:
    matched_intents: int
    missed_by_shadow: int
    invented_by_shadow: int
    mismatched_params: int
    confidence_delta: float
    collapse_agreement_rate: float

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ShadowCompareResult":
        return ShadowCompareResult(
            matched_intents=int(d.get("matched_intents") or 0),
            missed_by_shadow=int(d.get("missed_by_shadow") or 0),
            invented_by_shadow=int(d.get("invented_by_shadow") or 0),
            mismatched_params=int(d.get("mismatched_params") or 0),
            confidence_delta=float(d.get("confidence_delta") or 0.0),
            collapse_agreement_rate=float(d.get("collapse_agreement_rate") or 0.0),
        )
