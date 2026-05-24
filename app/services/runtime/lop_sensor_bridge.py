"""LOP Sensor Bridge — read-only sensory assembly for Phase 2 LOP."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    LopSensorAudit,
    LopSensorBridgeConfig,
    LopSensorBudget,
    LopSensorCaptureResult,
    LopSensorHealth,
    LopSensorHealthStatus,
    LiveOperationalRuntimeAction,
    Mission,
)
from app.contracts.operational_runtime_graph import OperationalRuntimeGraph
from app.core.logger import log
from app.services.runtime.live_operational_perception import (
    LiveOperationalPerceptionSnapshot,
    LivePerceptionInput,
    _collect_plain_text_budget,
    _flatten_uia,
    build_lop_audit,
    capture_live_operational_snapshot,
    refresh_lop_shadow,
)


def capture_active_window_context(*, overall_deadline: float, uia_budget_ms: int) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {
        "active_app_class": "",
        "active_process": "",
        "active_window_title": "",
        "foreground_hwnd": 0,
        "window_bbox": None,
        "dpi_x": None,
        "dpi_y": None,
        "monitor_dpi": None,
    }
    if time.perf_counter() >= overall_deadline:
        return ctx
    try:
        import uiautomation as auto  # type: ignore import-not-found

        root = auto.GetForegroundControl().GetTopLevelControl()
        if hasattr(root, "Exists") and not root.Exists(0, 0):
            return ctx
        hwnd = getattr(root, "NativeWindowHandle", None) or getattr(root, "Handle", None) or 0
        ctx["foreground_hwnd"] = int(hwnd) if hwnd else 0
        ctx["active_window_title"] = str(getattr(root, "Name", "") or "")[:440]
        ctx["active_app_class"] = str(getattr(root, "ClassName", "") or "")[:240]
        try:
            rect = getattr(root, "BoundingRectangle", None)
            if rect:
                ctx["window_bbox"] = (float(rect.left), float(rect.top), float(rect.width), float(rect.height))
        except Exception:
            ctx["window_bbox"] = None

        if ctx["foreground_hwnd"] and time.perf_counter() < overall_deadline:
            try:
                import ctypes

                wp = ctypes.wintypes.HWND(int(ctx["foreground_hwnd"]))
                pid_dw = ctypes.wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(wp, ctypes.byref(pid_dw))
                pid_v = int(pid_dw.value)
                proc_name = ""
                try:
                    import psutil  # type: ignore import-not-found

                    proc_name = str(psutil.Process(pid_v).name())
                except Exception:
                    proc_name = ""

                ctx["active_process"] = proc_name[:220]
                ctx["process_id"] = pid_v
            except Exception:
                pass

        try:
            import ctypes

            hdc = ctypes.windll.user32.GetDC(0)
            dpi_x = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
            dpi_y = ctypes.windll.gdi32.GetDeviceCaps(hdc, 90)
            ctypes.windll.user32.ReleaseDC(0, hdc)
            ctx["dpi_x"] = dpi_x
            ctx["dpi_y"] = dpi_y
            ctx["monitor_dpi"] = float(dpi_x or 96)
        except Exception:
            ctx["monitor_dpi"] = None
    except Exception:
        ctx["sensor_error"] = "active_window_unreadable"

    return ctx


def collect_uia_live_nodes(*, deadline: float, max_depth: int, max_nodes: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if time.perf_counter() >= deadline:
        return [], "uia_budget_preempt"

    try:
        import uiautomation as auto  # type: ignore import-not-found

        root_ctrl = auto.GetForegroundControl().GetTopLevelControl()
        if hasattr(root_ctrl, "Exists") and not root_ctrl.Exists(0, 0):
            return [], None
    except Exception as exc:
        log.debug("[LOP-SENSOR] UIA unavailable: %s", exc)
        return [], "uia_unavailable"

    count = [0]

    truncated = {"v": False}

    def ctrl_to_dict(ctrl: Any, parent_hint: str) -> Dict[str, Any]:
        ctype = ""
        try:
            ctype = ctrl.ControlTypeName or ""
        except Exception:
            ctype = ""

        name = ""
        try:
            name = ctrl.Name or ""
        except Exception:
            name = ""

        auto_id = ""
        try:
            auto_id = ctrl.AutomationId or ""
        except Exception:
            auto_id = ""

        bbox = None
        try:
            rr = getattr(ctrl, "BoundingRectangle", None)
            if rr:
                bbox = [float(rr.left), float(rr.top), float(rr.width), float(rr.height)]
        except Exception:
            bbox = None

        pwd = False
        try:
            pwd = bool(getattr(ctrl, "IsPassword", False))
        except Exception:
            pwd = False

        scroll_pattern = False
        try:
            scroll_pattern = bool(getattr(ctrl, "GetScrollPattern", lambda: None)())
        except Exception:
            scroll_pattern = False

        def snap_bool(attr: str) -> Optional[bool]:
            getter = getattr(ctrl, attr, None)
            if callable(getter):
                return None
            try:
                return bool(getter)
            except Exception:
                return None

        return {
            "name": name[:480],
            "control_type": ctype,
            "role": ctype.replace("Control", "").lower(),
            "automation_id": auto_id[:240],
            "bbox": bbox,
            "parent_hints": {"parent_control_type": parent_hint[:120]},
            "scroll_pattern": scroll_pattern,
            "modal": bool(ctype == "WindowControl" and name and ("dialog" in name.lower())),
            "enabled": snap_bool("IsEnabled"),
            "focused": snap_bool("HasKeyboardFocus"),
            "offscreen": snap_bool("IsOffscreen"),
            "password_field_hint": pwd,
            "clickable_hint": ctype
            in {
                "ButtonControl",
                "HyperlinkControl",
                "SplitButtonControl",
                "MenuItemControl",
                "TabItemControl",
                "ComboBoxControl",
            },
            "children": [],
        }

    def walk(ctrl: Any, depth: int, parent_ct: str) -> Optional[Dict[str, Any]]:
        if time.perf_counter() >= deadline:
            truncated["v"] = True
            return None
        if depth > max_depth or count[0] >= max_nodes:
            if count[0] >= max_nodes:
                truncated["v"] = True
            return None

        blob = ctrl_to_dict(ctrl, parent_ct)
        count[0] += 1

        if depth >= max_depth or count[0] >= max_nodes:
            return blob

        try:
            children = list(ctrl.GetChildren() or [])[:260]
        except Exception:
            children = []

        child_ctype = blob.get("control_type") or ""
        for child in children:
            if count[0] >= max_nodes or time.perf_counter() >= deadline:
                truncated["v"] = True
                break
            ch_dict = walk(child, depth + 1, child_ctype or parent_ct)
            if ch_dict is not None:
                blob.setdefault("children", []).append(ch_dict)

        return blob

    try:
        top = ctrl_to_dict(root_ctrl, parent_hint="desktop")
        count[0] = 1
        truncated["v"] = False
        try:
            children = list(root_ctrl.GetChildren() or [])[:260]
        except Exception:
            children = []

        ctype_root = top.get("control_type") or ""
        for ch in children:
            if count[0] >= max_nodes or time.perf_counter() >= deadline:
                truncated["v"] = True
                break
            ch_dict = walk(ch, depth=1, parent_ct=ctype_root or "pane")
            if ch_dict:
                top.setdefault("children", []).append(ch_dict)

        msg = "uia_budget_exceeded_truncation" if truncated["v"] else None
        return [top], msg
    except Exception as exc:
        log.debug("[LOP-SENSOR] UIA traversal failed: %s", exc)
        return [], "uia_traversal_fault"


def _dom_generic_js(max_nodes: int) -> str:
    m = int(max_nodes)
    return f"""() => {{
  try {{
    var maxNodes = {m};
    var url = "";
    try {{ url = String(location.href || ""); }} catch (e0) {{}}
    var domain = "";
    try {{ domain = (new URL(url)).hostname || ""; }} catch (e1) {{}}
    var title = "";
    try {{ title = String(document.title || ""); }} catch (e2) {{}}
    var focused = "";
    try {{
      if (document.activeElement) {{
        var el = document.activeElement;
        focused = (el.tagName || "").toLowerCase() + "|" + ((el.getAttribute("role") || "").toLowerCase());
      }}
    }} catch (e3) {{}}

    var nodes = [];
    var sels = [
      "input:not([type='hidden'])",
      "button",
      "a[href]",
      "[role='button']",
      "[role='link']",
      "[role='searchbox']",
      "[role='textbox']",
      "[role='article']",
      "select",
      "textarea"
    ];
    var seen = new Set();
    for (var i = 0; i < sels.length; i++) {{
      try {{
        document.querySelectorAll(sels[i]).forEach(function (el) {{
          if (nodes.length >= maxNodes) return;
          if (seen.has(el)) return;
          seen.add(el);
          nodes.push(el);
        }});
      }} catch (e4) {{}}
    }}

    var out = [];
    for (var j = 0; j < nodes.length; j++) {{
      if (out.length >= maxNodes) break;
      var el = nodes[j];
      var tag = (el.tagName || "").toLowerCase();
      var roleAttr = String(el.getAttribute("role") || "").toLowerCase().slice(0, 64);
      var it = String(el.getAttribute("type") || "").toLowerCase();
      var ph = String(el.getAttribute("placeholder") || "").slice(0, 240);
      var al = String(el.getAttribute("aria-label") || "").slice(0, 260);
      var vt = "";
      try {{ vt = String((el.innerText || "")).trim().slice(0, 260); }} catch (e5) {{}}
      out.push({{
        tag: tag.slice(0, 64),
        role: roleAttr,
        input_type: it.slice(0, 32),
        placeholder: ph,
        aria_label: al,
        visible_text: vt
      }});
    }}

    var scrollHints = [];
    try {{
      document.querySelectorAll("div,section,main,article").forEach(function (el) {{
        if (scrollHints.length >= 8) return;
        try {{
          var oy = window.getComputedStyle(el).overflowY;
          if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 24) {{
            scrollHints.push("scrollable:" + (el.tagName || "").toLowerCase());
          }}
        }} catch (e6) {{}}
      }});
    }} catch (e7) {{}}

    return {{
      url: url.slice(0, 2000),
      domain: domain.slice(0, 253),
      title: title.slice(0, 440),
      focused_element: focused.slice(0, 120),
      scrollable_hints: scrollHints,
      nodes: out,
      dom_source: "generic_page_eval"
    }};
  }} catch (e) {{
    return {{ nodes: [], url: "", domain: "", title: "", error: String(e), dom_source: "error" }};
  }}
}}"""


def _default_dom_collect(deadline: float, max_dom_nodes: int) -> Tuple[Dict[str, Any], Optional[str]]:
    if time.perf_counter() >= deadline:
        return {"nodes": [], "reason": "dom_deadline_early"}, "dom_timeout_preempt"

    page = None
    try:
        from app.skills.tools import web as webmod

        page = getattr(webmod, "_get_page", lambda **__: None)(connect_only=True)
    except Exception:
        page = None

    if page is None:
        return {"nodes": [], "reason": "no_live_page"}, "dom_unavailable"

    js = _dom_generic_js(max_dom_nodes)
    try:
        blob = page.evaluate(js)
    except Exception as exc:
        return {"nodes": [], "reason": str(exc)}, "dom_unavailable"

    if not isinstance(blob, dict):
        return {"nodes": [], "reason": "invalid_blob"}, "dom_unavailable"

    blob.setdefault("nodes", [])
    if blob.get("error"):
        return blob, "dom_unavailable"
    return blob, None


def collect_dom_live_snapshot(
    *,
    deadline: float,
    max_dom_nodes: int,
    collector: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]],
) -> Tuple[Dict[str, Any], Optional[str]]:
    if collector is None:
        return _default_dom_collect(deadline, max_dom_nodes)

    try:
        return collector({"deadline": deadline, "max_dom_nodes": max_dom_nodes}), None
    except Exception as exc:
        return {"nodes": [], "reason": str(exc)}, "dom_unavailable"


def collect_runtime_validator_observations(
    executor_signals: Optional[Dict[str, Any]],
    *,
    inferred: Dict[str, Any],
) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if executor_signals:
        merged.update({str(k): v for k, v in executor_signals.items()})
    for key, val in (inferred or {}).items():
        if key.startswith("__"):
            continue
        if key not in merged and val is not None:
            merged[key] = val

    if merged.get("permission_prompt") is True and merged.get("permission_dialog") is None:
        merged["permission_dialog"] = True

    ls = merged.get("loading_state")
    if ls is True and merged.get("loading") is None:
        merged["loading"] = True

    if merged.get("modal_open") is True and merged.get("modal_trusted") is not True:
        merged.setdefault("modal_unknown", True)

    return merged


def collect_visual_anchor_hints(
    *,
    anchors: Sequence[Dict[str, Any]],
    mission: Mission,
    deadline: float,
    include_visual: bool,
) -> List[Dict[str, Any]]:
    if not include_visual or time.perf_counter() >= deadline:
        return []

    hints: List[Dict[str, Any]] = []
    for a in list(anchors):
        if isinstance(a, dict):
            hints.append(
                {
                    "id": str(a.get("id") or a.get("catalog_id") or "")[:240],
                    "anchor_present": bool(a.get("anchor_present") or a.get("present_hint")),
                    "anchor_missing": bool(a.get("anchor_missing") or a.get("missing_hint")),
                    "approximate_region": a.get("region_hint") or a.get("bbox"),
                    "layout_shift_hint": a.get("layout_shift_hint"),
                },
            )

    plan = getattr(mission, "semantic_execution_plan", None) or {}
    if isinstance(plan, dict):
        fp = plan.get("visual_fingerprint_refs")
        if isinstance(fp, list):
            for entry in fp[:32]:
                if isinstance(entry, dict):
                    hints.append(
                        {
                            "id": str(entry.get("ref") or "")[:240],
                            "anchor_present_hint": bool(entry.get("present")),
                            "approximate_region": entry.get("bbox"),
                            "layout_shift_hint": entry.get("layout_delta"),
                        },
                    )

    seen: set[str] = set()
    uniq: List[Dict[str, Any]] = []
    for h in hints:
        key = json.dumps(h, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            uniq.append(h)
    return uniq[:48]


def collect_ocr_local_if_needed(
    *,
    need_ocr: bool,
    ocr_fn: Optional[Callable[[Dict[str, Any]], List[str]]],
    deadline: float,
    budget_pixels: int,
    max_regions: int,
    hints: Dict[str, Any],
) -> Tuple[List[str], int]:
    if not need_ocr or ocr_fn is None or max_regions <= 0 or budget_pixels <= 0:
        return [], 0
    if time.perf_counter() >= deadline:
        return [], 0

    try:
        payload = dict(hints)
        payload["_ocr_budget_pixels"] = int(budget_pixels)
        payload["_max_regions"] = int(max_regions)

        lines: List[str] = []
        out = list(ocr_fn(payload) or [])
        for s in out:
            txt = str(s).strip()
            if txt:
                lines.append(txt[:960])
            if len(lines) >= max_regions:
                break
        return lines, len(lines)
    except Exception as exc:
        log.debug("[LOP-SENSOR] OCR demandada falló: %s", exc)
        return [], 0


def _estimate_payload_bytes(inp: LivePerceptionInput) -> int:
    try:
        raw = json.dumps(
            {
                "uia_nodes": inp.uia_nodes,
                "dom": inp.dom,
                "runtime_validators": inp.runtime_validators,
                "visual_anchors": inp.visual_anchors,
                "context_hints": inp.context_hints,
                "active_app": inp.active_app,
                "active_window": inp.active_window,
            },
            default=str,
            separators=(",", ":"),
        )
        return len(raw.encode("utf-8"))
    except Exception:
        return 0


def _infer_safety_hints(
    *,
    uia_nodes: Sequence[Dict[str, Any]],
    dom: Dict[str, Any],
    title: str,
) -> Dict[str, Any]:
    hints: Dict[str, Any] = {}
    pay_markers = {"payment", "transfer", "withdraw"}
    auth_markers = {"sign in", "log in", "password", "authenticate"}
    flat: List[Dict[str, Any]] = []

    def uwalk(n: Dict[str, Any]) -> None:
        flat.append(n)
        for ch in n.get("children") or []:
            if isinstance(ch, dict):
                uwalk(ch)

    for root in uia_nodes:
        if isinstance(root, dict):
            uwalk(root)

    join = title.lower()
    for node in flat:
        name = str(node.get("name") or "").lower()
        role = str(node.get("role") or "").lower()
        join += " " + name + " " + role

        if node.get("password_field_hint") or "password" in role or "password" in name:
            hints["auth_screen"] = True
            hints["password_screen"] = True

    dom_nodes = []
    if isinstance(dom, dict):
        raw = dom.get("nodes")
        if isinstance(raw, list):
            dom_nodes = [d for d in raw if isinstance(d, dict)]

    for d in dom_nodes:
        it = str(d.get("input_type") or "").lower()
        blob = " ".join(
            str(d.get(k) or "").lower() for k in ("aria_label", "placeholder", "visible_text")
        )
        join += " " + blob
        if it == "password":
            hints["auth_screen"] = True
            hints["password_screen"] = True

    low = join.lower()
    for m in pay_markers:
        if m in low:
            hints["payment_context"] = True
            hints["transfer_context"] = "transfer" in low
    for m in auth_markers:
        if m in low:
            hints["auth_screen"] = True

    if ("are you sure" in low and "delete" in low) or "cannot be undone" in low:
        hints["destructive_confirmation_context"] = True

    launcher_classes = ("corewindow", "launcher")
    for tok in launcher_classes:
        if tok in title.lower():
            hints["launcher_like"] = True

    return hints


def _ambiguous_blank_button_count(uia_nodes: Sequence[Dict[str, Any]]) -> int:
    flat: List[Dict[str, Any]] = []

    def uwalk(n: Dict[str, Any]) -> None:
        flat.append(n)
        for ch in n.get("children") or []:
            if isinstance(ch, dict):
                uwalk(ch)

    for root in uia_nodes:
        if isinstance(root, dict):
            uwalk(root)

    n_blank = 0
    for node in flat:
        role = str(node.get("role") or "").lower()
        name = str(node.get("name") or "").strip()
        if "button" in role and not name:
            n_blank += 1
    return n_blank


def _shrink_live_input_for_bytes(inp: LivePerceptionInput, max_bytes: int) -> LivePerceptionInput:
    if max_bytes <= 0:
        return inp
    cur = _estimate_payload_bytes(inp)
    if cur <= max_bytes:
        return inp

    dom = dict(inp.dom or {})
    nodes = list(dom.get("nodes") or []) if isinstance(dom.get("nodes"), list) else []

    while nodes and _estimate_payload_bytes(inp) > max_bytes:
        nodes.pop()
        dom["nodes"] = nodes
        inp = replace(inp, dom=dom)

    uia = list(inp.uia_nodes)
    while uia and _estimate_payload_bytes(inp) > max_bytes:
        uia.pop()
        inp = replace(inp, uia_nodes=uia)

    return inp


def _settings_lop_live_enabled(settings_obj: Optional[Any]) -> bool:
    try:
        if settings_obj is None:
            from app.core.config import get_settings

            settings_obj = get_settings()
        return bool(getattr(settings_obj, "LOP_SHADOW_ENABLED", True))
    except Exception:
        return False


def merge_prior_runner_shadow_audits(existing: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extrae muestras previas del SmartRunner desde ``lop_sensor_audit``."""

    if not isinstance(existing, dict):
        return []
    tail = existing.get("runner_shadow_steps")
    return list(tail) if isinstance(tail, list) else []


