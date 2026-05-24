"""
Operational Memory & Pattern Consolidation (OMPC) — Fase 1
---------------------------------------------------------
Memoria operacional **local** que consolida **patrones de workflow** (no sólo
estrategias aisladas): secuencias COB + SEP, transiciones, recoveries ARL y
señales de validación — sin IA pesada, determinista y auditable.

- Persistencia: SQLite versionado bajo ``var/operational_memory/``.
- API: consolidación incremental, predicción de transiciones (Markov ligero),
  sugerencias de runtime (**sólo hints**; no ejecuta optimizaciones automáticas).
- Gobierno: ``OMPC_ENABLED`` default OFF; umbrales de evidencia/confianza;
  sin consolidar bajo ambigüedad fuerte o misión fallida no validada.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.logger import log
from app.core.paths import VAR_DIR

OMPC_STORAGE_VERSION = 1

_OMPC_DIR = VAR_DIR / "operational_memory"
_OMPC_DB = _OMPC_DIR / f"ompc_v{OMPC_STORAGE_VERSION}.sqlite"

_TRANSITION_WINDOW = 2


# ── Datamodel ───────────────────────────────────────────────────────────


@dataclass
class OperationalPatternProfile:
    pattern_id: str
    pattern_signature: str
    workflow_type: str = ""
    canonical_sequence: List[str] = field(default_factory=list)
    involved_capabilities: List[str] = field(default_factory=list)
    involved_apps: List[str] = field(default_factory=list)
    involved_domains: List[str] = field(default_factory=list)
    session_types: List[str] = field(default_factory=list)
    operational_intents: List[str] = field(default_factory=list)
    average_duration_ms: float = 0.0
    success_rate: float = 0.5
    fallback_rate: float = 0.0
    arl_recovery_rate: float = 0.0
    validator_failure_rate: float = 0.0
    ambiguity_rate: float = 0.0
    stability_score: float = 0.5
    fragility_score: float = 0.5
    portability_score: float = 0.5
    recurrence_count: int = 0
    confidence: float = 0.0
    last_seen: float = 0.0
    evidence_window: int = 0
    optimization_hints: List[str] = field(default_factory=list)
    predicted_next_patterns: List[Dict[str, Any]] = field(default_factory=list)
    known_recovery_patterns: List[Dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    layout_shift_rate: float = 0.0
    # Boosts deterministas para SLL: substring → bonus acumulable (cap externo)
    substring_boosts: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> OperationalPatternProfile:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {}
        for k in known:
            if k not in d:
                continue
            v = d[k]
            if k in ("canonical_sequence", "involved_capabilities", "involved_apps",
                     "involved_domains", "session_types", "operational_intents",
                     "optimization_hints"):
                kwargs[k] = list(v or [])
            elif k in ("predicted_next_patterns", "known_recovery_patterns"):
                kwargs[k] = [dict(x) for x in (v or [])]
            elif k == "substring_boosts":
                kwargs[k] = {str(a): float(b) for a, b in (v or {}).items()}
            else:
                kwargs[k] = v
        return cls(**kwargs)


@dataclass
class OmpcGlobalMetrics:
    operational_patterns_count: int = 0
    stable_workflows_count: int = 0
    fragile_workflows_count: int = 0
    recurring_recoveries_count: int = 0
    optimization_hint_count: int = 0
    predicted_transition_accuracy: float = 0.0
    runtime_optimization_gain_estimate: float = 0.0
    session_observations: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_OMPC_GLOBAL = OmpcGlobalMetrics()
_OMPC_GLOBAL_LOCK = threading.RLock()
_PRED_TRANS_HITS: List[int] = []


def _settings():
    try:
        from app.core.config import get_settings

        return get_settings()
    except Exception:
        return None


def _ambiguity_blocked(mission: Optional[Any]) -> bool:
    try:
        from app.services.runtime.strategy_learning_layer import _ambiguity_blocked

        return bool(_ambiguity_blocked(mission))
    except Exception:
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


def _ema(prev: float, x: float, alpha: float = 0.2) -> float:
    if prev <= 0:
        return x
    return alpha * x + (1.0 - alpha) * prev


def _confidence_from_recurrence(n: int, min_occ: int) -> float:
    if n < min_occ:
        return float(n) / float(min_occ + 3)
    return float(min(1.0, (n - min_occ) / (n + 10) + 0.35))


def derive_pattern_signature(
    canonical_sequence: Sequence[str],
    *,
    workflow_type: str = "",
    resolution_bucket: str = "",
    layout_density: str = "",
) -> str:
    """Firma determinista del patrón (hash corto estable)."""
    blob = {
        "seq": [str(x).strip().lower() for x in canonical_sequence],
        "workflow_type": str(workflow_type or "").strip().lower(),
        "resolution_bucket": str(resolution_bucket or "").strip().lower(),
        "layout_density": str(layout_density or "").strip().lower(),
    }
    raw = json.dumps(blob, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def derive_pattern_id(pattern_signature: str) -> str:
    return f"pat_{pattern_signature[:16]}"


def classify_pattern_stability(
    success_rate: float,
    fallback_rate: float,
    arl_recovery_rate: float,
    ambiguity_rate: float,
) -> float:
    penalty = (
        0.22 * max(0.0, min(1.0, fallback_rate))
        + 0.18 * max(0.0, min(1.0, arl_recovery_rate))
        + 0.35 * max(0.0, min(1.0, ambiguity_rate))
    )
    return max(0.0, min(1.0, float(success_rate) - penalty))


def classify_pattern_fragility(
    validator_failure_rate: float,
    layout_shift_rate: float,
    ambiguity_rate: float,
    success_rate: float,
) -> float:
    return max(
        0.0,
        min(
            1.0,
            0.36 * max(0.0, min(1.0, validator_failure_rate))
            + 0.28 * max(0.0, min(1.0, layout_shift_rate))
            + 0.28 * max(0.0, min(1.0, ambiguity_rate))
            + 0.18 * max(0.0, min(1.0, 1.0 - success_rate)),
        ),
    )


class OperationalMemoryStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db = Path(db_path) if db_path else _OMPC_DB
        self._lock = threading.RLock()
        try:
            _OMPC_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db), timeout=30)
        c.execute("PRAGMA journal_mode=WAL;")
        return c

    def _ensure_schema(self) -> None:
        with self._lock:
            try:
                c = self._connect()
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ompc_profile (
                        pattern_id TEXT PRIMARY KEY,
                        pattern_signature TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    """
                )
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ompc_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    """
                )
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ompc_transition (
                        from_key TEXT NOT NULL,
                        next_token TEXT NOT NULL,
                        count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (from_key, next_token)
                    );
                    """
                )
                c.commit()
                c.close()
            except Exception as e:
                log.debug("OMPC schema: %s", e)

    def load_profile(self, pattern_id: str) -> Optional[OperationalPatternProfile]:
        with self._lock:
            try:
                c = self._connect()
                row = c.execute(
                    "SELECT payload FROM ompc_profile WHERE pattern_id = ?",
                    (pattern_id,),
                ).fetchone()
                c.close()
                if not row:
                    return None
                return OperationalPatternProfile.from_dict(json.loads(row[0]))
            except Exception as e:
                log.debug("OMPC load_profile: %s", e)
                return None

    def upsert_profile(self, profile: OperationalPatternProfile) -> None:
        with self._lock:
            try:
                pl = json.dumps(profile.to_dict(), ensure_ascii=False)
                c = self._connect()
                c.execute(
                    """
                    INSERT INTO ompc_profile(pattern_id, pattern_signature, payload, updated_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(pattern_id) DO UPDATE SET
                      pattern_signature = excluded.pattern_signature,
                      payload = excluded.payload,
                      updated_at = excluded.updated_at
                    """,
                    (profile.pattern_id, profile.pattern_signature, pl, time.time()),
                )
                c.commit()
                c.close()
            except Exception as e:
                log.debug("OMPC upsert: %s", e)

    def all_profiles(self) -> List[OperationalPatternProfile]:
        out: List[OperationalPatternProfile] = []
        with self._lock:
            try:
                c = self._connect()
                rows = c.execute("SELECT payload FROM ompc_profile").fetchall()
                c.close()
                for (pl,) in rows:
                    try:
                        out.append(OperationalPatternProfile.from_dict(json.loads(pl)))
                    except Exception:
                        continue
            except Exception as e:
                log.debug("OMPC all_profiles: %s", e)
        cap = int(getattr(_settings(), "OMPC_MAX_PATTERN_MEMORY", 3000) or 3000)
        if len(out) > cap:
            out.sort(key=lambda p: p.last_seen, reverse=True)
            out = out[:cap]
        return out

    def bump_transition(self, from_key: str, next_token: str, delta: int = 1) -> None:
        with self._lock:
            try:
                c = self._connect()
                c.execute(
                    """
                    INSERT INTO ompc_transition(from_key, next_token, count)
                    VALUES(?,?,?)
                    ON CONFLICT(from_key, next_token) DO UPDATE SET
                      count = count + excluded.count
                    """,
                    (from_key, next_token, max(1, int(delta))),
                )
                c.commit()
                c.close()
            except Exception as e:
                log.debug("OMPC transition: %s", e)

    def next_tokens_for(self, from_key: str, limit: int = 8) -> List[Tuple[str, int]]:
        with self._lock:
            try:
                c = self._connect()
                rows = c.execute(
                    """
                    SELECT next_token, count FROM ompc_transition
                    WHERE from_key = ?
                    ORDER BY count DESC
                    LIMIT ?
                    """,
                    (from_key, int(limit)),
                ).fetchall()
                c.close()
                return [(str(a), int(b)) for a, b in rows]
            except Exception as e:
                log.debug("OMPC next_tokens: %s", e)
                return []

    def append_audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            try:
                c = self._connect()
                c.execute(
                    "INSERT INTO ompc_audit(ts, event_type, payload) VALUES(?,?,?)",
                    (time.time(), event_type, json.dumps(payload, ensure_ascii=False)),
                )
                c.commit()
                c.close()
            except Exception as e:
                log.debug("OMPC audit: %s", e)

    def prune_excess(self) -> None:
        cfg = _settings()
        cap = int(getattr(cfg, "OMPC_MAX_PATTERN_MEMORY", 3000) or 3000)
        with self._lock:
            try:
                c = self._connect()
                n = c.execute("SELECT COUNT(*) FROM ompc_profile").fetchone()[0]
                if n <= cap:
                    c.close()
                    return
                over = n - cap
                c.execute(
                    """
                    DELETE FROM ompc_profile WHERE pattern_id IN (
                      SELECT pattern_id FROM ompc_profile
                      ORDER BY updated_at ASC LIMIT ?
                    )
                    """,
                    (over,),
                )
                c.commit()
                c.close()
            except Exception as e:
                log.debug("OMPC prune: %s", e)

    def export_bundle(self) -> Dict[str, Any]:
        return {
            "version": OMPC_STORAGE_VERSION,
            "exported_at": time.time(),
            "patterns": [p.to_dict() for p in self.all_profiles()],
            "global_metrics": get_ompc_global_metrics().to_dict(),
        }


