"""
Nevlan — Execution Telemetry (PRD 2026-05-12 §Fase 2E)
========================================================

Colector liviano y thread-safe de métricas de ejecución. Sin cloud,
sin sockets, sin agentes externos — los datos viven en memoria y se
pueden volcar a JSON cuando el usuario quiera (Mission Review experto,
botón "diagnóstico", o un script offline).

Métricas tracked (todas las del PRD §2E):

  1.  target_resolution_time_ms       — histograma por strategy
  2.  strategy_used                    — counter por método
  3.  zero_capture_rate                — % de resoluciones que NO tomaron foto
  4.  fingerprint_usage_rate           — % donde visual_fingerprint participó
  5.  confirmation_rate                — % SAFE_WITH_CONFIRMATION
  6.  self_heal_success_rate           — % de pasos saltados/curados
  7.  replay_success_rate              — % de pasos completados exitosamente
  8.  sep_generation_rate              — % de misiones con SEP no-vacío
  9.  fallback_to_legacy_rate          — % de misiones que cayeron a legacy
  10. false_positive_clicks            — clicks marcados como erroneos a posteriori
  11. emergency_coords_usage           — count con coords_emergency_no_fingerprint
  12. repair_frequency                 — count de aperturas de reparación
  13. mission_ready_rate               — % de misiones con intent_status=READY

Diseño:
  * **Stateless por defecto** — un singleton ``_DEFAULT`` que cualquier
    módulo puede leer con :func:`get_default_collector()`. Tests crean
    un colector propio para aislar.
  * **Thread-safe** — un único ``threading.RLock`` interno.
  * **Sin side-effects al importar** — el colector no toca disco ni red
    hasta que se le pida ``dump_to_dict()``.
  * **Histogramas como listas ordenadas** + helpers ``p50``/``p95``
    derivados sobre demanda. Evita guardar resúmenes pre-agregados que
    pierden información útil de cola.

Política PRD §"no-heavy-AI":
  * Cero deps nuevas. Solo stdlib (``time``, ``threading``,
    ``collections``, ``statistics``).
"""
from __future__ import annotations

import statistics
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


__all__ = [
    "ResolutionEvent",
    "TelemetryCollector",
    "get_default_collector",
    "reset_default_collector",
]


# ──────────────────────────────────────────────────────────────────────
# Evento de resolución (input principal)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ResolutionEvent:
    """Una llamada a ``TargetResolver.resolve()`` produce uno de estos.

    Mínimo necesario para todas las métricas; cualquier extensión va
    bajo ``extra`` sin romper el esquema.
    """

    method: str                          # "visual_fingerprint", "uia_exact", ...
    score: int                           # 0-100
    target_kind: str                     # "uia_control" | "visual_point" | ...
    latency_ms: int                      # tiempo de resolución
    zero_capture: bool                   # True si nivel=coord (sin foto)
    fingerprint_used: bool               # True si _try_visual_fingerprint corrió
    needs_confirmation: bool             # True si SAFE_WITH_CONFIRMATION
    safety_reason: str = ""              # "" o el flag del resolver
    confidence: str = ""                 # "SAFE_TO_EXECUTE" | ...
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StepOutcomeEvent:
    """Resultado de un step a nivel de SmartExecutor / Player."""

    step_id: str
    kind: str
    status: str                          # "success" | "skipped" | "failed" | "needs_human"
    strategy_used: Optional[str] = None
    duration_ms: int = 0
    self_healed: bool = False            # True si recovery resolvió
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MissionOutcomeEvent:
    """Una misión completa que terminó (o se aborto)."""

    mission_id: str
    intent_status: str                   # "READY" | "BLOCKED" | ...
    has_sep: bool
    used_legacy: bool
    ready_provenance: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Collector
# ──────────────────────────────────────────────────────────────────────


