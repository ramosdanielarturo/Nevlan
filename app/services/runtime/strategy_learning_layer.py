"""
Strategy Learning Layer (SLL) — Fase 1
--------------------------------------
Aprendizaje operacional **local**, incremental y determinista sobre qué
estrategias funcionan mejor por *contexto real* (sin ML pesado).

- Persistencia: SQLite + export JSON (auditable, versionado).
- Ranking: combina LearningLogger legacy, perfiles de evidencia, señales
  de execution_truth / ARL, validadores y latencia.
- Gobierno: no elimina estrategias; umbral de intentos/confianza;
  opcional decaimiento suave; explicaciones legibles por estrategia.

La capa respeta ``SLL_ENABLED`` (default OFF): con el flag apagado las
funciones públicas de ranking delegan en el comportamiento histórico.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.logger import log
from app.core.paths import VAR_DIR

SLL_STORAGE_VERSION = 1

_SLL_DIR = VAR_DIR / "strategy_learning"
_SLL_DB = _SLL_DIR / f"sll_v{SLL_STORAGE_VERSION}.sqlite"


# ── Datamodel ────────────────────────────────────────────────────────


@dataclass
class StrategyContext:
    """Contexto operacional normalizado (dimensión de aprendizaje)."""

    capability_id: str = ""
    app_or_process: str = ""
    domain_or_site: str = ""
    session_type: str = ""
    operational_intent: str = ""
    control_role: str = ""
    validator_type: str = ""
    replay_strategy: str = ""
    browser_family: str = ""
    resolution_bucket: str = ""
    layout_density: str = ""
    target_kind: str = ""
    step_kind: str = ""

    def signature_blob(self) -> Dict[str, str]:
        d = asdict(self)
        # Orden estable para hash
        return {k: str(d.get(k) or "").strip().lower() for k in sorted(d.keys())}

    def signature_hash(self) -> str:
        raw = json.dumps(self.signature_blob(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class StrategyEvidenceProfile:
    key: str
    capability_id: str
    strategy_name: str
    context_signature: Dict[str, str]
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    validator_successes: int = 0
    validator_failures: int = 0
    weighted_success_rate: float = 0.5
    avg_latency_ms: float = 0.0
    arl_recovery_rate: float = 0.0
    validator_pass_rate: float = 0.5
    human_intervention_rate: float = 0.0
    confidence: float = 0.0
    last_updated: float = 0.0
    evidence_window: int = 0
    notes: str = ""
    # Contadores crudos adicionales (auditoría / métricas globales)
    arl_recoveries: int = 0
    arl_failures: int = 0
    fallback_to_sep: int = 0
    human_interventions: int = 0
    cumulative_ambiguity: float = 0.0
    relocation_events: int = 0
    continuity_preserved: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> StrategyEvidenceProfile:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)


@dataclass
class StrategyRankingExplanation:
    """Explicación determinista de por qué se priorizó una estrategia."""

    strategy: str
    rank: int
    score: float
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SllGlobalMetrics:
    """Métricas globales agregadas (memoria de proceso + snapshot DB)."""

    learned_strategy_coverage: float = 0.0
    avg_runtime_improvement: float = 0.0
    avg_validator_improvement: float = 0.0
    fallback_reduction: float = 0.0
    arl_recovery_improvement: float = 0.0
    strategy_confidence_distribution: Dict[str, int] = field(default_factory=dict)
    profiles_loaded: int = 0
    observations_recorded_session: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_GLOBAL = SllGlobalMetrics()
_GLOBAL_LOCK = threading.RLock()
_LAT_EMA_SESSION: List[float] = []
_VAL_EMA_SESSION: List[float] = []
_FB_SESSION: List[int] = []
_ARL_REC_SESSION: List[int] = []


def _settings():
    try:
        from app.core.config import get_settings

        return get_settings()
    except Exception:
        return None


def _ambiguity_blocked(mission: Optional[Any]) -> bool:
    if not mission:
        return False
    try:
        from app.services.runtime.adaptive_runtime_layer import (
            ambiguity_hint_from_shadow,
        )

        amb = ambiguity_hint_from_shadow(getattr(mission, "semantic_shadow_audit", None))
        if int(amb.get("count") or 0) >= 3:
            return True
    except Exception:
        pass
    for cob in getattr(mission, "canonical_operational_blocks", None) or []:
        try:
            snap = getattr(cob, "semantic_ambiguity_snapshot", None) or {}
            if snap.get("requires_human_confirmation"):
                return True
            amb = getattr(cob, "ambiguity", None) or {}
            if int(amb.get("hypothesis_type_count") or 0) > 1:
                return True
        except Exception:
            continue
    return False


def _execution_truth_ok(mission: Optional[Any]) -> bool:
    if mission is None:
        return True
    try:
        from app.services.missions.execution_truth_engine import (
            is_execution_truth_confirmed,
        )

        return bool(is_execution_truth_confirmed(mission))
    except Exception:
        return False


def execution_signals_for_mission(mission: Optional[Any]) -> Tuple[bool, float]:
    """execution_truth confirmada + score medio de alineación SEP desde auditoría ARL."""
    truth = _execution_truth_ok(mission)
    sep = 0.0
    if not mission:
        return truth, sep
    aud = getattr(mission, "adaptive_runtime_audit", None) or {}
    scores: List[float] = []
    for e in aud.get("events") or []:
        if not isinstance(e, dict):
            continue
        b = e.get("bundle") or {}
        ts = b.get("truth_signals") or b.get("execution_truth") or {}
        v = ts.get("sep_alignment_score")
        if v is not None:
            try:
                scores.append(float(v))
            except Exception:
                pass
    if scores:
        sep = sum(scores) / max(1, len(scores))
    return truth, sep


def resolution_bucket_from_screen(w: Optional[int], h: Optional[int]) -> str:
    if not w or not h:
        return ""
    m = max(w, h)
    if m < 1280:
        return "small"
    if m < 1920:
        return "hd"
    if m < 2560:
        return "fhd_plus"
    return "4k_plus"


def build_context_from_semantic_step(
    step: Any,
    mission: Optional[Any] = None,
    state_dict: Optional[Dict[str, Any]] = None,
) -> StrategyContext:
    """Construye contexto desde ``MissionStep`` (Smart Executor)."""
    params = getattr(step, "params", None) or {}
    st = state_dict or {}
    cap = str(params.get("uic_capability_id") or params.get("capability_id") or "").strip()
    app_p = str(
        st.get("active_app")
        or params.get("app")
        or params.get("name")
        or "",
    ).strip()
    dom = str(st.get("browser_domain") or params.get("domain") or "").strip()
    if not dom and st.get("browser_url"):
        try:
            from urllib.parse import urlparse

            dom = urlparse(str(st.get("browser_url"))).netloc or ""
        except Exception:
            dom = ""
    sess = ""
    op_int = ""
    ctrl = str(params.get("control_role") or "").strip()
    val_t = str(params.get("validator_type") or "").strip()
    replay = str(params.get("replay_strategy") or params.get("target_resolution_strategy") or "").strip()
    browser = "chrome" if "chrome" in (app_p or "").lower() else (app_p.split(".")[0] if app_p else "")
    screen = st.get("screen_size") or st.get("screen")
    w = h = None
    if isinstance(screen, (list, tuple)) and len(screen) >= 2:
        try:
            w, h = int(screen[0]), int(screen[1])
        except Exception:
            w = h = None
    res_b = resolution_bucket_from_screen(w, h)
    layout = str(params.get("layout_density") or "").strip()
    target_kind = str(params.get("target_kind") or "").strip()
    if mission:
        sess = str(getattr(mission, "session_type", "") or "")
        op_int = str(getattr(mission, "operational_intent", "") or "")
        if hasattr(mission, "semantic_sessions") and mission.semantic_sessions:
            try:
                last = mission.semantic_sessions[-1]
                sess = str(getattr(last, "session_type", "") or sess)
            except Exception:
                pass
    return StrategyContext(
        capability_id=cap,
        app_or_process=app_p,
        domain_or_site=dom,
        session_type=sess,
        operational_intent=op_int,
        control_role=ctrl,
        validator_type=val_t,
        replay_strategy=replay,
        browser_family=browser,
        resolution_bucket=res_b,
        layout_density=layout,
        target_kind=target_kind,
        step_kind=str(getattr(step, "kind", "") or ""),
    )


def build_context_from_compiled_step(
    step: Any,
    mission: Optional[Any] = None,
) -> StrategyContext:
    """Construye contexto desde ``CompiledStep`` (TargetResolver / Player)."""
    tc = getattr(step, "target_context", None)
    tc = tc if tc is not None else None
    web = (getattr(tc, "web_data", None) or {}) if tc else {}
    url = str(web.get("url") or "")
    dom = ""
    if url:
        try:
            from urllib.parse import urlparse

            dom = urlparse(url).netloc or ""
        except Exception:
            dom = ""
    proc = str((getattr(tc, "process_name", None) or "") if tc else "").strip()
    cap = ""
    try:
        ap = getattr(step, "action_payload", None) or {}
        cap = str(ap.get("uic_capability_id") or "").strip()
    except Exception:
        pass
    val = getattr(step, "validation_strategy", None)
    val_s = getattr(val, "value", str(val or ""))
    rep = getattr(step, "target_resolution_strategy", None)
    rep_s = getattr(rep, "value", str(rep or ""))
    op_int = str(getattr(mission, "operational_intent", "") or "") if mission else ""
    sess = ""
    if mission and getattr(mission, "semantic_sessions", None):
        try:
            sess = str(getattr(mission.semantic_sessions[-1], "session_type", "") or "")
        except Exception:
            sess = ""
    act = getattr(step, "action_strategy", None)
    act_s = getattr(act, "value", str(act or ""))
    return StrategyContext(
        capability_id=cap,
        app_or_process=proc,
        domain_or_site=dom,
        session_type=sess,
        operational_intent=op_int,
        control_role="",
        validator_type=val_s,
        replay_strategy=rep_s,
        browser_family="chrome" if "chrome" in proc.lower() else proc.split(".")[0] if proc else "",
        resolution_bucket="",
        layout_density="",
        target_kind="",
        step_kind=act_s,
    )


class StrategyLearningStore:
    """Persistencia SQLite thread-safe."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db = Path(db_path) if db_path else _SLL_DB
        self._lock = threading.RLock()
        try:
            _SLL_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            try:
                c = self._connect()
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sll_profile (
                        profile_key TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    """
                )
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sll_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    """
                )
                c.commit()
                c.close()
            except Exception as e:
                log.debug("SLL schema ensure failed: %s", e)

    def load_profile(self, key: str) -> Optional[StrategyEvidenceProfile]:
        with self._lock:
            try:
                c = self._connect()
                row = c.execute(
                    "SELECT payload FROM sll_profile WHERE profile_key = ?",
                    (key,),
                ).fetchone()
                c.close()
                if not row:
                    return None
                return StrategyEvidenceProfile.from_dict(json.loads(row[0]))
            except Exception as e:
                log.debug("SLL load_profile: %s", e)
                return None

    def upsert_profile(self, profile: StrategyEvidenceProfile) -> None:
        with self._lock:
            try:
                payload = json.dumps(profile.to_dict(), ensure_ascii=False)
                c = self._connect()
                c.execute(
                    """
                    INSERT INTO sll_profile(profile_key, payload, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(profile_key) DO UPDATE SET
                      payload = excluded.payload,
                      updated_at = excluded.updated_at
                    """,
                    (profile.key, payload, time.time()),
                )
                c.commit()
                c.close()
            except Exception as e:
                log.debug("SLL upsert_profile: %s", e)

    def all_profiles(self) -> List[StrategyEvidenceProfile]:
        out: List[StrategyEvidenceProfile] = []
        with self._lock:
            try:
                c = self._connect()
                rows = c.execute("SELECT payload FROM sll_profile").fetchall()
                c.close()
                for (pl,) in rows:
                    try:
                        out.append(StrategyEvidenceProfile.from_dict(json.loads(pl)))
                    except Exception:
                        continue
            except Exception as e:
                log.debug("SLL all_profiles: %s", e)
        cfg = _settings()
        cap = int(getattr(cfg, "SLL_MAX_PROFILE_SIZE", 5000) or 5000) if cfg else 5000
        if len(out) > cap:
            out.sort(key=lambda p: p.last_updated, reverse=True)
            out = out[:cap]
        return out

    def append_audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            try:
                c = self._connect()
                c.execute(
                    "INSERT INTO sll_audit(ts, event_type, payload) VALUES(?,?,?)",
                    (time.time(), event_type, json.dumps(payload, ensure_ascii=False)),
                )
                c.commit()
                c.close()
            except Exception as e:
                log.debug("SLL audit: %s", e)

    def prune_if_needed(self) -> None:
        cfg = _settings()
        cap = int(getattr(cfg, "SLL_MAX_PROFILE_SIZE", 5000) or 5000) if cfg else 5000
        with self._lock:
            try:
                c = self._connect()
                n = c.execute("SELECT COUNT(*) FROM sll_profile").fetchone()[0]
                if n <= cap:
                    c.close()
                    return
                # Borrar los más antiguos
                over = n - cap
                c.execute(
                    """
                    DELETE FROM sll_profile WHERE profile_key IN (
                      SELECT profile_key FROM sll_profile
                      ORDER BY updated_at ASC LIMIT ?
                    )
                    """,
                    (over,),
                )
                c.commit()
                c.close()
            except Exception as e:
                log.debug("SLL prune: %s", e)

    def export_bundle(self) -> Dict[str, Any]:
        return {
            "version": SLL_STORAGE_VERSION,
            "exported_at": time.time(),
            "profiles": [p.to_dict() for p in self.all_profiles()],
            "global_metrics": get_sll_global_metrics().to_dict(),
        }


