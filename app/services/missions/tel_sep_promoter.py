"""TEL → SEP Hybrid Promotion (Fase 2).

Compite contra SIPE/Ice sin borrar el pipeline viejo; audita y opcionalmente
promueve un SEP derivado de ``OperationalTruthBlock``.

Modos configuración: ``shadow`` | ``suggest`` | ``primary``.
"""
from __future__ import annotations

import urllib.parse as _urlparse
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Tuple

from app.contracts.mission import Mission, OperationalTruthBlock
from app.core.logger import log
from app.services.missions.semantic_ambiguity_engine import (
    aggregate_plan_ambiguity,
    evaluate_plan_semantic_ambiguity,
)
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    _strategies_for,
    attach_intent_status_to_plan,
    is_generic_profile_label,
)
from app.services.missions.truth_extraction_layer import extract_truth_blocks
from app.services.missions.uic_capability_registry import validate_step_against_registry


TelPromotionMode = Literal["shadow", "suggest", "primary"]

_FALLBACK_PLAN_REF = "semantic_execution_plan_current"

_MECHANICS_BANNED_IN_PRIMARY_STRATEGY: Tuple[str, ...] = (
    "uia_text_match",
    "smart_route",
    "ctrl_",
    "keypress",
    "mouse_scroll",
)

_MECHANICS_BANNED_STEP_TYPES: Tuple[str, ...] = (
    "uia_text_match",
    "smart_route",
    "dom_click",
    "keypress_enter",
    "mouse_scroll",
)


def _settings_get(settings_obj: Any, name: str, default: Any) -> Any:
    if settings_obj is None:
        return default
    return getattr(settings_obj, name, default)


def _truth_block_sort_key(tb: OperationalTruthBlock) -> str:
    return str((tb.temporal_signature or {}).get("started_at_iso") or tb.truth_id)


def _session_map(mission: Mission) -> Dict[str, Any]:
    return {s.id: s for s in (mission.semantic_sessions or []) if getattr(s, "id", None)}


def _hypothesis_map(mission: Mission) -> Dict[str, Any]:
    return {h.id: h for h in (mission.intent_timeline or []) if getattr(h, "id", None)}


def _merged_session_context(tb: OperationalTruthBlock, mission: Mission) -> Dict[str, Any]:
    sm = _session_map(mission)
    ctx: Dict[str, Any] = {}
    for sid in tb.absorbed_sessions or []:
        s = sm.get(sid)
        if s and isinstance(getattr(s, "context", None), dict):
            ctx.update(dict(s.context))
    return ctx


def _truth_derived_params(tb: OperationalTruthBlock) -> Dict[str, Any]:
    cw = tb.context_window or {}
    extras = cw.get("tel_derived_params")
    if isinstance(extras, dict):
        return dict(extras)
    return {}


def _hypothesis_carrier_fingerprint(tb: OperationalTruthBlock, mission: Mission) -> Set[str]:
    hm = _hypothesis_map(mission)
    acc: Set[str] = set()
    for hid in tb.absorbed_hypotheses or []:
        h = hm.get(hid)
        if not h:
            continue
        for c in getattr(h, "carriers", None) or []:
            acc.add(str(c).lower())
        acc.add(str(getattr(h, "candidate_intent_type", "") or "").lower())
    return acc