_STORE: Optional[OperationalMemoryStore] = None
_STORE_LOCK = threading.Lock()


def get_operational_memory_store() -> OperationalMemoryStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = OperationalMemoryStore()
        return _STORE


def reset_operational_memory_store_for_tests(path: Optional[Path] = None) -> OperationalMemoryStore:
    global _STORE
    with _STORE_LOCK:
        _STORE = OperationalMemoryStore(db_path=path)
        return _STORE


def reset_ompc_session_metrics_for_tests() -> None:
    """Limpia contadores en memoria (sesión de prueba); no borra SQLite."""
    global _OMPC_GLOBAL
    with _OMPC_GLOBAL_LOCK:
        _OMPC_GLOBAL = OmpcGlobalMetrics()
    _PRED_TRANS_HITS.clear()


def _transition_from_key(tokens: Sequence[str], window: int = _TRANSITION_WINDOW) -> str:
    if not tokens:
        return ""
    w = max(1, int(window))
    tail = list(tokens[-w:])
    return ">>".join(tail)


def _record_transitions(sequence: Sequence[str]) -> None:
    if len(sequence) < 2:
        return
    store = get_operational_memory_store()
    for w in (1, _TRANSITION_WINDOW):
        for i in range(len(sequence) - 1):
            prefix = sequence[max(0, i - w + 1) : i + 1]
            if len(prefix) < min(w, i + 1):
                continue
            fk = ">>".join(prefix[-w:]) if w > 1 else prefix[-1]
            nxt = sequence[i + 1]
            store.bump_transition(fk, nxt, 1)


