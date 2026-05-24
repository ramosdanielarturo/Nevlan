"""Semantic Session Engine — modo observación / sombra (Fase 1).

Modela intención operacional continua sobre raw_trace + intent_timeline.
No sustituye SEP/SIPE ni el runtime de ejecución.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from app.contracts.mission import (
    EventType,
    Mission,
    OperationalIntent,
    OperationalIntentStatus,
    RawEvent,
    SemanticHypothesis,
    SemanticSession,
    SemanticSessionStatus,
)
from app.core.logger import log
from app.services.missions.field_commit import looks_like_external_url
from app.services.missions.web_recorder import is_web_process

# ── Tipos de sesión (universales; sin apps hardcoded) ─────────────────────
SESSION_LAUNCHER = "launcher_session"
SESSION_NAVIGATION = "navigation_session"
SESSION_SEARCH = "search_session"
SESSION_SELECTION = "selection_session"
SESSION_FORM_FILL = "form_fill_session"
SESSION_DIALOG = "dialog_confirmation_session"
SESSION_FILE_NAV = "file_navigation_session"

SESSION_TYPES = frozenset({
    SESSION_LAUNCHER,
    SESSION_NAVIGATION,
    SESSION_SEARCH,
    SESSION_SELECTION,
    SESSION_FORM_FILL,
    SESSION_DIALOG,
    SESSION_FILE_NAV,
})

# Ventanas de absorción / interrupción (segundos)
_ABSORB_GAP_S = 8.0
_HARD_GAP_S = 18.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _wc(ev: RawEvent) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    wc = ev.window_context
    if wc is None:
        return None, None, None
    return (
        getattr(wc, "process_name", None),
        getattr(wc, "title", None),
        getattr(wc, "hwnd", None),
    )


def _hyp_types(hyps: Sequence[SemanticHypothesis]) -> Set[str]:
    return {h.candidate_intent_type for h in hyps}


def _is_shell_proc(proc: Optional[str]) -> bool:
    if not proc:
        return False
    p = proc.lower().strip()
    return p in {"searchhost.exe", "searchapp.exe"}


def _is_explorer_proc(proc: Optional[str]) -> bool:
    if not proc:
        return False
    return proc.lower().strip() in {"explorer.exe", "explorer"}


def _meta_uia_web(ev: RawEvent) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    m = dict(ev.metadata or {})
    uia = dict(m.get("uia") or m.get("uia_data") or {})
    web = dict(m.get("web") or m.get("web_data") or {})
    return uia, web


def _shell_launcher_hint(ev: RawEvent, hyps: Sequence[SemanticHypothesis]) -> bool:
    proc, _, _ = _wc(ev)
    if _is_shell_proc(proc):
        return True
    uia, _ = _meta_uia_web(ev)
    if (uia.get("automation_id") or "").strip().lower() == "searchtextbox":
        return True
    return "open_app_candidate" in _hyp_types(hyps) and _is_shell_proc(proc)


def _truth_blob(ev: RawEvent, hyps: Sequence[SemanticHypothesis]) -> Dict[str, Any]:
    m = dict(ev.metadata or {})
    iso = dict(m.get("target_identity_isolation") or {})
    out = {
        "trusted_pre_action_identity": bool(iso.get("trusted_pre_action_identity")),
        "event_id": ev.id,
    }
    if hyps:
        out["hypothesis_confidence_max"] = max(h.confidence for h in hyps)
    return out


def _ambi_blob(hyps: Sequence[SemanticHypothesis]) -> Dict[str, Any]:
    if not hyps:
        return {}
    return {"hypothesis_types": list(_hyp_types(hyps))}


def active_session(mission: Mission) -> Optional[SemanticSession]:
    for s in reversed(mission.semantic_sessions):
        if s.status == SemanticSessionStatus.ACTIVE:
            return s
    return None


def _session_transition(sess: SemanticSession, *, to_status: SemanticSessionStatus, reason: str) -> None:
    sess.transitions.append({
        "at": _utcnow().isoformat(),
        "from_status": sess.status.value,
        "to_status": to_status.value,
        "reason": reason,
    })


def open_session(
    mission: Mission,
    session_type: str,
    seed_ev: RawEvent,
    hyps: Sequence[SemanticHypothesis],
    *,
    inferred_goal: str = "",
    dominant_intent: str = "",
    confidence: float = 0.62,
    notes: str = "",
) -> SemanticSession:
    """Abre una nueva sesión ACTIVA (cierra sesiones incompatibles antes)."""
    if session_type not in SESSION_TYPES:
        session_type = SESSION_SELECTION

    proc, title, hwnd = _wc(seed_ev)
    ctx = {
        "primary_proc": proc,
        "primary_hwnd": hwnd,
        "seed_title_hint": (title or "")[:120],
    }

    sess = SemanticSession(
        session_type=session_type,
        started_at=seed_ev.timestamp,
        updated_at=seed_ev.timestamp,
        status=SemanticSessionStatus.ACTIVE,
        source_event_ids=[seed_ev.id],
        hypothesis_ids=[h.id for h in hyps],
        dominant_intent=dominant_intent or session_type,
        confidence=confidence,
        context=ctx,
        inferred_goal=inferred_goal,
        execution_truth_snapshot=_truth_blob(seed_ev, hyps),
        semantic_ambiguity_snapshot=_ambi_blob(hyps),
        absorbed_events_count=1,
        absorbed_hypotheses_count=len(hyps),
        notes=notes,
    )
    sess.transitions.append({
        "at": sess.started_at.isoformat(),
        "from_status": "",
        "to_status": SemanticSessionStatus.ACTIVE.value,
        "reason": "opened",
    })
    mission.semantic_sessions.append(sess)
    return sess


def absorb_event(
    sess: SemanticSession,
    ev: RawEvent,
    hyps: Sequence[SemanticHypothesis],
) -> None:
    """Absorbe evento + hipótesis en sesión activa."""
    if sess.source_event_ids and sess.source_event_ids[-1] != ev.id:
        sess.source_event_ids.append(ev.id)
    for h in hyps:
        if h.id not in sess.hypothesis_ids:
            sess.hypothesis_ids.append(h.id)
    sess.absorbed_events_count += 1
    sess.absorbed_hypotheses_count += len(hyps)
    sess.updated_at = ev.timestamp
    # Actualizar contexto operativo suave
    proc, title, hwnd = _wc(ev)
    if proc:
        sess.context["last_proc"] = proc
    if title:
        sess.context["last_title_hint"] = (title or "")[:120]
    if hwnd:
        sess.context["last_hwnd"] = hwnd


def absorb_hypothesis(sess: SemanticSession, hyp: SemanticHypothesis) -> None:
    if hyp.id not in sess.hypothesis_ids:
        sess.hypothesis_ids.append(h.id)
    sess.absorbed_hypotheses_count += 1
    sess.updated_at = hyp.timestamp


def close_session(
    mission: Mission,
    session_id: str,
    *,
    status: SemanticSessionStatus,
    reason: str,
    completion_signals: Optional[List[str]] = None,
) -> Optional[SemanticSession]:
    """Cierra sesión y deriva operational intents."""
    target: Optional[SemanticSession] = None
    for s in mission.semantic_sessions:
        if s.id == session_id:
            target = s
            break
    if target is None or target.status != SemanticSessionStatus.ACTIVE:
        return None

    if completion_signals:
        target.completion_signals.extend(completion_signals)

    _session_transition(target, to_status=status, reason=reason)
    target.status = status
    target.closed_at = _utcnow()
    target.interruption_reason = reason if status == SemanticSessionStatus.ABANDONED else None

    try:
        intents = derive_operational_intent(target)
        mission.operational_intents.extend(intents)
    except Exception as e:
        log.debug(f"derive_operational_intent: {e}")

    try:
        if getattr(mission, "cob_shadow_mode", False):
            from app.services.missions.canonical_operational_blocks import (
                append_promoted_cob_for_session,
            )

            append_promoted_cob_for_session(mission, target)
    except Exception as e:
        log.debug(f"canonical_operational_blocks.append: {e}")

    return target


def derive_operational_intent(sess: SemanticSession) -> List[OperationalIntent]:
    """Produce intents operacionales (no primitivas de teclado)."""
    out: List[OperationalIntent] = []
    types_seen = set(sess.completion_signals)

    if sess.session_type == SESSION_NAVIGATION:
        url = sess.context.get("navigation_url_hint") or sess.context.get("typed_url")
        out.append(
            OperationalIntent(
                session_id=sess.id,
                intent_type="navigate_to_site",
                params={"url_preview": str(url)[:200] if url else None},
                confidence=min(0.92, sess.confidence + 0.1),
                evidence=["semantic_session", "navigation_session"],
                outcome="completed" if sess.status == SemanticSessionStatus.COMPLETED else None,
                status=OperationalIntentStatus.COMPLETED
                if sess.status == SemanticSessionStatus.COMPLETED
                else OperationalIntentStatus.PROPOSED,
            )
        )

    elif sess.session_type == SESSION_SEARCH:
        q = sess.context.get("search_query") or sess.context.get("query_preview")
        out.append(
            OperationalIntent(
                session_id=sess.id,
                intent_type="submit_search_query",
                params={"query_preview": str(q)[:200] if q else None},
                confidence=min(0.9, sess.confidence + 0.12),
                evidence=["semantic_session", "search_session"],
                outcome="submitted" if "search_submit" in types_seen else None,
                status=OperationalIntentStatus.COMPLETED
                if "search_submit" in types_seen
                else OperationalIntentStatus.PROPOSED,
            )
        )
        if sess.context.get("had_scroll") or int(sess.context.get("scroll_count") or 0) > 0:
            out.append(
                OperationalIntent(
                    session_id=sess.id,
                    intent_type="browse_search_results",
                    params={"scroll_events": sess.context.get("scroll_count", 0)},
                    confidence=0.72,
                    evidence=["semantic_session", "mouse_scroll"],
                    status=OperationalIntentStatus.COMPLETED,
                )
            )

    elif sess.session_type == SESSION_LAUNCHER:
        out.append(
            OperationalIntent(
                session_id=sess.id,
                intent_type="launch_via_system_shell",
                params={
                    "query_preview": sess.context.get("launcher_query"),
                },
                confidence=sess.confidence,
                evidence=["launcher_session"],
                status=OperationalIntentStatus.COMPLETED
                if sess.status == SemanticSessionStatus.COMPLETED
                else OperationalIntentStatus.PROPOSED,
            )
        )

    elif sess.session_type == SESSION_SELECTION:
        out.append(
            OperationalIntent(
                session_id=sess.id,
                intent_type="activate_visible_choice",
                params={"label_hint": sess.context.get("selection_label")},
                confidence=sess.confidence,
                evidence=["selection_session"],
                status=OperationalIntentStatus.COMPLETED,
            )
        )

    elif sess.session_type == SESSION_FORM_FILL:
        out.append(
            OperationalIntent(
                session_id=sess.id,
                intent_type="complete_multi_field_form",
                params={
                    "fields_touched": sess.context.get("form_fields_count", 0),
                },
                confidence=sess.confidence,
                evidence=["form_fill_session"],
                status=OperationalIntentStatus.COMPLETED
                if "form_submit" in types_seen
                else OperationalIntentStatus.PROPOSED,
            )
        )

    elif sess.session_type == SESSION_DIALOG:
        out.append(
            OperationalIntent(
                session_id=sess.id,
                intent_type="resolve_modal_dialog",
                params={"resolution": sess.context.get("dialog_resolution")},
                confidence=sess.confidence,
                evidence=["dialog_confirmation_session"],
                status=OperationalIntentStatus.COMPLETED,
            )
        )

    elif sess.session_type == SESSION_FILE_NAV:
        out.append(
            OperationalIntent(
                session_id=sess.id,
                intent_type="navigate_filesystem",
                params={"path_hint": sess.context.get("path_hint")},
                confidence=sess.confidence,
                evidence=["file_navigation_session"],
                status=OperationalIntentStatus.COMPLETED,
            )
        )

    return out


def _seconds_between(a: datetime, b: datetime) -> float:
    try:
        return abs((a - b).total_seconds())
    except Exception:
        return 9999.0


def _compatible_proc(sess: SemanticSession, proc: Optional[str]) -> bool:
    primary = (sess.context.get("primary_proc") or "") or ""
    last = (sess.context.get("last_proc") or "") or ""
    ref = proc or ""
    if not ref:
        return True
    if sess.session_type == SESSION_LAUNCHER:
        return _is_shell_proc(ref) or ref.lower() == primary.lower()
    if sess.session_type == SESSION_NAVIGATION or sess.session_type == SESSION_SEARCH:
        return is_web_process(ref) or ref.lower() == primary.lower() or ref.lower() == last.lower()
    if sess.session_type == SESSION_FILE_NAV:
        return _is_explorer_proc(ref)
    return ref.lower() == primary.lower() or ref.lower() == last.lower()


def _hard_interrupt(
    sess: SemanticSession,
    ev: RawEvent,
    hyps: Sequence[SemanticHypothesis],
    mission: Mission,
) -> bool:
    proc, _, _ = _wc(ev)
    gap = _seconds_between(ev.timestamp, sess.updated_at)
    if gap > _HARD_GAP_S:
        return True

    if sess.session_type == SESSION_LAUNCHER:
        # Pasar a navegador/app distinta = fin de launcher
        if proc and not _is_shell_proc(proc) and is_web_process(proc):
            return True
        if proc and not _is_shell_proc(proc) and not _compatible_proc(sess, proc):
            return True

    if sess.session_type in {SESSION_NAVIGATION, SESSION_SEARCH}:
        if proc and not is_web_process(proc) and not _compatible_proc(sess, proc):
            if _is_explorer_proc(proc):
                return True
            if _is_shell_proc(proc):
                return True

    ht = _hyp_types(hyps)
    if "focus_browser_address_bar" in ht and sess.session_type != SESSION_NAVIGATION:
        return True

    return False


def _can_absorb(
    sess: SemanticSession,
    ev: RawEvent,
    hyps: Sequence[SemanticHypothesis],
) -> bool:
    gap = _seconds_between(ev.timestamp, sess.updated_at)
    if gap > _ABSORB_GAP_S:
        return False

    proc, _, _ = _wc(ev)
    if proc and not _compatible_proc(sess, proc):
        return False

    ht = _hyp_types(hyps)

    if sess.session_type == SESSION_NAVIGATION:
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            return True
        if ev.event_type == EventType.KEYBOARD_KEY_PRESS:
            return True
        if "open_url_candidate" in ht:
            return True
        return False

    if sess.session_type == SESSION_SEARCH:
        if ev.event_type == EventType.MOUSE_SCROLL:
            return True
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            return True
        if ev.event_type == EventType.KEYBOARD_KEY_PRESS:
            return True
        if "search_content_candidate" in ht or "confirm_semantic_value" in ht:
            return True
        return False

    if sess.session_type == SESSION_LAUNCHER:
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            return True
        if ev.event_type == EventType.MOUSE_CLICK:
            return True
        return bool(ht & {"open_app_candidate", "select_visible_option", "focus_input_field"})

    if sess.session_type == SESSION_SELECTION:
        return ev.event_type == EventType.KEYBOARD_KEY_PRESS and gap < 3.5

    if sess.session_type == SESSION_FORM_FILL:
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            return True
        if ev.event_type == EventType.KEYBOARD_KEY_PRESS:
            return True
        return False

    if sess.session_type == SESSION_DIALOG:
        return ev.event_type == EventType.KEYBOARD_KEY_PRESS

    if sess.session_type == SESSION_FILE_NAV:
        return ev.event_type in (EventType.MOUSE_CLICK, EventType.KEYBOARD_KEY_PRESS)

    return False


def _infer_open_session_type(ev: RawEvent, hyps: Sequence[SemanticHypothesis]) -> Optional[str]:
    ht = _hyp_types(hyps)
    proc, _, _ = _wc(ev)
    uia, web = _meta_uia_web(ev)

    if "focus_browser_address_bar" in ht:
        return SESSION_NAVIGATION

    if "select_visible_option" in ht or uia.get("control_type", "").lower() == "listitemcontrol":
        return SESSION_SELECTION

    if _shell_launcher_hint(ev, hyps) or (
        "focus_input_field" in ht and _is_shell_proc(proc)
    ):
        return SESSION_LAUNCHER

    role = (web.get("role") or "").lower()
    if role == "searchbox" or "search_content_candidate" in ht:
        return SESSION_SEARCH

    if _is_explorer_proc(proc):
        return SESSION_FILE_NAV

    if "confirm_input" in ht or "cancel_input" in ht:
        if role in {"alertdialog", "dialog"}:
            return SESSION_DIALOG

    # Form: campo texto genérico sin searchbox
    if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
        m = dict(ev.metadata or {})
        if m.get("field_session"):
            if role != "searchbox" and "search" not in (web.get("placeholder") or "").lower():
                if looks_like_external_url(str(m.get("final_text") or "")):
                    return SESSION_NAVIGATION
                return SESSION_FORM_FILL

    if ev.event_type == EventType.KEYBOARD_KEY_PRESS and ev.keyboard_action:
        mods = [str(m).lower() for m in (ev.keyboard_action.modifiers or [])]
        keys = [str(k).lower() for k in (ev.keyboard_action.keys or [])]
        if "ctrl" in mods and keys and keys[0] == "l":
            return SESSION_NAVIGATION

    return None


def _maybe_touch_completion(
    sess: SemanticSession,
    ev: RawEvent,
    hyps: Sequence[SemanticHypothesis],
    mission: Mission,
) -> None:
    """Señales de cierre suaves."""
    ht = _hyp_types(hyps)
    proc, _, _ = _wc(ev)

    if sess.session_type == SESSION_NAVIGATION:
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            ft = str(dict(ev.metadata or {}).get("final_text") or "").strip()
            if ft:
                sess.context["navigation_url_hint"] = ft[:500]
                if looks_like_external_url(ft):
                    sess.context["typed_url"] = ft[:500]
        if ev.event_type == EventType.KEYBOARD_KEY_PRESS and ev.keyboard_action:
            k = (ev.keyboard_action.keys or [""])[0].lower()
            if k in {"enter", "return"}:
                sess.completion_signals.append("navigation_submit")
                close_session(
                    mission, sess.id,
                    status=SemanticSessionStatus.COMPLETED,
                    reason="navigation_submit",
                    completion_signals=["navigation_submit"],
                )

    elif sess.session_type == SESSION_SEARCH:
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            ft = str(dict(ev.metadata or {}).get("final_text") or "").strip()
            if ft:
                sess.context["search_query"] = ft[:500]
        if "confirm_semantic_value" in ht or (
            ev.event_type == EventType.KEYBOARD_KEY_PRESS
            and ev.keyboard_action
            and (ev.keyboard_action.keys or [""])[0].lower() in {"enter", "return"}
        ):
            sess.completion_signals.append("search_submit")
            # Scroll puede seguir — cerramos tras submit solo si no hay scroll pronto;
            # simplificación: marcamos contexto y cerramos en siguiente scroll o gap
            sess.context["await_results_scroll"] = True

        if ev.event_type == EventType.MOUSE_SCROLL:
            sess.context["had_scroll"] = True
            sess.context["scroll_count"] = int(sess.context.get("scroll_count") or 0) + 1
            sess.completion_signals.append("scroll_continuation")
            if sess.context.get("await_results_scroll"):
                close_session(
                    mission, sess.id,
                    status=SemanticSessionStatus.COMPLETED,
                    reason="search_then_scroll",
                    completion_signals=["search_submit", "scroll_continuation"],
                )

    elif sess.session_type == SESSION_LAUNCHER:
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            ft = str(dict(ev.metadata or {}).get("final_text") or "").strip()
            if ft:
                sess.context["launcher_query"] = ft[:300]
        if "select_visible_option" in ht or (
            ev.event_type == EventType.MOUSE_CLICK and not _is_shell_proc(proc or "")
        ):
            close_session(
                mission, sess.id,
                status=SemanticSessionStatus.COMPLETED,
                reason="launcher_pick_or_leave_shell",
            )

    elif sess.session_type == SESSION_SELECTION:
        uia, _ = _meta_uia_web(ev)
        label = uia.get("name")
        if label:
            sess.context["selection_label"] = str(label)[:200]
        if ev.event_type == EventType.KEYBOARD_KEY_PRESS and ev.keyboard_action:
            k = (ev.keyboard_action.keys or [""])[0].lower()
            if k in {"enter", "return"}:
                close_session(
                    mission, sess.id,
                    status=SemanticSessionStatus.COMPLETED,
                    reason="selection_confirm",
                )

    elif sess.session_type == SESSION_FORM_FILL:
        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            n = int(sess.context.get("form_fields_count") or 0) + 1
            sess.context["form_fields_count"] = n
        if ev.event_type == EventType.KEYBOARD_KEY_PRESS and ev.keyboard_action:
            k = (ev.keyboard_action.keys or [""])[0].lower()
            mods = ev.keyboard_action.modifiers or []
            if k in {"enter", "return"} and not mods:
                sess.completion_signals.append("form_submit")
                close_session(
                    mission, sess.id,
                    status=SemanticSessionStatus.COMPLETED,
                    reason="form_submit",
                )

    elif sess.session_type == SESSION_DIALOG:
        if "confirm_input" in ht:
            sess.context["dialog_resolution"] = "confirm"
        elif "cancel_input" in ht:
            sess.context["dialog_resolution"] = "cancel"
        if "confirm_input" in ht or "cancel_input" in ht:
            close_session(
                mission, sess.id,
                status=SemanticSessionStatus.COMPLETED,
                reason="dialog_key",
            )

    elif sess.session_type == SESSION_FILE_NAV:
        if ev.event_type == EventType.MOUSE_CLICK:
            sess.context["path_hint"] = sess.context.get("last_title_hint")


def observe_after_record_step(mission: Mission, step: RawEvent) -> None:
    """Hook tras cada RawEvent durante grabación (después de semantic shadow)."""
    if not getattr(mission, "session_shadow_mode", False):
        return

    hyp_ids = list((step.metadata or {}).get("semantic_shadow_hypothesis_ids") or [])
    hyps = [h for h in mission.intent_timeline if h.id in hyp_ids]

    act = active_session(mission)

    if act:
        if _hard_interrupt(act, step, hyps, mission):
            close_session(
                mission, act.id,
                status=SemanticSessionStatus.COMPLETED,
                reason="hard_context_change",
            )
            act = active_session(mission)

    act = active_session(mission)
    if act and _can_absorb(act, step, hyps):
        absorb_event(act, step, hyps)
        _maybe_touch_completion(act, step, hyps, mission)
        return

    # Intento de apertura nueva
    stype = _infer_open_session_type(step, hyps)
    if stype is None:
        # Sin hipótesis: inferencia mínima desde evento crudo
        if step.event_type == EventType.MOUSE_SCROLL and active_session(mission):
            s = active_session(mission)
            if s and s.session_type == SESSION_SEARCH:
                absorb_event(s, step, [])
                _maybe_touch_completion(s, step, [], mission)
        return

    # Cerrar activa incompatible
    act = active_session(mission)
    if act:
        if act.session_type != stype:
            close_session(
                mission, act.id,
                status=SemanticSessionStatus.ABANDONED,
                reason=f"superseded_by:{stype}",
            )

    goals = {
        SESSION_LAUNCHER: "Usar el launcher del sistema para localizar o abrir un destino",
        SESSION_NAVIGATION: "Navegar a un sitio vía barra de direcciones u omnibox",
        SESSION_SEARCH: "Buscar contenido en contexto de aplicación",
        SESSION_SELECTION: "Elegir una opción visible de lista o menú",
        SESSION_FORM_FILL: "Completar uno o más campos de formulario",
        SESSION_DIALOG: "Confirmar o cancelar un diálogo modal",
        SESSION_FILE_NAV: "Explorar carpetas o archivos en el shell del sistema",
    }

    open_session(
        mission,
        stype,
        step,
        hyps,
        inferred_goal=goals.get(stype, ""),
        dominant_intent=stype,
        confidence=0.64 + 0.05 * min(3, len(hyps)),
        notes="opened_from_observation",
    )
    act = active_session(mission)
    if act:
        _maybe_touch_completion(act, step, hyps, mission)


# ── Comparación SEP (sombras) ─────────────────────────────────────────────

_SESSION_SEP_BUCKETS: Dict[str, List[str]] = {
    SESSION_LAUNCHER: ["open_app"],
    SESSION_NAVIGATION: ["open_url", "open_site", "open_new_tab"],
    SESSION_SEARCH: ["search_youtube", "search_site", "search_web"],
    SESSION_SELECTION: ["select_profile", "open_app", "open_search_result", "use_selected_profile"],
    SESSION_FORM_FILL: ["type_text"],
    SESSION_DIALOG: ["confirm_dialog"],
    SESSION_FILE_NAV: ["file_operation"],
}


def compare_session_vs_sep(mission: Mission) -> Dict[str, Any]:
    """Alineación grosera sesiones operacionales vs pasos SEP."""
    sep = mission.semantic_execution_plan or {}
    steps = sep.get("steps") or []
    sep_types = [str(s.get("type") or "").lower() for s in steps if isinstance(s, dict)]

    sessions = [s for s in mission.semantic_sessions if s.status != SemanticSessionStatus.ACTIVE]
    if not sep_types:
        return {
            "session_agreement_with_sep": 0.0,
            "matched_sessions_to_sep": 0,
            "detail": "no_sep",
        }

    sep_set = set(sep_types)
    matched_sess = 0
    for sess in sessions:
        buckets = _SESSION_SEP_BUCKETS.get(sess.session_type, [])
        if buckets and set(buckets) & sep_set:
            matched_sess += 1

    rate = round(
        min(1.0, matched_sess / max(len(sep_types), 1)),
        4,
    )
    return {
        "session_agreement_with_sep": rate,
        "matched_sessions_to_sep": matched_sess,
        "sep_step_count": len(sep_types),
        "closed_session_count": len(sessions),
    }


def build_session_audit_dict(mission: Mission) -> Dict[str, Any]:
    sess = mission.semantic_sessions or []
    intents = mission.operational_intents or []
    active_n = sum(1 for s in sess if s.status == SemanticSessionStatus.ACTIVE)
    confs = [s.confidence for s in sess if s.confidence]
    avg_conf = round(sum(confs) / len(confs), 4) if confs else 0.0
    cmp_r = compare_session_vs_sep(mission)

    return {
        "session_count": len(sess),
        "active_sessions": active_n,
        "operational_intent_count": len(intents),
        "session_agreement_with_sep": cmp_r.get("session_agreement_with_sep"),
        "average_session_confidence": avg_conf,
        "compare_detail": cmp_r,
    }