def _infer_provider_token_from_url(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    try:
        host = (_urlparse.urlparse(u).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        parts = host.split(".")
        if len(parts) >= 2:
            return parts[-2]
        return host
    except Exception:
        return ""


def _pick_sanitized_strategies(step_type: str) -> Tuple[str, List[str]]:
    """Elige estrategias evitando subcadenas de mecánica en primaria cuando hay alternativa."""
    sp = _strategies_for(step_type)
    cands = [sp.preferred, *tuple(sp.fallbacks)]
    def ok(s: str) -> bool:
        low = s.lower()
        return not any(b in low for b in _MECHANICS_BANNED_IN_PRIMARY_STRATEGY)

    primary = ""
    rests: List[str] = []
    for c in cands:
        if not c.strip():
            continue
        if not primary and ok(c):
            primary = c.strip()
            continue
        rests.append(c.strip())

    if not primary:
        primary = sp.preferred.strip() or "unknown"
    # dedupe fallbacks
    seen = {primary}
    fb_out: List[str] = []
    for r in rests + [c for c in cands if c.strip()]:
        if not r or r in seen:
            continue
        seen.add(r)
        fb_out.append(r)
    return primary, fb_out[:12]


def _map_select_entity_uces_step(
    tb: OperationalTruthBlock,
    mission: Mission,
    inj: Dict[str, Any],
    *,
    legacy_fallback: str,
) -> Optional[SemanticPlanStep]:
    """Mapea ``select_entity`` con contexto UCES a ``select_entity_from_collection``."""
    from app.services.runtime.universal_collection_entity_engine import (
        UNIVERSAL_SELECT_KIND,
        build_semantic_params_from_intent,
        human_label_for_uces_step,
        should_promote_legacy_to_uces,
    )
    from app.services.runtime.universal_collection_entity_engine import (
        EntitySelectionIntentResult,
    )
    from app.contracts.mission import (
        CollectionEntityReference,
        OperationalCollection,
        OperationalEntity,
    )

    coll_raw = inj.get("operational_collection") or inj.get("collection")
    ent_raw = inj.get("collection_entity") or inj.get("entity")
    if not coll_raw or not ent_raw:
        return None

    try:
        coll = OperationalCollection.model_validate(coll_raw)
        ent = CollectionEntityReference.model_validate(ent_raw)
    except Exception:
        return None

    op_ent = None
    if inj.get("operational_entity"):
        try:
            op_ent = OperationalEntity.model_validate(inj["operational_entity"])
        except Exception:
            op_ent = None

    ctx = dict(inj.get("selection_context") or {})
    intent = EntitySelectionIntentResult(
        selected=True,
        collection=coll,
        entity=ent,
        operational_entity=op_ent,
        confidence=float(
            ctx.get("selection_confidence")
            or inj.get("selection_confidence")
            or tb.operational_confidence
            or 0.8,
        ),
        candidate_count=int(ctx.get("candidate_count") or inj.get("candidate_count") or 1),
        needs_clarification=bool(ctx.get("needs_clarification")),
    )

    if not should_promote_legacy_to_uces(intent):
        return None

    params = build_semantic_params_from_intent(
        intent,
        legacy_type=legacy_fallback,
        selection_strategy="tel_sep_promoter",
    )
    preferred, fallbacks = _pick_sanitized_strategies(UNIVERSAL_SELECT_KIND)
    needs_tag = bool(intent.needs_clarification or intent.candidate_count > 1)

    return SemanticPlanStep(
        id=str(uuid.uuid4()),
        type=UNIVERSAL_SELECT_KIND,
        params=params,
        preferred_strategy=preferred,
        fallback_strategies=list(fallbacks),
        confidence=float(intent.confidence),
        human_label=human_label_for_uces_step(params),
        needs_user_label=needs_tag,
        label_prompt="Confirme el elemento seleccionado" if needs_tag else "",
        block_id=tb.truth_id,
    )


def map_truth_block_to_sep_step(
    tb: OperationalTruthBlock,
    mission: Mission,
    *,
    last_nav_url: Dict[str, str],
) -> Optional[SemanticPlanStep]:
    """Convierte un bloque operacional en un ``SemanticPlanStep``."""

    sess_ctx = _merged_session_context(tb, mission)
    inj = _truth_derived_params(tb)
    hypo_fp = _hypothesis_carrier_fingerprint(tb, mission)

    truth = tb.truth_type
    carriers = hypo_fp | {str(x).lower() for x in (tb.replay_anchor_candidates or [])}

    def _lid() -> str:
        return str(uuid.uuid4())

    if truth == "open_application":
        name = inj.get("name") or inj.get("app") or sess_ctx.get("launcher_query") or ""
        preferred, fallbacks = _pick_sanitized_strategies("open_app")
        return SemanticPlanStep(
            id=_lid(),
            type="open_app",
            params={
                "name": str(name)[:240] if name else "application",
                "method": inj.get("method") or sess_ctx.get("launch_method_hint") or "windows_search",
            },
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.75),
            human_label=str(tb.human_intent)[:300] or "Abrir aplicación",
            needs_user_label=False,
            block_id=tb.truth_id,
        )

    if truth == "select_entity":
        label = inj.get("label") or inj.get("profile_name") or inj.get(
            "selection_label",
        ) or sess_ctx.get("selection_label") or ""
        is_profileish = (
            "profile_pick" in carriers
            or "authentication" in carriers
            or bool(sess_ctx.get("profile_hint"))
            or bool(inj.get("force_profile"))
        )
        legacy_sep = "select_profile" if is_profileish else (
            "select_visible_option" if "semantic" not in hypo_fp else "select_profile"
        )

        uces_step = _map_select_entity_uces_step(
            tb, mission, inj, legacy_fallback=legacy_sep,
        )
        if uces_step is not None:
            return uces_step

        preferred, fallbacks = _pick_sanitized_strategies(legacy_sep)
        needs_tag = legacy_sep == "select_profile" and (
            tb.requires_human_confirmation
            or is_generic_profile_label(str(label))
            or not str(label).strip()
        )
        return SemanticPlanStep(
            id=_lid(),
            type=legacy_sep,
            params={
                "profile_name": "" if needs_tag else str(label)[:200],
                **({"label": str(label)[:240]} if legacy_sep != "select_profile" else {}),
            },
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.72),
            human_label=str(tb.human_intent)[:300] or "Seleccionar entidad",
            needs_user_label=bool(needs_tag),
            label_prompt="Confirme la opción seleccionada" if needs_tag else "",
            block_id=tb.truth_id,
        )

    if truth == "open_tab":
        preferred, fallbacks = _pick_sanitized_strategies("open_new_tab")
        return SemanticPlanStep(
            id=_lid(),
            type="open_new_tab",
            params={**({"app": inj.get("app")} if inj.get("app") else {})},
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.85),
            human_label=str(tb.human_intent)[:240] or "Nueva pestaña",
            needs_user_label=False,
            block_id=tb.truth_id,
        )

    if truth == "navigate_to_location":
        raw_url = inj.get("url") or sess_ctx.get("typed_url") or sess_ctx.get("navigation_url_hint") or ""
        preferred, fallbacks = _pick_sanitized_strategies("open_url")
        last_nav_url["url"] = str(raw_url)[:512]
        return SemanticPlanStep(
            id=_lid(),
            type="open_url",
            params={"url": str(raw_url)[:500]} if raw_url else {"alias": inj.get("alias", "")[:120]},
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.8),
            human_label=str(tb.human_intent)[:280] or "Navegar a destino",
            needs_user_label=not bool(str(raw_url or inj.get("alias") or "").strip()),
            label_prompt="Falta URL/nombre navegable reconocido",
            block_id=tb.truth_id,
        )

    if truth == "search_content":
        query = inj.get("query") or sess_ctx.get("search_query") or sess_ctx.get("query_preview") or ""
        site = inj.get("site") or inj.get("provider") or _infer_provider_token_from_url(
            last_nav_url.get("url", ""),
        )
        preferred, fallbacks = _pick_sanitized_strategies("search_content")
        return SemanticPlanStep(
            id=_lid(),
            type="search_content",
            params={
                "query": str(query)[:500],
                **({"provider": site} if site else {}),
                **({"site": site} if site else {}),
                **({"target": inj.get("target")} if inj.get("target") else {}),
            },
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.78),
            human_label=str(tb.human_intent)[:340] or "Buscar contenido",
            needs_user_label=not bool(str(query).strip()),
            label_prompt="Falta consulta estable",
            block_id=tb.truth_id,
        )

    if truth == "browse_results":
        preferred, fallbacks = _pick_sanitized_strategies("scroll_results")
        amt = inj.get("amount") or sess_ctx.get("scroll_count") or 1
        return SemanticPlanStep(
            id=_lid(),
            type="scroll_results",
            params={"amount": max(1, int(amt)), "direction": inj.get("direction") or "down"},
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.7),
            human_label=str(tb.human_intent)[:240] or "Explorar resultados",
            needs_user_label=False,
            block_id=tb.truth_id,
        )

    if truth == "fill_form":
        preferred, fallbacks = _pick_sanitized_strategies("fill_form")
        return SemanticPlanStep(
            id=_lid(),
            type="fill_form",
            params=dict(inj) if inj else {},
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.68),
            human_label=str(tb.human_intent)[:260] or "Rellenar formulario",
            needs_user_label=bool(tb.requires_human_confirmation),
            block_id=tb.truth_id,
        )

    if truth == "submit_form":
        preferred, fallbacks = _pick_sanitized_strategies("submit_form")
        return SemanticPlanStep(
            id=_lid(),
            type="submit_form",
            params=dict(inj) if inj else {},
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.66),
            human_label=str(tb.human_intent)[:240] or "Enviar formulario",
            needs_user_label=False,
            block_id=tb.truth_id,
        )

    if truth == "switch_context":
        preferred, fallbacks = _pick_sanitized_strategies("switch_context")
        return SemanticPlanStep(
            id=_lid(),
            type="switch_context",
            params={
                **dict(inj),
                "truth_hint": (tb.context_window or {}).get("bridge_from_cob_session", ""),
            },
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.55),
            human_label=str(tb.human_intent)[:260] or "Cambiar contexto",
            needs_user_label=False,
            block_id=tb.truth_id,
        )

    if truth == "confirm_dialog":
        preferred, fallbacks = _pick_sanitized_strategies("confirm_dialog")
        return SemanticPlanStep(
            id=_lid(),
            type="confirm_dialog",
            params=dict(inj),
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.6),
            human_label=str(tb.human_intent)[:260] or "Confirmar diálogo",
            needs_user_label=False,
            block_id=tb.truth_id,
        )

    if truth == "choose_option":
        preferred, fallbacks = _pick_sanitized_strategies("choose_option")
        return SemanticPlanStep(
            id=_lid(),
            type="choose_option",
            params={**inj, **({"choice_label": inj.get("label")} if inj.get("label") else {})},
            preferred_strategy=preferred,
            fallback_strategies=list(fallbacks),
            confidence=float(tb.operational_confidence or 0.66),
            human_label=str(tb.human_intent)[:260] or "Elegir opción",
            needs_user_label=not bool(str(inj.get("label") or "").strip()),
            block_id=tb.truth_id,
        )

    return None