def detect_pattern_recurrence(
    pattern_signature: str,
) -> List[OperationalPatternProfile]:
    """Perfiles cuya firma coincide exactamente (Fase 1)."""
    pid = derive_pattern_id(pattern_signature)
    p = get_operational_memory_store().load_profile(pid)
    return [p] if p and p.pattern_signature == pattern_signature else []


def _apply_decay(p: OperationalPatternProfile) -> None:
    cfg = _settings()
    if not cfg or not getattr(cfg, "OMPC_TEMPORAL_DECAY_ENABLED", False):
        return
    if p.last_seen <= 0:
        return
    days = max(0.0, (time.time() - p.last_seen) / 86400.0)
    if days <= 0:
        return
    factor = 0.992 ** min(days, 400.0)
    p.recurrence_count = max(0, int(round(p.recurrence_count * factor)))


def _merge_hints(base: List[str], new: Iterable[str]) -> List[str]:
    s = list(dict.fromkeys([*(base or []), *[str(x) for x in new if x]]))
    return s[:48]


def _merge_recovery_known(
    base: List[Dict[str, Any]],
    *,
    recovery_key: str,
    detail: Dict[str, Any],
    max_items: int = 24,
) -> List[Dict[str, Any]]:
    rows = [dict(x) for x in (base or [])]
    for r in rows:
        if r.get("key") == recovery_key:
            r["count"] = int(r.get("count", 0)) + 1
            r["detail"] = {**dict(r.get("detail") or {}), **detail}
            return rows[:max_items]
    rows.append({"key": recovery_key, "count": 1, "detail": detail})
    return rows[:max_items]


