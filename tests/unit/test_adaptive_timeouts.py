"""
Tests for AdaptiveTimeouts (Auto-Learning Executor).

Cubre caso #8: adaptive timeout aprende p95.
"""
from __future__ import annotations

import time

import pytest


def _build(tmp_path):
    from app.services.missions.execution_memory_store import ExecutionMemoryStore
    from app.services.missions.adaptive_timeouts import AdaptiveTimeouts
    store = ExecutionMemoryStore(
        db_path=tmp_path / "to.db", audit_jsonl=tmp_path / "to.jsonl",
    )
    return store, AdaptiveTimeouts(store=store)


class TestAdaptiveTimeouts:
    def test_returns_default_when_no_observations(self, tmp_path):
        _, t = _build(tmp_path)
        rec = t.recommend("youtube_loaded", default_ms=8000,
                          app="chrome.exe", domain="youtube.com")
        assert rec.used_default is True
        assert rec.suggested_ms == 8000

    def test_p95_plus_margin_after_enough_samples(self, tmp_path):
        """Caso aceptación #8: tras observaciones suficientes, el timeout
        sugerido debe ser p95 + margen, no el default."""
        _, t = _build(tmp_path)
        # 10 observaciones: 9 rápidas (~1200ms), 1 lenta (3800ms)
        for d in [1200, 1100, 1300, 1150, 1250, 1300, 1180, 1220, 1280, 3800]:
            t.record_observation(
                "youtube_loaded", duration_ms=d, success=True,
                app="chrome.exe", domain="youtube.com",
            )
        rec = t.recommend(
            "youtube_loaded", default_ms=8000,
            app="chrome.exe", domain="youtube.com",
        )
        assert rec.used_default is False
        assert rec.samples == 10
        # p95 (interpolación lineal con 10 muestras) cae en ~2.6-3.8s
        # según la distribución; el suggested debe ser ≥ p95.
        assert rec.p95_ms > rec.p50_ms
        assert rec.p95_ms >= 2500
        assert rec.suggested_ms >= rec.p95_ms

    def test_network_slow_inflates_suggestion(self, tmp_path):
        _, t = _build(tmp_path)
        for d in [1000] * 10:
            t.record_observation(
                "x", duration_ms=d, success=True, app="a", domain="d",
            )
        normal = t.recommend("x", default_ms=5000, app="a", domain="d")
        t.mark_network_slow(for_seconds=300)
        slow = t.recommend("x", default_ms=5000, app="a", domain="d")
        assert slow.suggested_ms > normal.suggested_ms

    def test_facade_recommended_timeout(self, tmp_path, monkeypatch):
        """Verifica que el façade global enrute al singleton."""
        from app.services.missions.adaptive_timeouts import (
            recommended_timeout, record_observation, get_adaptive_timeouts,
            reset_adaptive_timeouts_for_tests,
        )
        from app.services.missions import adaptive_timeouts as mod
        from app.services.missions.execution_memory_store import ExecutionMemoryStore
        from app.services.missions.adaptive_timeouts import AdaptiveTimeouts

        reset_adaptive_timeouts_for_tests()
        # Inyectamos un singleton temporal apuntando a un store de test.
        store = ExecutionMemoryStore(
            db_path=tmp_path / "f.db", audit_jsonl=tmp_path / "f.jsonl",
        )
        mod._adaptive = AdaptiveTimeouts(store=store)
        for d in [800, 850, 820, 900, 870, 900]:
            record_observation("c", duration_ms=d, success=True)
        rec_ms = recommended_timeout("c", default_ms=10000)
        assert rec_ms < 10000

    def test_floor_and_ceiling(self, tmp_path):
        _, t = _build(tmp_path)
        for d in [50, 60, 70, 80, 90]:
            t.record_observation("c", duration_ms=d, success=True)
        rec = t.recommend("c", default_ms=2000)
        assert rec.suggested_ms >= t.min_timeout_ms
        # Forzamos un ceiling pequeño:
        rec2 = t.recommend("c", default_ms=2000, max_ms=1000)
        assert rec2.suggested_ms <= 1000