_STORE: Optional[StrategyLearningStore] = None
_STORE_LOCK = threading.Lock()


def get_strategy_learning_store() -> StrategyLearningStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = StrategyLearningStore()
        return _STORE


def reset_strategy_learning_store_for_tests(path: Optional[Path] = None) -> StrategyLearningStore:
    """Tests: reinicia almacén apuntando a un SQLite efímero."""
    global _STORE
    with _STORE_LOCK:
        _STORE = StrategyLearningStore(db_path=path)
        return _STORE


def profile_key_for(context: StrategyContext, strategy_name: str) -> str:
    base = context.signature_hash()
    s = strategy_name.strip()
    return hashlib.sha256(f"{base}|{s}".encode("utf-8")).hexdigest()[:40]


def _beta_rate(ok: int, total: int) -> float:
    return float(ok + 1) / float(total + 2)


def _ema(prev: float, x: float, alpha: float = 0.15) -> float:
    if prev <= 0:
        return x
    return alpha * x + (1.0 - alpha) * prev


def _apply_decay_counters(p: StrategyEvidenceProfile) -> None:
    cfg = _settings()
    if not cfg or not getattr(cfg, "SLL_DECAY_ENABLED", False):
        return
    if p.last_updated <= 0:
        return
    days = max(0.0, (time.time() - p.last_updated) / 86400.0)
    if days <= 0:
        return
    factor = 0.995 ** min(days, 365.0)
    p.attempts = int(round(p.attempts * factor))
    p.successes = int(round(p.successes * factor))
    p.failures = int(round(p.failures * factor))
    p.validator_successes = int(round(p.validator_successes * factor))
    p.validator_failures = int(round(p.validator_failures * factor))
    p.arl_recoveries = int(round(p.arl_recoveries * factor))
    p.arl_failures = int(round(p.arl_failures * factor))
    p.fallback_to_sep = int(round(p.fallback_to_sep * factor))
    p.human_interventions = int(round(p.human_interventions * factor))


