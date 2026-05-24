"""
ArthurOS Services - ExecutionQuality (Auto-Learning Executor)
-------------------------------------------------------------
Calcula un **score de calidad por ejecución completa** (no por step):

  - success_rate            (steps OK / total)
  - retries_count           (suma de retries de todos los steps)
  - human_interventions     (steps que requirieron humano)
  - total_duration          (ms)
  - recovery_count          (steps que se salvaron por recovery)
  - strategy_confidence     (promedio del score actual de las estrategias usadas)
  - target_confidence       (promedio de reliability de los targets resueltos)
  - stability_score         (varianza inversa de duraciones — entre menos varíe, mejor)

Salida típica para el usuario:

    "Misión ejecutada con calidad 92%.
     0 intervenciones, 1 recuperación automática, 6.4s."

Este módulo NO duplica `step_quality` (que es semáforo per-step de
runtime/compile time). Aquí calculamos el agregado por **run**.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.core.logger import log
from app.services.missions.execution_memory_store import (
    ExecutionMemoryStore,
    get_execution_memory_store,
)
from app.services.missions.strategy_scoring import (
    StrategyScoringEngine,
    get_strategy_scoring_engine,
)
from app.services.missions.target_memory import (
    TargetMemory,
    get_target_memory,
)


# ──────────────────────────────────────────────────────────────────────
# DTO
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionQualityReport:
    run_id: str
    success: bool
    total_steps: int
    success_steps: int
    success_rate: float
    retries_count: int
    human_interventions: int
    recovery_count: int
    total_duration_ms: int
    strategy_confidence: float
    target_confidence: float
    stability_score: float
    quality_score: float           # 0..1
    user_facing_summary: str
    components: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "success": self.success,
            "total_steps": self.total_steps,
            "success_steps": self.success_steps,
            "success_rate": self.success_rate,
            "retries_count": self.retries_count,
            "human_interventions": self.human_interventions,
            "recovery_count": self.recovery_count,
            "total_duration_ms": self.total_duration_ms,
            "strategy_confidence": self.strategy_confidence,
            "target_confidence": self.target_confidence,
            "stability_score": self.stability_score,
            "quality_score": self.quality_score,
            "user_facing_summary": self.user_facing_summary,
            "components": dict(self.components),
        }


# ──────────────────────────────────────────────────────────────────────
# Calculator
# ──────────────────────────────────────────────────────────────────────

class ExecutionQualityCalculator:
    """Calcula reportes de calidad por ejecución."""

    def __init__(
        self,
        store: Optional[ExecutionMemoryStore] = None,
        *,
        scoring: Optional[StrategyScoringEngine] = None,
        targets: Optional[TargetMemory] = None,
    ) -> None:
        self.store = store or get_execution_memory_store()
        self.scoring = scoring or get_strategy_scoring_engine()
        self.targets = targets or get_target_memory()

    def compute(
        self,
        run_id: str,
        *,
        attempts: Optional[Sequence[Dict[str, Any]]] = None,
        run_status: Optional[str] = None,
    ) -> ExecutionQualityReport:
        rows = (
            list(attempts) if attempts is not None
            else self.store.list_step_attempts(run_id=run_id)
        )

        # Group por step_id — el último intento "manda" para success.
        by_step: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_step.setdefault(str(r.get("step_id") or ""), []).append(r)

        total_steps = len(by_step)
        success_steps = 0
        retries_count = 0
        human_interventions = 0
        recovery_count = 0
        total_duration_ms = 0
        strategy_scores: List[float] = []
        target_scores: List[float] = []
        durations_per_step: List[int] = []

        for step_id, attempts_list in by_step.items():
            attempts_list_sorted = sorted(attempts_list, key=lambda r: r.get("timestamp", 0))
            last = attempts_list_sorted[-1]
            ok = bool(last.get("success"))
            if ok:
                success_steps += 1
            for a in attempts_list_sorted:
                retries_count += int(a.get("retries") or 0)
                if a.get("user_intervention"):
                    human_interventions += 1
                if a.get("recovery_used"):
                    recovery_count += 1
                total_duration_ms += int(a.get("duration_ms") or 0)
            durations_per_step.append(int(last.get("duration_ms") or 0))

            # Confianza de la estrategia usada en el éxito final.
            if ok and last.get("strategy_used"):
                try:
                    sc = self.scoring.score_strategy(
                        str(last.get("step_kind") or ""),
                        str(last.get("strategy_used") or ""),
                        scope_app=last.get("app"),
                        scope_domain=last.get("domain"),
                        scope_target=last.get("semantic_target"),
                    )
                    strategy_scores.append(max(0.0, min(1.0, _normalize(sc.score))))
                except Exception:
                    pass
            # Reliability del target resuelto (target memory).
            t = last.get("semantic_target")
            if t:
                try:
                    entry = self.targets.lookup(
                        str(t),
                        domain=last.get("domain"),
                        app=last.get("app"),
                    )
                    if entry is not None:
                        target_scores.append(entry.reliability)
                except Exception:
                    pass

        # Defaults en runs sin steps.
        success_rate = (success_steps / total_steps) if total_steps else 0.0
        strategy_conf = statistics.fmean(strategy_scores) if strategy_scores else 0.5
        target_conf = statistics.fmean(target_scores) if target_scores else 0.5
        stability = _stability_score(durations_per_step)

        # Composición ponderada (suma = 1.0).
        components = {
            "success_rate": 0.45 * success_rate,
            "no_human_bonus": 0.15 * (0.0 if human_interventions > 0 else 1.0),
            "recovery_efficiency": 0.10 * (1.0 if recovery_count == 0 else max(0.0, 1.0 - recovery_count * 0.2)),
            "retry_efficiency": 0.10 * max(0.0, 1.0 - min(retries_count, 10) / 10.0),
            "strategy_confidence": 0.10 * strategy_conf,
            "target_confidence": 0.05 * target_conf,
            "stability": 0.05 * stability,
        }
        quality_score = max(0.0, min(1.0, sum(components.values())))
        success_global = (
            (run_status == "success") if run_status is not None else (success_rate >= 0.999)
        )

        # Mensaje user-facing
        duration_s = total_duration_ms / 1000.0
        recoveries_word = (
            "0 recuperaciones automáticas" if recovery_count == 0
            else (f"1 recuperación automática" if recovery_count == 1
                  else f"{recovery_count} recuperaciones automáticas")
        )
        humans_word = (
            "0 intervenciones" if human_interventions == 0
            else (f"1 intervención" if human_interventions == 1
                  else f"{human_interventions} intervenciones")
        )
        summary = (
            f"Misión ejecutada con calidad {int(round(quality_score * 100))}%. "
            f"{humans_word}, {recoveries_word}, {duration_s:.1f}s."
        )

        return ExecutionQualityReport(
            run_id=run_id,
            success=bool(success_global),
            total_steps=total_steps,
            success_steps=success_steps,
            success_rate=success_rate,
            retries_count=retries_count,
            human_interventions=human_interventions,
            recovery_count=recovery_count,
            total_duration_ms=total_duration_ms,
            strategy_confidence=strategy_conf,
            target_confidence=target_conf,
            stability_score=stability,
            quality_score=quality_score,
            user_facing_summary=summary,
            components=components,
        )


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _normalize(score: float) -> float:
    """Mapea el score "explained" del StrategyScoringEngine (~ -1..2)
    a un 0..1 razonable.
    """
    return max(0.0, min(1.0, (score + 0.5) / 2.0))


def _stability_score(durations_ms: Sequence[int]) -> float:
    if not durations_ms:
        return 0.5
    if len(durations_ms) == 1:
        return 1.0
    mean = statistics.fmean(durations_ms)
    if mean <= 0:
        return 1.0
    try:
        stdev = statistics.pstdev(durations_ms)
    except statistics.StatisticsError:
        return 1.0
    cv = stdev / mean  # coef. de variación
    # Mapeo: cv 0 → 1.0, cv 1 → 0.0; tanh para suavizar.
    return max(0.0, min(1.0, 1.0 - math.tanh(cv)))


# ──────────────────────────────────────────────────────────────────────
# Façades
# ──────────────────────────────────────────────────────────────────────

_calculator: Optional[ExecutionQualityCalculator] = None


def get_execution_quality_calculator() -> ExecutionQualityCalculator:
    global _calculator
    if _calculator is None:
        _calculator = ExecutionQualityCalculator()
    return _calculator


def reset_execution_quality_for_tests() -> None:
    global _calculator
    _calculator = None


def compute_execution_quality(
    run_id: str,
    *,
    attempts: Optional[Sequence[Dict[str, Any]]] = None,
    run_status: Optional[str] = None,
) -> ExecutionQualityReport:
    return get_execution_quality_calculator().compute(
        run_id, attempts=attempts, run_status=run_status,
    )


__all__ = [
    "ExecutionQualityReport",
    "ExecutionQualityCalculator",
    "compute_execution_quality",
    "get_execution_quality_calculator",
    "reset_execution_quality_for_tests",
]