def runner_shadow_bridge_config() -> LopSensorBridgeConfig:
    budget = LopSensorBudget(
        total_timeout_ms=90,
        uia_timeout_ms=40,
        dom_timeout_ms=35,
        ocr_timeout_ms=30,
        max_uia_nodes=80,
        max_uia_depth=4,
        max_dom_nodes=48,
        max_ocr_regions=1,
        max_snapshot_bytes=96_000,
    )
    return LopSensorBridgeConfig(budget=budget, include_uia=True, include_dom=True)


def _append_runner_shadow_entry(
    mission: Mission,
    *,
    phase: str,
    step_obj: Any,
    sample: Dict[str, Any],
) -> None:
    prior = merge_prior_runner_shadow_audits(getattr(mission, "lop_sensor_audit", None))

    entry = {
        "phase": phase,
        "step_index": getattr(step_obj, "index", None),
        "step_id": getattr(step_obj, "step_id", ""),
        "timestamp": time.time(),
        "sample": sample,
    }
    prior.append(entry)
    prior = prior[-30:]

    base = dict(getattr(mission, "lop_sensor_audit", None) or {})
    base["runner_shadow_steps"] = prior

    mission.lop_sensor_audit = base


def build_lop_sensor_audit(
    *,
    sensors_used: Sequence[str],
    sensors_failed: Sequence[str],
    nodes_collected: int,
    dom_nodes_collected: int,
    ocr_regions_used: int,
    confidence: float,
    blockers_detected: int,
    perception_gaps: Sequence[str],
    health_status: LopSensorHealthStatus,
    budget_used_ms: float,
    snapshot_bytes_estimate: int,
    runner_shadow_tail: Sequence[Dict[str, Any]],
) -> LopSensorAudit:
    gaps = list(dict.fromkeys(perception_gaps))
    tail = list(runner_shadow_tail)[-40:]
    return LopSensorAudit(
        sensors_used=list(dict.fromkeys(sensors_used)),
        sensors_failed=list(dict.fromkeys(sensors_failed)),
        budget_used_ms=round(max(0.0, budget_used_ms), 3),
        nodes_collected=max(0, int(nodes_collected)),
        dom_nodes_collected=max(0, int(dom_nodes_collected)),
        ocr_regions_used=max(0, int(ocr_regions_used)),
        confidence=max(0.0, min(1.0, float(confidence))),
        blockers_detected=max(0, int(blockers_detected)),
        perception_gaps=gaps,
        health_status=health_status,
        snapshot_bytes_estimate=max(0, int(snapshot_bytes_estimate)),
        runner_shadow_steps=tail,
    )


