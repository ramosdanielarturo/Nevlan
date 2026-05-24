"""
Tests for ExecutionMemoryStore (Auto-Learning Executor).

Cubre:
  - SQLite schema se crea
  - persistencia de runs / step_attempts / strategy_scores / target_resolutions
  - modo privado: nada se persiste a disco
  - mirror al LearningLogger legacy
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


def _fresh_store(tmp_path, *, private=False):
    from app.services.missions.execution_memory_store import (
        ExecutionMemoryStore,
    )
    db = tmp_path / "exec.db"
    audit = tmp_path / "audit.jsonl"
    return ExecutionMemoryStore(
        db_path=db, audit_jsonl=audit, private_mode=private,
    ), db, audit


class TestExecutionMemoryStore:
    def test_schema_created(self, tmp_path):
        store, db, _ = _fresh_store(tmp_path)
        assert db.exists()
        assert store.is_persistent
        # Verify expected tables exist.
        with sqlite3.connect(db) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        for t in (
            "execution_runs", "execution_step_attempts", "strategy_scores",
            "target_resolutions", "user_corrections", "failure_patterns",
            "timeout_observations", "machine_metrics",
        ):
            assert t in tables, f"missing table {t}"
        store.close()

    def test_runs_persist_and_finish(self, tmp_path):
        store, _, _ = _fresh_store(tmp_path)
        rec = store.start_run(mission_id="mid")
        store.finish_run(rec.run_id, status="success", quality_score=0.92)
        got = store.get_run(rec.run_id)
        assert got is not None
        assert got["status"] == "success"
        assert got["quality_score"] == 0.92
        assert got["mission_id"] == "mid"
        store.close()

    def test_step_attempt_records_and_lists(self, tmp_path):
        from app.services.missions.execution_memory_store import (
            StepAttemptRecord,
        )
        store, _, _ = _fresh_store(tmp_path)
        run = store.start_run(mission_id="m1")
        rec = StepAttemptRecord(
            run_id=run.run_id, mission_id="m1",
            step_id="s1", step_kind="open_url",
            attempt_index=1, strategy_used="cdp_open",
            success=True, retries=0, duration_ms=120,
            semantic_target="url:youtube",
            app="chrome.exe", domain="youtube.com",
        )
        store.record_step_attempt(rec)
        rows = store.list_step_attempts(run.run_id)
        assert len(rows) == 1
        assert rows[0]["strategy_used"] == "cdp_open"
        assert rows[0]["success"] == 1
        store.close()

    def test_strategy_scores_upsert(self, tmp_path):
        store, _, _ = _fresh_store(tmp_path)
        for _ in range(3):
            store.upsert_strategy_score(
                step_kind="search", strategy="dom_input",
                success=True, duration_ms=300,
                scope_app="chrome.exe", scope_domain="youtube.com",
                scope_target="youtube_search_box",
            )
        for _ in range(1):
            store.upsert_strategy_score(
                step_kind="search", strategy="dom_input",
                success=False, duration_ms=2000,
                scope_app="chrome.exe", scope_domain="youtube.com",
                scope_target="youtube_search_box",
            )
        row = store.get_strategy_score(
            step_kind="search", strategy="dom_input",
            scope_app="chrome.exe", scope_domain="youtube.com",
            scope_target="youtube_search_box",
        )
        assert row is not None
        assert row["ok"] == 3
        assert row["fail"] == 1
        assert (row["avg_duration_ms"] or 0) > 0
        store.close()

    def test_target_resolution_merges_selectors(self, tmp_path):
        store, _, _ = _fresh_store(tmp_path)
        store.upsert_target_resolution(
            semantic_target="youtube_search_box",
            domain="youtube.com", app="chrome.exe",
            successful_selectors=["input[name='search_query']"],
            success=True, last_successful_strategy="dom_input",
        )
        store.upsert_target_resolution(
            semantic_target="youtube_search_box",
            domain="youtube.com", app="chrome.exe",
            successful_selectors=["#search-input input"],
            success=True, last_successful_strategy="dom_input",
        )
        got = store.get_target_resolution(
            semantic_target="youtube_search_box",
            domain="youtube.com", app="chrome.exe",
        )
        assert got is not None
        sels = got["successful_selectors"]
        assert "input[name='search_query']" in sels
        assert "#search-input input" in sels
        assert got["success_count"] == 2
        store.close()

    def test_private_mode_does_not_persist(self, tmp_path, monkeypatch):
        """Caso de aceptación #10: modo privado no persiste aprendizaje."""
        store, db, audit = _fresh_store(tmp_path, private=True)
        assert not store.is_persistent
        rec = store.start_run(mission_id="m_priv")
        from app.services.missions.execution_memory_store import (
            StepAttemptRecord,
        )
        store.record_step_attempt(StepAttemptRecord(
            run_id=rec.run_id, mission_id="m_priv",
            step_id="s1", step_kind="open_url",
            attempt_index=1, strategy_used="cdp_open",
            success=True, duration_ms=100,
        ))
        store.upsert_strategy_score(
            step_kind="open_url", strategy="cdp_open",
            success=True, duration_ms=100,
        )
        # Nada debería haberse escrito al disco.
        assert not db.exists()
        assert not audit.exists()
        # Pero la consulta in-memory debe seguir funcionando.
        got = store.get_strategy_score(step_kind="open_url", strategy="cdp_open")
        assert got is not None
        assert got["ok"] == 1
        store.close()

    def test_audit_jsonl_written(self, tmp_path):
        store, _, audit = _fresh_store(tmp_path)
        store.start_run(mission_id="m_audit")
        assert audit.exists()
        content = audit.read_text(encoding="utf-8").strip().splitlines()
        assert any('"start_run"' in line for line in content)
        store.close()

    def test_forget_all_clears_persistence(self, tmp_path):
        store, _, _ = _fresh_store(tmp_path)
        rec = store.start_run(mission_id="m1")
        store.upsert_strategy_score(
            step_kind="open_url", strategy="cdp_open", success=True,
        )
        store.forget_all()
        assert store.get_strategy_score(step_kind="open_url", strategy="cdp_open") is None
        assert store.get_run(rec.run_id) is None
        store.close()

    def test_mirror_to_learning_logger(self, tmp_path):
        """El store debe espejar éxito/fallo al LearningLogger legacy."""
        from app.services.missions.execution_learning import LearningLogger
        from app.services.missions.execution_memory_store import (
            ExecutionMemoryStore, StepAttemptRecord,
        )
        learner = LearningLogger.__new__(LearningLogger)
        # Reinicializa sin tocar disco real:
        import threading
        learner._lock = threading.RLock()
        learner._stats = {}
        # Mockeamos _append/_save_stats para que no toque archivos.
        learner._append = lambda rec: None  # type: ignore[attr-defined]
        learner._save_stats = lambda: None  # type: ignore[attr-defined]

        store = ExecutionMemoryStore(
            db_path=tmp_path / "x.db",
            audit_jsonl=tmp_path / "x.jsonl",
            learning_logger=learner,
        )
        rec = StepAttemptRecord(
            run_id="r1", mission_id="m1", step_id="s1", step_kind="open_url",
            attempt_index=1, strategy_used="cdp_open",
            success=True, duration_ms=100,
        )
        store.record_step_attempt(rec)
        # El learner legacy debería haber acumulado el ok.
        stats = learner.stats_for("open_url")
        assert "cdp_open" in stats
        assert int(stats["cdp_open"]["ok"]) == 1
        store.close()
