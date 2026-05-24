"""
Universal Collection & Entity Semantics Engine (UCES)
-----------------------------------------------------
Entiende selecciones humanas como «entidad X dentro de colección Y»,
sin hardcode por app. Complementa ERL/TEL/GOOR; no sustituye SmartExecutor
ni SEP legacy.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    CollectionEntityReference,
    OperationalCollection,
    OperationalCollectionType,
    OperationalEntity,
    OperationalEntityEvidence,
    OperationalEntityType,
    RawEvent,
)
from app.core.logger import log
from app.core.paths import VAR_DIR
from app.services.runtime.entity_resolution_layer import (
    PROMOTION_ENTITY_CONFIDENCE_MIN,
    STRONG_ENTITY_CONFIDENCE_MIN,
    build_evidence_from_capture,
    compute_entity_resolution_confidence,
    extract_entity_from_semantic_step,
    extract_operational_entity,
    is_generic_entity_label,
    normalize_entity_name,
)

COLLECTION_MEMORY_VERSION = 1
_COLLECTION_DIR = VAR_DIR / "collection_memory"
_COLLECTION_DB = _COLLECTION_DIR / f"collection_memory_v{COLLECTION_MEMORY_VERSION}.sqlite"

# Acción universal (Fase 8)
UNIVERSAL_SELECT_KIND = "select_entity_from_collection"

# Step kinds legacy → universal
_LEGACY_KIND_TO_UNIVERSAL: Dict[str, str] = {
    "select_profile": UNIVERSAL_SELECT_KIND,
    "select_option": UNIVERSAL_SELECT_KIND,
    "choose_option": UNIVERSAL_SELECT_KIND,
    "click_result": UNIVERSAL_SELECT_KIND,
    "select_tab": UNIVERSAL_SELECT_KIND,
    "select_item": UNIVERSAL_SELECT_KIND,
    "click_item": UNIVERSAL_SELECT_KIND,
    "select_visible_option": UNIVERSAL_SELECT_KIND,
    "select_playlist": UNIVERSAL_SELECT_KIND,
    "select_ticket": UNIVERSAL_SELECT_KIND,
    "select_chat": UNIVERSAL_SELECT_KIND,
}

LEGACY_SELECTION_STEP_TYPES: frozenset = frozenset(_LEGACY_KIND_TO_UNIVERSAL.keys())

UCES_PRIMARY_STRATEGY = "select_entity:collection_first"
UCES_FALLBACK_STRATEGIES: Tuple[str, ...] = (
    "select_entity:operational_entity_match",
    "select_entity:legacy_kind_fallback",
)

COLLECTION_CONFIDENCE_MIN = 0.72
ENTITY_SELECTION_CONFIDENCE_MIN = 0.78
STRONG_COLLECTION_ENTITY_MIN = 0.88
CONTINUITY_MATCH_MIN = 0.70
FUZZY_ENTITY_THRESHOLD = 0.82
AMBIGUITY_GAP = 0.06

_ITEM_ROLE_MARKERS: Tuple[str, ...] = (
    "listitem", "treeitem", "tabitem", "menuitem", "dataitem",
    "option", "row", "gridcell", "article", "card", "link",
)

_EXPERT_ONLY_UCES_MARKERS: Tuple[str, ...] = (
    "uia_text_match",
    "structural_hash",
    "collection_memory",
    "fuzzy_reorder",
    "scroll_continuity",
)


@dataclass
class CollectionMatchResult:
    matched: bool = False
    collection: Optional[OperationalCollection] = None
    strategy: str = ""
    confidence: float = 0.0
    notes: List[str] = field(default_factory=list)


@dataclass
class EntityInCollectionMatchResult:
    matched: bool = False
    entity: Optional[CollectionEntityReference] = None
    strategy: str = ""
    confidence: float = 0.0
    candidate_count: int = 0
    notes: List[str] = field(default_factory=list)


@dataclass
class EntitySelectionIntentResult:
    """Resultado de inferir qué entidad eligió el humano."""

    selected: bool = False
    collection: Optional[OperationalCollection] = None
    entity: Optional[CollectionEntityReference] = None
    operational_entity: Optional[OperationalEntity] = None
    confidence: float = 0.0
    candidate_count: int = 0
    needs_clarification: bool = False
    notes: List[str] = field(default_factory=list)


# ── Helpers ─────────────────────────────────────────────────────────────


def _lower(s: Any) -> str:
    return str(s or "").strip().lower()


def _text_similarity(a: str, b: str) -> float:
    na, nb = normalize_entity_name(a), normalize_entity_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return float(SequenceMatcher(None, na, nb).ratio())


def _flatten_uia_nodes(
    uia: Any,
    *,
    parent_chain: Optional[List[str]] = None,
    depth: int = 0,
) -> List[Dict[str, Any]]:
    """Aplana jerarquía UIA/DOM-like a lista de nodos con parent_chain."""
    out: List[Dict[str, Any]] = []
    chain = list(parent_chain or [])

    def _walk(node: Any, pchain: List[str], d: int) -> None:
        if d > 24:
            return
        if isinstance(node, dict):
            name = str(node.get("name") or node.get("label") or "").strip()
            role = _lower(
                node.get("role")
                or node.get("control_type")
                or node.get("tag")
                or "",
            )
            rect = node.get("bounding_rectangle") or node.get("bbox") or {}
            node_parents = node.get("parent_chain")
            if isinstance(node_parents, list) and node_parents:
                effective_chain = [str(p) for p in node_parents if p][-8:]
            else:
                effective_chain = list(pchain)[-8:]
            item: Dict[str, Any] = {
                "name": name,
                "role": role,
                "automation_id": str(node.get("automation_id") or ""),
                "parent_chain": effective_chain,
                "depth": d,
                "scroll_pattern": bool(node.get("scroll_pattern")),
                "bbox": rect if isinstance(rect, dict) else {},
            }
            if name or any(m in role for m in _ITEM_ROLE_MARKERS):
                out.append(item)
            kids = node.get("children") or node.get("childs") or []
            child_chain = pchain + ([name] if name else [])
            if isinstance(kids, list):
                for ch in kids:
                    _walk(ch, child_chain, d + 1)
        elif isinstance(node, list):
            for ch in node:
                _walk(ch, pchain, d)

    if isinstance(uia, list):
        for n in uia:
            _walk(n, chain, depth)
    else:
        _walk(uia, chain, depth)
    return out


def _is_item_role(role: str) -> bool:
    if not role:
        return False
    return any(m in role for m in _ITEM_ROLE_MARKERS)


def _infer_collection_type(
    roles: Sequence[str],
    parent_ctx: str,
    *,
    scrollable: bool,
) -> OperationalCollectionType:
    ctx = parent_ctx.lower()
    role_set = " ".join(roles)
    if "tabitem" in role_set or " tab " in f" {ctx} ":
        return OperationalCollectionType.TABS
    if "menuitem" in role_set:
        return OperationalCollectionType.MENU
    if "treeitem" in role_set:
        return OperationalCollectionType.TREE
    if "dataitem" in role_set and "table" in ctx:
        return OperationalCollectionType.TABLE
    if "gridcell" in role_set or "datagrid" in ctx:
        return OperationalCollectionType.GRID
    if "dataitem" in role_set and "grid" in ctx:
        return OperationalCollectionType.TABLE
    if "search" in ctx and "listitem" in role_set:
        return OperationalCollectionType.SEARCH_RESULTS
    if "sidebar" in ctx or "navigation" in ctx:
        return OperationalCollectionType.SIDEBAR
    if "inbox" in ctx or "mail" in ctx:
        return OperationalCollectionType.INBOX
    if "playlist" in ctx:
        return OperationalCollectionType.PLAYLIST
    if "file" in ctx and ("folder" in ctx or "browser" in ctx):
        return OperationalCollectionType.FILE_BROWSER
    if "dashboard" in ctx:
        return OperationalCollectionType.DASHBOARD
    if "card" in role_set or "article" in role_set:
        return OperationalCollectionType.CARDS
    if scrollable and "listitem" in role_set:
        return OperationalCollectionType.LIST
    if "listitem" in role_set:
        return OperationalCollectionType.LIST
    return OperationalCollectionType.UNKNOWN


def _structural_signature(
    collection_type: OperationalCollectionType,
    parent_ctx: str,
    entity_names: Sequence[str],
) -> Dict[str, Any]:
    norm_names = sorted(
        normalize_entity_name(n) for n in entity_names if n and not is_generic_entity_label(n)
    )[:32]
    payload = f"{collection_type.value}|{parent_ctx}|{'|'.join(norm_names)}"
    sig_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return {
        "structural_hash": sig_hash,
        "parent_context": parent_ctx[:120],
        "entity_name_sample": norm_names[:8],
        "entity_count": len(norm_names),
    }


def universalize_step_kind(kind: str) -> str:
    """Converge kinds legacy a ``select_entity_from_collection``."""
    k = (kind or "").strip()
    return _LEGACY_KIND_TO_UNIVERSAL.get(k, k)


def is_universal_collection_step(kind: str) -> bool:
    return universalize_step_kind(kind) == UNIVERSAL_SELECT_KIND


# ── Fase 1: Collection detection ─────────────────────────────────────────


def detect_operational_collections(
    *,
    uia_hierarchy: Any = None,
    dom_hierarchy: Any = None,
    capture_bundle: Optional[Dict[str, Any]] = None,
) -> List[OperationalCollection]:
    """Detecta colecciones operacionales desde UIA/DOM (sin hardcode de app)."""
    bundle = dict(capture_bundle or {})
    nodes: List[Dict[str, Any]] = []

    if uia_hierarchy is not None:
        nodes.extend(_flatten_uia_nodes(uia_hierarchy))
    if dom_hierarchy is not None:
        nodes.extend(_flatten_uia_nodes(dom_hierarchy))

    meta = dict(bundle.get("metadata") or {})
    if not nodes and bundle:
        uia_meta = meta.get("uia") or bundle.get("uia")
        if uia_meta:
            nodes.extend(_flatten_uia_nodes(uia_meta))
        dom_meta = meta.get("web") or bundle.get("dom") or bundle.get("web")
        if dom_meta:
            nodes.extend(_flatten_uia_nodes(dom_meta))
        flat = meta.get("uia_flat") or bundle.get("uia_flat")
        if isinstance(flat, list):
            for fn in flat:
                if isinstance(fn, dict) and (fn.get("name") or fn.get("role")):
                    norm = dict(fn)
                    if not norm.get("role") and norm.get("control_type"):
                        norm["role"] = _lower(norm["control_type"])
                    nodes.append(norm)
                else:
                    nodes.extend(_flatten_uia_nodes(fn))

    item_nodes = [
        n for n in nodes
        if _is_item_role(_lower(n.get("role") or n.get("control_type")))
        and n.get("name")
    ]
    if len(item_nodes) < 2:
        return []

    # Agrupar por contexto de padre (estructura repetida)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for n in item_nodes:
        parent_key = "|".join(n.get("parent_chain") or []) or "_root"
        groups.setdefault(parent_key, []).append(n)

    collections: List[OperationalCollection] = []
    for parent_key, members in groups.items():
        if len(members) < 2:
            continue
        roles = [
            _lower(m.get("role") or m.get("control_type"))
            for m in members
        ]
        parent_ctx = parent_key
        scrollable = any(bool(m.get("scroll_pattern")) for m in members)
        ctype = _infer_collection_type(roles, parent_ctx, scrollable=scrollable)
        names = [str(m.get("name") or "") for m in members if m.get("name")]
        display = parent_ctx.split("|")[-1][:80] if parent_ctx else ctype.value
        if is_generic_entity_label(display):
            display = f"Colección {ctype.value}"

        coll = OperationalCollection(
            collection_type=ctype,
            display_name=display,
            visible_entity_count=len(members),
            structural_signals=_structural_signature(ctype, parent_ctx, names),
            continuity_signals={"parent_key": parent_key},
            selection_patterns=["click_select", "focus_change"],
            scrollable=scrollable,
            confidence=min(1.0, 0.55 + 0.06 * min(len(members), 8)),
            source="uia_dom_structure",
        )
        coll.entities = extract_entities_from_collection(coll, members)
        collections.append(coll)

    collections.sort(key=lambda c: c.confidence, reverse=True)
    return collections


# ── Fase 2: Entity extraction ───────────────────────────────────────────


def extract_entities_from_collection(
    collection: OperationalCollection,
    member_nodes: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[CollectionEntityReference]:
    """Extrae entidades de una colección detectada."""
    entities: List[CollectionEntityReference] = []
    nodes = list(member_nodes or [])
    if not nodes and collection.entities:
        return list(collection.entities)

    for idx, n in enumerate(nodes):
        name = str(n.get("name") or "").strip()
        if not name or is_generic_entity_label(name):
            continue
        role = str(n.get("role") or "")
        evidence = OperationalEntityEvidence(
            uia_name=name,
            automation_id=str(n.get("automation_id") or ""),
            parent_chain=list(n.get("parent_chain") or [])[:12],
            semantic_context={
                "collection_type": collection.collection_type.value,
                "collection_id": collection.collection_id,
                "item_role": role,
            },
        )
        nearby = [
            str(nodes[j].get("name") or "")
            for j in range(max(0, idx - 2), min(len(nodes), idx + 3))
            if j != idx and nodes[j].get("name")
        ]
        ent_type = OperationalEntityType.RESULT_ITEM
        if collection.collection_type == OperationalCollectionType.TABS:
            ent_type = OperationalEntityType.TAB
        elif collection.collection_type in (
            OperationalCollectionType.MENU,
            OperationalCollectionType.LIST,
        ):
            ent_type = OperationalEntityType.RESULT_ITEM
        elif collection.collection_type == OperationalCollectionType.FILE_BROWSER:
            ent_type = OperationalEntityType.FILE if "." in name else OperationalEntityType.FOLDER

        ref = CollectionEntityReference(
            entity_type=ent_type,
            display_name=name,
            normalized_name=normalize_entity_name(name),
            position_hint={
                "index": idx,
                "depth": int(n.get("depth") or 0),
                "bbox": dict(n.get("bbox") or {}),
            },
            nearby_entities=nearby[:6],
            collection_role=role,
            evidence=evidence,
        )
        entities.append(ref)

    collection.entities = entities
    collection.visible_entity_count = len(entities)
    return entities


# ── Fase 3: Selection intent ────────────────────────────────────────────


def _bbox_overlap(click_xy: Tuple[int, int], bbox: Dict[str, Any]) -> float:
    if not bbox:
        return 0.0
    try:
        x, y = int(click_xy[0]), int(click_xy[1])
        left = int(bbox.get("left", bbox.get("x", 0)))
        top = int(bbox.get("top", bbox.get("y", 0)))
        w = int(bbox.get("width", bbox.get("w", 0)))
        h = int(bbox.get("height", bbox.get("h", 0)))
        if w <= 0 or h <= 0:
            return 0.0
        if left <= x <= left + w and top <= y <= top + h:
            return 1.0
        # distancia normalizada al centro
        cx, cy = left + w // 2, top + h // 2
        dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        diag = (w ** 2 + h ** 2) ** 0.5 or 1.0
        return max(0.0, 1.0 - dist / diag)
    except Exception:
        return 0.0


def infer_entity_selection_intent(
    *,
    click_xy: Optional[Tuple[int, int]] = None,
    click_label: str = "",
    collections: Sequence[OperationalCollection],
    capture_bundle: Optional[Dict[str, Any]] = None,
    execution_truth_confirmed: bool = False,
) -> EntitySelectionIntentResult:
    """Infiere entidad elegida dentro de una colección visible."""
    result = EntitySelectionIntentResult()
    if not collections:
        result.notes.append("no_collections")
        return result

    bundle = dict(capture_bundle or {})
    label = (click_label or "").strip()
    if not label:
        label = str(
            bundle.get("target_label")
            or bundle.get("uia_name")
            or (bundle.get("uia") or {}).get("name")
            if isinstance(bundle.get("uia"), dict)
            else "",
        ).strip()

    best_coll: Optional[OperationalCollection] = None
    best_ent: Optional[CollectionEntityReference] = None
    best_score = 0.0
    candidates: List[Tuple[float, OperationalCollection, CollectionEntityReference]] = []

    for coll in collections:
        for ent in coll.entities or []:
            score = 0.0
            if label:
                score = max(score, _text_similarity(label, ent.display_name) * 0.92)
            if click_xy and ent.position_hint.get("bbox"):
                score = max(score, _bbox_overlap(click_xy, ent.position_hint["bbox"]) * 0.85)
            if execution_truth_confirmed and label:
                score = min(1.0, score + 0.08)
            score *= 0.4 + 0.6 * coll.confidence
            if score >= 0.55:
                candidates.append((score, coll, ent))

    candidates.sort(key=lambda t: t[0], reverse=True)
    result.candidate_count = len(candidates)

    if not candidates:
        result.notes.append("no_entity_candidate")
        return result

    top_score, best_coll, best_ent = candidates[0]
    if len(candidates) >= 2:
        gap = candidates[0][0] - candidates[1][0]
        if gap < AMBIGUITY_GAP and candidates[1][0] >= FUZZY_ENTITY_THRESHOLD - 0.1:
            result.needs_clarification = True
            result.notes.append("ambiguous_selection")

    if result.needs_clarification and result.candidate_count > 1:
        result.selected = False
        result.collection = best_coll
        result.confidence = top_score
        return result

    best_ent.selection_confidence = min(1.0, top_score)
    best_coll.active_entity_id = best_ent.entity_id

    op_ent = extract_operational_entity(
        capture_bundle={
            **bundle,
            "selection_intent": "entity_in_collection",
            "execution_truth_confirmed": execution_truth_confirmed,
            "sufficient_for_execution": execution_truth_confirmed,
        },
        selection_intent="entity_in_collection",
        trusted_pre_action_identity=execution_truth_confirmed or top_score >= 0.85,
        sufficient_for_execution=execution_truth_confirmed or top_score >= ENTITY_SELECTION_CONFIDENCE_MIN,
        store_on_success=False,
    )
    if op_ent is None and best_ent.display_name:
        evidence = best_ent.evidence or build_evidence_from_capture(capture_bundle=bundle)
        evidence.semantic_context["collection_id"] = best_coll.collection_id
        evidence.semantic_context["collection_type"] = best_coll.collection_type.value
        op_ent = OperationalEntity(
            entity_type=best_ent.entity_type,
            display_name=best_ent.display_name,
            normalized_name=best_ent.normalized_name,
            evidence=evidence,
            resolution_strategies=["uces_collection_selection"],
            operational_role="collection_member",
        )
        op_ent.confidence = compute_entity_resolution_confidence(
            op_ent,
            unique_candidate_count=1,
            successful_transition=execution_truth_confirmed,
        )

    result.selected = top_score >= ENTITY_SELECTION_CONFIDENCE_MIN
    result.collection = best_coll
    result.entity = best_ent
    result.operational_entity = op_ent
    result.confidence = top_score
    return result


# ── Fase 4: Continuity ──────────────────────────────────────────────────


def preserve_collection_continuity(
    previous: Optional[OperationalCollection],
    current_collections: Sequence[OperationalCollection],
) -> Tuple[Optional[OperationalCollection], float]:
    """Reconoce la misma colección tras cambios de layout/scroll/viewport."""
    if previous is None or not current_collections:
        return None, 0.0

    prev_sig = dict(previous.structural_signals or {})
    prev_hash = prev_sig.get("structural_hash", "")
    prev_type = previous.collection_type
    prev_count = int(previous.visible_entity_count or 0)

    best: Optional[OperationalCollection] = None
    best_score = 0.0

    for coll in current_collections:
        score = 0.0
        if coll.collection_type == prev_type:
            score += 0.35
        sig = dict(coll.structural_signals or {})
        if prev_hash and sig.get("structural_hash") == prev_hash:
            score += 0.45
        # overlap de nombres (reordenados)
        prev_names = set(prev_sig.get("entity_name_sample") or [])
        cur_names = set(sig.get("entity_name_sample") or [])
        if prev_names and cur_names:
            overlap = len(prev_names & cur_names) / max(1, len(prev_names | cur_names))
            score += 0.25 * overlap
        # conteo similar (scroll parcial)
        if prev_count and coll.visible_entity_count:
            ratio = min(prev_count, coll.visible_entity_count) / max(
                prev_count, coll.visible_entity_count,
            )
            if ratio >= 0.4:
                score += 0.15 * ratio
        if score > best_score:
            best_score = score
            best = coll

    if best_score >= CONTINUITY_MATCH_MIN:
        cont = dict(best.continuity_signals or {})
        cont["continuity_score"] = round(best_score, 4)
        cont["previous_collection_id"] = previous.collection_id
        best.continuity_signals = cont
        return best, best_score
    return None, best_score


# ── Fase 6: Collection memory ───────────────────────────────────────────


class CollectionMemoryStore:
    """SQLite: firmas de colección, selecciones exitosas, continuidad."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db = Path(db_path) if db_path else _COLLECTION_DB
        self._lock = threading.RLock()
        try:
            _COLLECTION_DIR.mkdir(parents=True, exist_ok=True)
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
                    CREATE TABLE IF NOT EXISTS collection_record (
                        collection_id TEXT PRIMARY KEY,
                        structural_hash TEXT NOT NULL,
                        collection_type TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_coll_hash
                        ON collection_record(structural_hash, collection_type);
                    CREATE TABLE IF NOT EXISTS collection_selection_success (
                        collection_id TEXT NOT NULL,
                        entity_normalized TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        success_count INTEGER NOT NULL DEFAULT 0,
                        last_success REAL NOT NULL,
                        PRIMARY KEY (collection_id, entity_normalized, strategy)
                    );
                    """
                )
                c.commit()
            finally:
                c.close()

    def upsert_collection(self, collection: OperationalCollection) -> None:
        payload = collection.model_dump(mode="json")
        sig = (collection.structural_signals or {}).get("structural_hash", "")
        now = time.time()
        with self._lock:
            c = self._connect()
            try:
                c.execute(
                    """
                    INSERT INTO collection_record
                        (collection_id, structural_hash, collection_type, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(collection_id) DO UPDATE SET
                        structural_hash=excluded.structural_hash,
                        collection_type=excluded.collection_type,
                        payload=excluded.payload,
                        updated_at=excluded.updated_at
                    """,
                    (
                        collection.collection_id,
                        sig,
                        collection.collection_type.value,
                        json.dumps(payload, ensure_ascii=False),
                        now,
                    ),
                )
                c.commit()
            finally:
                c.close()

    def record_selection_success(
        self,
        collection_id: str,
        entity_normalized: str,
        strategy: str,
    ) -> None:
        if not collection_id or not entity_normalized:
            return
        now = time.time()
        with self._lock:
            c = self._connect()
            try:
                c.execute(
                    """
                    INSERT INTO collection_selection_success
                        (collection_id, entity_normalized, strategy, success_count, last_success)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(collection_id, entity_normalized, strategy) DO UPDATE SET
                        success_count = success_count + 1,
                        last_success = excluded.last_success
                    """,
                    (collection_id, entity_normalized, strategy, now),
                )
                c.commit()
            finally:
                c.close()

    def historical_selection_boost(
        self,
        collection_id: str,
        entity_normalized: str,
    ) -> float:
        with self._lock:
            c = self._connect()
            try:
                row = c.execute(
                    """
                    SELECT SUM(success_count) FROM collection_selection_success
                    WHERE collection_id = ? AND entity_normalized = ?
                    """,
                    (collection_id, entity_normalized),
                ).fetchone()
                if not row or not row[0]:
                    return 0.0
                return min(0.25, 0.03 * int(row[0]))
            finally:
                c.close()


# ── Fase 7: Runtime matching ────────────────────────────────────────────


def match_collection_runtime(
    stored: OperationalCollection,
    runtime_collections: Sequence[OperationalCollection],
) -> CollectionMatchResult:
    """Empareja colección grabada con candidatos runtime."""
    if not runtime_collections:
        return CollectionMatchResult(notes=["no_runtime_collections"])

    best = CollectionMatchResult()
    prev_hash = (stored.structural_signals or {}).get("structural_hash", "")

    for cand in runtime_collections:
        score = 0.0
        if cand.collection_type == stored.collection_type:
            score += 0.3
        sig = cand.structural_signals or {}
        if prev_hash and sig.get("structural_hash") == prev_hash:
            score += 0.5
        # fuzzy name overlap
        prev_names = set((stored.structural_signals or {}).get("entity_name_sample") or [])
        cur_names = set(sig.get("entity_name_sample") or [])
        if prev_names and cur_names:
            overlap = len(prev_names & cur_names) / max(1, len(prev_names | cur_names))
            score += 0.35 * overlap
        if score > best.confidence:
            best = CollectionMatchResult(
                matched=score >= COLLECTION_CONFIDENCE_MIN,
                collection=cand,
                strategy="exact" if score >= 0.9 else "structural_fuzzy",
                confidence=min(1.0, score),
            )
    return best


def match_entity_inside_collection(
    stored_entity: CollectionEntityReference,
    runtime_collection: OperationalCollection,
    *,
    collection_id: str = "",
    allow_reorder: bool = True,
) -> EntityInCollectionMatchResult:
    """Encuentra la entidad grabada dentro de una colección runtime."""
    runtime_entities = list(runtime_collection.entities or [])
    if not runtime_entities:
        return EntityInCollectionMatchResult(candidate_count=0, notes=["empty_collection"])

    store = CollectionMemoryStore()
    boost = store.historical_selection_boost(
        collection_id or runtime_collection.collection_id,
        stored_entity.normalized_name,
    )

    best = EntityInCollectionMatchResult(candidate_count=len(runtime_entities))
    target_norm = stored_entity.normalized_name or normalize_entity_name(
        stored_entity.display_name,
    )

    scored: List[Tuple[float, CollectionEntityReference, str]] = []
    for ent in runtime_entities:
        sim = _text_similarity(stored_entity.display_name, ent.display_name)
        strategy = "fuzzy"
        if sim >= 0.98:
            strategy = "exact"
        elif allow_reorder and sim >= FUZZY_ENTITY_THRESHOLD:
            strategy = "fuzzy_reorder"
        ocr_sim = 0.0
        ocr = (ent.evidence.ocr_text if ent.evidence else "") or ""
        if ocr:
            ocr_sim = _text_similarity(stored_entity.display_name, ocr)
        conf = max(sim, ocr_sim * 0.95) + boost
        if conf >= FUZZY_ENTITY_THRESHOLD - 0.05:
            scored.append((conf, ent, strategy))

    if not scored:
        return best

    scored.sort(key=lambda t: t[0], reverse=True)
    top_conf, top_ent, top_strat = scored[0]
    best.matched = top_conf >= ENTITY_SELECTION_CONFIDENCE_MIN
    best.entity = top_ent
    best.strategy = top_strat
    best.confidence = min(1.0, top_conf)
    best.candidate_count = len(scored)

    if len(scored) >= 2:
        gap = scored[0][0] - scored[1][0]
        if gap < AMBIGUITY_GAP:
            best.notes.append("ambiguous_entities")
            if scored[1][0] >= FUZZY_ENTITY_THRESHOLD - 0.08:
                best.matched = False

    return best


# ── Fase 5: Entity-first replay hook ────────────────────────────────────


def build_selection_from_capture(
    *,
    raw_event: Optional[RawEvent] = None,
    capture_bundle: Optional[Dict[str, Any]] = None,
) -> EntitySelectionIntentResult:
    """Pipeline grabación: detectar colección + entidad seleccionada."""
    bundle = dict(capture_bundle or {})
    meta = dict(raw_event.metadata or {}) if raw_event else dict(bundle.get("metadata") or {})

    freeze_raw = meta.get("operational_freeze_snapshot") or bundle.get(
        "operational_freeze_snapshot",
    )
    freeze = _parse_pcof_snapshot(freeze_raw)
    if freeze is not None:
        try:
            from app.services.runtime.pre_click_operational_freeze import (
                freeze_to_capture_bundle,
            )

            bundle = {**bundle, **freeze_to_capture_bundle(freeze)}
            meta = {**meta, **bundle}
            if should_promote_pcof_freeze(freeze) or freeze.pre_transition_confirmed:
                pcof_intent = intent_from_pcof_freeze(freeze)
                if pcof_intent.selected:
                    try:
                        store = CollectionMemoryStore()
                        if pcof_intent.collection:
                            store.upsert_collection(pcof_intent.collection)
                    except Exception:
                        pass
                    return pcof_intent
        except Exception:
            pass

    uia = meta.get("uia") or bundle.get("uia")
    dom = meta.get("web") or bundle.get("dom")
    flat = meta.get("uia_flat") or bundle.get("uia_flat")
    collections = detect_operational_collections(
        uia_hierarchy=flat if isinstance(flat, list) and len(flat) >= 2 else uia,
        dom_hierarchy=dom,
        capture_bundle={**bundle, "metadata": meta},
    )

    click_xy: Optional[Tuple[int, int]] = None
    if raw_event and raw_event.mouse_action:
        click_xy = (int(raw_event.mouse_action.x), int(raw_event.mouse_action.y))

    iso = dict(meta.get("target_identity_isolation") or {})
    label = str(iso.get("target_label") or "").strip()
    et_ok = bool(
        bundle.get("execution_truth_confirmed")
        or iso.get("trusted_pre_action_identity"),
    )

    intent = infer_entity_selection_intent(
        click_xy=click_xy,
        click_label=label,
        collections=collections,
        capture_bundle=bundle,
        execution_truth_confirmed=et_ok,
    )

    if intent.selected and intent.collection:
        try:
            store = CollectionMemoryStore()
            store.upsert_collection(intent.collection)
            if intent.entity:
                store.record_selection_success(
                    intent.collection.collection_id,
                    intent.entity.normalized_name,
                    "recording",
                )
        except Exception as exc:
            log.debug("[UCES] memory store: %s", exc)

    return intent


def try_collection_entity_first_runtime(
    step: Any,
    runtime_candidates: Sequence[Dict[str, Any]],
    *,
    mission: Any = None,
    context: Optional[Dict[str, Any]] = None,
) -> EntityInCollectionMatchResult:
    """Entity-first dentro de colección (antes de coords)."""
    pars = dict(getattr(step, "params", None) or {})
    stored_coll_raw = pars.get("operational_collection")
    stored_ent_raw = pars.get("collection_entity") or pars.get("selected_collection_entity")

    if not stored_coll_raw or not stored_ent_raw:
        return EntityInCollectionMatchResult(notes=["no_stored_collection_context"])

    try:
        stored_coll = OperationalCollection.model_validate(stored_coll_raw)
        stored_ent = CollectionEntityReference.model_validate(stored_ent_raw)
    except Exception:
        return EntityInCollectionMatchResult(notes=["invalid_collection_payload"])

    ctx = dict(context or {})
    uia_flat = list(runtime_candidates or [])
    runtime_colls = detect_operational_collections(
        capture_bundle={"uia_flat": uia_flat, **ctx},
    )

    prev_cont = preserve_collection_continuity(stored_coll, runtime_colls)
    coll_match = match_collection_runtime(
        stored_coll,
        [prev_cont[0]] if prev_cont[0] else runtime_colls,
    )
    if not coll_match.matched or not coll_match.collection:
        return EntityInCollectionMatchResult(
            notes=["collection_not_matched"],
            candidate_count=len(runtime_colls),
        )

    ent_match = match_entity_inside_collection(
        stored_ent,
        coll_match.collection,
        collection_id=stored_coll.collection_id,
    )
    return ent_match


# ── Fase 9: Ready relief ────────────────────────────────────────────────


def apply_pcof_ready_relief(plan: Any, mission: Any) -> None:
    """Relief READY cuando PCOF confirmó entidad+colección pre-transición."""
    seq = getattr(plan, "steps", None)
    if not isinstance(seq, list):
        return
    for row in seq:
        pars = getattr(row, "params", None) or {}
        if not isinstance(pars, dict):
            continue
        if not pars.get("pcof_primary") and not pars.get("pre_transition_confirmed"):
            continue
        if str(getattr(row, "type", "") or "") != UNIVERSAL_SELECT_KIND:
            continue
        cand = int(pars.get("candidate_count") or 99)
        conf = float(pars.get("freeze_confidence") or pars.get("selection_confidence") or 0.0)
        ent_raw = pars.get("collection_entity") or pars.get("entity")
        name = ""
        if isinstance(ent_raw, dict):
            name = str(ent_raw.get("display_name") or "").strip()
        strong = (
            bool(pars.get("pre_transition_confirmed"))
            and conf >= STRONG_COLLECTION_ENTITY_MIN
            and cand <= 1
            and name
            and not is_generic_entity_label(name)
            and not bool(pars.get("needs_clarification"))
        )
        if not strong:
            continue
        try:
            row.needs_user_label = False
            pars["pcof_ready_relief"] = True
            row.human_label = human_label_for_uces_step(pars)
        except Exception:
            pass


def apply_collection_entity_ready_relief(plan: Any, mission: Any) -> None:
    """Relief READY cuando colección+entidad+continuidad son fuertes."""
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
        pars = getattr(row, "params", None) or {}
        if not isinstance(pars, dict):
            continue
        step_type = str(getattr(row, "type", "") or "")
        coll_raw = pars.get("operational_collection") or pars.get("collection")
        ent_raw = pars.get("collection_entity") or pars.get("entity")
        conf = float(
            pars.get("selection_confidence")
            or pars.get("collection_selection_confidence")
            or 0.0,
        )
        cand = int(
            pars.get("candidate_count")
            or pars.get("collection_candidate_count")
            or 99,
        )
        et_ok = bool(pars.get("execution_truth_confirmed"))
        cont = float(pars.get("collection_continuity_score") or 0.0)

        if step_type == UNIVERSAL_SELECT_KIND and coll_raw and ent_raw:
            strong = (
                conf >= STRONG_COLLECTION_ENTITY_MIN
                and cand <= 1
                and not bool(pars.get("needs_clarification"))
                and (et_ok or cont >= CONTINUITY_MATCH_MIN or conf >= ENTITY_SELECTION_CONFIDENCE_MIN)
            )
            if strong:
                try:
                    row.needs_user_label = False
                    pars["uces_ready_relief"] = True
                    if not row.human_label or "perfil" in str(row.human_label).lower():
                        row.human_label = human_label_for_uces_step(pars)
                except Exception:
                    pass
            continue

        if not coll_raw or not ent_raw:
            ent = extract_entity_from_semantic_step(
                {"type": step_type, "params": pars},
                mission,
            )
            if ent and ent.confidence >= PROMOTION_ENTITY_CONFIDENCE_MIN:
                try:
                    row.needs_user_label = False
                    pars["operational_entity"] = ent.model_dump(mode="json")
                except Exception:
                    pass
            continue

        strong = (
            conf >= STRONG_COLLECTION_ENTITY_MIN
            and cand <= 1
            and (et_ok or cont >= CONTINUITY_MATCH_MIN)
        )
        if strong:
            try:
                row.needs_user_label = False
                pars["uces_ready_relief"] = True
            except Exception:
                pass


def collection_entity_status_label_for_mission(mission: Any) -> Optional[str]:
    """Etiqueta vista normal cuando UCES identificó entidad en colección."""
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    steps = sep.get("steps") if isinstance(sep, dict) else []
    for sp in steps or []:
        if not isinstance(sp, dict):
            continue
        pars = sp.get("params") or {}
        if pars.get("collection_entity") and float(
            pars.get("collection_selection_confidence") or 0,
        ) >= STRONG_COLLECTION_ENTITY_MIN:
            return "Elemento reconocido automáticamente"
        ent = extract_entity_from_semantic_step(sp, mission)
        if ent and ent.confidence >= STRONG_ENTITY_CONFIDENCE_MIN:
            return "Entidad identificada automáticamente"
    return None


def mission_has_strong_collection_selection(mission: Any) -> bool:
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    steps = sep.get("steps") if isinstance(sep, dict) else []
    for sp in steps or []:
        if not isinstance(sp, dict):
            continue
        pars = sp.get("params") or {}
        if float(pars.get("collection_selection_confidence") or 0) >= STRONG_COLLECTION_ENTITY_MIN:
            return True
    return False


def sanitize_human_label_uces(label: str) -> str:
    if not label:
        return label
    out = str(label)
    for marker in _EXPERT_ONLY_UCES_MARKERS:
        out = re.sub(re.escape(marker), "", out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip(" -–—|,")


def expert_audit_lines(mission: Any) -> List[str]:
    rows = list(getattr(mission, "collection_entity_resolution_audit", None) or [])
    lines: List[str] = []
    for r in rows[-12:]:
        if not isinstance(r, dict):
            continue
        lines.append(
            f"  · {r.get('step_id', '?')}: coll={r.get('collection_type', '?')} "
            f"ent={r.get('entity_name', '?')[:40]} "
            f"conf={r.get('confidence', 0):.2f} "
            f"strat={r.get('strategy', '')}"
        )
    return lines


def append_collection_audit(mission: Any, entry: Dict[str, Any]) -> None:
    if mission is None:
        return
    try:
        rows = list(getattr(mission, "collection_entity_resolution_audit", None) or [])
        rows.append(entry)
        setattr(mission, "collection_entity_resolution_audit", rows[-200:])
    except Exception as exc:
        log.debug("[UCES] audit: %s", exc)


def attach_collection_context_to_step_params(
    params: Dict[str, Any],
    intent: EntitySelectionIntentResult,
) -> None:
    """Persiste contexto UCES en params del step (no destructivo)."""
    if not intent.selected:
        return
    if intent.collection:
        params["operational_collection"] = intent.collection.model_dump(mode="json")
        params["collection"] = params["operational_collection"]
    if intent.entity:
        params["collection_entity"] = intent.entity.model_dump(mode="json")
        params["entity"] = params["collection_entity"]
    params["collection_selection_confidence"] = intent.confidence
    params["selection_confidence"] = intent.confidence
    params["collection_candidate_count"] = intent.candidate_count
    params["candidate_count"] = intent.candidate_count
    params["needs_clarification"] = bool(intent.needs_clarification)
    if intent.operational_entity:
        params["operational_entity"] = intent.operational_entity.model_dump(mode="json")
    params["step_kind_universal"] = UNIVERSAL_SELECT_KIND


def human_label_for_uces_step(params: Dict[str, Any]) -> str:
    """Etiqueta humana para Mission Review (vista normal, vía UOL)."""
    try:
        from app.services.missions.universal_operational_language import (
            compile_semantic_step_to_uol,
            human_operational_label_for_uol,
        )

        step = {
            "type": "select_entity_from_collection",
            "params": dict(params),
        }
        action = compile_semantic_step_to_uol(step)
        if action is not None:
            return human_operational_label_for_uol(action)
    except Exception:
        pass
    ent_raw = params.get("collection_entity") or params.get("entity") or {}
    name = ""
    if isinstance(ent_raw, dict):
        name = str(ent_raw.get("display_name") or "").strip()
    if not name:
        name = str(params.get("selection_label") or "").strip()
    conf = float(
        params.get("freeze_confidence")
        or params.get("selection_confidence")
        or 0.0,
    )
    cand = int(params.get("candidate_count") or 99)
    if name and conf >= STRONG_COLLECTION_ENTITY_MIN and cand <= 1:
        return f"Seleccionar {name}"
    if name and conf >= ENTITY_SELECTION_CONFIDENCE_MIN:
        return f"Seleccionar {name}"
    return "Seleccionar elemento de la lista"


def _parse_pcof_snapshot(raw: Any) -> Optional[Any]:
    """``OperationalFreezeSnapshot`` desde dict de metadata (lazy import)."""
    if not isinstance(raw, dict) or not raw.get("freeze_id"):
        return None
    try:
        from app.contracts.mission import OperationalFreezeSnapshot

        return OperationalFreezeSnapshot.model_validate(raw)
    except Exception:
        return None


def _collection_from_pcof_freeze(freeze: Any) -> Optional[OperationalCollection]:
    coll_snap = freeze.best_collection_candidate
    if not coll_snap:
        return None
    try:
        ctype = OperationalCollectionType(coll_snap.collection_type)
    except Exception:
        ctype = OperationalCollectionType.UNKNOWN
    entities: List[CollectionEntityReference] = []
    try:
        from app.services.runtime.pre_click_operational_freeze import (
            is_pcof_affordance_label,
        )
    except Exception:
        is_pcof_affordance_label = lambda _n: False  # type: ignore

    for name in coll_snap.entities or []:
        nm = str(name or "").strip()
        if not nm or is_generic_entity_label(nm) or is_pcof_affordance_label(nm):
            continue
        entities.append(
            CollectionEntityReference(
                entity_type=OperationalEntityType.UNKNOWN,
                display_name=nm,
                normalized_name=normalize_entity_name(nm),
                selection_confidence=float(coll_snap.confidence or 0.8),
                collection_role="listitem",
            ),
        )
    if len(entities) < 1:
        return None
    best = freeze.best_entity_candidate
    active_id = None
    if best:
        for ent in entities:
            if ent.display_name == best.display_name:
                active_id = ent.entity_id
                ent.selection_confidence = max(
                    ent.selection_confidence,
                    float(best.confidence or freeze.freeze_confidence),
                )
    return OperationalCollection(
        collection_type=ctype,
        display_name=str(coll_snap.display_name or ctype.value),
        visible_entity_count=len(entities),
        entities=entities,
        structural_signals={
            "structural_hash": coll_snap.structural_hash,
            "source": "pcof_freeze",
        },
        scrollable=bool(coll_snap.scrollable),
        active_entity_id=active_id,
        confidence=float(coll_snap.confidence or freeze.freeze_confidence),
        source="pcof_freeze",
    )


def _synthetic_collection_from_pcof_best(freeze: Any) -> Optional[OperationalCollection]:
    """Colección mínima cuando hay entidad fuerte pero pocos hermanos en UIA."""
    best = freeze.best_entity_candidate
    if not best or not best.display_name:
        return None
    ent_ref = CollectionEntityReference(
        entity_type=OperationalEntityType.UNKNOWN,
        display_name=best.display_name,
        normalized_name=best.normalized_name or normalize_entity_name(best.display_name),
        selection_confidence=float(best.confidence or freeze.freeze_confidence),
        evidence=OperationalEntityEvidence(
            uia_name=best.uia_name or best.display_name,
            target_identity_confidence=float(freeze.freeze_confidence),
            execution_truth_confirmed=True,
        ),
    )
    return OperationalCollection(
        collection_type=OperationalCollectionType.UNKNOWN,
        display_name="operational_surface",
        visible_entity_count=1,
        entities=[ent_ref],
        active_entity_id=ent_ref.entity_id,
        confidence=float(freeze.freeze_confidence),
        source="pcof_synthetic",
    )


def intent_from_pcof_freeze(freeze: Any) -> EntitySelectionIntentResult:
    """Construye intent UCES desde snapshot PCOF (fuente primaria SEP)."""
    from app.services.runtime.pre_click_operational_freeze import (
        FREEZE_CONFIDENCE_MIN,
        actionable_pcof_entity_names,
        freeze_to_capture_bundle,
        pcof_best_dominates_actionables,
        pcof_candidate_count,
        qualifies_pcof_for_sep_promotion,
    )

    result = EntitySelectionIntentResult()
    ok, notes = qualifies_pcof_for_sep_promotion(
        freeze, require_single_candidate=True,
    )
    if not ok:
        result.notes.extend(notes)
        result.candidate_count = pcof_candidate_count(freeze)
        actionable = actionable_pcof_entity_names(freeze)
        if len(actionable) > 1 and not pcof_best_dominates_actionables(freeze, actionable):
            result.needs_clarification = True
        return result

    coll = _collection_from_pcof_freeze(freeze)
    best = freeze.best_entity_candidate
    if not coll and best:
        coll = _synthetic_collection_from_pcof_best(freeze)
    if not coll or not best:
        result.notes.append("pcof_collection_build_failed")
        return result

    ent_ref: Optional[CollectionEntityReference] = None
    for ent in coll.entities:
        if ent.display_name == best.display_name:
            ent_ref = ent
            break
    if ent_ref is None:
        ent_ref = CollectionEntityReference(
            entity_type=OperationalEntityType.UNKNOWN,
            display_name=best.display_name,
            normalized_name=best.normalized_name or normalize_entity_name(best.display_name),
            selection_confidence=float(best.confidence or freeze.freeze_confidence),
            evidence=OperationalEntityEvidence(
                uia_name=best.uia_name or best.display_name,
                ocr_text=best.ocr_text or "",
                target_identity_confidence=float(freeze.freeze_confidence),
                execution_truth_confirmed=True,
                semantic_context={"pcof": True},
            ),
        )
        coll.entities.append(ent_ref)

    actionable = actionable_pcof_entity_names(freeze)
    cand = len(actionable) if actionable else pcof_candidate_count(freeze)
    result.candidate_count = cand
    ambiguous = cand > 1 and not pcof_best_dominates_actionables(freeze, actionable)
    result.needs_clarification = ambiguous
    if result.needs_clarification:
        result.collection = coll
        result.confidence = float(freeze.freeze_confidence)
        result.notes.append("pcof_ambiguous_candidates")
        return result

    bundle = freeze_to_capture_bundle(freeze)
    op_ent = extract_operational_entity(
        capture_bundle={
            **bundle,
            "execution_truth_confirmed": True,
            "sufficient_for_execution": True,
        },
        selection_intent="pcof_entity_in_collection",
        trusted_pre_action_identity=True,
        sufficient_for_execution=True,
        unique_candidate_count=1,
        store_on_success=False,
    )
    if op_ent is None:
        op_ent = OperationalEntity(
            entity_type=OperationalEntityType.UNKNOWN,
            display_name=best.display_name,
            normalized_name=best.normalized_name or normalize_entity_name(best.display_name),
            confidence=float(freeze.freeze_confidence),
            evidence=OperationalEntityEvidence(
                uia_name=best.display_name,
                ocr_text=best.ocr_text or "",
                target_identity_confidence=float(freeze.freeze_confidence),
                execution_truth_confirmed=True,
            ),
            resolution_strategies=["pcof_pre_transition"],
        )

    result.selected = True
    result.collection = coll
    result.entity = ent_ref
    result.operational_entity = op_ent
    result.confidence = max(float(freeze.freeze_confidence), float(best.confidence or 0))
    result.notes.append("pcof_primary_intent")
    return result


def build_semantic_params_from_pcof_freeze(
    freeze: Any,
    intent: EntitySelectionIntentResult,
    *,
    legacy_type: str = "",
    source_event_id: str = "",
) -> Dict[str, Any]:
    """Params SEP canónicos derivados de PCOF."""
    cand = int(intent.candidate_count or 1)
    params: Dict[str, Any] = {
        "operational_freeze_snapshot_ref": freeze.freeze_id,
        "selection_strategy": "pre_click_operational_freeze",
        "validator_expectation": "outcome_transition_context_changed",
        "needs_clarification": bool(intent.needs_clarification),
        "candidate_count": cand,
        "collection_candidate_count": cand,
        "selection_confidence": float(freeze.freeze_confidence),
        "collection_selection_confidence": float(freeze.freeze_confidence),
        "freeze_quality": str(freeze.freeze_quality or ""),
        "transition_risk": getattr(
            freeze.transition_risk, "value", str(freeze.transition_risk),
        ),
        "freeze_confidence": float(freeze.freeze_confidence),
        "pre_transition_confirmed": bool(freeze.pre_transition_confirmed),
        "pcof_primary": True,
        "execution_truth_confirmed": True,
    }
    if source_event_id:
        params["source_event_id"] = source_event_id
    if legacy_type:
        params["fallback_legacy_type"] = legacy_type
    attach_collection_context_to_step_params(params, intent)
    if intent.operational_entity:
        params["operational_entity"] = intent.operational_entity.model_dump(mode="json")
    if intent.entity:
        params["selection_label"] = intent.entity.display_name
    params["operational_freeze_snapshot"] = freeze.model_dump(mode="json")
    return params


def find_pcof_freeze_for_legacy_step(
    mission: Any,
    *,
    legacy_type: str,
    params: Dict[str, Any],
) -> Optional[Any]:
    """Localiza el snapshot PCOF del click asociado al paso legacy."""
    label = _legacy_label_from_step_params(params)
    ev = _raw_event_for_uces(mission, click_label=label)
    if ev is None:
        trace = list(getattr(mission, "raw_trace", None) or [])
        for candidate in reversed(trace):
            meta = dict(getattr(candidate, "metadata", None) or {})
            if meta.get("operational_freeze_snapshot"):
                ev = candidate
                break
    if ev is None:
        return None
    meta = dict(ev.metadata or {})
    return _parse_pcof_snapshot(meta.get("operational_freeze_snapshot"))


def should_promote_pcof_freeze(freeze: Any) -> bool:
    try:
        from app.services.missions.freeze_first_execution_core import (
            promotion_gate_reasons,
        )
        ok_ffec, _ = promotion_gate_reasons(freeze)
        if ok_ffec:
            return True
    except Exception:
        pass
    from app.services.runtime.pre_click_operational_freeze import (
        qualifies_pcof_for_sep_promotion,
    )

    ok, _ = qualifies_pcof_for_sep_promotion(freeze, require_single_candidate=True)
    return ok


def find_strongest_pcof_in_trace(
    mission: Any,
) -> Tuple[Optional[Any], Optional[RawEvent]]:
    """Mejor freeze PCOF del ``raw_trace`` para promoción SEP universal."""
    from app.contracts.mission import EventType
    from app.services.runtime.pre_click_operational_freeze import (
        qualifies_pcof_for_sep_promotion,
    )

    best_fr = None
    best_ev: Optional[RawEvent] = None
    best_score = -1.0
    for ev in getattr(mission, "raw_trace", None) or []:
        if getattr(ev, "event_type", None) != EventType.MOUSE_CLICK:
            continue
        meta = dict(getattr(ev, "metadata", None) or {})
        fr = _parse_pcof_snapshot(meta.get("operational_freeze_snapshot"))
        if fr is None:
            continue
        ok, _ = qualifies_pcof_for_sep_promotion(fr, require_single_candidate=True)
        if not ok:
            continue
        score = float(fr.freeze_confidence or 0)
        if fr.pre_transition_confirmed:
            score += 0.5
        if score > best_score:
            best_score = score
            best_fr = fr
            best_ev = ev
    return best_fr, best_ev


def _promote_semantic_step_from_pcof(
    step: Any,
    freeze: Any,
    raw_event: Optional[RawEvent],
    *,
    promotions: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Convierte un paso legacy (o existente) en ``select_entity_from_collection`` vía PCOF."""
    from app.services.missions.semantic_execution_plan import SemanticPlanStep

    legacy_type = str(getattr(step, "type", "") or "").strip()
    if legacy_type == UNIVERSAL_SELECT_KIND:
        legacy_type = str((getattr(step, "params", None) or {}).get("fallback_legacy_type") or "")

    pcof_intent = intent_from_pcof_freeze(freeze)
    if not pcof_intent.selected or not pcof_intent.collection or not pcof_intent.entity:
        return step

    new_params = build_semantic_params_from_pcof_freeze(
        freeze,
        pcof_intent,
        legacy_type=legacy_type or "select_profile",
        source_event_id=str(getattr(raw_event, "id", "") or ""),
    )
    strat_plan = None
    try:
        from app.services.missions.semantic_execution_plan import _STRATEGY_BY_TYPE
        strat_plan = _STRATEGY_BY_TYPE.get(UNIVERSAL_SELECT_KIND)
    except Exception:
        pass
    promoted = SemanticPlanStep(
        id=getattr(step, "id", str(uuid.uuid4())),
        type=UNIVERSAL_SELECT_KIND,
        params=new_params,
        preferred_strategy=(
            strat_plan.preferred if strat_plan else UCES_PRIMARY_STRATEGY
        ),
        fallback_strategies=list(
            strat_plan.fallbacks if strat_plan else UCES_FALLBACK_STRATEGIES,
        ),
        confidence=max(
            float(getattr(step, "confidence", 0) or 0),
            float(freeze.freeze_confidence),
        ),
        human_label=human_label_for_uces_step(new_params),
        needs_user_label=bool(pcof_intent.needs_clarification),
        label_prompt="",
        block_id=getattr(step, "block_id", None),
    )
    if promotions is not None:
        promotions.append({
            "from": legacy_type or "raw_pcof",
            "to": UNIVERSAL_SELECT_KIND,
            "entity": pcof_intent.entity.display_name,
            "source": "pre_click_operational_freeze",
            "freeze_id": freeze.freeze_id,
            "source_event_id": getattr(raw_event, "id", ""),
            "reason": "universal_pcof_sep_promotion",
        })
    return promoted


def build_semantic_params_from_intent(
    intent: EntitySelectionIntentResult,
    *,
    legacy_type: str = "",
    selection_strategy: str = "uces_compilation",
) -> Dict[str, Any]:
    """Params canónicos para ``select_entity_from_collection`` en el SEP."""
    params: Dict[str, Any] = {
        "selection_strategy": selection_strategy,
        "validator_expectation": "single_visible_choice_resolved",
        "needs_clarification": bool(intent.needs_clarification),
        "candidate_count": int(intent.candidate_count),
        "selection_confidence": float(intent.confidence),
    }
    if legacy_type:
        params["fallback_legacy_type"] = legacy_type
    attach_collection_context_to_step_params(params, intent)
    if intent.entity:
        params["selection_label"] = intent.entity.display_name
    return params


def _legacy_label_from_step_params(params: Dict[str, Any]) -> str:
    for key in (
        "profile_name", "profile", "label", "choice_label",
        "selection_label", "name", "title",
    ):
        v = str(params.get(key) or "").strip()
        if v and not is_generic_entity_label(v):
            return v
    return ""


def _raw_event_for_uces(
    mission: Any,
    *,
    click_label: str = "",
) -> Optional[RawEvent]:
    """Localiza el click más relevante en ``raw_trace`` para UCES."""
    from app.contracts.mission import EventType

    trace = list(getattr(mission, "raw_trace", None) or [])
    label_norm = normalize_entity_name(click_label) if click_label else ""
    best: Optional[RawEvent] = None
    best_score = 0.0

    for ev in reversed(trace):
        if ev.event_type != EventType.MOUSE_CLICK:
            continue
        meta = dict(ev.metadata or {})
        iso = dict(meta.get("target_identity_isolation") or {})
        lbl = str(iso.get("target_label") or "").strip()
        score = 0.5
        if lbl and label_norm:
            score = max(score, _text_similarity(lbl, click_label))
        if score > best_score:
            best_score = score
            best = ev
        if label_norm and lbl and normalize_entity_name(lbl) == label_norm:
            return ev
    return best


def _capture_bundle_for_step(
    mission: Any,
    params: Dict[str, Any],
    raw_event: Optional[RawEvent],
) -> Dict[str, Any]:
    bundle: Dict[str, Any] = {"metadata": {}}
    if raw_event:
        meta = dict(raw_event.metadata or {})
        bundle["metadata"] = meta
        bundle["uia_flat"] = meta.get("uia_flat")
        bundle["uia"] = meta.get("uia")
        bundle["dom"] = meta.get("web") or meta.get("dom")
        iso = dict(meta.get("target_identity_isolation") or {})
        if iso.get("target_label"):
            bundle["target_label"] = iso["target_label"]
    label = _legacy_label_from_step_params(params)
    if label:
        bundle.setdefault("target_label", label)
    try:
        from app.services.missions.execution_truth_engine import (
            is_execution_truth_confirmed,
        )
        bundle["execution_truth_confirmed"] = is_execution_truth_confirmed(mission)
    except Exception:
        bundle["execution_truth_confirmed"] = False
    return bundle


def infer_uces_intent_for_legacy_step(
    mission: Any,
    *,
    legacy_type: str,
    params: Dict[str, Any],
    raw_event: Optional[RawEvent] = None,
) -> EntitySelectionIntentResult:
    """Intenta inferir colección+entidad para un paso legacy del SEP."""
    label = _legacy_label_from_step_params(params)
    ev = raw_event or _raw_event_for_uces(mission, click_label=label)
    bundle = _capture_bundle_for_step(mission, params, ev)
    return build_selection_from_capture(raw_event=ev, capture_bundle=bundle)


def should_promote_legacy_to_uces(intent: EntitySelectionIntentResult) -> bool:
    """Criterio de promoción primaria en compilación."""
    if not intent.selected or intent.needs_clarification:
        return False
    if not intent.collection or not intent.entity:
        return False
    if is_generic_entity_label(intent.entity.display_name):
        return False
    if intent.candidate_count > 1:
        return False
    if intent.confidence < ENTITY_SELECTION_CONFIDENCE_MIN:
        return False
    if float(intent.collection.confidence or 0) < COLLECTION_CONFIDENCE_MIN:
        return False
    return True


def promote_semantic_plan_step(
    step: Any,
    mission: Any,
    *,
    promotions: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Promueve un :class:`SemanticPlanStep` legacy a UCES si hay señal fuerte."""
    from app.services.missions.semantic_execution_plan import SemanticPlanStep

    legacy_type = str(getattr(step, "type", "") or "").strip()
    if legacy_type == UNIVERSAL_SELECT_KIND:
        pars = dict(getattr(step, "params", None) or {})
        if pars.get("operational_collection") and pars.get("collection_entity"):
            conf = float(pars.get("selection_confidence") or 0.0)
            cand = int(pars.get("candidate_count") or 1)
            if conf >= ENTITY_SELECTION_CONFIDENCE_MIN and cand <= 1:
                try:
                    step.needs_user_label = False
                    step.human_label = human_label_for_uces_step(pars)
                except Exception:
                    pass
            return step
        legacy_type = str(pars.get("fallback_legacy_type") or "select_profile")

    if legacy_type not in LEGACY_SELECTION_STEP_TYPES:
        return step

    pars = dict(getattr(step, "params", None) or {})

    # PCOF → SEP (prioridad absoluta sobre UIA post-transición).
    freeze = find_pcof_freeze_for_legacy_step(
        mission, legacy_type=legacy_type, params=pars,
    )
    if freeze is None:
        freeze, _ev = find_strongest_pcof_in_trace(mission)
    else:
        _ev = _raw_event_for_uces(mission, click_label=_legacy_label_from_step_params(pars))
    if freeze is not None and should_promote_pcof_freeze(freeze):
        promoted = _promote_semantic_step_from_pcof(
            step, freeze, _ev, promotions=promotions,
        )
        if getattr(promoted, "type", "") == UNIVERSAL_SELECT_KIND:
            try:
                pcof_intent = intent_from_pcof_freeze(freeze)
                append_collection_audit(mission, {
                    "step_id": promoted.id,
                    "collection_type": (
                        pcof_intent.collection.collection_type.value
                        if pcof_intent.collection else "?"
                    ),
                    "entity_name": (
                        pcof_intent.entity.display_name if pcof_intent.entity else ""
                    ),
                    "confidence": pcof_intent.confidence,
                    "strategy": "pre_click_operational_freeze",
                })
            except Exception:
                pass
            return promoted

    intent = infer_uces_intent_for_legacy_step(
        mission, legacy_type=legacy_type, params=pars,
    )
    if not should_promote_legacy_to_uces(intent):
        return step

    new_params = build_semantic_params_from_intent(
        intent,
        legacy_type=legacy_type,
        selection_strategy="uces_primary_compilation",
    )
    new_params["execution_truth_confirmed"] = bool(
        new_params.get("execution_truth_confirmed")
        or (mission and _capture_bundle_for_step(mission, pars, None).get(
            "execution_truth_confirmed",
        )),
    )

    strat_plan = None
    try:
        from app.services.missions.semantic_execution_plan import _STRATEGY_BY_TYPE
        strat_plan = _STRATEGY_BY_TYPE.get(UNIVERSAL_SELECT_KIND)
    except Exception:
        pass

    promoted = SemanticPlanStep(
        id=getattr(step, "id", str(uuid.uuid4())),
        type=UNIVERSAL_SELECT_KIND,
        params=new_params,
        preferred_strategy=(
            strat_plan.preferred if strat_plan else UCES_PRIMARY_STRATEGY
        ),
        fallback_strategies=list(
            strat_plan.fallbacks if strat_plan else UCES_FALLBACK_STRATEGIES,
        ),
        confidence=max(float(getattr(step, "confidence", 0) or 0), intent.confidence),
        human_label=human_label_for_uces_step(new_params),
        needs_user_label=False,
        label_prompt="",
        block_id=getattr(step, "block_id", None),
    )

    if promotions is not None:
        promotions.append({
            "from": legacy_type,
            "to": UNIVERSAL_SELECT_KIND,
            "entity": intent.entity.display_name if intent.entity else "",
            "collection_type": (
                intent.collection.collection_type.value
                if intent.collection else ""
            ),
            "confidence": round(intent.confidence, 4),
            "candidate_count": intent.candidate_count,
            "reason": "uces_primary_compilation",
        })

    try:
        append_collection_audit(mission, {
            "step_id": promoted.id,
            "collection_type": (
                intent.collection.collection_type.value
                if intent.collection else "?"
            ),
            "entity_name": intent.entity.display_name if intent.entity else "",
            "confidence": intent.confidence,
            "strategy": "uces_primary_compilation",
        })
    except Exception:
        pass

    return promoted


def apply_universal_pcof_sep_promotion(
    plan: Any,
    mission: Any,
    *,
    promotions: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Promueve cualquier paso legacy de selección si hay PCOF fuerte en el trace."""
    freeze, ev = find_strongest_pcof_in_trace(mission)
    if freeze is None or not should_promote_pcof_freeze(freeze):
        return 0
    steps = getattr(plan, "steps", None)
    if not isinstance(steps, list):
        return 0
    promo_log = promotions if promotions is not None else []
    count = 0
    for i, sp in enumerate(steps):
        stype = str(getattr(sp, "type", "") or "")
        if stype not in LEGACY_SELECTION_STEP_TYPES and stype != UNIVERSAL_SELECT_KIND:
            continue
        if stype == UNIVERSAL_SELECT_KIND:
            pars = dict(getattr(sp, "params", None) or {})
            if pars.get("pcof_primary") and pars.get("pre_transition_confirmed"):
                continue
        promoted = _promote_semantic_step_from_pcof(sp, freeze, ev, promotions=promo_log)
        if getattr(promoted, "type", "") == UNIVERSAL_SELECT_KIND:
            steps[i] = promoted
            count += 1
    return count


def apply_uces_primary_compilation(
    plan: Any,
    mission: Any,
    *,
    promotions: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Recorre el SEP y promueve pasos legacy con UCES fuerte (mutación in-place)."""
    steps = getattr(plan, "steps", None)
    if not isinstance(steps, list) or not steps:
        return 0
    promo_log = promotions if promotions is not None else []
    count = apply_universal_pcof_sep_promotion(plan, mission, promotions=promo_log)
    for i, sp in enumerate(steps):
        new_sp = promote_semantic_plan_step(sp, mission, promotions=promo_log)
        if getattr(new_sp, "type", "") == UNIVERSAL_SELECT_KIND and getattr(
            sp, "type", "",
        ) != UNIVERSAL_SELECT_KIND:
            steps[i] = new_sp
            count += 1
        elif getattr(new_sp, "type", "") == UNIVERSAL_SELECT_KIND:
            steps[i] = new_sp
    return count


def enrich_truth_block_with_collection_entity(
    tb: Any,
    mission: Any,
    *,
    first_ev: Optional[RawEvent] = None,
) -> None:
    """Enriquece ``OperationalTruthBlock`` select_entity con payload UCES."""
    if str(getattr(tb, "truth_type", "") or "") != "select_entity":
        return
    inj = dict((tb.context_window or {}).get("tel_derived_params") or {})
    if inj.get("operational_collection") and inj.get("collection_entity"):
        return
    if first_ev is not None:
        fr = _parse_pcof_snapshot(
            (dict(first_ev.metadata or {})).get("operational_freeze_snapshot"),
        )
        if fr is not None and should_promote_pcof_freeze(fr):
            pcof_intent = intent_from_pcof_freeze(fr)
            if pcof_intent.selected and pcof_intent.collection and pcof_intent.entity:
                inj.update(
                    build_semantic_params_from_pcof_freeze(
                        fr, pcof_intent, legacy_type="select_entity",
                    ),
                )
                cw = dict(tb.context_window or {})
                cw["tel_derived_params"] = inj
                cw["pcof_truth"] = {
                    "pcof_frozen_before_transition": True,
                    "freeze_confidence": float(fr.freeze_confidence),
                }
                try:
                    tb.context_window = cw
                except Exception:
                    pass
                return
    label = (
        inj.get("label")
        or inj.get("selection_label")
        or (tb.target_snapshot or {}).get("target_label")
        or ""
    )
    pars_stub = {"label": label, **inj}
    intent = infer_uces_intent_for_legacy_step(
        mission, legacy_type="select_entity", params=pars_stub, raw_event=first_ev,
    )
    if not intent.collection and not intent.entity:
        return
    if intent.collection:
        inj["operational_collection"] = intent.collection.model_dump(mode="json")
        inj["collection"] = inj["operational_collection"]
    if intent.entity:
        inj["collection_entity"] = intent.entity.model_dump(mode="json")
        inj["entity"] = inj["collection_entity"]
        inj["selection_label"] = intent.entity.display_name
    inj["selection_context"] = {
        "candidate_count": intent.candidate_count,
        "selection_confidence": intent.confidence,
        "needs_clarification": intent.needs_clarification,
    }
    if intent.operational_entity:
        inj["operational_entity"] = intent.operational_entity.model_dump(mode="json")
    cw = dict(tb.context_window or {})
    cw["tel_derived_params"] = inj
    try:
        tb.context_window = cw
    except Exception:
        pass


def compute_uces_ready_blockers(step: Any) -> List[str]:
    """Blockers READY específicos de ``select_entity_from_collection``."""
    if str(getattr(step, "type", "") or "") != UNIVERSAL_SELECT_KIND:
        return []
    try:
        from app.services.missions.semantic_execution_plan import ReadyBlockerCode
    except Exception:
        return []

    pars = getattr(step, "params", None) or {}
    if not isinstance(pars, dict):
        return [ReadyBlockerCode.SEMANTIC_INTENT_UNDER_SPECIFIED]

    if pars.get("pcof_primary") and pars.get("pre_transition_confirmed"):
        cand = int(pars.get("candidate_count") or 99)
        conf = float(pars.get("freeze_confidence") or pars.get("selection_confidence") or 0.0)
        ent_raw = pars.get("collection_entity") or pars.get("entity")
        name = ""
        if isinstance(ent_raw, dict):
            name = str(ent_raw.get("display_name") or "").strip()
        if (
            cand <= 1
            and conf >= ENTITY_SELECTION_CONFIDENCE_MIN
            and name
            and not is_generic_entity_label(name)
            and not bool(pars.get("needs_clarification"))
        ):
            return []

    blockers: List[str] = []
    cand = int(pars.get("candidate_count") or pars.get("collection_candidate_count") or 1)
    conf = float(pars.get("selection_confidence") or pars.get("collection_selection_confidence") or 0.0)
    needs_clar = bool(pars.get("needs_clarification")) or bool(getattr(step, "needs_user_label", False))

    ent_raw = pars.get("collection_entity") or pars.get("entity")
    name = ""
    if isinstance(ent_raw, dict):
        name = str(ent_raw.get("display_name") or "").strip()

    if needs_clar or cand > 1:
        blockers.append(ReadyBlockerCode.COLLECTION_ENTITY_NEEDS_CLARIFICATION)
    if not name or is_generic_entity_label(name):
        blockers.append(ReadyBlockerCode.COLLECTION_ENTITY_WEAK)
    if conf < ENTITY_SELECTION_CONFIDENCE_MIN and cand <= 1:
        blockers.append(ReadyBlockerCode.COLLECTION_ENTITY_WEAK)
    coll_raw = pars.get("operational_collection") or pars.get("collection")
    if not coll_raw:
        blockers.append(ReadyBlockerCode.COLLECTION_ENTITY_WEAK)
    return blockers


__all__ = [
    "COLLECTION_MEMORY_VERSION",
    "UNIVERSAL_SELECT_KIND",
    "CollectionMatchResult",
    "CollectionMemoryStore",
    "EntityInCollectionMatchResult",
    "EntitySelectionIntentResult",
    "LEGACY_SELECTION_STEP_TYPES",
    "UCES_FALLBACK_STRATEGIES",
    "UCES_PRIMARY_STRATEGY",
    "append_collection_audit",
    "apply_collection_entity_ready_relief",
    "apply_pcof_ready_relief",
    "apply_universal_pcof_sep_promotion",
    "apply_uces_primary_compilation",
    "find_strongest_pcof_in_trace",
    "build_semantic_params_from_pcof_freeze",
    "find_pcof_freeze_for_legacy_step",
    "intent_from_pcof_freeze",
    "should_promote_pcof_freeze",
    "attach_collection_context_to_step_params",
    "build_selection_from_capture",
    "build_semantic_params_from_intent",
    "compute_uces_ready_blockers",
    "enrich_truth_block_with_collection_entity",
    "human_label_for_uces_step",
    "infer_uces_intent_for_legacy_step",
    "promote_semantic_plan_step",
    "should_promote_legacy_to_uces",
    "collection_entity_status_label_for_mission",
    "detect_operational_collections",
    "expert_audit_lines",
    "extract_entities_from_collection",
    "infer_entity_selection_intent",
    "is_universal_collection_step",
    "match_collection_runtime",
    "match_entity_inside_collection",
    "mission_has_strong_collection_selection",
    "preserve_collection_continuity",
    "sanitize_human_label_uces",
    "try_collection_entity_first_runtime",
    "universalize_step_kind",
]