def _confidence_from_attempts(n: int) -> float:
    return float(min(1.0, n / (n + 12)))


def record_observation(
    *,
    context: StrategyContext,
    strategy_name: str,
    success: bool,
    duration_ms: float = 0.0,
    mission: Optional[Any] = None,
    validator_passed: Optional[bool] = None,
    fallback_to_sep: bool = False,
    arl_recovery: bool = False,
    arl_failure: bool = False,
    human_intervention: bool = False,
    ambiguity_rate: float = 0.0,
    relocation_hit: bool = False,
    continuity_preserved: bool = False,
    mission_terminal_failed: bool = False,
    notes: str = "",
    require_execution_truth_for_success: bool = True,
) -> Optional[StrategyEvidenceProfile]:
    """Registra un resultado operacional (incremental)."""
    cfg = _settings()
    if not cfg or not getattr(cfg, "SLL_ENABLED", False):
        return None
    if _ambiguity_blocked(mission):
        return None
    if (
        success
        and require_execution_truth_for_success
        and mission is not None
        and not _execution_truth_ok(mission)
    ):
        # No promover éxitos sin execution_truth confirmada en la misión.
        return None
    if not success and mission_terminal_failed and mission is not None and not _execution_truth_ok(mission):
        return None

    store = get_strategy_learning_store()
    key = profile_key_for(context, strategy_name)
    p = store.load_profile(key)
    if p is None:
        p = StrategyEvidenceProfile(
            key=key,
            capability_id=context.capability_id,
            strategy_name=strategy_name,
            context_signature=context.signature_blob(),
        )
    _apply_decay_counters(p)

    p.attempts += 1
    if success:
        p.successes += 1
    else:
        p.failures += 1
    if validator_passed is True:
        p.validator_successes += 1
    elif validator_passed is False:
        p.validator_failures += 1
    if fallback_to_sep:
        p.fallback_to_sep += 1
    if arl_recovery:
        p.arl_recoveries += 1
    if arl_failure:
        p.arl_failures += 1
    if human_intervention:
        p.human_interventions += 1
    p.cumulative_ambiguity += max(0.0, ambiguity_rate)
    if relocation_hit:
        p.relocation_events += 1
    if continuity_preserved:
        p.continuity_preserved += 1

    p.weighted_success_rate = _beta_rate(p.successes, p.attempts)
    tot_v = p.validator_successes + p.validator_failures
    p.validator_pass_rate = _beta_rate(p.validator_successes, tot_v) if tot_v else 0.5
    p.avg_latency_ms = _ema(p.avg_latency_ms, float(duration_ms), alpha=0.2)
    p.arl_recovery_rate = float(p.arl_recoveries) / float(max(1, p.attempts))
    p.human_intervention_rate = float(p.human_interventions) / float(max(1, p.attempts))
    p.confidence = _confidence_from_attempts(p.attempts)
    p.last_updated = time.time()
    p.evidence_window = min(5000, p.attempts)
    if notes:
        p.notes = (p.notes + " | " if p.notes else "") + notes[:240]

    store.upsert_profile(p)
    store.append_audit(
        "observation",
        {
            "key": key,
            "strategy": strategy_name,
            "success": success,
            "duration_ms": duration_ms,
            "context": context.signature_blob(),
        },
    )
    store.prune_if_needed()
    _bump_global_metrics(p, duration_ms, validator_passed, fallback_to_sep, arl_recovery)
    return p


