"""Telemetría de producción ligera — eventos JSON-friendly en misión.

Sin backend obligatorio: ideal para export posterior / dashboards locales."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

_MAX_EVENTS = 96


def _tail(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return payload
    return {"version": 1, "events": [], "run_ids": []}


def record_runtime_event(
    mission: Any,
    *,
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
    severity: str = "info",
) -> Dict[str, Any]:
    """Añade evento a ``mission.runtime_production_telemetry`` (cap FIFO)."""
    ev = {
        "id": str(uuid.uuid4())[:12],
        "ts": time.time(),
        "kind": str(kind),
        "severity": str(severity),
        "payload": dict(payload or {}),
    }
    tail = _tail(getattr(mission, "runtime_production_telemetry", None))
    events: List[Dict[str, Any]] = list(tail.get("events") or [])
    events.append(ev)
    if len(events) > _MAX_EVENTS:
        events = events[-_MAX_EVENTS:]
    tail["events"] = events
    try:
        mission.runtime_production_telemetry = tail
    except Exception:
        pass
    return ev


def telemetry_snapshot(mission: Any) -> Dict[str, Any]:
    return dict(_tail(getattr(mission, "runtime_production_telemetry", None)))


def summarize_telemetry(tail: Dict[str, Any]) -> Dict[str, Any]:
    events = list(tail.get("events") or [])
    kinds: Dict[str, int] = {}
    unsafe = 0
    repairs = 0
    arl = 0
    for e in events:
        k = str(e.get("kind") or "")
        kinds[k] = kinds.get(k, 0) + 1
        if k.startswith("unsafe"):
            unsafe += 1
        if "repair" in k:
            repairs += 1
        if k.startswith("arl"):
            arl += 1
    return {
        "event_count": len(events),
        "kinds": kinds,
        "unsafe_signals": unsafe,
        "repair_signals": repairs,
        "arl_signals": arl,
    }


__all__ = [
    "record_runtime_event",
    "telemetry_snapshot",
    "summarize_telemetry",
]
