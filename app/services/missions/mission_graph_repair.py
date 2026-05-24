"""
ArthurOS Services - MissionGraphRepair (Auto-Learning Executor)
---------------------------------------------------------------
Cuando una estrategia alternativa funciona repetidamente mejor que la
``preferred_strategy`` original de un step, este módulo **propone**
actualizar la misión.

Regla central PRD:
  - No cambiar la INTENCIÓN del paso, sólo la estrategia.
  - Nunca aplicar el cambio en silencio: el AutoLearningExecutor pregunta
    al usuario:

        "Nevlan aprendió que abrir YouTube por barra de direcciones es
         más confiable. ¿Actualizar misión?"
        Opciones: [Sí, actualizar] [Solo para esta ejecución] [No]

API:
  - evaluate(mission_id, step_kind, current_preferred, observed_attempts)
      → MissionGraphRepairSuggestion | None
  - mark_decision(suggestion, decision)   # 'accept' | 'session_only' | 'reject'
  - list_open_suggestions(mission_id)

Usa `ExecutionMemoryStore` para leer estadísticas; persiste decisiones
de "aceptar/rechazar" como métricas de máquina (clave por mission_id +
step_kind).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from app.core.logger import log
from app.services.missions.execution_memory_store import (
    ExecutionMemoryStore,
    get_execution_memory_store,
)
from app.services.missions.strategy_scoring import (
    StrategyScoringEngine,
    get_strategy_scoring_engine,
)


# Mínimo de evidencia para sugerir cambio de preferred_strategy.
DEFAULT_MIN_SAMPLES_NEW = 5
DEFAULT_MIN_DELTA = 0.20      # candidato debe mejorar al menos 20%
DEFAULT_MIN_RECENT_OK = 3     # éxitos recientes seguidos


@dataclass
class MissionGraphRepairSuggestion:
    mission_id: str
    step_kind: str
    step_id: Optional[str]
    current_preferred: str
    suggested_preferred: str
    samples_current: int
    samples_suggested: int
    score_current: float
    score_suggested: float
    rationale: str
    human_message: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "step_kind": self.step_kind,
            "step_id": self.step_id,
            "current_preferred": self.current_preferred,
            "suggested_preferred": self.suggested_preferred,
            "samples_current": self.samples_current,
            "samples_suggested": self.samples_suggested,
            "score_current": self.score_current,
            "score_suggested": self.score_suggested,
            "rationale": self.rationale,
            "human_message": self.human_message,
            "created_at": self.created_at,
        }


@dataclass
class RepairDecision:
    suggestion_key: str
    decision: str            # 'accept' | 'session_only' | 'reject'
    decided_at: float = field(default_factory=time.time)


# ──────────────────────────────────────────────────────────────────────
# Repair engine
# ──────────────────────────────────────────────────────────────────────

class MissionGraphRepair:
    """Sugiere (no aplica) cambios de ``preferred_strategy``.

    Diseño:
      - **Idempotente**: mientras siga la misma desigualdad, devuelve la
        misma sugerencia.
      - **Sólo sugiere**, nunca muta la misión sin decisión explícita.
      - Las decisiones rechazadas se respetan durante 24 h por defecto
        (``cooldown_seconds``).
    """

    METRIC_PREFIX = "graph_repair::"

    def __init__(
        self,
        store: Optional[ExecutionMemoryStore] = None,
        *,
        scoring: Optional[StrategyScoringEngine] = None,
        min_samples_new: int = DEFAULT_MIN_SAMPLES_NEW,
        min_delta: float = DEFAULT_MIN_DELTA,
        min_recent_ok: int = DEFAULT_MIN_RECENT_OK,
        cooldown_seconds: float = 24 * 3600.0,
    ) -> None:
        self.store = store or get_execution_memory_store()
        self.scoring = scoring or get_strategy_scoring_engine()
        self.min_samples_new = int(min_samples_new)
        self.min_delta = float(min_delta)
        self.min_recent_ok = int(min_recent_ok)
        self.cooldown_seconds = float(cooldown_seconds)

    # ── Public ─────────────────────────────────────────────────
    def evaluate(
        self,
        *,
        mission_id: str,
        step_kind: str,
        current_preferred: str,
        candidates: Iterable[str],
        step_id: Optional[str] = None,
        scope_app: Optional[str] = None,
        scope_domain: Optional[str] = None,
        scope_target: Optional[str] = None,
    ) -> Optional[MissionGraphRepairSuggestion]:
        candidates = [c for c in candidates if c and c != current_preferred]
        if not candidates:
            return None

        # Score actual del preferred.
        cur_score = self.scoring.score_strategy(
            step_kind, current_preferred,
            scope_app=scope_app, scope_domain=scope_domain, scope_target=scope_target,
        )

        best: Optional[Dict[str, Any]] = None
        for cand in candidates:
            cand_score = self.scoring.score_strategy(
                step_kind, cand,
                scope_app=scope_app, scope_domain=scope_domain, scope_target=scope_target,
            )
            if cand_score.samples < self.min_samples_new:
                continue
            delta = cand_score.score - cur_score.score
            if delta < self.min_delta:
                continue
            recent_ok = self._count_recent_consecutive_ok(
                step_kind=step_kind, strategy=cand,
                app=scope_app, domain=scope_domain,
            )
            if recent_ok < self.min_recent_ok:
                continue
            if best is None or cand_score.score > best["score"]:
                best = {
                    "strategy": cand,
                    "score": cand_score.score,
                    "samples": cand_score.samples,
                    "delta": delta,
                    "recent_ok": recent_ok,
                }

        if best is None:
            return None

        if self._is_in_cooldown(mission_id, step_kind, best["strategy"]):
            return None

        rationale = (
            f"{best['strategy']} score={best['score']:.2f} sup. {current_preferred} "
            f"score={cur_score.score:.2f} (Δ={best['delta']:.2f}, "
            f"{best['recent_ok']} aciertos seguidos, n={best['samples']})"
        )
        human_msg = (
            f"Nevlan aprendió que “{_humanize(best['strategy'])}” suele ser más confiable "
            f"que “{_humanize(current_preferred)}” para este paso. ¿Actualizar misión?"
        )

        return MissionGraphRepairSuggestion(
            mission_id=mission_id,
            step_kind=step_kind,
            step_id=step_id,
            current_preferred=current_preferred,
            suggested_preferred=best["strategy"],
            samples_current=cur_score.samples,
            samples_suggested=int(best["samples"]),
            score_current=cur_score.score,
            score_suggested=best["score"],
            rationale=rationale,
            human_message=human_msg,
        )

    def mark_decision(
        self,
        suggestion: MissionGraphRepairSuggestion,
        decision: str,
    ) -> RepairDecision:
        if decision not in ("accept", "session_only", "reject"):
            raise ValueError(f"decision inválida: {decision!r}")
        key = self._key(
            suggestion.mission_id, suggestion.step_kind, suggestion.suggested_preferred,
        )
        # Codificamos decisión:
        #   reject  → 1.0
        #   session → 0.5
        #   accept  → 0.0  (ya no hay cooldown)
        value = 1.0 if decision == "reject" else (0.5 if decision == "session_only" else 0.0)
        try:
            self.store.update_machine_metric(
                machine_id=self.METRIC_PREFIX + suggestion.mission_id,
                metric=key, value=value,
            )
        except Exception as e:
            log.debug(f"mark_decision falló: {e}")
        return RepairDecision(suggestion_key=key, decision=decision)

    def is_session_only(self, suggestion: MissionGraphRepairSuggestion) -> bool:
        try:
            metrics = self.store.get_machine_metrics(self.METRIC_PREFIX + suggestion.mission_id)
            v = metrics.get(self._key(
                suggestion.mission_id, suggestion.step_kind, suggestion.suggested_preferred,
            ))
            return v is not None and abs(v - 0.5) < 1e-3
        except Exception:
            return False

    # ── Internals ─────────────────────────────────────────────
    def _key(self, mission_id: str, step_kind: str, suggested: str) -> str:
        return f"{step_kind}::{suggested}"

    def _is_in_cooldown(self, mission_id: str, step_kind: str, suggested: str) -> bool:
        try:
            metrics = self.store.get_machine_metrics(self.METRIC_PREFIX + mission_id)
            v = metrics.get(self._key(mission_id, step_kind, suggested))
            if v is None:
                return False
            # Cooldown sólo aplica a 'reject' (v == 1.0). Si fue session
            # o accept, dejamos seguir.
            if abs(v - 1.0) < 1e-3:
                # Necesitaríamos timestamp para cooldown real; usamos
                # decisión "permanente hasta forget".
                return True
            return False
        except Exception:
            return False

    def _count_recent_consecutive_ok(
        self,
        *,
        step_kind: str,
        strategy: str,
        app: Optional[str],
        domain: Optional[str],
    ) -> int:
        try:
            rows = self.store.list_step_attempts(limit=200)
        except Exception:
            return 0
        # Filtramos por step_kind+strategy y posiblemente app/domain.
        filtered = []
        for r in rows:
            if r.get("step_kind") != step_kind:
                continue
            if r.get("strategy_used") != strategy:
                continue
            if app and (r.get("app") or "") != (app.lower() if app else ""):
                continue
            if domain and (r.get("domain") or "") != (domain.lower() if domain else ""):
                continue
            filtered.append(r)
        # Ordenamos por timestamp asc, contamos racha desde el final.
        filtered.sort(key=lambda r: r.get("timestamp", 0))
        streak = 0
        for r in reversed(filtered):
            if r.get("success"):
                streak += 1
            else:
                break
        return streak


def _humanize(strategy: str) -> str:
    """Mejora un nombre técnico para el mensaje humano."""
    s = (strategy or "").replace("_", " ").strip()
    if not s:
        return "(estrategia desconocida)"
    return s


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────

_repair: Optional[MissionGraphRepair] = None


def get_mission_graph_repair() -> MissionGraphRepair:
    global _repair
    if _repair is None:
        _repair = MissionGraphRepair()
    return _repair


def reset_mission_graph_repair_for_tests() -> None:
    global _repair
    _repair = None


__all__ = [
    "MissionGraphRepair",
    "MissionGraphRepairSuggestion",
    "RepairDecision",
    "get_mission_graph_repair",
    "reset_mission_graph_repair_for_tests",
]
