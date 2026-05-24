"""Operational truth lineage — materialización y Mission Review desde verdad preservada.

Construye la vista final solo desde:
  canonical_operational_intents → OTP → OCRX → SEP → UOL

Sin listas literales de pasos esperados ni hardcodes de producto/perfil.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    CanonicalOperationalIntent,
    Mission,
    OperationalTruthStage,
    RawEvent,
)
from app.core.logger import log

READY_BLOCKER_CANONICAL_NOT_MATERIALIZED = "CANONICAL_TRUTH_NOT_MATERIALIZED"

_ENTITY_SELECT_SEP_TYPES = frozenset({
    "select_profile",
    "select_entity_from_collection",
    "select_item",
    "use_selected_profile",
})


class OrphanCanonicalTruthError(RuntimeError):
    """COI canónico sin materialización en SEP/UOL."""

    def __init__(self, orphans: Sequence["OrphanCanonicalTruth"]) -> None:
        self.orphans = list(orphans)
        detail = "; ".join(
            f"{o.coi_intent_id} lost@{o.loss_stage or 'unknown'}" for o in self.orphans[:6]
        )
        super().__init__(f"canonical_truth orphan(s): {detail}")


@dataclass
class OrphanCanonicalTruth:
    coi_intent_id: str
    truth_origin_step_id: str
    source_event_id: str
    loss_stage: str
    uol_action: str
    human_label_from_uol: str


@dataclass
class OperationalStepLineage:
    """Trazabilidad de un paso visible (o COI huérfano)."""

    sep_step_id: str = ""
    human_label: str = ""
    visible_in_review: bool = False
    raw_event_id: str = ""
    coi_intent_id: str = ""
    truth_origin_step_id: str = ""
    otp_stage: str = ""
    otp_disposition: str = ""
    ocrx_thread_id: str = ""
    sep_step_type: str = ""
    uol_action: str = ""
    lineage_complete: bool = False
    missing_links: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sep_step_id": self.sep_step_id,
            "human_label": self.human_label,
            "visible_in_review": self.visible_in_review,
            "raw_event_id": self.raw_event_id,
            "coi_intent_id": self.coi_intent_id,
            "truth_origin_step_id": self.truth_origin_step_id,
            "otp_stage": self.otp_stage,
            "otp_disposition": self.otp_disposition,
            "ocrx_thread_id": self.ocrx_thread_id,
            "sep_step_type": self.sep_step_type,
            "uol_action": self.uol_action,
            "lineage_complete": self.lineage_complete,
            "missing_links": list(self.missing_links),
        }


@dataclass
class MaterializationReport:
    materialized_count: int = 0
    enriched_count: int = 0
    merged_step_count: int = 0


@dataclass
class PreservedTruthReviewStep:
    index: int
    human_label: str
    sep_step_id: str
    lineage: OperationalStepLineage
    needs_review: bool = False


@dataclass
class PreservedTruthReview:
    steps: List[PreservedTruthReviewStep] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    intent_status: str = ""
    status_ready: bool = False
    orphans: List[OrphanCanonicalTruth] = field(default_factory=list)
    lineages: List[OperationalStepLineage] = field(default_factory=list)


def _event_ts_ms(ev: RawEvent) -> float:
    ts = getattr(ev, "timestamp", None)
    try:
        return float(ts.timestamp() * 1000) if ts else float(getattr(ev, "offset_ms", 0) or 0)
    except Exception:
        return float(getattr(ev, "offset_ms", 0) or 0)


def _event_ms_for_id(mission: Mission, event_id: str) -> float:
    if not event_id:
        return float("inf")
    for ev in mission.raw_trace or []:
        if str(ev.id) == str(event_id):
            return _event_ts_ms(ev)
    return float("inf")


def human_label_from_coi(coi: CanonicalOperationalIntent) -> str:
    """Etiqueta humana solo desde UOL (acción universal), nunca strings esperados."""
    from app.services.missions.universal_operational_language import (
        compile_canonical_intent_to_uol,
        contains_technical_label_noise,
        human_operational_label_for_uol,
    )

    action = compile_canonical_intent_to_uol(coi)
    if action is None:
        return ""
    label = human_operational_label_for_uol(action)
    if contains_technical_label_noise(label):
        return ""
    return label


def uol_action_for_coi(coi: CanonicalOperationalIntent) -> str:
    from app.services.missions.universal_operational_language import (
        compile_canonical_intent_to_uol,
    )

    action = compile_canonical_intent_to_uol(coi)
    return action.uol_action.value if action else ""


def sep_step_dict_from_coi_uol(
    coi: CanonicalOperationalIntent,
    *,
    mission: Mission,
) -> Dict[str, Any]:
    """Materializa un paso SEP desde COI vía UOL (sin texto esperado externo)."""
    from app.services.missions.operational_truth_preservation import truth_origin_id_for_coi
    from app.services.missions.universal_operational_language import (
        compile_canonical_intent_to_uol,
    )
    from app.services.runtime.universal_collection_entity_engine import (
        UCES_PRIMARY_STRATEGY,
        UNIVERSAL_SELECT_KIND,
    )

    action = compile_canonical_intent_to_uol(coi)
    uol_type = action.uol_action.value if action else UNIVERSAL_SELECT_KIND
    sep_type = _uol_action_to_sep_type(uol_type)

    params: Dict[str, Any] = {
        "canonical_truth": True,
        "uol_canonical_truth": True,
        "canonical_operational_intent": coi.model_dump(mode="json"),
        "source_event_id": str(coi.source_event_id or ""),
        "uol_action": uol_type,
        "selection_confidence": float(coi.truth_strength or 0.0),
        "candidate_count": 1,
        "pre_transition_confirmed": bool(coi.pre_transition_confirmed),
        "materialized_from": "canonical_operational_intent",
    }
    if action:
        params.update(action.to_step_params())
    if coi.entity:
        params["collection_entity"] = coi.entity.model_dump(mode="json")
        params["selection_label"] = coi.entity.display_name
    if coi.collection:
        params["operational_collection"] = coi.collection.model_dump(mode="json")

    origin = truth_origin_id_for_coi(coi)
    return {
        "id": f"lineage:{origin}",
        "type": sep_type,
        "params": params,
        "preferred_strategy": UCES_PRIMARY_STRATEGY,
        "fallback_strategies": ["select_entity:operational_entity_match"],
        "confidence": float(coi.truth_strength or coi.freeze_confidence or 0.0),
        "human_label": human_label_from_coi(coi),
        "needs_user_label": False,
        "lineage_truth_origin": origin,
    }


def _uol_action_to_sep_type(uol_action: str) -> str:
    from app.services.missions.universal_operational_language import (
        UniversalOperationalActionType,
    )

    mapping = {
        UniversalOperationalActionType.OPEN_APPLICATION.value: "open_app",
        UniversalOperationalActionType.OPEN_TAB.value: "open_new_tab",
        UniversalOperationalActionType.NAVIGATE_TO_LOCATION.value: "open_site",
        UniversalOperationalActionType.SEARCH_CONTENT.value: "search_site",
        UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION.value: (
            "select_entity_from_collection"
        ),
        UniversalOperationalActionType.SCROLL_RESULTS.value: "scroll_results",
    }
    return mapping.get(uol_action, "select_entity_from_collection")


def _sep_step_to_dict(step: Any) -> Dict[str, Any]:
    from app.services.missions.operational_truth_preservation import _sep_step_to_dict as _otp_dict

    return _otp_dict(step)


def _normalize_sep(mission: Mission) -> Dict[str, Any]:
    from app.services.missions.operational_truth_preservation import _normalize_sep_blob

    blob = _normalize_sep_blob(mission.semantic_execution_plan or {})
    mission.semantic_execution_plan = blob
    return blob


def _coi_linked_in_sep(coi: CanonicalOperationalIntent, step: Dict[str, Any]) -> bool:
    from app.services.missions.operational_truth_preservation import _coi_covers_step

    if _coi_covers_step(coi, step):
        return True
    params = step.get("params") or {}
    if str(coi.intent_id or "") and str(params.get("coi_intent_id") or "") == str(coi.intent_id):
        return True
    eid = str(coi.source_event_id or "")
    if eid and str(params.get("source_event_id") or step.get("source_event_id") or "") == eid:
        return True
    coi_raw = params.get("canonical_operational_intent")
    if isinstance(coi_raw, dict) and str(coi_raw.get("intent_id") or "") == str(coi.intent_id):
        return True
    return False


def _step_trace_anchor_ms(mission: Mission, step: Dict[str, Any], *, fallback_index: int) -> float:
    params = step.get("params") or {}
    eid = str(
        params.get("source_event_id")
        or step.get("source_event_id")
        or "",
    )
    if eid:
        ms = _event_ms_for_id(mission, eid)
        if ms < float("inf"):
            return ms
    return 1e15 + float(fallback_index)


def _merge_steps_by_trace_order(
    mission: Mission,
    steps: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    indexed = [
        (i, s, _step_trace_anchor_ms(mission, s, fallback_index=i))
        for i, s in enumerate(steps)
    ]
    indexed.sort(key=lambda row: (row[2], row[0]))
    return [s for _, s, _ in indexed]


def _otp_node_for_origin(mission: Mission, origin: str) -> Optional[Any]:
    chain = mission.operational_truth_chain
    if chain is None:
        return None
    for n in chain.nodes or []:
        if str(getattr(n, "truth_origin_step_id", "")) == origin:
            return n
    return None


def _ocrx_thread_for_coi(mission: Mission, coi: CanonicalOperationalIntent) -> Optional[Any]:
    ch = mission.operational_continuity_chain
    if ch is None:
        return None
    cid = str(coi.intent_id or "")
    for th in ch.threads or []:
        if str(getattr(th, "coi_intent_id", "")) == cid:
            return th
    return None


def _loss_stage_for_origin(mission: Mission, origin: str) -> str:
    chain = mission.operational_truth_chain
    if chain is None:
        return ""
    for stage, ids in (chain.loss_by_stage or {}).items():
        if origin in ids:
            return str(stage)
    if origin in (chain.lost or []):
        return "otp_or_sep"
    return ""


def materialize_missing_canonical_steps(mission: Mission) -> MaterializationReport:
    """Materializa COI canónicos ausentes en SEP; enriquece los existentes."""
    from app.services.missions.operational_truth_preservation import (
        collect_canonical_cois,
        enforce_canonical_on_semantic_step_dict,
    )

    report = MaterializationReport()
    cois = [c for c in collect_canonical_cois(mission) if c.canonical_truth]
    if not cois:
        return report

    sep = _normalize_sep(mission)
    dict_steps: List[Dict[str, Any]] = []
    for sp in sep.get("steps") or []:
        d = _sep_step_to_dict(sp)
        if d:
            dict_steps.append(d)

    for coi in cois:
        matched_idx: Optional[int] = None
        for i, sp in enumerate(dict_steps):
            if _coi_linked_in_sep(coi, sp):
                dict_steps[i] = enforce_canonical_on_semantic_step_dict(sp, coi)
                hl = human_label_from_coi(coi)
                if hl:
                    dict_steps[i]["human_label"] = hl
                pars = dict(dict_steps[i].get("params") or {})
                pars["uol_action"] = uol_action_for_coi(coi)
                pars["materialized_from"] = pars.get("materialized_from") or "coi_enrich"
                dict_steps[i]["params"] = pars
                dict_steps[i]["needs_user_label"] = False
                matched_idx = i
                report.enriched_count += 1
                break
        if matched_idx is None:
            dict_steps.append(sep_step_dict_from_coi_uol(coi, mission=mission))
            report.materialized_count += 1

    merged = _merge_steps_by_trace_order(mission, dict_steps)
    report.merged_step_count = len(merged)
    sep["steps"] = merged
    mission.semantic_execution_plan = sep
    return report


def assert_no_orphan_canonical_truth(
    mission: Mission,
    *,
    raise_on_orphan: bool = False,
) -> List[OrphanCanonicalTruth]:
    """Falla si hay COI canónico sin presencia en SEP/UOL."""
    from app.services.missions.operational_truth_preservation import (
        collect_canonical_cois,
        truth_origin_id_for_coi,
    )

    orphans: List[OrphanCanonicalTruth] = []
    sep = mission.semantic_execution_plan or {}
    steps = list(sep.get("steps") or [])

    for coi in collect_canonical_cois(mission):
        if not coi.canonical_truth:
            continue
        present = any(_coi_linked_in_sep(coi, sp) for sp in steps if isinstance(sp, dict))
        if present:
            continue
        origin = truth_origin_id_for_coi(coi)
        orphans.append(
            OrphanCanonicalTruth(
                coi_intent_id=str(coi.intent_id or ""),
                truth_origin_step_id=origin,
                source_event_id=str(coi.source_event_id or ""),
                loss_stage=_loss_stage_for_origin(mission, origin),
                uol_action=uol_action_for_coi(coi),
                human_label_from_uol=human_label_from_coi(coi),
            ),
        )

    try:
        mission._canonical_truth_orphans = [o.__dict__ for o in orphans]
    except Exception:
        pass

    if raise_on_orphan and orphans:
        raise OrphanCanonicalTruthError(orphans)
    return orphans


def trace_operational_step_lineage(mission: Mission) -> List[OperationalStepLineage]:
    """Trazabilidad raw_event → COI → OTP → OCRX → SEP → UOL por paso."""
    from app.services.missions.operational_truth_preservation import (
        collect_canonical_cois,
        truth_origin_id_for_coi,
    )
    from app.services.missions.universal_operational_language import (
        compile_semantic_step_to_uol,
        human_operational_label_for_uol,
    )

    lineages: List[OperationalStepLineage] = []
    sep_steps = list((mission.semantic_execution_plan or {}).get("steps") or [])
    cois = collect_canonical_cois(mission)
    coi_by_intent = {str(c.intent_id): c for c in cois if c.intent_id}
    coi_by_eid = {str(c.source_event_id): c for c in cois if c.source_event_id}

    for sp in sep_steps:
        if not isinstance(sp, dict):
            continue
        params = sp.get("params") or {}
        eid = str(params.get("source_event_id") or sp.get("source_event_id") or "")
        coi: Optional[CanonicalOperationalIntent] = None
        coi_raw = params.get("canonical_operational_intent")
        if isinstance(coi_raw, dict):
            try:
                coi = CanonicalOperationalIntent.model_validate(coi_raw)
            except Exception:
                coi = None
        if coi is None and eid:
            coi = coi_by_eid.get(eid)
        origin = ""
        if coi:
            origin = truth_origin_id_for_coi(coi)

        otp_node = _otp_node_for_origin(mission, origin) if origin else None
        ocrx_th = _ocrx_thread_for_coi(mission, coi) if coi else None

        uol_action = str(params.get("uol_action") or "")
        hl = str(sp.get("human_label") or "")
        if not uol_action or not hl:
            action = compile_semantic_step_to_uol(sp, coi=coi)
            if action:
                uol_action = uol_action or action.uol_action.value
                hl = hl or human_operational_label_for_uol(action)

        missing: List[str] = []
        if eid and _event_ms_for_id(mission, eid) >= float("inf"):
            missing.append("raw_event")
        if coi is None and params.get("canonical_truth"):
            missing.append("coi")
        if origin and otp_node is None:
            missing.append("otp_node")
        if coi and ocrx_th is None:
            missing.append("ocrx_thread")

        lineage = OperationalStepLineage(
            sep_step_id=str(sp.get("id") or ""),
            human_label=hl,
            visible_in_review=bool(hl),
            raw_event_id=eid,
            coi_intent_id=str(coi.intent_id if coi else params.get("coi_intent_id") or ""),
            truth_origin_step_id=origin,
            otp_stage=str(getattr(otp_node, "stage", OperationalTruthStage.COI).value if otp_node else ""),
            otp_disposition=str(getattr(otp_node, "disposition", "") or "") if otp_node else "",
            ocrx_thread_id=str(getattr(ocrx_th, "thread_id", "") or "") if ocrx_th else "",
            sep_step_type=str(sp.get("type") or ""),
            uol_action=uol_action,
            lineage_complete=bool(
                hl and sp.get("id") and (coi is None or (origin and otp_node and ocrx_th))
            ),
            missing_links=[m for m in missing if m],
        )
        lineages.append(lineage)

    linked_origins = {ln.truth_origin_step_id for ln in lineages if ln.truth_origin_step_id}
    for coi in cois:
        if not coi.canonical_truth:
            continue
        origin = truth_origin_id_for_coi(coi)
        if origin in linked_origins:
            continue
        otp_node = _otp_node_for_origin(mission, origin)
        ocrx_th = _ocrx_thread_for_coi(mission, coi)
        lineages.append(
            OperationalStepLineage(
                sep_step_id="",
                human_label=human_label_from_coi(coi),
                visible_in_review=False,
                raw_event_id=str(coi.source_event_id or ""),
                coi_intent_id=str(coi.intent_id or ""),
                truth_origin_step_id=origin,
                otp_stage=str(getattr(otp_node, "stage", OperationalTruthStage.COI).value if otp_node else ""),
                otp_disposition=str(getattr(otp_node, "disposition", "") or "") if otp_node else "",
                ocrx_thread_id=str(getattr(ocrx_th, "thread_id", "") or "") if ocrx_th else "",
                sep_step_type="",
                uol_action=uol_action_for_coi(coi),
                lineage_complete=False,
                missing_links=["sep_step", "uol_visible"],
            ),
        )

    try:
        mission._operational_step_lineages = [ln.to_dict() for ln in lineages]
    except Exception:
        pass
    return lineages


def ensure_preserved_truth_pipeline(mission: Mission) -> MaterializationReport:
    """OTP + OCRX + materialización + auditoría (sin ocultar blockers)."""
    from app.services.missions.operational_continuity_reconstruction import (
        annotate_sep_with_threads,
        audit_operational_continuity,
    )
    from app.services.missions.operational_truth_preservation import (
        audit_operational_truth_chain,
    )

    try:
        from app.services.missions.operational_truth_preservation import (
            reconcile_canonical_truths_on_sep_steps,
        )

        sep = _normalize_sep(mission)
        steps = sep.get("steps") or []
        if steps:
            reconciled = reconcile_canonical_truths_on_sep_steps(mission, steps)
            sep["steps"] = [_sep_step_to_dict(s) for s in reconciled]
            mission.semantic_execution_plan = sep
    except Exception as exc:
        log.debug("[lineage] otp reconcile: %s", exc)

    audit_operational_truth_chain(mission)
    audit_operational_continuity(mission)

    report = materialize_missing_canonical_steps(mission)
    annotate_sep_with_threads(mission)
    trace_operational_step_lineage(mission)
    assert_no_orphan_canonical_truth(mission, raise_on_orphan=False)

    try:
        from app.services.missions.semantic_execution_plan import (
            SemanticExecutionPlan,
            attach_intent_status_to_plan,
        )

        blob = mission.semantic_execution_plan or {}
        if blob.get("steps"):
            plan = SemanticExecutionPlan.from_dict(blob)
            orphans = assert_no_orphan_canonical_truth(mission)
            if orphans:
                plan.not_ready_reasons = list(plan.not_ready_reasons or [])
                if READY_BLOCKER_CANONICAL_NOT_MATERIALIZED not in plan.not_ready_reasons:
                    plan.not_ready_reasons.append(READY_BLOCKER_CANONICAL_NOT_MATERIALIZED)
                plan.intent_status = "NEEDS_REVIEW"
            attach_intent_status_to_plan(plan, mission=mission)
            orphans_after = assert_no_orphan_canonical_truth(mission)
            if orphans_after:
                plan.not_ready_reasons = list(
                    dict.fromkeys(
                        list(plan.not_ready_reasons or [])
                        + [READY_BLOCKER_CANONICAL_NOT_MATERIALIZED],
                    ),
                )
                plan.intent_status = "NEEDS_REVIEW"
            mission.semantic_execution_plan = plan.to_dict()
    except Exception as exc:
        log.debug("[lineage] attach intent status: %s", exc)

    return report


def compute_review_from_preserved_truth(mission: Mission) -> PreservedTruthReview:
    """Mission Review normal desde SEP materializado + lineage (no legacy graph)."""
    from app.services.missions.universal_operational_language import (
        human_operational_label_for_review_step,
        should_suppress_ask_for_mission_step,
    )
    from app.services.runtime.entity_resolution_layer import (
        sanitize_human_label_for_normal_view,
    )

    ensure_preserved_truth_pipeline(mission)

    sep = mission.semantic_execution_plan or {}
    intent_status = str(sep.get("intent_status") or "")
    blockers = list(sep.get("not_ready_reasons") or [])
    orphans = assert_no_orphan_canonical_truth(mission)

    if orphans and READY_BLOCKER_CANONICAL_NOT_MATERIALIZED not in blockers:
        blockers.append(READY_BLOCKER_CANONICAL_NOT_MATERIALIZED)

    lineages = trace_operational_step_lineage(mission)
    lineage_by_step = {ln.sep_step_id: ln for ln in lineages if ln.sep_step_id}

    steps_out: List[PreservedTruthReviewStep] = []
    for i, sp in enumerate(sep.get("steps") or []):
        if not isinstance(sp, dict):
            continue
        sid = str(sp.get("id") or "")
        ln = lineage_by_step.get(sid)
        hl = str(sp.get("human_label") or "")
        try:
            uol_hl = human_operational_label_for_review_step(sp, mission)
            if uol_hl:
                hl = uol_hl
        except Exception:
            pass
        if not hl and ln:
            hl = ln.human_label
        hl = sanitize_human_label_for_normal_view(hl)
        if not hl:
            continue

        needs_human = bool(sp.get("needs_user_label"))
        try:
            if should_suppress_ask_for_mission_step(sp, mission):
                needs_human = False
        except Exception:
            pass
        if ln and ln.coi_intent_id and ln.lineage_complete:
            needs_human = False

        steps_out.append(
            PreservedTruthReviewStep(
                index=len(steps_out) + 1,
                human_label=hl,
                sep_step_id=sid,
                lineage=ln or OperationalStepLineage(
                    sep_step_id=sid,
                    human_label=hl,
                    visible_in_review=True,
                    sep_step_type=str(sp.get("type") or ""),
                ),
                needs_review=needs_human,
            ),
        )

    status_ready = intent_status in ("READY", "EXECUTABLE") and not orphans

    return PreservedTruthReview(
        steps=steps_out,
        blockers=blockers,
        intent_status=intent_status,
        status_ready=status_ready,
        orphans=orphans,
        lineages=lineages,
    )


def lineage_expert_lines(mission: Mission) -> List[str]:
    lineages = trace_operational_step_lineage(mission)
    lines = ["Operational step lineage:"]
    for ln in lineages[:12]:
        flag = "visible" if ln.visible_in_review else "orphan"
        lines.append(
            f"  [{flag}] {ln.sep_step_id or '—'}: "
            f"{ln.raw_event_id or '—'} → COI {ln.coi_intent_id or '—'} → "
            f"OTP@{ln.otp_stage or '—'} → OCRX {ln.ocrx_thread_id or '—'} → "
            f"SEP/{ln.sep_step_type} → UOL {ln.uol_action or '—'} "
            f"→ «{ln.human_label}»",
        )
        if ln.missing_links:
            lines.append(f"      missing: {', '.join(ln.missing_links)}")
    return lines


__all__ = [
    "MaterializationReport",
    "OperationalStepLineage",
    "OrphanCanonicalTruth",
    "OrphanCanonicalTruthError",
    "PreservedTruthReview",
    "PreservedTruthReviewStep",
    "READY_BLOCKER_CANONICAL_NOT_MATERIALIZED",
    "assert_no_orphan_canonical_truth",
    "compute_review_from_preserved_truth",
    "ensure_preserved_truth_pipeline",
    "human_label_from_coi",
    "lineage_expert_lines",
    "materialize_missing_canonical_steps",
    "sep_step_dict_from_coi_uol",
    "trace_operational_step_lineage",
    "uol_action_for_coi",
]
