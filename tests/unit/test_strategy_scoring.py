"""
Tests for StrategyScoringEngine (Auto-Learning Executor).

Cubre casos PRD:
  1. Estrategia exitosa sube de prioridad
  2. Estrategia fallida baja de prioridad
  3. DOM falla, UIA funciona → UIA sube para ese target
  + safety guard: coords_absolutas nunca por encima de DOM/UIA/OCR
"""
from __future__ import annotations

import time

import pytest


def _build(tmp_path):
    from app.services.missions.execution_memory_store import ExecutionMemoryStore
    from app.services.missions.execution_learning import LearningLogger
    import threading
    store = ExecutionMemoryStore(
        db_path=tmp_path / "score.db",
        audit_jsonl=tmp_path / "score.jsonl",
    )
    # LearningLogger fake (no toca disco):
    learner = LearningLogger.__new__(LearningLogger)
    learner._lock = threading.RLock()
    learner._stats = {}
    learner._append = lambda rec: None  # type: ignore
    learner._save_stats = lambda: None  # type: ignore

    from app.services.missions.strategy_scoring import StrategyScoringEngine
    engine = StrategyScoringEngine(store=store, learning=learner)
    return store, engine


class TestStrategyScoring:
    def test_successful_strategy_rises(self, tmp_path):
        """Caso #1: estrategia con muchos OK queda primera."""
        store, engine = _build(tmp_path)
        for _ in range(8):
            store.upsert_strategy_score(
                step_kind="search", strategy="dom_input",
                success=True, duration_ms=300,
            )
        for _ in range(3):
            store.upsert_strategy_score(
                step_kind="search", strategy="ocr_search",
                success=True, duration_ms=900,
            )
        ranked = engine.rank_strategies("search", ["ocr_search", "dom_input"])
        assert ranked[0] == "dom_input"

    def test_failed_strategy_drops(self, tmp_path):
        """Caso #2: muchos fail bajan la prioridad."""
        store, engine = _build(tmp_path)
        for _ in range(6):
            store.upsert_strategy_score(
                step_kind="open", strategy="bookmark_click",
                success=False, duration_ms=500,
            )
        for _ in range(3):
            store.upsert_strategy_score(
                step_kind="open", strategy="omnibox_url",
                success=True, duration_ms=200,
            )
        ranked = engine.rank_strategies("open", ["bookmark_click", "omnibox_url"])
        assert ranked[0] == "omnibox_url"
        assert ranked[-1] == "bookmark_click"

    def test_uia_rises_when_dom_fails_for_target(self, tmp_path):
        """Caso #3: si DOM falla para un target específico y UIA funciona,
        UIA debe quedar por encima de DOM en el scope de ese target."""
        store, engine = _build(tmp_path)
        target = "youtube_search_box"
        scope = dict(scope_app="chrome.exe", scope_domain="youtube.com",
                     scope_target=target)
        # DOM falla 4 veces para ese target
        for _ in range(4):
            store.upsert_strategy_score(
                step_kind="search_youtube", strategy="dom_input",
                success=False, duration_ms=1500, **scope,
            )
        # UIA funciona 5 veces para ese target
        for _ in range(5):
            store.upsert_strategy_score(
                step_kind="search_youtube", strategy="uia_searchbox",
                success=True, duration_ms=400, **scope,
            )
        ranked = engine.rank_strategies(
            "search_youtube",
            ["dom_input", "uia_searchbox", "ocr_search", "vision"],
            scope_app="chrome.exe", scope_domain="youtube.com",
            scope_target=target,
        )
        assert ranked[0] == "uia_searchbox"
        # DOM debe estar por debajo de UIA.
        assert ranked.index("dom_input") > ranked.index("uia_searchbox")

    def test_safety_guard_unsafe_never_above_preferred(self, tmp_path):
        """Aceptación: ninguna coordenada absoluta por encima de DOM/UIA/OCR
        salvo confirmación humana (allow_unsafe_top=True)."""
        store, engine = _build(tmp_path)
        # Aunque "rel_coords" tenga muchos éxitos, el safety floor debe
        # forzarla por debajo de DOM/UIA/OCR.
        for _ in range(20):
            store.upsert_strategy_score(
                step_kind="click", strategy="rel_coords",
                success=True, duration_ms=80,
            )
        # DOM con sólo 1 éxito.
        store.upsert_strategy_score(
            step_kind="click", strategy="dom_click",
            success=True, duration_ms=300,
        )
        ranked = engine.rank_strategies(
            "click", ["rel_coords", "dom_click", "uia_click"],
        )
        # DOM o UIA deben aparecer antes que rel_coords
        idx_unsafe = ranked.index("rel_coords")
        idx_dom = ranked.index("dom_click")
        idx_uia = ranked.index("uia_click")
        assert min(idx_dom, idx_uia) < idx_unsafe

        # Pero con confirmación humana se respeta el orden por score.
        ranked2 = engine.rank_strategies(
            "click", ["rel_coords", "dom_click", "uia_click"],
            allow_unsafe_top=True,
        )
        assert ranked2[0] == "rel_coords"

    def test_temporal_decay(self, tmp_path):
        """Una estrategia con éxitos viejos pesa menos que una con
        éxitos recientes — verifica vía `score_strategy`."""
        store, engine = _build(tmp_path)
        engine.half_life_s = 60.0  # half-life corta para test
        # Histórico antiguo: la inyectamos con last_ok_at lejano vía
        # acceso interno de SQLite, simulando que ya pasó tiempo.
        store.upsert_strategy_score(
            step_kind="x", strategy="old_winner", success=True, duration_ms=100,
        )
        store.upsert_strategy_score(
            step_kind="x", strategy="recent_winner", success=True, duration_ms=100,
        )
        # Antiguo: forzamos last_ok_at hace 1 hora
        with store._lock:  # type: ignore[union-attr]
            store._conn.execute(  # type: ignore[union-attr]
                "UPDATE strategy_scores SET last_ok_at = ?, ok=10, fail=0 "
                "WHERE strategy=?",
                (time.time() - 3600.0, "old_winner"),
            )
            store._conn.execute(  # type: ignore[union-attr]
                "UPDATE strategy_scores SET last_ok_at = ?, ok=4, fail=0 "
                "WHERE strategy=?",
                (time.time() - 5.0, "recent_winner"),
            )
        s_old = engine.score_strategy("x", "old_winner")
        s_new = engine.score_strategy("x", "recent_winner")
        # Reciente debe ganar pese a tener menos OK absolutos.
        assert s_new.score >= s_old.score

    def test_skip_set_pushed_to_back(self, tmp_path):
        """Estrategias en skip se mueven al final."""
        _, engine = _build(tmp_path)
        ranked = engine.rank_strategies(
            "kind", ["a", "b", "c"], skip=["b"],
        )
        assert ranked[-1] == "b"

    def test_no_data_falls_back_to_legacy_score(self, tmp_path):
        """Sin datos en SQLite, usa LearningLogger.score (back-compat)."""
        from app.services.missions.execution_memory_store import ExecutionMemoryStore
        from app.services.missions.execution_learning import LearningLogger
        import threading
        store = ExecutionMemoryStore(
            db_path=tmp_path / "y.db", audit_jsonl=tmp_path / "y.jsonl",
        )
        learner = LearningLogger.__new__(LearningLogger)
        learner._lock = threading.RLock()
        learner._stats = {"k": {"a": {"ok": 5, "fail": 0}, "b": {"ok": 0, "fail": 5}}}
        learner._append = lambda rec: None  # type: ignore
        learner._save_stats = lambda: None  # type: ignore
        from app.services.missions.strategy_scoring import StrategyScoringEngine
        engine = StrategyScoringEngine(store=store, learning=learner)
        ranked = engine.rank_strategies("k", ["b", "a"])
        assert ranked[0] == "a"
