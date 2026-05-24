"""
Entity Resolution Layer (ERL) — Fase 1
----------------------------------------
Promueve targets capturados a **entidades operacionales persistentes**
(perfiles, búsquedas, archivos, tickets, etc.) sin hardcode por app.

- No reemplaza target identity, execution truth, SEP, OMPC, TEL, COB ni ARL.
- Persistencia local: ``var/entity_memory/entity_memory_v1.sqlite``.
- API: extracción, normalización, matching, confianza y resolución en runtime.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    OperationalEntity,
    OperationalEntityEvidence,
    OperationalEntityType,
    RawEvent,
)
from app.core.logger import log
from app.core.paths import VAR_DIR

ENTITY_MEMORY_VERSION = 1
_ENTITY_DIR = VAR_DIR / "entity_memory"
_ENTITY_DB = _ENTITY_DIR / f"entity_memory_v{ENTITY_MEMORY_VERSION}.sqlite"

# Umbrales estructurales (no app-specific)
PROMOTION_IDENTITY_CONFIDENCE_MIN = 0.72
PROMOTION_ENTITY_CONFIDENCE_MIN = 0.78
PROMOTION_EVIDENCE_RICHNESS_MIN = 0.28
FUZZY_MATCH_THRESHOLD = 0.82
OCR_FUZZY_MATCH_THRESHOLD = 0.76
STRONG_ENTITY_CONFIDENCE_MIN = 0.88

# Tokens de etiqueta vacía/genérica — rechazo universal (no nombres de apps).
_GENERIC_ENTITY_TOKENS: frozenset = frozenset({
    "ok", "okay", "cancel", "cancelar", "aceptar", "accept", "close", "cerrar",
    "button", "boton", "btn", "item", "element", "elemento", "select",
    "seleccionar", "choose", "elegir", "profile", "perfil", "result", "resultado",
    "search", "buscar", "busqueda", "tab", "pestaña", "link", "enlace",
    "menu", "option", "opcion", "unknown", "default", "user", "usuario",
    "groupcontrol", "panecontrol", "customcontrol", "documentcontrol",
    "seleccionar perfil", "select profile", "select a profile",
    "browser profile", "chrome profile", "perfil del navegador",
})

# Substrings de runtime que no deben mostrarse en vista normal Mission Review.
EXPERT_ONLY_RESOLUTION_MARKERS: Tuple[str, ...] = (
    "uia_text_match",
    "target_source",
    "replay strategy",
    "replay_strategy",
    "ctrl+l",
    "smart_route",
    "visible_text_ocr",
    "chrome_profile_alias",
    "coords_fallback",
    "skip_if_loaded",
)


@dataclass
class EntityMatchResult:
    """Resultado de ``match_entity_against_runtime``."""

    matched: bool = False
    strategy: str = ""
    confidence: float = 0.0
    candidate_label: str = ""
    runtime_target: Optional[Dict[str, Any]] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class EntityResolutionRuntimeResult:
    """Resultado de ``resolve_entity_for_runtime``."""

    resolved: bool = False
    entity: Optional[OperationalEntity] = None
    strategy: str = ""
    confidence: float = 0.0
    used_legacy_fallback: bool = False
    notes: List[str] = field(default_factory=list)


# ── Normalización ───────────────────────────────────────────────────────


def normalize_entity_name(display_name: str) -> str:
    """Normaliza nombre visible → clave estable (snake_case ASCII)."""
    if not display_name:
        return ""
    text = unicodedata.normalize("NFKD", str(display_name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^\w\s@.\-]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"[@.]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(" ", "_")
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:240]


def is_generic_entity_label(label: Optional[str]) -> bool:
    """¿La etiqueta es demasiado genérica para promover entidad?"""
    if not label:
        return True
    norm = " ".join(str(label).strip().lower().split())
    if not norm or len(norm) < 2:
        return True
    if norm in _GENERIC_ENTITY_TOKENS:
        return True
    if len(norm) <= 12 and norm in {"perfil", "profile", "result", "item", "tab"}:
        return True
    # Una sola palabra corta sin dígitos ni mayúsculas mixtas significativas.
    tokens = norm.split()
    if len(tokens) == 1 and len(tokens[0]) <= 6 and not any(c.isdigit() for c in tokens[0]):
        if tokens[0] in _GENERIC_ENTITY_TOKENS:
            return True
    return False


def _text_similarity(a: str, b: str) -> float:
    na, nb = normalize_entity_name(a), normalize_entity_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return float(SequenceMatcher(None, na, nb).ratio())


# ── Inferencia de tipo (evidencia, no nombres de app) ───────────────────


def infer_entity_type_from_evidence(
    *,
    display_name: str,
    evidence: OperationalEntityEvidence,
    selection_intent: str = "",
    step_type_hint: str = "",
) -> OperationalEntityType:
    """Infiere tipo operacional desde señales estructurales."""
    intent = (selection_intent or evidence.semantic_context.get("selection_intent") or "").lower()
    st = (step_type_hint or evidence.semantic_context.get("step_type") or "").lower()
    name = display_name.strip()
    name_l = name.lower()

    if intent in ("primary_user_profile", "user_profile", "browser_profile"):
        return OperationalEntityType.USER_PROFILE
    if "profile" in intent and "browser" in intent:
        return OperationalEntityType.BROWSER_PROFILE
    if intent in ("account", "login_account"):
        return OperationalEntityType.ACCOUNT

    if st in ("search_content", "submit_search", "navigate_or_search"):
        if name and not is_generic_entity_label(name):
            return OperationalEntityType.SEARCH_QUERY
    if st in ("open_url", "open_bookmark", "navigate_to_location"):
        if name.startswith("http") or "." in name_l and " " not in name_l:
            return OperationalEntityType.TAB

    if "@" in name and "." in name.split("@")[-1]:
        return OperationalEntityType.EMAIL
    if re.search(r"\.(pdf|docx?|xlsx?|pptx?|txt|csv)$", name_l):
        return OperationalEntityType.DOCUMENT
    if re.search(r"\.(mp3|m4a|wav|flac|ogg)$", name_l):
        return OperationalEntityType.PLAYLIST
    if re.search(r"\.(mp4|mkv|avi|mov)$", name_l):
        return OperationalEntityType.RESULT_ITEM

    uia = (evidence.uia_name or "").lower()
    parent = " ".join(evidence.parent_chain or []).lower()
    nearby = " ".join(evidence.nearby_text or []).lower()
    ctx = f"{uia} {parent} {nearby}"

    if "searchbox" in ctx or "search box" in ctx or "omnibox" in ctx:
        if name and len(name) > 2:
            return OperationalEntityType.SEARCH_QUERY
    if "profile_option" in ctx or "profile option" in ctx:
        return OperationalEntityType.USER_PROFILE
    if "tabitem" in ctx or " tab " in f" {ctx} ":
        return OperationalEntityType.TAB
    if "listitem" in ctx and ("result" in ctx or "search" in ctx):
        return OperationalEntityType.RESULT_ITEM
    if "treeitem" in ctx or "folder" in ctx:
        return OperationalEntityType.FOLDER
    if "dataitem" in ctx and ("ticket" in name_l or "case" in name_l):
        return OperationalEntityType.TICKET
    if "conversation" in ctx or "chat" in ctx or "message" in ctx:
        return OperationalEntityType.CONVERSATION
    if "customer" in ctx or "cliente" in ctx:
        return OperationalEntityType.CUSTOMER

    if st in ("open_file", "file_operation"):
        return OperationalEntityType.FILE if "." in name else OperationalEntityType.FOLDER

    if len(name.split()) >= 2 and not is_generic_entity_label(name):
        return OperationalEntityType.RESULT_ITEM

    return OperationalEntityType.UNKNOWN


# ── Evidencia y extracción ──────────────────────────────────────────────


def _evidence_richness(evidence: OperationalEntityEvidence) -> float:
    score = 0.0
    if evidence.uia_name and not is_generic_entity_label(evidence.uia_name):
        score += 0.22
    if evidence.automation_id:
        score += 0.14
    if evidence.ocr_text and not is_generic_entity_label(evidence.ocr_text):
        score += 0.18
    if evidence.nearby_text:
        score += min(0.2, 0.05 * len(evidence.nearby_text))
    if evidence.parent_chain:
        score += min(0.16, 0.03 * len(evidence.parent_chain))
    if evidence.visual_fingerprint_available:
        score += 0.12 * max(0.0, evidence.visual_fingerprint_score)
    if evidence.window_title:
        score += 0.06
    if evidence.process_name:
        score += 0.04
    if evidence.semantic_context:
        score += min(0.12, 0.04 * len(evidence.semantic_context))
    return min(1.0, score)


def _pick_display_name(candidates: Sequence[str]) -> str:
    for c in candidates:
        c = str(c or "").strip()
        if c and not is_generic_entity_label(c):
            return c[:280]
    return ""


def build_evidence_from_capture(
    *,
    raw_event: Optional[RawEvent] = None,
    capture_bundle: Optional[Dict[str, Any]] = None,
    execution_truth_confirmed: bool = False,
    historical_success_score: float = 0.0,
) -> OperationalEntityEvidence:
    """Construye evidencia desde evento crudo o bundle de captura."""
    meta: Dict[str, Any] = {}
    wc = None
    if raw_event is not None:
        meta = dict(raw_event.metadata or {})
        wc = getattr(raw_event, "window_context", None)
    bundle = dict(capture_bundle or {})
    et_ok = bool(execution_truth_confirmed or bundle.get("execution_truth_confirmed"))

    uia = dict(meta.get("uia") or meta.get("uia_data") or bundle.get("uia") or {})
    iso = dict(meta.get("target_identity_isolation") or bundle.get("target_identity_isolation") or {})
    pre = dict(meta.get("pre_action_identity") or bundle.get("pre_action_identity") or {})
    vision = dict(meta.get("vision_data") or bundle.get("vision_data") or {})

    uia_name = str(
        iso.get("target_label")
        or iso.get("expected_label_hint")
        or uia.get("name")
        or pre.get("name")
        or bundle.get("uia_name")
        or "",
    ).strip()

    automation_id = str(uia.get("automation_id") or pre.get("automation_id") or "").strip()
    parent_chain = list(iso.get("parent_chain") or uia.get("parent_chain") or [])
    parent_chain = [str(p) for p in parent_chain if p][:16]

    ocr_text = str(
        meta.get("ocr_text")
        or (vision.get("ocr_text") if isinstance(vision, dict) else "")
        or bundle.get("ocr_text")
        or "",
    ).strip()

    nearby: List[str] = []
    for key in ("nearby_labels", "nearby_text", "label_candidates"):
        raw = meta.get(key) or bundle.get(key)
        if isinstance(raw, list):
            nearby.extend(str(x) for x in raw if x)
        elif raw:
            nearby.append(str(raw))
    nearby = list(dict.fromkeys(nearby))[:12]

    vf_score = 0.0
    vf_avail = False
    if isinstance(vision, dict):
        vf_avail = bool(vision.get("fingerprint") or vision.get("visual_hash"))
        try:
            vf_score = float(vision.get("match_score") or vision.get("confidence") or 0.0)
        except (TypeError, ValueError):
            vf_score = 0.0
        vf_avail = vf_avail or vf_score > 0.05

    ti_conf = 0.0
    if iso.get("trusted_pre_action_identity"):
        ti_conf = max(ti_conf, 0.88)
    try:
        ti_conf = max(ti_conf, float(iso.get("target_identity_confidence") or 0.0))
    except (TypeError, ValueError):
        pass
    if pre.get("confidence"):
        try:
            ti_conf = max(ti_conf, float(pre["confidence"]))
        except (TypeError, ValueError):
            pass

    freeze_raw = meta.get("operational_freeze_snapshot") or bundle.get(
        "operational_freeze_snapshot",
    )
    if isinstance(freeze_raw, dict):
        best = freeze_raw.get("best_entity_candidate") or {}
        if best.get("display_name") and freeze_raw.get("pre_transition_confirmed"):
            uia_name = str(best["display_name"])
        try:
            ti_conf = max(ti_conf, float(freeze_raw.get("freeze_confidence") or 0.0))
        except (TypeError, ValueError):
            pass
        if freeze_raw.get("pre_transition_confirmed"):
            ti_conf = max(ti_conf, 0.88)
        for lbl in freeze_raw.get("nearby_labels") or []:
            if lbl and lbl not in nearby:
                nearby.append(str(lbl))

    window_title = ""
    process_name = ""
    if wc is not None:
        window_title = str(getattr(wc, "title", "") or "")[:200]
        process_name = str(getattr(wc, "process_name", "") or "")[:120]
    window_title = window_title or str(bundle.get("window_title") or "")[:200]
    process_name = process_name or str(bundle.get("process_name") or "")[:120]

    semantic_context = dict(bundle.get("semantic_context") or {})
    if bundle.get("selection_intent"):
        semantic_context["selection_intent"] = bundle["selection_intent"]
    if bundle.get("step_type"):
        semantic_context["step_type"] = bundle["step_type"]

    return OperationalEntityEvidence(
        uia_name=uia_name,
        automation_id=automation_id,
        nearby_text=nearby,
        parent_chain=parent_chain,
        ocr_text=ocr_text,
        visual_fingerprint_available=vf_avail,
        visual_fingerprint_score=max(0.0, min(1.0, vf_score)),
        target_identity_confidence=max(0.0, min(1.0, ti_conf)),
        execution_truth_confirmed=et_ok,
        historical_success_score=max(0.0, min(1.0, historical_success_score)),
        semantic_context=semantic_context,
        window_title=window_title,
        process_name=process_name,
    )


def compute_entity_resolution_confidence(
    entity: OperationalEntity,
    *,
    unique_candidate_count: int = 1,
    continuity_agreement: float = 0.0,
    replay_validated: bool = False,
    successful_transition: bool = False,
    ambiguity_penalty: float = 0.0,
) -> float:
    """Score 0–1 para resolución de entidad."""
    ev = entity.evidence
    base = 0.28
    if ev.execution_truth_confirmed:
        base += 0.18
    if ev.target_identity_confidence >= PROMOTION_IDENTITY_CONFIDENCE_MIN:
        base += 0.14 * ev.target_identity_confidence
    base += 0.12 * _evidence_richness(ev)
    base += 0.1 * max(0.0, ev.historical_success_score)
    if ev.visual_fingerprint_available:
        base += 0.08 * ev.visual_fingerprint_score
    if continuity_agreement > 0:
        base += 0.1 * min(1.0, continuity_agreement)
    if replay_validated:
        base += 0.06
    if successful_transition:
        base += 0.05
    if unique_candidate_count <= 1:
        base += 0.06
    elif unique_candidate_count >= 4:
        base -= 0.12
    elif unique_candidate_count >= 2:
        base -= 0.06
    base -= max(0.0, min(0.35, ambiguity_penalty))
    if is_generic_entity_label(entity.display_name):
        base *= 0.35
    return max(0.0, min(1.0, round(base, 4)))


def should_promote_to_operational_entity(
    *,
    display_name: str,
    evidence: OperationalEntityEvidence,
    trusted_pre_action_identity: bool,
    sufficient_for_execution: bool,
    target_identity_confidence: float,
    multi_target_ambiguity: bool,
    click_inside_bbox: bool = True,
    unique_candidate_count: int = 1,
) -> Tuple[bool, List[str]]:
    """Reglas de promoción target → entidad operacional."""
    reasons: List[str] = []
    if not display_name or is_generic_entity_label(display_name):
        return False, ["generic_or_empty_label"]
    if not trusted_pre_action_identity:
        return False, ["trusted_pre_action_identity_false"]
    if not sufficient_for_execution:
        return False, ["sufficient_for_execution_false"]
    if target_identity_confidence < PROMOTION_IDENTITY_CONFIDENCE_MIN:
        return False, ["target_identity_confidence_below_threshold"]
    if multi_target_ambiguity and unique_candidate_count > 1:
        return False, ["multi_target_ambiguity"]
    if not click_inside_bbox:
        return False, ["click_outside_bbox"]
    richness = _evidence_richness(evidence)
    if richness < PROMOTION_EVIDENCE_RICHNESS_MIN:
        return False, ["evidence_richness_below_minimum"]
    reasons.append("promotion_criteria_met")
    return True, reasons


def extract_operational_entity(
    *,
    raw_event: Optional[RawEvent] = None,
    capture_bundle: Optional[Dict[str, Any]] = None,
    selection_intent: str = "",
    step_type_hint: str = "",
    trusted_pre_action_identity: Optional[bool] = None,
    sufficient_for_execution: Optional[bool] = None,
    multi_target_ambiguity: bool = False,
    click_inside_bbox: bool = True,
    unique_candidate_count: int = 1,
    continuity_agreement: float = 0.0,
    store_on_success: bool = True,
) -> Optional[OperationalEntity]:
    """Extrae y opcionalmente persiste una entidad operacional desde captura."""
    bundle = dict(capture_bundle or {})
    meta = dict((raw_event.metadata if raw_event else {}) or {})
    freeze_raw = meta.get("operational_freeze_snapshot") or bundle.get(
        "operational_freeze_snapshot",
    )
    if freeze_raw:
        try:
            from app.contracts.mission import OperationalFreezeSnapshot
            from app.services.runtime.pre_click_operational_freeze import (
                erl_entity_from_freeze,
            )

            freeze = OperationalFreezeSnapshot.model_validate(freeze_raw)
            erl_pick = erl_entity_from_freeze(freeze)
            if erl_pick:
                bundle.setdefault("display_name", erl_pick["display_name"])
                bundle["trusted_pre_action_identity"] = True
                bundle["target_identity_confidence"] = erl_pick.get("confidence", 0.88)
                bundle.setdefault("selection_intent", f"select_entity:{erl_pick.get('normalized_name', '')}")
        except Exception:
            pass

    et_confirmed = bool(bundle.get("execution_truth_confirmed"))
    hist = float(bundle.get("historical_success_score") or 0.0)

    evidence = build_evidence_from_capture(
        raw_event=raw_event,
        capture_bundle=bundle,
        execution_truth_confirmed=et_confirmed,
        historical_success_score=hist,
    )

    display = _pick_display_name([
        bundle.get("display_name"),
        evidence.uia_name,
        evidence.ocr_text,
        *(evidence.nearby_text or []),
    ])
    if not display:
        return None

    ti_conf = evidence.target_identity_confidence
    if trusted_pre_action_identity is None:
        meta = dict((raw_event.metadata if raw_event else {}) or {})
        iso = (
            meta.get("target_identity_isolation")
            or bundle.get("target_identity_isolation")
            or {}
        )
        if isinstance(iso, dict):
            trusted_pre_action_identity = bool(iso.get("trusted_pre_action_identity"))
        else:
            trusted_pre_action_identity = bool(bundle.get("trusted_pre_action_identity"))
    if sufficient_for_execution is None:
        sufficient_for_execution = bool(
            bundle.get("sufficient_for_execution")
            or (
                trusted_pre_action_identity
                and ti_conf >= PROMOTION_IDENTITY_CONFIDENCE_MIN
            )
        )

    ok, promo_reasons = should_promote_to_operational_entity(
        display_name=display,
        evidence=evidence,
        trusted_pre_action_identity=bool(trusted_pre_action_identity),
        sufficient_for_execution=bool(sufficient_for_execution),
        target_identity_confidence=ti_conf,
        multi_target_ambiguity=multi_target_ambiguity,
        click_inside_bbox=click_inside_bbox,
        unique_candidate_count=unique_candidate_count,
    )
    if not ok:
        return None

    entity_type = infer_entity_type_from_evidence(
        display_name=display,
        evidence=evidence,
        selection_intent=selection_intent,
        step_type_hint=step_type_hint,
    )
    normalized = normalize_entity_name(display)
    strategies = list(promo_reasons)
    if evidence.uia_name:
        strategies.append("uia_anchor")
    if evidence.ocr_text:
        strategies.append("ocr_anchor")
    if evidence.visual_fingerprint_available:
        strategies.append("visual_fingerprint")

    entity = OperationalEntity(
        entity_id=str(uuid.uuid4()),
        entity_type=entity_type,
        display_name=display,
        normalized_name=normalized,
        aliases=list(dict.fromkeys([display, normalized] + (evidence.nearby_text or [])[:4])),
        evidence=evidence,
        resolution_strategies=strategies,
        operational_role=selection_intent or str(bundle.get("operational_role") or ""),
        replay_anchor_candidates=[display, normalized] + list(evidence.parent_chain or [])[:4],
        learned_patterns=dict(bundle.get("learned_patterns") or {}),
    )
    entity.confidence = compute_entity_resolution_confidence(
        entity,
        unique_candidate_count=unique_candidate_count,
        continuity_agreement=continuity_agreement,
        replay_validated=bool(bundle.get("replay_validated")),
        successful_transition=bool(bundle.get("successful_transition")),
        ambiguity_penalty=float(bundle.get("ambiguity_penalty") or 0.0),
    )
    if "promotion_criteria_met" in promo_reasons:
        floor = PROMOTION_ENTITY_CONFIDENCE_MIN
        if len(display.split()) >= 2:
            floor = max(floor, STRONG_ENTITY_CONFIDENCE_MIN)
        entity.confidence = max(entity.confidence, floor)
    if entity.confidence < PROMOTION_ENTITY_CONFIDENCE_MIN:
        return None

    now = datetime.now(timezone.utc)
    entity.created_at = now
    entity.updated_at = now

    if store_on_success:
        try:
            EntityMemoryStore().upsert_entity(entity)
        except Exception as exc:
            log.debug("[ERL] entity memory upsert: %s", exc)

    try:
        from app.services.runtime.universal_collection_entity_engine import (
            build_selection_from_capture,
        )
        uces = build_selection_from_capture(
            raw_event=raw_event,
            capture_bundle=bundle,
        )
        if uces.selected and uces.collection:
            entity.evidence.semantic_context["uces"] = {
                "collection_id": uces.collection.collection_id,
                "collection_type": uces.collection.collection_type.value,
                "entity_id": uces.entity.entity_id if uces.entity else "",
                "selection_confidence": uces.confidence,
            }
            if uces.confidence >= PROMOTION_ENTITY_CONFIDENCE_MIN:
                entity.confidence = max(entity.confidence, uces.confidence)
                entity.resolution_strategies.append("uces_collection_selection")
    except Exception as exc:
        log.debug("[ERL] UCES enrich: %s", exc)

    return entity


# ── Matching ────────────────────────────────────────────────────────────


def match_entity_against_runtime(
    entity: OperationalEntity,
    runtime_candidates: Sequence[Dict[str, Any]],
    *,
    context: Optional[Dict[str, Any]] = None,
) -> EntityMatchResult:
    """Compara entidad grabada con candidatos runtime (multi-estrategia)."""
    ctx = dict(context or {})
    if not runtime_candidates:
        return EntityMatchResult(matched=False, notes=["no_runtime_candidates"])

    best: EntityMatchResult = EntityMatchResult()
    norm_target = entity.normalized_name or normalize_entity_name(entity.display_name)
    alias_norms = {normalize_entity_name(a) for a in (entity.aliases or []) if a}
    alias_norms.add(norm_target)

    store = EntityMemoryStore()
    hist_boost = store.historical_success_score(entity.entity_id, ctx.get("context_hash", ""))

    for cand in runtime_candidates:
        label = str(
            cand.get("label")
            or cand.get("name")
            or cand.get("uia_name")
            or cand.get("text")
            or "",
        ).strip()
        if not label:
            continue
        cand_norm = normalize_entity_name(label)

        # 1. exact
        if cand_norm and cand_norm == norm_target:
            conf = 0.98 + 0.02 * hist_boost
            if conf > best.confidence:
                best = EntityMatchResult(
                    matched=True,
                    strategy="exact",
                    confidence=min(1.0, conf),
                    candidate_label=label,
                    runtime_target=cand,
                )
            continue

        # 2. alias
        if cand_norm in alias_norms:
            conf = 0.94 + 0.04 * hist_boost
            if conf > best.confidence:
                best = EntityMatchResult(
                    matched=True,
                    strategy="alias",
                    confidence=min(1.0, conf),
                    candidate_label=label,
                    runtime_target=cand,
                )
            continue

        # 3. fuzzy
        sim = _text_similarity(entity.display_name, label)
        if sim >= FUZZY_MATCH_THRESHOLD:
            conf = 0.82 + 0.16 * sim + 0.02 * hist_boost
            if conf > best.confidence:
                best = EntityMatchResult(
                    matched=True,
                    strategy="fuzzy",
                    confidence=min(1.0, conf),
                    candidate_label=label,
                    runtime_target=cand,
                )

        # 4. OCR fuzzy
        ocr_hint = str(cand.get("ocr_text") or ctx.get("ocr_text") or "")
        if ocr_hint:
            ocr_sim = _text_similarity(entity.display_name, ocr_hint)
            if ocr_sim >= OCR_FUZZY_MATCH_THRESHOLD:
                conf = 0.78 + 0.18 * ocr_sim
                if conf > best.confidence:
                    best = EntityMatchResult(
                        matched=True,
                        strategy="ocr_fuzzy",
                        confidence=min(1.0, conf),
                        candidate_label=label or ocr_hint,
                        runtime_target=cand,
                    )

        # 5. nearby text
        nearby = cand.get("nearby_text") or cand.get("nearby_labels") or []
        if isinstance(nearby, list):
            for nb in nearby:
                if _text_similarity(entity.display_name, str(nb)) >= FUZZY_MATCH_THRESHOLD:
                    conf = 0.8
                    if conf > best.confidence:
                        best = EntityMatchResult(
                            matched=True,
                            strategy="nearby_text",
                            confidence=conf,
                            candidate_label=label or str(nb),
                            runtime_target=cand,
                        )
                    break

        # 6. visual continuity
        vf = float(cand.get("visual_fingerprint_score") or 0.0)
        if cand.get("visual_fingerprint_available") and vf >= 0.72:
            if _text_similarity(entity.display_name, label) >= 0.65:
                conf = 0.75 + 0.2 * vf
                if conf > best.confidence:
                    best = EntityMatchResult(
                        matched=True,
                        strategy="visual_continuity",
                        confidence=min(1.0, conf),
                        candidate_label=label,
                        runtime_target=cand,
                    )

        # 7. semantic continuity (contexto de ventana / rol)
        win = str(ctx.get("window_title") or "")
        if win and entity.evidence.window_title:
            if _text_similarity(win, entity.evidence.window_title) >= 0.55:
                if sim := _text_similarity(entity.display_name, label):
                    if sim >= 0.7:
                        conf = 0.72 + 0.15 * sim
                        if conf > best.confidence:
                            best = EntityMatchResult(
                                matched=True,
                                strategy="semantic_continuity",
                                confidence=min(1.0, conf),
                                candidate_label=label,
                                runtime_target=cand,
                            )

    if best.matched:
        best.notes.append(f"historical_boost={hist_boost:.2f}")
    return best


# ── Memoria local ───────────────────────────────────────────────────────


class EntityMemoryStore:
    """SQLite local-first para entidades exitosas y aliases."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db = Path(db_path) if db_path else _ENTITY_DB
        self._lock = threading.RLock()
        try:
            _ENTITY_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db), timeout=30)
        c.execute("PRAGMA journal_mode=WAL;")
        return c

    def _ensure_schema(self) -> None:
        with self._lock:
            c = self._connect()
            try:
                c.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS entity_record (
                        entity_id TEXT PRIMARY KEY,
                        normalized_name TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_entity_norm
                        ON entity_record(normalized_name, entity_type);
                    CREATE TABLE IF NOT EXISTS entity_alias (
                        alias_normalized TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        PRIMARY KEY (alias_normalized, entity_id)
                    );
                    CREATE TABLE IF NOT EXISTS entity_resolution_success (
                        entity_id TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        context_hash TEXT NOT NULL,
                        success_count INTEGER NOT NULL DEFAULT 0,
                        last_success REAL NOT NULL,
                        PRIMARY KEY (entity_id, strategy, context_hash)
                    );
                    """
                )
                c.commit()
            finally:
                c.close()

    def upsert_entity(self, entity: OperationalEntity) -> None:
        payload = entity.model_dump(mode="json")
        now = time.time()
        with self._lock:
            c = self._connect()
            try:
                c.execute(
                    """
                    INSERT INTO entity_record (entity_id, normalized_name, entity_type, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(entity_id) DO UPDATE SET
                        normalized_name=excluded.normalized_name,
                        entity_type=excluded.entity_type,
                        payload=excluded.payload,
                        updated_at=excluded.updated_at
                    """,
                    (
                        entity.entity_id,
                        entity.normalized_name,
                        entity.entity_type.value,
                        json.dumps(payload, ensure_ascii=False),
                        now,
                    ),
                )
                for alias in entity.aliases or []:
                    an = normalize_entity_name(alias)
                    if not an:
                        continue
                    c.execute(
                        """
                        INSERT OR IGNORE INTO entity_alias (alias_normalized, entity_id)
                        VALUES (?, ?)
                        """,
                        (an, entity.entity_id),
                    )
                c.commit()
            finally:
                c.close()

    def find_by_normalized_name(
        self,
        normalized_name: str,
        entity_type: Optional[OperationalEntityType] = None,
    ) -> List[OperationalEntity]:
        norm = normalize_entity_name(normalized_name)
        if not norm:
            return []
        with self._lock:
            c = self._connect()
            try:
                if entity_type is not None:
                    rows = c.execute(
                        """
                        SELECT payload FROM entity_record
                        WHERE normalized_name = ? AND entity_type = ?
                        ORDER BY updated_at DESC LIMIT 8
                        """,
                        (norm, entity_type.value),
                    ).fetchall()
                else:
                    rows = c.execute(
                        """
                        SELECT payload FROM entity_record
                        WHERE normalized_name = ?
                        ORDER BY updated_at DESC LIMIT 8
                        """,
                        (norm,),
                    ).fetchall()
            finally:
                c.close()
        out: List[OperationalEntity] = []
        for (payload,) in rows:
            try:
                out.append(OperationalEntity.model_validate(json.loads(payload)))
            except Exception:
                continue
        return out

    def find_by_alias(self, alias: str) -> List[OperationalEntity]:
        an = normalize_entity_name(alias)
        if not an:
            return []
        with self._lock:
            c = self._connect()
            try:
                ids = [
                    r[0]
                    for r in c.execute(
                        "SELECT entity_id FROM entity_alias WHERE alias_normalized = ?",
                        (an,),
                    ).fetchall()
                ]
            finally:
                c.close()
        entities: List[OperationalEntity] = []
        for eid in ids[:8]:
            ent = self.get_entity(eid)
            if ent:
                entities.append(ent)
        return entities

    def get_entity(self, entity_id: str) -> Optional[OperationalEntity]:
        with self._lock:
            c = self._connect()
            try:
                row = c.execute(
                    "SELECT payload FROM entity_record WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone()
            finally:
                c.close()
        if not row:
            return None
        try:
            return OperationalEntity.model_validate(json.loads(row[0]))
        except Exception:
            return None

    def record_resolution_success(
        self,
        entity_id: str,
        strategy: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        ctx_hash = _context_hash(context)
        now = time.time()
        with self._lock:
            c = self._connect()
            try:
                c.execute(
                    """
                    INSERT INTO entity_resolution_success
                        (entity_id, strategy, context_hash, success_count, last_success)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(entity_id, strategy, context_hash) DO UPDATE SET
                        success_count = success_count + 1,
                        last_success = excluded.last_success
                    """,
                    (entity_id, strategy, ctx_hash, now),
                )
                c.commit()
            finally:
                c.close()

    def historical_success_score(self, entity_id: str, context_hash: str = "") -> float:
        ch = context_hash or ""
        with self._lock:
            c = self._connect()
            try:
                row = c.execute(
                    """
                    SELECT SUM(success_count) FROM entity_resolution_success
                    WHERE entity_id = ? AND (context_hash = ? OR ? = '')
                    """,
                    (entity_id, ch, ch),
                ).fetchone()
            finally:
                c.close()
        total = int(row[0] or 0) if row else 0
        if total <= 0:
            return 0.0
        return min(1.0, 0.25 + 0.12 * total)


def _context_hash(context: Optional[Dict[str, Any]]) -> str:
    blob = json.dumps(context or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


# ── Runtime resolution ──────────────────────────────────────────────────


def resolve_entity_for_runtime(
    entity: OperationalEntity,
    runtime_candidates: Sequence[Dict[str, Any]],
    *,
    context: Optional[Dict[str, Any]] = None,
    legacy_resolver: Optional[Callable[..., Any]] = None,
    record_success: bool = True,
) -> EntityResolutionRuntimeResult:
    """Resuelve entidad en runtime: ERL primero, legacy al final."""
    ctx = dict(context or {})
    notes: List[str] = []

    # 1–6 vía match_entity (exact, fuzzy, visual, historical hints en score)
    match = match_entity_against_runtime(entity, runtime_candidates, context=ctx)
    if match.matched and match.confidence >= 0.72:
        if record_success:
            try:
                EntityMemoryStore().record_resolution_success(
                    entity.entity_id, match.strategy, ctx,
                )
            except Exception:
                pass
        return EntityResolutionRuntimeResult(
            resolved=True,
            entity=entity,
            strategy=match.strategy,
            confidence=match.confidence,
            notes=notes + [f"matched_via_{match.strategy}"],
        )

    # Historical: buscar en memoria por nombre/alias
    store = EntityMemoryStore()
    for hist in store.find_by_normalized_name(entity.normalized_name, entity.entity_type):
        hmatch = match_entity_against_runtime(hist, runtime_candidates, context=ctx)
        if hmatch.matched and hmatch.confidence >= 0.7:
            if record_success:
                try:
                    store.record_resolution_success(hist.entity_id, "historical_" + hmatch.strategy, ctx)
                except Exception:
                    pass
            return EntityResolutionRuntimeResult(
                resolved=True,
                entity=hist,
                strategy="historical_" + hmatch.strategy,
                confidence=hmatch.confidence,
                notes=notes + ["historical_memory_match"],
            )

    # Recovery: alias store en memoria
    for alias_ent in store.find_by_alias(entity.display_name):
        amatch = match_entity_against_runtime(alias_ent, runtime_candidates, context=ctx)
        if amatch.matched:
            return EntityResolutionRuntimeResult(
                resolved=True,
                entity=alias_ent,
                strategy="alias_memory_" + amatch.strategy,
                confidence=amatch.confidence * 0.98,
                notes=notes + ["alias_memory_recovery"],
            )

    # Legacy fallback
    if legacy_resolver is not None:
        try:
            legacy = legacy_resolver(entity, runtime_candidates, context=ctx)
            if legacy:
                return EntityResolutionRuntimeResult(
                    resolved=True,
                    entity=entity,
                    strategy="legacy_fallback",
                    confidence=float(getattr(legacy, "confidence", 0.65) or 0.65),
                    used_legacy_fallback=True,
                    notes=notes + ["legacy_resolver_invoked"],
                )
        except Exception as exc:
            notes.append(f"legacy_resolver_error:{exc}")

    return EntityResolutionRuntimeResult(
        resolved=False,
        entity=entity,
        strategy="unresolved",
        confidence=match.confidence if match.matched else 0.0,
        notes=notes,
    )


# ── Mission / Review helpers ────────────────────────────────────────────


def extract_entity_from_semantic_step(
    step: Dict[str, Any],
    mission: Any = None,
) -> Optional[OperationalEntity]:
    """Best-effort: entidad desde un step del SEP + misión."""
    params = step.get("params") if isinstance(step.get("params"), dict) else {}
    display = str(
        params.get("profile")
        or params.get("profile_name")
        or params.get("query")
        or params.get("label")
        or params.get("label_hint")
        or params.get("selection_label")
        or step.get("human_label")
        or "",
    ).strip()
    if is_generic_entity_label(display):
        return None

    et_confirmed = False
    if mission is not None:
        try:
            from app.services.missions.execution_truth_engine import (
                is_execution_truth_confirmed,
            )
            et_confirmed = bool(is_execution_truth_confirmed(mission))
        except Exception:
            pass

    bundle: Dict[str, Any] = {
        "display_name": display,
        "uia_name": display,
        "step_type": str(step.get("type") or ""),
        "execution_truth_confirmed": et_confirmed,
        "selection_intent": str(
            params.get("selection_intent") or "primary_user_profile",
        ),
        "nearby_text": [display],
    }
    if params.get("target_identity"):
        bundle["target_identity_isolation"] = params["target_identity"]
    elif display:
        bundle["target_identity_isolation"] = {
            "trusted_pre_action_identity": True,
            "target_label": display,
            "target_identity_confidence": 0.9,
        }

    ti_conf = float(params.get("target_identity_confidence") or 0.0)
    trusted = bool(params.get("trusted_pre_action_identity")) or (
        ti_conf >= PROMOTION_IDENTITY_CONFIDENCE_MIN
    )
    if et_confirmed and display and not is_generic_entity_label(display):
        trusted = True
        ti_conf = max(ti_conf, 0.9)
    if len(display.split()) >= 2 and ti_conf >= 0.8:
        trusted = True

    ent = extract_operational_entity(
        capture_bundle={
            **bundle,
            "trusted_pre_action_identity": trusted,
            "sufficient_for_execution": trusted and bool(display),
            "historical_success_score": float(params.get("historical_success_score") or 0.0),
            "replay_validated": et_confirmed,
        },
        selection_intent=str(params.get("selection_intent") or "primary_user_profile"),
        step_type_hint=str(step.get("type") or ""),
        trusted_pre_action_identity=trusted,
        sufficient_for_execution=trusted and bool(display),
        store_on_success=False,
    )
    if ent:
        return ent
    # Revisión: entidad sintética estable si hay nombre fuerte aunque falte raw_event.
    if not trusted or ti_conf < PROMOTION_IDENTITY_CONFIDENCE_MIN:
        return None
    evidence = build_evidence_from_capture(capture_bundle=bundle)
    evidence.target_identity_confidence = max(ti_conf, evidence.target_identity_confidence)
    evidence.execution_truth_confirmed = et_confirmed
    entity_type = infer_entity_type_from_evidence(
        display_name=display,
        evidence=evidence,
        selection_intent=str(params.get("selection_intent") or ""),
        step_type_hint=str(step.get("type") or ""),
    )
    now = datetime.now(timezone.utc)
    synthetic = OperationalEntity(
        entity_type=entity_type,
        display_name=display,
        normalized_name=normalize_entity_name(display),
        aliases=[display],
        evidence=evidence,
        resolution_strategies=["semantic_step_synthetic"],
        operational_role=str(params.get("selection_intent") or ""),
        confidence=0.0,
        created_at=now,
        updated_at=now,
    )
    synthetic.confidence = compute_entity_resolution_confidence(
        synthetic,
        replay_validated=et_confirmed,
    )
    if trusted and display and not is_generic_entity_label(display):
        floor = PROMOTION_ENTITY_CONFIDENCE_MIN
        if len(display.split()) >= 2:
            floor = max(floor, STRONG_ENTITY_CONFIDENCE_MIN)
        synthetic.confidence = max(synthetic.confidence, floor)
    if synthetic.confidence >= PROMOTION_ENTITY_CONFIDENCE_MIN:
        return synthetic
    return None


def mission_has_strong_operational_entities(mission: Any) -> bool:
    """¿La misión tiene al menos una entidad ERL fuerte?"""
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    steps = sep.get("steps") if isinstance(sep, dict) else []
    for sp in steps or []:
        if not isinstance(sp, dict):
            continue
        ent = extract_entity_from_semantic_step(sp, mission)
        if ent and ent.confidence >= STRONG_ENTITY_CONFIDENCE_MIN:
            return True
    return False


def entity_status_label_for_mission(mission: Any) -> Optional[str]:
    """Etiqueta amigable cuando ERL valida entidades (vista normal)."""
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    steps = sep.get("steps") if isinstance(sep, dict) else []
    profile_seen = False
    any_strong = False
    for sp in steps or []:
        if not isinstance(sp, dict):
            continue
        ent = extract_entity_from_semantic_step(sp, mission)
        if not ent or ent.confidence < STRONG_ENTITY_CONFIDENCE_MIN:
            continue
        any_strong = True
        if ent.entity_type in (
            OperationalEntityType.USER_PROFILE,
            OperationalEntityType.BROWSER_PROFILE,
        ):
            profile_seen = True
    if not any_strong:
        return None
    if profile_seen:
        return "Perfil principal identificado"
    return "Entidad validada automáticamente"


def sanitize_human_label_for_normal_view(label: str) -> str:
    """Quita marcadores de runtime expert-only del texto visible."""
    if not label:
        return label
    out = str(label)
    low = out.lower()
    for marker in EXPERT_ONLY_RESOLUTION_MARKERS:
        if marker in low:
            # Quitar fragmentos tipo «(uia_text_match)»
            out = re.sub(re.escape(marker), "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip(" -–—|,")
    return out.strip()


def apply_entity_resolution_relief(plan: Any, mission: Any) -> None:
    """Limpia ``needs_user_label`` cuando ERL tiene entidad fuerte estable."""
    seq = getattr(plan, "steps", None)
    if not isinstance(seq, list):
        return
    for row in seq:
        try:
            needs = bool(getattr(row, "needs_user_label", False))
        except Exception:
            continue
        if not needs:
            continue
        d: Dict[str, Any] = {}
        if hasattr(row, "to_dict"):
            try:
                d = row.to_dict()
            except Exception:
                d = {}
        if not d and hasattr(row, "type"):
            pars = getattr(row, "params", None) or {}
            d = {
                "id": getattr(row, "id", ""),
                "type": getattr(row, "type", ""),
                "params": dict(pars) if isinstance(pars, dict) else {},
                "human_label": getattr(row, "human_label", ""),
                "needs_user_label": getattr(row, "needs_user_label", False),
            }
        if not d:
            continue
        ent = extract_entity_from_semantic_step(d, mission)
        if ent and ent.confidence >= PROMOTION_ENTITY_CONFIDENCE_MIN:
            try:
                row.needs_user_label = False
                pars = getattr(row, "params", None)
                if isinstance(pars, dict):
                    pars["operational_entity"] = ent.model_dump(mode="json")
                    pars["entity_resolution_confidence"] = ent.confidence
                    uces_ctx = (ent.evidence.semantic_context or {}).get("uces")
                    if uces_ctx and pars.get("operational_collection"):
                        pars["collection_continuity_score"] = float(
                            uces_ctx.get("selection_confidence") or ent.confidence,
                        )
            except Exception:
                pass


__all__ = [
    "ENTITY_MEMORY_VERSION",
    "EXPERT_ONLY_RESOLUTION_MARKERS",
    "EntityMatchResult",
    "EntityMemoryStore",
    "EntityResolutionRuntimeResult",
    "apply_entity_resolution_relief",
    "compute_entity_resolution_confidence",
    "entity_status_label_for_mission",
    "extract_entity_from_semantic_step",
    "extract_operational_entity",
    "infer_entity_type_from_evidence",
    "is_generic_entity_label",
    "match_entity_against_runtime",
    "mission_has_strong_operational_entities",
    "normalize_entity_name",
    "resolve_entity_for_runtime",
    "sanitize_human_label_for_normal_view",
    "should_promote_to_operational_entity",
]
