"""Universal Operational Affordance Classifier (UOAC).

PCOF/FFEC captura **qué elemento** tocó el humano (evidencia estructural).
UOAC decide **qué función operacional** tenía ese elemento.

Flujo::

    PCOF → FFEC/COI → UOAC → OTP → OCRX → SEP → UOL → Runtime

Prohibido: PCOF → ``select_entity_from_collection`` directo por etiqueta UIA.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    EventType,
    OperationalAffordance,
    OperationalAffordanceType,
    OperationalFreezeEntityCandidate,
    OperationalFreezeSnapshot,
    OperationalFreezeTarget,
    RawEvent,
)
from app.core.logger import log

# ── Señales estructurales (sin nombres de producto) ─────────────────────────

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

_TAB_BAR_MARKERS: Tuple[str, ...] = (
    "tab",
    "tabs",
    "pestaña",
    "tabstrip",
    "tabbar",
    "toolbar",
    "commandbar",
    "titlebar",
)

_LAUNCHER_MARKERS: Tuple[str, ...] = (
    "launcher",
    "start",
    "searchui",
    "search host",
    "app list",
    "all apps",
    "grid",
    "tile",
)

_INPUT_PLACEHOLDER_MARKERS: Tuple[str, ...] = (
    "escribe",
    "type here",
    "search",
    "buscar",
    "busca",
    "find",
    "query",
    "enter a",
    "introduce",
    "placeholder",
    "lupa",
    "search box",
    "search field",
    "campo de búsqueda",
    "campo de busqueda",
)

_NEW_CONTEXT_MARKERS: Tuple[str, ...] = (
    "new tab",
    "nueva pestaña",
    "nueva pestana",
    "pestaña nueva",
    "add tab",
    "create tab",
    "open tab",
    "añadir pestaña",
    "agregar pestaña",
)

_PLUS_ICON_MARKERS: Tuple[str, ...] = (
    "+",
    "plus",
    "add",
    "new",
    "create",
    "añadir",
    "agregar",
    "insert",
)

_MENU_MARKERS: Tuple[str, ...] = (
    "menu",
    "more actions",
    "más acciones",
    "overflow",
    "options",
    "opciones",
    "⋯",
    "...",
)

_CONFIRM_MARKERS: Tuple[str, ...] = (
    "ok",
    "aceptar",
    "confirm",
    "confirmar",
    "apply",
    "aplicar",
    "done",
    "hecho",
)

_DISMISS_MARKERS: Tuple[str, ...] = (
    "cancel",
    "cancelar",
    "close",
    "cerrar",
    "dismiss",
    "no thanks",
    "skip",
    "omitir",
)

_IDENTITY_COLLECTION_MARKERS: Tuple[str, ...] = (
    "profile",
    "perfil",
    "account",
    "cuenta",
    "user",
    "usuario",
    "identity",
    "person",
    "member",
    "workspace",
)

_UOL_HINT_BY_AFFORDANCE: Dict[OperationalAffordanceType, str] = {
    OperationalAffordanceType.ACTIVATE_LAUNCHER: "open_application",
    OperationalAffordanceType.CREATE_NEW_CONTEXT: "open_tab",
    OperationalAffordanceType.NAVIGATE_TO_RESOURCE: "navigate_to_location",
    OperationalAffordanceType.SELECT_IDENTITY_CONTEXT: "select_entity_from_collection",
    OperationalAffordanceType.FOCUS_INPUT: "fill_field",
    OperationalAffordanceType.SEARCH_WITH_QUERY: "search_content",
    OperationalAffordanceType.SUBMIT_INPUT: "submit_input",
    OperationalAffordanceType.BROWSE_CONTENT: "scroll_results",
    OperationalAffordanceType.OPEN_MENU: "switch_context",
    OperationalAffordanceType.CHOOSE_OPTION: "select_entity_from_collection",
    OperationalAffordanceType.CONFIRM_ACTION: "confirm_action",
    OperationalAffordanceType.DISMISS_DIALOG: "close_context",
    OperationalAffordanceType.UNKNOWN: "",
}


@dataclass
class AffordanceClassificationContext:
    """Entrada multi-señal para UOAC."""

    freeze: Optional[OperationalFreezeSnapshot] = None
    primary_target: Optional[OperationalFreezeTarget] = None
    previous_event: Optional[RawEvent] = None
    next_event: Optional[RawEvent] = None
    current_event: Optional[RawEvent] = None
    icon_signature: str = ""
    uia_role: str = ""
    uia_control_type: str = ""
    dom_role: str = ""
    dom_aria: str = ""
    ocr_nearby: str = ""
    tooltip: str = ""
    parent_hierarchy: List[str] = field(default_factory=list)
    surrounding_labels: List[str] = field(default_factory=list)
    surface_type: str = ""
    collection_entity_count: int = 0
    in_collection: bool = False
    position_hint: str = ""
    outcome_transition: str = ""
    historical_success_affordance: str = ""
    visual_pattern: str = ""


def _txt(x: Any) -> str:
    return str(x or "").strip()


def _lower(x: Any) -> str:
    return _txt(x).lower()


def _contains_any(hay: str, needles: Sequence[str]) -> bool:
    low = _lower(hay)
    return any(n in low for n in needles if n)


def _hierarchy_text(parts: Sequence[str]) -> str:
    return " ".join(_lower(p) for p in parts if p)


def _primary_target_from_freeze(
    freeze: OperationalFreezeSnapshot,
) -> Optional[OperationalFreezeTarget]:
    best = freeze.best_entity_candidate
    best_name = _txt(best.display_name if best else "")
    for seq in (freeze.uia_targets, freeze.ocr_targets, freeze.dom_targets):
        for tgt in seq or []:
            if isinstance(tgt, dict):
                name = _txt(tgt.get("name") or tgt.get("text"))
                role = _txt(tgt.get("role") or tgt.get("control_type"))
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


def build_context_from_freeze(
    freeze: OperationalFreezeSnapshot,
    *,
    previous_event: Optional[RawEvent] = None,
    next_event: Optional[RawEvent] = None,
    current_event: Optional[RawEvent] = None,
    historical_success_affordance: str = "",
) -> AffordanceClassificationContext:
    """Construye contexto UOAC desde snapshot PCOF."""
    from app.services.runtime.pre_click_operational_freeze import is_pcof_affordance_label

    tgt = _primary_target_from_freeze(freeze)
    role = _lower(getattr(tgt, "role", "") if tgt else "")
    ctrl = _lower(getattr(tgt, "role", "") if tgt else "")
    if tgt and hasattr(tgt, "role"):
        ctrl = _lower(getattr(tgt, "role", ""))
    name = _txt(getattr(tgt, "name", "") if tgt else "")
    if not name and freeze.best_entity_candidate:
        name = _txt(freeze.best_entity_candidate.display_name)

    hierarchy = list(freeze.hierarchy_chain or [])
    if tgt and getattr(tgt, "parent_chain", None):
        hierarchy = list(tgt.parent_chain or []) + hierarchy

    coll = freeze.best_collection_candidate
    coll_count = int(coll.entity_count if coll else 0) or len(coll.entities or []) if coll else 0
    in_coll = bool(coll and coll_count > 1)

    dom_role = ""
    dom_aria = ""
    for dt in freeze.dom_targets or []:
        if isinstance(dt, dict):
            dom_role = dom_role or _txt(dt.get("role"))
            dom_aria = dom_aria or _txt(dt.get("aria_label") or dt.get("name"))
        else:
            dom_role = dom_role or _txt(getattr(dt, "role", ""))

    ocr_bits = list(freeze.nearby_labels or [])
    for ot in freeze.ocr_targets or []:
        if isinstance(ot, dict):
            ocr_bits.append(_txt(ot.get("text") or ot.get("name")))
        else:
            ocr_bits.append(_txt(getattr(ot, "text", "") or getattr(ot, "name", "")))

    surface = "generic"
    htext = _hierarchy_text(hierarchy)
    win = dict(freeze.window_context or {})
    proc = _lower(win.get("process_name") or win.get("process"))
    if _contains_any(htext, _LAUNCHER_MARKERS) or _contains_any(proc, ("searchui", "launcher")):
        surface = "application_launcher"
    elif _contains_any(htext, _TAB_BAR_MARKERS):
        surface = "tab_bar"
    elif in_coll:
        surface = "collection_surface"
    elif _contains_any(role, _INPUT_CONTROL_ROLES) or _contains_any(dom_role, _INPUT_CONTROL_ROLES):
        surface = "input_surface"

    icon_sig = _lower(freeze.visual_group_signature or freeze.layout_signature or "")
    if name in ("+", "＋") or _contains_any(name, _PLUS_ICON_MARKERS):
        icon_sig = (icon_sig + " plus_icon").strip()

    position_hint = ""
    if surface == "tab_bar":
        position_hint = "tab_bar"
    elif surface == "application_launcher":
        position_hint = "launcher_grid"

    if is_pcof_affordance_label(name):
        surface = "auxiliary_control"

    return AffordanceClassificationContext(
        freeze=freeze,
        primary_target=tgt,
        previous_event=previous_event,
        next_event=next_event,
        current_event=current_event,
        icon_signature=icon_sig,
        uia_role=role,
        uia_control_type=ctrl,
        dom_role=_lower(dom_role),
        dom_aria=_lower(dom_aria),
        ocr_nearby=" ".join(ocr_bits[:8]),
        tooltip=_txt(getattr(tgt, "text", "") if tgt else ""),
        parent_hierarchy=hierarchy,
        surrounding_labels=list(freeze.nearby_labels or [])[:12],
        surface_type=surface,
        collection_entity_count=coll_count,
        in_collection=in_coll,
        position_hint=position_hint,
        outcome_transition=_txt(freeze.trigger),
        historical_success_affordance=historical_success_affordance,
        visual_pattern=_lower(freeze.layout_signature),
    )


def build_context_from_raw_event(
    event: RawEvent,
    *,
    freeze: Optional[OperationalFreezeSnapshot] = None,
    previous_event: Optional[RawEvent] = None,
    next_event: Optional[RawEvent] = None,
) -> AffordanceClassificationContext:
    if freeze is not None:
        return build_context_from_freeze(
            freeze,
            previous_event=previous_event,
            next_event=next_event,
            current_event=event,
        )
    meta = dict(event.metadata or {})
    fr_raw = meta.get("operational_freeze_snapshot")
    if isinstance(fr_raw, dict):
        try:
            fr = OperationalFreezeSnapshot.model_validate(fr_raw)
            return build_context_from_freeze(
                fr,
                previous_event=previous_event,
                next_event=next_event,
                current_event=event,
            )
        except Exception:
            pass
    return AffordanceClassificationContext(
        current_event=event,
        previous_event=previous_event,
        next_event=next_event,
    )


def _event_is_scroll(event: Optional[RawEvent]) -> bool:
    if event is None:
        return False
    return event.event_type in (
        EventType.MOUSE_SCROLL,
        EventType.MOUSE_DRAG,
    )


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


def _label_blob(ctx: AffordanceClassificationContext) -> str:
    parts = [
        _txt(getattr(ctx.primary_target, "name", "") if ctx.primary_target else ""),
        ctx.ocr_nearby,
        ctx.tooltip,
        ctx.dom_aria,
        " ".join(ctx.surrounding_labels),
    ]
    if ctx.freeze and ctx.freeze.best_entity_candidate:
        parts.append(_txt(ctx.freeze.best_entity_candidate.display_name))
    return " ".join(p for p in parts if p)


def _is_input_affordance_label(label: str) -> bool:
    from app.services.runtime.entity_resolution_layer import is_generic_entity_label
    from app.services.runtime.pre_click_operational_freeze import is_pcof_affordance_label

    if not label or is_pcof_affordance_label(label):
        return True
    if _contains_any(label, _INPUT_PLACEHOLDER_MARKERS):
        return True
    if is_generic_entity_label(label):
        return True
    if _contains_any(label, _NEW_CONTEXT_MARKERS):
        return True
    low = _lower(label)
    if len(low) <= 24 and _contains_any(low, ("buscar", "search", "escribe", "type", "find")):
        return True
    return False


def _score_affordances(
    ctx: AffordanceClassificationContext,
) -> Dict[OperationalAffordanceType, Tuple[float, List[str]]]:
    """Votación por señales estructurales (sin nombres de producto)."""
    scores: Dict[OperationalAffordanceType, Tuple[float, List[str]]] = {
        t: (0.0, []) for t in OperationalAffordanceType
    }

    def add(t: OperationalAffordanceType, w: float, reason: str) -> None:
        cur, rs = scores[t]
        scores[t] = (cur + w, rs + [reason])

    label = _label_blob(ctx)
    role = _lower(ctx.uia_role or ctx.uia_control_type)
    dom = _lower(ctx.dom_role)
    hier = _hierarchy_text(ctx.parent_hierarchy)

    if _event_is_scroll(ctx.current_event):
        add(OperationalAffordanceType.BROWSE_CONTENT, 0.95, "mouse_scroll_event")

    if _event_is_text_entry(ctx.next_event) and (
        _contains_any(role, _INPUT_CONTROL_ROLES) or _contains_any(dom, _INPUT_CONTROL_ROLES)
    ):
        add(OperationalAffordanceType.SEARCH_WITH_QUERY, 1.05, "input_then_type_text")
        add(OperationalAffordanceType.FOCUS_INPUT, 0.2, "input_focus_precursor")

    if _event_is_submit(ctx.next_event):
        add(OperationalAffordanceType.SUBMIT_INPUT, 1.0, "submit_enter_next")
        if _contains_any(role, _INPUT_CONTROL_ROLES) or _contains_any(dom, _INPUT_CONTROL_ROLES):
            add(OperationalAffordanceType.SEARCH_WITH_QUERY, 0.45, "submit_on_input_surface")
        add(OperationalAffordanceType.FOCUS_INPUT, 0.15, "submit_not_primary_focus")

    if (
        _contains_any(ctx.icon_signature, _PLUS_ICON_MARKERS)
        or _txt(getattr(ctx.primary_target, "name", "") if ctx.primary_target else "") in ("+", "＋")
        or _contains_any(label, _PLUS_ICON_MARKERS)
    ):
        if ctx.position_hint == "tab_bar" or _contains_any(hier, _TAB_BAR_MARKERS):
            add(OperationalAffordanceType.CREATE_NEW_CONTEXT, 0.92, "plus_in_tab_bar")
        else:
            add(OperationalAffordanceType.CREATE_NEW_CONTEXT, 0.65, "plus_icon_generic")

    if _contains_any(label, _NEW_CONTEXT_MARKERS) or _contains_any(hier, _NEW_CONTEXT_MARKERS):
        add(OperationalAffordanceType.CREATE_NEW_CONTEXT, 0.88, "new_context_label")

    _has_input_successor = _event_is_text_entry(ctx.next_event) or _event_is_submit(
        ctx.next_event,
    )
    if _contains_any(role, _INPUT_CONTROL_ROLES) or _contains_any(dom, _INPUT_CONTROL_ROLES):
        if not _has_input_successor:
            add(OperationalAffordanceType.FOCUS_INPUT, 0.75, "input_control_role")
        if _is_input_affordance_label(label) and not _has_input_successor:
            add(OperationalAffordanceType.FOCUS_INPUT, 0.25, "placeholder_label")

    if _is_input_affordance_label(label) and not _has_input_successor:
        add(OperationalAffordanceType.FOCUS_INPUT, 0.7, "non_entity_input_label")
        add(OperationalAffordanceType.SEARCH_WITH_QUERY, 0.45, "search_surface_label")

    if ctx.surface_type == "application_launcher" or _contains_any(hier, _LAUNCHER_MARKERS):
        add(OperationalAffordanceType.ACTIVATE_LAUNCHER, 0.7, "launcher_surface")
        if ctx.in_collection and ctx.collection_entity_count >= 2:
            add(OperationalAffordanceType.NAVIGATE_TO_RESOURCE, 0.55, "launcher_tile_in_grid")

    if ctx.in_collection and ctx.collection_entity_count >= 2:
        if _contains_any(role, _COLLECTION_ITEM_ROLES):
            if _contains_any(hier, _IDENTITY_COLLECTION_MARKERS) or _contains_any(
                _hierarchy_text([_txt(ctx.freeze.best_collection_candidate.display_name)
                                 if ctx.freeze and ctx.freeze.best_collection_candidate else ""]),
                _IDENTITY_COLLECTION_MARKERS,
            ):
                add(OperationalAffordanceType.SELECT_IDENTITY_CONTEXT, 0.88, "identity_collection_item")
            else:
                add(OperationalAffordanceType.CHOOSE_OPTION, 0.72, "collection_option")
                add(OperationalAffordanceType.SELECT_IDENTITY_CONTEXT, 0.35, "collection_item_generic")

    if _contains_any(label, _MENU_MARKERS) or _contains_any(hier, _MENU_MARKERS):
        add(OperationalAffordanceType.OPEN_MENU, 0.78, "menu_affordance")

    if _contains_any(label, _CONFIRM_MARKERS) and _contains_any(role, _BUTTON_ROLES):
        add(OperationalAffordanceType.CONFIRM_ACTION, 0.8, "confirm_button")

    if _contains_any(label, _DISMISS_MARKERS) and _contains_any(role, _BUTTON_ROLES):
        add(OperationalAffordanceType.DISMISS_DIALOG, 0.8, "dismiss_button")

    if _contains_any(role, _BUTTON_ROLES) and not ctx.in_collection:
        if ctx.surface_type == "application_launcher":
            add(OperationalAffordanceType.NAVIGATE_TO_RESOURCE, 0.5, "launcher_button")
        if not _txt(label) or len(_txt(label)) <= 1:
            add(OperationalAffordanceType.UNKNOWN, 0.82, "unlabeled_button")

    if ctx.historical_success_affordance:
        try:
            hist = OperationalAffordanceType(ctx.historical_success_affordance)
            add(hist, 0.15, "historical_success_memory")
        except ValueError:
            pass

    # Penalizar select_entity cuando la etiqueta parece campo de entrada, no entidad
    sel_score, sel_rs = scores[OperationalAffordanceType.SELECT_IDENTITY_CONTEXT]
    if _is_input_affordance_label(label):
        scores[OperationalAffordanceType.SELECT_IDENTITY_CONTEXT] = (
            max(0.0, sel_score - 0.9),
            sel_rs + ["penalize_uia_label_as_entity"],
        )

    return scores


def _human_label_for_affordance(
    aff_type: OperationalAffordanceType,
    ctx: AffordanceClassificationContext,
    *,
    query_text: str = "",
) -> str:
    if aff_type == OperationalAffordanceType.CREATE_NEW_CONTEXT:
        return "Abrir nueva pestaña"
    if aff_type == OperationalAffordanceType.FOCUS_INPUT:
        return "Enfocar campo de entrada"
    if aff_type == OperationalAffordanceType.SEARCH_WITH_QUERY:
        if query_text:
            return f'Buscar "{query_text}"'
        return "Buscar contenido"
    if aff_type == OperationalAffordanceType.SUBMIT_INPUT:
        return "Confirmar búsqueda"
    if aff_type == OperationalAffordanceType.BROWSE_CONTENT:
        return "Explorar contenido"
    if aff_type == OperationalAffordanceType.ACTIVATE_LAUNCHER:
        return "Activar lanzador"
    if aff_type == OperationalAffordanceType.NAVIGATE_TO_RESOURCE:
        name = _txt(
            getattr(ctx.primary_target, "name", "") if ctx.primary_target else "",
        )
        if name and not _is_input_affordance_label(name):
            return f"Abrir {name}"
        return "Navegar a recurso"
    if aff_type == OperationalAffordanceType.OPEN_MENU:
        return "Abrir menú"
    if aff_type == OperationalAffordanceType.CONFIRM_ACTION:
        return "Confirmar acción"
    if aff_type == OperationalAffordanceType.DISMISS_DIALOG:
        return "Cerrar diálogo"
    if aff_type == OperationalAffordanceType.SELECT_IDENTITY_CONTEXT:
        ent = ctx.freeze.best_entity_candidate if ctx.freeze else None
        name = _txt(ent.display_name if ent else "")
        if name and not _is_input_affordance_label(name):
            return f"Seleccionar {name}"
        return "Seleccionar identidad"
    if aff_type == OperationalAffordanceType.CHOOSE_OPTION:
        name = _txt(
            getattr(ctx.primary_target, "name", "") if ctx.primary_target else "",
        )
        if name:
            return f"Elegir opción {name}"
        return "Elegir opción"
    return ""


def classify_operational_affordance(
    ctx: AffordanceClassificationContext,
) -> OperationalAffordance:
    """Clasifica la función operacional del elemento (UOAC)."""
    scores = _score_affordances(ctx)

    # UOEI refuerza clasificación desde identidad de elemento (no label UIA).
    if ctx.freeze is not None:
        try:
            from app.services.runtime.universal_operational_element_identity import (
                identify_from_freeze,
            )
            from app.contracts.mission import OperationalElementType

            ident = identify_from_freeze(
                ctx.freeze,
                previous_event=ctx.previous_event,
                next_event=ctx.next_event,
                current_event=ctx.current_event,
            )
            _UOEI_AFFORDANCE_BOOST: Dict[OperationalElementType, Tuple[OperationalAffordanceType, float]] = {
                OperationalElementType.LAUNCHER_INPUT_SURFACE: (
                    OperationalAffordanceType.ACTIVATE_LAUNCHER, 0.35,
                ),
                OperationalElementType.SEARCH_INPUT_SURFACE: (
                    OperationalAffordanceType.SEARCH_WITH_QUERY, 0.35,
                ),
                OperationalElementType.CONTEXT_CREATION_AFFORDANCE: (
                    OperationalAffordanceType.CREATE_NEW_CONTEXT, 0.4,
                ),
                OperationalElementType.SELECTABLE_IDENTITY: (
                    OperationalAffordanceType.SELECT_IDENTITY_CONTEXT, 0.4,
                ),
                OperationalElementType.SCROLLABLE_RESULTS_SURFACE: (
                    OperationalAffordanceType.BROWSE_CONTENT, 0.35,
                ),
                OperationalElementType.NAVIGATION_RESOURCE: (
                    OperationalAffordanceType.NAVIGATE_TO_RESOURCE, 0.3,
                ),
                OperationalElementType.CONFIRMATION_AFFORDANCE: (
                    OperationalAffordanceType.CONFIRM_ACTION, 0.35,
                ),
            }
            boost = _UOEI_AFFORDANCE_BOOST.get(ident.operational_element_type)
            if boost:
                aff_t, w = boost
                cur, rs = scores[aff_t]
                scores[aff_t] = (cur + w, rs + [f"uoei:{ident.operational_element_type.value}"])
        except Exception:
            pass

    ranked = sorted(
        ((t, s, rs) for t, (s, rs) in scores.items() if s > 0),
        key=lambda row: row[1],
        reverse=True,
    )

    if not ranked:
        return OperationalAffordance(
            affordance_type=OperationalAffordanceType.UNKNOWN,
            confidence=0.35,
            evidence_sources=["no_structural_signal"],
            operational_intent_kind=OperationalAffordanceType.UNKNOWN.value,
            ambiguity_reason="insufficient_evidence",
            should_materialize_as_step=False,
        )

    best_type, best_score, reasons = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    confidence = min(1.0, max(0.35, best_score - second_score * 0.35))

    if best_type == OperationalAffordanceType.UNKNOWN:
        confidence = min(confidence, 0.5)

    query_text = ""
    if ctx.next_event and _event_is_text_entry(ctx.next_event):
        meta = dict(ctx.next_event.metadata or {})
        query_text = _txt(
            meta.get("final_text") or meta.get("text") or meta.get("typed_text") or "",
        )

    merge_adjacent = best_type in (
        OperationalAffordanceType.SEARCH_WITH_QUERY,
        OperationalAffordanceType.SUBMIT_INPUT,
    ) or (
        best_type == OperationalAffordanceType.SUBMIT_INPUT
        and _event_is_text_entry(ctx.previous_event)
    )

    transition = ""
    if ctx.freeze:
        risk = getattr(ctx.freeze.transition_risk, "value", str(ctx.freeze.transition_risk))
        transition = _txt(risk) or ctx.outcome_transition

    materialize = best_type != OperationalAffordanceType.UNKNOWN or confidence >= 0.55
    if _is_input_affordance_label(_label_blob(ctx)) and best_type in (
        OperationalAffordanceType.SELECT_IDENTITY_CONTEXT,
        OperationalAffordanceType.CHOOSE_OPTION,
    ):
        materialize = False
        best_type = OperationalAffordanceType.FOCUS_INPUT
        confidence = max(confidence, 0.72)
        reasons = reasons + ["override_uia_label_not_entity"]

    ambiguity = ""
    if second_score >= best_score * 0.85 and len(ranked) > 1:
        ambiguity = f"close_runner_up:{ranked[1][0].value}"

    hl = _human_label_for_affordance(best_type, ctx, query_text=query_text)

    return OperationalAffordance(
        affordance_type=best_type,
        confidence=round(confidence, 4),
        evidence_sources=reasons[:10],
        operational_intent_kind=best_type.value,
        uol_action_hint=_UOL_HINT_BY_AFFORDANCE.get(best_type, ""),
        human_label_hint=hl,
        should_materialize_as_step=bool(materialize),
        should_merge_with_adjacent_events=bool(merge_adjacent),
        transition_expectation=transition,
        ambiguity_reason=ambiguity,
    )


def classify_from_freeze(
    freeze: OperationalFreezeSnapshot,
    *,
    previous_event: Optional[RawEvent] = None,
    next_event: Optional[RawEvent] = None,
    current_event: Optional[RawEvent] = None,
    historical_success_affordance: str = "",
) -> OperationalAffordance:
    ctx = build_context_from_freeze(
        freeze,
        previous_event=previous_event,
        next_event=next_event,
        current_event=current_event,
        historical_success_affordance=historical_success_affordance,
    )
    return classify_operational_affordance(ctx)


def classify_from_raw_event(
    event: RawEvent,
    *,
    freeze: Optional[OperationalFreezeSnapshot] = None,
    previous_event: Optional[RawEvent] = None,
    next_event: Optional[RawEvent] = None,
) -> OperationalAffordance:
    if event.event_type == EventType.MOUSE_SCROLL:
        ctx = AffordanceClassificationContext(current_event=event)
        return classify_operational_affordance(ctx)
    ctx = build_context_from_raw_event(
        event,
        freeze=freeze,
        previous_event=previous_event,
        next_event=next_event,
    )
    return classify_operational_affordance(ctx)


def attach_affordance_to_metadata(
    metadata: Dict[str, Any],
    affordance: OperationalAffordance,
) -> Dict[str, Any]:
    metadata["operational_affordance"] = affordance.model_dump(mode="json")
    return metadata


def affordance_from_metadata(metadata: Dict[str, Any]) -> Optional[OperationalAffordance]:
    raw = metadata.get("operational_affordance")
    if not isinstance(raw, dict):
        return None
    try:
        return OperationalAffordance.model_validate(raw)
    except Exception:
        return None


def should_promote_entity_coi(affordance: OperationalAffordance) -> bool:
    """``True`` solo cuando UOAC clasifica selección de identidad/opción en colección."""
    return affordance.affordance_type in (
        OperationalAffordanceType.SELECT_IDENTITY_CONTEXT,
        OperationalAffordanceType.CHOOSE_OPTION,
    ) and affordance.confidence >= 0.55


def uoac_expert_lines(metadata: Dict[str, Any]) -> List[str]:
    aff = affordance_from_metadata(metadata)
    if aff is None:
        return []
    return [
        "Universal Operational Affordance (UOAC):",
        f"  type: {aff.affordance_type.value} ({aff.confidence:.2f})",
        f"  uol_hint: {aff.uol_action_hint or '—'}",
        f"  human_hint: {aff.human_label_hint or '—'}",
        f"  evidence: {', '.join(aff.evidence_sources[:6]) or '—'}",
    ]


__all__ = [
    "AffordanceClassificationContext",
    "OperationalAffordance",
    "OperationalAffordanceType",
    "attach_affordance_to_metadata",
    "affordance_from_metadata",
    "build_context_from_freeze",
    "build_context_from_raw_event",
    "classify_from_freeze",
    "classify_from_raw_event",
    "classify_operational_affordance",
    "should_promote_entity_coi",
    "uoac_expert_lines",
]
