"""
Pre-Click Operational Freeze (PCOF)
------------------------------------
Congela identidad operacional viva ANTES de mouse_up y transiciones de UI.

Regla: ``post_action_state`` solo valida outcome; nunca identifica el target.
PCOF captura colección + entidad + señales multi-fuente en el instante PRE-click.

Flag: ``settings.PCOF_ENABLED`` (default False).
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    OperationalFreezeCollectionSnapshot,
    OperationalFreezeEntityCandidate,
    OperationalFreezeSnapshot,
    OperationalFreezeTarget,
    TransitionRiskLevel,
)
from app.core.logger import log

# Reutiliza detección de colección universal (sin hardcode por app).
from app.services.runtime.universal_collection_entity_engine import (
    detect_operational_collections,
    extract_entities_from_collection,
)
from app.services.runtime.entity_resolution_layer import (
    is_generic_entity_label,
    normalize_entity_name,
)

# Umbrales estructurales (no app-specific)
FREEZE_CONFIDENCE_MIN = 0.72
FREEZE_QUALITY_DEEP = "deep"
FREEZE_QUALITY_STANDARD = "standard"
FREEZE_QUALITY_MINIMAL = "minimal"
HOVER_DEBOUNCE_MS = 45
Mousedown_DEBOUNCE_MS = 12

_DYNAMIC_SURFACE_MARKERS: Tuple[str, ...] = (
    "loading", "spinner", "progress", "busy", "skeleton", "placeholder",
    "virtual", "lazy", "async", "refresh", "animat", "transition",
    "updating", "searching", "cargando", "cargando…",
)

_HIGH_RISK_CONTROL_MARKERS: Tuple[str, ...] = (
    "listitem", "menuitem", "tabitem", "combobox", "popup", "flyout",
    "autocomplete", "suggestion", "launcher", "picker", "profile",
)

_HIGH_RISK_CONTEXT_MARKERS: Tuple[str, ...] = (
    "search", "result", "profile", "picker", "launcher", "menu",
    "autocomplete", "suggestion", "dropdown", "overlay", "dialog",
)

_ITEM_ROLES: Tuple[str, ...] = (
    "listitem", "treeitem", "tabitem", "menuitem", "dataitem",
    "option", "row", "gridcell", "article", "card", "link",
)


def pcof_enabled(settings_obj: Optional[Any] = None) -> bool:
    if settings_obj is None:
        try:
            from app.core.config import get_settings

            settings_obj = get_settings()
        except Exception:
            return False
    return bool(getattr(settings_obj, "PCOF_ENABLED", False))


def _txt(x: Any) -> str:
    return str(x or "").strip()


def _lower(x: Any) -> str:
    return _txt(x).lower()


def _bbox_overlap(
    click_xy: Tuple[int, int],
    bbox: Optional[Dict[str, Any]],
) -> float:
    if not bbox or not click_xy:
        return 0.0
    try:
        cx, cy = int(click_xy[0]), int(click_xy[1])
        left = int(bbox.get("left", bbox.get("x", 0)))
        top = int(bbox.get("top", bbox.get("y", 0)))
        w = int(bbox.get("width", bbox.get("w", 0)))
        h = int(bbox.get("height", bbox.get("h", 0)))
        if w <= 0 or h <= 0:
            return 0.0
        if left <= cx <= left + w and top <= cy <= top + h:
            return 1.0
        # distancia normalizada al centro
        mx, my = left + w / 2, top + h / 2
        dist = ((cx - mx) ** 2 + (cy - my) ** 2) ** 0.5
        diag = (w ** 2 + h ** 2) ** 0.5 or 1.0
        return max(0.0, 1.0 - dist / diag)
    except (TypeError, ValueError):
        return 0.0


def _flatten_uia_nodes(uia: Any, *, max_nodes: int = 320) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []

    def walk(n: Any) -> None:
        if len(nodes) >= max_nodes:
            return
        if isinstance(n, dict):
            nodes.append(n)
            for ch in n.get("children") or []:
                walk(ch)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(uia)
    return nodes


def _dom_nodes(dom: Any) -> List[Dict[str, Any]]:
    if not isinstance(dom, dict):
        return []
    raw = dom.get("nodes")
    if isinstance(raw, list):
        return [n for n in raw if isinstance(n, dict)][:400]
    return []


def score_transition_risk(
    *,
    uia: Optional[Dict[str, Any]] = None,
    dom: Optional[Dict[str, Any]] = None,
    window_ctx: Optional[Dict[str, Any]] = None,
    flat_nodes: Optional[Sequence[Dict[str, Any]]] = None,
    collection_count: int = 0,
) -> TransitionRiskLevel:
    """Score estructural de riesgo de transición (sin reglas por app)."""
    score = 0
    uia = dict(uia or {})
    dom = dict(dom or {})
    wc = dict(window_ctx or {})

    ct = _lower(uia.get("control_type") or uia.get("role"))
    name = _lower(uia.get("name"))
    ctx_blob = " ".join([
        ct, name,
        _lower(wc.get("title")),
        _lower(dom.get("url")),
    ])
    for m in _HIGH_RISK_CONTROL_MARKERS:
        if m in ct or m in name:
            score += 2
    for m in _HIGH_RISK_CONTEXT_MARKERS:
        if m in ctx_blob:
            score += 1

    if collection_count >= 2:
        score += 2
    elif collection_count == 1:
        score += 1

    nodes = list(flat_nodes or [])
    item_count = sum(
        1 for n in nodes
        if any(m in _lower(n.get("role") or n.get("control_type")) for m in _ITEM_ROLES)
        and n.get("name")
    )
    if item_count >= 4:
        score += 2
    elif item_count >= 2:
        score += 1

    if score >= 5:
        return TransitionRiskLevel.HIGH
    if score >= 2:
        return TransitionRiskLevel.MEDIUM
    return TransitionRiskLevel.LOW


def detect_dynamic_surface(
    *,
    uia: Optional[Dict[str, Any]] = None,
    dom: Optional[Dict[str, Any]] = None,
    flat_nodes: Optional[Sequence[Dict[str, Any]]] = None,
) -> bool:
    """Superficie inestable: loading, virtualización, async, animación."""
    blob_parts: List[str] = []
    if uia:
        blob_parts.extend([
            _lower(uia.get("name")),
            _lower(uia.get("control_type")),
            _lower(uia.get("class_name")),
        ])
    if dom:
        blob_parts.append(_lower(dom.get("url")))
    for n in flat_nodes or []:
        blob_parts.append(_lower(n.get("name")))
        blob_parts.append(_lower(n.get("aria_label")))
        st = n.get("states") or n.get("state")
        if isinstance(st, (list, tuple)):
            blob_parts.extend(_lower(s) for s in st)
        elif st:
            blob_parts.append(_lower(st))
    blob = " ".join(blob_parts)
    return any(m in blob for m in _DYNAMIC_SURFACE_MARKERS)


def _targets_from_uia(
    flat: Sequence[Dict[str, Any]],
    click_xy: Tuple[int, int],
    *,
    max_targets: int = 48,
) -> List[OperationalFreezeTarget]:
    scored: List[Tuple[float, OperationalFreezeTarget]] = []
    for n in flat:
        name = _txt(n.get("name"))
        role = _lower(n.get("role") or n.get("control_type"))
        if not name and not n.get("automation_id"):
            continue
        bbox = n.get("bbox") if isinstance(n.get("bbox"), dict) else {}
        ov = _bbox_overlap(click_xy, bbox)
        conf = 0.35 + 0.45 * ov
        if name and not is_generic_entity_label(name):
            conf += 0.12
        if n.get("automation_id"):
            conf += 0.08
        tgt = OperationalFreezeTarget(
            source="uia",
            role=role,
            name=name,
            automation_id=_txt(n.get("automation_id")),
            bbox=dict(bbox),
            confidence=min(1.0, conf),
            parent_chain=[str(p) for p in (n.get("parent_chain") or []) if p][:12],
            overlap_score=ov,
        )
        scored.append((conf, tgt))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [t for _, t in scored[:max_targets]]


def _targets_from_ocr(
    ocr: Optional[Dict[str, Any]],
    click_xy: Tuple[int, int],
) -> List[OperationalFreezeTarget]:
    if not ocr:
        return []
    lines = ocr.get("lines") or ocr.get("text_lines") or []
    if isinstance(ocr.get("text"), str) and not lines:
        lines = [ln.strip() for ln in ocr["text"].splitlines() if ln.strip()]
    out: List[OperationalFreezeTarget] = []
    for ln in lines[:24]:
        text = _txt(ln if isinstance(ln, str) else (ln.get("text") if isinstance(ln, dict) else ""))
        if not text or is_generic_entity_label(text):
            continue
        bbox = {}
        if isinstance(ln, dict) and isinstance(ln.get("bbox"), dict):
            bbox = dict(ln["bbox"])
        conf = float(ln.get("confidence", 0.75)) if isinstance(ln, dict) else 0.72
        out.append(
            OperationalFreezeTarget(
                source="ocr",
                role="text",
                name=text,
                text=text,
                bbox=bbox,
                confidence=min(1.0, conf),
                overlap_score=_bbox_overlap(click_xy, bbox) if bbox else 0.25,
            ),
        )
    return out


def _targets_from_dom(
    dom: Optional[Dict[str, Any]],
    click_xy: Tuple[int, int],
) -> List[OperationalFreezeTarget]:
    out: List[OperationalFreezeTarget] = []
    for n in _dom_nodes(dom):
        text = _txt(
            n.get("visible_text") or n.get("inner_text") or n.get("aria_label") or n.get("name"),
        )
        if not text or is_generic_entity_label(text):
            continue
        bbox = n.get("bbox") if isinstance(n.get("bbox"), dict) else {}
        role = _lower(n.get("role") or n.get("tag"))
        sel = _txt(n.get("selector") or n.get("css") or n.get("xpath"))
        out.append(
            OperationalFreezeTarget(
                source="dom",
                role=role,
                name=text,
                text=text,
                dom_selector=sel,
                bbox=dict(bbox),
                confidence=0.68 + 0.2 * _bbox_overlap(click_xy, bbox),
                overlap_score=_bbox_overlap(click_xy, bbox),
            ),
        )
    return out[:40]


def _nearby_labels_from_targets(
    targets: Sequence[OperationalFreezeTarget],
    *,
    click_xy: Tuple[int, int],
    radius_px: int = 180,
) -> List[str]:
    labels: List[str] = []
    cx, cy = click_xy
    for t in targets:
        text = _txt(t.name or t.text)
        if not text or is_generic_entity_label(text):
            continue
        bb = t.bbox
        if bb:
            mx = int(bb.get("left", 0)) + int(bb.get("width", 0)) // 2
            my = int(bb.get("top", 0)) + int(bb.get("height", 0)) // 2
            if abs(mx - cx) + abs(my - cy) > radius_px * 2:
                continue
        labels.append(text)
    return list(dict.fromkeys(labels))[:16]


def _fuse_entity_candidates(
    *,
    uia_targets: Sequence[OperationalFreezeTarget],
    ocr_targets: Sequence[OperationalFreezeTarget],
    dom_targets: Sequence[OperationalFreezeTarget],
    click_xy: Tuple[int, int],
    collection_entities: Sequence[OperationalFreezeEntityCandidate],
) -> List[OperationalFreezeEntityCandidate]:
    """Fusión multi-señal: UIA + OCR + DOM + overlap + colección."""
    by_key: Dict[str, OperationalFreezeEntityCandidate] = {}

    def absorb(label: str, source: str, conf: float, **extra: Any) -> None:
        label = _txt(label)
        if not label or is_generic_entity_label(label):
            return
        key = normalize_entity_name(label) or label.lower()
        cur = by_key.get(key)
        if cur is None:
            cur = OperationalFreezeEntityCandidate(
                display_name=label,
                normalized_name=key,
                evidence_sources=[source],
                confidence=conf,
                **{k: v for k, v in extra.items() if k in (
                    "uia_name", "ocr_text", "dom_text", "bbox", "collection_role",
                )},
            )
            by_key[key] = cur
            return
        if source not in cur.evidence_sources:
            cur.evidence_sources.append(source)
        cur.confidence = min(1.0, cur.confidence + conf * 0.35)
        for k, v in extra.items():
            if k == "uia_name" and v:
                cur.uia_name = str(v)
            elif k == "ocr_text" and v:
                cur.ocr_text = str(v)
            elif k == "dom_text" and v:
                cur.dom_text = str(v)
            elif k == "bbox" and v and not cur.bbox:
                cur.bbox = dict(v)

    for t in uia_targets:
        if t.overlap_score >= 0.15 or t.confidence >= 0.55:
            absorb(
                t.name, "uia", t.confidence * max(0.5, t.overlap_score),
                uia_name=t.name, bbox=t.bbox,
            )
    for t in ocr_targets:
        absorb(t.text or t.name, "ocr", t.confidence * 0.9, ocr_text=t.text or t.name, bbox=t.bbox)
    for t in dom_targets:
        if t.overlap_score >= 0.1:
            absorb(
                t.name, "dom", t.confidence * max(0.45, t.overlap_score),
                dom_text=t.text or t.name, bbox=t.bbox,
            )
    for ent in collection_entities:
        absorb(
            ent.display_name, "collection",
            ent.confidence * 0.95,
            collection_role=ent.collection_role,
        )

    ranked = sorted(by_key.values(), key=lambda c: c.confidence, reverse=True)
    if len(ranked) >= 2:
        gap = ranked[0].confidence - ranked[1].confidence
        if gap < 0.08:
            for c in ranked:
                c.absorbed_ambiguity = True
        else:
            ranked[0].absorbed_ambiguity = False
    return ranked


def _collection_snapshots_from_detected(
    collections: Sequence[Any],
) -> Tuple[
    List[OperationalFreezeCollectionSnapshot],
    List[OperationalFreezeEntityCandidate],
]:
    snaps: List[OperationalFreezeCollectionSnapshot] = []
    entities: List[OperationalFreezeEntityCandidate] = []
    for coll in collections:
        ctype = getattr(coll.collection_type, "value", str(coll.collection_type))
        names = [str(e.display_name) for e in (coll.entities or []) if e.display_name]
        snaps.append(
            OperationalFreezeCollectionSnapshot(
                collection_type=ctype,
                display_name=str(coll.display_name or ctype),
                entities=names,
                entity_count=len(names),
                structural_hash=str((coll.structural_signals or {}).get("structural_hash", "")),
                scrollable=bool(coll.scrollable),
                confidence=float(coll.confidence or 0.0),
            ),
        )
        for ent in coll.entities or []:
            entities.append(
                OperationalFreezeEntityCandidate(
                    display_name=str(ent.display_name),
                    normalized_name=str(ent.normalized_name or normalize_entity_name(ent.display_name)),
                    confidence=float(ent.selection_confidence or 0.7),
                    evidence_sources=["collection"],
                    collection_role=str(ent.collection_role or ""),
                ),
            )
    return snaps, entities


def _layout_signature(flat: Sequence[Dict[str, Any]], click_xy: Tuple[int, int]) -> str:
    names = sorted(
        normalize_entity_name(_txt(n.get("name")))
        for n in flat
        if n.get("name") and not is_generic_entity_label(_txt(n.get("name")))
    )[:24]
    payload = f"{click_xy[0]}:{click_xy[1]}|{'|'.join(names)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _visual_group_signature(targets: Sequence[OperationalFreezeTarget]) -> str:
    tops = sorted(
        (normalize_entity_name(t.name or t.text), round(t.overlap_score, 2))
        for t in targets[:12]
        if t.name or t.text
    )
    raw = repr(tops)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def capture_operational_freeze(
    *,
    cursor_xy: Tuple[int, int],
    window_context: Optional[Dict[str, Any]] = None,
    uia: Optional[Dict[str, Any]] = None,
    dom: Optional[Dict[str, Any]] = None,
    ocr: Optional[Dict[str, Any]] = None,
    uia_flat: Optional[Sequence[Dict[str, Any]]] = None,
    trigger: str = "mouse_up",
    pre_buffer_path: Optional[str] = None,
    hierarchy_chain: Optional[Sequence[str]] = None,
    active_affordances: Optional[Sequence[str]] = None,
) -> OperationalFreezeSnapshot:
    """Captura operacional PRE-transición (pure; inyectable en tests)."""
    click_xy = (int(cursor_xy[0]), int(cursor_xy[1]))
    wc = dict(window_context or {})
    uia_d = dict(uia or {})
    dom_d = dict(dom or {})

    flat = list(uia_flat or [])
    if not flat:
        flat = _flatten_uia_nodes(uia_d)
        if dom_d:
            flat.extend(_dom_nodes(dom_d))

    risk = score_transition_risk(
        uia=uia_d,
        dom=dom_d,
        window_ctx=wc,
        flat_nodes=flat,
    )
    dynamic = detect_dynamic_surface(uia=uia_d, dom=dom_d, flat_nodes=flat)
    depth = FREEZE_QUALITY_STANDARD
    if risk == TransitionRiskLevel.HIGH or dynamic:
        depth = FREEZE_QUALITY_DEEP
    elif risk == TransitionRiskLevel.LOW and not flat:
        depth = FREEZE_QUALITY_MINIMAL

    uia_targets = _targets_from_uia(flat, click_xy)
    ocr_targets = _targets_from_ocr(ocr, click_xy)
    dom_targets = _targets_from_dom(dom_d, click_xy)

    capture_bundle = {
        "metadata": {"uia": uia_d, "web": dom_d, "uia_flat": flat},
        "uia_flat": flat,
    }
    op_colls = detect_operational_collections(
        uia_hierarchy=flat if len(flat) >= 2 else uia_d,
        dom_hierarchy=dom_d,
        capture_bundle=capture_bundle,
    )
    for c in op_colls:
        try:
            extract_entities_from_collection(c, flat)
        except Exception:
            pass

    coll_snaps, coll_entities = _collection_snapshots_from_detected(op_colls)
    if risk == TransitionRiskLevel.HIGH and not coll_snaps and len(flat) >= 2:
        # reintento con más nodos en superficies de alto riesgo
        op_colls = detect_operational_collections(uia_hierarchy=flat, capture_bundle=capture_bundle)
        coll_snaps, coll_entities = _collection_snapshots_from_detected(op_colls)

    candidates = _fuse_entity_candidates(
        uia_targets=uia_targets,
        ocr_targets=ocr_targets,
        dom_targets=dom_targets,
        click_xy=click_xy,
        collection_entities=coll_entities,
    )
    best_ent = candidates[0] if candidates else None
    best_coll = coll_snaps[0] if coll_snaps else None
    if best_ent and coll_snaps:
        for cs in coll_snaps:
            if best_ent.display_name in cs.entities:
                best_coll = cs
                break

    nearby = _nearby_labels_from_targets(
        list(uia_targets) + list(ocr_targets) + list(dom_targets),
        click_xy=click_xy,
    )
    chain = [str(x) for x in (hierarchy_chain or (uia_d.get("parent_chain") or [])) if x][:16]
    affordances = [str(a) for a in (active_affordances or []) if a][:12]

    reasons: List[str] = [f"trigger:{trigger}", f"depth:{depth}"]
    if risk == TransitionRiskLevel.HIGH:
        reasons.append("high_transition_risk_deep_capture")
    if dynamic:
        reasons.append("dynamic_surface_escalation")
    if best_ent:
        reasons.append("entity_candidate_fused")
    if best_coll:
        reasons.append("collection_snapshot_captured")
    if pre_buffer_path:
        reasons.append("pre_buffer_frame_linked")

    freeze_conf = 0.32
    if best_ent:
        freeze_conf = max(freeze_conf, best_ent.confidence)
    if best_coll and best_coll.entity_count >= 2:
        freeze_conf = max(freeze_conf, best_coll.confidence, 0.55)
    if len(candidates) == 1:
        freeze_conf = min(1.0, freeze_conf + 0.1)
    multi_signal = len({
        s for c in candidates[:3] for s in (c.evidence_sources or [])
    })
    freeze_conf = min(1.0, freeze_conf + 0.04 * multi_signal)

    pre_confirmed = bool(
        best_ent
        and best_ent.confidence >= FREEZE_CONFIDENCE_MIN
        and not is_generic_entity_label(best_ent.display_name)
        and (best_ent.evidence_sources and "coords" not in best_ent.evidence_sources[0])
    )

    quality = depth
    if freeze_conf >= 0.85 and pre_confirmed:
        quality = "excellent"
    elif freeze_conf >= FREEZE_CONFIDENCE_MIN:
        quality = "good"

    viewport_sig = _layout_signature(flat, click_xy)

    return OperationalFreezeSnapshot(
        cursor_xy=click_xy,
        window_context=wc,
        viewport_signature=viewport_sig,
        uia_targets=uia_targets[:24],
        ocr_targets=ocr_targets[:16],
        dom_targets=dom_targets[:20],
        operational_collections=coll_snaps,
        collection_entities=coll_entities[:48],
        best_entity_candidate=best_ent,
        best_collection_candidate=best_coll,
        nearby_labels=nearby,
        visual_group_signature=_visual_group_signature(uia_targets),
        layout_signature=viewport_sig,
        freeze_confidence=round(freeze_conf, 4),
        freeze_quality=quality,
        freeze_reasons=reasons,
        transition_risk=risk,
        dynamic_surface_detected=dynamic,
        pre_transition_confirmed=pre_confirmed,
        trigger=trigger,
        freeze_depth=depth,
        hierarchy_chain=chain,
        active_affordances=affordances,
    )


@dataclass
class _PCOFPending:
    """Estado entre mouse_down y mouse_up."""

    freeze_id: str
    cursor_xy: Tuple[int, int]
    started_ms: int
    window_context: Dict[str, Any] = field(default_factory=dict)
    hover_xy: Optional[Tuple[int, int]] = None
    hover_updated_ms: int = 0
    early_snapshot: Optional[Dict[str, Any]] = None


_pending: Optional[_PCOFPending] = None
_pending_lock = __import__("threading").Lock()
_last_finalize_ms: int = 0
_last_finalize_process: str = ""


def _eager_uia_at_point(x: int, y: int) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """UIA síncrona en mouse_down (antes de transición de ventana transitoria)."""
    uia_d: Dict[str, Any] = {}
    flat: List[Dict[str, Any]] = []
    try:
        import uiautomation as auto
        from app.services.missions.recorder_capture_contract import capture_uia_parent_chain
    except ImportError:
        return uia_d, flat
    try:
        with auto.UIAutomationInitializerInThread():
            elem = auto.ControlFromPoint(int(x), int(y))
            if elem is None:
                return uia_d, flat
            bbox_struct: Optional[Dict[str, int]] = None
            try:
                r = elem.BoundingRectangle
                left = int(getattr(r, "left", 0))
                top = int(getattr(r, "top", 0))
                right = int(getattr(r, "right", 0))
                bottom = int(getattr(r, "bottom", 0))
                w = max(0, right - left)
                h = max(0, bottom - top)
                if w > 0 and h > 0:
                    bbox_struct = {"left": left, "top": top, "width": w, "height": h}
            except Exception:
                pass
            uia_d = {
                "name": str(elem.Name or ""),
                "control_type": str(elem.ControlTypeName or ""),
                "automation_id": str(elem.AutomationId or ""),
                "class_name": str(elem.ClassName or ""),
            }
            if bbox_struct:
                uia_d["bbox"] = bbox_struct
            chain = capture_uia_parent_chain(
                elem, click_xy=(int(x), int(y)), max_siblings=32,
            )
            if chain:
                uia_d["parent_chain"] = chain
                ancestor_names = [
                    _txt(a.get("name"))
                    for a in (chain.get("ancestors") or [])
                    if _txt(a.get("name"))
                ]
                parent_chain_labels = ancestor_names + ["Window"]
                for sib in chain.get("siblings_near_click") or []:
                    nm = _txt(sib.get("name"))
                    if not nm:
                        continue
                    flat.append({
                        "name": nm,
                        "control_type": _txt(sib.get("control_type") or "listitemcontrol"),
                        "role": "listitem",
                        "parent_chain": list(parent_chain_labels),
                        "bbox": bbox_struct,
                        "children": [],
                    })
            if uia_d.get("name") or uia_d.get("automation_id"):
                hit = dict(uia_d)
                if "parent_chain" in hit:
                    del hit["parent_chain"]
                flat.insert(0, hit)
    except Exception as exc:
        log.debug("[PCOF] eager UIA: %s", exc)
    return uia_d, flat


def _capture_deep_freeze_at_point(
    x: int,
    y: int,
    *,
    window_context: Optional[Dict[str, Any]] = None,
    pre_snapshot_path: Optional[str] = None,
) -> OperationalFreezeSnapshot:
    """Freeze operacional profundo en mouse_down (fuente de verdad si la UI cierra)."""
    click_xy = (int(x), int(y))
    wc = dict(window_context or {})
    uia_d, flat = _eager_uia_at_point(x, y)
    ocr: Optional[Dict[str, Any]] = None
    if pre_snapshot_path:
        try:
            from app.services.missions.recorder_capture_contract import maybe_run_ocr

            ocr = maybe_run_ocr(
                pre_snapshot_full=pre_snapshot_path,
                click_xy=click_xy,
            )
        except Exception as exc:
            log.debug("[PCOF] mousedown OCR: %s", exc)
    risk = score_transition_risk(
        uia=uia_d,
        window_ctx=wc,
        flat_nodes=flat,
        collection_count=0,
    )
    if len(flat) >= 2:
        risk = TransitionRiskLevel.HIGH
    snap = capture_operational_freeze(
        cursor_xy=click_xy,
        window_context=wc,
        uia=uia_d,
        uia_flat=flat,
        ocr=ocr,
        trigger="mouse_down_deep",
        pre_buffer_path=pre_snapshot_path,
        hierarchy_chain=[
            _txt(a.get("name"))
            for a in ((uia_d.get("parent_chain") or {}).get("ancestors") or [])
            if _txt(a.get("name"))
        ],
    )
    snap.freeze_depth = FREEZE_QUALITY_DEEP
    if "mouse_down_deep" not in snap.freeze_reasons:
        snap.freeze_reasons.append("mouse_down_deep_capture")
    if risk == TransitionRiskLevel.HIGH:
        snap.transition_risk = risk
    return snap


def _freeze_strength(freeze: OperationalFreezeSnapshot) -> float:
    score = float(freeze.freeze_confidence or 0.0)
    if freeze.pre_transition_confirmed:
        score += 0.55
    best = freeze.best_entity_candidate
    if best and best.display_name:
        score += 0.35
    coll = freeze.best_collection_candidate
    if coll and int(coll.entity_count or 0) >= 2:
        score += 0.3
    if freeze.collection_entities:
        score += min(0.2, 0.04 * len(freeze.collection_entities))
    return score


def _window_transition_detected(
    *,
    pending: Optional[_PCOFPending],
    late_window_context: Dict[str, Any],
    metadata: Dict[str, Any],
) -> bool:
    iso = dict(metadata.get("target_identity_isolation") or {})
    state = dict(iso.get("state_change") or {})
    if state.get("hwnd_changed") or state.get("title_changed"):
        return True
    if not pending:
        return False
    pw = dict(pending.window_context or {})
    lw = dict(late_window_context or {})
    ph = pw.get("hwnd")
    lh = lw.get("hwnd")
    if ph is not None and lh is not None and ph != lh:
        return True
    pt = _txt(pw.get("title"))
    lt = _txt(lw.get("title"))
    if pt and lt and pt != lt:
        return True
    return False


def _merge_pcof_freezes(
    early: OperationalFreezeSnapshot,
    late: OperationalFreezeSnapshot,
    *,
    window_transition: bool,
    freeze_id: str,
) -> OperationalFreezeSnapshot:
    """Si la ventana transitoria desapareció, el freeze de mouse_down manda."""
    early_score = _freeze_strength(early)
    late_score = _freeze_strength(late)
    late_weak = not late.pre_transition_confirmed or not (
        late.best_entity_candidate and late.best_entity_candidate.display_name
    )
    early_strong = bool(
        early.pre_transition_confirmed
        or (early.best_entity_candidate and early.best_entity_candidate.display_name)
        or (early.best_collection_candidate and (early.best_collection_candidate.entity_count or 0) >= 2)
    )
    use_early = early_strong and (
        early_score > late_score + 0.04
        or (window_transition and late_weak)
        or (window_transition and not late.uia_targets and bool(early.uia_targets or early.collection_entities))
    )
    if use_early:
        merged = early.model_copy(deep=True)
        merged.freeze_id = freeze_id
        merged.freeze_reasons = list(early.freeze_reasons) + [
            "mouse_down_deep_authoritative",
            "late_finalize_superseded",
        ]
        if window_transition:
            merged.freeze_reasons.append("transient_window_mousedown_truth")
        merged.trigger = "mouse_down_deep"
        return merged
    if early_strong and late_weak:
        late.best_entity_candidate = early.best_entity_candidate
        late.best_collection_candidate = early.best_collection_candidate
        late.collection_entities = early.collection_entities or late.collection_entities
        late.operational_collections = early.operational_collections or late.operational_collections
        late.pre_transition_confirmed = early.pre_transition_confirmed
        late.freeze_confidence = max(
            float(late.freeze_confidence or 0),
            float(early.freeze_confidence or 0),
        )
        late.freeze_reasons = list(late.freeze_reasons) + [
            "early_entity_merged_into_late_finalize",
        ]
    late.freeze_id = freeze_id
    return late


def _audit_missed_click_suspected(
    metadata: Dict[str, Any],
    *,
    early: Optional[OperationalFreezeSnapshot],
    late: OperationalFreezeSnapshot,
    window_transition: bool,
    pending: Optional[_PCOFPending],
) -> None:
    """Marca huecos SearchUI→Chrome→contenido cuando el perfil no sobrevivió al finalize."""
    reasons: List[str] = []
    if early and early.pre_transition_confirmed and not late.pre_transition_confirmed:
        reasons.append("late_finalize_lost_mousedown_entity")
    if window_transition and not late.pre_transition_confirmed:
        reasons.append("hwnd_or_title_transition_before_trusted_freeze")
    proc = _lower((pending.window_context if pending else {}).get("process_name"))
    global _last_finalize_ms, _last_finalize_process
    now_ms = int(time.time() * 1000)
    gap_ms = now_ms - _last_finalize_ms if _last_finalize_ms else 0
    if (
        _last_finalize_process
        and proc
        and _last_finalize_process != proc
        and gap_ms > 400
        and gap_ms < 12000
        and not late.pre_transition_confirmed
    ):
        reasons.append("cross_surface_gap_without_pcof_entity")
    _last_finalize_ms = now_ms
    _last_finalize_process = proc or _last_finalize_process
    if reasons:
        metadata["missed_click_suspected"] = {
            "suspected": True,
            "reasons": reasons,
            "gap_ms_since_last_click": gap_ms if gap_ms else None,
            "had_mousedown_deep": bool(early),
            "mousedown_entity": _txt(
                (early.best_entity_candidate.display_name if early and early.best_entity_candidate else ""),
            ),
        }


def begin_pre_click_freeze(
    x: int,
    y: int,
    *,
    window_context: Optional[Dict[str, Any]] = None,
    pre_snapshot_path: Optional[str] = None,
) -> Optional[str]:
    """Inicia congelación en mouse_down (antes de mouse_up)."""
    global _pending
    if not pcof_enabled():
        return None
    now_ms = int(time.time() * 1000)
    fid: Optional[str] = None
    with _pending_lock:
        if _pending is not None and (now_ms - _pending.started_ms) < Mousedown_DEBOUNCE_MS:
            return _pending.freeze_id
        fid = str(uuid.uuid4())
        _pending = _PCOFPending(
            freeze_id=fid,
            cursor_xy=(int(x), int(y)),
            started_ms=now_ms,
            window_context=dict(window_context or {}),
        )
    try:
        early = _capture_deep_freeze_at_point(
            x, y,
            window_context=window_context,
            pre_snapshot_path=pre_snapshot_path,
        )
        early.freeze_id = fid or early.freeze_id
        with _pending_lock:
            if _pending is not None and _pending.freeze_id == fid:
                _pending.early_snapshot = early.model_dump(mode="json")
    except Exception as exc:
        log.debug("[PCOF] deep mousedown freeze: %s", exc)
    return fid


def update_pre_click_hover(x: int, y: int) -> None:
    """Actualiza hover pre-click con debounce."""
    global _pending
    if not pcof_enabled():
        return
    now_ms = int(time.time() * 1000)
    with _pending_lock:
        if _pending is None:
            return
        if now_ms - _pending.hover_updated_ms < HOVER_DEBOUNCE_MS:
            return
        _pending.hover_xy = (int(x), int(y))
        _pending.hover_updated_ms = now_ms


def finalize_pre_click_freeze(
    *,
    cursor_xy: Tuple[int, int],
    metadata: Dict[str, Any],
    window_context: Optional[Dict[str, Any]] = None,
    pre_snapshot_path: Optional[str] = None,
) -> OperationalFreezeSnapshot:
    """Finaliza freeze con evidencia completa PRE mouse_up processing."""
    global _pending
    pending: Optional[_PCOFPending] = None
    with _pending_lock:
        pending = _pending
        _pending = None

    late_wc = dict(window_context or {})
    mousedown_wc = dict(pending.window_context if pending else {})
    window_transition = _window_transition_detected(
        pending=pending,
        late_window_context=late_wc,
        metadata=metadata,
    )
    if window_transition and mousedown_wc:
        wc = dict(mousedown_wc)
    elif pending and mousedown_wc:
        wc = {**mousedown_wc, **late_wc}
    else:
        wc = late_wc
    use_xy = cursor_xy
    if pending:
        use_xy = pending.cursor_xy
        if pending.hover_xy is not None:
            use_xy = pending.hover_xy

    early_snap: Optional[OperationalFreezeSnapshot] = None
    if pending and pending.early_snapshot:
        try:
            early_snap = OperationalFreezeSnapshot.model_validate(pending.early_snapshot)
        except Exception:
            early_snap = None

    late_snap = capture_operational_freeze(
        cursor_xy=use_xy,
        window_context=wc,
        uia=metadata.get("uia"),
        dom=metadata.get("web"),
        ocr=metadata.get("ocr_fallback") or metadata.get("ocr"),
        uia_flat=metadata.get("uia_flat"),
        trigger="mouse_up_finalize",
        pre_buffer_path=pre_snapshot_path,
        hierarchy_chain=(metadata.get("uia") or {}).get("parent_chain"),
    )
    fid = pending.freeze_id if pending else late_snap.freeze_id
    if early_snap is not None:
        snap = _merge_pcof_freezes(
            early_snap,
            late_snap,
            window_transition=window_transition,
            freeze_id=fid,
        )
    else:
        snap = late_snap
        snap.freeze_id = fid
    if pending:
        snap.freeze_reasons.append("mousedown_anchor_preserved")
    _audit_missed_click_suspected(
        metadata,
        early=early_snap,
        late=late_snap,
        window_transition=window_transition,
        pending=pending,
    )
    return snap


def apply_freeze_to_metadata(
    metadata: Dict[str, Any],
    freeze: OperationalFreezeSnapshot,
) -> Dict[str, Any]:
    """Persiste snapshot y enriquece identidad PRE-click."""
    metadata["operational_freeze_snapshot"] = freeze.model_dump(mode="json")
    metadata["pre_action_identity"] = freeze_identity_dict(freeze)

    try:
        from app.services.runtime.universal_operational_element_identity import (
            attach_identity_to_metadata,
            identify_from_freeze,
        )

        element_identity = identify_from_freeze(freeze)
        attach_identity_to_metadata(metadata, element_identity)
    except Exception as exc:
        log.debug("[PCOF] UOEI attach: %s", exc)

    iso = dict(metadata.get("target_identity_isolation") or {})
    best = freeze.best_entity_candidate
    if best and freeze.pre_transition_confirmed:
        iso["target_label"] = best.display_name
        iso["expected_label_hint"] = best.display_name
        iso["trusted_pre_action_identity"] = True
        iso["trusted_pre_action_reasons"] = list(
            set(list(iso.get("trusted_pre_action_reasons") or []) + [
                "pcof_pre_transition_confirmed",
                *list(best.evidence_sources or [])[:4],
            ]),
        )
        iso["target_identity_confidence"] = max(
            float(iso.get("target_identity_confidence") or 0.0),
            float(freeze.freeze_confidence),
        )
        iso["successful_state_transition"] = True
        iso["identity_preserved_after_transition"] = True
        iso["degradation_reason"] = None
        metadata["target_identity_isolation"] = iso

    cc = dict(metadata.get("capture_contract") or {})
    if freeze.pre_transition_confirmed:
        cc["trusted_pre_action_identity"] = True
        cc["sufficient_for_execution"] = True
        cc.setdefault("pcof_confirmed", True)
    metadata["capture_contract"] = cc

    if freeze.best_collection_candidate:
        metadata["operational_collection_freeze"] = freeze.best_collection_candidate.model_dump(
            mode="json",
        )
    return metadata


def freeze_identity_dict(freeze: OperationalFreezeSnapshot) -> Dict[str, Any]:
    best = freeze.best_entity_candidate
    out: Dict[str, Any] = {
        "freeze_id": freeze.freeze_id,
        "name": best.display_name if best else "",
        "confidence": float(freeze.freeze_confidence),
        "pre_transition_confirmed": bool(freeze.pre_transition_confirmed),
        "transition_risk": getattr(
            freeze.transition_risk, "value", str(freeze.transition_risk),
        ),
        "evidence_sources": list(best.evidence_sources) if best else [],
    }
    try:
        from app.services.runtime.universal_operational_element_identity import (
            identify_from_freeze,
            identity_dict,
        )

        ident = identify_from_freeze(freeze)
        out["operational_element_type"] = ident.operational_element_type.value
        out["operational_element_identity"] = identity_dict(ident)
    except Exception:
        pass
    return out


_AFFORDANCE_LABEL_MARKERS: Tuple[str, ...] = (
    "más acciones", "more actions", "more options", "acciones para",
    "actions for", "overflow", "menu ", " additional ", " opciones ",
)


def is_pcof_affordance_label(name: str) -> bool:
    """Controles auxiliares (⋯, menú contextual) — no son entidades seleccionables."""
    low = _lower(name)
    if not low:
        return False
    return any(m in low for m in _AFFORDANCE_LABEL_MARKERS)


def actionable_pcof_entity_names(freeze: OperationalFreezeSnapshot) -> List[str]:
    """Entidades seleccionables deduplicadas (sin affordances ni duplicados UIA)."""
    names: List[str] = []
    seen: set = set()
    best = freeze.best_entity_candidate
    if best and best.display_name and not is_generic_entity_label(best.display_name):
        if not is_pcof_affordance_label(best.display_name):
            nk = normalize_entity_name(best.display_name)
            if nk and nk not in seen:
                seen.add(nk)
                names.append(best.display_name.strip())

    coll = freeze.best_collection_candidate
    if coll:
        for raw in coll.entities or []:
            nm = _txt(raw)
            if not nm or is_generic_entity_label(nm) or is_pcof_affordance_label(nm):
                continue
            nk = normalize_entity_name(nm)
            if nk in seen:
                continue
            seen.add(nk)
            names.append(nm)

    for ent in freeze.collection_entities or []:
        nm = _txt(getattr(ent, "display_name", "") or "")
        if not nm or is_generic_entity_label(nm) or is_pcof_affordance_label(nm):
            continue
        nk = normalize_entity_name(nm)
        if nk in seen:
            continue
        seen.add(nk)
        names.append(nm)

    return names


def pcof_best_dominates_actionables(
    freeze: OperationalFreezeSnapshot,
    actionable: Optional[Sequence[str]] = None,
) -> bool:
    """Click resolvió una entidad aunque la colección tenga ruido/duplicados."""
    names = list(actionable or actionable_pcof_entity_names(freeze))
    if len(names) <= 1:
        return True
    best = freeze.best_entity_candidate
    if not best or not best.display_name:
        return False
    if float(best.confidence or 0) >= 0.88 and float(freeze.freeze_confidence) >= FREEZE_CONFIDENCE_MIN:
        for tgt in freeze.uia_targets or []:
            nm = _txt(getattr(tgt, "name", None) or (tgt.get("name") if isinstance(tgt, dict) else ""))
            ov = float(
                getattr(tgt, "overlap_score", None)
                or (tgt.get("overlap_score") if isinstance(tgt, dict) else 0)
                or 0,
            )
            if nm == best.display_name and ov >= 0.85:
                return True
        if best.absorbed_ambiguity and float(best.confidence or 0) >= 0.95:
            return True
    return False


def pcof_candidate_count(freeze: OperationalFreezeSnapshot) -> int:
    """Candidatos operacionales seleccionables (no affordances ni duplicados)."""
    actionable = actionable_pcof_entity_names(freeze)
    if actionable:
        return len(actionable)
    best = freeze.best_entity_candidate
    if freeze.pre_transition_confirmed and best and best.display_name:
        return 1
    return 99


def _pcof_has_collection_context(freeze: OperationalFreezeSnapshot) -> bool:
    if freeze.best_collection_candidate:
        return True
    risk = getattr(freeze.transition_risk, "value", str(freeze.transition_risk))
    return risk in ("medium", "high") or bool(freeze.operational_collections)


def qualifies_pcof_for_sep_promotion(
    freeze: OperationalFreezeSnapshot,
    *,
    require_single_candidate: bool = True,
) -> Tuple[bool, List[str]]:
    """¿El freeze es fuente primaria válida para ``select_entity_from_collection``?"""
    reasons: List[str] = []
    if not freeze.pre_transition_confirmed:
        return False, ["pre_transition_confirmed_false"]
    if float(freeze.freeze_confidence) < FREEZE_CONFIDENCE_MIN:
        return False, ["freeze_confidence_below_threshold"]
    best = freeze.best_entity_candidate
    if not best or not best.display_name:
        return False, ["no_best_entity_candidate"]
    if is_generic_entity_label(best.display_name):
        return False, ["generic_entity_label"]
    if not _pcof_has_collection_context(freeze):
        return False, ["no_collection_or_risk_context"]
    actionable = actionable_pcof_entity_names(freeze)
    if not actionable:
        return False, ["no_actionable_entities"]
    cand = len(actionable)
    if require_single_candidate and cand > 1:
        if not pcof_best_dominates_actionables(freeze, actionable):
            return False, ["multiple_entity_candidates"]
    reasons.append("pcof_sep_qualified")
    return True, reasons


def freeze_to_capture_bundle(freeze: OperationalFreezeSnapshot) -> Dict[str, Any]:
    """Bundle para UCES / ERL."""
    best = freeze.best_entity_candidate
    bundle: Dict[str, Any] = {
        "operational_freeze_snapshot": freeze.model_dump(mode="json"),
        "trusted_pre_action_identity": bool(freeze.pre_transition_confirmed),
        "sufficient_for_execution": bool(freeze.pre_transition_confirmed),
        "target_identity_confidence": float(freeze.freeze_confidence),
        "uia_flat": [
            {
                "name": t.name,
                "role": t.role,
                "control_type": t.role,
                "parent_chain": t.parent_chain,
                "bbox": t.bbox,
            }
            for t in freeze.uia_targets
        ],
        "nearby_labels": list(freeze.nearby_labels),
        "window_title": str((freeze.window_context or {}).get("title", "")),
        "process_name": str((freeze.window_context or {}).get("process_name", "")),
    }
    if best:
        bundle["display_name"] = best.display_name
        bundle["selection_intent"] = f"select_entity:{best.normalized_name}"
    if freeze.best_collection_candidate:
        bundle["operational_collection"] = freeze.best_collection_candidate.model_dump(
            mode="json",
        )
    return bundle


def erl_entity_from_freeze(freeze: OperationalFreezeSnapshot) -> Optional[Dict[str, Any]]:
    """Prioridad absoluta ERL: candidato congelado pre-transición."""
    best = freeze.best_entity_candidate
    if not best or not freeze.pre_transition_confirmed:
        return None
    return {
        "display_name": best.display_name,
        "normalized_name": best.normalized_name,
        "confidence": max(float(best.confidence), float(freeze.freeze_confidence)),
        "evidence_sources": list(best.evidence_sources),
        "source": "pcof_best_entity_candidate",
    }


def lone_initial_ong_node_from_freeze(
    freeze: OperationalFreezeSnapshot,
) -> Optional[Dict[str, Any]]:
    """Nodo inicial ONG para LONE desde freeze."""
    if not freeze.pre_transition_confirmed:
        return None
    visible = []
    if freeze.best_collection_candidate:
        visible = list(freeze.best_collection_candidate.entities)
    elif freeze.best_entity_candidate:
        visible = [freeze.best_entity_candidate.display_name]
    return {
        "node_id": f"pcof_{freeze.freeze_id[:12]}",
        "collection_signature": freeze.layout_signature,
        "visible_entities": visible[:32],
        "viewport_signature": freeze.viewport_signature,
        "active_context": {
            "freeze_quality": freeze.freeze_quality,
            "transition_risk": getattr(
                freeze.transition_risk, "value", str(freeze.transition_risk),
            ),
        },
        "continuity_score": float(freeze.freeze_confidence),
        "structural_hash": freeze.visual_group_signature,
    }


def tel_truth_enrichment_from_freeze(
    freeze: OperationalFreezeSnapshot,
) -> Dict[str, Any]:
    """Bloques de verdad más precisos para TEL."""
    out: Dict[str, Any] = {
        "pcof_frozen_before_transition": bool(freeze.pre_transition_confirmed),
        "freeze_confidence": float(freeze.freeze_confidence),
        "transition_risk": getattr(
            freeze.transition_risk, "value", str(freeze.transition_risk),
        ),
    }
    if freeze.best_entity_candidate:
        out["operational_entity_label"] = freeze.best_entity_candidate.display_name
    if freeze.best_collection_candidate:
        out["operational_collection_type"] = freeze.best_collection_candidate.collection_type
        out["collection_entity_count"] = freeze.best_collection_candidate.entity_count
    return out


def pcof_status_label_for_mission(mission: Any) -> Optional[str]:
    """Mensajes vista normal Mission Review."""
    trace = getattr(mission, "raw_trace", None) or []
    for ev in trace:
        meta = dict(getattr(ev, "metadata", None) or {})
        fr = meta.get("operational_freeze_snapshot")
        if not isinstance(fr, dict):
            continue
        if not fr.get("pre_transition_confirmed"):
            continue
        best = fr.get("best_entity_candidate") or {}
        coll = fr.get("best_collection_candidate") or {}
        name = _txt(best.get("display_name"))
        if coll.get("entities") and name:
            return "Entidad operacional congelada antes de transición."
        if name:
            if coll.get("collection_type") in ("list", "menu", "search_results", "cards"):
                return "Colección operacional reconocida"
            return "Elemento operacional identificado"
    return None


def pcof_expert_panel_lines(mission: Any) -> List[str]:
    """Panel experto «Pre-Click Operational Freeze»."""
    lines: List[str] = []
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    for raw in (sep.get("steps") or []):
        if not isinstance(raw, dict):
            continue
        pars = raw.get("params") or {}
        if (
            raw.get("type") == "select_entity_from_collection"
            and pars.get("selection_strategy") == "pre_click_operational_freeze"
        ):
            ent = (pars.get("collection_entity") or pars.get("entity") or {})
            nm = _txt(ent.get("display_name") if isinstance(ent, dict) else "")
            lines.append("PCOF → SEP promotion")
            lines.append(f"  promoted_step: {raw.get('id', '')}")
            if nm:
                lines.append(f"  entity: {nm}")
            lines.append(f"  freeze_confidence: {pars.get('freeze_confidence')}")
            lines.append(f"  fallback_legacy_type: {pars.get('fallback_legacy_type', '')}")
            return lines

    trace = getattr(mission, "raw_trace", None) or []
    for ev in trace[-8:]:
        meta = dict(getattr(ev, "metadata", None) or {})
        fr = meta.get("operational_freeze_snapshot")
        if not isinstance(fr, dict):
            continue
        lines.append("Pre-Click Operational Freeze")
        lines.append(f"  freeze_quality: {fr.get('freeze_quality')}")
        lines.append(f"  freeze_confidence: {fr.get('freeze_confidence')}")
        lines.append(
            f"  frozen_before_transition: {fr.get('pre_transition_confirmed')}",
        )
        lines.append(
            f"  transition_risk: {fr.get('transition_risk')}",
        )
        best = fr.get("best_entity_candidate") or {}
        if best.get("display_name"):
            lines.append(f"  entity: {best.get('display_name')}")
            lines.append(f"  entity_sources: {best.get('evidence_sources')}")
        coll = fr.get("best_collection_candidate") or {}
        if coll.get("entities"):
            preview = ", ".join(str(x) for x in (coll.get("entities") or [])[:5])
            lines.append(f"  collection ({coll.get('collection_type')}): {preview}")
        if best.get("absorbed_ambiguity"):
            lines.append("  absorbed_ambiguity: true")
        lines.append("  operational_continuity: pre-click freeze active")
        break
    return lines


def notify_pcof_mouse_down(
    x: int,
    y: int,
    *,
    window_context: Optional[Dict[str, Any]] = None,
    pre_snapshot_path: Optional[str] = None,
) -> None:
    """Hook público para recorder / intent layer."""
    try:
        begin_pre_click_freeze(
            x, y,
            window_context=window_context,
            pre_snapshot_path=pre_snapshot_path,
        )
    except Exception as exc:
        log.debug("[PCOF] mouse_down: %s", exc)


def notify_pcof_mouse_move(x: int, y: int) -> None:
    try:
        update_pre_click_hover(x, y)
    except Exception as exc:
        log.debug("[PCOF] mouse_move: %s", exc)


def process_click_with_pcof(
    metadata: Dict[str, Any],
    *,
    x: int,
    y: int,
    window_context: Optional[Dict[str, Any]] = None,
    pre_snapshot_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Pipeline recorder: finalize + apply si flag activo."""
    if not pcof_enabled():
        return metadata
    try:
        freeze = finalize_pre_click_freeze(
            cursor_xy=(int(x), int(y)),
            metadata=metadata,
            window_context=window_context,
            pre_snapshot_path=pre_snapshot_path,
        )
        return apply_freeze_to_metadata(metadata, freeze)
    except Exception as exc:
        log.debug("[PCOF] process_click: %s", exc)
        return metadata


__all__ = [
    "FREEZE_CONFIDENCE_MIN",
    "OperationalFreezeSnapshot",
    "apply_freeze_to_metadata",
    "begin_pre_click_freeze",
    "capture_operational_freeze",
    "pcof_candidate_count",
    "qualifies_pcof_for_sep_promotion",
    "detect_dynamic_surface",
    "erl_entity_from_freeze",
    "finalize_pre_click_freeze",
    "freeze_to_capture_bundle",
    "lone_initial_ong_node_from_freeze",
    "notify_pcof_mouse_down",
    "notify_pcof_mouse_move",
    "pcof_enabled",
    "pcof_expert_panel_lines",
    "pcof_status_label_for_mission",
    "process_click_with_pcof",
    "score_transition_risk",
    "tel_truth_enrichment_from_freeze",
    "update_pre_click_hover",
]
