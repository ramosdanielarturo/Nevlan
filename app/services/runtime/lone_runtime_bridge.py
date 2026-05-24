"""
LONE Runtime Bridge — percepción LOP viva + ejecución ActionRunner segura.

Conecta el loop LONE (ONG, planificación, memoria) a sensores LOP reales y
estrategias ActionRunner sin coords primarios ni runtime paralelo.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    LiveOperationalBlocker,
    LiveOperationalEntity,
    LiveOperationalPerceptionSnapshot,
    LiveOperationalRuntimeAction,
    OperationalCollection,
    OperationalEntity,
    OperationalNavigationAction,
)
from app.core.logger import log
from app.services.missions.action_runner import ActionRunner
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime.live_operational_navigation_engine import (
    NavigationLoopResult,
    append_navigation_audit,
    detect_dynamic_surface_state,
    is_unsafe_navigation_context,
    run_operational_navigation_loop,
)
from app.services.runtime.live_operational_perception import (
    LiveOperationalSurfaceType,
)

# Re-export markers for safety alignment with LONE engine
from app.services.runtime.live_operational_navigation_engine import (  # noqa: F401
    UNSAFE_NAV_MARKERS,
)

ObserveFn = Callable[
    [Dict[str, Any]],
    Tuple[Sequence[OperationalCollection], Sequence[Dict[str, Any]]],
]
NavigationExecutor = Callable[
    [OperationalNavigationAction, Dict[str, Any]],
    Dict[str, Any],
]

_LOP_UNSAFE_BLOCKER_TYPES = frozenset({
    "auth_required",
    "permission_dialog",
    "unknown_destructive_confirmation",
    "modal_blocking",
    "unexpected_app_switch",
})
_LOP_UNSAFE_SURFACE_HINTS = frozenset({
    "auth", "password", "payment", "checkout", "transfer", "delete",
})
_PAGINATE_LABEL_MARKERS = frozenset({
    "next", "siguiente", "next page", "página siguiente", "more results",
})
_SEARCHBOX_ROLES = frozenset({
    "searchbox", "combobox", "edit", "edittext", "editcontrol",
})


@dataclass
class LoneRuntimeBridgeConfig:
    """Configuración efectiva del bridge (settings + overrides de test)."""

    enabled: bool = False
    max_depth: int = 5
    settle_ms: int = 300
    require_lop_safe: bool = True
    allow_browser_history: bool = False
    min_lop_confidence: float = 0.35


@dataclass
class LoneLiveRecoveryResult(NavigationLoopResult):
    """Resultado del loop live con auditoría extendida."""

    live_mode: bool = True
    snapshots_taken: int = 0
    blockers_seen: List[str] = field(default_factory=list)
    progress_signals: List[str] = field(default_factory=list)
    safety_stop_reason: str = ""
    used_lop: bool = False
    used_action_runner: bool = False
    total_latency_ms: float = 0.0
    bridge_fallback: bool = False


def lone_bridge_config_from_settings(settings: Any = None) -> LoneRuntimeBridgeConfig:
    if settings is None:
        try:
            from app.core.config import get_settings

            settings = get_settings()
        except Exception:
            return LoneRuntimeBridgeConfig()
    return LoneRuntimeBridgeConfig(
        enabled=bool(getattr(settings, "LONE_LIVE_BRIDGE_ENABLED", False)),
        max_depth=int(getattr(settings, "LONE_MAX_LIVE_DEPTH", 5)),
        settle_ms=int(getattr(settings, "LONE_ACTION_SETTLE_MS", 300)),
        require_lop_safe=bool(getattr(settings, "LONE_REQUIRE_LOP_SAFE", True)),
        allow_browser_history=bool(
            getattr(settings, "LONE_ALLOW_BROWSER_HISTORY", False),
        ),
    )


def is_lone_live_bridge_available(
    mission: Any,
    *,
    settings: Any = None,
) -> bool:
    """True si el bridge puede usarse (flag + LOP live feed)."""
    cfg = lone_bridge_config_from_settings(settings)
    if not cfg.enabled:
        return False
    if mission is None:
        return False
    if not getattr(mission, "lop_live_feed_enabled", False):
        return False
    if not getattr(mission, "lop_shadow_mode", False):
        return False
    try:
        from app.core.config import get_settings

        s = settings or get_settings()
        if not getattr(s, "LOP_SHADOW_ENABLED", True):
            return False
    except Exception:
        return False
    return True


def _parse_lop_snapshot(mission: Any) -> Optional[LiveOperationalPerceptionSnapshot]:
    raw = getattr(mission, "lop_last_snapshot", None)
    if not raw:
        return None
    try:
        if isinstance(raw, LiveOperationalPerceptionSnapshot):
            return raw
        return LiveOperationalPerceptionSnapshot.model_validate(raw)
    except Exception:
        return None


def _entity_to_uia_node(ent: LiveOperationalEntity) -> Dict[str, Any]:
    label = str(ent.label_guess or "").strip()
    roles = list(ent.roles or [])
    role = roles[0] if roles else str(ent.kind.value)
    hints = dict(ent.uia_hints or {})
    node: Dict[str, Any] = {
        "name": label,
        "label": label,
        "control_type": role,
        "role": role,
        "actionable": bool(ent.actionable),
        "confidence": float(ent.confidence or 0.0),
        "entity_id": ent.entity_id,
        "children": [],
    }
    if hints:
        node.update({k: v for k, v in hints.items() if k not in node})
    for snippet in ent.text_snippets or []:
        if snippet and snippet not in (node.get("name"), node.get("label")):
            node.setdefault("help_text", str(snippet)[:280])
    return node


def lop_snapshot_to_uia_flat(
    snap: LiveOperationalPerceptionSnapshot,
    *,
    max_nodes: int = 280,
) -> List[Dict[str, Any]]:
    """Convierte entidades LOP a nodos UIA-like para UCES/LONE."""
    nodes: List[Dict[str, Any]] = []
    seen: set = set()

    def add_ent(ent: LiveOperationalEntity) -> None:
        if len(nodes) >= max_nodes:
            return
        key = (ent.entity_id, ent.label_guess)
        if key in seen:
            return
        seen.add(key)
        if ent.label_guess or ent.roles:
            nodes.append(_entity_to_uia_node(ent))

    for ent in snap.actionable_entities or []:
        add_ent(ent)
    for ent in snap.visible_entities or []:
        add_ent(ent)

    for surf in (snap.navigation_surfaces or []) + (snap.result_surfaces or []):
        for ev in surf.evidence or []:
            if ev and len(nodes) < max_nodes:
                nodes.append({
                    "name": str(ev)[:200],
                    "control_type": str(surf.surface_type.value),
                    "role": "surface_hint",
                    "children": [],
                })

    return nodes


def assess_lop_navigation_safety(
    snap: Optional[LiveOperationalPerceptionSnapshot],
    *,
    uia_flat: Optional[Sequence[Dict[str, Any]]] = None,
    require_lop_safe: bool = True,
    min_confidence: float = 0.35,
) -> Tuple[bool, str]:
    """
    Fase 3 — safety gates antes de ejecutar navegación.

    Returns (unsafe, reason). unsafe=True ⇒ REQUEST_HUMAN / STOP_UNSAFE.
    """
    if snap is None:
        if require_lop_safe:
            return True, "lop_snapshot_missing"
        return False, ""

    for blk in snap.live_blockers or []:
        bt = str(getattr(blk, "blocker_type", "") or "").lower()
        sev = str(getattr(blk, "severity", "") or "").lower()
        if bt in _LOP_UNSAFE_BLOCKER_TYPES or sev == "critical":
            return True, f"blocker:{bt or 'critical'}"
        desc = str(getattr(blk, "description", "") or "").lower()
        if any(m in desc for m in UNSAFE_NAV_MARKERS):
            return True, "blocker_description_unsafe"

    if snap.modal_detected:
        if not snap.safe_to_continue:
            return True, "unknown_modal"
        for blk in snap.live_blockers or []:
            if str(blk.blocker_type).lower() in _LOP_UNSAFE_BLOCKER_TYPES:
                return True, "modal_with_blocker"

    rec = snap.recommended_runtime_action
    if rec == LiveOperationalRuntimeAction.STOP_UNSAFE:
        return True, "lop_stop_unsafe"
    if rec == LiveOperationalRuntimeAction.REQUEST_HUMAN:
        return True, "lop_request_human"

    if require_lop_safe and not snap.safe_to_continue:
        return True, "lop_not_safe_to_continue"

    conf = float(snap.confidence or 0.0)
    if require_lop_safe and conf < min_confidence:
        return True, "low_confidence_surface"

    st = str(snap.surface_type.value if snap.surface_type else "").lower()
    if any(h in st for h in _LOP_UNSAFE_SURFACE_HINTS):
        return True, "unsafe_surface_type"

    state_guess = str(snap.operational_state_guess or "").lower()
    if any(m in state_guess for m in ("auth", "password", "payment", "checkout")):
        return True, "unsafe_operational_state"

    win = f"{snap.active_window} {snap.active_url}".lower()
    if any(m in win for m in ("sign in", "password", "checkout", "payment", "transfer")):
        return True, "unsafe_window_context"

    # Multiple equivalent dangerous actions
    dangerous: List[str] = []
    for ent in snap.actionable_entities or []:
        blob = f"{ent.label_guess} {' '.join(ent.roles)}".lower()
        if any(m in blob for m in UNSAFE_NAV_MARKERS):
            dangerous.append(ent.label_guess)
    if len(dangerous) >= 2:
        return True, "multiple_dangerous_actions"

    unsafe_ctx, reason = is_unsafe_navigation_context(uia_flat=uia_flat)
    if unsafe_ctx:
        return True, reason or "unsafe_uia_context"

    return False, ""


def _refresh_lop_snapshot(
    mission: Any,
    settings: Any = None,
) -> Optional[LiveOperationalPerceptionSnapshot]:
    try:
        from app.services.runtime.lop_sensor_bridge import refresh_lop_from_live_sensors

        return refresh_lop_from_live_sensors(mission, settings)
    except Exception as exc:
        log.debug("[LONE-BRIDGE] LOP refresh failed: %s", exc)
        return _parse_lop_snapshot(mission)


def build_lone_observe_fn_from_lop(
    mission: Any,
    *,
    settings: Any = None,
    state: Any = None,
    bridge_state: Optional[Dict[str, Any]] = None,
) -> ObserveFn:
    """
    Fase 1 — observe_fn que refresca LOP y alimenta UCES con percepción viva.
    """
    shared = bridge_state if bridge_state is not None else {}
    cfg = lone_bridge_config_from_settings(settings)

    def observe(ctx: Dict[str, Any]) -> Tuple[List[OperationalCollection], List[Dict[str, Any]]]:
        from app.services.runtime.universal_collection_entity_engine import (
            detect_operational_collections,
        )

        t0 = time.perf_counter()
        snap = _refresh_lop_snapshot(mission, settings)
        shared["snapshots_taken"] = int(shared.get("snapshots_taken") or 0) + 1
        shared["used_lop"] = True

        uia_flat: List[Dict[str, Any]] = []
        if snap is not None:
            uia_flat = lop_snapshot_to_uia_flat(snap)
            ctx["lop_snapshot"] = snap
            ctx["viewport_signature"] = _viewport_from_snapshot(snap, uia_flat)
            ctx["active_app"] = snap.active_app
            ctx["active_window"] = snap.active_window
            ctx["active_url"] = snap.active_url
            dynamic = detect_dynamic_surface_state(uia_flat=uia_flat, context=ctx)
            ctx["dynamic_surface_state"] = dynamic
            ctx["operational_affordances"] = [
                e.label_guess for e in (snap.actionable_entities or [])[:24]
            ]
            for blk in snap.live_blockers or []:
                bt = str(getattr(blk, "blocker_type", "") or "")
                if bt:
                    shared.setdefault("blockers_seen", []).append(bt)

            unsafe, reason = assess_lop_navigation_safety(
                snap,
                uia_flat=uia_flat,
                require_lop_safe=cfg.require_lop_safe,
                min_confidence=cfg.min_lop_confidence,
            )
            if unsafe:
                ctx["lone_safety_stop"] = reason
                shared["safety_stop_reason"] = reason
                return [], uia_flat

        elif state is not None:
            try:
                sd = state.to_dict() if hasattr(state, "to_dict") else {}
                uia_flat = list(sd.get("uia_flat") or sd.get("candidates") or [])
            except Exception:
                pass

        colls = detect_operational_collections(
            capture_bundle={"uia_flat": uia_flat, **ctx},
        )
        shared["last_observe_ms"] = (time.perf_counter() - t0) * 1000.0
        return list(colls), uia_flat

    return observe


def _viewport_from_snapshot(
    snap: LiveOperationalPerceptionSnapshot,
    uia_flat: Sequence[Dict[str, Any]],
) -> str:
    from app.services.runtime.live_operational_navigation_engine import (
        _viewport_signature,
    )

    names = [str(n.get("name") or "") for n in uia_flat[:16]]
    topo = snap.topology
    return _viewport_signature(
        width=int(getattr(topo, "scroll_container_count", 0) or 0) * 100,
        height=len(names) * 10,
        scroll_y=int(snap.raw_sensor_stats.get("scroll_y") or 0),
        entity_sample=names,
    )


def translate_navigation_action_to_runtime(
    action: OperationalNavigationAction,
    ctx: Dict[str, Any],
    *,
    allow_browser_history: bool = False,
) -> Dict[str, Any]:
    """Mapea OperationalNavigationAction → estrategia ActionRunner / metadatos."""
    base: Dict[str, Any] = {
        "action": action.value,
        "entity_first": True,
        "use_coords": False,
    }
    if action == OperationalNavigationAction.SCROLL_DOWN:
        base.update(kind="scroll_page", strategy="scroll_page:wheel", direction="down", amount=1)
    elif action == OperationalNavigationAction.SCROLL_UP:
        base.update(kind="scroll_page", strategy="scroll_page:wheel", direction="up", amount=1)
    elif action == OperationalNavigationAction.WAIT_DYNAMIC_CONTENT:
        base.update(kind="wait", strategy="internal:settle", wait_ms=ctx.get("wait_ms", 400))
    elif action == OperationalNavigationAction.PAGINATE_NEXT:
        base.update(kind="click", strategy="entity_resolution", affordance="paginate_next")
    elif action == OperationalNavigationAction.PAGINATE_PREVIOUS:
        base.update(kind="click", strategy="entity_resolution", affordance="paginate_previous")
    elif action == OperationalNavigationAction.FOCUS_SEARCH:
        base.update(kind="focus", strategy="entity_resolution", affordance="searchbox")
    elif action == OperationalNavigationAction.RETRY_LOAD:
        base.update(kind="refresh", strategy="scroll_page:keyboard", safe_refresh=True)
    elif action == OperationalNavigationAction.REFRESH_SURFACE:
        base.update(kind="refresh", strategy="internal:revalidate", safe_refresh=True)
    elif action in (
        OperationalNavigationAction.NAVIGATE_BACK,
        OperationalNavigationAction.NAVIGATE_FORWARD,
    ):
        if allow_browser_history:
            base.update(
                kind="browser_history",
                strategy="keyboard:alt_left" if action == OperationalNavigationAction.NAVIGATE_BACK else "keyboard:alt_right",
            )
        else:
            base.update(kind="noop", strategy="blocked:browser_history_disabled")
    else:
        base.update(kind="scroll_page", strategy="scroll_page:keyboard", direction="down", amount=1)
    return base


def _find_affordance_label(
    uia_flat: Sequence[Dict[str, Any]],
    markers: frozenset,
) -> Optional[str]:
    for n in uia_flat:
        name = str(n.get("name") or n.get("label") or "").strip()
        if not name:
            continue
        blob = name.lower()
        if any(m in blob for m in markers):
            return name
    return None


def _click_entity_by_label(label: str) -> bool:
    try:
        from app.services.runtime.entity_runtime_resolution import (
            execute_entity_target_click,
        )
        from app.contracts.mission import OperationalEntity, OperationalEntityType

        ent = OperationalEntity(
            entity_type=OperationalEntityType.UNKNOWN,
            display_name=label,
        )
        res = execute_entity_target_click({"label": label}, ent)
        return bool(res.ok)
    except Exception as exc:
        log.debug("[LONE-BRIDGE] entity click: %s", exc)
        return False


def build_lone_execute_fn_from_action_runner(
    *,
    action_runner: Optional[ActionRunner] = None,
    state: Optional[StateSnapshot] = None,
    step: Optional[MissionStep] = None,
    settings: Any = None,
    bridge_state: Optional[Dict[str, Any]] = None,
) -> NavigationExecutor:
    """
    Fase 2 — execute_fn que traduce acciones LONE a ActionRunner (sin coords).
    """
    runner = action_runner or ActionRunner()
    cfg = lone_bridge_config_from_settings(settings)
    shared = bridge_state if bridge_state is not None else {}
    snap_state = state or StateSnapshot()

    def execute(action: OperationalNavigationAction, ctx: Dict[str, Any]) -> Dict[str, Any]:
        if ctx.get("lone_safety_stop"):
            ctx["execution_blocked"] = ctx["lone_safety_stop"]
            return ctx

        before_vp = str(ctx.get("viewport_signature") or "")
        runtime = translate_navigation_action_to_runtime(
            action,
            ctx,
            allow_browser_history=cfg.allow_browser_history,
        )
        ctx["last_runtime_plan"] = runtime
        shared["used_action_runner"] = True
        t0 = time.perf_counter()
        ok = False
        msg = ""

        uia_flat = list(ctx.get("last_uia_flat") or [])
        snap = ctx.get("lop_snapshot")
        if snap is not None and not uia_flat:
            uia_flat = lop_snapshot_to_uia_flat(snap)

        strat = str(runtime.get("strategy") or "")

        if runtime.get("kind") == "scroll_page" and strat.startswith("scroll_page"):
            nav_step = MissionStep(
                id=str(getattr(step, "id", "lone:scroll") if step else "lone:scroll"),
                kind="scroll_page",
                params={
                    "direction": runtime.get("direction", "down"),
                    "amount": runtime.get("amount", 1),
                    "lone_bridge": True,
                },
            )
            res = runner.run_strategy(strat, nav_step, snap_state)
            ok, msg = res.ok, res.message
            if ok:
                step_px = int(ctx.get("scroll_step") or 320)
                sy = int(ctx.get("scroll_y") or 0)
                if runtime.get("direction") == "up":
                    ctx["scroll_y"] = max(0, sy - step_px)
                else:
                    ctx["scroll_y"] = sy + step_px

        elif action == OperationalNavigationAction.WAIT_DYNAMIC_CONTENT:
            wait_ms = min(2000, int(runtime.get("wait_ms") or cfg.settle_ms * 2))
            time.sleep(wait_ms / 1000.0)
            ok, msg = True, f"wait {wait_ms}ms"

        elif runtime.get("affordance") == "paginate_next":
            label = _find_affordance_label(uia_flat, _PAGINATE_LABEL_MARKERS)
            if label:
                ok = _click_entity_by_label(label)
                msg = f"paginate entity-first '{label[:60]}'"
            else:
                ctx["no_paginate_affordance"] = True
                ok, msg = False, "no next-page affordance"

        elif runtime.get("affordance") == "searchbox":
            label = None
            for n in uia_flat:
                role = str(n.get("role") or n.get("control_type") or "").lower()
                if any(r in role for r in _SEARCHBOX_ROLES) or "search" in role:
                    label = str(n.get("name") or n.get("label") or "Search")
                    break
            if label:
                ok = _click_entity_by_label(label)
                msg = f"focus search '{label[:60]}'"
            else:
                ok, msg = False, "no searchbox affordance"

        elif action == OperationalNavigationAction.RETRY_LOAD and runtime.get("safe_refresh"):
            wait_ms = cfg.settle_ms
            time.sleep(wait_ms / 1000.0)
            ok, msg = True, "retry_load settle (no destructive refresh)"

        elif runtime.get("strategy", "").startswith("blocked:"):
            ctx["execution_blocked"] = runtime["strategy"]
            ok, msg = False, runtime["strategy"]

        else:
            nav_step = MissionStep(
                id="lone:fallback_scroll",
                kind="scroll_page",
                params={"direction": "down", "amount": 1, "lone_bridge": True},
            )
            res = runner.run_strategy("scroll_page:keyboard", nav_step, snap_state)
            ok, msg = res.ok, res.message

        settle = max(0, cfg.settle_ms)
        time.sleep(settle / 1000.0)

        ctx["last_action_ok"] = ok
        ctx["last_action_msg"] = msg
        ctx["last_action"] = action.value
        ctx["last_uia_flat"] = uia_flat
        shared.setdefault("actions_executed", []).append({
            "action": action.value,
            "ok": ok,
            "message": msg,
            "latency_ms": (time.perf_counter() - t0) * 1000.0,
        })

        after_vp = str(ctx.get("viewport_signature") or "")
        if before_vp and after_vp == before_vp and action in (
            OperationalNavigationAction.SCROLL_DOWN,
            OperationalNavigationAction.SCROLL_UP,
            OperationalNavigationAction.PAGINATE_NEXT,
        ):
            ctx["no_viewport_progress"] = int(ctx.get("no_viewport_progress") or 0) + 1
            shared.setdefault("progress_signals", []).append("no_viewport_change")
        elif before_vp != after_vp:
            shared.setdefault("progress_signals", []).append("viewport_changed")
            ctx.pop("no_viewport_progress", None)

        return ctx

    return execute


def validate_navigation_after_action(
    ctx: Dict[str, Any],
    *,
    previous_viewport: str = "",
    max_stall_without_progress: int = 2,
) -> Tuple[bool, str]:
    """
    Fase 5 — validación post-acción: progreso, blockers, app inesperada.
    """
    if ctx.get("lone_safety_stop"):
        return False, str(ctx["lone_safety_stop"])

    if ctx.get("execution_blocked"):
        return False, str(ctx["execution_blocked"])

    snap = ctx.get("lop_snapshot")
    if isinstance(snap, LiveOperationalPerceptionSnapshot):
        unsafe, reason = assess_lop_navigation_safety(snap, require_lop_safe=True)
        if unsafe:
            ctx["post_action_blocker"] = reason
            return False, reason

    stall = int(ctx.get("no_viewport_progress") or 0)
    if stall >= max_stall_without_progress:
        return False, "no_measurable_progress"

    vp = str(ctx.get("viewport_signature") or "")
    if previous_viewport and vp and vp == previous_viewport:
        if ctx.get("last_action") in (
            OperationalNavigationAction.SCROLL_DOWN.value,
            OperationalNavigationAction.PAGINATE_NEXT.value,
        ):
            dynamic = ctx.get("dynamic_surface_state")
            if dynamic is not None and getattr(dynamic, "needs_wait", False):
                return True, "wait_loading"
            if stall >= 1:
                return False, "stalled_viewport"

    if ctx.get("last_action_ok") is False and ctx.get("no_paginate_affordance"):
        return False, "paginate_failed"

    return True, "ok"


def persist_lone_runtime_audit(
    mission: Any,
    *,
    result: LoneLiveRecoveryResult,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Fase 7 — auditoría extendida en operational_navigation_audit."""
    entry: Dict[str, Any] = {
        "live_mode": result.live_mode,
        "success": result.success,
        "actions_executed": list(result.actions_executed),
        "snapshots_taken": result.snapshots_taken,
        "blockers_seen": list(result.blockers_seen),
        "progress_signals": list(result.progress_signals),
        "found_entity": bool(result.entity),
        "safety_stop_reason": result.safety_stop_reason or "",
        "used_lop": result.used_lop,
        "used_action_runner": result.used_action_runner,
        "total_latency_ms": round(result.total_latency_ms, 2),
        "depth_used": result.depth_reached,
        "stopped_reason": result.stopped_reason,
        "confidence": result.confidence,
        "request_human": result.request_human,
    }
    if extra:
        entry.update(extra)
    append_navigation_audit(mission, entry)


