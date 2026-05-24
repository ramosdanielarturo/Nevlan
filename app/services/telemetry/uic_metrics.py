"""Telemetría UIC agregada (campo) — sin texto sensible ni screenshots."""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _redact_private(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 3:
        return "***"
    return val[:2] + "***" + val[-1:]


@dataclass
class UICMetrics:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    capability_used: Counter = field(default_factory=Counter)
    strategy_used: Counter = field(default_factory=Counter)
    blocker_type: Counter = field(default_factory=Counter)
    events: List[Dict[str, Any]] = field(default_factory=list)

    # Histogramas simples (ms)
    target_resolution_ms: List[int] = field(default_factory=list)
    execution_ms: List[int] = field(default_factory=list)

    # Contadores pedidos por PRD
    self_heal_attempted: int = 0
    self_heal_success: int = 0
    rerecord_started: int = 0
    rerecord_success: int = 0
    rerecord_cancelled: int = 0
    repair_success: int = 0
    fallback_to_legacy: int = 0
    coords_emergency_used: int = 0
    confirmation_requested: int = 0
    private_mode_redactions: int = 0

    def record_capability_used(self, capability_id: str, *, private_mode: bool = False) -> None:
        cid = _redact_private(str(capability_id)) if private_mode else str(capability_id)
        with self._lock:
            self.capability_used[cid] += 1
            if private_mode:
                self.private_mode_redactions += 1

    def record_strategy_used(self, strategy: str, *, private_mode: bool = False) -> None:
        s = _redact_private(str(strategy)) if private_mode else str(strategy)
        with self._lock:
            self.strategy_used[s] += 1

    def record_timing_bucket(self, field: str, ms: int) -> None:
        with self._lock:
            if field == "target_resolution":
                self.target_resolution_ms.append(int(ms))
            elif field == "execution":
                self.execution_ms.append(int(ms))

    def record_rerecord(self, event: str) -> None:
        with self._lock:
            if event == "started":
                self.rerecord_started += 1
            elif event == "success":
                self.rerecord_success += 1
            elif event == "cancelled":
                self.rerecord_cancelled += 1

    def record_event(self, name: str, payload: Optional[Dict[str, Any]] = None, *, private_mode: bool = False) -> None:
        pl = dict(payload or {})
        if private_mode:
            pl = {k: "***" for k in pl}
            with self._lock:
                self.private_mode_redactions += 1
        if "screenshot" in pl or "screenshot_path" in pl:
            pl.pop("screenshot", None)
            pl.pop("screenshot_path", None)
        with self._lock:
            self.events.append({"name": name, "payload": pl, "ts": time.time()})

    def legacy_fallback_rate(self) -> float:
        tot = sum(self.capability_used.values()) or 1
        return round(self.fallback_to_legacy / float(tot), 6)

    def coords_emergency_rate(self) -> float:
        tot = sum(self.strategy_used.values()) or 1
        return round(self.coords_emergency_used / float(tot), 6)

    def rerecord_success_rate(self) -> float:
        if self.rerecord_started <= 0:
            return 0.0
        return round(self.rerecord_success / float(self.rerecord_started), 6)

    def self_heal_success_rate(self) -> float:
        if self.self_heal_attempted <= 0:
            return 0.0
        return round(self.self_heal_success / float(self.self_heal_attempted), 6)

    def average_resolution_latency_ms(self) -> float:
        with self._lock:
            arr = list(self.target_resolution_ms)
        if not arr:
            return 0.0
        return round(sum(arr) / len(arr), 3)

    def export_summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "capability_used": dict(self.capability_used),
                "strategy_used": dict(self.strategy_used),
                "legacy_fallback_rate": self.legacy_fallback_rate(),
                "coords_emergency_rate": self.coords_emergency_rate(),
                "rerecord_success_rate": self.rerecord_success_rate(),
                "self_heal_success_rate": self.self_heal_success_rate(),
                "average_resolution_latency_ms": self.average_resolution_latency_ms(),
                "private_mode_redactions": self.private_mode_redactions,
            }


uic_metrics = UICMetrics()

__all__ = ["UICMetrics", "uic_metrics"]
