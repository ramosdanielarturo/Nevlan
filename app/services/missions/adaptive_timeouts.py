"""
ArthurOS Services - AdaptiveTimeouts (Auto-Learning Executor)
-------------------------------------------------------------
Timeouts inteligentes por (condition, app, domain).

Idea: en vez de hardcodear esperas humanas tipo ``time.sleep(2)``,
medimos cuánto tarda cada condición real y proponemos un timeout
**p95 + margen**. Si la red empeora (network_slow detectado), elevamos
temporalmente el techo.

Uso típico:

    from app.services.missions.adaptive_timeouts import (
        record_observation, recommended_timeout,
    )

    t0 = time.monotonic()
    ok = wait_until(youtube_loaded, timeout_ms=recommended_timeout(
        "youtube_loaded", app="chrome.exe", domain="youtube.com",
        default_ms=8000,
    ))
    record_observation("youtube_loaded", duration_ms=int((time.monotonic()-t0)*1000),
                       success=ok, app="chrome.exe", domain="youtube.com")

Reglas (PRD):
  - Nunca usar sleeps humanos arbitrarios.
  - Timeout se aprende por máquina/red.
  - Si network_slow detectado, aumentar temporalmente.
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.core.logger import log
from app.services.missions.execution_memory_store import (
    ExecutionMemoryStore,
    get_execution_memory_store,
)


# ──────────────────────────────────────────────────────────────────────
# Reglas
# ──────────────────────────────────────────────────────────────────────

DEFAULT_MIN_TIMEOUT_MS = 1500
DEFAULT_MAX_TIMEOUT_MS = 60_000
DEFAULT_MARGIN_MS = 800              # margen sumado al p95
DEFAULT_NETWORK_SLOW_FACTOR = 1.6    # multiplicador si red lenta
DEFAULT_SAMPLE_FLOOR = 5             # mínimo de muestras para confiar en p95


# ──────────────────────────────────────────────────────────────────────
# DTO
# ──────────────────────────────────────────────────────────────────────

@dataclass
class TimeoutRecommendation:
    condition: str
    app: str
    domain: str
    samples: int
    p50_ms: int
    p95_ms: int
    suggested_ms: int
    floor_ms: int
    ceiling_ms: int
    used_default: bool


# ──────────────────────────────────────────────────────────────────────
# AdaptiveTimeouts
# ──────────────────────────────────────────────────────────────────────

class AdaptiveTimeouts:
    """Timeouts que aprenden por condición."""

    def __init__(
        self,
        store: Optional[ExecutionMemoryStore] = None,
        *,
        margin_ms: int = DEFAULT_MARGIN_MS,
        min_timeout_ms: int = DEFAULT_MIN_TIMEOUT_MS,
        max_timeout_ms: int = DEFAULT_MAX_TIMEOUT_MS,
        network_slow_factor: float = DEFAULT_NETWORK_SLOW_FACTOR,
    ) -> None:
        self.store = store or get_execution_memory_store()
        self.margin_ms = int(margin_ms)
        self.min_timeout_ms = int(min_timeout_ms)
        self.max_timeout_ms = int(max_timeout_ms)
        self.network_slow_factor = float(network_slow_factor)
        # Estado runtime opcional: last "network slow" decay timestamp.
        self._network_slow_until: float = 0.0

    # ── API pública ────────────────────────────────────────────
    def record_observation(
        self,
        condition: str,
        *,
        duration_ms: int,
        success: bool,
        app: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> None:
        try:
            self.store.record_timeout_observation(
                condition=condition, duration_ms=int(duration_ms),
                success=bool(success), app=app, domain=domain,
            )
        except Exception as e:
            log.debug(f"AdaptiveTimeouts.record_observation falló: {e}")

    def recommended_timeout(
        self,
        condition: str,
        *,
        default_ms: int,
        app: Optional[str] = None,
        domain: Optional[str] = None,
        max_ms: Optional[int] = None,
    ) -> int:
        rec = self.recommend(
            condition, default_ms=default_ms, app=app, domain=domain, max_ms=max_ms,
        )
        return rec.suggested_ms

    def recommend(
        self,
        condition: str,
        *,
        default_ms: int,
        app: Optional[str] = None,
        domain: Optional[str] = None,
        max_ms: Optional[int] = None,
    ) -> TimeoutRecommendation:
        ceiling = int(max_ms or self.max_timeout_ms)
        # El ceiling siempre manda sobre el floor: si el caller pide un
        # max_ms más estricto que nuestro floor por defecto, el caller
        # tiene la última palabra.
        floor = min(self.min_timeout_ms, ceiling)
        observations = self._fetch_observations(condition, app=app, domain=domain)
        if len(observations) < DEFAULT_SAMPLE_FLOOR:
            suggested = max(floor, min(int(default_ms), ceiling))
            return TimeoutRecommendation(
                condition=condition,
                app=(app or "").lower(), domain=(domain or "").lower(),
                samples=len(observations),
                p50_ms=int(default_ms), p95_ms=int(default_ms),
                suggested_ms=suggested, floor_ms=floor, ceiling_ms=ceiling,
                used_default=True,
            )
        durations = [int(d) for d in observations]
        durations.sort()
        p50 = _percentile(durations, 0.50)
        p95 = _percentile(durations, 0.95)
        suggested = int(p95 + self.margin_ms)
        if self._is_network_slow_now():
            suggested = int(suggested * self.network_slow_factor)
        suggested = max(floor, min(suggested, ceiling))
        return TimeoutRecommendation(
            condition=condition,
            app=(app or "").lower(), domain=(domain or "").lower(),
            samples=len(durations),
            p50_ms=int(p50), p95_ms=int(p95),
            suggested_ms=suggested, floor_ms=floor, ceiling_ms=ceiling,
            used_default=False,
        )

    def mark_network_slow(self, *, for_seconds: float = 600.0) -> None:
        """Activa el factor de red lenta durante una ventana corta.

        Esto lo llama el AutoLearningExecutor cuando el clasificador
        detecta ``network_slow``.
        """
        self._network_slow_until = time.time() + max(0.0, float(for_seconds))

    def is_network_slow(self) -> bool:
        return self._is_network_slow_now()

    # ── Internals ─────────────────────────────────────────────
    def _fetch_observations(
        self, condition: str,
        *, app: Optional[str], domain: Optional[str], limit: int = 200,
    ) -> List[int]:
        try:
            rows = self.store.list_timeout_observations(
                condition=condition, app=app, domain=domain, limit=limit,
            )
        except Exception as e:
            log.debug(f"_fetch_observations falló: {e}")
            return []
        durations: List[int] = []
        for r in rows:
            try:
                durations.append(int(r.get("duration_ms") or 0))
            except Exception:
                continue
        # Quitamos ceros (instantáneos) que probablemente son ruido.
        return [d for d in durations if d > 0]

    def _is_network_slow_now(self) -> bool:
        return self._network_slow_until > time.time()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _percentile(sorted_values: List[int], p: float) -> float:
    """Percentil simple sobre lista ordenada (interpolación lineal)."""
    if not sorted_values:
        return 0.0
    if p <= 0:
        return float(sorted_values[0])
    if p >= 1:
        return float(sorted_values[-1])
    k = (len(sorted_values) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_values[int(k)])
    d0 = sorted_values[int(f)] * (c - k)
    d1 = sorted_values[int(c)] * (k - f)
    return float(d0 + d1)


# ──────────────────────────────────────────────────────────────────────
# Singleton + façades
# ──────────────────────────────────────────────────────────────────────

_adaptive: Optional[AdaptiveTimeouts] = None


def get_adaptive_timeouts() -> AdaptiveTimeouts:
    global _adaptive
    if _adaptive is None:
        _adaptive = AdaptiveTimeouts()
    return _adaptive


def reset_adaptive_timeouts_for_tests() -> None:
    global _adaptive
    _adaptive = None


def record_observation(
    condition: str, *, duration_ms: int, success: bool,
    app: Optional[str] = None, domain: Optional[str] = None,
) -> None:
    get_adaptive_timeouts().record_observation(
        condition, duration_ms=duration_ms, success=success,
        app=app, domain=domain,
    )


def recommended_timeout(
    condition: str, *, default_ms: int,
    app: Optional[str] = None, domain: Optional[str] = None,
    max_ms: Optional[int] = None,
) -> int:
    return get_adaptive_timeouts().recommended_timeout(
        condition, default_ms=default_ms, app=app, domain=domain, max_ms=max_ms,
    )


__all__ = [
    "AdaptiveTimeouts",
    "TimeoutRecommendation",
    "get_adaptive_timeouts",
    "reset_adaptive_timeouts_for_tests",
    "record_observation",
    "recommended_timeout",
]