def consolidate_operational_pattern(
    *,
    canonical_sequence: Sequence[str],
    workflow_type: str = "",
    mission: Optional[Any] = None,
    ended_success: bool = False,
    duration_ms: float = 0.0,
    fallbacks: int = 0,
    arl_recoveries: int = 0,
    arl_failures: int = 0,
    validator_failures: int = 0,
    ambiguity_events: int = 0,
    layout_shift_events: int = 0,
    recovery_detail: Optional[Dict[str, Any]] = None,
    resolution_bucket: str = "",
    layout_density: str = "",
    portability_score_hint: float = 0.5,
    notes: str = "",
    mission_terminal_failed: bool = False,
) -> Optional[OperationalPatternProfile]:
    """Actualiza o crea un perfil de patrón operacional."""
    cfg = _settings()
    if not cfg or not getattr(cfg, "OMPC_ENABLED", False):
        return None
    if _ambiguity_blocked(mission):
        return None
    min_occ = int(getattr(cfg, "OMPC_MIN_PATTERN_OCCURRENCES", 2) or 2)
    conf_thr = float(getattr(cfg, "OMPC_CONFIDENCE_THRESHOLD", 0.35) or 0.35)

    if ended_success and mission is not None and not _execution_truth_ok(mission):
        return None
    if mission_terminal_failed and mission is not None and not _execution_truth_ok(mission):
        return None
    amb_rate = float(ambiguity_events) / max(1, len(canonical_sequence))
    if amb_rate > 0.45:
        return None

    sig = derive_pattern_signature(
        canonical_sequence,
        workflow_type=workflow_type,
        resolution_bucket=resolution_bucket,
        layout_density=layout_density,
    )
    pid = derive_pattern_id(sig)
    store = get_operational_memory_store()
    p = store.load_profile(pid)
    if p is None:
        p = OperationalPatternProfile(
            pattern_id=pid,
            pattern_signature=sig,
            workflow_type=workflow_type,
            canonical_sequence=list(canonical_sequence),
        )
    _apply_decay(p)

    p.recurrence_count += 1
    tot = p.recurrence_count
    if tot <= 1:
        p.success_rate = 1.0 if ended_success else 0.0
    else:
        p.success_rate = (
            (tot - 1) * float(p.success_rate) + (1.0 if ended_success else 0.0)
        ) / float(tot)

    p.average_duration_ms = _ema(p.average_duration_ms, float(duration_ms), alpha=0.18)

    fb_r = float(fallbacks) / max(1, tot)
    p.fallback_rate = _ema(p.fallback_rate, fb_r, alpha=0.25)
    arl_r = float(arl_recoveries) / max(1, tot)
    p.arl_recovery_rate = _ema(p.arl_recovery_rate, arl_r, alpha=0.25)
    vf_r = float(validator_failures) / max(1, tot)
    p.validator_failure_rate = _ema(p.validator_failure_rate, vf_r, alpha=0.25)
    p.ambiguity_rate = _ema(p.ambiguity_rate, amb_rate, alpha=0.22)
    ls_r = float(layout_shift_events) / max(1, tot)
    p.layout_shift_rate = _ema(p.layout_shift_rate, ls_r, alpha=0.22)

    p.stability_score = classify_pattern_stability(
        p.success_rate, p.fallback_rate, p.arl_recovery_rate, p.ambiguity_rate
    )
    p.fragility_score = classify_pattern_fragility(
        p.validator_failure_rate, p.layout_shift_rate, p.ambiguity_rate, p.success_rate
    )
    p.portability_score = _ema(
        p.portability_score, float(portability_score_hint), alpha=0.15
    )

    p.workflow_type = workflow_type or p.workflow_type
    p.canonical_sequence = list(canonical_sequence) or p.canonical_sequence
    p.last_seen = time.time()
    p.evidence_window = min(10_000, tot)
    p.confidence = _confidence_from_recurrence(tot, min_occ)
    if p.confidence < conf_thr:
        p.notes = (p.notes + " | " if p.notes else "") + "low_confidence_blend"

    # Hints deterministas
    hints: List[str] = []
    if p.layout_shift_rate >= 0.2:
        hints.append("expect_layout_shift")
    if p.ambiguity_rate >= 0.15:
        hints.append("strict_validator_mode")
    elif p.stability_score >= 0.72 and p.fragility_score < 0.35:
        hints.append("aggressive_validator_mode")
    if "youtube" in " ".join(canonical_sequence).lower() or any(
        "web" in x for x in canonical_sequence
    ):
        hints.append("preload_dom_strategy")
    if any("search" in x for x in canonical_sequence):
        hints.append("warmup_search_context")
    if arl_recoveries > 0 and ended_success:
        hints.append("arl_pre_arm")
        hints.append("expect_semantic_relocation")
    p.optimization_hints = _merge_hints(p.optimization_hints, hints)

    # Substring boosts (SLL)
    subs: Dict[str, float] = dict(p.substring_boosts or {})
    if "preload_dom_strategy" in p.optimization_hints:
        subs["dom"] = min(0.04, float(subs.get("dom", 0.0)) + 0.012)
    if "expect_semantic_relocation" in p.optimization_hints:
        subs["semantic"] = min(0.035, float(subs.get("semantic", 0.0)) + 0.01)
    if "expect_label_translation" in p.optimization_hints:
        subs["label"] = min(0.03, float(subs.get("label", 0.0)) + 0.01)
    p.substring_boosts = subs

    if recovery_detail:
        rkey = str(recovery_detail.get("key") or "generic")
        p.known_recovery_patterns = _merge_recovery_known(
            p.known_recovery_patterns,
            recovery_key=rkey,
            detail={k: v for k, v in recovery_detail.items() if k != "key"},
        )

    if notes:
        p.notes = (p.notes + " | " if p.notes else "") + notes[:200]

    # Predicción cacheada (top siguientes según grafo)
    p.predicted_next_patterns = [
        {"token": t, "weight": c, "from": "transition_table"}
        for t, c in predict_next_operational_tokens(list(canonical_sequence), limit=6)
    ]

    store.upsert_profile(p)
    store.append_audit(
        "consolidate",
        {"pattern_id": pid, "success": ended_success, "seq_len": len(canonical_sequence)},
    )
    _record_transitions(list(canonical_sequence))
    store.prune_excess()
    _bump_ompc_global(p, ended_success)
    return p


