"""Adaptive Runtime Layer — métricas en memoria (sin I/O externo)."""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

__all__ = ["ArlMetrics", "get_arl_metrics", "reset_arl_metrics"]


class ArlMetrics:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.adaptations_attempted = 0
        self.adaptations_successful = 0
        self.fallbacks_avoided = 0
        self.continuity_preserved = 0
        self.validator_adaptations = 0
        self.relocation_attempted = 0
        self.relocation_successful = 0
        self.fallbacks_triggered = 0
        self.human_request_hints = 0

    def record_adaptation(self, *, success: bool) -> None:
        with self._lock:
            self.adaptations_attempted += 1
            if success:
                self.adaptations_successful += 1

    def record_fallback_avoidance(self) -> None:
        with self._lock:
            self.fallbacks_avoided += 1

    def record_continuity_preserved(self) -> None:
        with self._lock:
            self.continuity_preserved += 1

    def record_validator_adaptation(self) -> None:
        with self._lock:
            self.validator_adaptations += 1

    def record_relocation_attempt(self, *, success: bool) -> None:
        with self._lock:
            self.relocation_attempted += 1
            if success:
                self.relocation_successful += 1

    def record_fallback(self) -> None:
        with self._lock:
            self.fallbacks_triggered += 1

    def record_human_request_hint(self) -> None:
        with self._lock:
            self.human_request_hints += 1

    def relocation_success_rate(self) -> Optional[float]:
        with self._lock:
            if self.relocation_attempted <= 0:
                return None
            return round(self.relocation_successful / self.relocation_attempted, 6)

    def dump_to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "adaptations_attempted": self.adaptations_attempted,
                "adaptations_successful": self.adaptations_successful,
                "fallbacks_avoided": self.fallbacks_avoided,
                "continuity_preserved": self.continuity_preserved,
                "validator_adaptations": self.validator_adaptations,
                "relocation_attempted": self.relocation_attempted,
                "relocation_successful": self.relocation_successful,
                "relocation_success_rate": self.relocation_success_rate(),
                "fallbacks_triggered": self.fallbacks_triggered,
                "human_request_hints": self.human_request_hints,
            }

    def merge_into_summary(self, summary: Dict[str, Any]) -> None:
        d = self.dump_to_dict()
        summary["arl_metric_snapshot"] = d

    def reset(self) -> None:
        with self._lock:
            self.adaptations_attempted = 0
            self.adaptations_successful = 0
            self.fallbacks_avoided = 0
            self.continuity_preserved = 0
            self.validator_adaptations = 0
            self.relocation_attempted = 0
            self.relocation_successful = 0
            self.fallbacks_triggered = 0
            self.human_request_hints = 0


_singleton: Optional[ArlMetrics] = None
_singleton_lock = threading.RLock()


def get_arl_metrics() -> ArlMetrics:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ArlMetrics()
        return _singleton


def reset_arl_metrics() -> None:
    get_arl_metrics().reset()