def build_live_perception_input(
    mission: Mission,
    config: LopSensorBridgeConfig,
    *,
    executor_signals: Optional[Dict[str, Any]] = None,
    visual_anchor_signals: Sequence[Dict[str, Any]] = (),
    uia_sampler: Optional[Callable[..., Tuple[List[Dict[str, Any]], Optional[str]]]] = None,
    dom_sampler: Optional[Callable[..., Tuple[Dict[str, Any], Optional[str]]]] = None,
    ocr_regions_fn: Optional[Callable[[Dict[str, Any]], List[str]]] = None,
    extra_hints: Optional[Dict[str, Any]] = None,
) -> Tuple[LivePerceptionInput, LopSensorCaptureResult, LopSensorHealth]:
    """Ensambla entrada LOP sólo observando (injectable/testeable por samplers OCR)."""

    bud = config.budget
    t0 = time.perf_counter()
    deadline = t0 + max(bud.total_timeout_ms / 1000.0, 1e-4)

    cap = LopSensorCaptureResult(
        ok=True,
        budget_exceeded_flags=[],
        partial_safe=True,
        health=LopSensorHealth(status=LopSensorHealthStatus.HEALTHY, reasons=[]),
        started_at_perf=t0,
    )

    sensors_used: List[str] = ["window_context"]
    sensors_failed: List[str] = []
    notes: List[str] = []

    win = capture_active_window_context(
        overall_deadline=deadline,
        uia_budget_ms=min(bud.uia_timeout_ms, bud.total_timeout_ms),
    )
    if win.get("sensor_error"):
        notes.append(str(win["sensor_error"]))

    uia_roots: List[Dict[str, Any]] = []
    if config.include_uia:
        sampler = uia_sampler or collect_uia_live_nodes
        u_dead = min(deadline, time.perf_counter() + bud.uia_timeout_ms / 1000.0)
        uia_roots, u_msg = sampler(deadline=u_dead, max_depth=bud.max_uia_depth, max_nodes=bud.max_uia_nodes)
        sensors_used.append("uia")
        if u_msg:
            notes.append(f"uia_diag:{u_msg}")
            sensors_failed.append("uia_degraded_stub")
            cap.budget_exceeded_flags.append(u_msg)
            if "budget" in u_msg or "preempt" in u_msg:
                cap.partial_safe = False
        if not uia_roots:
            sensors_failed.append("uia_empty_tree_stub")
            cap.partial_safe = False

    dom: Dict[str, Any]
    outer_dom_msg: Optional[str] = None
    if config.include_dom:
        dom_deadline = min(deadline, time.perf_counter() + bud.dom_timeout_ms / 1000.0)
        if dom_sampler is None:
            dom, outer_dom_msg = _default_dom_collect(dom_deadline, bud.max_dom_nodes)
        else:
            dom_try, outer_dom_msg = dom_sampler(dom_deadline, bud.max_dom_nodes)
            dom = dom_try if isinstance(dom_try, dict) else {"nodes": []}

        sensors_used.append("dom")
        dom.setdefault("nodes", [])
        dom.setdefault("url", "")
        dom.setdefault("title", "")
        dom.setdefault("domain", "")
        if outer_dom_msg:
            sensors_failed.append("dom_stub_fail")
            notes.append(f"dom:{outer_dom_msg}")
            cap.partial_safe = cap.partial_safe and bool(uia_roots)
    else:
        dom = {"nodes": [], "url": "", "title": "", "domain": "", "reason": "dom_disabled_policy"}

    dom_nodes_raw = dom.get("nodes") if isinstance(dom.get("nodes"), list) else []
    dom_cnt = sum(1 for z in dom_nodes_raw if isinstance(z, dict))

    blank_buttons = _ambiguous_blank_button_count(uia_roots)

    inferred: Dict[str, Any] = {}

    if blank_buttons >= 10:
        inferred["ambiguous_targets_stub"] = blank_buttons
        notes.append(f"multi_blank_buttons_stub={blank_buttons}")

    safety = _infer_safety_hints(uia_nodes=uia_roots, dom=dom, title=str(win.get("active_window_title") or ""))
    if safety.get("destructive_confirmation_context"):
        inferred.setdefault("destructive_confirm_unknown", True)

    validators_runtime = collect_runtime_validator_observations(executor_signals or {}, inferred=inferred)

    anchors_blob: List[Dict[str, Any]] = []
    if config.include_visual_anchor_hints:
        anchors_blob = collect_visual_anchor_hints(
            anchors=visual_anchor_signals,
            mission=mission,
            deadline=deadline,
            include_visual=True,
        )

    flat_uia = _flatten_uia(list(uia_roots), max_nodes=max(bud.max_uia_nodes * 4, bud.max_uia_nodes))
    text_budget, _snippets = _collect_plain_text_budget(flat_uia, dom)

    want_ocr = bool(
        config.allow_ocr_on_demand
        and ocr_regions_fn is not None
        and bud.max_ocr_regions > 0
        and (
            text_budget < bud.text_sufficiency_chars
            or bool(validators_runtime.get("prefer_ocr_probe"))
            or bool(validators_runtime.get("ocr_probe_requested"))
            or (
                getattr(mission, "goor_shadow_mode", False)
                and getattr(mission, "operational_goal_snapshots", [])
                and text_budget < bud.text_sufficiency_chars + 32
            )
        ),
    )

    ocr_regions_used = 0
    if want_ocr:
        hint_payload = dict(safety)
        hint_payload["_window_bbox_stub"] = win.get("window_bbox")
        hint_payload["_dom_stub"] = {
            "url": dom.get("url"),
            "focused": dom.get("focused_element"),
            "samples_tail": dom_nodes_raw[:14],
        }
        dl_ocr = min(deadline, time.perf_counter() + bud.ocr_timeout_ms / 1000.0)

        chunks, regions = collect_ocr_local_if_needed(
            need_ocr=True,
            ocr_fn=ocr_regions_fn,
            deadline=dl_ocr,
            budget_pixels=bud.max_ocr_pixels,
            max_regions=bud.max_ocr_regions,
            hints=hint_payload,
        )

        text_budget += sum(len(str(c).strip()) for c in chunks)
        ocr_regions_used = regions

        sensors_used.extend(["ocr_on_demand"] if regions > 0 else [])
        if not regions:

            sensors_failed.append("ocr_unavailable_budget")
            cap.partial_safe = False

    hints_ctx: Dict[str, Any] = {**{str(k): v for k, v in win.items()}, **safety}

    hints_ctx["ambiguous_blank_buttons"] = blank_buttons

    hints_ctx.setdefault("lop_sensor_budget_stub", []).extend(cap.budget_exceeded_flags[-6:])

    if extra_hints:
        hints_ctx.update({str(k): v for k, v in extra_hints.items()})

    inp = LivePerceptionInput(
        uia_nodes=uia_roots,
        dom=dict(dom),

        runtime_validators=validators_runtime,
        visual_anchors=anchors_blob,
        context_hints=hints_ctx,
        active_app=str(win.get("active_process") or win.get("active_app_class") or "")[:220],

        active_window=str(dom.get("title") or win.get("active_window_title") or "")[:520],

    )


    before_bytes = _estimate_payload_bytes(inp)
    inp2 = _shrink_live_input_for_bytes(inp, bud.max_snapshot_bytes)


    after_bytes = _estimate_payload_bytes(inp2)
    if after_bytes < before_bytes and before_bytes > bud.max_snapshot_bytes:
        cap.budget_exceeded_flags.append("max_snapshot_bytes_shrink_stub")
        cap.partial_safe = False
        notes.append("lop_sensor_budget_snapshot_trim_stub")



    structural = bool(_flatten_uia(inp2.uia_nodes)) or dom_cnt > 0
    tb2, _junk2 = _collect_plain_text_budget(_flatten_uia(inp2.uia_nodes), inp2.dom)
    textual = bool(tb2 >= 12 or ocr_regions_used > 0)

    reasons = list(dict.fromkeys(notes + cap.budget_exceeded_flags))


    severity = LopSensorHealthStatus.HEALTHY
    if not structural:
        severity = LopSensorHealthStatus.DEGRADED
        reasons.append("no_structural_signals")
    if not textual:
        reasons.append("no_text_signals_stub")
        if severity == LopSensorHealthStatus.HEALTHY:
            severity = LopSensorHealthStatus.DEGRADED
    if not structural and not textual:
        severity = LopSensorHealthStatus.UNAVAILABLE
    if sensors_failed.count("dom_stub_fail") and not dom_cnt and not structural:
        reasons.append("dom_unreadable")
        severity = LopSensorHealthStatus.DEGRADED

    cap.health = LopSensorHealth(status=severity, reasons=list(dict.fromkeys(reasons))[-16:])
    cap.finished_at_perf = time.perf_counter()


    flat_nodes = sum(1 for _ in inp2.uia_nodes)



    setattr(cap, "_debug_nodes_stub", flat_nodes)
    setattr(cap, "_dom_cnt_stub", dom_cnt)
    setattr(cap, "_sensor_telemetry_stub", (len(sensors_used), len(sensors_failed)))

    return inp2, cap, cap.health


