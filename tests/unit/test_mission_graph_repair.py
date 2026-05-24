"""
Tests for MissionGraphRepair (Auto-Learning Executor).

Cubre caso #9: misión sugiere actualizar preferred_strategy.
"""
from __future__ import annotations

import time

import pytest


def _build(tmp_path):
    from app.services.missions.execution_memory_store import (
        ExecutionMemoryStore, StepAttemptRecord,
    )
    from app.services.missions.strategy_scoring import StrategyScoringEngine
    from app.services.missions.mission_graph_repair import MissionGraphRepair
    from app.services.missions.execution_learning import LearningLogger
    import threading

    store = ExecutionMemoryStore(
        db_path=tmp_path / "rp.db", audit_jsonl=tmp_path / "rp.jsonl",
    )
    learner = LearningLogger.__new__(LearningLogger)
    learner._lock = threading.RLock()
    learner._stats = {}
    learner._append = lambda rec: None  # type: ignore
    learner._save_stats = lambda: None  # type: ignore
    scoring = StrategyScoringEngine(store=store, learning=learner)
    repair = MissionGraphRepair(store=store, scoring=scoring)
    return store, scoring, repair


def _seed_attempts(store, *, kind, strategy, success, n, app=None, domain=None):
    """Inserta N attempts para crear streak / contar racha."""
    from app.services.missions.execution_memory_store import StepAttemptRecord
    now = time.time()
    for i in range(n):
        rec = StepAttemptRecord(
            run_id=f"r-{kind}-{strategy}-{i}",
            mission_id="m1",
            step_id="s1", step_kind=kind, attempt_index=1,
            strategy_used=strategy, success=success, retries=0,
            duration_ms=200,
            app=app, domain=domain,
            timestamp=now - (n - i),
        )
        store.record_step_attempt(rec)


class TestMissionGraphRepair:
    def test_no_suggestion_without_data(self, tmp_path):
        _, _, repair = _build(tmp_path)
        s = repair.evaluate(
            mission_id="m1", step_kind="open_url",
            current_preferred="bookmark_click",
            candidates=["omnibox_url", "click"],
        )
        assert s is None

    def test_suggests_when_alternative_clearly_better(self, tmp_path):
        """Caso aceptación #9: alt funciona 10 veces, preferred falla
        repetidamente → debería sugerir actualizar."""
        store, _, repair = _build(tmp_path)
        # Preferred (bookmark) muchos fail
        for _ in range(8):
            store.upsert_strategy_score(
                step_kind="open_url", strategy="bookmark_click",
                success=False, duration_ms=2000,
            )
        # Alternativo (omnibox) muchos OK
        for _ in range(10):
            store.upsert_strategy_score(
                step_kind="open_url", strategy="omnibox_url",
                success=True, duration_ms=300,
            )
        # Necesitamos también attempts contiguos en éxito para la racha.
        _seed_attempts(store, kind="open_url", strategy="omnibox_url",
                       success=True, n=4)
        s = repair.evaluate(
            mission_id="m1", step_kind="open_url",
            current_preferred="bookmark_click",
            candidates=["omnibox_url", "alt_x"],
            step_id="s1",
        )
        assert s is not None
        assert s.suggested_preferred == "omnibox_url"
        assert s.score_suggested > s.score_current
        assert "omnibox" in s.human_message.lower() or "omnibox" in s.suggested_preferred

    def test_marks_decision_persists_choice(self, tmp_path):
        store, _, repair = _build(tmp_path)
        for _ in range(10):
            store.upsert_strategy_score(
                step_kind="open_url", strategy="omnibox_url",
                success=True, duration_ms=300,
            )
        for _ in range(8):
            store.upsert_strategy_score(
                step_kind="open_url", strategy="bookmark_click",
                success=False, duration_ms=2000,
            )
        _seed_attempts(store, kind="open_url", strategy="omnibox_url",
                       success=True, n=4)
        s = repair.evaluate(
            mission_id="m1", step_kind="open_url",
            current_preferred="bookmark_click",
            candidates=["omnibox_url"],
        )
        assert s is not None
        d = repair.mark_decision(s, "session_only")
        assert d.decision == "session_only"
        # session_only ⇒ is_session_only debe responder True
        assert repair.is_session_only(s) is True

    def test_reject_creates_cooldown(self, tmp_path):
        store, _, repair = _build(tmp_path)
        for _ in range(10):
            store.upsert_strategy_score(
                step_kind="open_url", strategy="omnibox_url",
                success=True, duration_ms=300,
            )
        for _ in range(8):
            store.upsert_strategy_score(
                step_kind="open_url", strategy="bookmark_click",
                success=False, duration_ms=2000,
            )
        _seed_attempts(store, kind="open_url", strategy="omnibox_url",
                       success=True, n=4)
        s = repair.evaluate(
            mission_id="m1", step_kind="open_url",
            current_preferred="bookmark_click",
            candidates=["omnibox_url"],
        )
        assert s is not None
        repair.mark_decision(s, "reject")
        # Tras reject, evaluate no debería volver a sugerir.
        s2 = repair.evaluate(
            mission_id="m1", step_kind="open_url",
            current_preferred="bookmark_click",
            candidates=["omnibox_url"],
        )
        assert s2 is None
