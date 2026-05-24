"""
Nevlan — Instrumentación de performance del Player.
Thread-safe. Emite eventos al bus opcionalmente.
"""
from __future__ import annotations

import time
import threading
from contextlib import contextmanager
from typing import Dict, List, Optional

from app.core.logger import log


class StepTiming:
    __slots__ = ("step_index", "strategy", "resolve_ms", "action_ms",
                 "validate_ms", "total_ms", "attempts", "resolver",
                 "visual_match_ms", "premium_strategy", "premium_click_meta")

    def __init__(self, step_index: int, strategy: str):
        self.step_index = step_index
        self.strategy = strategy
        self.resolve_ms = 0.0
        self.action_ms = 0.0
        self.validate_ms = 0.0
        self.total_ms = 0.0
        self.attempts = 1
        self.visual_match_ms = 0.0
        self.premium_strategy: Optional[str] = None
        self.premium_click_meta: Optional[Dict] = None
        # 'uia' | 'icon_validated' | 'vision' | 'rel_window' | 'coords' | 'window'
        self.resolver: Optional[str] = None

    def to_dict(self) -> Dict:
        out = {
            "step_index": self.step_index,
            "strategy": self.strategy,
            "resolve_ms": round(self.resolve_ms, 1),
            "action_ms": round(self.action_ms, 1),
            "validate_ms": round(self.validate_ms, 1),
            "visual_match_ms": round(self.visual_match_ms, 1),
            "premium_strategy": self.premium_strategy or "",
            "total_ms": round(self.total_ms, 1),
            "attempts": self.attempts,
            "resolver": self.resolver,
        }
        if self.premium_click_meta:
            pm = dict(self.premium_click_meta)
            pm.pop("strategy_used", None)
            # serializable sólo valores simples para logs externos
            for k in list(pm.keys()):
                if callable(pm[k]):  # type: ignore[arg-type]
                    pm.pop(k, None)
            out["premium_click_meta"] = pm
        else:
            out["premium_click_meta"] = {}
        return out


class PerfTracker:
    """Mide tiempos por paso. Mantener liviano para no añadir overhead."""

    def __init__(self):
        self._lock = threading.Lock()
        self._steps: List[StepTiming] = []

    def start_step(self, index: int, strategy: str) -> StepTiming:
        t = StepTiming(index, strategy)
        with self._lock:
            self._steps.append(t)
        return t

    def snapshot(self) -> List[Dict]:
        with self._lock:
            return [s.to_dict() for s in self._steps]

    def slowest(self, n: int = 3) -> List[Dict]:
        with self._lock:
            data = sorted(self._steps, key=lambda s: s.total_ms, reverse=True)
        return [d.to_dict() for d in data[:n]]

    def totals(self) -> Dict:
        with self._lock:
            n = len(self._steps)
            total = sum(s.total_ms for s in self._steps)
            resolve = sum(s.resolve_ms for s in self._steps)
            action = sum(s.action_ms for s in self._steps)
            validate = sum(s.validate_ms for s in self._steps)
            by_resolver: Dict[str, Dict[str, float]] = {}
            for s in self._steps:
                key = s.resolver or "unknown"
                b = by_resolver.setdefault(key, {"count": 0, "total_ms": 0.0})
                b["count"] += 1
                b["total_ms"] += s.total_ms
        return {
            "steps": n,
            "total_ms": round(total, 1),
            "avg_ms": round(total / n, 1) if n else 0,
            "resolve_total_ms": round(resolve, 1),
            "action_total_ms": round(action, 1),
            "validate_total_ms": round(validate, 1),
            "by_resolver": {
                k: {"count": int(v["count"]), "total_ms": round(v["total_ms"], 1)}
                for k, v in by_resolver.items()
            },
        }


@contextmanager
def measure():
    t0 = time.perf_counter()
    yield lambda: (time.perf_counter() - t0) * 1000.0