def build_sep_from_truth_blocks(
    mission: Mission,
    truths: Sequence[OperationalTruthBlock],
) -> SemanticExecutionPlan:
    last_nav_url: Dict[str, str] = {}
    steps: List[SemanticPlanStep] = []
    for tb in sorted(truths, key=_truth_block_sort_key):
        st = map_truth_block_to_sep_step(tb, mission, last_nav_url=last_nav_url)
        if st is not None:
            steps.append(st)
    avg = sum(s.confidence for s in steps) / max(len(steps), 1) if steps else 0.0
    plan = SemanticExecutionPlan(
        steps=steps,
        source="tel_sep_promoter_candidate",
        version=1,
        average_confidence=avg,
        coords_used=False,
        legacy_graph_ignored=True,
    )
    try:
        from app.services.runtime.universal_collection_entity_engine import (
            apply_uces_primary_compilation,
        )
        apply_uces_primary_compilation(plan, mission)
    except Exception as exc:
        log.debug("[tel_sep] uces primary compilation: %s", exc)
    return attach_intent_status_to_plan(plan, mission=mission)


def _metrics_leakage_penalty(blob: Dict[str, Any]) -> float:
    steps = blob.get("steps") or []
    leaks = inspected = 0
    banned = ("uia_text_match", "smart_route", "dom_click", "keypress_enter", "mouse_scroll")
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        inspected += 1
        t = str(raw.get("type") or "").lower()
        ps = str(raw.get("preferred_strategy") or "").lower()
        lbl = str(raw.get("human_label") or "").lower()
        hay = ps + " " + t + " " + lbl
        if any(b in hay for b in banned) or any(b in t for b in banned):
            leaks += 1
    return round(leaks / max(inspected, 1), 4)


