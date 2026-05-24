"""Operational Truth Preservation (OTP) — extremo a extremo.

Ley: si existe ``canonical_operational_intent`` con ``canonical_truth=True``,
no puede eliminarse por collapse, fusionarse en silencio, degradarse a
``smart_route`` ni perder ``human_label``.

Pipeline rastreado::

    Recorder → FFEC → COI → ICE → UCES → SEP → UOL → Mission Review → Runtime
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    CanonicalOperationalIntent,
    Mission,
    OperationalTruthChain,
    OperationalTruthDisposition,
    OperationalTruthNode,
    OperationalTruthStage,
)
from app.core.logger import log

_TECHNICAL_LABEL_MARKERS = (
    "smart_route",
    "uia_text_match",
    "click_visual",
    "click_fallback",
    "elemento identificado automáticamente",
    "coords",
    "collection_first",
)

_STAGE_ORDER: Tuple[OperationalTruthStage, ...] = (
    OperationalTruthStage.RECORDER,
    OperationalTruthStage.FFEC,
    OperationalTruthStage.COI,
    OperationalTruthStage.ICE,
    OperationalTruthStage.UCES,
    OperationalTruthStage.SEP,
    OperationalTruthStage.UOL_COMPILER,
    OperationalTruthStage.MISSION_REVIEW,
    OperationalTruthStage.RUNTIME_PLANNER,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def truth_origin_id_for_coi(coi: CanonicalOperationalIntent) -> str:
    iid = str(coi.intent_id or "").strip()
    if iid:
        return f"coi:{iid}"
    eid = str(coi.source_event_id or "").strip()
    return f"coi:ev:{eid}" if eid else f"coi:anon:{id(coi)}"


def _normalize_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def truth_fingerprint(
    *,
    operational_kind: str,
    human_label: str = "",
    target_name: str = "",
    query: str = "",
    coi_origin_id: str = "",
    operational_element_identity_id: str = "",
) -> str:
    """Huella estable para emparejar verdad entre etapas."""
    if operational_element_identity_id:
        return f"uoei:{operational_element_identity_id}"
    if coi_origin_id:
        return coi_origin_id
    kind = _normalize_key(operational_kind)
    tgt = _normalize_key(target_name)
    q = _normalize_key(query)
    hl = _normalize_key(human_label)
    if kind == "select_entity_from_collection" and tgt:
        return f"sel:{tgt}"
    if kind in ("select_profile",) and tgt:
        return f"sel:{tgt}"
    if kind in ("search_site", "search_content", "search_youtube") and q:
        return f"search:{q}"
    if kind == "open_app" and tgt:
        return f"open_app:{tgt}"
    if hl:
        return f"hl:{hl}"
    return f"kind:{kind}"


def _contains_technical_noise(label: str) -> bool:
    low = (label or "").lower()
    return any(m in low for m in _TECHNICAL_LABEL_MARKERS)


def node_from_coi(
    coi: CanonicalOperationalIntent,
    stage: OperationalTruthStage,
    *,
    disposition: OperationalTruthDisposition = OperationalTruthDisposition.PRESERVED,
    truth_loss_reason: str = "",
) -> OperationalTruthNode:
    from app.services.missions.freeze_first_execution_core import human_ready_label_for_coi

    origin = truth_origin_id_for_coi(coi)
    hl = human_ready_label_for_coi(coi)
    ent = coi.entity
    return OperationalTruthNode(
        truth_origin_step_id=origin,
        stage=stage,
        disposition=disposition,
        truth_preserved=disposition != OperationalTruthDisposition.LOST,
        truth_loss_reason=truth_loss_reason,
        human_label=hl,
        operational_kind="select_entity_from_collection",
        canonical_truth=bool(coi.canonical_truth),
        coi_intent_id=str(coi.intent_id or ""),
        source_event_id=str(coi.source_event_id or ""),
        metadata={
            "entity_display": ent.display_name if ent else "",
            "truth_strength": float(coi.truth_strength or 0.0),
        },
    )


def _extract_target_from_step_dict(step: Dict[str, Any]) -> str:
    params = step.get("params") or {}
    ent = params.get("collection_entity") or params.get("entity") or {}
    if isinstance(ent, dict):
        name = str(ent.get("display_name") or "").strip()
        if name:
            return name
    for key in ("profile", "profile_name", "selection_label", "name", "app"):
        v = str(params.get(key) or "").strip()
        if v:
            return v
    return ""


def _extract_query_from_step_dict(step: Dict[str, Any]) -> str:
    params = step.get("params") or {}
    return str(
        params.get("query") or params.get("val") or params.get("text") or "",
    ).strip()


def node_from_semantic_step_dict(
    step: Dict[str, Any],
    stage: OperationalTruthStage,
    *,
    coi: Optional[CanonicalOperationalIntent] = None,
    disposition: Optional[OperationalTruthDisposition] = None,
    truth_loss_reason: str = "",
) -> OperationalTruthNode:
    step_id = str(step.get("id") or "")
    kind = str(step.get("type") or "")
    hl = str(step.get("human_label") or "")
    params = step.get("params") or {}
    tgt = _extract_target_from_step_dict(step)
    query = _extract_query_from_step_dict(step)

    canon = bool(params.get("canonical_truth") or params.get("uol_canonical_truth"))
    coi_id = ""
    origin = step_id or f"sep:{kind}:{tgt or query or hl}"
    if coi and coi.canonical_truth:
        origin = truth_origin_id_for_coi(coi)
        canon = True
        coi_id = str(coi.intent_id or "")
        if not hl or _contains_technical_noise(hl):
            from app.services.missions.freeze_first_execution_core import (
                human_ready_label_for_coi,
            )

            hl = human_ready_label_for_coi(coi)

    fp = truth_fingerprint(
        operational_kind=kind,
        human_label=hl,
        target_name=tgt,
        query=query,
        coi_origin_id=origin if canon and coi else "",
    )
    if not step_id:
        origin = fp

    disp = disposition
    if disp is None:
        if _contains_technical_noise(hl) and canon:
            disp = OperationalTruthDisposition.DEGRADED
        elif canon and hl:
            disp = OperationalTruthDisposition.PRESERVED
        else:
            disp = OperationalTruthDisposition.TRANSFORMED

    preserved = disp not in (
        OperationalTruthDisposition.LOST,
        OperationalTruthDisposition.DEGRADED,
    )
    if disp == OperationalTruthDisposition.DEGRADED:
        preserved = False

    pref = str(step.get("preferred_strategy") or params.get("preferred_strategy") or "")
    loss = truth_loss_reason
    if canon and "smart_route" in pref.lower() and not loss:
        disp = OperationalTruthDisposition.DEGRADED
        preserved = False
        loss = loss or "degraded_to_smart_route_strategy"

    return OperationalTruthNode(
        truth_origin_step_id=origin,
        stage=stage,
        disposition=disp,
        truth_preserved=preserved,
        truth_loss_reason=loss,
        human_label=hl,
        operational_kind=kind,
        canonical_truth=canon,
        coi_intent_id=coi_id,
        source_event_id=str(
            step.get("source_event_id")
            or params.get("source_event_id")
            or (coi.source_event_id if coi else "")
            or "",
        ),
        metadata={"fingerprint": fp, "preferred_strategy": pref},
    )


def collect_canonical_cois(mission: Mission) -> List[CanonicalOperationalIntent]:
    out: List[CanonicalOperationalIntent] = []
    seen: set = set()
    for coi in mission.canonical_operational_intents or []:
        if not coi.canonical_truth:
            continue
        oid = truth_origin_id_for_coi(coi)
        if oid in seen:
            continue
        seen.add(oid)
        out.append(coi)
    for ev in mission.raw_trace or []:
        meta = dict(getattr(ev, "metadata", None) or {})
        raw = meta.get("canonical_operational_intent")
        if not isinstance(raw, dict) or not raw.get("canonical_truth"):
            continue
        try:
            coi = CanonicalOperationalIntent.model_validate(raw)
        except Exception:
            continue
        oid = truth_origin_id_for_coi(coi)
        if oid in seen:
            continue
        seen.add(oid)
        out.append(coi)
    return out


def _coi_covers_step(coi: CanonicalOperationalIntent, step: Dict[str, Any]) -> bool:
    params = step.get("params") or {}
    eid = str(coi.source_event_id or "")
    if eid and str(params.get("source_event_id") or step.get("source_event_id") or "") == eid:
        return True
    kind = str(step.get("type") or "")
    if kind not in (
        "select_profile",
        "select_entity_from_collection",
        "select_item",
        "use_selected_profile",
    ):
        return False
    ent = coi.entity
    if not ent or not ent.display_name:
        return False
    tgt = _extract_target_from_step_dict(step)
    if not tgt:
        return False
    from app.services.missions.universal_operational_language import normalize_display_name

    a = _normalize_key(normalize_display_name(ent.display_name))
    b = _normalize_key(normalize_display_name(tgt))
    if a == b:
        return True
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if a_tokens and a_tokens == b_tokens:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 10 and shorter in longer:
        return True
    return False


def enforce_canonical_on_semantic_step_dict(
    step: Dict[str, Any],
    coi: CanonicalOperationalIntent,
) -> Dict[str, Any]:
    """Refuerza human_label, params y estrategias para verdad canónica."""
    from app.services.missions.freeze_first_execution_core import human_ready_label_for_coi

    out = dict(step)
    params = dict(out.get("params") or {})
    params["canonical_truth"] = True
    params["uol_canonical_truth"] = True
    params["canonical_operational_intent"] = coi.model_dump(mode="json")
    params["source_event_id"] = str(
        coi.source_event_id or params.get("source_event_id") or "",
    )
    if coi.entity:
        params.setdefault(
            "collection_entity",
            coi.entity.model_dump(mode="json"),
        )
        params["selection_label"] = coi.entity.display_name
        params.setdefault("candidate_count", 1)
        params.setdefault("selection_confidence", float(coi.truth_strength or 0.95))

    pref = str(out.get("preferred_strategy") or params.get("preferred_strategy") or "")
    if "smart_route" in pref.lower():
        pref = "select_entity:collection_first"
    fallbacks = [
        f for f in (out.get("fallback_strategies") or params.get("fallback_strategies") or [])
        if "smart_route" not in str(f).lower()
    ]

    try:
        from app.services.missions.operational_truth_lineage import human_label_from_coi

        hl = human_label_from_coi(coi)
    except Exception:
        from app.services.missions.freeze_first_execution_core import human_ready_label_for_coi

        hl = human_ready_label_for_coi(coi)
    out["params"] = params
    out["human_label"] = hl
    out["preferred_strategy"] = pref
    out["fallback_strategies"] = fallbacks
    if str(out.get("type") or "") == "select_profile":
        out["type"] = "select_entity_from_collection"
        out["needs_user_label"] = False
    return out


def semantic_step_dict_from_coi(
    coi: CanonicalOperationalIntent,
    *,
    step_index: int = 0,
    mission_id: str = "m",
    mission: Optional[Mission] = None,
) -> Dict[str, Any]:
    """Construye un paso SEP desde COI vía UOL (delegación lineage)."""
    from app.services.missions.operational_truth_lineage import sep_step_dict_from_coi_uol

    m = mission if mission is not None else Mission(id=mission_id, name="")
    return sep_step_dict_from_coi_uol(coi, mission=m)


def reconcile_canonical_truths_on_sep_steps(
    mission: Mission,
    steps: List[Any],
) -> List[Any]:
    """Asegura que cada COI canónico tenga paso SEP con label humano correcto."""
    cois = collect_canonical_cois(mission)
    if not cois:
        return steps

    dict_steps: List[Dict[str, Any]] = []
    for sp in steps:
        converted = _sep_step_to_dict(sp)
        if converted:
            dict_steps.append(converted)

    for coi in cois:
        matched = False
        for i, sp in enumerate(dict_steps):
            if _coi_covers_step(coi, sp):
                dict_steps[i] = enforce_canonical_on_semantic_step_dict(sp, coi)
                matched = True
                break
        if not matched:
            dict_steps.append(
                semantic_step_dict_from_coi(
                    coi,
                    step_index=len(dict_steps),
                    mission_id=str(mission.id or "m"),
                    mission=mission,
                ),
            )
            log.info(
                "[otp] backfill SEP step for canonical COI %s at index %s",
                coi.intent_id,
                len(dict_steps) - 1,
            )

    from app.services.missions.semantic_execution_plan import SemanticPlanStep

    try:
        from app.services.missions.operational_truth_lineage import _merge_steps_by_trace_order

        dict_steps = _merge_steps_by_trace_order(mission, dict_steps)
    except Exception as exc:
        log.debug("[otp] chronological merge: %s", exc)

    out: List[Any] = []
    for raw in dict_steps:
        try:
            out.append(SemanticPlanStep.from_dict(raw))
        except Exception:
            out.append(raw)
    return out


def record_stage_transform(
    chain: OperationalTruthChain,
    *,
    stage_in: OperationalTruthStage,
    stage_out: OperationalTruthStage,
    nodes_in: Sequence[OperationalTruthNode],
    nodes_out: Sequence[OperationalTruthNode],
) -> None:
    """Registra transformación entre etapas y detecta pérdidas."""
    out_by_origin: Dict[str, OperationalTruthNode] = {
        n.truth_origin_step_id: n for n in nodes_out if n.truth_origin_step_id
    }
    for nin in nodes_in:
        if not nin.canonical_truth:
            continue
        oid = nin.truth_origin_step_id
        nout = out_by_origin.get(oid)
        link_in = nin.model_copy(
            update={"emitted_to_stage": stage_out, "received_from_stage": stage_in},
        )
        chain.nodes.append(link_in)
        if nout is None:
            lost_node = nin.model_copy(
                update={
                    "stage": stage_out,
                    "disposition": OperationalTruthDisposition.LOST,
                    "truth_preserved": False,
                    "truth_loss_reason": f"lost_at_{stage_out.value}",
                    "received_from_stage": stage_in,
                },
            )
            chain.nodes.append(lost_node)
            if oid not in chain.lost:
                chain.lost.append(oid)
            chain.loss_by_stage.setdefault(stage_out.value, [])
            if oid not in chain.loss_by_stage[stage_out.value]:
                chain.loss_by_stage[stage_out.value].append(oid)
            continue

        chain.nodes.append(nout)
        if nout.disposition == OperationalTruthDisposition.PRESERVED:
            if oid not in chain.preserved:
                chain.preserved.append(oid)
        elif nout.disposition == OperationalTruthDisposition.DEGRADED:
            if oid not in chain.degraded:
                chain.degraded.append(oid)
            chain.loss_by_stage.setdefault(stage_out.value, [])
            if oid not in chain.loss_by_stage[stage_out.value]:
                chain.loss_by_stage[stage_out.value].append(oid)
        elif nout.disposition == OperationalTruthDisposition.TRANSFORMED:
            if oid not in chain.transformed:
                chain.transformed.append(oid)
        elif nout.disposition == OperationalTruthDisposition.LOST:
            if oid not in chain.lost:
                chain.lost.append(oid)


def build_coi_stage_nodes(mission: Mission) -> List[OperationalTruthNode]:
    nodes: List[OperationalTruthNode] = []
    for coi in collect_canonical_cois(mission):
        nodes.append(node_from_coi(coi, OperationalTruthStage.COI))
    return nodes


def build_sep_stage_nodes(mission: Mission) -> List[OperationalTruthNode]:
    sep = mission.semantic_execution_plan or {}
    cois = {truth_origin_id_for_coi(c): c for c in collect_canonical_cois(mission)}
    nodes: List[OperationalTruthNode] = []
    for sp in sep.get("steps") or []:
        if not isinstance(sp, dict):
            continue
        coi = None
        for c in cois.values():
            if _coi_covers_step(c, sp):
                coi = c
                break
        nodes.append(
            node_from_semantic_step_dict(sp, OperationalTruthStage.SEP, coi=coi),
        )
    return nodes


def build_uol_stage_nodes(mission: Mission) -> List[OperationalTruthNode]:
    from app.services.missions.universal_operational_language import (
        compile_semantic_step_to_uol,
        human_operational_label_for_uol,
    )
    from app.services.missions.freeze_first_execution_core import find_coi_for_event

    sep = mission.semantic_execution_plan or {}
    nodes: List[OperationalTruthNode] = []
    for sp in sep.get("steps") or []:
        if not isinstance(sp, dict):
            continue
        coi = None
        eid = str(sp.get("source_event_id") or (sp.get("params") or {}).get("source_event_id") or "")
        if eid:
            coi = find_coi_for_event(mission, eid)
        action = compile_semantic_step_to_uol(sp, coi=coi)
        if action is None:
            continue
        hl = human_operational_label_for_uol(action)
        disp = OperationalTruthDisposition.PRESERVED
        loss = ""
        if action.canonical_truth and _contains_technical_noise(hl):
            disp = OperationalTruthDisposition.DEGRADED
            loss = "uol_human_label_technical_noise"
        pref = ""
        if action.execution_contract.preferred_runtime_strategies:
            pref = action.execution_contract.preferred_runtime_strategies[0]
        if action.canonical_truth and "smart_route" in pref.lower():
            disp = OperationalTruthDisposition.DEGRADED
            loss = loss or "uol_preferred_smart_route"

        origin = (
            truth_origin_id_for_coi(coi)
            if coi and coi.canonical_truth
            else str(sp.get("id") or "")
        )
        nodes.append(
            OperationalTruthNode(
                truth_origin_step_id=origin,
                stage=OperationalTruthStage.UOL_COMPILER,
                disposition=disp,
                truth_preserved=disp == OperationalTruthDisposition.PRESERVED,
                truth_loss_reason=loss,
                human_label=hl,
                operational_kind=action.uol_action.value,
                canonical_truth=action.canonical_truth,
                coi_intent_id=action.coi_intent_id,
                source_event_id=action.source_event_id,
                metadata={"preferred_strategy": pref},
            ),
        )
    return nodes


def build_mission_review_stage_nodes(mission: Mission) -> List[OperationalTruthNode]:
    from app.services.missions.mission_review_summary import build_mission_review_summary

    summary = build_mission_review_summary(mission, recompute=False)
    sep = mission.semantic_execution_plan or {}
    sem_steps = list(sep.get("steps") or [])
    nodes: List[OperationalTruthNode] = []
    for i, line in enumerate(summary.steps):
        sp = sem_steps[i] if i < len(sem_steps) else {}
        coi = None
        for c in collect_canonical_cois(mission):
            if isinstance(sp, dict) and _coi_covers_step(c, sp):
                coi = c
                break
        origin = (
            truth_origin_id_for_coi(coi)
            if coi
            else str(sp.get("id") if isinstance(sp, dict) else f"review:{i}")
        )
        disp = OperationalTruthDisposition.PRESERVED
        loss = ""
        if coi and coi.canonical_truth:
            if _contains_technical_noise(line.human_label):
                disp = OperationalTruthDisposition.DEGRADED
                loss = "mission_review_adapter_degraded_label"
            elif not line.human_label:
                disp = OperationalTruthDisposition.LOST
                loss = "mission_review_adapter_empty_label"
        nodes.append(
            OperationalTruthNode(
                truth_origin_step_id=origin,
                stage=OperationalTruthStage.MISSION_REVIEW,
                disposition=disp,
                truth_preserved=disp == OperationalTruthDisposition.PRESERVED,
                truth_loss_reason=loss,
                human_label=line.human_label,
                operational_kind=str(sp.get("type") if isinstance(sp, dict) else ""),
                canonical_truth=bool(coi and coi.canonical_truth),
                coi_intent_id=str(coi.intent_id if coi else ""),
            ),
        )
    return nodes


def audit_operational_truth_chain(mission: Mission) -> OperationalTruthChain:
    """Auditoría completa OTP; persiste resumen en la misión."""
    if mission.semantic_execution_plan:
        mission.semantic_execution_plan = _normalize_sep_blob(
            mission.semantic_execution_plan,
        )
    chain = OperationalTruthChain(last_audit_at=utc_now_iso())
    coi_nodes = build_coi_stage_nodes(mission)
    sep_nodes = build_sep_stage_nodes(mission)
    uol_nodes = build_uol_stage_nodes(mission)
    review_nodes = build_mission_review_stage_nodes(mission)

    record_stage_transform(
        chain,
        stage_in=OperationalTruthStage.COI,
        stage_out=OperationalTruthStage.SEP,
        nodes_in=coi_nodes,
        nodes_out=sep_nodes,
    )
    record_stage_transform(
        chain,
        stage_in=OperationalTruthStage.SEP,
        stage_out=OperationalTruthStage.UOL_COMPILER,
        nodes_in=sep_nodes,
        nodes_out=uol_nodes,
    )
    record_stage_transform(
        chain,
        stage_in=OperationalTruthStage.UOL_COMPILER,
        stage_out=OperationalTruthStage.MISSION_REVIEW,
        nodes_in=uol_nodes,
        nodes_out=review_nodes,
    )

    for n in review_nodes:
        if n.canonical_truth and n.truth_preserved and n.truth_origin_step_id:
            if n.truth_origin_step_id not in chain.preserved:
                chain.preserved.append(n.truth_origin_step_id)

    try:
        mission.operational_truth_chain = chain
    except Exception as exc:
        log.debug("[otp] persist chain: %s", exc)
    return chain


def _sep_step_to_dict(step: Any) -> Dict[str, Any]:
    if hasattr(step, "to_dict") and callable(getattr(step, "to_dict")):
        return step.to_dict()
    if hasattr(step, "model_dump"):
        return step.model_dump(mode="json")
    if isinstance(step, dict):
        return dict(step)
    return {}


def _normalize_sep_blob(sep: Any) -> Dict[str, Any]:
    """Garantiza que ``steps`` sean dicts JSON-serializables."""
    if not isinstance(sep, dict):
        return {}
    out = dict(sep)
    out["steps"] = [_sep_step_to_dict(s) for s in (out.get("steps") or [])]
    return out


def apply_otp_after_promotion(mission: Mission) -> OperationalTruthChain:
    """Hook post-SIPE/UCES: reconcilia SEP y audita cadena."""
    sep = _normalize_sep_blob(mission.semantic_execution_plan or {})
    raw_steps = sep.get("steps") or []
    if raw_steps:
        reconciled = reconcile_canonical_truths_on_sep_steps(mission, raw_steps)
        plan_dict = dict(sep)
        plan_dict["steps"] = [_sep_step_to_dict(s) for s in reconciled]
        mission.semantic_execution_plan = plan_dict
        try:
            from app.services.missions.semantic_execution_plan import (
                SemanticExecutionPlan,
                attach_intent_status_to_plan,
            )

            plan = SemanticExecutionPlan.from_dict(plan_dict)
            attach_intent_status_to_plan(plan, mission=mission)
            mission.semantic_execution_plan = plan.to_dict()
        except Exception as exc:
            log.debug("[otp] re-attach intent status: %s", exc)

    try:
        from app.services.missions.operational_continuity_reconstruction import (
            apply_ocrx_after_otp,
        )

        apply_ocrx_after_otp(mission)
    except Exception as ocrx_exc:
        log.debug("[otp] ocrx after promotion: %s", ocrx_exc)
    return audit_operational_truth_chain(mission)


def truth_preservation_expert_lines(mission: Mission) -> List[str]:
    """Líneas para Mission Review experto — sección Truth Preservation."""
    chain = mission.operational_truth_chain
    if chain is None:
        chain = audit_operational_truth_chain(mission)
    lines = [
        "Truth Preservation:",
        f"  preserved ({len(chain.preserved)}): "
        + (", ".join(chain.preserved[:8]) or "—"),
        f"  degraded ({len(chain.degraded)}): "
        + (", ".join(chain.degraded[:8]) or "—"),
        f"  lost ({len(chain.lost)}): " + (", ".join(chain.lost[:8]) or "—"),
        f"  transformed ({len(chain.transformed)}): "
        + (", ".join(chain.transformed[:8]) or "—"),
    ]
    if chain.loss_by_stage:
        lines.append("  loss_by_stage:")
        for stage, ids in chain.loss_by_stage.items():
            lines.append(f"    {stage}: {', '.join(ids[:6])}")
    for n in chain.nodes:
        if n.disposition in (
            OperationalTruthDisposition.LOST,
            OperationalTruthDisposition.DEGRADED,
        ) and n.truth_loss_reason:
            lines.append(
                f"  · {n.truth_origin_step_id} @ {n.stage.value}: {n.truth_loss_reason}",
            )
    return lines


def register_coi_on_mission(mission: Mission, coi: CanonicalOperationalIntent) -> None:
    """Registra COI en cadena OTP (etapa FFEC/COI) al promover freeze."""
    if not coi.canonical_truth:
        return
    chain = mission.operational_truth_chain
    if chain is None:
        chain = OperationalTruthChain(last_audit_at=utc_now_iso())
    origin = truth_origin_id_for_coi(coi)
    if any(
        n.truth_origin_step_id == origin and n.stage == OperationalTruthStage.COI
        for n in chain.nodes
    ):
        return
    chain.nodes.append(node_from_coi(coi, OperationalTruthStage.COI))
    try:
        mission.operational_truth_chain = chain
    except Exception:
        pass


__all__ = [
    "OperationalTruthChain",
    "OperationalTruthDisposition",
    "OperationalTruthNode",
    "OperationalTruthStage",
    "apply_otp_after_promotion",
    "audit_operational_truth_chain",
    "build_coi_stage_nodes",
    "build_mission_review_stage_nodes",
    "build_sep_stage_nodes",
    "build_uol_stage_nodes",
    "collect_canonical_cois",
    "enforce_canonical_on_semantic_step_dict",
    "node_from_coi",
    "node_from_semantic_step_dict",
    "reconcile_canonical_truths_on_sep_steps",
    "record_stage_transform",
    "register_coi_on_mission",
    "semantic_step_dict_from_coi",
    "truth_fingerprint",
    "truth_origin_id_for_coi",
    "truth_preservation_expert_lines",
]
