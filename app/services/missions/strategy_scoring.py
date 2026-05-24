"""
ArthurOS Services - StrategyScoringEngine (Auto-Learning Executor)
------------------------------------------------------------------
Reordena las estrategias candidatas para un step usando aprendizaje real,
con **decay temporal** (lo reciente pesa más), penalizaciones por
duración/reintentos y *bonus* por similitud de target.

Reglas literales del PRD:
  - No eliminar estrategias, solo reordenar.
  - **Nunca** poner coordenadas absolutas por encima de DOM/UIA/OCR
    salvo confirmación humana (regla de seguridad inviolable).
  - Decay temporal: éxitos/fallos viejos pesan menos.
  - Score separado por (app/domain/target) cuando se conoce.

Fórmula:
    score =
        success_rate_weighted          # (ok+α) / (ok+fail+α+β)
      - failure_penalty                # k * recent_failures
      - retry_penalty                  # k * avg_retries
      - duration_penalty               # normalizado contra mediana
      + recent_success_bonus           # bonus si ok en ventana corta
      + target_similarity_bonus        # match exacto > prefijo > global

Diseño:
  - **Pure**: el motor no muta el ExecutionMemoryStore, sólo lee de él.
  - Cae a `LearningLogger.score()` cuando no hay datos de scope (back-
    compat con código existente).
  - El método público de alto nivel es `rank_strategies(...)`. Es el
    drop-in replacement del `LearningLogger.rank_strategies` para callers
    que conocen el contexto (app/domain/target).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.logger import log
from app.services.missions.execution_learning import LearningLogger, get_learning_logger
from app.services.missions.execution_memory_store import (
    ExecutionMemoryStore,
    get_execution_memory_store,
)


# ──────────────────────────────────────────────────────────────────────
# Reglas inviolables: estrategias "inseguras" que sólo suben tras
# confirmación humana explícita. La presencia (substring) basta.
# ──────────────────────────────────────────────────────────────────────

UNSAFE_STRATEGY_TOKENS = (
    "absolute_coords", "rel_coords", "relative_coords",
    "fallback_coords", "blind_click", "raw_pixel",
)

PREFERRED_STRATEGY_TOKENS = (
    "dom", "uia", "ocr", "vision", "playwright",
)

# Half-life del decay temporal en segundos (≈ 7 días):
# un éxito hace 7 días vale la mitad que uno hoy.
DEFAULT_HALF_LIFE_SECONDS = 7 * 24 * 3600.0

# Recent-success window: bonus si ok en últimas N horas.
RECENT_SUCCESS_WINDOW_S = 6 * 3600.0


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _is_unsafe(strategy: str) -> bool:
    s = (strategy or "").lower()
    return any(tok in s for tok in UNSAFE_STRATEGY_TOKENS)


def _is_preferred(strategy: str) -> bool:
    s = (strategy or "").lower()
    return any(tok in s for tok in PREFERRED_STRATEGY_TOKENS)


def _decay(value: float, age_s: float, half_life_s: float) -> float:
    if age_s <= 0:
        return value
    return value * math.pow(0.5, age_s / max(half_life_s, 1.0))


# ──────────────────────────────────────────────────────────────────────
# DTO con desglose explicable (útil para debug/UI)
# ──────────────────────────────────────────────────────────────────────

@dataclass
class StrategyScore:
    strategy: str
    score: float
    components: Dict[str, float]
    samples: int
    last_ok_at: Optional[float] = None
    last_fail_at: Optional[float] = None


# ──────────────────────────────────────────────────────────────────────
# StrategyScoringEngine
# ──────────────────────────────────────────────────────────────────────

class StrategyScoringEngine:
    """Score y reordenamiento de estrategias con aprendizaje contextual.

    Uso:

        engine = StrategyScoringEngine()
        ranked = engine.rank_strategies(
            "search_youtube",
            ["dom_input", "uia_searchbox", "ocr_search", "vision", "rel_coords"],
            scope=Scope(app="chrome.exe", domain="youtube.com",
                        semantic_target="youtube_search_box"),
        )

    Notas:
      - Si no hay nada en `ExecutionMemoryStore`, cae a
        `LearningLogger.score(kind, strategy)` (back-compat).
      - `rank_strategies` siempre devuelve una permutación de la entrada;
        nunca filtra estrategias.
      - Aplica un *safety guard*: estrategias "unsafe" no pueden quedar
        por encima de DOM/UIA/OCR salvo `allow_unsafe_top=True`.
    """

    def __init__(
        self,
        store: Optional[ExecutionMemoryStore] = None,
        *,
        learning: Optional[LearningLogger] = None,
        half_life_seconds: float = DEFAULT_HALF_LIFE_SECONDS,
        recent_success_window_s: float = RECENT_SUCCESS_WINDOW_S,
        weights: Optional[Dict[str, float]] = None,
    ) -> None:
        self.store = store or get_execution_memory_store()
        self.learning = learning or get_learning_logger()
        self.half_life_s = float(half_life_seconds)
        self.recent_window_s = float(recent_success_window_s)
        # Pesos por defecto (se pueden afinar con telemetría):
        self.weights: Dict[str, float] = {
            "success_rate": 1.0,
            "failure_penalty": 0.4,
            "retry_penalty": 0.05,        # por reintento promedio
            "duration_penalty": 0.15,
            "recent_bonus": 0.12,
            "similarity_bonus": 0.10,
        }
        if weights:
            self.weights.update(weights)

    # ── Public ─────────────────────────────────────────────────
    def score_strategy(
        self,
        step_kind: str,
        strategy: str,
        *,
        scope_app: Optional[str] = None,
        scope_domain: Optional[str] = None,
        scope_target: Optional[str] = None,
        now: Optional[float] = None,
    ) -> StrategyScore:
        now = now if now is not None else time.time()
        rows = self._collect_rows(
            step_kind, strategy, scope_app, scope_domain, scope_target,
        )
        components: Dict[str, float] = {}

        # Acumulado ponderado por similitud y decay (para el "score"),
        # más un conteo bruto de muestras (no ponderado) para reportar.
        total_ok = 0.0
        total_fail = 0.0
        raw_samples = 0
        avg_dur_acc = 0.0
        avg_dur_n = 0
        last_ok = None
        last_fail = None
        for similarity, row in rows:
            ok = float(row.get("ok") or 0)
            fail = float(row.get("fail") or 0)
            raw_samples = max(raw_samples, int(ok + fail))
            last_ok_at = row.get("last_ok_at")
            last_fail_at = row.get("last_fail_at")
            ref_age_ok = (now - float(last_ok_at)) if last_ok_at else self.half_life_s * 4
            ref_age_fail = (now - float(last_fail_at)) if last_fail_at else self.half_life_s * 4
            decayed_ok = _decay(ok, ref_age_ok, self.half_life_s) * similarity
            decayed_fail = _decay(fail, ref_age_fail, self.half_life_s) * similarity
            total_ok += decayed_ok
            total_fail += decayed_fail
            avg_dur = float(row.get("avg_duration_ms") or 0.0)
            if avg_dur > 0:
                avg_dur_acc += avg_dur * similarity
                avg_dur_n += 1
            if last_ok_at is not None:
                last_ok = max(last_ok or 0.0, float(last_ok_at))
            if last_fail_at is not None:
                last_fail = max(last_fail or 0.0, float(last_fail_at))

        # Si no hay datos en el store, caer al LearningLogger global.
        legacy_score: Optional[float] = None
        if total_ok + total_fail <= 0:
            try:
                legacy_score = float(self.learning.score(step_kind, strategy))
            except Exception:
                legacy_score = 0.5

        # Suavizado bayesiano (Beta(1,1)).
        n = total_ok + total_fail
        if legacy_score is not None and n <= 0:
            base = legacy_score
        else:
            base = (total_ok + 1.0) / (total_ok + total_fail + 2.0)
        components["success_rate"] = base * self.weights["success_rate"]

        # Penalización por fallo reciente: si hubo fallo en últimas 24 h,
        # y la racha reciente es desfavorable, restamos.
        recent_fail = 0.0
        if last_fail is not None and (now - last_fail) < self.recent_window_s * 4:
            ratio_recent = total_fail / max(n, 1.0)
            recent_fail = ratio_recent * self.weights["failure_penalty"]
        components["failure_penalty"] = -recent_fail

        # Retry penalty: usamos 1 retry implícito por sample fallido y
        # promediamos respecto al total para no castigar de golpe.
        retry_pen = 0.0
        if n > 0:
            retry_pen = (total_fail / n) * self.weights["retry_penalty"] * 2.0
        components["retry_penalty"] = -retry_pen

        # Duration penalty: normalizamos por 5 s (5000 ms) como referencia
        # razonable; >10 s pesa fuerte, <1 s casi nada.
        avg_dur = avg_dur_acc / max(avg_dur_n, 1)
        dur_pen = 0.0
        if avg_dur > 0:
            ratio = min(avg_dur / 5000.0, 3.0)
            dur_pen = math.tanh(ratio - 1.0) * self.weights["duration_penalty"]
            if dur_pen < 0:
                dur_pen = 0.0  # rapidez no premia aquí, sólo evitamos lentitud
        components["duration_penalty"] = -dur_pen

        # Recent success bonus.
        recent_bonus = 0.0
        if last_ok is not None and (now - last_ok) <= self.recent_window_s:
            recent_bonus = self.weights["recent_bonus"]
        components["recent_bonus"] = recent_bonus

        # Similarity bonus: ya está implícito en `_collect_rows` al
        # ponderar; aquí añadimos un pequeño plus si tenemos al menos
        # una observación con scope_target.
        sim_bonus = 0.0
        if scope_target and any(s == 1.0 for s, _ in rows):
            sim_bonus = self.weights["similarity_bonus"]
        components["similarity_bonus"] = sim_bonus

        score = sum(components.values())
        # Acotamos a un rango razonable por seguridad.
        score = max(-1.0, min(2.0, score))

        return StrategyScore(
            strategy=strategy,
            score=score,
            components=components,
            samples=int(max(raw_samples, n)),
            last_ok_at=last_ok,
            last_fail_at=last_fail,
        )

    def rank_strategies(
        self,
        step_kind: str,
        strategies: Sequence[str],
        *,
        scope_app: Optional[str] = None,
        scope_domain: Optional[str] = None,
        scope_target: Optional[str] = None,
        skip: Optional[Iterable[str]] = None,
        allow_unsafe_top: bool = False,
        now: Optional[float] = None,
    ) -> List[str]:
        """Reordena devolviendo permutación.

        Reglas:
          - ``skip`` van al final preservando orden original (compat con
            el LearningLogger legacy).
          - ``allow_unsafe_top=False``: estrategias "unsafe" (coords
            absolutas) NO pueden quedar antes de la primera "preferida"
            (DOM/UIA/OCR/vision). Esto es la regla de seguridad PRD.
        """
        skip_set = set(skip or [])
        ranked_pairs: List[Tuple[float, int, str]] = []
        deferred: List[str] = []
        for idx, s in enumerate(strategies):
            if s in skip_set:
                deferred.append(s)
                continue
            sc = self.score_strategy(
                step_kind, s,
                scope_app=scope_app, scope_domain=scope_domain,
                scope_target=scope_target, now=now,
            )
            ranked_pairs.append((-sc.score, idx, s))
        ranked_pairs.sort()
        ranked = [s for _, _, s in ranked_pairs]

        if not allow_unsafe_top:
            ranked = self._enforce_safety_floor(ranked)

        return ranked + deferred

    def get_score_report(
        self,
        step_kind: str,
        strategies: Sequence[str],
        *,
        scope_app: Optional[str] = None,
        scope_domain: Optional[str] = None,
        scope_target: Optional[str] = None,
    ) -> List[StrategyScore]:
        """Devuelve el desglose completo (para dashboard / debug)."""
        out: List[StrategyScore] = []
        for s in strategies:
            out.append(
                self.score_strategy(
                    step_kind, s,
                    scope_app=scope_app, scope_domain=scope_domain,
                    scope_target=scope_target,
                )
            )
        out.sort(key=lambda x: -x.score)
        return out

    # ── Internals ─────────────────────────────────────────────
    def _collect_rows(
        self,
        step_kind: str,
        strategy: str,
        scope_app: Optional[str],
        scope_domain: Optional[str],
        scope_target: Optional[str],
    ) -> List[Tuple[float, Dict[str, object]]]:
        """Obtiene scores con peso por similitud.

        Niveles:
          - Match exacto (todos los scopes coinciden) ⇒ similarity = 1.0
          - Match parcial app+domain                  ⇒ similarity = 0.6
          - Match parcial sólo app                    ⇒ similarity = 0.4
          - Global (sin scope)                        ⇒ similarity = 0.25
        Si no hay datos en ningún nivel, devuelve lista vacía.
        """
        out: List[Tuple[float, Dict[str, object]]] = []
        try:
            if scope_target:
                exact = self.store.get_strategy_score(
                    step_kind=step_kind, strategy=strategy,
                    scope_app=scope_app, scope_domain=scope_domain,
                    scope_target=scope_target,
                )
                if exact:
                    out.append((1.0, exact))
            if scope_app and scope_domain:
                ad = self.store.get_strategy_score(
                    step_kind=step_kind, strategy=strategy,
                    scope_app=scope_app, scope_domain=scope_domain,
                    scope_target=None,
                )
                if ad:
                    out.append((0.6, ad))
            if scope_app:
                a = self.store.get_strategy_score(
                    step_kind=step_kind, strategy=strategy,
                    scope_app=scope_app, scope_domain=None, scope_target=None,
                )
                if a:
                    out.append((0.4, a))
            glob = self.store.get_strategy_score(
                step_kind=step_kind, strategy=strategy,
                scope_app=None, scope_domain=None, scope_target=None,
            )
            if glob:
                out.append((0.25, glob))
        except Exception as e:  # pragma: no cover
            log.debug(f"_collect_rows falló: {e}")
        return out

    def _enforce_safety_floor(self, ranked: List[str]) -> List[str]:
        """Garantiza que NINGUNA estrategia "unsafe" quede antes de
        cualquier estrategia "preferida" (DOM/UIA/OCR/vision).

        Implementación: separamos en tres cubetas y reanclamos las
        unsafe al final relativo, manteniendo orden interno.
        """
        if not any(_is_unsafe(s) for s in ranked):
            return list(ranked)
        if not any(_is_preferred(s) for s in ranked):
            return list(ranked)
        preferred = [s for s in ranked if _is_preferred(s) and not _is_unsafe(s)]
        unsafe = [s for s in ranked if _is_unsafe(s)]
        rest = [s for s in ranked if not _is_preferred(s) and not _is_unsafe(s)]
        # preserve relative order of preferred from original ranked
        return preferred + rest + unsafe


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────

_engine: Optional[StrategyScoringEngine] = None


def get_strategy_scoring_engine() -> StrategyScoringEngine:
    global _engine
    if _engine is None:
        _engine = StrategyScoringEngine()
    return _engine


def reset_strategy_scoring_engine_for_tests() -> None:
    global _engine
    _engine = None


__all__ = [
    "StrategyScoringEngine",
    "StrategyScore",
    "UNSAFE_STRATEGY_TOKENS",
    "PREFERRED_STRATEGY_TOKENS",
    "get_strategy_scoring_engine",
    "reset_strategy_scoring_engine_for_tests",
]
