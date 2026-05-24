"""
ArthurOS Services - ExecutionMemoryStore (Auto-Learning Executor)
-----------------------------------------------------------------
Memoria estructurada de ejecución para Nevlan: cada step intentado, cada
estrategia, cada resolución de target, cada corrección humana y cada
patrón de fallo se persiste en una base **SQLite local** consultable, con
un espejo **JSONL append-only** para auditoría e ingestión externa.

Diseño:
  * SQLite es la fuente canónica (consultas analíticas baratas).
  * JSONL es opcional (auditoría, debug, migración).
  * `LearningLogger` (legacy) sigue funcionando: este store **lo compone**
    para no perder datos históricos.
  * Modo privado: si ``NEVLAN_LEARNING_PRIVATE=1`` o ``private_mode=True``,
    el store mantiene memoria SÓLO en RAM y no escribe nada a disco.
  * Thread-safe: lock por instancia + ``check_same_thread=False`` para
    permitir uso desde el hilo del executor y del UI.
  * Imports defensivos: si SQLite no está disponible (raro en Python
    estándar) caemos a un backend en memoria.

Tablas (esquema mínimo, evolutivo via PRAGMA user_version):
  - execution_runs
  - execution_step_attempts
  - strategy_scores
  - target_resolutions
  - user_corrections
  - failure_patterns
  - adaptive_timeouts (sí, vive aquí para queries cruzadas)
  - machine_profile_metrics

Integración:
  - El AutoLearningExecutor llama ``record_*`` después de cada step y
    ``finish_run`` al cerrar la misión.
  - Modules como TargetMemory, StrategyScoringEngine y AdaptiveTimeouts
    leen y escriben aquí — sin tener que conocer SQLite directamente,
    usan los métodos de alto nivel (``upsert_strategy_score``, etc).

Convenciones:
  - Timestamps en epoch float (segundos).
  - duration en milisegundos (int).
  - target/app/domain/window se normalizan a lower-case strip al guardar.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.logger import log
from app.core.paths import VAR_DIR
from app.services.missions.execution_learning import (
    LearningLogger,
    get_learning_logger,
)


_MEMORY_DIR = VAR_DIR / "missions" / "learning"
_DEFAULT_DB = _MEMORY_DIR / "execution_memory.db"
_AUDIT_JSONL = _MEMORY_DIR / "execution_memory_audit.jsonl"

_PRIVATE_ENV = "NEVLAN_LEARNING_PRIVATE"

_SCHEMA_VERSION = 1


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _norm(value: Optional[str]) -> Optional[str]:
    """Normalización suave para claves de búsqueda."""
    if value is None:
        return None
    v = str(value).strip()
    return v.lower() if v else None


def _is_private_env() -> bool:
    return os.environ.get(_PRIVATE_ENV, "").strip().lower() in {"1", "true", "yes", "y"}


# ──────────────────────────────────────────────────────────────────────
# DTOs
# ──────────────────────────────────────────────────────────────────────

@dataclass
class RunRecord:
    run_id: str
    mission_id: Optional[str]
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    status: str = "running"            # running | success | failed | needs_human
    quality_score: Optional[float] = None
    private_mode: bool = False
    machine_id: Optional[str] = None
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StepAttemptRecord:
    run_id: str
    mission_id: Optional[str]
    step_id: str
    step_kind: str
    attempt_index: int
    strategy_used: str
    success: bool
    retries: int = 0
    duration_ms: int = 0
    semantic_target: Optional[str] = None
    app: Optional[str] = None
    domain: Optional[str] = None
    window_title: Optional[str] = None
    state_before: Optional[Dict[str, Any]] = None
    state_after: Optional[Dict[str, Any]] = None
    recovery_used: Optional[str] = None
    user_intervention: bool = False
    confidence_before: Optional[float] = None
    confidence_after: Optional[float] = None
    failure_type: Optional[str] = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────────────────────────────

class ExecutionMemoryStore:
    """Memoria estructurada de ejecución (SQLite + audit JSONL).

    Métodos públicos clave:
        - start_run / finish_run
        - record_step_attempt
        - upsert_strategy_score / get_strategy_score / list_strategy_scores
        - upsert_target_resolution / get_target_resolution
        - record_user_correction / find_user_correction
        - record_failure_pattern / list_failure_patterns
        - record_timeout_observation / list_timeout_observations
        - update_machine_metric / get_machine_metrics
        - close

    Diseño "fail-soft": cualquier excepción de IO se loggea a debug y se
    silencia; el executor no debe abortar porque la memoria falle.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        private_mode: Optional[bool] = None,
        learning_logger: Optional[LearningLogger] = None,
        audit_jsonl: Optional[Path] = None,
    ) -> None:
        if private_mode is None:
            private_mode = _is_private_env()
        self.private_mode = bool(private_mode)
        self._lock = threading.RLock()
        self._learning = learning_logger or get_learning_logger()
        self._db_path: Optional[Path] = None
        self._audit_path: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None
        # Memoria interna de respaldo si no podemos abrir SQLite o
        # estamos en modo privado.
        self._mem: Dict[str, List[Dict[str, Any]]] = {
            "runs": [],
            "step_attempts": [],
            "strategy_scores": [],
            "target_resolutions": [],
            "user_corrections": [],
            "failure_patterns": [],
            "timeout_observations": [],
            "machine_metrics": [],
        }

        if not self.private_mode:
            try:
                self._db_path = Path(db_path or _DEFAULT_DB)
                self._audit_path = Path(audit_jsonl or _AUDIT_JSONL)
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                self._audit_path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(
                    str(self._db_path),
                    check_same_thread=False,
                    isolation_level=None,  # autocommit; transacciones manuales
                )
                self._conn.row_factory = sqlite3.Row
                self._init_schema()
            except Exception as e:
                log.debug(f"ExecutionMemoryStore SQLite init falló (caemos a memoria): {e}")
                self._conn = None

    # ── Propiedades útiles ──────────────────────────────────────
    @property
    def is_persistent(self) -> bool:
        return self._conn is not None and not self.private_mode

    @property
    def db_path(self) -> Optional[Path]:
        return self._db_path

    # ── Schema ──────────────────────────────────────────────────
    def _init_schema(self) -> None:
        assert self._conn is not None
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS execution_runs (
                run_id TEXT PRIMARY KEY,
                mission_id TEXT,
                started_at REAL NOT NULL,
                finished_at REAL,
                status TEXT NOT NULL,
                quality_score REAL,
                private_mode INTEGER NOT NULL DEFAULT 0,
                machine_id TEXT,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS execution_step_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                mission_id TEXT,
                step_id TEXT NOT NULL,
                step_kind TEXT NOT NULL,
                attempt_index INTEGER NOT NULL,
                strategy_used TEXT NOT NULL,
                success INTEGER NOT NULL,
                retries INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                semantic_target TEXT,
                app TEXT,
                domain TEXT,
                window_title TEXT,
                state_before TEXT,
                state_after TEXT,
                recovery_used TEXT,
                user_intervention INTEGER NOT NULL DEFAULT 0,
                confidence_before REAL,
                confidence_after REAL,
                failure_type TEXT,
                error TEXT,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_attempts_run ON execution_step_attempts(run_id);
            CREATE INDEX IF NOT EXISTS ix_attempts_kind ON execution_step_attempts(step_kind);
            CREATE INDEX IF NOT EXISTS ix_attempts_target ON execution_step_attempts(semantic_target);

            CREATE TABLE IF NOT EXISTS strategy_scores (
                step_kind TEXT NOT NULL,
                strategy TEXT NOT NULL,
                scope_app TEXT NOT NULL DEFAULT '',
                scope_domain TEXT NOT NULL DEFAULT '',
                scope_target TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 0,
                fail INTEGER NOT NULL DEFAULT 0,
                last_ok_at REAL,
                last_fail_at REAL,
                avg_duration_ms REAL,
                last_score REAL,
                PRIMARY KEY (step_kind, strategy, scope_app, scope_domain, scope_target)
            );

            CREATE TABLE IF NOT EXISTS target_resolutions (
                semantic_target TEXT NOT NULL,
                domain TEXT NOT NULL DEFAULT '',
                app TEXT NOT NULL DEFAULT '',
                successful_selectors TEXT,         -- JSON list
                successful_uia TEXT,               -- JSON dict
                ocr_anchor TEXT,
                visual_asset_hash TEXT,
                last_successful_strategy TEXT,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_success_at REAL,
                last_failure_at REAL,
                PRIMARY KEY (semantic_target, domain, app)
            );

            CREATE TABLE IF NOT EXISTS user_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                mission_id TEXT,
                step_id TEXT,
                step_kind TEXT,
                semantic_target TEXT,
                domain TEXT,
                app TEXT,
                question TEXT,
                click_x INTEGER,
                click_y INTEGER,
                target_label TEXT,
                nearest_text TEXT,
                uia_blob TEXT,                     -- JSON
                dom_blob TEXT,                     -- JSON
                visual_crop_path TEXT,
                screenshot_context_path TEXT,
                confirmation_label TEXT,
                created_at REAL NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                last_used_at REAL
            );
            CREATE INDEX IF NOT EXISTS ix_corrections_target
              ON user_corrections(step_kind, semantic_target, domain, app);

            CREATE TABLE IF NOT EXISTS failure_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                mission_id TEXT,
                step_id TEXT,
                step_kind TEXT,
                strategy TEXT,
                failure_type TEXT NOT NULL,
                error_signature TEXT,
                recovery_label TEXT,
                recovered INTEGER NOT NULL DEFAULT 0,
                count INTEGER NOT NULL DEFAULT 1,
                last_seen_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_failures_type ON failure_patterns(failure_type);

            CREATE TABLE IF NOT EXISTS timeout_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition TEXT NOT NULL,
                app TEXT NOT NULL DEFAULT '',
                domain TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL,
                success INTEGER NOT NULL,
                observed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_timeouts_cond
              ON timeout_observations(condition, app, domain);

            CREATE TABLE IF NOT EXISTS machine_metrics (
                machine_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (machine_id, metric)
            );

            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        cur.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
            (str(_SCHEMA_VERSION),),
        )

    # ── Audit ───────────────────────────────────────────────────
    def _audit(self, kind: str, payload: Dict[str, Any]) -> None:
        if self.private_mode or self._audit_path is None:
            return
        try:
            entry = {"kind": kind, "ts": time.time(), **payload}
            with self._lock:
                with open(self._audit_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            log.debug(f"audit JSONL falló (ignorado): {e}")

    # ── Runs ────────────────────────────────────────────────────
    def start_run(
        self,
        run_id: Optional[str] = None,
        *,
        mission_id: Optional[str] = None,
        machine_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> RunRecord:
        rec = RunRecord(
            run_id=run_id or str(uuid.uuid4()),
            mission_id=mission_id,
            started_at=time.time(),
            status="running",
            private_mode=self.private_mode,
            machine_id=machine_id,
            notes=notes,
        )
        if self.is_persistent:
            try:
                with self._lock:
                    self._conn.execute(  # type: ignore[union-attr]
                        """INSERT OR REPLACE INTO execution_runs
                           (run_id, mission_id, started_at, finished_at, status,
                            quality_score, private_mode, machine_id, notes)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            rec.run_id, rec.mission_id, rec.started_at, None, rec.status,
                            None, int(self.private_mode), rec.machine_id, rec.notes,
                        ),
                    )
            except Exception as e:
                log.debug(f"start_run insert falló: {e}")
        else:
            self._mem["runs"].append(rec.to_dict())
        self._audit("start_run", rec.to_dict())
        return rec

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        quality_score: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> None:
        finished_at = time.time()
        if self.is_persistent:
            try:
                with self._lock:
                    self._conn.execute(  # type: ignore[union-attr]
                        """UPDATE execution_runs
                           SET finished_at = ?, status = ?, quality_score = ?,
                               notes = COALESCE(?, notes)
                           WHERE run_id = ?""",
                        (finished_at, status, quality_score, notes, run_id),
                    )
            except Exception as e:
                log.debug(f"finish_run update falló: {e}")
        else:
            for r in self._mem["runs"]:
                if r.get("run_id") == run_id:
                    r["finished_at"] = finished_at
                    r["status"] = status
                    r["quality_score"] = quality_score
                    if notes:
                        r["notes"] = notes
                    break
        self._audit("finish_run", {
            "run_id": run_id, "status": status, "quality_score": quality_score,
        })

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        if self.is_persistent:
            try:
                with self._lock:
                    row = self._conn.execute(  # type: ignore[union-attr]
                        "SELECT * FROM execution_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    return dict(row) if row else None
            except Exception as e:
                log.debug(f"get_run falló: {e}")
                return None
        for r in self._mem["runs"]:
            if r.get("run_id") == run_id:
                return dict(r)
        return None

    def list_runs(self, mission_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if self.is_persistent:
            try:
                with self._lock:
                    if mission_id is not None:
                        rows = self._conn.execute(  # type: ignore[union-attr]
                            "SELECT * FROM execution_runs WHERE mission_id = ? "
                            "ORDER BY started_at DESC LIMIT ?",
                            (mission_id, int(limit)),
                        ).fetchall()
                    else:
                        rows = self._conn.execute(  # type: ignore[union-attr]
                            "SELECT * FROM execution_runs ORDER BY started_at DESC LIMIT ?",
                            (int(limit),),
                        ).fetchall()
                    return [dict(r) for r in rows]
            except Exception as e:
                log.debug(f"list_runs falló: {e}")
                return []
        runs = list(self._mem["runs"])
        if mission_id is not None:
            runs = [r for r in runs if r.get("mission_id") == mission_id]
        runs.sort(key=lambda r: r.get("started_at", 0), reverse=True)
        return runs[:limit]

    # ── Step attempts ──────────────────────────────────────────
    def record_step_attempt(self, rec: StepAttemptRecord) -> None:
        # Mirror al LearningLogger legacy (best-effort) para no perder
        # consumidores que ya leen sus stats. En modo privado **no**
        # espejamos: el LearningLogger global escribe a
        # ``var/missions/learning/*.jsonl`` y eso violaría la promesa
        # de cero persistencia.
        if not self.private_mode:
            try:
                if rec.success:
                    self._learning.record_success(
                        run_id=rec.run_id,
                        kind=rec.step_kind,
                        step_id=rec.step_id,
                        strategy=rec.strategy_used,
                        retries=rec.retries,
                        duration_ms=rec.duration_ms,
                        state_before=rec.state_before,
                        state_after=rec.state_after,
                        correction_applied=rec.recovery_used,
                    )
                else:
                    self._learning.record_failure(
                        run_id=rec.run_id,
                        kind=rec.step_kind,
                        step_id=rec.step_id,
                        strategy=rec.strategy_used,
                        error=rec.error or rec.failure_type or "unknown",
                        retries=rec.retries,
                        duration_ms=rec.duration_ms,
                        state_before=rec.state_before,
                        state_after=rec.state_after,
                    )
            except Exception as e:
                log.debug(f"mirror LearningLogger falló: {e}")

        if self.is_persistent:
            try:
                with self._lock:
                    self._conn.execute(  # type: ignore[union-attr]
                        """INSERT INTO execution_step_attempts
                           (run_id, mission_id, step_id, step_kind, attempt_index,
                            strategy_used, success, retries, duration_ms,
                            semantic_target, app, domain, window_title,
                            state_before, state_after, recovery_used,
                            user_intervention, confidence_before, confidence_after,
                            failure_type, error, timestamp)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            rec.run_id, rec.mission_id, rec.step_id, rec.step_kind,
                            int(rec.attempt_index), rec.strategy_used,
                            1 if rec.success else 0, int(rec.retries), int(rec.duration_ms),
                            _norm(rec.semantic_target), _norm(rec.app), _norm(rec.domain),
                            rec.window_title,
                            json.dumps(rec.state_before, ensure_ascii=False, default=str)
                                if rec.state_before else None,
                            json.dumps(rec.state_after, ensure_ascii=False, default=str)
                                if rec.state_after else None,
                            rec.recovery_used,
                            1 if rec.user_intervention else 0,
                            rec.confidence_before, rec.confidence_after,
                            rec.failure_type, (rec.error or "")[:500] if rec.error else None,
                            rec.timestamp,
                        ),
                    )
            except Exception as e:
                log.debug(f"record_step_attempt insert falló: {e}")
        else:
            self._mem["step_attempts"].append(rec.to_dict())
        self._audit("step_attempt", rec.to_dict())

    def list_step_attempts(
        self, run_id: Optional[str] = None, *, limit: int = 500
    ) -> List[Dict[str, Any]]:
        if self.is_persistent:
            try:
                with self._lock:
                    if run_id:
                        rows = self._conn.execute(  # type: ignore[union-attr]
                            "SELECT * FROM execution_step_attempts WHERE run_id = ? "
                            "ORDER BY id ASC LIMIT ?", (run_id, int(limit)),
                        ).fetchall()
                    else:
                        rows = self._conn.execute(  # type: ignore[union-attr]
                            "SELECT * FROM execution_step_attempts ORDER BY id DESC LIMIT ?",
                            (int(limit),),
                        ).fetchall()
                    return [dict(r) for r in rows]
            except Exception as e:
                log.debug(f"list_step_attempts falló: {e}")
                return []
        rows = list(self._mem["step_attempts"])
        if run_id:
            rows = [r for r in rows if r.get("run_id") == run_id]
        return rows[:limit]

    # ── Strategy scores ────────────────────────────────────────
    def upsert_strategy_score(
        self,
        *,
        step_kind: str,
        strategy: str,
        success: bool,
        duration_ms: int = 0,
        scope_app: Optional[str] = None,
        scope_domain: Optional[str] = None,
        scope_target: Optional[str] = None,
        last_score: Optional[float] = None,
    ) -> None:
        scope_app = _norm(scope_app) or ""
        scope_domain = _norm(scope_domain) or ""
        scope_target = _norm(scope_target) or ""
        now = time.time()

        if self.is_persistent:
            try:
                with self._lock:
                    cur = self._conn.execute(  # type: ignore[union-attr]
                        """SELECT ok, fail, avg_duration_ms FROM strategy_scores
                           WHERE step_kind=? AND strategy=? AND scope_app=?
                             AND scope_domain=? AND scope_target=?""",
                        (step_kind, strategy, scope_app, scope_domain, scope_target),
                    ).fetchone()
                    if cur is None:
                        ok = 1 if success else 0
                        fail = 0 if success else 1
                        avg = float(duration_ms)
                        self._conn.execute(  # type: ignore[union-attr]
                            """INSERT INTO strategy_scores
                               (step_kind, strategy, scope_app, scope_domain, scope_target,
                                ok, fail, last_ok_at, last_fail_at, avg_duration_ms, last_score)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                step_kind, strategy, scope_app, scope_domain, scope_target,
                                ok, fail,
                                now if success else None,
                                None if success else now,
                                avg, last_score,
                            ),
                        )
                    else:
                        ok = int(cur["ok"]) + (1 if success else 0)
                        fail = int(cur["fail"]) + (0 if success else 1)
                        prev_avg = float(cur["avg_duration_ms"] or 0.0)
                        n = ok + fail
                        new_avg = (prev_avg * (n - 1) + float(duration_ms)) / max(n, 1)
                        self._conn.execute(  # type: ignore[union-attr]
                            """UPDATE strategy_scores
                               SET ok=?, fail=?,
                                   last_ok_at = CASE WHEN ?=1 THEN ? ELSE last_ok_at END,
                                   last_fail_at = CASE WHEN ?=0 THEN ? ELSE last_fail_at END,
                                   avg_duration_ms=?, last_score=COALESCE(?, last_score)
                               WHERE step_kind=? AND strategy=? AND scope_app=?
                                 AND scope_domain=? AND scope_target=?""",
                            (
                                ok, fail,
                                1 if success else 0, now,
                                0 if success else 1, now,
                                new_avg, last_score,
                                step_kind, strategy, scope_app, scope_domain, scope_target,
                            ),
                        )
            except Exception as e:
                log.debug(f"upsert_strategy_score falló: {e}")
        else:
            row = self._find_score_row(step_kind, strategy, scope_app, scope_domain, scope_target)
            if row is None:
                row = {
                    "step_kind": step_kind, "strategy": strategy,
                    "scope_app": scope_app, "scope_domain": scope_domain,
                    "scope_target": scope_target,
                    "ok": 0, "fail": 0,
                    "last_ok_at": None, "last_fail_at": None,
                    "avg_duration_ms": 0.0, "last_score": last_score,
                }
                self._mem["strategy_scores"].append(row)
            if success:
                row["ok"] += 1
                row["last_ok_at"] = now
            else:
                row["fail"] += 1
                row["last_fail_at"] = now
            n = row["ok"] + row["fail"]
            row["avg_duration_ms"] = (
                (row["avg_duration_ms"] * (n - 1) + duration_ms) / max(n, 1)
            )
            if last_score is not None:
                row["last_score"] = last_score

    def _find_score_row(
        self, step_kind: str, strategy: str,
        scope_app: str, scope_domain: str, scope_target: str,
    ) -> Optional[Dict[str, Any]]:
        for r in self._mem["strategy_scores"]:
            if (r["step_kind"] == step_kind and r["strategy"] == strategy
                and r["scope_app"] == scope_app and r["scope_domain"] == scope_domain
                    and r["scope_target"] == scope_target):
                return r
        return None

    def list_strategy_scores(
        self,
        *,
        step_kind: Optional[str] = None,
        scope_app: Optional[str] = None,
        scope_domain: Optional[str] = None,
        scope_target: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        scope_app = _norm(scope_app) or ""
        scope_domain = _norm(scope_domain) or ""
        scope_target = _norm(scope_target) or ""
        if self.is_persistent:
            try:
                with self._lock:
                    sql = "SELECT * FROM strategy_scores WHERE 1=1"
                    args: List[Any] = []
                    if step_kind is not None:
                        sql += " AND step_kind = ?"
                        args.append(step_kind)
                    sql += (" AND scope_app = ? AND scope_domain = ? AND scope_target = ?")
                    args.extend([scope_app, scope_domain, scope_target])
                    rows = self._conn.execute(sql, args).fetchall()  # type: ignore[union-attr]
                    return [dict(r) for r in rows]
            except Exception as e:
                log.debug(f"list_strategy_scores falló: {e}")
                return []
        out: List[Dict[str, Any]] = []
        for r in self._mem["strategy_scores"]:
            if step_kind is not None and r["step_kind"] != step_kind:
                continue
            if r["scope_app"] != scope_app:
                continue
            if r["scope_domain"] != scope_domain:
                continue
            if r["scope_target"] != scope_target:
                continue
            out.append(dict(r))
        return out

    def get_strategy_score(
        self, *, step_kind: str, strategy: str,
        scope_app: Optional[str] = None,
        scope_domain: Optional[str] = None,
        scope_target: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        rows = self.list_strategy_scores(
            step_kind=step_kind, scope_app=scope_app,
            scope_domain=scope_domain, scope_target=scope_target,
        )
        for r in rows:
            if r["strategy"] == strategy:
                return r
        return None

    # ── Target memory ──────────────────────────────────────────
    def upsert_target_resolution(
        self,
        *,
        semantic_target: str,
        domain: Optional[str] = None,
        app: Optional[str] = None,
        successful_selectors: Optional[List[str]] = None,
        successful_uia: Optional[Dict[str, Any]] = None,
        ocr_anchor: Optional[str] = None,
        visual_asset_hash: Optional[str] = None,
        last_successful_strategy: Optional[str] = None,
        success: bool = True,
    ) -> None:
        domain = _norm(domain) or ""
        app = _norm(app) or ""
        target = _norm(semantic_target) or ""
        if not target:
            return
        now = time.time()

        if self.is_persistent:
            try:
                with self._lock:
                    cur = self._conn.execute(  # type: ignore[union-attr]
                        """SELECT successful_selectors, successful_uia, success_count, failure_count
                           FROM target_resolutions
                           WHERE semantic_target=? AND domain=? AND app=?""",
                        (target, domain, app),
                    ).fetchone()
                    sel_list = list(successful_selectors or [])
                    if cur is not None and cur["successful_selectors"]:
                        try:
                            existing = json.loads(cur["successful_selectors"])
                        except Exception:
                            existing = []
                        # Merge sin duplicados, manteniendo orden histórico.
                        merged = list(existing)
                        for s in sel_list:
                            if s and s not in merged:
                                merged.append(s)
                        sel_list = merged
                    uia_blob = successful_uia
                    if cur is not None and cur["successful_uia"]:
                        try:
                            uia_existing = json.loads(cur["successful_uia"])
                        except Exception:
                            uia_existing = None
                        if isinstance(uia_existing, dict) and isinstance(uia_blob, dict):
                            merged_u = dict(uia_existing)
                            merged_u.update(uia_blob)
                            uia_blob = merged_u
                        elif uia_blob is None:
                            uia_blob = uia_existing
                    success_count = (int(cur["success_count"]) if cur else 0) + (1 if success else 0)
                    failure_count = (int(cur["failure_count"]) if cur else 0) + (0 if success else 1)

                    if cur is None:
                        self._conn.execute(  # type: ignore[union-attr]
                            """INSERT INTO target_resolutions
                               (semantic_target, domain, app, successful_selectors,
                                successful_uia, ocr_anchor, visual_asset_hash,
                                last_successful_strategy, success_count, failure_count,
                                last_success_at, last_failure_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                target, domain, app,
                                json.dumps(sel_list, ensure_ascii=False) if sel_list else None,
                                json.dumps(uia_blob, ensure_ascii=False, default=str) if uia_blob else None,
                                ocr_anchor, visual_asset_hash,
                                last_successful_strategy if success else None,
                                success_count, failure_count,
                                now if success else None,
                                None if success else now,
                            ),
                        )
                    else:
                        self._conn.execute(  # type: ignore[union-attr]
                            """UPDATE target_resolutions
                               SET successful_selectors=?,
                                   successful_uia=?,
                                   ocr_anchor=COALESCE(?, ocr_anchor),
                                   visual_asset_hash=COALESCE(?, visual_asset_hash),
                                   last_successful_strategy=CASE WHEN ?=1 THEN COALESCE(?, last_successful_strategy)
                                                                  ELSE last_successful_strategy END,
                                   success_count=?, failure_count=?,
                                   last_success_at=CASE WHEN ?=1 THEN ? ELSE last_success_at END,
                                   last_failure_at=CASE WHEN ?=0 THEN ? ELSE last_failure_at END
                               WHERE semantic_target=? AND domain=? AND app=?""",
                            (
                                json.dumps(sel_list, ensure_ascii=False) if sel_list else None,
                                json.dumps(uia_blob, ensure_ascii=False, default=str) if uia_blob else None,
                                ocr_anchor, visual_asset_hash,
                                1 if success else 0, last_successful_strategy,
                                success_count, failure_count,
                                1 if success else 0, now,
                                0 if success else 1, now,
                                target, domain, app,
                            ),
                        )
            except Exception as e:
                log.debug(f"upsert_target_resolution falló: {e}")
        else:
            row = self._find_target_row(target, domain, app)
            if row is None:
                row = {
                    "semantic_target": target, "domain": domain, "app": app,
                    "successful_selectors": [], "successful_uia": {},
                    "ocr_anchor": None, "visual_asset_hash": None,
                    "last_successful_strategy": None,
                    "success_count": 0, "failure_count": 0,
                    "last_success_at": None, "last_failure_at": None,
                }
                self._mem["target_resolutions"].append(row)
            for s in (successful_selectors or []):
                if s and s not in row["successful_selectors"]:
                    row["successful_selectors"].append(s)
            if isinstance(successful_uia, dict):
                row["successful_uia"].update(successful_uia)
            if ocr_anchor:
                row["ocr_anchor"] = ocr_anchor
            if visual_asset_hash:
                row["visual_asset_hash"] = visual_asset_hash
            if success:
                row["success_count"] += 1
                row["last_success_at"] = now
                if last_successful_strategy:
                    row["last_successful_strategy"] = last_successful_strategy
            else:
                row["failure_count"] += 1
                row["last_failure_at"] = now

    def _find_target_row(
        self, target: str, domain: str, app: str
    ) -> Optional[Dict[str, Any]]:
        for r in self._mem["target_resolutions"]:
            if (r["semantic_target"] == target and r["domain"] == domain
                    and r["app"] == app):
                return r
        return None

    def get_target_resolution(
        self, *, semantic_target: str,
        domain: Optional[str] = None, app: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        domain = _norm(domain) or ""
        app = _norm(app) or ""
        target = _norm(semantic_target) or ""
        if not target:
            return None
        if self.is_persistent:
            try:
                with self._lock:
                    row = self._conn.execute(  # type: ignore[union-attr]
                        """SELECT * FROM target_resolutions
                           WHERE semantic_target=? AND domain=? AND app=?""",
                        (target, domain, app),
                    ).fetchone()
                if row is None:
                    return None
                d = dict(row)
                if d.get("successful_selectors"):
                    try:
                        d["successful_selectors"] = json.loads(d["successful_selectors"])
                    except Exception:
                        pass
                if d.get("successful_uia"):
                    try:
                        d["successful_uia"] = json.loads(d["successful_uia"])
                    except Exception:
                        pass
                return d
            except Exception as e:
                log.debug(f"get_target_resolution falló: {e}")
                return None
        row = self._find_target_row(target, domain, app)
        return dict(row) if row else None

    # ── User corrections ───────────────────────────────────────
    def record_user_correction(self, payload: Dict[str, Any]) -> int:
        payload = dict(payload)
        payload.setdefault("created_at", time.time())
        payload["semantic_target"] = _norm(payload.get("semantic_target"))
        payload["domain"] = _norm(payload.get("domain"))
        payload["app"] = _norm(payload.get("app"))
        for k in ("uia_blob", "dom_blob"):
            v = payload.get(k)
            if v is not None and not isinstance(v, str):
                payload[k] = json.dumps(v, ensure_ascii=False, default=str)
        if self.is_persistent:
            try:
                with self._lock:
                    cur = self._conn.execute(  # type: ignore[union-attr]
                        """INSERT INTO user_corrections
                           (run_id, mission_id, step_id, step_kind, semantic_target,
                            domain, app, question, click_x, click_y, target_label,
                            nearest_text, uia_blob, dom_blob, visual_crop_path,
                            screenshot_context_path, confirmation_label, created_at,
                            used_count, last_used_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)""",
                        (
                            payload.get("run_id"), payload.get("mission_id"),
                            payload.get("step_id"), payload.get("step_kind"),
                            payload.get("semantic_target"),
                            payload.get("domain"), payload.get("app"),
                            payload.get("question"),
                            payload.get("click_x"), payload.get("click_y"),
                            payload.get("target_label"), payload.get("nearest_text"),
                            payload.get("uia_blob"), payload.get("dom_blob"),
                            payload.get("visual_crop_path"),
                            payload.get("screenshot_context_path"),
                            payload.get("confirmation_label"),
                            payload["created_at"],
                        ),
                    )
                    return int(cur.lastrowid or 0)
            except Exception as e:
                log.debug(f"record_user_correction falló: {e}")
                return 0
        payload["id"] = len(self._mem["user_corrections"]) + 1
        payload["used_count"] = 0
        payload["last_used_at"] = None
        self._mem["user_corrections"].append(payload)
        return int(payload["id"])

    def find_user_correction(
        self,
        *,
        step_kind: Optional[str],
        semantic_target: Optional[str],
        domain: Optional[str] = None,
        app: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        target = _norm(semantic_target)
        domain = _norm(domain)
        app = _norm(app)
        if self.is_persistent:
            try:
                with self._lock:
                    sql = ("SELECT * FROM user_corrections "
                           "WHERE 1=1")
                    args: List[Any] = []
                    if step_kind is not None:
                        sql += " AND step_kind = ?"
                        args.append(step_kind)
                    if target is not None:
                        sql += " AND semantic_target = ?"
                        args.append(target)
                    if domain is not None:
                        sql += " AND domain = ?"
                        args.append(domain)
                    if app is not None:
                        sql += " AND app = ?"
                        args.append(app)
                    sql += " ORDER BY created_at DESC LIMIT 1"
                    row = self._conn.execute(sql, args).fetchone()  # type: ignore[union-attr]
                if row is None:
                    return None
                d = dict(row)
                for k in ("uia_blob", "dom_blob"):
                    if d.get(k):
                        try:
                            d[k] = json.loads(d[k])
                        except Exception:
                            pass
                return d
            except Exception as e:
                log.debug(f"find_user_correction falló: {e}")
                return None
        candidates = list(self._mem["user_corrections"])
        candidates.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        for r in candidates:
            if step_kind is not None and r.get("step_kind") != step_kind:
                continue
            if target is not None and r.get("semantic_target") != target:
                continue
            if domain is not None and r.get("domain") != domain:
                continue
            if app is not None and r.get("app") != app:
                continue
            return dict(r)
        return None

    def mark_user_correction_used(self, correction_id: int) -> None:
        now = time.time()
        if self.is_persistent:
            try:
                with self._lock:
                    self._conn.execute(  # type: ignore[union-attr]
                        "UPDATE user_corrections SET used_count = used_count + 1, last_used_at = ? "
                        "WHERE id = ?",
                        (now, int(correction_id)),
                    )
            except Exception as e:
                log.debug(f"mark_user_correction_used falló: {e}")
            return
        for r in self._mem["user_corrections"]:
            if int(r.get("id", -1)) == int(correction_id):
                r["used_count"] = int(r.get("used_count", 0)) + 1
                r["last_used_at"] = now
                return

    # ── Failure patterns ───────────────────────────────────────
    def record_failure_pattern(
        self,
        *,
        run_id: Optional[str],
        mission_id: Optional[str],
        step_id: Optional[str],
        step_kind: Optional[str],
        strategy: Optional[str],
        failure_type: str,
        error_signature: Optional[str] = None,
        recovery_label: Optional[str] = None,
        recovered: bool = False,
    ) -> None:
        now = time.time()
        if self.is_persistent:
            try:
                with self._lock:
                    cur = self._conn.execute(  # type: ignore[union-attr]
                        """SELECT id, count FROM failure_patterns
                           WHERE step_kind IS ? AND strategy IS ? AND failure_type = ?
                             AND error_signature IS ?""",
                        (step_kind, strategy, failure_type, error_signature),
                    ).fetchone()
                    if cur:
                        self._conn.execute(  # type: ignore[union-attr]
                            "UPDATE failure_patterns SET count = count + 1, "
                            "last_seen_at = ?, recovered = recovered OR ?, "
                            "recovery_label = COALESCE(?, recovery_label) "
                            "WHERE id = ?",
                            (now, 1 if recovered else 0, recovery_label, int(cur["id"])),
                        )
                    else:
                        self._conn.execute(  # type: ignore[union-attr]
                            """INSERT INTO failure_patterns
                               (run_id, mission_id, step_id, step_kind, strategy,
                                failure_type, error_signature, recovery_label,
                                recovered, count, last_seen_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                            (
                                run_id, mission_id, step_id, step_kind, strategy,
                                failure_type, error_signature, recovery_label,
                                1 if recovered else 0, now,
                            ),
                        )
            except Exception as e:
                log.debug(f"record_failure_pattern falló: {e}")
            return
        # in-memory
        for r in self._mem["failure_patterns"]:
            if (r.get("step_kind") == step_kind and r.get("strategy") == strategy
                and r.get("failure_type") == failure_type
                    and r.get("error_signature") == error_signature):
                r["count"] = int(r.get("count", 0)) + 1
                r["last_seen_at"] = now
                if recovered:
                    r["recovered"] = True
                if recovery_label and not r.get("recovery_label"):
                    r["recovery_label"] = recovery_label
                return
        self._mem["failure_patterns"].append({
            "id": len(self._mem["failure_patterns"]) + 1,
            "run_id": run_id, "mission_id": mission_id, "step_id": step_id,
            "step_kind": step_kind, "strategy": strategy,
            "failure_type": failure_type, "error_signature": error_signature,
            "recovery_label": recovery_label,
            "recovered": bool(recovered), "count": 1, "last_seen_at": now,
        })

    def list_failure_patterns(
        self, *, step_kind: Optional[str] = None,
        failure_type: Optional[str] = None, limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if self.is_persistent:
            try:
                with self._lock:
                    sql = "SELECT * FROM failure_patterns WHERE 1=1"
                    args: List[Any] = []
                    if step_kind is not None:
                        sql += " AND step_kind = ?"
                        args.append(step_kind)
                    if failure_type is not None:
                        sql += " AND failure_type = ?"
                        args.append(failure_type)
                    sql += " ORDER BY count DESC, last_seen_at DESC LIMIT ?"
                    args.append(int(limit))
                    rows = self._conn.execute(sql, args).fetchall()  # type: ignore[union-attr]
                    return [dict(r) for r in rows]
            except Exception as e:
                log.debug(f"list_failure_patterns falló: {e}")
                return []
        rows = list(self._mem["failure_patterns"])
        if step_kind is not None:
            rows = [r for r in rows if r.get("step_kind") == step_kind]
        if failure_type is not None:
            rows = [r for r in rows if r.get("failure_type") == failure_type]
        rows.sort(key=lambda r: (r.get("count", 0), r.get("last_seen_at", 0)), reverse=True)
        return rows[:limit]

    # ── Timeout observations ───────────────────────────────────
    def record_timeout_observation(
        self,
        *,
        condition: str,
        duration_ms: int,
        success: bool,
        app: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> None:
        app = _norm(app) or ""
        domain = _norm(domain) or ""
        now = time.time()
        if self.is_persistent:
            try:
                with self._lock:
                    self._conn.execute(  # type: ignore[union-attr]
                        """INSERT INTO timeout_observations
                           (condition, app, domain, duration_ms, success, observed_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (condition, app, domain, int(duration_ms), 1 if success else 0, now),
                    )
            except Exception as e:
                log.debug(f"record_timeout_observation falló: {e}")
            return
        self._mem["timeout_observations"].append({
            "condition": condition, "app": app, "domain": domain,
            "duration_ms": int(duration_ms),
            "success": bool(success), "observed_at": now,
        })

    def list_timeout_observations(
        self, *, condition: str,
        app: Optional[str] = None, domain: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        app = _norm(app) or ""
        domain = _norm(domain) or ""
        if self.is_persistent:
            try:
                with self._lock:
                    rows = self._conn.execute(  # type: ignore[union-attr]
                        """SELECT * FROM timeout_observations
                           WHERE condition=? AND app=? AND domain=?
                           ORDER BY observed_at DESC LIMIT ?""",
                        (condition, app, domain, int(limit)),
                    ).fetchall()
                    return [dict(r) for r in rows]
            except Exception as e:
                log.debug(f"list_timeout_observations falló: {e}")
                return []
        rows = [
            r for r in self._mem["timeout_observations"]
            if r.get("condition") == condition and r.get("app") == app
            and r.get("domain") == domain
        ]
        rows.sort(key=lambda r: r.get("observed_at", 0), reverse=True)
        return rows[:limit]

    # ── Machine metrics ───────────────────────────────────────
    def update_machine_metric(
        self, *, machine_id: str, metric: str, value: float
    ) -> None:
        now = time.time()
        if self.is_persistent:
            try:
                with self._lock:
                    self._conn.execute(  # type: ignore[union-attr]
                        """INSERT INTO machine_metrics
                           (machine_id, metric, value, updated_at)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(machine_id, metric) DO UPDATE SET
                             value = excluded.value, updated_at = excluded.updated_at""",
                        (machine_id, metric, float(value), now),
                    )
            except Exception as e:
                log.debug(f"update_machine_metric falló: {e}")
            return
        for r in self._mem["machine_metrics"]:
            if r.get("machine_id") == machine_id and r.get("metric") == metric:
                r["value"] = float(value)
                r["updated_at"] = now
                return
        self._mem["machine_metrics"].append({
            "machine_id": machine_id, "metric": metric,
            "value": float(value), "updated_at": now,
        })

    def get_machine_metrics(self, machine_id: str) -> Dict[str, float]:
        if self.is_persistent:
            try:
                with self._lock:
                    rows = self._conn.execute(  # type: ignore[union-attr]
                        "SELECT metric, value FROM machine_metrics WHERE machine_id=?",
                        (machine_id,),
                    ).fetchall()
                    return {r["metric"]: float(r["value"]) for r in rows}
            except Exception as e:
                log.debug(f"get_machine_metrics falló: {e}")
                return {}
        return {
            r["metric"]: float(r["value"])
            for r in self._mem["machine_metrics"]
            if r.get("machine_id") == machine_id
        }

    # ── Privacidad ─────────────────────────────────────────────
    def forget_all(self) -> None:
        """Borra todo el aprendizaje persistido (modo "olvidar")."""
        if self.is_persistent:
            try:
                with self._lock:
                    cur = self._conn.cursor()  # type: ignore[union-attr]
                    for table in (
                        "execution_runs", "execution_step_attempts",
                        "strategy_scores", "target_resolutions",
                        "user_corrections", "failure_patterns",
                        "timeout_observations", "machine_metrics",
                    ):
                        cur.execute(f"DELETE FROM {table}")
            except Exception as e:
                log.debug(f"forget_all falló: {e}")
        for k in self._mem:
            self._mem[k] = []
        self._audit("forget_all", {})

    # ── Cierre ─────────────────────────────────────────────────
    def close(self) -> None:
        if self._conn is not None:
            try:
                with self._lock:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────

_default_store: Optional[ExecutionMemoryStore] = None
_default_lock = threading.Lock()


def get_execution_memory_store() -> ExecutionMemoryStore:
    """Devuelve un store compartido por proceso."""
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                _default_store = ExecutionMemoryStore()
    return _default_store


def reset_execution_memory_store_for_tests() -> None:
    """Permite a los tests cerrar el singleton y reinstanciar."""
    global _default_store
    with _default_lock:
        if _default_store is not None:
            try:
                _default_store.close()
            except Exception:
                pass
        _default_store = None


__all__ = [
    "ExecutionMemoryStore",
    "RunRecord",
    "StepAttemptRecord",
    "get_execution_memory_store",
    "reset_execution_memory_store_for_tests",
]
