"""
Live Operational Navigation Engine (LONE)
-----------------------------------------
Navega espacios operacionales vivos hasta reencontrar entidades cuando el
target ya no es visible (lazy load, scroll, virtualización, re-render).

Complementa UCES/ERL/ARL/SLL/OMPC/LOP; no sustituye SmartExecutor.
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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    CollectionEntityReference,
    OperationalCollection,
    OperationalEntity,
    OperationalNavigationAction,
    OperationalNavigationEdge,
    OperationalNavigationGoal,
    OperationalNavigationNode,
)
from app.core.logger import log
from app.core.paths import VAR_DIR
from app.services.runtime.entity_resolution_layer import (
    normalize_entity_name,
    is_generic_entity_label,
)

NAVIGATION_MEMORY_VERSION = 1
_NAV_DIR = VAR_DIR / "navigation_memory"
_NAV_DB = _NAV_DIR / f"navigation_memory_v{NAVIGATION_MEMORY_VERSION}.sqlite"

DEFAULT_MAX_DEPTH = 12
DEFAULT_WAIT_MS = 400
ENTITY_MATCH_MIN = 0.78
CONTINUITY_NAV_MIN = 0.68
MEMORY_ROUTE_BOOST = 0.12

# Affordance markers (universal, no app-specific)
_AFFORDANCE_SCROLL = frozenset({
    "scroll", "scrollable", "scrollviewer", "scrollbar", "overflow",
})
_AFFORDANCE_PAGINATE = frozenset({
    "next", "previous", "pager", "pagination", "page", "siguiente", "anterior",
})
_AFFORDANCE_TAB = frozenset({"tab", "tabitem", "tabstrip", "pestaña"})
_AFFORDANCE_SEARCH = frozenset({
    "search", "buscar", "filter", "filtro", "query", "find",
})
_AFFORDANCE_EXPAND = frozenset({"expand", "collapse", "accordion", "more"})
_AFFORDANCE_RETRY = frozenset({
    "retry", "reload", "refresh", "reintentar", "try again", "cargar",
})
_AFFORDANCE_LOADING = frozenset({
    "loading", "skeleton", "spinner", "progress", "cargando", "please wait",
    "shimmer", "placeholder",
})

UNSAFE_NAV_MARKERS: frozenset = frozenset({
    "pay", "payment", "pago", "transfer", "delete", "eliminar", "checkout",
    "password", "auth", "sign in", "wire", "destructive", "borrar",
})

NORMAL_STATUS_RECOVERING = "Reencontrando elemento…"


@dataclass
class DynamicSurfaceState:
    """Fase 6 — estados dinámicos de superficie."""

    has_skeleton: bool = False
    has_loading: bool = False
    has_empty_transient: bool = False
    likely_virtualized: bool = False
    async_reload_suspected: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def needs_wait(self) -> bool:
        return self.has_skeleton or self.has_loading or self.async_reload_suspected


@dataclass
class EntityLiveSearchResult:
    """Resultado de búsqueda en espacio vivo."""

    found: bool = False
    entity: Optional[CollectionEntityReference] = None
    collection: Optional[OperationalCollection] = None
    node_id: str = ""
    confidence: float = 0.0
    strategy: str = ""
    candidate_count: int = 0
    region_hint: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class NavigationPlan:
    """Plan de acciones navegacionales."""

    actions: List[OperationalNavigationAction] = field(default_factory=list)
    confidence: float = 0.0
    rationale: str = ""
    memory_backed: bool = False


@dataclass
class OperationalNavigationGraph:
    """ONG — grafo de estados operacionales observados."""

    nodes: Dict[str, OperationalNavigationNode] = field(default_factory=dict)
    edges: List[OperationalNavigationEdge] = field(default_factory=list)
    goal: Optional[OperationalNavigationGoal] = None
    current_node_id: str = ""

    def add_node(self, node: OperationalNavigationNode) -> None:
        self.nodes[node.node_id] = node

    def add_edge(self, edge: OperationalNavigationEdge) -> None:
        self.edges.append(edge)


@dataclass
class NavigationLoopResult:
    """Resultado del loop adaptativo."""

    success: bool = False
    entity: Optional[CollectionEntityReference] = None
    collection: Optional[OperationalCollection] = None
    confidence: float = 0.0
    depth_reached: int = 0
    stopped_reason: str = ""
    actions_executed: List[str] = field(default_factory=list)
    request_human: bool = False
    human_message: str = ""
    graph: Optional[OperationalNavigationGraph] = None


# ── Helpers ─────────────────────────────────────────────────────────────


def _lower(s: Any) -> str:
    return str(s or "").strip().lower()


def _text_sim(a: str, b: str) -> float:
    na, nb = normalize_entity_name(a), normalize_entity_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = float(SequenceMatcher(None, na, nb).ratio())
    # Penalizar sufijos numéricos distintos (listas virtualizadas / scroll).
    ma = re.search(r"(\d+)\s*$", na)
    mb = re.search(r"(\d+)\s*$", nb)
    if ma and mb and ma.group(1) != mb.group(1):
        ratio *= 0.45
    return ratio


def _viewport_signature(
    *,
    width: int = 0,
    height: int = 0,
    scroll_y: int = 0,
    entity_sample: Sequence[str] = (),
) -> str:
    sample = "|".join(sorted(normalize_entity_name(n) for n in entity_sample if n)[:12])
    payload = f"{width}x{height}|sy={scroll_y}|{sample}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _structural_hash(collection: OperationalCollection) -> str:
    sig = dict(collection.structural_signals or {})
    return str(sig.get("structural_hash") or collection.collection_id[:16])


# ── Fase 7: Navigation memory ───────────────────────────────────────────


class NavigationMemoryStore:
    """SQLite: rutas exitosas, secuencias, recovery paths."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db = Path(db_path) if db_path else _NAV_DB
        self._lock = threading.RLock()
        try:
            _NAV_DIR.mkdir(parents=True, exist_ok=True)
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
                    CREATE TABLE IF NOT EXISTS nav_route_success (
                        route_key TEXT NOT NULL,
                        action_sequence TEXT NOT NULL,
                        success_count INTEGER NOT NULL DEFAULT 0,
                        last_success REAL NOT NULL,
                        avg_depth REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (route_key, action_sequence)
                    );
                    CREATE TABLE IF NOT EXISTS nav_recovery_path (
                        collection_sig TEXT NOT NULL,
                        entity_normalized TEXT NOT NULL,
                        action_sequence TEXT NOT NULL,
                        success_rate REAL NOT NULL DEFAULT 0,
                        last_used REAL NOT NULL,
                        PRIMARY KEY (collection_sig, entity_normalized, action_sequence)
                    );
                    CREATE TABLE IF NOT EXISTS nav_viewport_pattern (
                        viewport_sig TEXT NOT NULL,
                        collection_sig TEXT NOT NULL,
                        entity_found INTEGER NOT NULL DEFAULT 0,
                        hit_count INTEGER NOT NULL DEFAULT 0,
                        last_hit REAL NOT NULL,
                        PRIMARY KEY (viewport_sig, collection_sig, entity_found)
                    );
                    """
                )
                c.commit()
            finally:
                c.close()

    def record_route_success(
        self,
        route_key: str,
        actions: Sequence[str],
        *,
        depth: int = 0,
    ) -> None:
        if not route_key or not actions:
            return
        seq = json.dumps(list(actions), ensure_ascii=False)
        now = time.time()
        with self._lock:
            c = self._connect()
            try:
                c.execute(
                    """
                    INSERT INTO nav_route_success
                        (route_key, action_sequence, success_count, last_success, avg_depth)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(route_key, action_sequence) DO UPDATE SET
                        success_count = success_count + 1,
                        last_success = excluded.last_success,
                        avg_depth = (avg_depth + excluded.avg_depth) / 2.0
                    """,
                    (route_key, seq, now, float(depth)),
                )
                c.commit()
            finally:
                c.close()

    def best_recovery_sequence(
        self,
        collection_sig: str,
        entity_normalized: str,
    ) -> Tuple[List[str], float]:
        if not collection_sig or not entity_normalized:
            return [], 0.0
        with self._lock:
            c = self._connect()
            try:
                row = c.execute(
                    """
                    SELECT action_sequence, success_rate FROM nav_recovery_path
                    WHERE collection_sig = ? AND entity_normalized = ?
                    ORDER BY success_rate DESC, last_used DESC LIMIT 1
                    """,
                    (collection_sig, entity_normalized),
                ).fetchone()
            finally:
                c.close()
        if not row:
            return [], 0.0
        try:
            actions = json.loads(row[0])
            return [str(a) for a in actions], float(row[1] or 0)
        except Exception:
            return [], 0.0

    def record_recovery_path(
        self,
        collection_sig: str,
        entity_normalized: str,
        actions: Sequence[str],
        *,
        success: bool,
    ) -> None:
        if not collection_sig or not entity_normalized or not actions:
            return
        seq = json.dumps(list(actions), ensure_ascii=False)
        now = time.time()
        rate = 1.0 if success else 0.0
        with self._lock:
            c = self._connect()
            try:
                c.execute(
                    """
                    INSERT INTO nav_recovery_path
                        (collection_sig, entity_normalized, action_sequence,
                         success_rate, last_used)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(collection_sig, entity_normalized, action_sequence)
                    DO UPDATE SET
                        success_rate = (success_rate + excluded.success_rate) / 2.0,
                        last_used = excluded.last_used
                    """,
                    (collection_sig, entity_normalized, seq, rate, now),
                )
                c.commit()
            finally:
                c.close()


# ── Fase 6: Dynamic content ─────────────────────────────────────────────


def detect_dynamic_surface_state(
    *,
    uia_flat: Optional[Sequence[Dict[str, Any]]] = None,
    ocr_snippets: Optional[Sequence[str]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> DynamicSurfaceState:
    """Detecta skeleton, loading, virtualización, reload async."""
    state = DynamicSurfaceState()
    ctx = dict(context or {})
    hay = " ".join(_lower(x) for x in (ocr_snippets or [])[:40])
    nodes = list(uia_flat or [])

    for n in nodes:
        role = _lower(n.get("role") or n.get("control_type"))
        name = _lower(n.get("name") or n.get("label"))
        aid = _lower(n.get("automation_id"))
        blob = f"{role} {name} {aid}"
        if any(m in blob for m in _AFFORDANCE_LOADING):
            if "skeleton" in blob or "shimmer" in blob or "placeholder" in blob:
                state.has_skeleton = True
                state.notes.append("skeleton_ui")
            else:
                state.has_loading = True
                state.notes.append("loading_ui")
        if role in ("scrollbar", "scrollviewer") and int(n.get("scrollable_height") or 0) > 2000:
            state.likely_virtualized = True
            state.notes.append("tall_scroll_surface")

    if ctx.get("empty_results") or ctx.get("transient_empty"):
        state.has_empty_transient = True
        state.notes.append("transient_empty")

    if ctx.get("dom_mutated") or ctx.get("async_reload"):
        state.async_reload_suspected = True
        state.notes.append("async_reload")

    if any(m in hay for m in _AFFORDANCE_LOADING):
        state.has_loading = True

    return state


# ── Fase 8: Affordances ─────────────────────────────────────────────────


def detect_navigation_affordances(
    *,
    uia_flat: Optional[Sequence[Dict[str, Any]]] = None,
    collections: Optional[Sequence[OperationalCollection]] = None,
    dynamic: Optional[DynamicSurfaceState] = None,
) -> List[str]:
    """Affordances universales detectadas en superficie viva."""
    found: List[str] = []
    nodes = list(uia_flat or [])

    for coll in collections or []:
        if coll.scrollable:
            found.append(OperationalNavigationAction.SCROLL_DOWN.value)

    for n in nodes:
        role = _lower(n.get("role") or n.get("control_type"))
        name = _lower(n.get("name") or n.get("label"))
        blob = f"{role} {name}"
        if any(m in blob for m in _AFFORDANCE_SCROLL) or role == "scrollbar":
            if OperationalNavigationAction.SCROLL_DOWN.value not in found:
                found.append(OperationalNavigationAction.SCROLL_DOWN.value)
        if any(m in blob for m in _AFFORDANCE_PAGINATE):
            if "next" in blob or "siguiente" in blob:
                found.append(OperationalNavigationAction.PAGINATE_NEXT.value)
            if "prev" in blob or "anterior" in blob:
                found.append(OperationalNavigationAction.PAGINATE_PREVIOUS.value)
        if any(m in blob for m in _AFFORDANCE_TAB):
            found.append(OperationalNavigationAction.SWITCH_TAB.value)
        if any(m in blob for m in _AFFORDANCE_SEARCH):
            found.append(OperationalNavigationAction.FOCUS_SEARCH.value)
        if any(m in blob for m in _AFFORDANCE_EXPAND):
            found.append(OperationalNavigationAction.EXPAND_SECTION.value)
        if any(m in blob for m in _AFFORDANCE_RETRY):
            found.append(OperationalNavigationAction.RETRY_LOAD.value)

    dyn = dynamic or detect_dynamic_surface_state(uia_flat=nodes)
    if dyn.needs_wait:
        found.append(OperationalNavigationAction.WAIT_DYNAMIC_CONTENT.value)
    if dyn.likely_virtualized and OperationalNavigationAction.SCROLL_DOWN.value not in found:
        found.append(OperationalNavigationAction.SCROLL_DOWN.value)

    return list(dict.fromkeys(found))


# ── Fase 11: Safety ─────────────────────────────────────────────────────


def is_unsafe_navigation_context(
    *,
    step: Any = None,
    entity: Optional[OperationalEntity] = None,
    uia_flat: Optional[Sequence[Dict[str, Any]]] = None,
) -> Tuple[bool, str]:
    """Bloquea navegación automática en contextos destructivos/sensibles."""
    if step is not None:
        kind = _lower(getattr(step, "kind", ""))
        if any(m in kind for m in UNSAFE_NAV_MARKERS):
            return True, "unsafe_step_kind"

    if entity is not None:
        blob = _lower(entity.display_name) + " " + _lower(entity.operational_role)
        if any(m in blob for m in UNSAFE_NAV_MARKERS):
            return True, "unsafe_entity_label"

    for n in uia_flat or []:
        name = _lower(n.get("name") or n.get("label"))
        if any(m in name for m in UNSAFE_NAV_MARKERS):
            return True, "unsafe_visible_control"

    return False, ""


# ── Fase 1: ONG ─────────────────────────────────────────────────────────


def build_live_operational_navigation_graph(
    *,
    collections: Sequence[OperationalCollection],
    uia_flat: Optional[Sequence[Dict[str, Any]]] = None,
    goal: Optional[OperationalNavigationGoal] = None,
    context: Optional[Dict[str, Any]] = None,
    previous_graph: Optional[OperationalNavigationGraph] = None,
) -> OperationalNavigationGraph:
    """Construye/actualiza ONG desde colecciones UCES + percepción viva."""
    ctx = dict(context or {})
    graph = previous_graph or OperationalNavigationGraph(goal=goal)
    graph.goal = goal or graph.goal

    seed = ctx.get("pcof_initial_ong_node")
    if isinstance(seed, dict) and not graph.nodes:
        try:
            seed_node = OperationalNavigationNode.model_validate(seed)
            graph.add_node(seed_node)
            graph.current_node_id = seed_node.node_id
        except Exception as exc:
            log.debug("[LONE] pcof seed node skipped: %s", exc)

    dynamic = detect_dynamic_surface_state(
        uia_flat=uia_flat,
        ocr_snippets=ctx.get("ocr_snippets"),
        context=ctx,
    )
    affordances = detect_navigation_affordances(
        uia_flat=uia_flat,
        collections=collections,
        dynamic=dynamic,
    )

    width = int(ctx.get("viewport_width") or 0)
    height = int(ctx.get("viewport_height") or 0)
    scroll_y = int(ctx.get("scroll_y") or 0)

    for coll in collections:
        names = [e.display_name for e in (coll.entities or []) if e.display_name]
        vp_sig = _viewport_signature(
            width=width,
            height=height,
            scroll_y=scroll_y,
            entity_sample=names,
        )
        node = OperationalNavigationNode(
            node_id=str(uuid.uuid4()),
            collection_signature=_structural_hash(coll),
            visible_entities=names[:48],
            viewport_signature=vp_sig,
            active_context={
                "collection_id": coll.collection_id,
                "collection_type": coll.collection_type.value,
                "scrollable": coll.scrollable,
                "dynamic": dynamic.notes[:8],
            },
            navigation_affordances=affordances,
            continuity_score=float(coll.confidence or 0.0),
            structural_hash=_structural_hash(coll),
        )
        graph.add_node(node)

        if graph.current_node_id and graph.current_node_id in graph.nodes:
            edge = OperationalNavigationEdge(
                source_node=graph.current_node_id,
                target_node=node.node_id,
                navigation_action=OperationalNavigationAction.UNKNOWN,
                confidence=0.5,
                observed_effect="observe",
            )
            graph.add_edge(edge)

        graph.current_node_id = node.node_id

    return graph


# ── Fase 2: Entity live search ──────────────────────────────────────────


def search_entity_in_live_operational_space(
    *,
    target_entity: OperationalEntity,
    target_collection: Optional[OperationalCollection],
    graph: OperationalNavigationGraph,
    collections: Sequence[OperationalCollection],
    memory: Optional[NavigationMemoryStore] = None,
) -> EntityLiveSearchResult:
    """Busca entidad por continuidad, similitud y memoria — no coords exactos."""
    result = EntityLiveSearchResult()
    norm_target = normalize_entity_name(target_entity.display_name)
    if not norm_target or is_generic_entity_label(target_entity.display_name):
        result.notes.append("generic_target")
        return result

    coll_sig = ""
    if target_collection:
        coll_sig = _structural_hash(target_collection)

    store = memory or NavigationMemoryStore()
    mem_actions, mem_rate = store.best_recovery_sequence(coll_sig, norm_target)
    if mem_actions and mem_rate >= 0.4:
        result.notes.append(f"memory_route:{mem_rate:.2f}")

    best_score = 0.0
    best_ent: Optional[CollectionEntityReference] = None
    best_coll: Optional[OperationalCollection] = None
    best_node = ""
    candidates = 0

    for coll in collections:
        cont_boost = 0.0
        if target_collection and _structural_hash(coll) == coll_sig:
            cont_boost = 0.15
        for ent in coll.entities or []:
            sim = _text_sim(target_entity.display_name, ent.display_name)
            score = sim + cont_boost
            if ent.normalized_name and ent.normalized_name == norm_target:
                score = max(score, 0.98)
            if score >= 0.55:
                candidates += 1
            if score > best_score:
                best_score = score
                best_ent = ent
                best_coll = coll

    # Buscar en nodos ONG (entidades vistas recientemente)
    for nid, node in graph.nodes.items():
        for vis in node.visible_entities:
            sim = _text_sim(target_entity.display_name, vis)
            if sim > best_score:
                best_score = sim
                best_node = nid
                result.region_hint = f"ong_node:{nid[:8]}"

    if best_ent and best_score >= ENTITY_MATCH_MIN:
        result.found = True
        result.entity = best_ent
        result.collection = best_coll
        result.confidence = min(1.0, best_score + mem_rate * MEMORY_ROUTE_BOOST)
        result.strategy = "live_fuzzy_match" if best_score < 0.95 else "exact_visible"
        result.candidate_count = candidates
        result.node_id = best_node
        return result

    # Continuidad estructural sin entidad visible aún
    if target_collection:
        from app.services.runtime.universal_collection_entity_engine import (
            preserve_collection_continuity,
        )
        matched, score = preserve_collection_continuity(target_collection, collections)
        if matched and score >= CONTINUITY_NAV_MIN:
            result.confidence = score * 0.85
            result.strategy = "collection_continuity_only"
            result.notes.append("collection_present_entity_not_visible")
            result.collection = matched

    result.candidate_count = candidates
    return result


# ── Fase 3: Navigation planning ─────────────────────────────────────────


def plan_navigation_actions(
    *,
    search: EntityLiveSearchResult,
    graph: OperationalNavigationGraph,
    dynamic: DynamicSurfaceState,
    affordances: Sequence[str],
    memory: Optional[NavigationMemoryStore] = None,
    goal: Optional[OperationalNavigationGoal] = None,
) -> NavigationPlan:
    """Planifica scroll/paginate/wait/reopen según affordances e historial."""
    plan = NavigationPlan()
    g = goal or graph.goal
    coll_sig = ""
    ent_norm = ""
    if g:
        ent_norm = normalize_entity_name(
            str(g.target_operational_context.get("display_name") or ""),
        )
        coll_sig = str(g.target_operational_context.get("collection_sig") or "")

    store = memory or NavigationMemoryStore()
    mem_seq, mem_rate = store.best_recovery_sequence(coll_sig, ent_norm)
    valid_actions = {e.value for e in OperationalNavigationAction}
    if mem_seq and mem_rate >= 0.45:
        plan.actions = [
            OperationalNavigationAction(a)
            for a in mem_seq
            if a in valid_actions
        ]
        if plan.actions:
            plan.confidence = mem_rate
            plan.rationale = "navigation_memory"
            plan.memory_backed = True
            return plan

    if dynamic.needs_wait:
        plan.actions = [OperationalNavigationAction.WAIT_DYNAMIC_CONTENT]
        plan.confidence = 0.7
        plan.rationale = "dynamic_surface_wait"
        return plan

    if search.found:
        plan.actions = []
        plan.confidence = search.confidence
        plan.rationale = "entity_already_visible"
        return plan

    aff = list(affordances)
    actions: List[OperationalNavigationAction] = []

    if OperationalNavigationAction.SCROLL_DOWN.value in aff:
        actions.append(OperationalNavigationAction.SCROLL_DOWN)
    if OperationalNavigationAction.PAGINATE_NEXT.value in aff:
        actions.append(OperationalNavigationAction.PAGINATE_NEXT)
    if OperationalNavigationAction.RETRY_LOAD.value in aff:
        actions.append(OperationalNavigationAction.RETRY_LOAD)
    if OperationalNavigationAction.REOPEN_COLLECTION.value in aff:
        actions.append(OperationalNavigationAction.REOPEN_COLLECTION)
    if OperationalNavigationAction.FOCUS_SEARCH.value in aff:
        actions.append(OperationalNavigationAction.REFINE_QUERY)
    if not actions:
        actions.append(OperationalNavigationAction.SCROLL_DOWN)

    if dynamic.likely_virtualized and OperationalNavigationAction.SCROLL_DOWN not in actions:
        actions.insert(0, OperationalNavigationAction.SCROLL_DOWN)

    plan.actions = actions[:4]
    plan.confidence = 0.55 + 0.1 * min(len(aff), 4)
    plan.rationale = "affordance_heuristic"
    return plan


# ── Fase 4: Navigation loop ───────────────────────────────────────────────

NavigationExecutor = Callable[
    [OperationalNavigationAction, Dict[str, Any]],
    Dict[str, Any],
]


def _default_nav_executor(
    action: OperationalNavigationAction,
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """Executor simulado para tests; runtime real inyecta scroll/wait reales."""
    time.sleep(min(0.05, (ctx.get("wait_ms") or DEFAULT_WAIT_MS) / 10000.0))
    scroll_y = int(ctx.get("scroll_y") or 0)
    step = int(ctx.get("scroll_step") or 320)
    if action == OperationalNavigationAction.SCROLL_DOWN:
        ctx["scroll_y"] = scroll_y + step
    elif action == OperationalNavigationAction.SCROLL_UP:
        ctx["scroll_y"] = max(0, scroll_y - step)
    elif action == OperationalNavigationAction.WAIT_DYNAMIC_CONTENT:
        time.sleep(0.02)
    elif action == OperationalNavigationAction.PAGINATE_NEXT:
        ctx["page"] = int(ctx.get("page") or 0) + 1
    return ctx


def run_operational_navigation_loop(
    *,
    target_entity: OperationalEntity,
    target_collection: Optional[OperationalCollection],
    observe_fn: Callable[[Dict[str, Any]], Tuple[Sequence[OperationalCollection], Sequence[Dict[str, Any]]]],
    goal: Optional[OperationalNavigationGoal] = None,
    execute_fn: Optional[NavigationExecutor] = None,
    memory: Optional[NavigationMemoryStore] = None,
    mission: Any = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    context: Optional[Dict[str, Any]] = None,
    deadline_mono: Optional[float] = None,
    progress_budget: Any = None,
) -> NavigationLoopResult:
    """
    Loop adaptativo: observar → ONG → buscar → planear → ejecutar → reevaluar.
    ``observe_fn(ctx)`` devuelve (collections, uia_flat) tras cada transición.
    """
    out = NavigationLoopResult()
    ctx = dict(context or {})
    store = memory or NavigationMemoryStore()

    if goal is None:
        goal = OperationalNavigationGoal(
            target_entity_id=target_entity.entity_id,
            target_collection_id=(
                target_collection.collection_id if target_collection else ""
            ),
            target_operational_context={
                "display_name": target_entity.display_name,
                "collection_sig": (
                    _structural_hash(target_collection) if target_collection else ""
                ),
            },
            target_semantic_intent="recover_entity",
            max_navigation_depth=max_depth,
        )

    unsafe, reason = is_unsafe_navigation_context(entity=target_entity)
    if unsafe:
        out.stopped_reason = reason
        out.request_human = True
        out.human_message = "Esta acción requiere confirmación humana."
        return out

    executor = execute_fn or _default_nav_executor
    graph = OperationalNavigationGraph(goal=goal)
    executed_actions: List[str] = []

    for depth in range(max_depth):
        if deadline_mono is not None and time.monotonic() >= deadline_mono:
            out.stopped_reason = "entity_runtime_timeout"
            break
        out.depth_reached = depth + 1
        collections, uia_flat = observe_fn(ctx)
        viewport_sig = str(ctx.get("viewport_signature") or ctx.get("step_id") or "")
        if progress_budget is not None and hasattr(progress_budget, "note_progress"):
            if not progress_budget.note_progress(viewport_sig):
                out.stopped_reason = "no_state_progress"
                break
        graph = build_live_operational_navigation_graph(
            collections=collections,
            uia_flat=uia_flat,
            goal=goal,
            context=ctx,
            previous_graph=graph,
        )
        dynamic = detect_dynamic_surface_state(uia_flat=uia_flat, context=ctx)
        search = search_entity_in_live_operational_space(
            target_entity=target_entity,
            target_collection=target_collection,
            graph=graph,
            collections=collections,
            memory=store,
        )

        if search.found and search.entity:
            out.success = True
            out.entity = search.entity
            out.collection = search.collection
            out.confidence = search.confidence
            out.stopped_reason = "entity_found"
            out.graph = graph
            if executed_actions:
                store.record_recovery_path(
                    _structural_hash(target_collection) if target_collection else "",
                    normalize_entity_name(target_entity.display_name),
                    executed_actions,
                    success=True,
                )
            append_navigation_audit(mission, {
                "depth": depth,
                "success": True,
                "strategy": search.strategy,
                "actions": executed_actions,
            })
            return out

        if search.candidate_count >= 3 and search.confidence >= 0.75 and not search.found:
            out.stopped_reason = "ambiguous_candidates"
            out.request_human = True
            out.human_message = "Varias opciones similares; indica cuál elegir."
            out.graph = graph
            return out

        affordances = detect_navigation_affordances(
            uia_flat=uia_flat,
            collections=collections,
            dynamic=dynamic,
        )
        plan = plan_navigation_actions(
            search=search,
            graph=graph,
            dynamic=dynamic,
            affordances=affordances,
            memory=store,
            goal=goal,
        )

        if not plan.actions:
            out.stopped_reason = "no_actions_planned"
            break

        for act in plan.actions:
            ctx = executor(act, ctx)
            executed_actions.append(act.value)
            out.actions_executed.append(act.value)

    out.stopped_reason = out.stopped_reason or "max_depth_exhausted"
    out.graph = graph
    append_navigation_audit(mission, {
        "depth": out.depth_reached,
        "success": False,
        "reason": out.stopped_reason,
        "actions": executed_actions,
    })
    return out


def try_live_navigation_recovery(
    step: Any,
    state: Any,
    mission: Any,
    *,
    target_entity: OperationalEntity,
    target_collection: Optional[OperationalCollection] = None,
    observe_fn: Optional[
        Callable[[Dict[str, Any]], Tuple[Sequence[OperationalCollection], Sequence[Dict[str, Any]]]]
    ] = None,
    execute_fn: Optional[NavigationExecutor] = None,
    deadline_mono: Optional[float] = None,
    progress_budget: Any = None,
) -> NavigationLoopResult:
    """
    Punto de integración ERL/UCES: cuando la entidad no está visible, LONE navega.
  """
    from app.services.runtime.universal_collection_entity_engine import (
        detect_operational_collections,
    )

    def _observe(ctx: Dict[str, Any]) -> Tuple[List[OperationalCollection], List[Dict[str, Any]]]:
        if observe_fn:
            return observe_fn(ctx)
        uia_flat: List[Dict[str, Any]] = []
        if state is not None:
            try:
                snap = state.to_dict() if hasattr(state, "to_dict") else {}
                uia_flat = list(snap.get("uia_flat") or snap.get("candidates") or [])
            except Exception:
                pass
        colls = detect_operational_collections(
            capture_bundle={"uia_flat": uia_flat, **ctx},
        )
        return colls, uia_flat

    return run_operational_navigation_loop(
        target_entity=target_entity,
        target_collection=target_collection,
        observe_fn=_observe,
        execute_fn=execute_fn,
        mission=mission,
        context={"step_id": getattr(step, "id", "")},
        deadline_mono=deadline_mono,
        progress_budget=progress_budget,
    )


# ── UX ──────────────────────────────────────────────────────────────────


def normal_status_label_for_navigation(active: bool) -> Optional[str]:
    if active:
        return NORMAL_STATUS_RECOVERING
    return None


def sanitize_navigation_human_message(msg: str) -> str:
    if not msg:
        return msg
    out = str(msg)
    for tok in ("ong", "navigation_graph", "uia_flat", "coords", "bbox", "dom"):
        out = re.sub(re.escape(tok), "", out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip()


def expert_navigation_audit_lines(mission: Any) -> List[str]:
    rows = list(getattr(mission, "operational_navigation_audit", None) or [])
    lines: List[str] = []
    for r in rows[-16:]:
        if not isinstance(r, dict):
            continue
        lines.append(
            f"  · depth={r.get('depth', '?')} ok={r.get('success')} "
            f"reason={r.get('reason', r.get('strategy', ''))} "
            f"actions={r.get('actions', [])}"
        )
    return lines


def append_navigation_audit(mission: Any, entry: Dict[str, Any]) -> None:
    if mission is None:
        return
    try:
        rows = list(getattr(mission, "operational_navigation_audit", None) or [])
        rows.append(entry)
        setattr(mission, "operational_navigation_audit", rows[-200:])
    except Exception as exc:
        log.debug("[LONE] audit: %s", exc)


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "NAVIGATION_MEMORY_VERSION",
    "DynamicSurfaceState",
    "EntityLiveSearchResult",
    "NavigationLoopResult",
    "NavigationMemoryStore",
    "NavigationPlan",
    "OperationalNavigationGraph",
    "NORMAL_STATUS_RECOVERING",
    "append_navigation_audit",
    "build_live_operational_navigation_graph",
    "detect_dynamic_surface_state",
    "detect_navigation_affordances",
    "expert_navigation_audit_lines",
    "is_unsafe_navigation_context",
    "normal_status_label_for_navigation",
    "plan_navigation_actions",
    "run_operational_navigation_loop",
    "sanitize_navigation_human_message",
    "search_entity_in_live_operational_space",
    "try_live_navigation_recovery",
]
