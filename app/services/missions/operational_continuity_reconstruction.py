"""Operational Continuity Reconstruction (OCRX).

Preserva y reconstruye planes ejecutables a través de transiciones fuertes de
superficie (launcher → aplicación, picker → shell, navegación → contenido).

Ley: si ``canonical_truth`` + ``truth_preserved`` + entidad ≥ 0.90 y la
transición es coherente con el intent, el runtime NO puede degradar a
``GATE_PLAN_NOT_READY`` / blockers de perfil vacío solo por cambio de hwnd/proceso.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    CanonicalOperationalIntent,
    Mission,
    OperationalContinuityChain,
    OperationalThread,
    OperationalTransitionBridge,
    RawEvent,
    SurfaceTransitionClassification,
    TruthContinuityState,
)
from app.core.logger import log

# ── Bridge types (universales, sin producto) ───────────────────────────────────

BRIDGE_SEARCH_LAUNCHER_TO_APPLICATION = "search_launcher_to_application"
BRIDGE_PROFILE_PICKER_TO_APPLICATION = "profile_picker_to_application"
BRIDGE_BROWSER_SHELL_TO_TAB = "browser_shell_to_tab"
BRIDGE_NAVIGATION_TO_CONTENT_SURFACE = "navigation_to_content_surface"
BRIDGE_SEARCH_SUBMISSION_TO_RESULTS = "search_submission_to_results_surface"

_ENTITY_CONFIDENCE_MIN = 0.90
_TRANSITION_WINDOW_MS = 12_000

_LAUNCHER_PROC_MARKERS = (
    "searchui",
    "searchhost",
    "shellexperiencehost",
    "startmenuexperiencehost",
    "launcher",
)
_BROWSER_PROC_MARKERS = (
    "chrome",
    "msedge",
    "edge",
    "firefox",
    "brave",
    "opera",
    "vivaldi",
)

# Blockers suprimidos cuando continuidad + verdad canónica están cerradas.
_OCRX_GATE_PROTECTED_BLOCKERS: frozenset = frozenset({
    "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE",
    "PROFILE_RESOLUTION_REQUIRED",
    "AMBIGUOUS_PROFILE_PICKER",
    "COLLECTION_ENTITY_NEEDS_CLARIFICATION",
    "COLLECTION_ENTITY_WEAK",
    "NEEDS_USER_LABEL_PENDING",
    "NEEDS_CLARIFICATION",
})

_PROMOTION_BLOCKER_NOISE = frozenset({
    "AMBIGUOUS_PROFILE_PICKER",
})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _proc_name(ev: Optional[RawEvent]) -> str:
    if ev is None:
        return ""
    wc = getattr(ev, "window_context", None)
    return str(getattr(wc, "process_name", "") or "").strip().lower()


def _proc_category(proc: str) -> str:
    p = (proc or "").lower()
    if not p:
        return "unknown"
    if any(m in p for m in _LAUNCHER_PROC_MARKERS):
        return "launcher"
    if any(m in p for m in _BROWSER_PROC_MARKERS):
        return "browser"
    return "application"


def _event_ts_ms(ev: RawEvent) -> float:
    ts = getattr(ev, "timestamp", None)
    try:
        return float(ts.timestamp() * 1000) if ts else float(getattr(ev, "offset_ms", 0) or 0)
    except Exception:
        return float(getattr(ev, "offset_ms", 0) or 0)


def _coi_entity_confidence(coi: CanonicalOperationalIntent) -> float:
    ent = coi.entity
    if ent is None:
        return float(coi.truth_strength or coi.freeze_confidence or 0.0)
    return max(
        float(ent.confidence or 0.0),
        float(coi.truth_strength or 0.0),
        float(coi.freeze_confidence or 0.0),
    )


def _infer_bridge_type(
    from_cat: str,
    to_cat: str,
    *,
    coi: CanonicalOperationalIntent,
) -> str:
    if from_cat == "launcher" and to_cat in ("browser", "application"):
        return BRIDGE_SEARCH_LAUNCHER_TO_APPLICATION
    if from_cat in ("launcher", "application") and to_cat == "browser":
        coll = coi.collection
        ctype = str(getattr(coll, "collection_type", "") or "").lower() if coll else ""
        if ctype in ("list", "grid", "cards", "menu") or coi.transition_expected:
            return BRIDGE_PROFILE_PICKER_TO_APPLICATION
        return BRIDGE_SEARCH_LAUNCHER_TO_APPLICATION
    if from_cat == "browser" and to_cat == "browser":
        return BRIDGE_BROWSER_SHELL_TO_TAB
    if to_cat == "browser":
        return BRIDGE_NAVIGATION_TO_CONTENT_SURFACE
    return BRIDGE_PROFILE_PICKER_TO_APPLICATION


def _classify_transition(
    from_proc: str,
    to_proc: str,
) -> SurfaceTransitionClassification:
    if not from_proc or not to_proc:
        return SurfaceTransitionClassification.NONE
    if from_proc != to_proc:
        fc = _proc_category(from_proc)
        tc = _proc_category(to_proc)
        if fc == "launcher" and tc in ("browser", "application"):
            return SurfaceTransitionClassification.APPLICATION_HANDOFF
        return SurfaceTransitionClassification.PROCESS_CHANGE
    return SurfaceTransitionClassification.WINDOW_SHELL_CHANGE


def _find_post_transition_event(
    trace: Sequence[RawEvent],
    anchor_id: str,
    *,
    after_ms: float,
) -> Optional[RawEvent]:
    anchor_ms: Optional[float] = None
    for ev in trace:
        if str(ev.id) == anchor_id:
            anchor_ms = _event_ts_ms(ev)
            break
    if anchor_ms is None:
        return None
    best: Optional[RawEvent] = None
    best_delta = float(_TRANSITION_WINDOW_MS)
    for ev in trace:
        delta = _event_ts_ms(ev) - anchor_ms
        if delta <= 0 or delta > _TRANSITION_WINDOW_MS:
            continue
        proc = _proc_name(ev)
        if not proc:
            continue
        if delta < best_delta:
            best_delta = delta
            best = ev
    return best


def _transition_observed_for_coi(
    coi: CanonicalOperationalIntent,
    trace: Sequence[RawEvent],
) -> bool:
    if coi.transition_observed:
        return True
    eid = str(coi.source_event_id or "")
    if not eid:
        return False
    post = _find_post_transition_event(trace, eid, after_ms=0)
    if post is None:
        return False
    anchor_proc = ""
    for ev in trace:
        if str(ev.id) == eid:
            anchor_proc = _proc_name(ev)
            break
    post_proc = _proc_name(post)
    if anchor_proc and post_proc and anchor_proc != post_proc:
        return True
    return _proc_category(post_proc) == "browser"


def build_operational_threads(
    mission: Mission,
    *,
    cois: Optional[Sequence[CanonicalOperationalIntent]] = None,
) -> List[OperationalThread]:
    from app.services.missions.operational_truth_preservation import (
        collect_canonical_cois,
        truth_origin_id_for_coi,
    )
    from app.services.missions.freeze_first_execution_core import human_ready_label_for_coi

    trace = list(mission.raw_trace or [])
    chain = mission.operational_truth_chain
    truth_preserved_ids = set(chain.preserved or []) if chain else set()

    threads: List[OperationalThread] = []
    for coi in cois or collect_canonical_cois(mission):
        if not coi.canonical_truth:
            continue
        conf = _coi_entity_confidence(coi)
        if conf < _ENTITY_CONFIDENCE_MIN:
            continue
        origin = truth_origin_id_for_coi(coi)
        truth_ok = (not truth_preserved_ids) or (origin in truth_preserved_ids)
        if truth_preserved_ids and not truth_ok:
            truth_ok = True  # COI canónico fuerte cuenta como preservado

        trans_obs = _transition_observed_for_coi(coi, trace)
        ent = coi.entity
        display = str(ent.display_name if ent else "").strip()
        try:
            from app.services.missions.operational_truth_lineage import human_label_from_coi

            hl = human_label_from_coi(coi) or human_ready_label_for_coi(coi)
        except Exception:
            hl = human_ready_label_for_coi(coi)

        continuity = bool(
            coi.pre_transition_confirmed
            and truth_ok
            and (trans_obs or coi.transition_expected)
        )
        t_state = (
            TruthContinuityState.PRESERVED
            if continuity
            else TruthContinuityState.UNKNOWN
        )

        threads.append(
            OperationalThread(
                thread_id=f"thread:{coi.intent_id or origin}",
                truth_origin_step_id=origin,
                coi_intent_id=str(coi.intent_id or ""),
                source_event_id=str(coi.source_event_id or ""),
                human_label=hl,
                canonical_truth=True,
                truth_preserved=truth_ok,
                continuity_preserved=continuity,
                continuity_confidence=min(1.0, conf) if continuity else 0.0,
                entity_display_name=display,
                entity_confidence=conf,
                transition_expected=bool(coi.transition_expected),
                transition_observed=trans_obs,
                truth_continuity_state=t_state,
                operational_element_type=(
                    coi.operational_element_identity.operational_element_type.value
                    if coi.operational_element_identity
                    else ""
                ),
                element_identity_id=(
                    coi.operational_element_identity.identity_id
                    if coi.operational_element_identity
                    else ""
                ),
            ),
        )
    return threads


def _is_collection_entity_intent(coi: CanonicalOperationalIntent) -> bool:
    coll = coi.collection
    if coll is not None:
        return True
    ent = coi.entity
    if ent is None:
        return False
    hint = str(getattr(ent, "entity_type_hint", "") or "").lower()
    return hint in ("user_profile", "browser_profile", "profile") or bool(ent.display_name)


def build_transition_bridges(
    mission: Mission,
    threads: Sequence[OperationalThread],
) -> List[OperationalTransitionBridge]:
    from app.services.missions.operational_truth_preservation import collect_canonical_cois

    trace = list(mission.raw_trace or [])
    coi_by_eid = {
        str(c.source_event_id): c
        for c in collect_canonical_cois(mission)
        if c.source_event_id
    }
    bridges: List[OperationalTransitionBridge] = []

    for th in threads:
        if not th.continuity_preserved:
            continue
        eid = th.source_event_id
        if not eid:
            continue
        coi = coi_by_eid.get(eid)
        if coi is None:
            for c in collect_canonical_cois(mission):
                if str(c.intent_id) == th.coi_intent_id:
                    coi = c
                    break
        if coi is None:
            continue

        anchor_ev: Optional[RawEvent] = None
        for ev in trace:
            if str(ev.id) == eid:
                anchor_ev = ev
                break
        post = _find_post_transition_event(trace, eid, after_ms=0)
        if anchor_ev is None or post is None:
            continue

        from_proc = _proc_name(anchor_ev)
        to_proc = _proc_name(post)
        from_cat = _proc_category(from_proc)
        to_cat = _proc_category(to_proc)
        bridge_type = _infer_bridge_type(from_cat, to_cat, coi=coi)
        stype = _classify_transition(from_proc, to_proc)

        bridges.append(
            OperationalTransitionBridge(
                bridge_type=bridge_type,
                from_surface=from_cat,
                to_surface=to_cat,
                from_process=from_proc,
                to_process=to_proc,
                confidence=th.continuity_confidence,
                source_event_id=eid,
                coi_intent_id=th.coi_intent_id,
                thread_id=th.thread_id,
                surface_transition_type=stype,
            ),
        )
        try:
            th.continuity_bridge_used = bridge_type
            th.surface_transition_type = stype
        except Exception:
            pass

    return bridges


def audit_operational_continuity(mission: Mission) -> OperationalContinuityChain:
    """Construye cadena OCRX y la persiste en la misión."""
    threads = build_operational_threads(mission)
    bridges = build_transition_bridges(mission, threads)

    otp = mission.operational_truth_chain
    truth_preserved = bool(
        otp and (otp.preserved or not otp.lost)
    ) or any(t.truth_preserved for t in threads)

    continuity_preserved = bool(
        threads
        and all(t.continuity_preserved for t in threads if t.canonical_truth)
    )
    if not threads:
        continuity_preserved = False

    confs = [float(t.continuity_confidence) for t in threads if t.continuity_preserved]
    avg_conf = sum(confs) / len(confs) if confs else 0.0

    chain = OperationalContinuityChain(
        threads=threads,
        bridges=bridges,
        continuity_preserved=continuity_preserved,
        continuity_confidence=round(avg_conf, 4),
        truth_preserved=truth_preserved,
        last_audit_at=utc_now_iso(),
    )
    try:
        mission.operational_continuity_chain = chain
    except Exception as exc:
        log.debug("[ocrx] persist chain: %s", exc)
    return chain


def mission_continuity_preserved(mission: Any) -> bool:
    ch = getattr(mission, "operational_continuity_chain", None)
    if ch is not None and getattr(ch, "continuity_preserved", False):
        return True
    if isinstance(ch, dict) and ch.get("continuity_preserved"):
        return True
    return False


def mission_truth_and_continuity_gate_open(mission: Any) -> bool:
    """True si OTP+OCRX permiten saltar bloqueos de perfil/gate espurios."""
    if not mission_truth_preserved(mission):
        return False
    return mission_continuity_preserved(mission)


def mission_truth_preserved(mission: Any) -> bool:
    ch = getattr(mission, "operational_truth_chain", None)
    if ch is not None:
        if getattr(ch, "lost", None):
            return len(ch.lost or []) == 0 and bool(ch.preserved or ch.nodes)
        if getattr(ch, "preserved", None):
            return len(ch.preserved or []) > 0
    if isinstance(ch, dict):
        return bool(ch.get("preserved")) and not ch.get("lost")
    from app.services.missions.freeze_first_execution_core import mission_has_canonical_truth

    return mission_has_canonical_truth(mission)


def filter_blockers_for_continuity(
    blockers: List[str],
    mission: Any,
) -> List[str]:
    """Quita blockers espurios cuando continuidad+verdad están preservadas."""
    if not mission_truth_and_continuity_gate_open(mission):
        return blockers
    out: List[str] = []
    for b in blockers:
        code = str(b or "").strip()
        if code in _OCRX_GATE_PROTECTED_BLOCKERS:
            continue
        if any(p in code.upper() for p in _OCRX_GATE_PROTECTED_BLOCKERS):
            continue
        out.append(b)
    return out


def annotate_sep_with_threads(mission: Mission) -> int:
    """Inyecta ``operational_thread_id`` y telemetría OCRX en pasos SEP."""
    chain = mission.operational_continuity_chain
    if chain is None or not chain.threads:
        return 0
    sep = mission.semantic_execution_plan
    if not isinstance(sep, dict):
        return 0
    steps = sep.get("steps") or []
    if not isinstance(steps, list):
        return 0

    from app.services.missions.operational_truth_preservation import _coi_covers_step

    updated = 0
    for th in chain.threads:
        for sp in steps:
            if not isinstance(sp, dict):
                continue
            pars = sp.get("params") or {}
            matched = False
            if th.source_event_id and str(pars.get("source_event_id") or "") == th.source_event_id:
                matched = True
            elif th.entity_display_name:
                stub_coi = None
                for c in (mission.canonical_operational_intents or []):
                    if str(c.intent_id) == th.coi_intent_id:
                        stub_coi = c
                        break
                if stub_coi and _coi_covers_step(stub_coi, sp):
                    matched = True
            if not matched:
                continue
            pars = dict(pars)
            pars["operational_thread_id"] = th.thread_id
            pars["continuity_preserved"] = th.continuity_preserved
            pars["continuity_confidence"] = th.continuity_confidence
            pars["surface_transition_type"] = (
                th.surface_transition_type.value
                if hasattr(th.surface_transition_type, "value")
                else str(th.surface_transition_type)
            )
            if th.continuity_bridge_used:
                pars["continuity_bridge_used"] = th.continuity_bridge_used
            sp["params"] = pars
            if th.human_label and (
                not sp.get("human_label")
                or _contains_technical_noise(str(sp.get("human_label") or ""))
            ):
                sp["human_label"] = th.human_label
            sp["needs_user_label"] = False
            updated += 1

    sep["steps"] = steps
    mission.semantic_execution_plan = sep
    return updated


def _contains_technical_noise(label: str) -> bool:
    low = (label or "").lower()
    markers = (
        "smart_route",
        "uia_text_match",
        "elemento identificado",
        "click_fallback",
        "wheel",
    )
    return any(m in low for m in markers)


def reconstruct_sep_with_continuity(mission: Mission) -> int:
    """OTP backfill + anotación OCRX en el SEP."""
    from app.services.missions.operational_truth_preservation import (
        apply_otp_after_promotion,
        reconcile_canonical_truths_on_sep_steps,
        _normalize_sep_blob,
        _sep_step_to_dict,
    )

    sep = _normalize_sep_blob(mission.semantic_execution_plan or {})
    steps = sep.get("steps") or []
    if not steps:
        return 0

    reconciled = reconcile_canonical_truths_on_sep_steps(mission, steps)
    plan_dict = dict(sep)
    plan_dict["steps"] = [_sep_step_to_dict(s) for s in reconciled]
    mission.semantic_execution_plan = plan_dict
    n = annotate_sep_with_threads(mission)
    return n


def apply_ocrx_ready_blocker_relief(
    plan: Any,
    mission: Any,
    blockers: List[str],
) -> None:
    """Mutación in-place de blockers tras compute_ready_status."""
    if mission is None:
        return
    if not mission_truth_and_continuity_gate_open(mission):
        return

    filtered = filter_blockers_for_continuity(list(blockers), mission)
    blockers[:] = filtered

    seq = getattr(plan, "steps", None) or []
    for row in seq:
        try:
            if getattr(row, "needs_user_label", False):
                pars = getattr(row, "params", None) or {}
                if pars.get("continuity_preserved") or pars.get("pcof_primary"):
                    row.needs_user_label = False
        except Exception:
            pass


def apply_ocrx_before_ready_status(mission: Mission) -> OperationalContinuityChain:
    """Hook antes de ``attach_intent_status_to_plan`` / truth gate."""
    from app.services.missions.operational_truth_lineage import (
        ensure_preserved_truth_pipeline,
    )

    ensure_preserved_truth_pipeline(mission)
    ch = mission.operational_continuity_chain
    if ch is None:
        ch = audit_operational_continuity(mission)
    return ch


def apply_ocrx_after_otp(mission: Mission) -> OperationalContinuityChain:
    """Invocado tras OTP en el pipeline SIPE."""
    return apply_ocrx_before_ready_status(mission)


def continuity_expert_lines(mission: Mission) -> List[str]:
    """Sección experto Mission Review — Operational Continuity."""
    ch = mission.operational_continuity_chain
    if ch is None:
        ch = audit_operational_continuity(mission)
    lines = [
        "Operational Continuity (OCRX):",
        f"  continuity_preserved: {ch.continuity_preserved}",
        f"  continuity_confidence: {ch.continuity_confidence}",
        f"  truth_preserved: {ch.truth_preserved}",
        f"  threads: {len(ch.threads)}",
        f"  bridges: {len(ch.bridges)}",
    ]
    for th in ch.threads[:4]:
        lines.append(
            f"  · {th.thread_id}: {th.human_label!r} "
            f"bridge={th.continuity_bridge_used or '—'} "
            f"state={th.truth_continuity_state.value}",
        )
    return lines


def enrich_uol_telemetry_record(
    record: Any,
    step: Any,
    mission: Any,
) -> None:
    """Añade campos OCRX a ``UolStepTelemetryRecord``."""
    pars = getattr(step, "params", None) or {}
    if not isinstance(pars, dict):
        return
    for field, key in (
        ("continuity_preserved", "continuity_preserved"),
        ("continuity_bridge_used", "continuity_bridge_used"),
        ("operational_thread_id", "operational_thread_id"),
        ("surface_transition_type", "surface_transition_type"),
        ("continuity_confidence", "continuity_confidence"),
    ):
        if key in pars:
            try:
                setattr(record, field, pars[key])
            except Exception:
                pass
    if mission is not None and mission_continuity_preserved(mission):
        try:
            record.continuity_preserved = bool(
                getattr(record, "continuity_preserved", False) or pars.get("continuity_preserved"),
            )
        except Exception:
            pass


__all__ = [
    "BRIDGE_BROWSER_SHELL_TO_TAB",
    "BRIDGE_NAVIGATION_TO_CONTENT_SURFACE",
    "BRIDGE_PROFILE_PICKER_TO_APPLICATION",
    "BRIDGE_SEARCH_LAUNCHER_TO_APPLICATION",
    "BRIDGE_SEARCH_SUBMISSION_TO_RESULTS",
    "OperationalContinuityChain",
    "OperationalThread",
    "OperationalTransitionBridge",
    "SurfaceTransitionClassification",
    "TruthContinuityState",
    "annotate_sep_with_threads",
    "apply_ocrx_after_otp",
    "apply_ocrx_before_ready_status",
    "apply_ocrx_ready_blocker_relief",
    "audit_operational_continuity",
    "build_operational_threads",
    "build_transition_bridges",
    "continuity_expert_lines",
    "enrich_uol_telemetry_record",
    "filter_blockers_for_continuity",
    "mission_continuity_preserved",
    "mission_truth_and_continuity_gate_open",
    "mission_truth_preserved",
    "reconstruct_sep_with_continuity",
]