def _bump_global_metrics(
    prof: StrategyEvidenceProfile,
    duration_ms: float,
    validator_passed: Optional[bool],
    fallback_to_sep: bool,
    arl_recovery: bool,
) -> None:
    global _GLOBAL
    with _GLOBAL_LOCK:
        _GLOBAL.observations_recorded_session += 1
        _LAT_EMA_SESSION.append(float(duration_ms))
        if validator_passed is True:
            _VAL_EMA_SESSION.append(1.0)
        elif validator_passed is False:
            _VAL_EMA_SESSION.append(0.0)
        _FB_SESSION.append(1 if fallback_to_sep else 0)
        _ARL_REC_SESSION.append(1 if arl_recovery else 0)

        all_p = get_strategy_learning_store().all_profiles()
        _GLOBAL.profiles_loaded = len(all_p)
        min_att = int(getattr(_settings(), "SLL_MIN_ATTEMPTS", 5) or 5)
        learned = sum(1 for x in all_p if x.attempts >= min_att)
        _GLOBAL.learned_strategy_coverage = (
            float(learned) / float(max(1, len(all_p))) if all_p else 0.0
        )
        if _LAT_EMA_SESSION:
            # Mejora heurística vs 500ms de referencia (sólo sesión).
            ref = 500.0
            avg = sum(_LAT_EMA_SESSION[-200:]) / max(1, len(_LAT_EMA_SESSION[-200:]))
            _GLOBAL.avg_runtime_improvement = max(0.0, (ref - avg) / ref)
        if _VAL_EMA_SESSION:
            vm = sum(_VAL_EMA_SESSION[-200:]) / max(1, len(_VAL_EMA_SESSION[-200:]))
            _GLOBAL.avg_validator_improvement = vm
        if _FB_SESSION:
            _GLOBAL.fallback_reduction = 1.0 - (
                sum(_FB_SESSION[-200:]) / max(1, len(_FB_SESSION[-200:]))
            )
        if _ARL_REC_SESSION:
            _GLOBAL.arl_recovery_improvement = sum(_ARL_REC_SESSION[-200:]) / max(
                1, len(_ARL_REC_SESSION[-200:])
            )
        # Distribución de confianza en buckets
        b = int(min(9, prof.confidence * 10))
        dist = dict(_GLOBAL.strategy_confidence_distribution)
        dist[str(b)] = int(dist.get(str(b), 0)) + 1
        _GLOBAL.strategy_confidence_distribution = dist


