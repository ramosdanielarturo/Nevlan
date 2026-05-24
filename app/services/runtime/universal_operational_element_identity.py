"""Universal Operational Element Identity (UOEI).

PCOF/FFEC captura evidencia; UOAC decide función operacional; UOEI define
**qué tipo de elemento operacional** es el target — multi-señal, estable entre
labels, layouts, idiomas y plataformas.

Flujo::

    PCOF → UOEI → FFEC/COI → UOAC → OTP → OCRX → SEP → UOL → Runtime

Prohibido: identidad por label textual, coordenadas o control type aislado.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    EventType,
    OperationalCollectionType,
    OperationalElementIdentity,
    OperationalElementType,
    OperationalFreezeSnapshot,
    OperationalFreezeTarget,
    RawEvent,
)
from app.core.logger import log

# ── Señales estructurales (sin nombres de producto ni labels concretos) ───────

_INPUT_CONTROL_ROLES: Tuple[str, ...] = (
    "edit",
    "textbox",
    "combobox",
    "searchbox",
    "search",
    "autocomplete",
    "input",
)

_COLLECTION_ITEM_ROLES: Tuple[str, ...] = (
    "listitem",
    "treeitem",
    "dataitem",
    "option",
    "row",
    "gridcell",
    "card",
    "article",
)

_BUTTON_ROLES: Tuple[str, ...] = (
    "button",
    "hyperlink",
    "link",
    "splitbutton",
    "menuitem",
)

_TAB_BAR_HIERARCHY: Tuple[str, ...] = (
    "tab",
    "tabs",
    "tabstrip",
    "tabbar",
    "tabitem",
)

_LAUNCHER_HIERARCHY: Tuple[str, ...] = (
    "launcher",
    "searchhost",
    "searchui",
    "applist",
    "tilegrid",
    "startmenu",
    "allapps",
)

_MODAL_HIERARCHY: Tuple[str, ...] = (
    "dialog",
    "popup",
    "modal",
    "overlay",
    "flyout",
    "alert",
)

_NAV_HIERARCHY: Tuple[str, ...] = (
    "navigation",
    "navbar",
    "menubar",
    "toolbar",
    "breadcrumb",
    "sidebar",
    "commandbar",
)

_SCROLL_CONTAINER_ROLES: Tuple[str, ...] = (
    "scroll",
    "scrollviewer",
    "scrollarea",
    "list",
    "datagrid",
    "table",
    "feed",
)

_RESULTS_HIERARCHY: Tuple[str, ...] = (
    "results",
    "resultlist",
    "searchresults",
    "suggestions",
    "autocomplete",
)

_PLUS_ICON_MARKERS: Tuple[str, ...] = (
    "+",
    "＋",
    "plus_icon",
    "add_icon",
    "new_icon",
)

_IDENTITY_COLLECTION_TYPES: Tuple[str, ...] = (
    OperationalCollectionType.CARDS.value,
    OperationalCollectionType.GRID.value,
)


@dataclass
class ElementIdentityContext:
    """Entrada multi-señal para UOEI."""

    freeze: Optional[OperationalFreezeSnapshot] = None
    primary_target: Optional[OperationalFreezeTarget] = None
    previous_event: Optional[RawEvent] = None
    next_event: Optional[RawEvent] = None
    current_event: Optional[RawEvent] = None
    uia_role: str = ""
    uia_control_type: str = ""
    dom_role: str = ""
    dom_aria: str = ""
    icon_signature: str = ""
    parent_hierarchy: List[str] = field(default_factory=list)
    sibling_count: int = 0
    collection_entity_count: int = 0
    in_collection: bool = False
    collection_type: str = ""
    scrollable: bool = False
    surface_type: str = ""
    position_hint: str = ""
    viewport_signature: str = ""
    layout_signature: str = ""
    visual_group_signature: str = ""
    outcome_transition: str = ""
    historical_element_type: str = ""
    interaction_outcome: str = ""


def _txt(x: Any) -> str:
    return str(x or "").strip()


def _lower(x: Any) -> str:
    return _txt(x).lower()


def _contains_any(hay: str, needles: Sequence[str]) -> bool:
    low = _lower(hay)
    return any(n in low for n in needles if n)


def _hierarchy_text(parts: Sequence[str]) -> str:
    return " ".join(_lower(p) for p in parts if p)


def _hash_payload(*parts: str) -> str:
    payload = "|".join(p for p in parts if p)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _primary_target_from_freeze(
    freeze: OperationalFreezeSnapshot,
) -> Optional[OperationalFreezeTarget]:
    best = freeze.best_entity_candidate
    best_name = _txt(best.display_name if best else "")
    for seq in (freeze.uia_targets, freeze.ocr_targets, freeze.dom_targets):
        for tgt in seq or []:
            if isinstance(tgt, dict):
                name = _txt(tgt.get("name") or tgt.get("text"))
                if best_name and name == best_name:
                    return OperationalFreezeTarget.model_validate(tgt)
                if float(tgt.get("overlap_score") or 0) >= 0.85:
                    return OperationalFreezeTarget.model_validate(tgt)
            else:
                name = _txt(getattr(tgt, "name", "") or getattr(tgt, "text", ""))
                if best_name and name == best_name:
                    return tgt
                if float(getattr(tgt, "overlap_score", 0) or 0) >= 0.85:
                    return tgt
    if freeze.uia_targets:
        t0 = freeze.uia_targets[0]
        if isinstance(t0, dict):
            return OperationalFreezeTarget.model_validate(t0)
        return t0
    return None


def _event_is_scroll(event: Optional[RawEvent]) -> bool:
    if event is None:
        return False
    return event.event_type in (EventType.MOUSE_SCROLL, EventType.MOUSE_DRAG)


def _event_is_text_entry(event: Optional[RawEvent]) -> bool:
    if event is None:
        return False
    if event.event_type != EventType.KEYBOARD_TYPE_TEXT:
        return False
    meta = dict(event.metadata or {})
    if meta.get("final_text") or meta.get("text") or meta.get("typed_text"):
        return True
    ka = event.keyboard_action
    return bool(ka and list(ka.keys or []))


def _event_is_submit(event: Optional[RawEvent]) -> bool:
    if event is None:
        return False
    ka = event.keyboard_action
    if ka:
        keys = [_lower(k) for k in (ka.keys or [])]
        if "enter" in keys or "return" in keys:
            return True
    if event.event_type == EventType.KEYBOARD_HOTKEY:
        combo = _lower(getattr(event, "key_combination", "") or "")
        return "enter" in combo
    return False


def _is_plus_icon(name: str, icon_sig: str) -> bool:
    if name in ("+", "＋"):
        return True
    return _contains_any(icon_sig, _PLUS_ICON_MARKERS)


def build_identity_context_from_freeze(
    freeze: OperationalFreezeSnapshot,
    *,
    previous_event: Optional[RawEvent] = None,
    next_event: Optional[RawEvent] = None,
    current_event: Optional[RawEvent] = None,
    historical_element_type: str = "",
) -> ElementIdentityContext:
    """Construye contexto UOEI desde snapshot PCOF."""
    tgt = _primary_target_from_freeze(freeze)
    role = _lower(getattr(tgt, "role", "") if tgt else "")
    hierarchy = list(freeze.hierarchy_chain or [])
    if tgt and getattr(tgt, "parent_chain", None):
        hierarchy = list(tgt.parent_chain or []) + hierarchy

    coll = freeze.best_collection_candidate
    coll_count = int(coll.entity_count if coll else 0) or len(coll.entities or []) if coll else 0
    in_coll = bool(coll and coll_count > 1)
    coll_type = _lower(getattr(coll, "collection_type", "") if coll else "")

    dom_role = ""
    for dt in freeze.dom_targets or []:
        if isinstance(dt, dict):
            dom_role = dom_role or _txt(dt.get("role"))
        else:
            dom_role = dom_role or _txt(getattr(dt, "role", ""))

    scrollable = bool(coll and getattr(coll, "scrollable", False))
    if not scrollable:
        for oc in freeze.operational_collections or []:
            if getattr(oc, "scrollable", False):
                scrollable = True
                break

    htext = _hierarchy_text(hierarchy)
    surface = "generic"
    position_hint = ""
    if _contains_any(htext, _LAUNCHER_HIERARCHY):
        surface = "launcher_surface"
        position_hint = "launcher_grid"
    elif _contains_any(htext, _TAB_BAR_HIERARCHY):
        surface = "tab_bar"
        position_hint = "tab_bar"
    elif _contains_any(htext, _MODAL_HIERARCHY):
        surface = "modal_surface"
    elif in_coll:
        surface = "collection_surface"
    elif _contains_any(role, _INPUT_CONTROL_ROLES) or _contains_any(dom_role, _INPUT_CONTROL_ROLES):
        surface = "input_surface"

    icon_sig = _lower(freeze.visual_group_signature or freeze.layout_signature or "")
    name = _txt(getattr(tgt, "name", "") if tgt else "")
    if _is_plus_icon(name, icon_sig):
        icon_sig = (icon_sig + " plus_icon").strip()

    sibling_count = coll_count if in_coll else 0

    return ElementIdentityContext(
        freeze=freeze,
        primary_target=tgt,
        previous_event=previous_event,
        next_event=next_event,
        current_event=current_event,
        uia_role=role,
        uia_control_type=role,
        dom_role=_lower(dom_role),
        icon_signature=icon_sig,
        parent_hierarchy=hierarchy,
        sibling_count=sibling_count,
        collection_entity_count=coll_count,
        in_collection=in_coll,
        collection_type=coll_type,
        scrollable=scrollable,
        surface_type=surface,
        position_hint=position_hint,
        viewport_signature=_txt(freeze.viewport_signature),
        layout_signature=_txt(freeze.layout_signature),
        visual_group_signature=_txt(freeze.visual_group_signature),
        outcome_transition=_txt(freeze.trigger),
        historical_element_type=historical_element_type,
    )


def compute_structural_signature(ctx: ElementIdentityContext) -> str:
    role = _lower(ctx.uia_role or ctx.dom_role)
    hier = _hierarchy_text(ctx.parent_hierarchy)
    coll = ctx.collection_type or "none"
    pos = ctx.position_hint or "none"
    in_coll = "1" if ctx.in_collection else "0"
    scroll = "1" if ctx.scrollable else "0"
    return _hash_payload(f"struct:{role}:{hier[:80]}:{coll}:{pos}:{in_coll}:{scroll}")


def compute_visual_signature(ctx: ElementIdentityContext) -> str:
    icon = _lower(ctx.icon_signature)
    layout = _lower(ctx.layout_signature)
    vgroup = _lower(ctx.visual_group_signature)
    viewport = _lower(ctx.viewport_signature)
    return _hash_payload(f"visual:{icon}:{layout[:40]}:{vgroup[:40]}:{viewport[:40]}")


def compute_behavioral_signature(ctx: ElementIdentityContext) -> str:
    bits: List[str] = []
    if _event_is_scroll(ctx.current_event):
        bits.append("scroll")
    if _event_is_text_entry(ctx.next_event):
        bits.append("type_successor")
    if _event_is_submit(ctx.next_event):
        bits.append("submit_successor")
    if ctx.outcome_transition:
        bits.append(f"transition:{ctx.outcome_transition[:24]}")
    if ctx.interaction_outcome:
        bits.append(f"outcome:{ctx.interaction_outcome[:24]}")
    return _hash_payload("behavior:" + ":".join(bits or ["none"]))


def compute_contextual_signature(ctx: ElementIdentityContext) -> str:
    surface = ctx.surface_type or "generic"
    coll_count = str(ctx.collection_entity_count)
    siblings = str(ctx.sibling_count)
    return _hash_payload(f"context:{surface}:{coll_count}:{siblings}")


def compute_interaction_signature(ctx: ElementIdentityContext) -> str:
    role = _lower(ctx.uia_role or ctx.dom_role)
    exec_role = ""
    if _contains_any(role, _BUTTON_ROLES):
        exec_role = "clickable"
    elif _contains_any(role, _INPUT_CONTROL_ROLES):
        exec_role = "input"
    elif _contains_any(role, _COLLECTION_ITEM_ROLES):
        exec_role = "selectable_item"
    return _hash_payload(f"interaction:{exec_role}:{role}")


def compute_continuity_signature(ctx: ElementIdentityContext) -> str:
    hist = ctx.historical_element_type or "none"
    cross = ctx.freeze.freeze_id if ctx.freeze else ""
    return _hash_payload(f"continuity:{hist}:{cross[:16]}")


def compute_semantic_signature(ctx: ElementIdentityContext) -> str:
    """Firma semántica estructural — sin labels textuales."""
    role = _lower(ctx.uia_role or ctx.dom_role)
    hier = _hierarchy_text(ctx.parent_hierarchy)
    coll = ctx.collection_type or "none"
    return _hash_payload(f"semantic:{role}:{hier[:60]}:{coll}")


def _infer_roles(ctx: ElementIdentityContext) -> Dict[str, str]:
    hier = _hierarchy_text(ctx.parent_hierarchy)
    role = _lower(ctx.uia_role or ctx.dom_role)

    collection_role = ""
    if ctx.in_collection:
        collection_role = ctx.collection_type or "collection_item"
    elif ctx.collection_entity_count >= 2:
        collection_role = "container_parent"

    navigation_role = ""
    if _contains_any(hier, _TAB_BAR_HIERARCHY):
        navigation_role = "tab_bar"
    elif _contains_any(hier, _NAV_HIERARCHY):
        navigation_role = "navigation_bar"
    elif ctx.collection_type == OperationalCollectionType.TABS.value:
        navigation_role = "tab_collection"

    surface_role = ctx.surface_type or "generic"
    if _contains_any(hier, _MODAL_HIERARCHY):
        surface_role = "modal_surface"

    input_role = ""
    if _contains_any(role, _INPUT_CONTROL_ROLES):
        if surface_role == "launcher_surface" or _contains_any(hier, _LAUNCHER_HIERARCHY):
            input_role = "launcher_query"
        elif _event_is_text_entry(ctx.next_event) or _event_is_submit(ctx.next_event):
            input_role = "query_field"
        else:
            input_role = "form_field"

    execution_role = ""
    if _contains_any(role, _BUTTON_ROLES):
        execution_role = "trigger"
    elif _contains_any(role, _INPUT_CONTROL_ROLES):
        execution_role = "input_target"
    elif _contains_any(role, _COLLECTION_ITEM_ROLES):
        execution_role = "selection_target"

    return {
        "collection_role": collection_role,
        "navigation_role": navigation_role,
        "surface_role": surface_role,
        "input_role": input_role,
        "execution_role": execution_role,
    }


def _score_element_types(
    ctx: ElementIdentityContext,
) -> Dict[OperationalElementType, Tuple[float, List[str]]]:
    """Votación multi-señal por tipo de elemento (sin labels de producto)."""
    scores: Dict[OperationalElementType, Tuple[float, List[str]]] = {
        t: (0.0, []) for t in OperationalElementType
    }

    def add(t: OperationalElementType, w: float, reason: str) -> None:
        cur, rs = scores[t]
        scores[t] = (cur + w, rs + [reason])

    role = _lower(ctx.uia_role or ctx.dom_role)
    hier = _hierarchy_text(ctx.parent_hierarchy)
    name = _txt(getattr(ctx.primary_target, "name", "") if ctx.primary_target else "")

    # INPUT / QUERY
    if _contains_any(role, _INPUT_CONTROL_ROLES):
        if ctx.surface_type == "launcher_surface" or _contains_any(hier, _LAUNCHER_HIERARCHY):
            add(OperationalElementType.LAUNCHER_INPUT_SURFACE, 0.92, "input_in_launcher_hierarchy")
        elif _event_is_text_entry(ctx.next_event) or _event_is_submit(ctx.next_event):
            add(OperationalElementType.SEARCH_INPUT_SURFACE, 0.88, "input_with_query_successor")
        elif _contains_any(hier, _RESULTS_HIERARCHY):
            add(OperationalElementType.SEARCH_INPUT_SURFACE, 0.75, "input_near_results_hierarchy")
        else:
            add(OperationalElementType.FORM_INPUT_SURFACE, 0.7, "input_control_generic")

    # CONTEXT CREATION — plus icon in tab bar (estructural, no label)
    if _is_plus_icon(name, ctx.icon_signature):
        if ctx.position_hint == "tab_bar" or _contains_any(hier, _TAB_BAR_HIERARCHY):
            add(OperationalElementType.CONTEXT_CREATION_AFFORDANCE, 0.95, "plus_in_tab_bar")
        else:
            add(OperationalElementType.CONTEXT_CREATION_AFFORDANCE, 0.6, "plus_icon_generic")

    # CONTEXT SWITCH — tab items
    if ctx.collection_type == OperationalCollectionType.TABS.value:
        if _contains_any(role, _COLLECTION_ITEM_ROLES + ("tabitem",)):
            add(OperationalElementType.CONTEXT_SWITCH_AFFORDANCE, 0.85, "tab_collection_item")
    elif "tabitem" in role:
        add(OperationalElementType.CONTEXT_SWITCH_AFFORDANCE, 0.8, "tabitem_role")

    # SESSION SWITCH — identity picker surfaces (estructural via collection)
    if ctx.in_collection and ctx.collection_entity_count >= 2:
        if ctx.collection_type in _IDENTITY_COLLECTION_TYPES:
            if _contains_any(role, _COLLECTION_ITEM_ROLES):
                add(OperationalElementType.SELECTABLE_IDENTITY, 0.9, "identity_collection_item")
                add(OperationalElementType.IDENTITY_CARD, 0.75, "identity_card_in_collection")
        if ctx.collection_type == OperationalCollectionType.MENU.value:
            add(OperationalElementType.SESSION_SWITCH_SURFACE, 0.7, "menu_session_switcher")

    # NAVIGATION
    if _contains_any(hier, _NAV_HIERARCHY) and _contains_any(role, _BUTTON_ROLES + ("link",)):
        if not ctx.in_collection:
            add(OperationalElementType.NAVIGATION_RESOURCE, 0.78, "nav_hierarchy_link")
    if _contains_any(hier, _TAB_BAR_HIERARCHY) and _contains_any(role, _BUTTON_ROLES):
        if not _is_plus_icon(name, ctx.icon_signature):
            add(OperationalElementType.NAVIGATION_AFFORDANCE, 0.65, "tab_bar_button")
    if ctx.surface_type == "tab_bar" and len(name) <= 2 and not _is_plus_icon(name, ctx.icon_signature):
        add(OperationalElementType.BROWSER_NAVIGATION_SURFACE, 0.7, "compact_tab_bar_control")

    # CONTENT / RESULTS
    if ctx.collection_type == OperationalCollectionType.SEARCH_RESULTS.value:
        if ctx.in_collection and _contains_any(role, _COLLECTION_ITEM_ROLES):
            add(OperationalElementType.RESULTS_CONTAINER, 0.5, "search_result_item")
        else:
            add(OperationalElementType.RESULTS_CONTAINER, 0.85, "search_results_collection")
    if ctx.scrollable and ctx.in_collection and ctx.collection_entity_count >= 3:
        add(OperationalElementType.SCROLLABLE_RESULTS_SURFACE, 0.92, "scrollable_multi_item_collection")
    elif _event_is_scroll(ctx.current_event) and ctx.in_collection:
        add(OperationalElementType.SCROLLABLE_RESULTS_SURFACE, 0.85, "scroll_on_collection")

    if not ctx.in_collection and not _contains_any(role, _INPUT_CONTROL_ROLES + _BUTTON_ROLES):
        if role and _contains_any(hier, ("document", "pane", "group", "main")):
            add(OperationalElementType.CONTENT_SURFACE, 0.45, "content_pane")
            add(OperationalElementType.OPERATIONAL_WORKSPACE, 0.4, "workspace_pane")

    # ACTION affordances in modal context
    if _contains_any(hier, _MODAL_HIERARCHY):
        add(OperationalElementType.MODAL_SURFACE, 0.8, "modal_hierarchy")
        if _contains_any(role, _BUTTON_ROLES):
            add(OperationalElementType.CONFIRMATION_AFFORDANCE, 0.65, "modal_button")
            add(OperationalElementType.SUBMIT_AFFORDANCE, 0.55, "modal_submit_candidate")

    if ctx.in_collection and ctx.collection_entity_count >= 2 and not _contains_any(
        role, _COLLECTION_ITEM_ROLES,
    ):
        add(OperationalElementType.COLLECTION_CONTAINER, 0.75, "collection_container_parent")

    # Launcher tiles (estructural: launcher hierarchy + collection item)
    if ctx.surface_type == "launcher_surface" and _contains_any(role, _COLLECTION_ITEM_ROLES):
        add(OperationalElementType.NAVIGATION_RESOURCE, 0.72, "launcher_tile_item")

    # Historical continuity boost
    if ctx.historical_element_type:
        try:
            hist = OperationalElementType(ctx.historical_element_type)
            add(hist, 0.12, "historical_element_match")
        except ValueError:
            pass

    return scores


def _pick_best_type(
    scores: Dict[OperationalElementType, Tuple[float, List[str]]],
) -> Tuple[OperationalElementType, float, List[str], float, List[str]]:
    ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
    best_type, (best_score, best_reasons) = ranked[0]
    second_score = ranked[1][1][0] if len(ranked) > 1 else 0.0
    alternatives = [
        t.value for t, (sc, _) in ranked[1:4] if sc >= 0.35 and sc > 0
    ]
    gap = best_score - second_score
    ambiguity = 0.0
    if best_score < 0.45:
        ambiguity = min(1.0, 1.0 - best_score)
    elif gap < 0.15 and second_score >= 0.4:
        ambiguity = min(1.0, 0.5 + (0.15 - gap))
    return best_type, best_score, best_reasons, ambiguity, alternatives


def _expected_outcomes(element_type: OperationalElementType) -> List[str]:
    _OUTCOMES: Dict[OperationalElementType, List[str]] = {
        OperationalElementType.LAUNCHER_INPUT_SURFACE: ["focus_input", "query_ready"],
        OperationalElementType.SEARCH_INPUT_SURFACE: ["focus_input", "query_submitted"],
        OperationalElementType.FORM_INPUT_SURFACE: ["focus_input", "field_filled"],
        OperationalElementType.CONTEXT_CREATION_AFFORDANCE: ["new_context_opened"],
        OperationalElementType.CONTEXT_SWITCH_AFFORDANCE: ["context_switched"],
        OperationalElementType.SELECTABLE_IDENTITY: ["identity_selected"],
        OperationalElementType.RESULTS_CONTAINER: ["results_visible"],
        OperationalElementType.SCROLLABLE_RESULTS_SURFACE: ["content_scrolled"],
        OperationalElementType.NAVIGATION_RESOURCE: ["navigation_occurred"],
        OperationalElementType.CONFIRMATION_AFFORDANCE: ["action_confirmed"],
        OperationalElementType.SUBMIT_AFFORDANCE: ["form_submitted"],
    }
    return list(_OUTCOMES.get(element_type, []))


def _stability_score(
    ctx: ElementIdentityContext,
    confidence: float,
    ambiguity: float,
) -> float:
    base = confidence
    if ctx.freeze and ctx.freeze.pre_transition_confirmed:
        base = min(1.0, base + 0.08)
    if ctx.in_collection and ctx.collection_entity_count >= 2:
        base = min(1.0, base + 0.05)
    if ctx.historical_element_type:
        base = min(1.0, base + 0.06)
    stability = max(0.0, min(1.0, base - ambiguity * 0.35))
    return round(stability, 4)


def resolve_operational_element_identity(
    ctx: ElementIdentityContext,
) -> OperationalElementIdentity:
    """Resuelve identidad operacional universal desde contexto multi-señal."""
    scores = _score_element_types(ctx)
    best_type, best_score, reasons, ambiguity, alternatives = _pick_best_type(scores)
    confidence = best_score

    if confidence < 0.35 or best_score < 0.4:
        best_type = OperationalElementType.UNKNOWN_OPERATIONAL_ELEMENT
        confidence = max(confidence, 0.2)
        ambiguity = max(ambiguity, 0.55)

    roles = _infer_roles(ctx)
    struct_sig = compute_structural_signature(ctx)
    visual_sig = compute_visual_signature(ctx)
    behavioral_sig = compute_behavioral_signature(ctx)
    contextual_sig = compute_contextual_signature(ctx)
    interaction_sig = compute_interaction_signature(ctx)
    continuity_sig = compute_continuity_signature(ctx)
    semantic_sig = compute_semantic_signature(ctx)

    identity_id = _hash_payload(
        struct_sig, visual_sig, contextual_sig, semantic_sig, best_type.value,
    )

    cross_surface = _hash_payload(struct_sig, semantic_sig)

    stability = _stability_score(ctx, min(1.0, confidence), ambiguity)

    historical: List[str] = []
    if ctx.historical_element_type:
        historical.append(ctx.historical_element_type)

    return OperationalElementIdentity(
        identity_id=identity_id,
        operational_element_type=best_type,
        confidence=round(min(1.0, confidence), 4),
        structural_signature=struct_sig,
        visual_signature=visual_sig,
        behavioral_signature=behavioral_sig,
        contextual_signature=contextual_sig,
        interaction_signature=interaction_sig,
        continuity_signature=continuity_sig,
        semantic_signature=semantic_sig,
        collection_role=roles["collection_role"],
        navigation_role=roles["navigation_role"],
        surface_role=roles["surface_role"],
        input_role=roles["input_role"],
        execution_role=roles["execution_role"],
        expected_outcomes=_expected_outcomes(best_type),
        ambiguity_score=round(ambiguity, 4),
        alternative_identities=alternatives,
        identity_stability_score=stability,
        cross_surface_identity=cross_surface,
        historical_matches=historical,
    )


def identify_from_freeze(
    freeze: OperationalFreezeSnapshot,
    *,
    previous_event: Optional[RawEvent] = None,
    next_event: Optional[RawEvent] = None,
    current_event: Optional[RawEvent] = None,
    historical_element_type: str = "",
) -> OperationalElementIdentity:
    """Punto de entrada principal: PCOF freeze → identidad operacional."""
    ctx = build_identity_context_from_freeze(
        freeze,
        previous_event=previous_event,
        next_event=next_event,
        current_event=current_event,
        historical_element_type=historical_element_type,
    )
    return resolve_operational_element_identity(ctx)


def identity_fingerprint(identity: OperationalElementIdentity) -> str:
    """Huella estable para replay/OTP — identidad, no label."""
    return f"uoei:{identity.identity_id}:{identity.operational_element_type.value}"


def identity_dict(identity: OperationalElementIdentity) -> Dict[str, Any]:
    """Serialización compacta para metadata."""
    return {
        "identity_id": identity.identity_id,
        "operational_element_type": identity.operational_element_type.value,
        "confidence": identity.confidence,
        "identity_stability_score": identity.identity_stability_score,
        "ambiguity_score": identity.ambiguity_score,
        "structural_signature": identity.structural_signature,
        "cross_surface_identity": identity.cross_surface_identity,
        "collection_role": identity.collection_role,
        "navigation_role": identity.navigation_role,
        "surface_role": identity.surface_role,
    }


def attach_identity_to_metadata(
    metadata: Dict[str, Any],
    identity: OperationalElementIdentity,
) -> Dict[str, Any]:
    metadata["operational_element_identity"] = identity.model_dump(mode="json")
    metadata["operational_element_fingerprint"] = identity_fingerprint(identity)
    return metadata


def identity_from_metadata(metadata: Dict[str, Any]) -> Optional[OperationalElementIdentity]:
    raw = metadata.get("operational_element_identity")
    if not isinstance(raw, dict):
        return None
    try:
        return OperationalElementIdentity.model_validate(raw)
    except Exception:
        return None


def match_identity_stability(
    prior: OperationalElementIdentity,
    current: OperationalElementIdentity,
) -> float:
    """Score de continuidad entre identidades (0–1). Labels distintos no penalizan."""
    if prior.operational_element_type == current.operational_element_type:
        base = 0.85
    elif current.operational_element_type.value in prior.alternative_identities:
        base = 0.65
    else:
        base = 0.25

    if prior.cross_surface_identity and prior.cross_surface_identity == current.cross_surface_identity:
        base = min(1.0, base + 0.12)
    if prior.structural_signature == current.structural_signature:
        base = min(1.0, base + 0.08)
    if prior.semantic_signature == current.semantic_signature:
        base = min(1.0, base + 0.05)

    # Penalize only true ambiguity, not label changes
    amb_penalty = (prior.ambiguity_score + current.ambiguity_score) * 0.1
    return round(max(0.0, min(1.0, base - amb_penalty)), 4)


def replay_resolve_by_identity(
    expected: OperationalElementIdentity,
    candidates: Sequence[OperationalElementIdentity],
) -> Optional[OperationalElementIdentity]:
    """Runtime replay: resuelve por identidad operacional, no label."""
    if not candidates:
        return None
    best: Optional[OperationalElementIdentity] = None
    best_score = 0.0
    for cand in candidates:
        score = match_identity_stability(expected, cand)
        if score > best_score:
            best_score = score
            best = cand
    if best_score >= 0.55:
        return best
    return None


def enrich_affordance_with_identity(
    affordance_type_value: str,
    identity: OperationalElementIdentity,
) -> str:
    """Refuerza hint UOAC desde identidad UOEI (sin reemplazar UOAC)."""
    mapping: Dict[OperationalElementType, str] = {
        OperationalElementType.LAUNCHER_INPUT_SURFACE: "activate_launcher",
        OperationalElementType.SEARCH_INPUT_SURFACE: "search_with_query",
        OperationalElementType.CONTEXT_CREATION_AFFORDANCE: "create_new_context",
        OperationalElementType.SELECTABLE_IDENTITY: "select_identity_context",
        OperationalElementType.SCROLLABLE_RESULTS_SURFACE: "browse_content",
        OperationalElementType.NAVIGATION_RESOURCE: "navigate_to_resource",
        OperationalElementType.CONFIRMATION_AFFORDANCE: "confirm_action",
    }
    hint = mapping.get(identity.operational_element_type, "")
    if hint and affordance_type_value == "unknown":
        return hint
    return affordance_type_value


def uoei_expert_panel_lines(mission: Any) -> List[str]:
    """Panel Mission Review experto: identidad operacional."""
    lines: List[str] = []
    seen: set = set()

    for coi in getattr(mission, "canonical_operational_intents", None) or []:
        ident = getattr(coi, "operational_element_identity", None)
        if ident is None:
            continue
        fp = identity_fingerprint(ident)
        if fp in seen:
            continue
        seen.add(fp)
        lines.append("UOEI — Operational Element Identity")
        lines.append(f"  type: {ident.operational_element_type.value}")
        lines.append(f"  identity_id: {ident.identity_id[:16]}…")
        lines.append(f"  stability: {ident.identity_stability_score:.2f}")
        lines.append(f"  ambiguity: {ident.ambiguity_score:.2f}")
        if ident.alternative_identities:
            lines.append(f"  alternatives: {', '.join(ident.alternative_identities[:3])}")
        if ident.historical_matches:
            lines.append(f"  lineage: {' → '.join(ident.historical_matches[:4])}")
        lines.append(f"  cross_surface: {ident.cross_surface_identity[:16]}…")
        return lines

    trace = getattr(mission, "raw_trace", None) or []
    for ev in trace[-10:]:
        meta = dict(getattr(ev, "metadata", None) or {})
        ident = identity_from_metadata(meta)
        if ident is None:
            continue
        fp = identity_fingerprint(ident)
        if fp in seen:
            continue
        seen.add(fp)
        lines.append("UOEI — Operational Element Identity")
        lines.append(f"  type: {ident.operational_element_type.value}")
        lines.append(f"  identity_id: {ident.identity_id[:16]}…")
        lines.append(f"  stability: {ident.identity_stability_score:.2f}")
        lines.append(f"  ambiguity: {ident.ambiguity_score:.2f}")
        if ident.alternative_identities:
            lines.append(f"  alternatives: {', '.join(ident.alternative_identities[:3])}")
        lines.append(f"  structural: {ident.structural_signature[:16]}…")
        break

    return lines


def goor_element_type_echo(mission: Any) -> Dict[str, Any]:
    """Eco de tipos de elemento para GOOR (goal inference)."""
    types: List[str] = []
    stabilities: List[float] = []
    for coi in getattr(mission, "canonical_operational_intents", None) or []:
        ident = getattr(coi, "operational_element_identity", None)
        if ident is None:
            continue
        types.append(ident.operational_element_type.value)
        stabilities.append(float(ident.identity_stability_score or 0))
    if not types:
        for ev in getattr(mission, "raw_trace", None) or []:
            meta = dict(getattr(ev, "metadata", None) or {})
            ident = identity_from_metadata(meta)
            if ident:
                types.append(ident.operational_element_type.value)
                stabilities.append(float(ident.identity_stability_score or 0))
    return {
        "element_types": types[-12:],
        "mean_stability": round(sum(stabilities) / max(1, len(stabilities)), 4) if stabilities else 0.0,
        "identity_count": len(types),
    }


__all__ = [
    "ElementIdentityContext",
    "OperationalElementIdentity",
    "OperationalElementType",
    "attach_identity_to_metadata",
    "build_identity_context_from_freeze",
    "compute_behavioral_signature",
    "compute_contextual_signature",
    "compute_continuity_signature",
    "compute_interaction_signature",
    "compute_semantic_signature",
    "compute_structural_signature",
    "compute_visual_signature",
    "enrich_affordance_with_identity",
    "goor_element_type_echo",
    "identify_from_freeze",
    "identity_dict",
    "identity_fingerprint",
    "identity_from_metadata",
    "match_identity_stability",
    "replay_resolve_by_identity",
    "resolve_operational_element_identity",
    "uoei_expert_panel_lines",
]