def run_lone_recovery_with_live_perception(
    step: Any,
    state: Any,
    mission: Any,
    *,
    target_entity: OperationalEntity,
    target_collection: Optional[OperationalCollection] = None,
    settings: Any = None,
    action_runner: Optional[ActionRunner] = None,
    config: Optional[LoneRuntimeBridgeConfig] = None,
    budget: Any = None,
) -> LoneLiveRecoveryResult:
    """
    Fase 4 — loop live: LOP observe → plan → ActionRunner execute → revalidate.
    """
    cfg = config or lone_bridge_config_from_settings(settings)
    t_start = time.perf_counter()
    bridge_state: Dict[str, Any] = {
        "snapshots_taken": 0,
        "blockers_seen": [],
        "progress_signals": [],
        "actions_executed": [],
    }

    if not cfg.enabled:
        out = LoneLiveRecoveryResult(
            live_mode=False,
            bridge_fallback=True,
            stopped_reason="bridge_disabled",
        )
        return out

    mission_step = step if isinstance(step, MissionStep) else None
    if mission_step is None and step is not None:
        mission_step = MissionStep(
            id=str(getattr(step, "id", "lone:live")),
            kind=str(getattr(step, "kind", "click")),
            params=dict(getattr(step, "params", None) or {}),
        )

    observe = build_lone_observe_fn_from_lop(
        mission,
        settings=settings,
        state=state,
        bridge_state=bridge_state,
    )
    execute = build_lone_execute_fn_from_action_runner(
        action_runner=action_runner,
        state=state if isinstance(state, StateSnapshot) else StateSnapshot(),
        step=mission_step,
        settings=settings,
        bridge_state=bridge_state,
    )

    ctx: Dict[str, Any] = {"step_id": getattr(step, "id", "")}
    try:
        pars = dict(getattr(step, "params", None) or {})
        fr = pars.get("operational_freeze_snapshot")
        if isinstance(fr, dict):
            from app.contracts.mission import OperationalFreezeSnapshot
            from app.services.runtime.pre_click_operational_freeze import (
                lone_initial_ong_node_from_freeze,
            )

            freeze = OperationalFreezeSnapshot.model_validate(fr)
            seed = lone_initial_ong_node_from_freeze(freeze)
            if seed:
                ctx["pcof_initial_ong_node"] = seed
    except Exception as exc:
        log.debug("[LONE bridge] pcof seed: %s", exc)
    prev_vp = ""

    def wrapped_observe(inner_ctx: Dict[str, Any]) -> Tuple[List[OperationalCollection], List[Dict[str, Any]]]:
        nonlocal prev_vp
        if inner_ctx.get("lone_safety_stop"):
            return [], list(inner_ctx.get("last_uia_flat") or [])
        colls, uia = observe(inner_ctx)
        inner_ctx["last_uia_flat"] = uia
        prev_vp = str(inner_ctx.get("viewport_signature") or prev_vp)
        return colls, uia

    def wrapped_execute(
        action: OperationalNavigationAction,
        inner_ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        if inner_ctx.get("lone_safety_stop"):
            return inner_ctx
        before = str(inner_ctx.get("viewport_signature") or "")
        inner_ctx = execute(action, inner_ctx)
        valid, reason = validate_navigation_after_action(
            inner_ctx,
            previous_viewport=before,
        )
        if not valid and reason not in ("ok", "wait_loading"):
            inner_ctx["navigation_validation_failed"] = reason
            bridge_state.setdefault("progress_signals", []).append(f"invalid:{reason}")
        return inner_ctx

    if budget is not None and hasattr(budget, "deadline_mono"):
        loop_deadline = budget.deadline_mono
    else:
        lone_budget_ms = 8000
        try:
            if settings is None:
                from app.core.config import get_settings
                settings = get_settings()
            lone_budget_ms = int(getattr(settings, "LONE_LOOP_BUDGET_MS", 8000))
        except Exception:
            pass
        loop_deadline = time.perf_counter() + (lone_budget_ms / 1000.0)

    loop_result = run_operational_navigation_loop(
        target_entity=target_entity,
        target_collection=target_collection,
        observe_fn=wrapped_observe,
        execute_fn=wrapped_execute,
        mission=mission,
        max_depth=cfg.max_depth,
        context=ctx,
        deadline_mono=loop_deadline,
        progress_budget=budget,
    )

    if ctx.get("lone_safety_stop"):
        loop_result.request_human = True
        loop_result.stopped_reason = str(ctx["lone_safety_stop"])
        loop_result.human_message = "Esta acción requiere confirmación humana."

    out = LoneLiveRecoveryResult(
        success=loop_result.success,
        entity=loop_result.entity,
        collection=loop_result.collection,
        confidence=loop_result.confidence,
        depth_reached=loop_result.depth_reached,
        stopped_reason=loop_result.stopped_reason,
        actions_executed=list(loop_result.actions_executed),
        request_human=loop_result.request_human,
        human_message=loop_result.human_message,
        graph=loop_result.graph,
        live_mode=True,
        snapshots_taken=int(bridge_state.get("snapshots_taken") or 0),
        blockers_seen=list(bridge_state.get("blockers_seen") or []),
        progress_signals=list(bridge_state.get("progress_signals") or []),
        safety_stop_reason=str(
            bridge_state.get("safety_stop_reason") or ctx.get("lone_safety_stop") or "",
        ),
        used_lop=bool(bridge_state.get("used_lop")),
        used_action_runner=bool(bridge_state.get("used_action_runner")),
        total_latency_ms=(time.perf_counter() - t_start) * 1000.0,
    )

    if ctx.get("navigation_validation_failed") and not out.success:
        out.stopped_reason = str(ctx["navigation_validation_failed"])

    persist_lone_runtime_audit(mission, result=out)
    return out


__all__ = [
    "LoneLiveRecoveryResult",
    "LoneRuntimeBridgeConfig",
    "assess_lop_navigation_safety",
    "build_lone_execute_fn_from_action_runner",
    "build_lone_observe_fn_from_lop",
    "is_lone_live_bridge_available",
    "lone_bridge_config_from_settings",
    "lop_snapshot_to_uia_flat",
    "persist_lone_runtime_audit",
    "run_lone_recovery_with_live_perception",
    "translate_navigation_action_to_runtime",
    "validate_navigation_after_action",
]