def get_sll_global_metrics() -> SllGlobalMetrics:
    with _GLOBAL_LOCK:
        return SllGlobalMetrics(
            learned_strategy_coverage=_GLOBAL.learned_strategy_coverage,
            avg_runtime_improvement=_GLOBAL.avg_runtime_improvement,
            avg_validator_improvement=_GLOBAL.avg_validator_improvement,
            fallback_reduction=_GLOBAL.fallback_reduction,
            arl_recovery_improvement=_GLOBAL.arl_recovery_improvement,
            strategy_confidence_distribution=dict(_GLOBAL.strategy_confidence_distribution),
            profiles_loaded=_GLOBAL.profiles_loaded,
            observations_recorded_session=_GLOBAL.observations_recorded_session,
        )


def rank_strategies_with_learning(
    strategies: List[str],
    context: StrategyContext,
    *,
    learning: Optional[Any] = None,
    execution_truth_confirmed: bool = True,
    sep_alignment_score: float = 0.0,
    skip: Optional[Iterable[str]] = None,
    ompc_substring_boosts: Optional[Dict[str, float]] = None,
) -> Tuple[List[str], Dict[str, StrategyRankingExplanation]]:
    """
    Combina ranking histórico (LearningLogger) + evidencia SLL + señales
    operacionales. Con ``SLL_ENABLED=False`` equivale a ``learning.rank_strategies``.
    """
    cfg = _settings()
    skip_set = set(skip or [])
    base_list = [s for s in strategies if s not in skip_set]
    tail = [s for s in strategies if s in skip_set]

    if not cfg or not getattr(cfg, "SLL_ENABLED", False):
        if learning is not None and hasattr(learning, "rank_strategies"):
            try:
                ranked = learning.rank_strategies(
                    context.step_kind or "*",
                    list(base_list),
                    skip=[],
                )
            except TypeError:
                ranked = learning.rank_strategies(
                    context.step_kind or "*",
                    list(base_list),
                )
        else:
            ranked = list(base_list)
        expl: Dict[str, StrategyRankingExplanation] = {}
        for i, s in enumerate(ranked, start=1):
            expl[s] = StrategyRankingExplanation(
                strategy=s,
                rank=i,
                score=0.0,
                reasons=["SLL desactivado — orden por LearningLogger o base."],
                metrics={},
            )
        return ranked + tail, expl

    store = get_strategy_learning_store()
    min_att = int(getattr(cfg, "SLL_MIN_ATTEMPTS", 5) or 5)
    conf_thr = float(getattr(cfg, "SLL_CONFIDENCE_THRESHOLD", 0.35) or 0.35)

    scored: List[Tuple[float, int, str]] = []
    explanations: Dict[str, StrategyRankingExplanation] = {}

    for idx, s in enumerate(base_list):
        legacy = 0.5
        if learning is not None and hasattr(learning, "score"):
            try:
                legacy = float(learning.score(context.step_kind or "*", s))
            except Exception:
                legacy = 0.5

        prof = store.load_profile(profile_key_for(context, s))
        attempts = prof.attempts if prof else 0
        w_sr = prof.weighted_success_rate if prof else 0.5
        conf = prof.confidence if prof else 0.0
        if attempts < min_att:
            blend = 0.5 * w_sr + 0.5 * 0.5
            conf_eff = 0.0
        else:
            blend = w_sr
            conf_eff = conf
        if conf_eff < conf_thr:
            blend = 0.65 * blend + 0.35 * 0.5

        val_pass = prof.validator_pass_rate if prof else 0.5
        lat = prof.avg_latency_ms if prof else 200.0
        lat_pen = min(0.2, max(0.0, (lat - 120.0) / 4500.0))

        fb_rate = (
            float(prof.fallback_to_sep) / float(max(1, prof.attempts)) if prof else 0.0
        )
        fb_pen = min(0.25, fb_rate * 0.55)

        arl_part = 0.0
        arl_rr = float(prof.arl_recovery_rate) if prof and prof.attempts > 0 else 0.0
        if prof and prof.attempts > 0:
            arl_part = min(0.07, arl_rr * 0.12)

        truth_part = 1.0 if execution_truth_confirmed else 0.42
        sep_part = max(0.0, min(1.0, sep_alignment_score))

        combined = (
            0.22 * legacy
            + 0.28 * blend
            + 0.12 * val_pass
            + 0.10 * (1.0 - lat_pen)
            + 0.10 * truth_part
            + 0.08 * sep_part
            + 0.06 * (1.0 - fb_pen)
            + arl_part
        )
        # Pequeño sesgo a orden base estable
        combined += 0.001 * (len(base_list) - idx) / max(1, len(base_list))

        ompc_bonus = 0.0
        if ompc_substring_boosts:
            sl = (s or "").lower()
            for sub, b in ompc_substring_boosts.items():
                if str(sub).lower() in sl:
                    ompc_bonus += float(b)
            ompc_bonus = min(0.06, ompc_bonus)
            combined += ompc_bonus

        reasons = [
            f"legacy_score={legacy:.3f}",
            f"learned_success_rate={w_sr:.3f} (attempts={attempts})",
            f"validator_pass_rate={val_pass:.3f}",
            f"avg_latency_ms={lat:.1f}",
            f"fallback_share={fb_rate:.3f}",
            f"arl_recovery_rate={arl_rr:.3f}",
            f"truth_prior={truth_part:.2f} sep_align={sep_part:.2f}",
        ]
        if ompc_bonus > 0:
            reasons.append(f"ompc_substring_boost={ompc_bonus:.4f}")
        explanations[s] = StrategyRankingExplanation(
            strategy=s,
            rank=0,
            score=combined,
            reasons=reasons,
            metrics={
                "attempts": attempts,
                "confidence": conf_eff,
                "weighted_success_rate": w_sr,
            },
        )
        scored.append((combined, -idx, s))

    scored.sort(reverse=True)
    ranked_out = [t[2] for t in scored]
    for i, s in enumerate(ranked_out, start=1):
        explanations[s].rank = i

    return ranked_out + tail, explanations


