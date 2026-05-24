"""Semantic-first recorder — modo shadow (Fase 1).

Emite hipótesis durante la grabación sin sustituir SIPE/Collapse ni el SEP.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    EventType,
    IntentSnapshot,
    Mission,
    RawEvent,
    SemanticHypothesis,
    SemanticHypothesisStatus,
)
from app.core.logger import log
from app.services.missions.field_commit import looks_like_external_url


def is_shadow_enabled(settings) -> bool:
    """True si ``SEMANTIC_FIRST_SHADOW_ENABLED`` está activo en settings."""
    try:
        return bool(getattr(settings, "SEMANTIC_FIRST_SHADOW_ENABLED", False))
    except Exception:
        return False


def mission_shadow_active(mission: Mission) -> bool:
    return (mission.semantic_first_mode or "").lower() == "shadow"


def _confidence_label(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.42:
        return "medium"
    return "low"


def _uia(meta: Dict[str, Any]) -> Dict[str, Any]:
    m = meta or {}
    return dict(m.get("uia") or m.get("uia_data") or {})


def _web(meta: Dict[str, Any]) -> Dict[str, Any]:
    m = meta or {}
    return dict(m.get("web") or m.get("web_data") or {})


def _truth_snap(meta: Dict[str, Any]) -> Dict[str, Any]:
    iso = meta.get("target_identity_isolation") or {}
    cc = meta.get("capture_contract") or {}
    return {
        "trusted_pre_action_identity": bool(iso.get("trusted_pre_action_identity")),
        "trusted_pre_action_reasons": list(iso.get("trusted_pre_action_reasons") or []),
        "capture_complete": bool(cc.get("capture_complete")),
        "sufficient_for_execution": bool(cc.get("sufficient_for_execution")),
    }


def _ambiguity_snap(meta: Dict[str, Any]) -> Dict[str, Any]:
    il = meta.get("intent_layer") or {}
    if not isinstance(il, dict):
        return {}
    return {
        k: il.get(k)
        for k in ("ambiguity", "confidence", "fusion_audit", "intent_decision")
        if k in il
    }


def _is_windows_shell_launcher(
    proc: Optional[str], meta: Dict[str, Any],
) -> bool:
    p = (proc or "").lower().strip()
    if p in {"searchhost.exe", "searchapp.exe"}:
        return True
    uia = _uia(meta)
    aid = (uia.get("automation_id") or "").strip().lower()
    return aid == "searchtextbox"


def _clear_click_target(uia: Dict[str, Any], web: Dict[str, Any]) -> bool:
    ct = (uia.get("control_type") or "").strip().lower()
    if ct in {
        "buttoncontrol", "menuitemcontrol", "listitemcontrol",
        "tabitemcontrol", "hyperlinkscontrol", "hyperlinkcontrol",
        "editcontrol", "edit", "comboboxcontrol", "checkboxcontrol",
        "radiobuttoncontrol",
    }:
        return True
    role = (web.get("role") or "").strip().lower()
    tag = (web.get("tag") or "").strip().lower()
    if role in {"button", "link", "menuitem", "option", "tab", "searchbox", "textbox"}:
        return True
    if tag in {"input", "textarea", "button", "a", "select"}:
        return True
    return bool(uia.get("automation_id")) or bool(web.get("locators"))


def _should_emit_click_hypotheses(meta: Dict[str, Any]) -> bool:
    iso = meta.get("target_identity_isolation") or {}
    trusted = bool(iso.get("trusted_pre_action_identity"))
    uia, web = _uia(meta), _web(meta)
    clear = _clear_click_target(uia, web)
    return trusted or clear


def build_click_hypotheses(ev: RawEvent) -> List[SemanticHypothesis]:
    meta = dict(ev.metadata or {})
    if not _should_emit_click_hypotheses(meta):
        return []

    uia, web = _uia(meta), _web(meta)
    wc = ev.window_context
    proc = getattr(wc, "process_name", None) if wc else None

    ct = (uia.get("control_type") or "").strip().lower()
    name = (uia.get("name") or "").strip()
    role = (web.get("role") or "").strip().lower()

    hyps: List[SemanticHypothesis] = []
    truth = _truth_snap(meta)
    amb = _ambiguity_snap(meta)

    shell = _is_windows_shell_launcher(proc, meta)

    def add(
        intent: str,
        confidence: float,
        *,
        params: Optional[Dict[str, Any]] = None,
        notes: str,
        carriers: Optional[List[str]] = None,
        evidence_refs: Optional[List[str]] = None,
    ) -> None:
        hyps.append(
            SemanticHypothesis(
                event_id=ev.id,
                timestamp=ev.timestamp,
                source_event_ids=[ev.id],
                candidate_intent_type=intent,
                params=params or {},
                confidence=confidence,
                confidence_label=_confidence_label(confidence),
                evidence_refs=evidence_refs or ["raw_trace", "metadata.uia", "metadata.web"],
                carriers=carriers or ["uia", "dom", "window_context"],
                execution_truth_snapshot=truth,
                semantic_ambiguity_snapshot=amb,
                status=SemanticHypothesisStatus.PROPOSED,
                created_by="recorder_semantic_shadow",
                notes=notes,
            )
        )

    # Edit / search field in launcher
    if shell and ct in {"editcontrol", "edit", "comboboxcontrol"}:
        add(
            "focus_input_field",
            0.78,
            params={"field_label": name or None, "shell_launcher": True},
            notes="Campo de texto en launcher del sistema; probable foco para escribir.",
        )
        add(
            "open_app_candidate",
            0.55,
            params={"channel": "system_launcher_search"},
            notes="Escribir aquí suele preceder abrir una app desde resultados.",
        )

    elif ct in {"editcontrol", "edit", "documentcontrol", "comboboxcontrol"} or role in {"textbox", "searchbox", "combobox"}:
        add(
            "focus_input_field",
            0.74,
            params={"field_label": name or web.get("placeholder") or web.get("accessible_name")},
            notes="Click sobre control de entrada; probable foco de campo.",
        )

    # Buttons / links / list items
    href = (web.get("href") or "").strip()
    if ct in {"listitemcontrol", "menuitemcontrol"} or role in {"option", "menuitem"}:
        add(
            "select_visible_option",
            0.8,
            params={"label": name or web.get("text"), "role": role or ct},
            notes="Selección de opción visible en lista o menú.",
        )
        if shell:
            add(
                "open_app_candidate",
                0.62,
                params={"context": "launcher_result"},
                notes="Resultado en launcher del sistema; podría lanzar una app.",
            )
    elif ct in {"buttoncontrol", "hyperlinkscontrol", "hyperlinkcontrol"} or role in {"button", "link"}:
        add(
            "click_button_by_label",
            0.76,
            params={"label": name or web.get("text")},
            notes="Activación de botón o enlace identificado.",
        )
        if href and looks_like_external_url(href):
            add(
                "open_url_candidate",
                0.68,
                params={"href_preview": href[:120]},
                notes="Enlace con URL externa aparente.",
            )

    elif shell and name:
        # Generic result tile in launcher (non-edit)
        add(
            "open_app_candidate",
            0.58,
            params={"result_label": name},
            notes="Interacción en launcher del sistema con elemento nombrado.",
        )

    # Dedupe identical intent+params (rare)
    seen: set = set()
    unique: List[SemanticHypothesis] = []
    for h in hyps:
        key = (h.candidate_intent_type, str(sorted((h.params or {}).items())))
        if key in seen:
            continue
        seen.add(key)
        unique.append(h)
    return unique


def build_keyboard_type_text_hypotheses(ev: RawEvent) -> List[SemanticHypothesis]:
    meta = dict(ev.metadata or {})
    if not meta.get("field_session"):
        return []
    wc = ev.window_context
    proc = getattr(wc, "process_name", None) if wc else None
    web = _web(meta)
    uia = _uia(meta)
    text = (meta.get("final_text") or "").strip()
    hyps: List[SemanticHypothesis] = []

    truth = _truth_snap(meta)
    amb = _ambiguity_snap(meta)

    def add(intent: str, conf: float, *, params: Dict[str, Any], notes: str) -> None:
        hyps.append(
            SemanticHypothesis(
                event_id=ev.id,
                timestamp=ev.timestamp,
                source_event_ids=[ev.id],
                candidate_intent_type=intent,
                params=params,
                confidence=conf,
                confidence_label=_confidence_label(conf),
                evidence_refs=["raw_trace", "field_commit"],
                carriers=["field_session"],
                execution_truth_snapshot=truth,
                semantic_ambiguity_snapshot=amb,
                status=SemanticHypothesisStatus.PROPOSED,
                created_by="recorder_semantic_shadow",
                notes=notes,
            )
        )

    role = (web.get("role") or "").strip().lower()
    placeholder = (web.get("placeholder") or "").strip().lower()

    add(
        "type_field_value",
        0.82,
        params={"text_preview": text[:80], "label": meta.get("field_label")},
        notes="Valor consolidado tras sesión de campo.",
    )

    if _is_windows_shell_launcher(proc, meta):
        add(
            "open_app_candidate",
            0.6,
            params={"channel": "system_launcher_search", "query_preview": text[:80]},
            notes="Texto en launcher del sistema; suele usarse para abrir apps.",
        )

    if looks_like_external_url(text):
        add(
            "open_url_candidate",
            0.7,
            params={"url_preview": text[:120]},
            notes="Texto parece URL externa.",
        )

    if role == "searchbox" or "search" in placeholder:
        add(
            "search_content_candidate",
            0.77,
            params={"query_preview": text[:120], "role": role},
            notes="Campo de búsqueda con contenido consolidado.",
        )

    return hyps


def build_hotkey_hypotheses(ev: RawEvent) -> List[SemanticHypothesis]:
    meta = dict(ev.metadata or {})
    ka = ev.keyboard_action
    if not ka or not ka.keys:
        return []
    keys = [str(k).lower() for k in ka.keys]
    mods = [str(m).lower() for m in (ka.modifiers or [])]
    k0 = keys[0]
    truth = _truth_snap(meta)
    amb = _ambiguity_snap(meta)

    def one(intent: str, conf: float, notes: str) -> SemanticHypothesis:
        return SemanticHypothesis(
            event_id=ev.id,
            timestamp=ev.timestamp,
            source_event_ids=[ev.id],
            candidate_intent_type=intent,
            params={"key": k0, "modifiers": mods},
            confidence=conf,
            confidence_label=_confidence_label(conf),
            evidence_refs=["raw_trace", "keyboard_action"],
            carriers=["hotkey"],
            execution_truth_snapshot=truth,
            semantic_ambiguity_snapshot=amb,
            status=SemanticHypothesisStatus.PROPOSED,
            created_by="recorder_semantic_shadow",
            notes=notes,
        )

    hyps: List[SemanticHypothesis] = []

    if "ctrl" in mods and k0 == "t":
        hyps.append(one("open_new_tab", 0.88, "Atajo común para nueva pestaña en navegador."))
    if "ctrl" in mods and k0 == "l":
        hyps.append(one("focus_browser_address_bar", 0.85, "Atajo común para foco en barra de direcciones."))

    if not mods:
        if k0 in {"enter", "return"}:
            hyps.append(one("confirm_input", 0.62, "Enter: confirmación genérica."))
        elif k0 in {"esc", "escape"}:
            hyps.append(one("cancel_input", 0.58, "Escape: cancelación o cierre de diálogo."))
        elif k0 == "tab":
            hyps.append(one("focus_next_field", 0.55, "Tab: foco al siguiente control."))

    return hyps


def build_enter_confirmation_hypotheses(
    ev: RawEvent,
    prior: Optional[RawEvent],
) -> List[SemanticHypothesis]:
    """Enter tras consolidar texto en campo de búsqueda."""
    if ev.event_type != EventType.KEYBOARD_KEY_PRESS:
        return []
    ka = ev.keyboard_action
    if not ka or not ka.keys:
        return []
    if str(ka.keys[0]).lower() not in {"enter", "return"}:
        return []
    if prior is None or prior.event_type != EventType.KEYBOARD_TYPE_TEXT:
        return []
    pmeta = dict(prior.metadata or {})
    if not pmeta.get("field_session"):
        return []
    web = _web(pmeta)
    role = (web.get("role") or "").strip().lower()
    placeholder = (web.get("placeholder") or "").strip().lower()
    text = (pmeta.get("final_text") or "").strip()

    if role != "searchbox" and "search" not in placeholder:
        return []

    meta = dict(ev.metadata or {})
    truth = _truth_snap(meta)
    amb = _ambiguity_snap(meta)

    return [
        SemanticHypothesis(
            event_id=ev.id,
            timestamp=ev.timestamp,
            source_event_ids=[prior.id, ev.id],
            candidate_intent_type="confirm_semantic_value",
            params={"after_field_commit": True},
            confidence=0.72,
            confidence_label=_confidence_label(0.72),
            evidence_refs=["raw_trace", "keyboard_sequence"],
            carriers=["field_session", "key_press"],
            execution_truth_snapshot=truth,
            semantic_ambiguity_snapshot=amb,
            status=SemanticHypothesisStatus.PROPOSED,
            created_by="recorder_semantic_shadow",
            notes="Enter tras texto en control tipo búsqueda; envío/consolidación probable.",
        ),
        SemanticHypothesis(
            event_id=ev.id,
            timestamp=ev.timestamp,
            source_event_ids=[prior.id, ev.id],
            candidate_intent_type="search_content_candidate",
            params={"query_preview": text[:120], "submitted": True},
            confidence=0.8,
            confidence_label=_confidence_label(0.8),
            evidence_refs=["raw_trace", "keyboard_sequence"],
            carriers=["field_session", "key_press"],
            execution_truth_snapshot=truth,
            semantic_ambiguity_snapshot=amb,
            status=SemanticHypothesisStatus.PROPOSED,
            created_by="recorder_semantic_shadow",
            notes="Búsqueda enviada tras campo searchbox.",
        ),
    ]


def _append_snapshot(mission: Mission, hyps: Sequence[SemanticHypothesis]) -> None:
    if not hyps:
        return
    types = [h.candidate_intent_type for h in hyps]
    summary_bits = [f"{t}:{h.confidence_label}" for t, h in zip(types, hyps)]
    snap = IntentSnapshot(
        hypothesis_ids=[h.id for h in hyps],
        dominant_candidate_types=list(dict.fromkeys(types)),
        summary=" · ".join(summary_bits[:6]),
    )
    mission.intent_snapshots.append(snap)


def emit_for_raw_event(mission: Mission, step: RawEvent) -> None:
    """Extiende ``mission.intent_timeline`` según el evento (best-effort)."""
    from app.core.config import settings as _settings

    if not is_shadow_enabled(_settings):
        return
    if not mission_shadow_active(mission):
        return

    new_hyps: List[SemanticHypothesis] = []

    try:
        if step.event_type in (
            EventType.MOUSE_CLICK,
            EventType.MOUSE_DOUBLE_CLICK,
            EventType.MOUSE_RIGHT_CLICK,
        ):
            new_hyps.extend(build_click_hypotheses(step))

        elif step.event_type == EventType.KEYBOARD_TYPE_TEXT:
            new_hyps.extend(build_keyboard_type_text_hypotheses(step))

        elif step.event_type == EventType.KEYBOARD_KEY_PRESS:
            new_hyps.extend(build_hotkey_hypotheses(step))
            prior: Optional[RawEvent] = None
            if mission.raw_trace:
                rt = mission.raw_trace
                if len(rt) >= 2:
                    prior = rt[-2]
            new_hyps.extend(build_enter_confirmation_hypotheses(step, prior))

        elif step.event_type == EventType.KEYBOARD_HOTKEY:
            new_hyps.extend(build_hotkey_hypotheses(step))

    except Exception as e:
        log.debug(f"recorder_semantic_shadow.emit_for_raw_event: {e}")
        return

    if not new_hyps:
        return

    mission.intent_timeline.extend(new_hyps)

    meta = dict(step.metadata or {})
    meta["semantic_shadow_hypothesis_ids"] = [h.id for h in new_hyps]
    step.metadata = meta

    _append_snapshot(mission, new_hyps)


def build_shadow_audit_dict(mission: Mission) -> Dict[str, Any]:
    """Diagnóstico para Mission Review (evita errores silent en UI/tests).

    Nota 2026-05 TEL (Fase 1): ``tel_diag`` evalúa TEL mediante
    ``extract_truth_blocks`` (no persiste ``truth_blocks`` en la misión).
    Para persistencia al cerrar grabación ver ``MissionRecorder.stop``.
    """
    from app.services.missions.semantic_shadow_compare import compare_shadow_to_sep

    hyps = mission.intent_timeline or []
    accepted = [h for h in hyps if h.status == SemanticHypothesisStatus.ACCEPTED]
    cmp_res = compare_shadow_to_sep(mission)
    audit: Dict[str, Any] = {
        "semantic_first_shadow_enabled": mission.semantic_first_mode == "shadow",
        "hypotheses_count": len(hyps),
        "accepted_hypotheses_count": len(accepted),
        "shadow_vs_sipe_agreement": cmp_res.get("collapse_agreement_rate"),
        "top_mismatches": cmp_res.get("top_mismatches", []),
        "compare_detail": {k: v for k, v in cmp_res.items() if k != "top_mismatches"},
    }
    try:
        if getattr(mission, "session_shadow_mode", False) or getattr(
            mission, "semantic_sessions", None,
        ):
            from app.services.missions.semantic_session_engine import (
                build_session_audit_dict as _sess_audit,
            )

            audit["semantic_sessions_diag"] = _sess_audit(mission)
    except Exception:
        pass
    try:
        if getattr(mission, "cob_shadow_mode", False) or getattr(
            mission, "canonical_operational_blocks", None,
        ):
            from app.services.missions.canonical_operational_blocks import (
                build_cob_audit,
            )

            audit["cob_diag"] = build_cob_audit(mission)
    except Exception:
        pass
    try:
        if getattr(mission, "tel_shadow_mode", False) or getattr(mission, "truth_blocks", None):
            from app.services.missions.truth_extraction_layer import (
                extract_truth_blocks,
                build_tel_audit,
            )

            audit["tel_diag"] = build_tel_audit(mission, extract_truth_blocks(mission))
    except Exception:
        pass
    return audit
