"""Truth Extraction Layer (TEL) — Fase 1 sombra.

Convierte señales intermedias ya modeladas por sesiones/COBs/hipótesis en bloques
operacionales *humanos* estables sin filtrar mecánica de runtime hacia etiquetas.

No sustituye SEP, SmartExecutor, COB ni replay. Persiste paralelo auditable.

Arquitectura futura (no activa aquí): gating SEP tras ``execution_readiness``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from app.contracts.mission import (
    CanonicalOperationalBlock,
    EventType,
    Mission,
    OperationalTruthBlock,
    OperationalTruthStatus,
    RawEvent,
    SemanticHypothesis,
)

# ── Configuración de estabilización (determinista, sin whitelist por producto).
_SILENT_GAP_SUPPRESSES_DUPLICATE_S = 0.35
# Fusionar duplicados casi simultáneos del mismo ``truth_type``.

# Tokens que indican fuga de mecánica hacia texto operacional auditoría SEP.
_RUNTIME_MECHANIC_MARKERS = (
    "uia_",
    "dom_",
    "smart_route",
    "coords",
    "keypress",
    "ctrl+l",
    "ctrl+t",
)


# Hipótesis que representan ruído de mecanismo absorbidas en verdades más altas.
_MECHANICS_HYPOTHESIS_KINDS: FrozenSet[str] = frozenset(
    {
        "focus_browser_address_bar",
        "confirm_input",
        "focus_next_field",
        "cancel_input",
    }
)


# Comparación SEP / context‑switch (segundos). No valores por app — solo deltas temporales grandes.
CONTEXT_SWITCH_GAP_HINT_S = 12.0
_HYP_MECHANICS_CARRIER: FrozenSet[str] = frozenset({"hotkey", "key_press"})


COB_CANONICAL_TO_TRUTH_TYPE: Dict[str, str] = {
    "open_application": "open_application",
    "navigate_to_site": "navigate_to_location",
    "search_content_and_browse_results": "search_content",
    "select_visible_entity": "select_entity",
    "fill_and_submit_form": "fill_form",
    "open_new_tab": "open_tab",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mission_tel_shadow_active(mission: Mission) -> bool:
    return bool(getattr(mission, "tel_shadow_mode", False))


def _ev_index(mission: Mission) -> Dict[str, RawEvent]:
    return {ev.id: ev for ev in (mission.raw_trace or [])}


def _cob_anchor_time(cob: CanonicalOperationalBlock, idx: Dict[str, RawEvent]) -> float:
    eids = list(cob.source_event_ids or [])
    if not eids:
        return 0.0
    anchor = idx.get(eids[0])
    if anchor is None:
        return float(eids and hash(eids[0]) % 1000)
    ts = getattr(anchor, "timestamp", None)
    try:
        return ts.timestamp() if ts else float(anchor.offset_ms or 0)
    except Exception:
        return 0.0


def _gather_hypotheses(
    mission: Mission,
    hyp_ids: Sequence[str],
    extra_event_ids: Optional[Sequence[str]] = None,
) -> List[SemanticHypothesis]:
    want = set(hyp_ids or [])
    evt_set = set(extra_event_ids or [])
    out: List[SemanticHypothesis] = []
    for h in mission.intent_timeline or []:
        if h.id in want:
            out.append(h)
            continue
        if not evt_set:
            continue
        hid = list(h.source_event_ids or [])
        if hid and evt_set.intersection(hid):
            out.append(h)
    # Dedup por id estable
    seen: Set[str] = set()
    uniq: List[SemanticHypothesis] = []
    for h in sorted(out, key=lambda x: (x.timestamp, x.id)):
        if h.id in seen:
            continue
        seen.add(h.id)
        uniq.append(h)
    return uniq


def _hypothesis_noise_metrics(hyps: Sequence[SemanticHypothesis]) -> Tuple[int, int]:
    absorbed_noise = 0
    mechanic_only = 0
    for h in hyps:
        carriers = {str(c).lower() for c in (h.carriers or [])}
        if carriers and carriers.issubset(_HYP_MECHANICS_CARRIER):
            absorbed_noise += 1
            mechanic_only += 1
            continue
        if str(h.candidate_intent_type) in _MECHANICS_HYPOTHESIS_KINDS:
            absorbed_noise += 1
    return absorbed_noise, mechanic_only


def _target_identity_from_ev(ev: Optional[RawEvent]) -> Dict[str, Any]:
    if ev is None:
        return {}
    meta = dict(ev.metadata or {})
    iso = meta.get("target_identity_isolation")
    pre = meta.get("pre_action_identity")
    wc = getattr(ev, "window_context", None)
    tgt: Dict[str, Any] = {
        "from_event_id": ev.id,
    }
    if isinstance(iso, dict):
        tgt["trusted_pre_action_identity"] = bool(iso.get("trusted_pre_action_identity"))
        tgt["trusted_pre_action_reasons"] = list(iso.get("trusted_pre_action_reasons") or [])
        lbl = iso.get("target_label") or iso.get("expected_label_hint")
        if lbl:
            tgt["target_label"] = lbl
    if wc is not None:
        pn = getattr(wc, "process_name", None)
        ttl = getattr(wc, "title", None)
        if pn:
            tgt["foreground_process_hint"] = str(pn)[:140]
        if ttl:
            tgt["foreground_title_hint"] = str(ttl)[:160]
    if isinstance(pre, dict) and pre:
        tgt["pre_action_digest"] = {k: pre.get(k) for k in list(pre.keys())[:12]}
    fr = meta.get("operational_freeze_snapshot")
    if isinstance(fr, dict) and fr.get("pre_transition_confirmed"):
        try:
            from app.services.runtime.pre_click_operational_freeze import (
                tel_truth_enrichment_from_freeze,
            )
            from app.contracts.mission import OperationalFreezeSnapshot

            freeze = OperationalFreezeSnapshot.model_validate(fr)
            tgt["pcof_truth"] = tel_truth_enrichment_from_freeze(freeze)
            best = fr.get("best_entity_candidate") or {}
            if best.get("display_name"):
                tgt["target_label"] = best["display_name"]
            tgt["trusted_pre_action_identity"] = True
        except Exception:
            pass
    return tgt


def _outcome_identity_from_ev(ev: Optional[RawEvent]) -> Dict[str, Any]:
    if ev is None:
        return {}
    meta = dict(ev.metadata or {})
    pst = meta.get("post_action_state") or meta.get("outcome_preview")
    out: Dict[str, Any] = {"from_event_id": ev.id}
    if pst:
        out["post_action_compact"] = str(pst)[:400]
    uia_n = (((meta.get("uia") or meta.get("uia_data")) or {}) or {}).get("name")
    if uia_n:
        out["post_uia_name_hint"] = str(uia_n)[:200]
    return out


def _ambiguity_scores(cob: CanonicalOperationalBlock, hyps: Sequence[SemanticHypothesis]) -> Tuple[float, float]:
    base = cob.ambiguity or {}
    sess_amb = dict((base.get("session_ambiguity") or {}))
    hyp_n = float(sess_amb.get("hypothesis_type_count") or 0)
    alt_penalty = 0.06 * min(hyp_n, 6.0)
    amb = float(0.22 + alt_penalty)
    gates = dict((base.get("promotion_gates") or {}))
    overall_gate = float(gates.get("overall") or cob.confidence)
    clarification = gates.get("requires_clarification")
    amb_bump = 0.12 if clarification else 0.0
    ambiguity_score = max(0.0, min(1.0, 1.0 - overall_gate * 0.55 + amb + amb_bump))
    stab = float(cob.stability_score or 0.0)
    return ambiguity_score, stab


def _execution_readiness(stability: float, ambiguity_score: float, anchor_strength: float) -> float:
    readiness = (
        0.38 * stability
        + 0.37 * max(0.0, 1.0 - ambiguity_score)
        + 0.25 * anchor_strength
    )
    return max(0.0, min(1.0, round(readiness, 4)))


def _anchor_strength(first_ev: Optional[RawEvent], last_ev: Optional[RawEvent]) -> float:
    acc = _target_identity_from_ev(first_ev)
    base = 0.42
    if acc.get("trusted_pre_action_identity"):
        base += 0.38
    if acc.get("target_label"):
        base += 0.12
    pst = _outcome_identity_from_ev(last_ev)
    if pst.get("post_action_compact"):
        base += 0.08
    return max(0.0, min(1.0, base))


def _replay_anchors(acc: Dict[str, Any]) -> List[str]:
    anchors: List[str] = []
    lbl = acc.get("target_label") or acc.get("foreground_title_hint")
    if lbl:
        anchors.append(str(lbl))
    pn = acc.get("foreground_process_hint")
    if pn:
        anchors.append(f"app:{pn}")
    return anchors[:12]


def _validators_for_truth(truth_type: str) -> List[str]:
    return {
        "open_application": ["foreground_matches_expected_executable", "no_transient_shell_overlay"],
        "select_entity": ["single_visible_choice_resolved"],
        "navigate_to_location": ["url_bar_or_omnibox_resolved", "navigation_settled"],
        "search_content": ["search_box_value_matches_intent"],
        "browse_results": ["results_surface_visible"],
        "fill_form": ["field_roles_identified"],
        "submit_form": ["commit_or_implicit_submit_ack"],
        "open_tab": ["new_tab_foreground_stable"],
        "switch_context": ["window_activation_coherent"],
        "confirm_dialog": ["modal_resolved"],
        "choose_option": ["option_committed"],
        "open_document": ["document_workspace_ready"],
        "save_document": ["persist_ack"],
        "export_content": ["export_finished_or_dialog_clear"],
    }.get(truth_type, ["semantic_surface_ready"])


def _human_strings(cob: CanonicalOperationalBlock, truth_type: str) -> str:
    params = cob.params or {}
    if truth_type == "open_application":
        q = params.get("query_preview") or params.get("launcher_query")
        if q:
            return f"Abrir aplicación (consulta launcher: «{str(q)[:120]}»)"
        return "Abrir aplicación desde el lanzador del sistema"
    if truth_type == "select_entity":
        lab = params.get("label_hint") or params.get("selection_label")
        if lab:
            return f"Seleccionar entidad visible: «{str(lab)[:140]}»"
        return "Seleccionar elemento visible"
    if truth_type == "navigate_to_location":
        u = params.get("url_preview") or params.get("navigation_url_hint") or params.get("typed_url")
        if u:
            return f"Navegar a «{str(u)[:140]}»"
        return "Navegar al destino declarado"
    if truth_type == "search_content":
        q = params.get("query_preview") or params.get("search_query")
        if q:
            return f"Buscar contenido: «{str(q)[:200]}»"
        return "Buscar contenido en el contexto activo"
    if truth_type == "browse_results":
        return "Revisar resultados (desplazamiento / exploración)"
    if truth_type == "fill_form":
        nf = params.get("fields_touched") or params.get("form_fields_count")
        return f"Rellenar formulario ({nf} campo(s) tocados)"
    if truth_type == "submit_form":
        return "Enviar formulario consolidado"
    if truth_type == "open_tab":
        return "Abrir nueva pestaña"
    return f"Operación: {truth_type}".replace("_", " ")


def _sep_mechanics_leakage_penalty(mission: Mission) -> float:
    sep = mission.semantic_execution_plan or {}
    steps = sep.get("steps") or []
    if not isinstance(steps, list) or not steps:
        return 0.0
    leaks = 0
    inspected = 0
    for s in steps:
        if not isinstance(s, dict):
            continue
        inspected += 1
        hay = (
            str(s.get("preferred_strategy") or "").lower()
            + " "
            + str(s.get("type") or "").lower()
        )
        lbl = str(s.get("human_label") or "").lower()
        for tok in _RUNTIME_MECHANIC_MARKERS:
            if tok in hay or tok in lbl:
                leaks += 1
                break
    if not inspected:
        return 0.0
    return round(leaks / inspected, 4)


def _truth_block_core(
    *,
    truth_type: str,
    cob: CanonicalOperationalBlock,
    hyps: List[SemanticHypothesis],
    first_ev: Optional[RawEvent],
    last_ev: Optional[RawEvent],
    emitted_note: str,
    absorbed_events_override: Optional[List[str]] = None,
) -> OperationalTruthBlock:
    ambiguity_score, stability = _ambiguity_scores(cob, hyps)
    anchors = _replay_anchors(_target_identity_from_ev(first_ev))
    anchor_strength = _anchor_strength(first_ev, last_ev)
    readiness = _execution_readiness(stability, ambiguity_score, anchor_strength)

    tgt = _target_identity_from_ev(first_ev)
    outcome = _outcome_identity_from_ev(last_ev)

    absorbed_noise, _ = _hypothesis_noise_metrics(hyps)
    temporal = {}
    try:
        if first_ev:
            temporal["started_at_iso"] = first_ev.timestamp.isoformat()
        if last_ev:
            temporal["ended_at_iso"] = last_ev.timestamp.isoformat()
        if first_ev and last_ev:
            dt = (last_ev.timestamp - first_ev.timestamp).total_seconds()
            temporal["elapsed_s_rounded"] = round(max(0.0, dt), 3)
    except Exception:
        pass

    requires_confirm = ambiguity_score >= 0.46 or (
        tgt.get("trusted_pre_action_identity") is False and truth_type != "browse_results"
    )

    dbg = {
        "commit_rationale": emitted_note,
        "source_cob_id": cob.session_id or "",
        "cob_canonical_action": cob.canonical_action,
        "absorbed_noise_hypothesis_approx": absorbed_noise,
        "absorbed_raw_steps_in_cob": int(cob.absorbed_steps or 0),
    }

    evt_ids = list(absorbed_events_override or cob.source_event_ids or [])

    return OperationalTruthBlock(
        truth_type=truth_type,
        canonical_action=truth_type,
        human_intent=_human_strings(cob, truth_type),
        absorbed_events=list(dict.fromkeys(evt_ids)),
        absorbed_hypotheses=[h.id for h in hyps],
        absorbed_sessions=list(dict.fromkeys(cob.source_session_ids or [cob.session_id])),
        context_window=dict(cob.semantic_ambiguity_snapshot or {})
        | {"truth_window_note": emitted_note},
        stability_score=stability,
        operational_confidence=float(min(1.0, max(cob.confidence, readiness))),
        ambiguity_score=ambiguity_score,
        execution_readiness=readiness,
        requires_human_confirmation=bool(requires_confirm),
        target_snapshot=tgt,
        outcome_snapshot=outcome,
        temporal_signature=temporal,
        replay_anchor_candidates=anchors or _replay_anchors(tgt),
        validator_expectations=_validators_for_truth(truth_type),
        emitted_from="truth_extraction_layer",
        truth_status=OperationalTruthStatus.COMMITTED_SHADOW,
        debug_trace=dbg,
    )


def _split_search_blocks(
    mission: Mission,
    cob: CanonicalOperationalBlock,
    idx: Dict[str, RawEvent],
) -> List[OperationalTruthBlock]:
    hyps = _gather_hypotheses(mission, cob.source_hypothesis_ids or [], cob.source_event_ids)
    eids = list(cob.source_event_ids or [])
    first_ev = idx.get(eids[0]) if eids else None
    last_ev = idx.get(eids[-1]) if eids else None

    comps = list(cob.completion_signals or ())
    ctxt = getattr(
        mission,
        "semantic_sessions",
        [],
    )
    sess = next((s for s in ctxt if s.id == cob.session_id), None)
    scroll_evidence = False
    if sess and isinstance(sess.context, dict):
        scroll_evidence = bool(
            sess.context.get("had_scroll")
            or int(sess.context.get("scroll_count") or 0) > 0,
        )

    truths: List[OperationalTruthBlock] = []
    reason_base = (
        "search_session_stable; mechanics like ctrl+l / enter folded into absorbed hypotheses"
    )
    truths.append(
        _truth_block_core(
            truth_type="search_content",
            cob=cob,
            hyps=hyps,
            first_ev=first_ev,
            last_ev=last_ev,
            emitted_note=reason_base,
        )
    )

    if scroll_evidence or any("scroll" in str(c).lower() for c in comps):
        br = OperationalTruthBlock(
            truth_type="browse_results",
            canonical_action="browse_results",
            human_intent="Explorar resultados de búsqueda",
            absorbed_events=list(eids)[-max(12, len(eids) // 3) :] or list(eids),
            absorbed_hypotheses=[h.id for h in hyps],
            absorbed_sessions=list(dict.fromkeys(cob.source_session_ids or [cob.session_id])),
            context_window=dict(cob.semantic_ambiguity_snapshot or {})
            | {"split_from": cob.session_id},
            stability_score=min(1.0, float(cob.stability_score or 0) + 0.06),
            operational_confidence=float(cob.confidence) * 0.96,
            ambiguity_score=max(
                0.05,
                _ambiguity_scores(cob, hyps)[0] * 0.85,
            ),
            execution_readiness=_execution_readiness(
                float(cob.stability_score or 0.55),
                _ambiguity_scores(cob, hyps)[0] * 0.85,
                _anchor_strength(first_ev, last_ev),
            ),
            requires_human_confirmation=False,
            target_snapshot=dict(_target_identity_from_ev(first_ev)),
            outcome_snapshot=dict(_outcome_identity_from_ev(last_ev)),
            temporal_signature=dict(
                truths[-1].temporal_signature,
            ),
            replay_anchor_candidates=_replay_anchors(_target_identity_from_ev(first_ev))
            + ["viewport:scroll_region"],
            validator_expectations=_validators_for_truth("browse_results"),
            emitted_from="truth_extraction_layer",
            truth_status=OperationalTruthStatus.COMMITTED_SHADOW,
            debug_trace={
                "commit_rationale": "scroll_continuation_consolidates_browsing_intent",
                "parent_truth_id": truths[-1].truth_id if truths else "",
            },
        )
        truths.append(br)
    return truths


def _form_fill_blocks(
    mission: Mission,
    cob: CanonicalOperationalBlock,
    idx: Dict[str, RawEvent],
) -> List[OperationalTruthBlock]:
    hyps = _gather_hypotheses(mission, cob.source_hypothesis_ids or [], cob.source_event_ids)
    eids = list(cob.source_event_ids or [])
    first_ev = idx.get(eids[0]) if eids else None
    last_ev = idx.get(eids[-1]) if eids else None
    comps = cob.completion_signals or []
    truths: List[OperationalTruthBlock] = [
        _truth_block_core(
            truth_type="fill_form",
            cob=cob,
            hyps=hyps,
            first_ev=first_ev,
            last_ev=last_ev,
            emitted_note="multi_field_accumulation_stable",
        )
    ]

    submit_tail: Optional[str] = None
    if "form_submit" in comps and eids:
        for evid in reversed(eids):
            ev = idx.get(evid)
            if not ev:
                continue
            if ev.event_type == EventType.KEYBOARD_KEY_PRESS:
                submit_tail = ev.id
                break
            if ev.event_type == EventType.MOUSE_CLICK:
                submit_tail = ev.id
                break
        sid = submit_tail or eids[-1]
        truths.append(
            _truth_block_core(
                truth_type="submit_form",
                cob=cob,
                hyps=hyps,
                first_ev=idx.get(sid),
                last_ev=last_ev,
                emitted_note="form_submit_boundary_detected",
                absorbed_events_override=[sid],
            )
        )
    return truths


def _truth_from_cob_generic(
    mission: Mission,
    cob: CanonicalOperationalBlock,
    truth_type: str,
    idx: Dict[str, RawEvent],
    note: str,
) -> OperationalTruthBlock:
    hyps = _gather_hypotheses(mission, cob.source_hypothesis_ids or [], cob.source_event_ids)
    eids = list(cob.source_event_ids or [])
    first_ev = idx.get(eids[0]) if eids else None
    last_ev = idx.get(eids[-1]) if eids else None
    block = _truth_block_core(
        truth_type=truth_type,
        cob=cob,
        hyps=hyps,
        first_ev=first_ev,
        last_ev=last_ev,
        emitted_note=note,
    )
    cob_params = dict(cob.params or {})
    if cob_params:
        cw = dict(block.context_window or {})
        derived = dict(cw.get("tel_derived_params") or {})
        for key in (
            "label_hint", "selection_label", "label", "profile_name",
            "launcher_query", "query_preview",
        ):
            if cob_params.get(key) and key not in derived:
                derived[key] = cob_params[key]
        cw["tel_derived_params"] = derived
        block.context_window = cw
    if truth_type in ("select_entity", "choose_option"):
        try:
            from app.services.runtime.universal_collection_entity_engine import (
                enrich_truth_block_with_collection_entity,
            )
            enrich_truth_block_with_collection_entity(
                block, mission, first_ev=first_ev,
            )
        except Exception as exc:
            log.debug("[TEL] uces enrich truth block: %s", exc)
    return block


def truths_from_standalone_hypotheses(
    mission: Mission,
    cob_event_union: Set[str],
) -> List[OperationalTruthBlock]:
    idx = _ev_index(mission)
    truths: List[OperationalTruthBlock] = []
    for h in sorted(mission.intent_timeline or [], key=lambda x: (x.timestamp, x.id)):
        if str(h.candidate_intent_type) != "open_new_tab":
            continue
        eids = list(h.source_event_ids or [])
        if not eids:
            continue
        if cob_event_union.issuperset(set(eids)):
            continue

        synth_cob_like = CanonicalOperationalBlock(
            session_id="hypothesis_standalone_open_tab",
            operational_intent_id="",
            block_type="hotkey_hypothesis",
            canonical_action="open_new_tab_hypothesis",
            params={},
            confidence=float(h.confidence or 0.75),
            source_event_ids=eids,
            source_hypothesis_ids=[h.id],
            absorbed_steps=len(eids),
            stability_score=0.74,
            notes="standalone_signal",
            semantic_ambiguity_snapshot=dict(h.semantic_ambiguity_snapshot or {}),
            ambiguity={"session_ambiguity": {"hypothesis_type_count": 1}},
        )
        truths.append(
            _truth_from_cob_generic(
                mission,
                synth_cob_like,
                "open_tab",
                idx,
                "ctrl+t hypothesis not fully absorbed into navigation COB",
            )
        )
    return truths


def _window_switch_context_truth(
    mission: Mission,
    idx: Dict[str, RawEvent],
    cob_windows: Sequence[CanonicalOperationalBlock],
) -> List[OperationalTruthBlock]:
    """Ventanas explícitas con gap entre COBs grandes → cambio de contexto."""
    if len(cob_windows) < 2:
        return []
    truths: List[OperationalTruthBlock] = []
    for a, b in zip(cob_windows, cob_windows[1:]):
        ta = _cob_anchor_time(a, idx)
        tb = _cob_anchor_time(b, idx)
        if tb > ta + CONTEXT_SWITCH_GAP_HINT_S and a.canonical_action != b.canonical_action:
            ev_b = idx.get((b.source_event_ids or ["", ""])[0])
            synth = CanonicalOperationalBlock(
                session_id=f"interop_{b.session_id}",
                operational_intent_id="",
                block_type="context_gap",
                canonical_action="switch_context_gap",
                params={"gap_estimate_s": round(tb - ta, 2)},
                confidence=0.62,
                source_event_ids=[(b.source_event_ids or ["", ""])[0]],
                absorbed_steps=0,
                stability_score=0.5,
                notes="infer_context_switch_between_blocks",
                ambiguity={"session_ambiguity": {}},
            )
            truths.append(
                _truth_from_cob_generic(
                    mission,
                    synth,
                    "switch_context",
                    idx,
                    "temporal_gap_and_foreground_transition",
                )
            )
            if ev_b:
                truths[-1].target_snapshot.setdefault("bridge_from_cob_session", a.session_id)
                truths[-1].outcome_snapshot.setdefault("following_cob_session", b.session_id)
    return truths


def stabilization_merge_neighbor_duplicates(blocks: List[OperationalTruthBlock]) -> List[OperationalTruthBlock]:
    if len(blocks) < 2:
        return blocks
    merged: List[OperationalTruthBlock] = []
    i = 0
    n = len(blocks)
    while i < n:
        cur = blocks[i]
        j = i + 1
        while j < n:
            nxt = blocks[j]
            if cur.truth_type != nxt.truth_type:
                break
            t0 = (cur.temporal_signature or {}).get("started_at_iso")
            t1 = (nxt.temporal_signature or {}).get("started_at_iso")
            consume = False
            try:
                if t0 and t1:
                    t_a = datetime.fromisoformat(str(t0).replace("Z", "+00:00"))
                    t_b = datetime.fromisoformat(str(t1).replace("Z", "+00:00"))
                    consume = (
                        abs((t_b - t_a).total_seconds()) < _SILENT_GAP_SUPPRESSES_DUPLICATE_S
                    )
            except Exception:
                consume = False
            if not consume:
                break
            cur.absorbed_events = list(dict.fromkeys(cur.absorbed_events + nxt.absorbed_events))
            cur.absorbed_hypotheses = list(
                dict.fromkeys(cur.absorbed_hypotheses + nxt.absorbed_hypotheses)
            )
            cur.debug_trace = dict(cur.debug_trace or {})
            cur.debug_trace["merged_duplicate_neighbor"] = True
            j += 1
        merged.append(cur)
        i = j
    return merged


def extract_truth_blocks(mission: Mission) -> List[OperationalTruthBlock]:
    """Construye lista ordenada por tiempo de OperationalTruthBlock desde COBs + hipótesis."""

    cob_list = sorted(
        mission.canonical_operational_blocks or [],
        key=lambda c: _cob_anchor_time(c, _ev_index(mission)),
    )

    idx = _ev_index(mission)

    truths: List[OperationalTruthBlock] = []
    for cob in cob_list:
        ctype = cob.canonical_action or ""
        if ctype == "search_content_and_browse_results":
            truths.extend(_split_search_blocks(mission, cob, idx))
        elif ctype == "fill_and_submit_form":
            truths.extend(_form_fill_blocks(mission, cob, idx))
        else:
            ttype = COB_CANONICAL_TO_TRUTH_TYPE.get(ctype)
            if not ttype:
                continue
            note = "closed_session_stable_outcome_mapped_to_human_truth"

            truths.append(_truth_from_cob_generic(mission, cob, ttype, idx, note))

    union_ids: Set[str] = set()
    for cob in cob_list:
        union_ids.update(cob.source_event_ids or [])

    truths.extend(truths_from_standalone_hypotheses(mission, union_ids))
    truths.extend(_window_switch_context_truth(mission, idx, cob_list))

    truths.sort(
        key=lambda b: datetime.fromisoformat(
            ((b.temporal_signature or {}).get("started_at_iso") or utc_now_iso()).replace(
                "Z",
                "+00:00",
            ),
        )
        .timestamp(),
    )

    truths = stabilization_merge_neighbor_duplicates(truths)
    for tb in truths:
        if tb.truth_type not in ("select_entity", "choose_option"):
            continue
        eids = list(tb.absorbed_events or [])
        first_ev = idx.get(eids[0]) if eids else None
        try:
            from app.services.runtime.universal_collection_entity_engine import (
                enrich_truth_block_with_collection_entity,
            )
            enrich_truth_block_with_collection_entity(
                tb, mission, first_ev=first_ev,
            )
        except Exception as exc:
            log.debug("[TEL] uces enrich pass: %s", exc)
    return truths


def build_tel_audit(mission: Mission, blocks: Sequence[OperationalTruthBlock]) -> Dict[str, Any]:
    sep = mission.semantic_execution_plan or {}
    sep_steps = [s for s in (sep.get("steps") or []) if isinstance(s, dict)]

    sep_n = len(sep_steps)
    tel_n = len(blocks)

    compression_tel_vs_sep = round(sep_n / max(tel_n, 1), 4) if sep_n else None
    raw_n = len(mission.raw_trace or [])
    compression_raw_vs_truth = round(raw_n / max(tel_n, 1), 4) if raw_n else 0.0

    noise_hypotheses_absorbed = 0
    for b in blocks:
        dbg = b.debug_trace or {}
        noise_hypotheses_absorbed += int(dbg.get("absorbed_noise_hypothesis_approx") or 0)

    operational_clarity = round(
        sum(b.execution_readiness for b in blocks) / max(tel_n, 1),
        4,
    ) if tel_n else 0.0

    amb_reduction = round(
        max(0.0, 1.0 - (sum(b.ambiguity_score for b in blocks) / max(tel_n, 1))),
        4,
    ) if tel_n else 0.0

    mech_penalty_sep = _sep_mechanics_leakage_penalty(mission)

    summary_lines = []
    for b in blocks:
        dbg = b.debug_trace or {}
        summary_lines.append(
            {
                "truth_type": b.truth_type,
                "human_intent": b.human_intent[:120],
                "stability_score": b.stability_score,
                "ambiguity_score": b.ambiguity_score,
                "execution_readiness": b.execution_readiness,
                "noise_absorption_hint": dbg.get("absorbed_noise_hypothesis_approx"),
                "commit_rationale": dbg.get("commit_rationale"),
            }
        )

    return {
        "tel_shadow_mode": getattr(mission, "tel_shadow_mode", False),
        "truth_block_count": tel_n,
        "sep_step_count": sep_n,
        "compression_sep_steps_per_truth_block": compression_tel_vs_sep,
        "compression_raw_events_per_truth_block": compression_raw_vs_truth,
        "noise_hypotheses_absorbed_aggregate": noise_hypotheses_absorbed,
        "operational_clarity_avg": operational_clarity,
        "ambiguity_reduction_score": amb_reduction,
        "replay_simplification_ratio": compression_tel_vs_sep,
        "sep_runtime_mechanics_leak_penalty": mech_penalty_sep,
        "truth_summary": summary_lines[:24],
        "compared_at": utc_now_iso(),
    }


def apply_tel_to_mission_record(mission: Mission, *, rebuild_audit: bool = True) -> None:
    """Puebla mission.truth_blocks y tel_audit cuando ``tel_shadow_mode``."""

    if not mission_tel_shadow_active(mission):
        return
    blocks = extract_truth_blocks(mission)
    mission.truth_blocks = blocks
    if rebuild_audit:
        mission.tel_audit = build_tel_audit(mission, blocks)


def finalize_tel_audit_only(mission: Mission) -> None:
    """Solo auditoría desde truth_blocks existentes."""

    blocks = getattr(mission, "truth_blocks", None) or []
    mission.tel_audit = build_tel_audit(mission, blocks)


__all__ = [
    "apply_tel_to_mission_record",
    "mission_tel_shadow_active",
    "extract_truth_blocks",
    "build_tel_audit",
    "finalize_tel_audit_only",
    "utc_now_iso",
]