def compare_tel_sep_vs_existing_sep(
    tel_blob: Dict[str, Any],
    existing_blob: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    ex = existing_blob or {}
    tel_steps = [s for s in (tel_blob.get("steps") or []) if isinstance(s, dict)]
    ex_steps = [s for s in (ex.get("steps") or []) if isinstance(s, dict)]

    tel_types = [str(s.get("type") or "") for s in tel_steps]
    ex_types = [str(s.get("type") or "") for s in ex_steps]

    return {
        "tel_steps_count": len(tel_steps),
        "existing_sep_steps_count": len(ex_steps),
        "compression_ratio": round(len(ex_steps) / max(len(tel_steps), 1), 4),
        "tel_kind_sequence": tel_types,
        "sep_kind_sequence": ex_types,
        "mechanics_leakage_reduction": round(
            _metrics_leakage_penalty(ex) - _metrics_leakage_penalty(tel_blob),
            4,
        ),
        "tel_leak_penalty": _metrics_leakage_penalty(tel_blob),
        "sep_leak_penalty": _metrics_leakage_penalty(ex),
    }


def _types_compatible(a: str, b: str) -> bool:
    if a == b:
        return True
    group_profile = frozenset({
        "select_profile",
        "select_visible_option",
        "choose_option",
        "select_entity_from_collection",
    })
    if a in group_profile and b in group_profile:
        return True
    group_scroll = frozenset({"scroll_results", "scroll_page"})
    return a in group_scroll and b in group_scroll


def merge_tel_sep_with_existing_sep(
    tel_blob: Dict[str, Any],
    existing_blob: Dict[str, Any],
) -> Dict[str, Any]:
    """Alineación greedy: cada paso SEP busca primera TEL compatible no usada."""

    tel_steps = [dict(s) for s in (tel_blob.get("steps") or []) if isinstance(s, dict)]
    sep_steps = [dict(s) for s in (existing_blob.get("steps") or []) if isinstance(s, dict)]

    out: List[Dict[str, Any]] = []
    conflicts: List[str] = []
    sep_only_kept: List[int] = []
    rejected_truth_block_ids: List[str] = []
    used_tel: Set[int] = set()

    for qj, ss in enumerate(sep_steps):
        st = str(ss.get("type") or "")
        match_i: Optional[int] = None
        for qi, ts in enumerate(tel_steps):
            if qi in used_tel:
                continue
            tt = str(ts.get("type") or "")
            if _types_compatible(tt, st):
                match_i = qi
                break
        if match_i is None:
            out.append(dict(ss) | {"merged_from": "sep_kept_truth_gap"})
            sep_only_kept.append(qj)
            continue
        used_tel.add(match_i)
        ts = tel_steps[match_i]
        tt = str(ts.get("type") or "")
        if not _types_compatible(tt, st):
            conflicts.append(f"type_pair_unexpected qi={match_i} qj={qj}")
        merged = dict(ss)
        merged["human_label"] = ts.get("human_label") or merged.get("human_label")
        merged["preferred_strategy"] = ts.get("preferred_strategy") or merged.get("preferred_strategy")
        merged["fallback_strategies"] = ts.get("fallback_strategies") or merged.get(
            "fallback_strategies",
        )
        merged["tel_truth_block_id"] = ts.get("block_id")
        merged["merged_from"] = "tel+sep_compatible"
        out.append(merged)

    for qi, ts in enumerate(tel_steps):
        if qi in used_tel:
            continue
        out.append(dict(ts) | {"merged_from": "tel_only_candidate"})
        rejected_truth_block_ids.append(str(ts.get("block_id") or "") or ts.get("id") or "")

    return {
        "steps": out,
        "conflicts": conflicts,
        "sep_only_indices_kept": sep_only_kept,
        "rejected_truth_block_ids": [x for x in rejected_truth_block_ids if x],
    }


@dataclass
class TelSepValidationResult:
    ok: bool
    reasons: List[str]


def validate_tel_sep_ready(
    mission: Mission,
    tel_plan: SemanticExecutionPlan,
    *,
    settings: Any = None,
) -> TelSepValidationResult:
    reasons: List[str] = []
    forbidden_coord_keys = frozenset(
        {"bbox", "coords_xy", "fallback_coords", "absolute_xy", "coords"},
    )

    ambiguity_gate = bool(_settings_get(settings, "TEL_SEP_AMBIGUITY_GATE_ENABLED", True))

    agg_amb = aggregate_plan_ambiguity(
        evaluate_plan_semantic_ambiguity(tel_plan, mission),
    )
    if ambiguity_gate and agg_amb.requires_human_confirmation:
        reasons.append("SEMANTIC_AMBIGUITY_REQUIRES_HUMAN_CONFIRMATION")

    blob = tel_plan.to_dict()
    for i, raw in enumerate(blob.get("steps") or []):
        if not isinstance(raw, dict):
            continue
        t = str(raw.get("type") or "")
        low = str(t).lower()
        if low in _MECHANICS_BANNED_STEP_TYPES or any(b in low for b in ("uia_text_match",)):
            reasons.append(f"BLOCKED_ILLEGAL_STEP_TYPE[{i}:{t}]")

        ps = str(raw.get("preferred_strategy") or "").lower()
        lbl = str(raw.get("human_label") or "").lower()
        banned_hits = tuple(b for b in _MECHANICS_BANNED_IN_PRIMARY_STRATEGY if b in ps or b in lbl)
        if banned_hits and ("smart_route" in banned_hits or "uia_text_match" in banned_hits):
            reasons.append(f"PRIMARY_STRATEGY_MECHANICS_LEAK[{i}]")

        params = raw.get("params") or {}
        if isinstance(params, dict) and forbidden_coord_keys.intersection(set(params)):
            reasons.append(f"COORDS_PRIMARY_FORBIDDEN[{i}]")

        uic_err = validate_step_against_registry(raw | {"params": dict(params)})
        if uic_err:
            reasons.append(f"UIC::{uic_err}")

    ok = len(reasons) == 0
    return TelSepValidationResult(ok=ok, reasons=reasons[:40])


def decide_tel_promotion(
    *,
    mode: TelPromotionMode,
    enabled: bool,
    tel_plan: SemanticExecutionPlan,
    existing_blob: Optional[Dict[str, Any]],
    comparison: Dict[str, Any],
    validation: TelSepValidationResult,
    truth_blocks_used: int,
    min_confidence: float,
    require_ready: bool,
    max_truth_ambiguity: float,
    avg_truth_ambiguity: float,
    has_conflicts_in_merge_preview: bool,
) -> Dict[str, Any]:
    reasons: List[str] = []
    decision = "KEEP_EXISTING"
    promote = False

    if not enabled:
        reasons.append("TEL_SEP_PROMOTION_DISABLED")
        return {
            "decision": "KEEP_EXISTING",
            "promote": False,
            "reasons": reasons,
            "mode": mode,
        }

    if truth_blocks_used <= 0:
        reasons.append("NO_TRUTH_BLOCKS")
        return {
            "decision": decision,
            "promote": False,
            "reasons": reasons,
            "mode": mode,
        }

    if require_ready and validation.reasons:
        reasons.extend([f"READY_GATE::{x}" for x in validation.reasons[:12]])

    if avg_truth_ambiguity > max_truth_ambiguity:
        reasons.append(f"TRUTH_AMBIGUITY_TOO_HIGH<{avg_truth_ambiguity}>")

    if tel_plan.needs_user_label:
        reasons.append("TEL_SEP_NEEDS_USER_LABEL")

    if tel_plan.average_confidence < min_confidence:
        reasons.append(f"BELOW_MIN_CONFIDENCE<{tel_plan.average_confidence}>")

    tel_better = (
        comparison.get("tel_leak_penalty", 99) <= comparison.get("sep_leak_penalty", 99) - 0.0001
        or comparison.get("tel_steps_count", 999) <= max(comparison.get("existing_sep_steps_count", 0) - 1, 1)
    )
    gate_ok = not (
        tel_plan.needs_user_label
        or (require_ready and not validation.ok)
        or tel_plan.average_confidence < min_confidence
        or avg_truth_ambiguity > max_truth_ambiguity
    )

    if mode == "shadow":
        reasons.append("MODE_SHADOW_NO_WRITE")
        promote = False
        decision = "SHADOW_ANALYSIS_ONLY"
        if has_conflicts_in_merge_preview:
            reasons.append("MERGE_PREVIEW_CONFLICT")
            decision = "SHADOW_MERGE_PREVIEW_CONFLICT"
        return {"decision": decision, "promote": promote, "reasons": reasons, "mode": mode, "tel_better": tel_better}

    if mode == "suggest":
        promote = False
        if has_conflicts_in_merge_preview:
            reasons.append("MERGE_PREVIEW_CONFLICT")
            decision = "SUGGEST_CONFLICT_NO_AUTOREPLACE"
        else:
            decision = "SUGGEST_TEL_PRIMARY" if gate_ok and tel_better else "SUGGEST_WEAK_OR_BLOCK"
        reasons.append(
            "SUGGEST_MODE_NO_AUTOREPLACE — candidate stored in tel_semantic_execution_plan_candidate",
        )
        return {"decision": decision, "promote": promote, "reasons": reasons, "mode": mode, "tel_better": tel_better}

    # primary
    if has_conflicts_in_merge_preview:
        reasons.append("MERGE_PREVIEW_CONFLICT")
        promote = False
        decision = "CONFLICT_KEEP_SEP"
        return {"decision": decision, "promote": promote, "reasons": reasons, "mode": mode, "tel_better": tel_better}

    if gate_ok and tel_better:
        promote = True
        decision = "PROMOTE_TEL_PRIMARY"
    else:
        promote = False
        decision = "KEEP_EXISTING_PRIMARY_GATE_FAILED"

    return {"decision": decision, "promote": promote, "reasons": reasons, "mode": mode, "tel_better": tel_better}


def attach_tel_promotion_audit(mission: Mission, audit_blob: Dict[str, Any]) -> None:
    mission.tel_sep_promotion_audit = dict(audit_blob)


def attach_tel_promotion_decision(mission: Mission, decision_blob: Dict[str, Any]) -> None:
    mission.tel_sep_promotion_decision = dict(decision_blob)


def run_tel_sep_hybrid_promotion(mission: Mission, settings: Any = None) -> Dict[str, Any]:
    enabled = bool(_settings_get(settings, "TEL_SEP_PROMOTION_ENABLED", False))
    mode_raw = (_settings_get(settings, "TEL_SEP_PROMOTION_MODE", "shadow") or "shadow").lower()
    mode: TelPromotionMode = (
        mode_raw if mode_raw in {"shadow", "suggest", "primary"} else "shadow"
    )  # type: ignore[assignment]
    min_cf = float(_settings_get(settings, "TEL_SEP_PROMOTION_MIN_CONFIDENCE", 0.68))
    req_ready = bool(_settings_get(settings, "TEL_SEP_PROMOTION_REQUIRE_READY", True))
    max_truth_ambiguity = float(_settings_get(settings, "TEL_SEP_MAX_AMBIGUITY_AGGREGATE", 0.52))

    truth_src = getattr(mission, "truth_blocks", None) or []
    if not truth_src:
        truth_src = extract_truth_blocks(mission)

    truths_used_ids = [t.truth_id for t in truth_src]
    truths_rejected: List[str] = []

    if not truths_used_ids:
        audit = _empty_audit("NO_TRUTH_BLOCKS")
        audit["truth_blocks_used"] = 0
        audit["truth_blocks_rejected"] = []
        audit["fallback_plan_ref"] = _FALLBACK_PLAN_REF
        try:
            mission.tel_semantic_execution_plan_candidate = None
        except Exception:
            pass
        attach_tel_promotion_audit(mission, audit)
        dec = decide_tel_promotion(
            mode=mode,
            enabled=enabled,
            tel_plan=SemanticExecutionPlan(steps=[]),
            existing_blob=dict(mission.semantic_execution_plan or {}),
            comparison={
                "tel_steps_count": 0,
                "existing_sep_steps_count": len(
                    (mission.semantic_execution_plan or {}).get("steps") or [],
                ),
            },
            validation=TelSepValidationResult(ok=False, reasons=["NO_TRUTH_BLOCKS"]),
            truth_blocks_used=0,
            min_confidence=min_cf,
            require_ready=req_ready,
            max_truth_ambiguity=max_truth_ambiguity,
            avg_truth_ambiguity=99.0,
            has_conflicts_in_merge_preview=False,
        )
        attach_tel_promotion_decision(mission, dec)
        return audit

    tel_plan = build_sep_from_truth_blocks(mission, truth_src)
    tel_blob = tel_plan.to_dict()
    mission.tel_semantic_execution_plan_candidate = tel_blob

    existing_blob = dict(mission.semantic_execution_plan or {}) if isinstance(
        mission.semantic_execution_plan,
        dict,
    ) else {}

    cmp_res = compare_tel_sep_vs_existing_sep(tel_blob, existing_blob)
    merge_preview = merge_tel_sep_with_existing_sep(tel_blob, existing_blob)
    has_conflicts_in_merge_preview = bool(merge_preview.get("conflicts"))

    validation = validate_tel_sep_ready(mission, tel_plan, settings=settings)

    truths_conf = [
        float(t.ambiguity_score)
        for t in truth_src
        if getattr(t, "truth_id", None) not in truths_rejected
    ]
    avg_amb = sum(truths_conf) / max(len(truths_conf), 1)

    sep_steps_raw = existing_blob.get("steps") or []
    sep_avg_conf = 0.0
    if isinstance(sep_steps_raw, list) and sep_steps_raw:
        try:
            sep_avg_conf = sum(
                float(s.get("confidence") or 0.0)
                for s in sep_steps_raw
                if isinstance(s, dict)
            ) / max(len(sep_steps_raw), 1)
        except Exception:
            sep_avg_conf = 0.0

    audit: Dict[str, Any] = {
        "tel_steps_count": cmp_res["tel_steps_count"],
        "existing_sep_steps_count": cmp_res["existing_sep_steps_count"],
        "compression_ratio": cmp_res["compression_ratio"],
        "mechanics_leakage_reduction": cmp_res["mechanics_leakage_reduction"],
        "ambiguity_delta": round(
            float(existing_blob.get("average_confidence") or 0) - float(tel_plan.average_confidence),
            4,
        ),
        "readiness_delta": round(float(tel_plan.average_confidence) - float(sep_avg_conf), 4),
        "validator_coverage": "UIC_REGISTRY_PER_STEP_ATTEMPTED",
        "truth_blocks_used": len(truth_src),
        "truth_blocks_rejected": truths_rejected,
        "decision": "PENDING",
        "reasons": [],
        "fallback_plan_ref": _FALLBACK_PLAN_REF,
        "comparison": cmp_res,
        "merge_preview": merge_preview,
        "validation": validation.reasons[:24],
        "truth_block_ids_used": truths_used_ids,
        "truth_avg_ambiguity": round(avg_amb, 4),
    }

    dec = decide_tel_promotion(
        mode=mode,
        enabled=enabled,
        tel_plan=tel_plan,
        existing_blob=existing_blob,
        comparison=cmp_res,
        validation=validation,
        truth_blocks_used=len(truth_src),
        min_confidence=min_cf,
        require_ready=req_ready,
        max_truth_ambiguity=max_truth_ambiguity,
        avg_truth_ambiguity=avg_amb,
        has_conflicts_in_merge_preview=has_conflicts_in_merge_preview,
    )

    audit["decision"] = dec.get("decision")
    audit["reasons"] = dec.get("reasons", []) + validation.reasons[:8]

    attach_tel_promotion_audit(mission, audit)
    attach_tel_promotion_decision(mission, dec)

    if enabled and mode == "primary" and bool(dec.get("promote")) and not has_conflicts_in_merge_preview:
        tel_plan.source = "tel_sep_promoter_primary"
        tel_plan.version = max(int(existing_blob.get("version") or 1), 1)
        tel_plan.coords_used = False
        tel_plan.legacy_graph_ignored = True
        attach_intent_status_to_plan(tel_plan, mission=mission)
        mission.semantic_execution_plan = tel_plan.to_dict()
        try:
            mission.legacy_compiled_graph_purpose = "legacy_debug_only"
        except Exception:
            pass
        log.info("[TEL→SEP] primary promotion applied — SEP now sourced from TEL-derived plan.")

    return audit


def _empty_audit(note: str) -> Dict[str, Any]:
    return {
        "tel_steps_count": 0,
        "existing_sep_steps_count": 0,
        "compression_ratio": 0.0,
        "mechanics_leakage_reduction": 0.0,
        "ambiguity_delta": 0.0,
        "readiness_delta": 0.0,
        "validator_coverage": "N/A",
        "truth_blocks_used": 0,
        "truth_blocks_rejected": [],
        "decision": note,
        "reasons": [note],
        "fallback_plan_ref": _FALLBACK_PLAN_REF,
    }


__all__ = [
    "map_truth_block_to_sep_step",
    "build_sep_from_truth_blocks",
    "merge_tel_sep_with_existing_sep",
    "compare_tel_sep_vs_existing_sep",
    "decide_tel_promotion",
    "attach_tel_promotion_audit",
    "validate_tel_sep_ready",
    "run_tel_sep_hybrid_promotion",
    "TelSepValidationResult",
]
