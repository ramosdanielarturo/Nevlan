"""Métricas en memoria del COB Execution Pilot (Fase 1).

Thread-safe, sin I/O por defecto. Los tests pueden llamar ``reset()`` para aislar.
"""
from __future__ import annotations

import statistics
import threading
from typing import Any, Dict, List, Optional

__all__ = [
    "CobPilotMetrics",
    "get_cob_pilot_metrics",
    "reset_cob_pilot_metrics",
]


class CobPilotMetrics:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.pilot_attempts: int = 0
        self.pilot_success: int = 0
        self.pilot_fallbacks: int = 0
        self.validator_failures: int = 0
        self._delta_samples_ms: List[float] = []
        self.pilot_primitive_ops: int = 0

    def record_attempt(self) -> None:
        with self._lock:
            self.pilot_attempts += 1

    def record_success(self) -> None:
        with self._lock:
            self.pilot_success += 1

    def record_fallback(self) -> None:
        with self._lock:
            self.pilot_fallbacks += 1

    def record_validator_failure(self) -> None:
        with self._lock:
            self.validator_failures += 1

    def record_primitive_op(self, count: int = 1) -> None:
        with self._lock:
            self.pilot_primitive_ops += max(0, int(count))

    def record_execution_delta_ms(self, cob_ms: float, sep_estimate_ms: float) -> None:
        with self._lock:
            self._delta_samples_ms.append(float(cob_ms) - float(sep_estimate_ms))

    def pilot_coverage_ratio(self) -> Optional[float]:
        with self._lock:
            if self.pilot_primitive_ops <= 0 or self.pilot_attempts <= 0:
                return None
            return round(
                self.pilot_success / max(self.pilot_attempts, 1),
                6,
            )

    def avg_execution_delta_vs_sep_ms(self) -> Optional[float]:
        with self._lock:
            if not self._delta_samples_ms:
                return None
            return round(statistics.fmean(self._delta_samples_ms), 4)

    def dump_to_dict(self) -> Dict[str, Any]:
        with self._lock:
            popa = (
                round(
                    self.pilot_primitive_ops / max(self.pilot_attempts, 1),
                    4,
                )
                if self.pilot_attempts
                else None
            )
            return {
                "pilot_attempts": self.pilot_attempts,
                "pilot_success": self.pilot_success,
                "pilot_fallbacks": self.pilot_fallbacks,
                "validator_failures": self.validator_failures,
                "avg_execution_delta_vs_sep_ms": self.avg_execution_delta_vs_sep_ms(),
                "pilot_coverage_ratio": self.pilot_coverage_ratio(),
                "pilot_primitive_ops": self.pilot_primitive_ops,
                "primitive_ops_per_attempt_estimate": popa,
            }

    def reset(self) -> None:
        with self._lock:
            self.pilot_attempts = 0
            self.pilot_success = 0
            self.pilot_fallbacks = 0
            self.validator_failures = 0
            self._delta_samples_ms.clear()
            self.pilot_primitive_ops = 0


_singleton: Optional[CobPilotMetrics] = None
_singleton_lock = threading.RLock()


def get_cob_pilot_metrics() -> CobPilotMetrics:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = CobPilotMetrics()
        return _singleton


def reset_cob_pilot_metrics() -> None:
    get_cob_pilot_metrics().reset()