def refresh_lop_from_live_sensors(
    mission: Mission,
    settings: Optional[Any] = None,
    *,
    graph: Optional[OperationalRuntimeGraph] = None,
    config: Optional[LopSensorBridgeConfig] = None,
    executor_signals: Optional[Dict[str, Any]] = None,
    ocr_light_callable_factory: Optional[
        Callable[[Mission], Optional[Callable[[], List[str]]]]
    ] = None,
    perception_input_override: Optional[LivePerceptionInput] = None,
    uia_sampler: Optional[Callable[..., Tuple[List[Dict[str, Any]], Optional[str]]]] = None,
    dom_sampler: Optional[Callable[..., Tuple[Dict[str, Any], Optional[str]]]] = None,
) -> LiveOperationalPerceptionSnapshot:
    """Persiste LOP con sensores vivos (readonly; no muta SEP ni ejecutor)."""
    from app.services.runtime import live_operational_perception as lopmod

    cfg = config or LopSensorBridgeConfig()

    if not getattr(mission, "lop_shadow_mode", False):
        return lopmod.refresh_lop_shadow(mission, settings, graph=graph)

    if not getattr(mission, "lop_live_feed_enabled", False):
        return lopmod.refresh_lop_shadow(mission, settings, graph=graph)

    if not lopmod._settings_lop_enabled(settings):
        miss = LiveOperationalPerceptionSnapshot(
            perception_gaps=["LOP_SHADOW_DISABLED"],
            recommended_runtime_action=LiveOperationalRuntimeAction.WAIT,
        )
        mission.live_operational_perception_audit = {"ok": False, "reason": "LOP_SHADOW_DISABLED"}
        mission.lop_last_snapshot = miss.model_dump(mode="json")
        return miss

    ocr_fn: Optional[Callable[[], List[str]]] = None
    if ocr_light_callable_factory is not None:
        try:
            ocr_fn = ocr_light_callable_factory(mission)
        except Exception:
            ocr_fn = None

    if perception_input_override is None:
        perception_inp, bridge_cap, _hp = build_live_perception_input(
            mission,
            cfg,
            executor_signals=executor_signals,
            uia_sampler=uia_sampler,
            dom_sampler=dom_sampler,
            ocr_regions_fn=None,
        )
    else:
        perception_inp = perception_input_override
        bridge_cap = LopSensorCaptureResult(
            ok=True,
            budget_exceeded_flags=[],
            partial_safe=True,
            health=LopSensorHealth(status=LopSensorHealthStatus.HEALTHY, reasons=[]),
            started_at_perf=time.perf_counter(),
            finished_at_perf=time.perf_counter(),
        )

    snap = capture_live_operational_snapshot(
        mission,
        perception_input=perception_inp,
        graph=graph,
        ocr_light=ocr_fn,
        text_sufficiency_threshold=float(cfg.budget.text_sufficiency_chars),
        max_elapsed_ms=float(cfg.budget.total_timeout_ms + 380.0),
    )

    for bf in getattr(bridge_cap, "budget_exceeded_flags", [])[:12]:
        snap.perception_gaps.append(f"lop_sensor_budget:{bf}")

    blocks = list(getattr(snap, "live_blockers", None) or [])
    audit = build_lop_audit(
        snapshot=snap,
        elapsed_ms=float(snap.raw_sensor_stats.get("elapsed_ms") or 0.0),
        ocr_invoked=bool(snap.raw_sensor_stats.get("ocr_invoked")),
        sensor_sources=list(
            dict.fromkeys(
                [
                    "uia",
                    "dom",
                    "validators",
                    "operational_context",
                    "lop_sensor_bridge_v2",
                    *([] if not snap.raw_sensor_stats.get("ocr_invoked") else ["ocr_light"]),
                ]
            )
        ),
    )
    audit["blocker_count"] = len(blocks)
    audit["ok"] = True
    audit["lop_sensor_budget_tail"] = list(getattr(bridge_cap, "budget_exceeded_flags", [])[:12])
    mission.live_operational_perception_audit = audit
    mission.lop_last_snapshot = snap.model_dump(mode="json")

    dom_ns = perception_inp.dom.get("nodes") if isinstance(perception_inp.dom.get("nodes"), list) else []
    dom_cnt = sum(1 for z in dom_ns if isinstance(z, dict))
    uia_ct = sum(1 for _ in perception_inp.uia_nodes)
    elapsed_bridge = max(
        0.0,
        (getattr(bridge_cap, "finished_at_perf", time.perf_counter()) - getattr(bridge_cap, "started_at_perf", time.perf_counter()))
        * 1000,
    )

    tail_rs = merge_prior_runner_shadow_audits(getattr(mission, "lop_sensor_audit", None))
    sensors_used_audit = sorted(
        dict.fromkeys(
            getattr(bridge_cap, "budget_exceeded_flags", [])[-3:]
            + [
                ("uia" if uia_ct else ""),
                ("dom_layer" if dom_cnt else ""),
            ]
        ),
    )
    sensors_used_audit = [s for s in sensors_used_audit if s]

    health_model = getattr(bridge_cap, "health", LopSensorHealth(status=LopSensorHealthStatus.DEGRADED))
    setattr(mission, "lop_last_sensor_health", health_model.model_dump(mode="json"))

    setattr(
        mission,
        "lop_sensor_audit",
        build_lop_sensor_audit(
            sensors_used=sensors_used_audit or ["lop_sensor_refresh"],
            sensors_failed=["dom_unreachable"] if not dom_cnt and not uia_ct else [],
            nodes_collected=uia_ct,
            dom_nodes_collected=dom_cnt,
            ocr_regions_used=int(bool(snap.raw_sensor_stats.get("ocr_invoked"))),
            confidence=float(snap.confidence),
            blockers_detected=len(blocks),
            perception_gaps=list(snap.perception_gaps or []),
            health_status=getattr(health_model, "status", LopSensorHealthStatus.DEGRADED),
            budget_used_ms=float(snap.raw_sensor_stats.get("elapsed_ms") or elapsed_bridge),
            snapshot_bytes_estimate=_estimate_payload_bytes(perception_inp),
            runner_shadow_tail=tail_rs,
        ).model_dump(mode="json"),
    )

    try:
        from app.services.runtime.operational_continuity_runtime import (
            merge_live_operational_perception_into_continuity_shadow,
        )

        merge_live_operational_perception_into_continuity_shadow(mission)
    except Exception:
        pass

    return snap


