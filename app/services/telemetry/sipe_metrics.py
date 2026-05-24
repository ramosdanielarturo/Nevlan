"""Nevlan — Telemetría agregada del Semantic Intent Promotion Engine.

Cuenta cuántas misiones llegan al ``smart_runner_bridge`` ya
promovidas (``source == "semantic_intent_promotion_engine"``) vs.
cuántas hay que promover en runtime vs. cuántas caen al fallback
clásico (``build_semantic_execution_plan``).

Sirve para **decidir cuándo retirar el camino on-demand** del bridge.
Cuando ``promoted_at_runtime`` cae a 0% durante una ventana de uso
significativa, podemos eliminar la rama de promoción del bridge y
dejar SIPE solo en el recorder/compiler.

Diseño
------

* Singleton ``sipe_metrics`` (instancia de :class:`SipeMetrics`).
* Thread-safe vía ``threading.Lock``.
* En memoria — esta capa es operacional, no auditable. Para
  persistencia por misión usar ``mission.intent_promotions``.
* API ergonómica:

  >>> from app.services.telemetry.sipe_metrics import sipe_metrics
  >>> sipe_metrics.record_already_promoted()
  >>> sipe_metrics.record_promoted_at_runtime(promotions_count=3)
  >>> sipe_metrics.record_legacy_fallback(reason="ice_collapsed_to_zero")
  >>> sipe_metrics.snapshot()
  {'already_promoted': 1, 'promoted_at_runtime': 1,
   'legacy_fallback': 1, 'total': 3, 'promoted_at_runtime_pct': 33.3, ...}
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class SipeMetricsSnapshot:
    """Snapshot inmutable del estado de :class:`SipeMetrics`.

    Devuelto por :meth:`SipeMetrics.snapshot`. Es serializable a
    JSON y útil para dashboards / logs estructurados.
    """

    already_promoted: int
    promoted_at_runtime: int
    legacy_fallback: int
    failure: int
    total_promotions_emitted: int
    fallback_reasons: Dict[str, int]
    started_at: datetime
    last_updated_at: Optional[datetime]

    @property
    def total(self) -> int:
        return (
            self.already_promoted
            + self.promoted_at_runtime
            + self.legacy_fallback
            + self.failure
        )

    @property
    def already_promoted_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return round(100.0 * self.already_promoted / self.total, 2)

    @property
    def promoted_at_runtime_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return round(100.0 * self.promoted_at_runtime / self.total, 2)

    @property
    def legacy_fallback_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return round(100.0 * self.legacy_fallback / self.total, 2)

    @property
    def failure_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return round(100.0 * self.failure / self.total, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "already_promoted": self.already_promoted,
            "promoted_at_runtime": self.promoted_at_runtime,
            "legacy_fallback": self.legacy_fallback,
            "failure": self.failure,
            "total": self.total,
            "total_promotions_emitted": self.total_promotions_emitted,
            "already_promoted_pct": self.already_promoted_pct,
            "promoted_at_runtime_pct": self.promoted_at_runtime_pct,
            "legacy_fallback_pct": self.legacy_fallback_pct,
            "failure_pct": self.failure_pct,
            "fallback_reasons": dict(self.fallback_reasons),
            "started_at": self.started_at.isoformat(),
            "last_updated_at": (
                self.last_updated_at.isoformat()
                if self.last_updated_at else None
            ),
        }


class SipeMetrics:
    """Contadores agregados del SIPE wiring en el bridge.

    Cuatro contadores principales:

    * ``already_promoted`` — la misión llegó al bridge con
      ``mission.semantic_execution_plan["source"] ==
      "semantic_intent_promotion_engine"``. SIPE NO se invocó.
    * ``promoted_at_runtime`` — el bridge invocó
      ``apply_promotion_to_mission`` y el plan resultante tiene
      steps. Caso típico: misión grabada antes del wiring de SIPE.
    * ``legacy_fallback`` — SIPE falló y el bridge tuvo que caer
      a ``build_semantic_execution_plan`` clásico (o al adapter
      legacy sobre ``compiled_execution_graph``).
    * ``failure`` — SIPE Y el fallback clásico fallaron. Caso
      patológico — debería ser 0 en producción saludable.

    También agregamos:

    * ``total_promotions_emitted`` — suma de ``len(promotions)``
      por cada promoción exitosa. Útil para ver el ritmo de
      transformación legacy → genérico.
    * ``fallback_reasons`` — ``Counter`` por motivo string corto
      (ej. ``"sipe_exception"``, ``"build_classic_exception"``).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._already_promoted = 0
        self._promoted_at_runtime = 0
        self._legacy_fallback = 0
        self._failure = 0
        self._total_promotions_emitted = 0
        self._fallback_reasons: Counter = Counter()
        self._started_at = datetime.now(timezone.utc)
        self._last_updated_at: Optional[datetime] = None

    # ── Recordings ─────────────────────────────────────────────────

    def record_already_promoted(self) -> None:
        """La misión llegó con SEP source=SIPE — no se reprocesó."""
        with self._lock:
            self._already_promoted += 1
            self._last_updated_at = datetime.now(timezone.utc)

    def record_promoted_at_runtime(self, *, promotions_count: int = 0) -> None:
        """SIPE corrió en runtime. ``promotions_count`` = len de las
        promociones (legacy → generic) emitidas para esa misión.
        """
        with self._lock:
            self._promoted_at_runtime += 1
            self._total_promotions_emitted += max(0, int(promotions_count))
            self._last_updated_at = datetime.now(timezone.utc)

    def record_legacy_fallback(self, *, reason: str = "unknown") -> None:
        """SIPE falló y tuvimos que caer al camino clásico."""
        with self._lock:
            self._legacy_fallback += 1
            self._fallback_reasons[(reason or "unknown").strip().lower()] += 1
            self._last_updated_at = datetime.now(timezone.utc)

    def record_failure(self, *, reason: str = "unknown") -> None:
        """SIPE Y el fallback clásico fallaron. Caso patológico."""
        with self._lock:
            self._failure += 1
            self._fallback_reasons[
                f"failure:{(reason or 'unknown').strip().lower()}"
            ] += 1
            self._last_updated_at = datetime.now(timezone.utc)

    # ── Inspección ─────────────────────────────────────────────────

    def snapshot(self) -> SipeMetricsSnapshot:
        """Devuelve un snapshot inmutable de los contadores."""
        with self._lock:
            return SipeMetricsSnapshot(
                already_promoted=self._already_promoted,
                promoted_at_runtime=self._promoted_at_runtime,
                legacy_fallback=self._legacy_fallback,
                failure=self._failure,
                total_promotions_emitted=self._total_promotions_emitted,
                fallback_reasons=dict(self._fallback_reasons),
                started_at=self._started_at,
                last_updated_at=self._last_updated_at,
            )

    def reset(self) -> None:
        """Reinicia todos los contadores. Sólo para tests / dashboards."""
        with self._lock:
            self._already_promoted = 0
            self._promoted_at_runtime = 0
            self._legacy_fallback = 0
            self._failure = 0
            self._total_promotions_emitted = 0
            self._fallback_reasons.clear()
            self._started_at = datetime.now(timezone.utc)
            self._last_updated_at = None


# Singleton global — el bridge importa esta instancia directamente.
sipe_metrics = SipeMetrics()


__all__ = ["SipeMetrics", "SipeMetricsSnapshot", "sipe_metrics"]