def predict_next_operational_tokens(
    prefix_tokens: Sequence[str],
    *,
    limit: int = 6,
) -> List[Tuple[str, int]]:
    """Siguientes tokens más frecuentes tras el prefijo (ventana corta)."""
    if not prefix_tokens:
        return []
    store = get_operational_memory_store()
    out: List[Tuple[str, int]] = []
    fk2 = _transition_from_key(list(prefix_tokens), _TRANSITION_WINDOW)
    out.extend(store.next_tokens_for(fk2, limit=limit))
    fk1 = _transition_from_key(list(prefix_tokens), 1)
    merged: Dict[str, int] = {}
    for t, c in out:
        merged[t] = merged.get(t, 0) + int(c)
    for t, c in store.next_tokens_for(fk1, limit=limit):
        merged[t] = merged.get(t, 0) + int(c)
    ranked = sorted(merged.items(), key=lambda x: -x[1])[:limit]
    return ranked


def predict_next_operational_patterns(
    prefix_tokens: Sequence[str],
    *,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """Versión explicable para Mission Review / audit."""
    total = 0
    raw = predict_next_operational_tokens(prefix_tokens, limit=limit)
    for _, c in raw:
        total += int(c)
    out: List[Dict[str, Any]] = []
    for t, c in raw:
        p = float(c) / max(1, total)
        out.append(
            {
                "next_token": t,
                "count": int(c),
                "probability": round(p, 4),
                "explain": f"P({t} | tail) ≈ {p:.2f} (evidencia local, n={total})",
            }
        )
    return out


def predict_recovery_path(
    profile: OperationalPatternProfile,
    *,
    limit: int = 4,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for r in profile.known_recovery_patterns[:limit]:
        rows.append(
            {
                "key": r.get("key"),
                "count": r.get("count"),
                "plan": r.get("detail") or {},
                "explain": f"recovery_pattern={r.get('key')} visto {r.get('count')} veces",
            }
        )
    return rows


def build_operational_memory_audit(
    *,
    mission: Optional[Any] = None,
    top_n: int = 12,
) -> Dict[str, Any]:
    store = get_operational_memory_store()
    profs = store.all_profiles()
    profs.sort(key=lambda p: (p.confidence, p.recurrence_count), reverse=True)
    items = []
    for p in profs[:top_n]:
        items.append(
            {
                "pattern_id": p.pattern_id,
                "workflow_type": p.workflow_type,
                "stability": round(p.stability_score, 4),
                "fragility": round(p.fragility_score, 4),
                "confidence": round(p.confidence, 4),
                "recurrence": p.recurrence_count,
                "hints": list(p.optimization_hints[:8]),
                "sequence_preview": " → ".join(p.canonical_sequence[:6]),
                "recoveries_known": len(p.known_recovery_patterns),
            }
        )
    return {
        "version": OMPC_STORAGE_VERSION,
        "generated_at": time.time(),
        "global_metrics": get_ompc_global_metrics(recalc=True, profiles=profs).to_dict(),
        "top_patterns": items,
        "mission_hint": (
            ompc_hints_for_mission_preview(mission) if mission is not None else None
        ),
    }


def ompc_hints_for_mission_preview(mission: Any) -> Optional[Dict[str, Any]]:
    seq = build_canonical_sequence_from_mission(mission, step_kinds_executed=[])
    if not seq:
        return None
    pid = derive_pattern_id(derive_pattern_signature(seq))
    p = get_operational_memory_store().load_profile(pid)
    if not p:
        return {
            "matched": False,
            "predicted_next": predict_next_operational_patterns(seq, limit=4),
        }
    return {
        "matched": True,
        "pattern_id": p.pattern_id,
        "optimization_hints": p.optimization_hints[:12],
        "predicted_next": predict_next_operational_patterns(seq, limit=4),
        "recovery_paths": predict_recovery_path(p, limit=3),
    }


def ompc_strategy_boosts_for_prefix(
    mission: Optional[Any],
    prior_step_kinds: Sequence[str],
    *,
    current_step_kind: str,
) -> Dict[str, float]:
    """Bonos deterministas por substring para combinar con SLL (no ejecución)."""
    cfg = _settings()
    if not cfg or not getattr(cfg, "OMPC_ENABLED", False):
        return {}
    seq = build_canonical_sequence_from_mission(
        mission, step_kinds_executed=list(prior_step_kinds) + [str(current_step_kind)]
    )
    if not seq:
        return {}
    sig = derive_pattern_signature(seq)
    pid = derive_pattern_id(sig)
    p = get_operational_memory_store().load_profile(pid)
    if not p or p.confidence < float(
        getattr(cfg, "OMPC_CONFIDENCE_THRESHOLD", 0.35) or 0.35
    ):
        # prefijo parcial: buscar mejor sufijo coincidente
        best: Optional[OperationalPatternProfile] = None
        tail = ">>".join(seq[-4:])
        for cand in get_operational_memory_store().all_profiles():
            if tail and tail in ">>".join(cand.canonical_sequence):
                best = cand
                break
        p = best
    if not p:
        return {}
    out: Dict[str, float] = {}
    for sub, bonus in (p.substring_boosts or {}).items():
        out[str(sub)] = float(bonus)
    return out


def build_canonical_sequence_from_mission(
    mission: Optional[Any],
    *,
    step_kinds_executed: Sequence[str],
) -> List[str]:
    """COBs en orden + pasos SEP ejecutados (prefijo ``cob:`` / ``sep:``)."""
    parts: List[str] = []
    if mission is None:
        return [f"sep:{k}" for k in step_kinds_executed]
    cobs = getattr(mission, "canonical_operational_blocks", None) or []
    for c in cobs:
        ca = str(getattr(c, "canonical_action", "") or "")
        bt = str(getattr(c, "block_type", "") or "")
        parts.append(f"cob:{ca}:{bt}")
    for k in step_kinds_executed:
        parts.append(f"sep:{k}")
    return parts


def _mission_session_types(mission: Optional[Any]) -> List[str]:
    out: List[str] = []
    if not mission:
        return out
    ss = getattr(mission, "semantic_sessions", None) or []
    for s in ss:
        try:
            out.append(str(getattr(s, "session_type", "") or ""))
        except Exception:
            continue
    return [x for x in out if x]


def _extract_arl_counts(mission: Optional[Any]) -> Tuple[int, int, int]:
    """(recoveries inferred, failures, layout_shift hints) desde auditoría ARL."""
    if not mission:
        return 0, 0, 0
    aud = getattr(mission, "adaptive_runtime_audit", None) or {}
    evs = aud.get("events") or []
    rec = fail = 0
    layout_hits = 0
    for e in evs:
        if not isinstance(e, dict):
            continue
        t = str(e.get("type") or "")
        b = e.get("bundle") or {}
        dev = b.get("deviation") or {}
        kinds = list(dev.get("deviation_types") or [])
        if "layout_shift" in kinds:
            layout_hits += 1
        dec = (b.get("decision") or {}).get("decision") or ""
        if t.endswith("failure") or "fail" in t:
            fail += 1
        if dec in ("CONTINUE",) and dev.get("deviation_detected"):
            rec += 1
    return rec, fail, layout_hits


def observe_smart_mission_execution(
    mission: Optional[Any],
    steps: Sequence[Any],
    result: Any,
) -> None:
    """Hook post-``run_mission`` del SmartExecutor."""
    cfg = _settings()
    if not cfg or not getattr(cfg, "OMPC_ENABLED", False):
        return
    kinds = [str(getattr(s, "kind", "") or "") for s in steps]
    outcomes = getattr(result, "outcomes", None) or []
    exec_kinds: List[str] = []
    for i, o in enumerate(outcomes):
        if getattr(o, "status", "") in ("success", "skipped"):
            if i < len(kinds):
                exec_kinds.append(kinds[i])
    seq = build_canonical_sequence_from_mission(mission, step_kinds_executed=exec_kinds)
    if not seq:
        return
    ended_ok = getattr(result, "status", "") == "success"
    try:
        dur = float(getattr(result, "duration_ms", 0))
    except Exception:
        dur = 0.0
    fb = sum(1 for o in outcomes if int(getattr(o, "retries", 0) or 0) > 1)
    ar, af, ls = _extract_arl_counts(mission)
    wf = sum(1 for o in outcomes if getattr(o, "status", "") == "failed")
    amb = 0
    try:
        from app.services.runtime.adaptive_runtime_layer import ambiguity_hint_from_shadow

        ah = ambiguity_hint_from_shadow(getattr(mission, "semantic_shadow_audit", None))
        amb = int(ah.get("count") or 0)
    except Exception:
        pass
    wt = "/".join(_mission_session_types(mission)[:3]) or "unknown"
    consolidate_operational_pattern(
        canonical_sequence=seq,
        workflow_type=wt,
        mission=mission,
        ended_success=ended_ok,
        duration_ms=dur,
        fallbacks=min(3, fb),
        arl_recoveries=min(10, ar),
        arl_failures=min(10, af),
        validator_failures=min(10, wf),
        ambiguity_events=amb,
        layout_shift_events=min(10, ls),
        recovery_detail={"key": "arl_bundle", "arl_recoveries": ar} if ar else None,
        notes="smart_executor",
        mission_terminal_failed=not ended_ok,
    )


def observe_cob_pilot_chain(
    mission: Any,
    cob: Any,
    *,
    ok: bool,
    elapsed_ms: float,
    fell_back: bool,
) -> None:
    """Refuerza patrón COB en memoria operacional."""
    cfg = _settings()
    if not cfg or not getattr(cfg, "OMPC_ENABLED", False):
        return
    ca = str(getattr(cob, "canonical_action", "") or "")
    bt = str(getattr(cob, "block_type", "") or "")
    seq = build_canonical_sequence_from_mission(mission, step_kinds_executed=[])
    # Enfatizar el COB observado al final de la secuencia base
    seq = [x for x in seq if not x.startswith(f"cob:{ca}:")] + [f"cob:{ca}:{bt}"]
    consolidate_operational_pattern(
        canonical_sequence=seq,
        workflow_type=str(getattr(cob, "block_type", "") or "cob_pilot"),
        mission=mission,
        ended_success=bool(ok),
        duration_ms=float(elapsed_ms),
        fallbacks=1 if fell_back else 0,
        arl_recoveries=0,
        validator_failures=0,
        ambiguity_events=0,
        layout_shift_events=0,
        notes="cob_pilot",
        mission_terminal_failed=not ok,
    )


def _bump_ompc_global(p: OperationalPatternProfile, success: bool) -> None:
    global _OMPC_GLOBAL
    with _OMPC_GLOBAL_LOCK:
        _OMPC_GLOBAL.session_observations += 1
        allp = get_operational_memory_store().all_profiles()
        _OMPC_GLOBAL.operational_patterns_count = len(allp)
        _OMPC_GLOBAL.stable_workflows_count = sum(
            1 for x in allp if x.stability_score >= 0.65
        )
        _OMPC_GLOBAL.fragile_workflows_count = sum(
            1 for x in allp if x.fragility_score >= 0.55
        )
        _OMPC_GLOBAL.recurring_recoveries_count = sum(
            len(x.known_recovery_patterns) for x in allp
        )
        _OMPC_GLOBAL.optimization_hint_count = sum(
            len(x.optimization_hints) for x in allp
        )
        gain = max(0.0, p.stability_score - 0.5) * 0.1 + max(0.0, 0.5 - p.fragility_score) * 0.08
        _OMPC_GLOBAL.runtime_optimization_gain_estimate = _ema(
            _OMPC_GLOBAL.runtime_optimization_gain_estimate,
            gain,
            alpha=0.12,
        )
        if success and p.predicted_next_patterns:
            _PRED_TRANS_HITS.append(1)
        elif p.predicted_next_patterns:
            _PRED_TRANS_HITS.append(0)
        tail = _PRED_TRANS_HITS[-400:]
        if tail:
            _OMPC_GLOBAL.predicted_transition_accuracy = sum(tail) / len(tail)


def get_ompc_global_metrics(
    *,
    recalc: bool = False,
    profiles: Optional[List[OperationalPatternProfile]] = None,
) -> OmpcGlobalMetrics:
    with _OMPC_GLOBAL_LOCK:
        if recalc and profiles is not None:
            g = OmpcGlobalMetrics()
            g.operational_patterns_count = len(profiles)
            g.stable_workflows_count = sum(1 for x in profiles if x.stability_score >= 0.65)
            g.fragile_workflows_count = sum(1 for x in profiles if x.fragility_score >= 0.55)
            g.recurring_recoveries_count = sum(len(x.known_recovery_patterns) for x in profiles)
            g.optimization_hint_count = sum(len(x.optimization_hints) for x in profiles)
            tail = _PRED_TRANS_HITS[-400:]
            if tail:
                g.predicted_transition_accuracy = sum(tail) / len(tail)
            g.runtime_optimization_gain_estimate = _OMPC_GLOBAL.runtime_optimization_gain_estimate
            g.session_observations = _OMPC_GLOBAL.session_observations
            return g
        return OmpcGlobalMetrics(
            operational_patterns_count=_OMPC_GLOBAL.operational_patterns_count,
            stable_workflows_count=_OMPC_GLOBAL.stable_workflows_count,
            fragile_workflows_count=_OMPC_GLOBAL.fragile_workflows_count,
            recurring_recoveries_count=_OMPC_GLOBAL.recurring_recoveries_count,
            optimization_hint_count=_OMPC_GLOBAL.optimization_hint_count,
            predicted_transition_accuracy=_OMPC_GLOBAL.predicted_transition_accuracy,
            runtime_optimization_gain_estimate=_OMPC_GLOBAL.runtime_optimization_gain_estimate,
            session_observations=_OMPC_GLOBAL.session_observations,
        )


__all__ = [
    "OMPC_STORAGE_VERSION",
    "OperationalPatternProfile",
    "OmpcGlobalMetrics",
    "OperationalMemoryStore",
    "get_operational_memory_store",
    "reset_operational_memory_store_for_tests",
    "reset_ompc_session_metrics_for_tests",
    "derive_pattern_signature",
    "derive_pattern_id",
    "classify_pattern_stability",
    "classify_pattern_fragility",
    "consolidate_operational_pattern",
    "detect_pattern_recurrence",
    "predict_next_operational_patterns",
    "predict_next_operational_tokens",
    "predict_recovery_path",
    "build_operational_memory_audit",
    "observe_smart_mission_execution",
    "observe_cob_pilot_chain",
    "ompc_strategy_boosts_for_prefix",
    "ompc_hints_for_mission_preview",
    "build_canonical_sequence_from_mission",
    "get_ompc_global_metrics",
]