def record_cob_pilot_observation(
    mission: Any,
    cob: Any,
    *,
    ok: bool,
    elapsed_ms: float,
    fell_back: bool,
) -> None:
    ctx = StrategyContext(
        capability_id=str(getattr(cob, "id", "") or ""),
        app_or_process="cob_pilot",
        domain_or_site="",
        session_type="cob",
        operational_intent=str(getattr(cob, "canonical_action", "") or ""),
        control_role="cob_aggregate",
        validator_type="cob_eligibility",
        replay_strategy="cob_pilot",
        browser_family="",
        resolution_bucket="",
        layout_density="",
        target_kind="aggregate",
        step_kind=str(getattr(cob, "canonical_action", "") or ""),
    )
    strategy = f"cob_pilot:{getattr(cob, 'canonical_action', '') or 'unknown'}"
    record_observation(
        context=ctx,
        strategy_name=strategy,
        success=bool(ok),
        duration_ms=elapsed_ms,
        mission=mission,
        fallback_to_sep=fell_back,
        notes="cob_execution_pilot",
        require_execution_truth_for_success=False,
    )


def explanations_as_human_summary(expl: Dict[str, StrategyRankingExplanation], top_n: int = 4) -> List[str]:
    lines: List[str] = []
    for s, e in sorted(expl.items(), key=lambda it: it[1].rank)[:top_n]:
        lines.append(
            f"{e.rank}. {s}: score={e.score:.4f} — " + "; ".join(e.reasons[:3])
        )
    return lines


__all__ = [
    "StrategyContext",
    "StrategyEvidenceProfile",
    "StrategyRankingExplanation",
    "SllGlobalMetrics",
    "StrategyLearningStore",
    "get_strategy_learning_store",
    "reset_strategy_learning_store_for_tests",
    "build_context_from_semantic_step",
    "build_context_from_compiled_step",
    "rank_strategies_with_learning",
    "record_observation",
    "record_cob_pilot_observation",
    "get_sll_global_metrics",
    "explanations_as_human_summary",
    "profile_key_for",
    "resolution_bucket_from_screen",
    "execution_signals_for_mission",
    "SLL_STORAGE_VERSION",
]