class TelemetryCollector:
    """Colector thread-safe en memoria.

    Ejemplo de uso::

        col = get_default_collector()
        col.record_resolution(ResolutionEvent(...))
        ...
        report = col.dump_to_dict()
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._resolutions: List[ResolutionEvent] = []
        self._step_outcomes: List[StepOutcomeEvent] = []
        self._mission_outcomes: List[MissionOutcomeEvent] = []
        # Contadores derivados explícitos para los tres "free counters"
        # que no encajan en ResolutionEvent.
        self._false_positive_clicks: int = 0
        self._repair_open_count: int = 0
        # Latency histograms por strategy (lista de ms).
        self._latency_by_strategy: Dict[str, List[int]] = defaultdict(list)
        self._started_at: float = time.time()

    # ── Recording API ───────────────────────────────────────────────

    def record_resolution(self, evt: ResolutionEvent) -> None:
        if not isinstance(evt, ResolutionEvent):
            return
        with self._lock:
            self._resolutions.append(evt)
            if evt.method and evt.method != "none":
                self._latency_by_strategy[evt.method].append(int(evt.latency_ms))

    def record_step_outcome(self, evt: StepOutcomeEvent) -> None:
        if not isinstance(evt, StepOutcomeEvent):
            return
        with self._lock:
            self._step_outcomes.append(evt)

    def record_mission_outcome(self, evt: MissionOutcomeEvent) -> None:
        if not isinstance(evt, MissionOutcomeEvent):
            return
        with self._lock:
            self._mission_outcomes.append(evt)

    def record_false_positive_click(self) -> None:
        with self._lock:
            self._false_positive_clicks += 1

    def record_repair_opened(self) -> None:
        with self._lock:
            self._repair_open_count += 1

    # ── Helpers derivados ───────────────────────────────────────────

    @staticmethod
    def _pct(numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round(100.0 * numerator / denominator, 2)

    @staticmethod
    def _percentile(values: List[int], pct: float) -> int:
        if not values:
            return 0
        if len(values) == 1:
            return int(values[0])
        try:
            # statistics.quantiles devuelve n-1 cortes. Para p50 / p95 lo
            # hacemos a mano por simplicidad y para evitar ediciones de
            # n cuando hay pocos puntos.
            sv = sorted(values)
            k = max(0, min(len(sv) - 1, int(round((pct / 100.0) * (len(sv) - 1)))))
            return int(sv[k])
        except Exception:
            return 0

    # ── Dump / report ───────────────────────────────────────────────

    def dump_to_dict(self) -> Dict[str, Any]:
        """Devuelve el snapshot completo de telemetría como dict
        serializable. Idempotente; no muta el estado.
        """
        with self._lock:
            n_res = len(self._resolutions)
            zero_cap = sum(1 for r in self._resolutions if r.zero_capture)
            fp_used = sum(1 for r in self._resolutions if r.fingerprint_used)
            need_conf = sum(
                1 for r in self._resolutions if r.needs_confirmation
            )
            emergency = sum(
                1 for r in self._resolutions
                if r.safety_reason == "coords_emergency_no_fingerprint_available"
            )
            strategies = Counter(
                r.method for r in self._resolutions if r.method
            )

            # Step outcomes
            n_steps = len(self._step_outcomes)
            success_steps = sum(
                1 for s in self._step_outcomes if s.status == "success"
            )
            skipped_steps = sum(
                1 for s in self._step_outcomes if s.status == "skipped"
            )
            healed_steps = sum(
                1 for s in self._step_outcomes if s.self_healed
            )
            # PRD: self_heal_success_rate combina "skipped:already_satisfied"
            # + "success tras recovery".
            self_heal_attempts = sum(
                1 for s in self._step_outcomes
                if s.self_healed or s.status == "skipped"
            )
            self_heal_success = sum(
                1 for s in self._step_outcomes
                if (s.self_healed and s.status == "success") or s.status == "skipped"
            )

            # Mission outcomes
            n_miss = len(self._mission_outcomes)
            sep_present = sum(
                1 for m in self._mission_outcomes if m.has_sep
            )
            ready_miss = sum(
                1 for m in self._mission_outcomes if m.intent_status == "READY"
            )
            legacy_fallbacks = sum(
                1 for m in self._mission_outcomes if m.used_legacy
            )

            # Latency stats por strategy
            latency_stats: Dict[str, Dict[str, int]] = {}
            for strat, vals in self._latency_by_strategy.items():
                if not vals:
                    continue
                latency_stats[strat] = {
                    "count": len(vals),
                    "min_ms": int(min(vals)),
                    "p50_ms": self._percentile(vals, 50),
                    "p95_ms": self._percentile(vals, 95),
                    "max_ms": int(max(vals)),
                    "avg_ms": int(round(sum(vals) / len(vals))),
                }

            return {
                "uptime_s": round(time.time() - self._started_at, 2),
                "counts": {
                    "resolutions": n_res,
                    "steps": n_steps,
                    "missions": n_miss,
                    "false_positive_clicks": self._false_positive_clicks,
                    "repair_opened": self._repair_open_count,
                },
                "strategy_used": dict(strategies),
                "rates": {
                    "zero_capture_rate_pct": self._pct(zero_cap, n_res),
                    "fingerprint_usage_rate_pct": self._pct(fp_used, n_res),
                    "confirmation_rate_pct": self._pct(need_conf, n_res),
                    "emergency_coords_rate_pct": self._pct(emergency, n_res),
                    "replay_success_rate_pct": self._pct(success_steps, n_steps),
                    "self_heal_success_rate_pct": self._pct(
                        self_heal_success, self_heal_attempts,
                    ),
                    "sep_generation_rate_pct": self._pct(sep_present, n_miss),
                    "mission_ready_rate_pct": self._pct(ready_miss, n_miss),
                    "fallback_to_legacy_rate_pct": self._pct(
                        legacy_fallbacks, n_miss,
                    ),
                },
                "latency_by_strategy_ms": latency_stats,
                "raw_counts": {
                    "zero_capture": zero_cap,
                    "fingerprint_used": fp_used,
                    "needs_confirmation": need_conf,
                    "emergency_coords": emergency,
                    "success_steps": success_steps,
                    "skipped_steps": skipped_steps,
                    "healed_steps": healed_steps,
                    "sep_present_missions": sep_present,
                    "ready_missions": ready_miss,
                    "legacy_fallbacks": legacy_fallbacks,
                },
            }

    def reset(self) -> None:
        """Limpia todos los buffers. Usado por tests / "iniciar sesión nueva"."""
        with self._lock:
            self._resolutions.clear()
            self._step_outcomes.clear()
            self._mission_outcomes.clear()
            self._false_positive_clicks = 0
            self._repair_open_count = 0
            self._latency_by_strategy.clear()
            self._started_at = time.time()


# ──────────────────────────────────────────────────────────────────────
# Singleton por defecto
# ──────────────────────────────────────────────────────────────────────

_DEFAULT: Optional[TelemetryCollector] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_collector() -> TelemetryCollector:
    """Devuelve el colector global. Lazy-init para evitar side-effects
    al importar el módulo (clave para tests).
    """
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = TelemetryCollector()
    return _DEFAULT


def reset_default_collector() -> None:
    """Limpia el singleton. Útil entre tests."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is not None:
            _DEFAULT.reset()