def augment_smart_runner_callbacks_for_lop_shadow(
    mission: Mission,
    settings: Any,
    on_step_started: Optional[Callable[[Any], None]],
    on_step_done: Optional[Callable[[Any], None]],
    *,
    uia_sampler: Optional[Callable[..., Tuple[List[Dict[str, Any]], Optional[str]]]] = None,
    dom_sampler: Optional[Callable[..., Tuple[Dict[str, Any], Optional[str]]]] = None,
    executor_signals_factory: Optional[Callable[[Mission, str, Any], Dict[str, Any]]] = None,
) -> Tuple[Optional[Callable[[Any], None]], Optional[Callable[[Any], None]]]:
    runner_on = bool(getattr(settings, "LOP_RUNNER_SHADOW_ENABLED", True))
    if not runner_on or not (
        getattr(mission, "lop_live_feed_enabled", False) and getattr(mission, "lop_shadow_mode", False)
    ):
        return on_step_started, on_step_done

    cfg = runner_shadow_bridge_config()

    def _signals(ph: str, marker: Any) -> Dict[str, Any]:
        if executor_signals_factory is None:
            return {}
        try:
            return dict(executor_signals_factory(mission, ph, marker) or {})
        except Exception:
            return {}

    def _capture(ph: str, marker: Any) -> Dict[str, Any]:
        try:
            pi, caps, hlth = build_live_perception_input(
                mission,
                cfg,
                executor_signals=_signals(ph, marker),
                uia_sampler=uia_sampler,
                dom_sampler=dom_sampler,
            )
            flats = _flatten_uia(pi.uia_nodes)
            dns = pi.dom.get("nodes") if isinstance(pi.dom.get("nodes"), list) else []
            return {
                "phase_stub": ph,
                "budget_flags": list(getattr(caps, "budget_exceeded_flags", [])[:8]),
                "health_status": getattr(hlth.status, "value", str(getattr(hlth, "status", ""))),
                "n_dom": sum(1 for z in dns if isinstance(z, dict)),
                "n_uia": len(flats),
                "validators_tail": sorted(list(pi.runtime_validators.keys()))[-6:],
                "partial_safe_stub": getattr(caps, "partial_safe", True),
                "url_stub": bool(str(pi.dom.get("url") or "").strip()),
            }

        except Exception as exc:

            return {"phase_stub": ph, "runner_shadow_error_stub": str(exc)}

    def started_hook(evt: Any) -> None:
        _append_runner_shadow_entry(
            mission,
            phase="runner_before_stub",
            step_obj=evt,
            sample=_capture("before_step", evt),
        )
        if on_step_started is not None:

            try:

                on_step_started(evt)

            except Exception:

                raise

    def done_hook(oc: Any) -> None:


        _append_runner_shadow_entry(


            mission,


            phase="runner_after_stub",


            step_obj=oc,


            sample=_capture("after_step", oc),

        )


        if on_step_done is not None:

            try:


                on_step_done(oc)



            except Exception:


                raise

    return started_hook, done_hook
