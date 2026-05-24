"""Live Operational Perception (LOP) — Fase 1 sombra.

Fusiona UIA, DOM, OCR ligero bajo demanda, anclas visuales y contexto operacional
(ORG, GOOR, OCR, TEL, validators) para inferir *estado operacional vivo*.

No ejecuta input ni muta SEP / SmartExecutor / COB. Sólo observa y recomienda.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from app.contracts.mission import (
    LiveOperationalBlocker,
    LiveOperationalEntity,
    LiveOperationalEntityKind,
    LiveOperationalPerceptionSnapshot,
    LiveOperationalRuntimeAction,
    LiveOperationalSignal,
    LiveOperationalSurface,
    LiveOperationalSurfaceType,
    LiveOperationalTopology,
    LiveOperationalValidatorObservation,
    Mission,
    OperationalTruthBlock,
)
from app.contracts.operational_runtime_graph import OperationalRuntimeGraph
from app.core.logger import log


# --- Settings -----------------------------------------------------------------

def _settings_lop_enabled(settings_obj: Optional[Any]) -> bool:
    if settings_obj is None:
        try:
            from app.core.config import get_settings

            settings_obj = get_settings()
        except Exception:
            return False
    return bool(getattr(settings_obj, "LOP_SHADOW_ENABLED", True))


# --- Input bundle (inyectable / testeable) -----------------------------------

@dataclass
class LivePerceptionInput:
    """Cabina de sensores liviana. Todo opcional salvo lo que el caller provea."""

    uia_nodes: List[Dict[str, Any]] = field(default_factory=list)
    dom: Dict[str, Any] = field(default_factory=dict)
    runtime_validators: Dict[str, Any] = field(default_factory=dict)
    visual_anchors: List[Dict[str, Any]] = field(default_factory=list)
    context_hints: Dict[str, Any] = field(default_factory=dict)
    active_app: str = ""
    active_window: str = ""

OcrLightFn = Optional[Callable[[], List[str]]]


def _txt(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def _lower(x: Any) -> str:
    return _txt(x).lower()


def _flatten_uia(nodes: Sequence[Dict[str, Any]], *, max_nodes: int = 240) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(n: Dict[str, Any]) -> None:
        if len(out) >= max_nodes:
            return
        out.append(n)
        for ch in n.get("children") or []:
            if isinstance(ch, dict):
                walk(ch)

    for root in nodes:
        if isinstance(root, dict):
            walk(root)
    return out


def _dom_nodes(dom: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = dom.get("nodes")
    if not isinstance(raw, list):
        return []
    return [n for n in raw if isinstance(n, dict)][:400]


def _collect_plain_text_budget(uia_flat: Sequence[Dict[str, Any]], dom: Dict[str, Any]) -> Tuple[int, List[str]]:
    snippets: List[str] = []
    for n in uia_flat:
        for key in ("name", "help_text", "value", "access_key"):
            t = _txt(n.get(key))
            if t and len(t) >= 2:
                snippets.append(t[:280])
        ct = _lower(n.get("control_type"))
        if ct and ct not in snippets:
            snippets.append(ct[:120])
    for dn in _dom_nodes(dom):
        for key in ("visible_text", "aria_label", "placeholder", "title"):
            t = _txt(dn.get(key))
            if t:
                snippets.append(t[:280])
    joined = " ".join(snippets)
    return len(joined), snippets


def classify_live_surface(
    *,
    uia_flat: Sequence[Dict[str, Any]],
    dom: Dict[str, Any],
    validators: Dict[str, Any],
    context_hints: Dict[str, Any],
) -> Tuple[LiveOperationalSurfaceType, List[LiveOperationalSurface]]:
    """Clasificación heurística por roles/validators — sin producto fijo."""

    scores: Dict[LiveOperationalSurfaceType, float] = {t: 0.0 for t in LiveOperationalSurfaceType}
    evidence: Dict[LiveOperationalSurfaceType, List[str]] = {t: [] for t in LiveOperationalSurfaceType}

    url = _txt(dom.get("url"))
    parsed = urlparse(url) if url else None
    domain = (parsed.hostname or "").lower() if parsed else ""

    if url:
        scores[LiveOperationalSurfaceType.BROWSER_SURFACE] += 0.55
        evidence[LiveOperationalSurfaceType.BROWSER_SURFACE].append("dom:url_present")
    if validators.get("address_bar_visible") or validators.get("has_navigation_bar"):
        scores[LiveOperationalSurfaceType.BROWSER_SURFACE] += 0.25
        evidence[LiveOperationalSurfaceType.BROWSER_SURFACE].append("validator:navigation_chrome")

    if context_hints.get("launcher_like") and not url:
        scores[LiveOperationalSurfaceType.LAUNCHER_SURFACE] += 0.6
        evidence[LiveOperationalSurfaceType.LAUNCHER_SURFACE].append("context:launcher_like")

    search_hits = 0
    text_inputs = 0
    push_like = 0
    list_items = 0
    dialogs = 0
    tables = 0
    scroll_hints = 0

    for n in uia_flat:
        role = _lower(n.get("role") or n.get("control_type"))
        name = _lower(n.get("name"))
        if "search" in role or "searchbox" in role or "search" in name:
            search_hits += 1
        if "edit" in role or "text" in role:
            text_inputs += 1
        if "button" in role or "splitbutton" in role or "hyperlink" in role:
            push_like += 1
        if role and ("listitem" in role or role.endswith("item")):
            list_items += 1
        if "window" in role and n.get("modal"):
            dialogs += 1
        if "table" in role or "datagrid" in role or "list" in role:
            tables += 1
        if n.get("scroll_pattern") or "scroll" in role:
            scroll_hints += 1

    for dn in _dom_nodes(dom):
        role = _lower(dn.get("role") or dn.get("tag"))
        itype = _lower(dn.get("input_type"))
        al = _lower(dn.get("aria_label"))
        ph = _lower(dn.get("placeholder"))
        if itype == "search" or "searchbox" in role or "search" in al or "search" in ph:
            search_hits += 1
        if itype in {"text", "email", "password", "search", "tel", "url"} or role in {"textbox", "input"}:
            text_inputs += 1
        if role in {"button", "link", "menuitem", "tab"}:
            push_like += 1
        if role in {"listitem", "article", "row", "gridcell"}:
            list_items += 1
        if role in {"dialog", "alert"}:
            dialogs += 1
        if role in {"table", "grid", "treegrid"}:
            tables += 1

    if validators.get("field_focused") or validators.get("search_field_focused"):
        scores[LiveOperationalSurfaceType.SEARCH_SURFACE] += 0.15
        evidence[LiveOperationalSurfaceType.SEARCH_SURFACE].append("validator:focused_field")

    if search_hits:
        s = min(0.75, 0.28 + 0.09 * search_hits)
        scores[LiveOperationalSurfaceType.SEARCH_SURFACE] += s
        evidence[LiveOperationalSurfaceType.SEARCH_SURFACE].append(f"signal:search_controls={search_hits}")

    if validators.get("results_visible") or validators.get("feed_visible"):
        scores[LiveOperationalSurfaceType.RESULTS_SURFACE] += 0.65
        evidence[LiveOperationalSurfaceType.RESULTS_SURFACE].append("validator:results_visible")

    if list_items >= 4 and scroll_hints >= 1 and search_hits >= 1:
        scores[LiveOperationalSurfaceType.RESULTS_SURFACE] += 0.35
        evidence[LiveOperationalSurfaceType.RESULTS_SURFACE].append("pattern:listitems_scroll")

    if text_inputs >= 2 and push_like >= 1:
        scores[LiveOperationalSurfaceType.FORM_SURFACE] += 0.45
        evidence[LiveOperationalSurfaceType.FORM_SURFACE].append("pattern:multi_input")

    if validators.get("form_submitted"):
        scores[LiveOperationalSurfaceType.FORM_SURFACE] += 0.2
        evidence[LiveOperationalSurfaceType.FORM_SURFACE].append("validator:form_submitted")

    if dialogs or validators.get("modal_open") or validators.get("permission_dialog"):
        scores[LiveOperationalSurfaceType.DIALOG_SURFACE] += 0.7
        evidence[LiveOperationalSurfaceType.DIALOG_SURFACE].append("pattern:dialog_or_modal_validator")

    if tables >= 1 and list_items >= 3:
        scores[LiveOperationalSurfaceType.TABLE_SURFACE] += 0.4
        evidence[LiveOperationalSurfaceType.TABLE_SURFACE].append("pattern:table_region")

    if validators.get("file_picker_open") or context_hints.get("file_dialog"):
        scores[LiveOperationalSurfaceType.FILE_SURFACE] += 0.65
        evidence[LiveOperationalSurfaceType.FILE_SURFACE].append("validator:file_picker")

    if push_like >= 8 and not url and not context_hints.get("launcher_like"):
        scores[LiveOperationalSurfaceType.DASHBOARD_SURFACE] += 0.35
        evidence[LiveOperationalSurfaceType.DASHBOARD_SURFACE].append("pattern:many_actions")

    if text_inputs >= 1 and (validators.get("rich_editor") or context_hints.get("editor_like")):
        scores[LiveOperationalSurfaceType.EDITOR_SURFACE] += 0.4
        evidence[LiveOperationalSurfaceType.EDITOR_SURFACE].append("context:editor_like")

    if not url and push_like >= 6 and not context_hints.get("launcher_like") and not dialogs:
        scores[LiveOperationalSurfaceType.LAUNCHER_SURFACE] += 0.25
        evidence[LiveOperationalSurfaceType.LAUNCHER_SURFACE].append("pattern:desktop_tile_like")

    # Pick best
    best_type = max(scores, key=lambda k: scores[k])
    best_score = scores[best_type]
    if best_score < 0.18:
        best_type = LiveOperationalSurfaceType.UNKNOWN_SURFACE
        best_score = 0.1

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:5]
    surf_objs: List[LiveOperationalSurface] = []
    for t, sc in ranked:
        if sc <= 0.01:
            continue
        surf_objs.append(
            LiveOperationalSurface(surface_type=t, score=round(sc, 4), evidence=list(evidence.get(t, []))[:6]),
        )

    return best_type, surf_objs


def extract_live_entities(
    *,
    uia_flat: Sequence[Dict[str, Any]],
    dom: Dict[str, Any],
) -> Tuple[List[LiveOperationalEntity], List[LiveOperationalEntity]]:
    visible: List[LiveOperationalEntity] = []
    actionable: List[LiveOperationalEntity] = []

    def kind_from_uia(n: Dict[str, Any]) -> LiveOperationalEntityKind:
        role = _lower(n.get("role") or n.get("control_type"))
        name = _lower(n.get("name"))
        if "search" in role or "searchbox" in role:
            return LiveOperationalEntityKind.SEARCHBOX
        if "button" in role or "splitbutton" in role:
            return LiveOperationalEntityKind.BUTTON
        if "hyperlink" in role or "link" in role:
            return LiveOperationalEntityKind.LINK
        if "edit" in role or "text" in role:
            return LiveOperationalEntityKind.INPUT
        if "tabitem" in role or role.endswith("tab"):
            return LiveOperationalEntityKind.TAB
        if "menu" in role:
            return LiveOperationalEntityKind.MENU_ITEM
        if "listitem" in role:
            return LiveOperationalEntityKind.RESULT_ITEM
        if "table" in role or "datagrid" in role:
            return LiveOperationalEntityKind.TABLE
        if "dialog" in role or "window" in role:
            return LiveOperationalEntityKind.DIALOG
        if "password" in name:
            return LiveOperationalEntityKind.INPUT
        return LiveOperationalEntityKind.INPUT

    for n in uia_flat[:180]:
        k = kind_from_uia(n)
        entity = LiveOperationalEntity(
            kind=k,
            label_guess=_txt(n.get("name"))[:200],
            roles=[_txt(n.get("role") or n.get("control_type"))],
            automation_ids=[_txt(n.get("automation_id"))] if n.get("automation_id") else [],
            text_snippets=[_txt(n.get("name"))][:1],
            uia_hints={
                "control_type": n.get("control_type"),
                "modal": n.get("modal"),
            },
            actionable=not bool(n.get("offscreen")),
            confidence=0.55,
        )
        visible.append(entity)
        if entity.actionable and k not in {LiveOperationalEntityKind.TABLE, LiveOperationalEntityKind.DIALOG}:
            actionable.append(entity)

    for dn in _dom_nodes(dom)[:220]:
        role = _lower(dn.get("role") or dn.get("tag"))
        itype = _lower(dn.get("input_type"))
        if role == "a" or role == "link":
            k = LiveOperationalEntityKind.LINK
        elif role == "button":
            k = LiveOperationalEntityKind.BUTTON
        elif itype == "search" or role == "searchbox":
            k = LiveOperationalEntityKind.SEARCHBOX
        elif itype in {"text", "password", "email"} or role in {"textbox", "input"}:
            k = LiveOperationalEntityKind.INPUT
        elif role in {"listitem", "article"}:
            k = LiveOperationalEntityKind.RESULT_ITEM
        elif role in {"dialog", "alert"}:
            k = LiveOperationalEntityKind.ALERT if role == "alert" else LiveOperationalEntityKind.DIALOG
        elif role == "tab":
            k = LiveOperationalEntityKind.TAB
        else:
            continue
        label = _txt(dn.get("aria_label") or dn.get("visible_text"))[:200]
        entity = LiveOperationalEntity(
            kind=k,
            label_guess=label,
            roles=[role],
            text_snippets=[label] if label else [],
            dom_hints={
                "input_type": dn.get("input_type"),
                "tag": dn.get("tag"),
            },
            actionable=True,
            confidence=0.52,
        )
        visible.append(entity)
        actionable.append(entity)

    return visible, actionable


def observe_live_validators(validators: Dict[str, Any]) -> List[LiveOperationalValidatorObservation]:
    obs: List[LiveOperationalValidatorObservation] = []
    if not isinstance(validators, dict):
        return obs
    mapping = [
        ("url_matches", "url_matches"),
        ("field_focused", "field_focused"),
        ("search_field_focused", "search_field_focused"),
        ("results_visible", "results_visible"),
        ("form_submitted", "form_submitted"),
        ("download_complete", "download_complete"),
        ("text_present", "text_present"),
        ("app_active", "app_active"),
        ("window_active", "window_active"),
        ("file_exists", "file_exists"),
        ("modal_open", "modal_open"),
        ("modal_closed", "modal_closed"),
        ("loading", "loading"),
        ("loading_state", "loading_state"),
        ("permission_prompt", "permission_prompt"),
        ("permission_dialog", "permission_dialog"),
        ("network_stalled", "network_stalled"),
        ("destructive_confirm_unknown", "destructive_confirm_unknown"),
    ]
    for key, vk in mapping:
        if vk not in validators:
            continue
        val = validators.get(vk)
        if isinstance(val, bool):
            satisfied = val
            conf = 0.85 if val else 0.55
        else:
            satisfied = bool(val)
            conf = 0.6
        obs.append(
            LiveOperationalValidatorObservation(
                validator_key=key,
                satisfied=satisfied,
                confidence=conf,
                detail=str(type(val).__name__),
            ),
        )
    return obs


def match_live_state_to_org(
    surface: LiveOperationalSurfaceType,
    mission: Mission,
    graph: Optional[OperationalRuntimeGraph],
) -> List[str]:
    if not graph:
        blob = getattr(mission, "operational_runtime_graph", None)
        if isinstance(blob, dict) and blob.get("nodes"):
            try:
                graph = OperationalRuntimeGraph.model_validate(blob)
            except Exception:
                graph = None
    if not graph:
        return []

    want = _surface_to_states(surface)
    matched: List[str] = []
    for n in graph.nodes:
        ok = False
        for v in n.expected_validators or []:
            if any(s in _lower(v) for s in want):
                ok = True
                break
        ctx = n.context_requirements or {}
        if any(s in _lower(ctx.get("surface_hint") or "") for s in want):
            ok = True
        ss = [str(x) for x in (n.success_signals or [])]
        if any(any(w in _lower(x) for w in want) for x in ss):
            ok = True
        if ok:
            matched.append(str(n.node_id))
    return matched[:24]


def _surface_to_states(surface: LiveOperationalSurfaceType) -> Set[str]:
    s = surface.value
    return {
        s,
        s.replace("_surface", ""),
        "browser" if surface == LiveOperationalSurfaceType.BROWSER_SURFACE else "",
        "search" if surface == LiveOperationalSurfaceType.SEARCH_SURFACE else "",
        "results" if surface == LiveOperationalSurfaceType.RESULTS_SURFACE else "",
        "form" if surface == LiveOperationalSurfaceType.FORM_SURFACE else "",
    } - {""}


def match_live_state_to_goals(
    *,
    snapshot: LiveOperationalPerceptionSnapshot,
    mission: Mission,
    graph: Optional[OperationalRuntimeGraph],
) -> List[str]:
    if not graph:
        blob = getattr(mission, "operational_runtime_graph", None)
        if isinstance(blob, dict) and blob.get("goals"):
            try:
                graph = OperationalRuntimeGraph.model_validate(blob)
            except Exception:
                graph = None
    if not graph:
        out: List[str] = []
        for vo in snapshot.validators_observed:
            if vo.validator_key == "results_visible" and vo.satisfied:
                out.append("results_visible")
            if vo.validator_key == "form_submitted" and vo.satisfied:
                out.append("form_submitted")
        return list(dict.fromkeys(out))

    matched: List[str] = []
    val_keys = {v.validator_key for v in snapshot.validators_observed if v.satisfied}

    for g in graph.goals:
        hid = _lower(g.headline)
        amb = " ".join(_lower(x) for x in (g.ambiguity_notes or []))
        blob = hid + " " + amb
        if "results" in blob and snapshot.surface_type == LiveOperationalSurfaceType.RESULTS_SURFACE:
            matched.append(str(g.goal_id))
        if "submit" in blob and "form_submitted" in val_keys:
            matched.append(str(g.goal_id))
        if "results_visible" in val_keys and ("result" in blob or not blob.strip()):
            matched.append(str(g.goal_id))
    # Validator-first labels
    if "results_visible" in val_keys:
        matched.append("results_visible")
    if "form_submitted" in val_keys:
        matched.append("form_submitted")

    return list(dict.fromkeys(matched))[:24]


def detect_live_blockers(
    *,
    snapshot: LiveOperationalPerceptionSnapshot,
    actionable: Sequence[LiveOperationalEntity],
    validators: Dict[str, Any],
    context_hints: Dict[str, Any],
) -> List[LiveOperationalBlocker]:
    blockers: List[LiveOperationalBlocker] = []
    if snapshot.modal_detected and validators.get("modal_open"):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="modal_blocking",
                severity="high",
                description="Modal reported open —foreground interaction required.",
                evidence_signals=["validator:modal_open"],
            ),
        )
    if validators.get("permission_dialog"):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="permission_dialog",
                severity="high",
                description="OS or browser permission gate active.",
                evidence_signals=["validator:permission_dialog"],
            ),
        )
    if context_hints.get("auth_screen") or context_hints.get("password_screen"):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="auth_required",
                severity="critical",
                description="Credential surface — STOP for automation safety.",
                evidence_signals=["context:auth_screen"],
            ),
        )
    if context_hints.get("payment_context") or context_hints.get("transfer_context"):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="payment_context",
                severity="critical",
                description="Payment / transfer context — human gate required.",
                evidence_signals=["context:payment"],
            ),
        )

    # Multiple equivalent actionable buttons (same kind+empty label)
    buckets: Dict[Tuple[str, str], int] = {}
    for e in actionable:
        lab = _lower(e.label_guess) or "*empty*"
        buckets[(e.kind.value, lab)] = buckets.get((e.kind.value, lab), 0) + 1
    for (kind, lab), n in buckets.items():
        if kind == LiveOperationalEntityKind.BUTTON.value and lab == "*empty*" and n >= 3:
            blockers.append(
                LiveOperationalBlocker(
                    blocker_type="multiple_equivalent_targets",
                    severity="high",
                    description="Several indistinguishable primary actions.",
                    evidence_signals=[f"ambiguous_buttons={n}"],
                ),
            )

    if validators.get("destructive_confirm_unknown"):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="unknown_destructive_confirmation",
                severity="critical",
                description="Unverified destructive confirmation dialog.",
                evidence_signals=["validator:destructive_confirm_unknown"],
            ),
        )

    if validators.get("modal_unknown"):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="unknown_modal",
                severity="high",
                description="Modal or system prompt without classified intent — pause automation.",
                evidence_signals=["validator:modal_unknown"],
            ),
        )

    if validators.get("network_stalled") or (validators.get("loading") and validators.get("stall_elapsed_s", 0) > 8):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="network_loading_stall",
                severity="medium",
                description="Loading / network stall — wait before continuing.",
                evidence_signals=["validator:loading_stall"],
            ),
        )

    if context_hints.get("unexpected_app_switch"):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="unexpected_app_switch",
                severity="high",
                description="Foreground application diverges from mission corridor.",
                evidence_signals=["context:unexpected_app_switch"],
            ),
        )

    exp = context_hints.get("expected_surface_types") or []
    if isinstance(exp, list) and exp:
        if snapshot.surface_type.value not in {str(x) for x in exp}:
            blockers.append(
                LiveOperationalBlocker(
                    blocker_type="missing_required_surface",
                    severity="medium",
                    description="Current surface not in expected operational corridor.",
                    evidence_signals=["context:expected_surface_mismatch"],
                ),
            )

    # validator explicit mismatch flag from sensors
    if validators.get("validator_mismatch"):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="validator_mismatch",
                severity="high",
                description="Live validators disagree with execution truth expectations.",
                evidence_signals=["validator:mismatch"],
            ),
        )

    if snapshot.confidence < 0.28 and not context_hints.get("permit_low_confidence"):
        blockers.append(
            LiveOperationalBlocker(
                blocker_type="low_confidence",
                severity="medium",
                description="Aggregate perception confidence very low.",
                evidence_signals=[f"confidence={snapshot.confidence:.2f}"],
            ),
        )

    return blockers


def decide_live_runtime_action(
    *,
    snapshot: LiveOperationalPerceptionSnapshot,
    blockers: Sequence[LiveOperationalBlocker],
) -> Tuple[LiveOperationalRuntimeAction, bool]:
    """Devuelve (acción, safe_to_continue)."""

    crit = [b for b in blockers if b.severity == "critical"]
    high = [b for b in blockers if b.severity == "high"]

    if any(b.blocker_type in {"unknown_destructive_confirmation", "payment_context"} for b in crit):
        return LiveOperationalRuntimeAction.STOP_UNSAFE, False
    if any(b.blocker_type == "auth_required" for b in crit):
        return LiveOperationalRuntimeAction.REQUEST_HUMAN, False
    if any(b.blocker_type == "unknown_destructive_confirmation" for b in blockers):
        return LiveOperationalRuntimeAction.STOP_UNSAFE, False

    if any(b.blocker_type == "multiple_equivalent_targets" for b in blockers):
        return LiveOperationalRuntimeAction.REQUEST_HUMAN, False

    if any(b.blocker_type == "unknown_modal" for b in blockers):
        return LiveOperationalRuntimeAction.REQUEST_HUMAN, False

    if any(b.blocker_type == "modal_blocking" for b in blockers) and snapshot.modal_detected:
        # Unknown modal — bias human when not clearly safe
        if snapshot.confidence < 0.55:
            return LiveOperationalRuntimeAction.REQUEST_HUMAN, False
        return LiveOperationalRuntimeAction.REQUEST_HUMAN, False

    if any(b.blocker_type == "network_loading_stall" for b in blockers):
        return LiveOperationalRuntimeAction.WAIT, False

    if any(b.blocker_type == "missing_required_surface" for b in blockers) or any(
        b.blocker_type == "validator_mismatch" for b in blockers
    ):
        return LiveOperationalRuntimeAction.RECOVER, False

    if snapshot.surface_type == LiveOperationalSurfaceType.UNKNOWN_SURFACE and snapshot.confidence < 0.35:
        return LiveOperationalRuntimeAction.REVALIDATE, False

    if snapshot.confidence < 0.22:
        return LiveOperationalRuntimeAction.REQUEST_HUMAN, False

    if high and any(b.blocker_type == "permission_dialog" for b in high):
        return LiveOperationalRuntimeAction.REQUEST_HUMAN, False

    # CONTINUE only when moderately confident and no unresolved high blockers
    if high:
        return LiveOperationalRuntimeAction.WAIT, False

    if snapshot.confidence >= 0.45:
        return LiveOperationalRuntimeAction.CONTINUE, True

    return LiveOperationalRuntimeAction.WAIT, False


def _apply_budget_and_critical_gap_guards(snapshot: LiveOperationalPerceptionSnapshot) -> None:
    """Evita CONTINUE cuando hay presupuesto sobrepasado o pérdidas críticas de señal."""

    gaps = list(snapshot.perception_gaps or [])
    bad = False
    for g in gaps:
        if not isinstance(g, str):
            continue
        if (
            g.startswith("budget_exceeded")
            or g.startswith("lop_sensor_budget")
            or g.startswith("critical_signal_loss")
            or g == "lop_sensor_budget_partial"
        ):
            bad = True
            break

    if bad and snapshot.recommended_runtime_action == LiveOperationalRuntimeAction.CONTINUE:
        snapshot.recommended_runtime_action = LiveOperationalRuntimeAction.REVALIDATE
        snapshot.safe_to_continue = False


def _truth_execution_signals(mission: Mission) -> List[LiveOperationalSignal]:
    sigs: List[LiveOperationalSignal] = []
    for tb in mission.truth_blocks or []:
        if not isinstance(tb, OperationalTruthBlock):
            continue
        for v in tb.validator_expectations or []:
            sigs.append(
                LiveOperationalSignal(
                    kind="tel_expectation",
                    source="operational_context",
                    weight=0.45,
                    payload={"validator": str(v), "truth_id": str(tb.truth_id)},
                ),
            )
    return sigs[:48]


def _continuity_signals_from_mission(mission: Mission) -> List[LiveOperationalSignal]:
    out: List[LiveOperationalSignal] = []
    blob = getattr(mission, "continuity_runtime_shadow", None)
    if isinstance(blob, dict) and blob:
        out.append(
            LiveOperationalSignal(
                kind="ocr_continuity",
                source="operational_context",
                weight=0.4,
                payload={"keys": list(blob.keys())[:12]},
            ),
        )
    oh = getattr(mission, "operational_goal_history", None) or []
    if isinstance(oh, list) and oh:
        out.append(
            LiveOperationalSignal(
                kind="ocr_goal_history",
                source="operational_context",
                weight=0.35,
                payload={"tail": str(oh[-1])[:220]},
            ),
        )
    return out


def build_lop_audit(
    *,
    snapshot: LiveOperationalPerceptionSnapshot,
    elapsed_ms: float,
    ocr_invoked: bool,
    sensor_sources: Sequence[str],
) -> Dict[str, Any]:
    return {
        "ok": True,
        "elapsed_ms": round(elapsed_ms, 3),
        "ocr_light_invoked": ocr_invoked,
        "sensor_sources": list(sensor_sources),
        "surface_type": snapshot.surface_type.value,
        "confidence": snapshot.confidence,
        "safe_to_continue": snapshot.safe_to_continue,
        "recommended_runtime_action": snapshot.recommended_runtime_action.value,
        "perception_gap_count": len(snapshot.perception_gaps),
        "blocker_count": len(snapshot.live_blockers),
    }


def capture_live_operational_snapshot(
    mission: Mission,
    *,
    perception_input: Optional[LivePerceptionInput] = None,
    graph: Optional[OperationalRuntimeGraph] = None,
    ocr_light: OcrLightFn = None,
    text_sufficiency_threshold: int = 48,
    max_elapsed_ms: float = 120.0,
) -> LiveOperationalPerceptionSnapshot:
    """Construye un snapshot LOP completo (no persiste en Mission)."""

    t0 = time.perf_counter()
    inp = perception_input or LivePerceptionInput()
    uia_flat = _flatten_uia(inp.uia_nodes)
    dom = inp.dom if isinstance(inp.dom, dict) else {}
    validators = inp.runtime_validators if isinstance(inp.runtime_validators, dict) else {}
    hints = inp.context_hints if isinstance(inp.context_hints, dict) else {}

    budget, snippets = _collect_plain_text_budget(uia_flat, dom)
    ocr_invoked = False
    if budget < text_sufficiency_threshold and ocr_light is not None:
        try:
            extra = ocr_light() or []
        except Exception as exc:
            extra = []
            log.debug("[LOP] ocr_light failed: %s", exc)
        ocr_invoked = True
        for s in extra[:24]:
            if isinstance(s, str) and s.strip():
                snippets.append(s.strip()[:280])
        budget = len(" ".join(snippets))

    surface, ranked = classify_live_surface(
        uia_flat=uia_flat,
        dom=dom,
        validators=validators,
        context_hints=hints,
    )
    visible, actionable = extract_live_entities(uia_flat=uia_flat, dom=dom)
    val_obs = observe_live_validators(validators)

    url = _txt(dom.get("url"))
    parsed = urlparse(url) if url else None
    domain = (parsed.hostname or "").lower() if parsed else ""

    modal_detected = bool(validators.get("modal_open") or hints.get("modal_ui"))

    shot = LiveOperationalPerceptionSnapshot(
        active_app=inp.active_app,
        active_window=inp.active_window,
        active_url=url,
        active_domain=domain,
        surface_type=surface,
        operational_state_guess=surface.value,
        visible_entities=visible,
        actionable_entities=list(actionable)[:120],
        input_surfaces=[s for s in ranked if s.surface_type in {LiveOperationalSurfaceType.SEARCH_SURFACE, LiveOperationalSurfaceType.FORM_SURFACE, LiveOperationalSurfaceType.EDITOR_SURFACE}],
        navigation_surfaces=[s for s in ranked if s.surface_type == LiveOperationalSurfaceType.BROWSER_SURFACE],
        result_surfaces=[s for s in ranked if s.surface_type == LiveOperationalSurfaceType.RESULTS_SURFACE],
        blocking_surfaces=[s for s in ranked if s.surface_type == LiveOperationalSurfaceType.DIALOG_SURFACE],
        modal_detected=modal_detected,
        validators_observed=val_obs,
        execution_truth_signals=_truth_execution_signals(mission),
        continuity_signals=_continuity_signals_from_mission(mission),
        ambiguity_signals=[],
        topology=LiveOperationalTopology(
            uia_estimated_depth=min(32, len(uia_flat)),
            dom_estimated_depth=min(48, len(_dom_nodes(dom))),
            scroll_container_count=sum(1 for n in uia_flat if n.get("scroll_pattern")),
            fingerprint_refs=[_txt(a.get("id")) for a in inp.visual_anchors if isinstance(a, dict)][:12],
        ),
        raw_sensor_stats={
            "uia_nodes": len(uia_flat),
            "dom_nodes": len(_dom_nodes(dom)),
            "text_budget": budget,
            "ocr_invoked": ocr_invoked,
        },
    )

    shot.matched_org_node_ids = match_live_state_to_org(surface, mission, graph)
    shot.matched_goal_ids = match_live_state_to_goals(snapshot=shot, mission=mission, graph=graph)

    blockers = detect_live_blockers(
        snapshot=shot,
        actionable=shot.actionable_entities,
        validators=validators,
        context_hints=hints,
    )
    shot.live_blockers = blockers

    # Confidence heuristic
    reasons: List[str] = []
    conf = 0.35
    if url:
        conf += 0.12
        reasons.append("url_present")
    if val_obs:
        conf += min(0.2, 0.04 * len([v for v in val_obs if v.satisfied]))
        reasons.append("validators_positive")
    if visible:
        conf += min(0.18, 0.02 * min(len(visible), 9))
        reasons.append("entities_observed")
    if surface != LiveOperationalSurfaceType.UNKNOWN_SURFACE:
        conf += 0.08
        reasons.append("surface_classified")
    if ocr_invoked:
        reasons.append("ocr_light_used")
        conf -= 0.02
    if not uia_flat and not _dom_nodes(dom):
        conf -= 0.15
        shot.perception_gaps.append("no_uia_no_dom")
    conf = max(0.0, min(0.95, conf))
    shot.confidence = round(conf, 4)
    shot.confidence_reasons = reasons[:12]

    act, safe = decide_live_runtime_action(snapshot=shot, blockers=blockers)
    shot.recommended_runtime_action = act
    shot.safe_to_continue = safe

    _apply_budget_and_critical_gap_guards(snapshot=shot)

    elapsed = (time.perf_counter() - t0) * 1000.0
    shot.raw_sensor_stats["elapsed_ms"] = round(elapsed, 3)
    if elapsed > max_elapsed_ms:
        shot.perception_gaps.append(f"slow_path:{elapsed:.1f}ms> {max_elapsed_ms}")

    if hints.get("inject_ambiguity_signal"):
        shot.ambiguity_signals.append(
            LiveOperationalSignal(kind="test", source="operational_context", weight=0.2, payload={}),
        )

    return shot


def refresh_lop_shadow(
    mission: Mission,
    settings: Optional[Any] = None,
    *,
    perception_input: Optional[LivePerceptionInput] = None,
    graph: Optional[OperationalRuntimeGraph] = None,
    ocr_light: OcrLightFn = None,
) -> LiveOperationalPerceptionSnapshot:
    """Persiste último snapshot + auditoría en Mission (sombra). No muta SEP."""

    if not getattr(mission, "lop_shadow_mode", False):
        miss = LiveOperationalPerceptionSnapshot(
            perception_gaps=["lop_shadow_mode_false"],
            recommended_runtime_action=LiveOperationalRuntimeAction.WAIT,
        )
        mission.live_operational_perception_audit = {"ok": False, "reason": "lop_shadow_mode_false"}
        return miss

    if not _settings_lop_enabled(settings):
        miss = LiveOperationalPerceptionSnapshot(
            perception_gaps=["LOP_SHADOW_DISABLED"],
            recommended_runtime_action=LiveOperationalRuntimeAction.WAIT,
        )
        mission.live_operational_perception_audit = {"ok": False, "reason": "LOP_SHADOW_DISABLED"}
        mission.lop_last_snapshot = miss.model_dump(mode="json")
        return miss

    try:
        snap = capture_live_operational_snapshot(
            mission,
            perception_input=perception_input,
            graph=graph,
            ocr_light=ocr_light,
        )
        blockers = list(getattr(snap, "live_blockers", None) or [])
        audit = build_lop_audit(
            snapshot=snap,
            elapsed_ms=float(snap.raw_sensor_stats.get("elapsed_ms") or 0.0),
            ocr_invoked=bool(snap.raw_sensor_stats.get("ocr_invoked")),
            sensor_sources=["uia", "dom", "validators", "operational_context"]
            + (["ocr_light"] if snap.raw_sensor_stats.get("ocr_invoked") else []),
        )
        audit["blocker_count"] = len(blockers)
        audit["ok"] = True
        mission.live_operational_perception_audit = audit
        mission.lop_last_snapshot = snap.model_dump(mode="json")
        log.debug(
            "[LOP] surface=%s action=%s safe=%s conf=%s",
            snap.surface_type.value,
            snap.recommended_runtime_action.value,
            snap.safe_to_continue,
            snap.confidence,
        )
        return snap
    except Exception as exc:
        log.debug("[LOP] refresh failed: %s", exc)
        mission.live_operational_perception_audit = {"ok": False, "reason": str(exc)}
        fail = LiveOperationalPerceptionSnapshot(
            perception_gaps=[f"lop_fault:{exc}"],
            recommended_runtime_action=LiveOperationalRuntimeAction.REQUEST_HUMAN,
        )
        mission.lop_last_snapshot = fail.model_dump(mode="json")
        return fail


def arl_hints_from_lop(mission: Mission) -> Dict[str, Any]:
    """ARL puede consumir blockers LOP sin acoplarse al modelo completo."""

    raw = getattr(mission, "lop_last_snapshot", None)
    if not isinstance(raw, dict) or not raw:
        return {"blockers": [], "lop_present": False}
    blockers: List[Dict[str, Any]] = []
    for b in raw.get("live_blockers") or []:
        if isinstance(b, dict):
            blockers.append({"type": b.get("blocker_type"), "description": b.get("description"), "source": "lop"})
    aud = getattr(mission, "live_operational_perception_audit", None) or {}
    if raw.get("modal_detected"):
        blockers.append({"type": "modal_blocking", "source": "lop"})
    if str(raw.get("recommended_runtime_action") or "") in {"STOP_UNSAFE", "REQUEST_HUMAN"}:
        blockers.append(
            {
                "type": "lop_recommended_pause",
                "action": raw.get("recommended_runtime_action"),
                "source": "lop",
            },
        )
    for g in raw.get("perception_gaps") or []:
        blockers.append({"type": "perception_gap", "detail": str(g), "source": "lop"})
    return {
        "blockers": blockers,
        "lop_present": True,
        "safe_to_continue": raw.get("safe_to_continue"),
        "confidence": raw.get("confidence"),
        "audit_ok": isinstance(aud, dict) and aud.get("ok"),
    }


def build_arl_lop_recovery_hints(mission: Mission) -> Dict[str, Any]:
    """API pública — mismos hints que :func:`arl_hints_from_lop`."""

    return arl_hints_from_lop(mission)


def goor_context_with_lop(mission: Mission, graph: OperationalRuntimeGraph) -> Any:
    """Helper: OperationalDecisionContext incluye eco LOP cuando existe snapshot."""

    from app.services.runtime.goal_oriented_runtime import build_operational_decision_context

    return build_operational_decision_context(mission, graph)


def lop_expert_panel_lines(mission: Mission) -> List[str]:
    """Texto para Mission Review (experto) — sin dependencia de Qt."""

    raw = getattr(mission, "lop_last_snapshot", None)
    if not isinstance(raw, dict) or not raw:
        return []
    lines = [
        f"  surface_type: {raw.get('surface_type')}",
        f"  operational_state_guess: {raw.get('operational_state_guess')}",
        f"  matched_goal_ids: {raw.get('matched_goal_ids')}",
        f"  matched_org_node_ids: {raw.get('matched_org_node_ids')}",
        f"  modal_detected: {raw.get('modal_detected')}",
        f"  safe_to_continue: {raw.get('safe_to_continue')}",
        f"  recommended_runtime_action: {raw.get('recommended_runtime_action')}",
        f"  confidence: {raw.get('confidence')}",
    ]
    gaps = raw.get("perception_gaps") or []
    if gaps:
        lines.append(f"  perception_gaps: {gaps[:6]}")
    lines.append(f"  live_blockers: {len(raw.get('live_blockers') or [])}")
    ve = raw.get("validators_observed") or []
    if isinstance(ve, list) and ve:
        first = ve[:5]
        lines.append(f"  validators_observed (sample): {first}")
    entn = len(raw.get("visible_entities") or []) if isinstance(raw.get("visible_entities"), list) else 0
    lines.append(f"  entities_visible_count: {entn}")
    return lines


__all__ = [
    "LivePerceptionInput",
    "arl_hints_from_lop",
    "build_arl_lop_recovery_hints",
    "build_lop_audit",
    "capture_live_operational_snapshot",
    "classify_live_surface",
    "decide_live_runtime_action",
    "detect_live_blockers",
    "extract_live_entities",
    "goor_context_with_lop",
    "lop_expert_panel_lines",
    "match_live_state_to_goals",
    "match_live_state_to_org",
    "observe_live_validators",
    "refresh_lop_shadow",
]
