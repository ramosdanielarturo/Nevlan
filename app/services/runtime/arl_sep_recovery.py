"""ARL — hook de recuperación para SmartExecutor / SEP (sin reemplazo del runtime).

Invocado cuando se agotan las estrategias de un paso o el TargetResolver
devolvió resultado insuficiente. Persiste auditoría en ``mission.adaptive_runtime_audit``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.logger import log
from app.services.missions.execution_contracts import MissionStep, StepContract
from app.services.runtime.adaptive_runtime_layer import (
    ArlDecision,
    ambiguity_hint_from_shadow,
    arl_append_audit_event,
    run_arl_safe_bundle,
    state_snapshot_to_observed_state,
)
from app.services.runtime.arl_metrics import get_arl_metrics
from app.services.runtime.live_operational_perception import arl_hints_from_lop


# Alineado con SmartMissionExecutor ``_FORBIDDEN_PRIMARY_HINTS`` (coords ciegas / visión emergencia).
_EMERGENCY_STRATEGY_SUBSTRINGS: Tuple[str, ...] = (
    "vision_coords",
    "relative_coords",
    "visual_asset",
    "click_button",
    "group_control",
    "guide_service",
    "video_stream",
    "use_coords",
)


def _is_emergency_coord_like_strategy(strategy: str) -> bool:
    s = (strategy or "").lower()
    return any(h in s for h in _EMERGENCY_STRATEGY_SUBSTRINGS)


ARL_UNSAFE_COORDS_ONLY = "ARL_UNSAFE_COORDS_ONLY"


def filter_strategies_exclude_emergency_coords(
    ranked: Sequence[str],
) -> Tuple[List[str], Optional[str]]:
    """Quita estrategias tipo coords/visión emergencia.

    Si el plan original tenía estrategias pero **todas** eran sólo coords
    emergencia, devuelve lista vacía y ``ARL_UNSAFE_COORDS_ONLY`` (no se
    reintroduce click ciego en la ronda ARL repitiendo el ranking original).

    Lista de entrada vacía → ``([], None)``.
    """

    lst = [str(s) for s in ranked]
    safe = [s for s in lst if not _is_emergency_coord_like_strategy(s)]
    if safe:
        return safe, None
    if lst:
        return [], ARL_UNSAFE_COORDS_ONLY
    return [], None


# Kinds SEP actuales considerados aptos para relajación SAFE (whitelist).
_ARL_SEMANTIC_KIND_SAFE = frozenset(
    {
        "open_app",
        "select_profile",
        "use_selected_profile",
        "open_new_tab",
        "open_url",
        "search_content",
        "search_site",
        "search_youtube",
        "search_web",
        "scroll_results",
        "open_search_result",
        "scroll_page",
    },
)

_NOTFOUND_MARKERS_EN = ("not found", "no encontr", "missing", "sin ", "unable to find")


def sep_step_blocked_for_arl(step: MissionStep) -> Tuple[bool, str]:
    """Bloquea adaptación para acciones marcadas destructivas / test-only."""

    if step.params.get("confirm_destructive") or step.params.get("destructive_hint"):
        return True, "destructive_marker"
    if step.params.get("arl_blocked_destructive"):  # test seam
        return True, "test_blocked_destructive"
    if step.kind not in _ARL_SEMANTIC_KIND_SAFE:
        return True, "kind_not_in_safe_whitelist"
    return False, ""


def _urlparse_netloc_hint(url_frag: str) -> str:
    if not url_frag:
        return ""
    try:
        from urllib.parse import urlparse

        return str((urlparse(url_frag.strip()).hostname or "")).lower()
    except Exception:
        return ""


def recorded_signature_from_sep_step(step: MissionStep, before_state: Dict[str, Any]) -> Dict[str, Any]:
    """Construye firma esperada desde ``MissionStep`` + snapshot previo."""

    p = dict(step.params or {})
    title_hint = ""
    ud = ""
    wt = ""
    try:
        title_hint = (
            str(p.get("title") or p.get("link_text") or p.get("result_title") or "")
            .strip()
        )
    except Exception:
        title_hint = ""
    try:
        ud = (
            _urlparse_netloc_hint(str(p.get("url") or p.get("alias") or ""))
            or ""
        )
    except Exception:
        ud = ""
    try:
        wt = str((before_state or {}).get("active_window_title") or "")[:240]
    except Exception:
        wt = ""

    label_keys = ("button_text", "label", "name", "profile_name", "query", "text")
    label_comp = ""
    for k in label_keys:
        v = str(p.get(k) or "").strip()
        if v:
            label_comp = v
            break

    dom_weak = ""
    hl = str(step.human_label or "").strip()
    dom_weak = f"{step.kind}|{hl}|{label_comp}|{ud}"[:380]

    return {
        "window_title_fragment": label_comp or wt or hl or ud or step.kind,
        "url_host_fragment": ud or _urlparse_netloc_hint(str((before_state or {}).get("browser_url") or "")),
        "target_label_exact": label_comp or hl,
        "expected_search_button_text": str(p.get("search_button_hint") or ""),
        "dom_fingerprint_weak": dom_weak or step.kind,
    }


def classify_executor_failure_signals(
    *,
    last_strategy_ok: bool,
    last_error: str,
) -> Dict[str, Any]:
    m = str(last_error or "").lower()

    ambiguous = any(x in m for x in ("ambiguous", "multiple target", "candidat"))

    tgt_missing = False
    if not last_strategy_ok:
        tgt_missing = (
            bool(m.strip())
            and any(x in m for x in _NOTFOUND_MARKERS_EN)
            or bool(re.search(r"\b(no pude|failed|unable)\b", m))
        )

    validator_partial = last_strategy_ok and bool(m.strip())

    return {
        "ambiguous": ambiguous,
        "target_missing": tgt_missing,
        "validator_partial": validator_partial,
    }


def sep_truth_signals_placeholder() -> Dict[str, Any]:
    return {"execution_truth_confirmed": True, "sep_alignment_score": 0.74}


def _apply_semantic_relocation_to_step(step: MissionStep, relocated: str, mutations: List[str]) -> None:
    rl = relocated.strip()
    if not rl:
        return
    p = step.params
    if p.get("profile_name"):
        p["_arl_original_profile_name"] = p.get("profile_name")
        p["profile_name"] = rl
        mutations.append("profile_name")
        return

    pref = list(p.get("arl_uia_control_names_try_first") or [])
    if isinstance(pref, str) and pref.strip():
        pref = [pref.strip()]
    if not pref:
        pref = []
    if rl not in pref:
        pref.insert(0, rl)
        p["arl_uia_control_names_try_first"] = pref
        mutations.append("arl_uia_control_names_try_first")


def apply_arl_recovery_mutations(
    step: MissionStep,
    bundle: Dict[str, Any],
    mutations_acc: List[str],
    *,
    failure_sigs: Dict[str, Any],
) -> None:
    """Actualiza ``step.params`` con decisiones SAFE del bundle ARL."""

    sem = bundle.get("semantic_relocation") or {}
    if sem.get("success") and sem.get("relocated_label"):
        _apply_semantic_relocation_to_step(step, str(sem["relocated_label"]), mutations_acc)

    adapted = bundle.get("adapted_validator_expectations") or {}
    bonus = 0
    kinds = adapted.get("adapted_for_deviations") or ()
    kind_set = frozenset(str(k) for k in kinds)
    if "label_translation" in kind_set:
        bonus = max(bonus, 1800)
    if "text_variation" in kind_set:
        bonus = max(bonus, 1600)
    if "minor_dom_change" in kind_set:
        bonus = max(bonus, 2400)
    if adapted.get("adaptations_applied"):
        bonus = max(bonus, 1200)
    prev = int(step.params.get("_arl_post_timeout_addon_ms") or 0)
    if bonus > 0:
        step.params["_arl_post_timeout_addon_ms"] = max(prev, bonus)
        mutations_acc.append("_arl_post_timeout_addon_ms")

    if failure_sigs.get("validator_partial"):
        pv = int(step.params.get("_arl_post_timeout_addon_ms") or 0)
        if pv < 2000:
            step.params["_arl_post_timeout_addon_ms"] = 2000
            mutations_acc.append("_arl_post_timeout_addon_ms_validator_partial_forced")


def _mission_plan_slice_for_step(step: MissionStep, observed_url: str) -> Dict[str, Any]:
    p = dict(step.params or ())
    mh = ""
    ou = observed_url or ""
    if ou:
        try:
            from urllib.parse import urlparse

            mh = (urlparse(ou).hostname or "").lower()
        except Exception:
            mh = ""
    q = str(p.get("query") or "").strip()
    tgt = (
        _urlparse_netloc_hint(str(p.get("url") or ""))
        or _urlparse_netloc_hint(str(p.get("alias") or ""))
    )
    return {
        "invariant_host": tgt or mh,
        "browser_host_observed": mh,
        "expected_queries": [q] if q else [],
        "cob_action": step.kind,
        "last_typed_fragment": q,
    }


@dataclass
class SepArlRecoveryVerdict:
    """Salida del hook ARL antes de RecoveryEngine."""

    retry_step: bool = False
    escalate_human_immediate: bool = False
    human_message: str = ""
    clear_attempt_counters: bool = False


def evaluate_sep_arl_recovery(
    *,
    mission: Optional[Any],
    step: MissionStep,
    contract: StepContract,
    observed_snapshot: Any,
    before_state: Dict[str, Any],
    last_strategy_used: Optional[str],
    last_strategy_ok: bool,
    last_error: str,
    arl_round_done: int,
    max_extra_rounds: int,
    retry_budget: int,
) -> SepArlRecoveryVerdict:
    """Decide si reintentamos el mismo paso con adaptaciones SAFE."""

    if max_extra_rounds <= 0 or arl_round_done >= max_extra_rounds:
        return SepArlRecoveryVerdict()

    blocked, breason = sep_step_blocked_for_arl(step)
    if blocked:
        log.debug("ARL SEP recovery omitido (%s)", breason)
        if mission is not None:
            arl_append_audit_event(
                mission,
                {
                    "type": "sep_smart_executor_recovery",
                    "skipped": True,
                    "reason": breason,
                    "step_kind": step.kind,
                    "step_id": step.id,
                },
            )
        return SepArlRecoveryVerdict()

    sigs = classify_executor_failure_signals(
        last_strategy_ok=last_strategy_ok,
        last_error=str(last_error or ""),
    )

    amb_shadow = ambiguity_hint_from_shadow(
        getattr(mission, "semantic_shadow_audit", None) if mission else None,
    )

    sess: Dict[str, Any] = {
        "ambiguous_target_count": int(amb_shadow.get("count") or 0),
        "scroll_offset_bucket_changed": False,
        "extra_sidebar_detected": False,
    }
    ambiguous_count_eff = int(amb_shadow.get("count") or 0)

    if sigs["validator_partial"]:
        sess["validator_partial_retry"] = True

    if sigs["ambiguous"]:
        sess["ambiguous_target_count"] = 6
        ambiguous_count_eff = 6

    if sigs["ambiguous"]:
        if mission is not None:
            arl_append_audit_event(
                mission,
                {
                    "type": "sep_smart_executor_recovery",
                    "step_kind": step.kind,
                    "step_id": step.id,
                    "decision_summary": {"decision": ArlDecision.REQUEST_HUMAN.value, "reason": "multiple_targets_signals"},
                    "failure_signals": sigs,
                },
            )
        return SepArlRecoveryVerdict(
            escalate_human_immediate=True,
            human_message=(
                "Varias coincidencias posibles para el objetivo — "
                "elige cuál ejecutar antes de seguir."
            ),
        )

    recorded = recorded_signature_from_sep_step(step, before_state)
    observed = state_snapshot_to_observed_state(observed_snapshot)
    bundle = run_arl_safe_bundle(
        recorded_signature=recorded,
        observed_state=observed,
        session_context=sess,
        truth_signals=sep_truth_signals_placeholder(),
        ambiguity={"count": ambiguous_count_eff},
        mission_plan_slice=_mission_plan_slice_for_step(step, str(observed.get("browser_url") or "")),
        validator_spec={"source": "smart_executor_sep"},
        retry_budget=max(0, int(retry_budget)),
        human_escalation_preferred=False,
    )

    raw_decision = bundle.get("decision") or {}
    dec_val = raw_decision.get("decision") or ""

    mutations: List[str] = []

    evt = {
        "type": "sep_smart_executor_recovery",
        "step_kind": step.kind,
        "step_id": step.id,
        "bundle": bundle,
        "last_strategy": last_strategy_used,
        "last_strategy_ok": last_strategy_ok,
        "failure_signals": sigs,
    }

    if mission is not None:
        lh = arl_hints_from_lop(mission)

        if lh.get("lop_present"):
            evt["lop_recovery_hints_shadow"] = lh

    if dec_val in (ArlDecision.FALLBACK_TO_SEP.value,):
        if mission is not None:
            arl_append_audit_event(mission, evt)
        return SepArlRecoveryVerdict()

    if dec_val == ArlDecision.REQUEST_HUMAN.value:
        if mission is not None:
            arl_append_audit_event(mission, evt)
        return SepArlRecoveryVerdict(
            escalate_human_immediate=True,
            human_message="Señal ARL: se requiere confirmación humana antes de continuar.",
        )

    if dec_val in (
        ArlDecision.CONTINUE.value,
        ArlDecision.ADAPT.value,
        ArlDecision.REVALIDATE.value,
    ):
        apply_arl_recovery_mutations(step, bundle, mutations, failure_sigs=sigs)
        if mutations:
            get_arl_metrics().record_adaptation(success=True)
        if not mutations and dec_val == ArlDecision.CONTINUE.value:
            if mission is not None:
                arl_append_audit_event(mission, {**evt, "note": "continue_without_local_mutations"})
            return SepArlRecoveryVerdict()
        if mission is not None:
            evt["mutations"] = list(mutations)
            arl_append_audit_event(mission, evt)
        return SepArlRecoveryVerdict(
            retry_step=True,
            clear_attempt_counters=True,
        )

    if mission is not None:
        arl_append_audit_event(mission, evt)
    return SepArlRecoveryVerdict()


def maybe_adjust_postcondition_deadline_ms(step: MissionStep, base_ms: int) -> int:
    try:
        add = int(step.params.get("_arl_post_timeout_addon_ms") or 0)
    except (TypeError, ValueError):
        add = 0
    return max(0, int(base_ms) + max(0, add))


def try_target_resolver_arl_second_pass(
    *,
    mission: Optional[Any],
    compiled_step: Any,
    resolution: Any,
    detector: Any,
) -> bool:
    """Si el resolver falló, intenta un pase ARL mutando UIA/semantic rico.

    Devuelve True si se aplicó alguna mutación (el caller debe volver a ``resolve``).
    """

    try:
        from app.core.config import get_settings

        if not getattr(get_settings(), "ARL_ENABLED", False):
            return False
        if getattr(get_settings(), "ARL_SEP_RECOVERY_MAX_ROUNDS", 0) <= 0:
            return False
    except Exception:
        return False

    if mission is None:
        return False

    if getattr(compiled_step, "target_context", None) is None:
        return False

    if resolution is not None:
        if getattr(resolution, "method", "") != "none" and int(getattr(resolution, "score", 0) or 0) >= 50:
            return False

    snap = detector.detect(deep=True, want_url=True) if detector else None
    observed = state_snapshot_to_observed_state(snap)
    tc = compiled_step.target_context
    label = ""
    try:
        sem = tc.semantic or {}
        label = str(sem.get("human_label") or sem.get("near_text") or "").strip()
    except Exception:
        label = ""
    if not label:
        try:
            ud = tc.uia_data or {}
            label = str(ud.get("Name") or "").strip()
        except Exception:
            label = ""

    fake_step = MissionStep(
        id=str(getattr(compiled_step, "id", "compiled")),
        kind="open_search_result",
        params={"title": label} if label else {},
        human_label=label,
    )

    recorded = recorded_signature_from_sep_step(fake_step, {})
    bundle = run_arl_safe_bundle(
        recorded_signature=recorded,
        observed_state=observed,
        session_context={"ambiguous_target_count": 0},
        truth_signals=sep_truth_signals_placeholder(),
        ambiguity={"count": 0},
        mission_plan_slice=_mission_plan_slice_for_step(fake_step, str(observed.get("browser_url") or "")),
        validator_spec={"source": "target_resolver"},
        retry_budget=1,
    )
    dec = (bundle.get("decision") or {}).get("decision")
    semr = bundle.get("semantic_relocation") or {}

    evt = {
        "type": "target_resolver_recovery",
        "bundle": bundle,
        "compiled_step_id": getattr(compiled_step, "id", None),
    }

    if dec in (ArlDecision.ADAPT.value, ArlDecision.CONTINUE.value, ArlDecision.REVALIDATE.value) and semr.get(
        "success",
    ) and semr.get("relocated_label"):
        new_lbl = str(semr["relocated_label"])
        try:
            tc2 = tc.model_copy(deep=True)
            ud = dict(tc2.uia_data or {})
            if ud:
                ud["Name"] = new_lbl
                tc2.uia_data = ud
            sm = dict(tc2.semantic or {})
            if sm:
                sm["human_label"] = new_lbl
                tc2.semantic = sm
            compiled_step.target_context = tc2
            evt["mutations"] = ["uia_data.Name", "semantic.human_label"]
            arl_append_audit_event(mission, evt)
            return True
        except Exception as e:
            log.debug("ARL target_resolver mutation failed: %s", e)

    arl_append_audit_event(mission, {**evt, "mutations": []})
    return False


__all__ = [
    "ARL_UNSAFE_COORDS_ONLY",
    "SepArlRecoveryVerdict",
    "evaluate_sep_arl_recovery",
    "filter_strategies_exclude_emergency_coords",
    "maybe_adjust_postcondition_deadline_ms",
    "recorded_signature_from_sep_step",
    "sep_step_blocked_for_arl",
    "try_target_resolver_arl_second_pass",
]
