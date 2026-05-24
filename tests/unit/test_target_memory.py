"""
Tests for TargetMemory (Auto-Learning Executor).

Cubre caso #5: TargetMemory encuentra ``youtube_search_box`` sin preguntar.
"""
from __future__ import annotations

import pytest


def _build(tmp_path):
    from app.services.missions.execution_memory_store import ExecutionMemoryStore
    from app.services.missions.target_memory import TargetMemory
    store = ExecutionMemoryStore(
        db_path=tmp_path / "tm.db",
        audit_jsonl=tmp_path / "tm.jsonl",
    )
    return store, TargetMemory(store=store)


class TestTargetMemory:
    def test_lookup_returns_none_when_empty(self, tmp_path):
        _, mem = _build(tmp_path)
        assert mem.lookup("non_existent") is None

    def test_record_success_then_lookup_finds_target(self, tmp_path):
        """Caso aceptación #5: tras éxitos, el target queda memorizado."""
        store, mem = _build(tmp_path)
        mem.record_success(
            "youtube_search_box",
            strategy="dom_input",
            selectors=["input[name='search_query']"],
            uia={"name": "Buscar", "control_type": "Edit"},
            ocr_anchor="Buscar",
            domain="youtube.com",
            app="chrome.exe",
        )
        entry = mem.lookup("youtube_search_box", domain="youtube.com", app="chrome.exe")
        assert entry is not None
        assert entry.semantic_target == "youtube_search_box"
        assert "input[name='search_query']" in entry.successful_selectors
        assert entry.successful_uia.get("name") == "Buscar"
        assert entry.ocr_anchor == "Buscar"
        assert entry.last_successful_strategy == "dom_input"
        assert entry.success_count == 1

    def test_failure_increments_failure_count(self, tmp_path):
        store, mem = _build(tmp_path)
        mem.record_success(
            "youtube_search_box", strategy="dom_input",
            domain="youtube.com", app="chrome.exe",
        )
        mem.record_failure(
            "youtube_search_box", strategy="dom_input",
            domain="youtube.com", app="chrome.exe",
        )
        entry = mem.lookup("youtube_search_box", domain="youtube.com", app="chrome.exe")
        assert entry is not None
        assert entry.success_count == 1
        assert entry.failure_count == 1

    def test_prefer_strategies_moves_winner_to_front(self, tmp_path):
        store, mem = _build(tmp_path)
        mem.record_success(
            "search_box",
            strategy="uia_searchbox",
            domain="youtube.com", app="chrome.exe",
        )
        ordered = mem.prefer_strategies(
            "search_box",
            ["dom_input", "uia_searchbox", "ocr_search"],
            domain="youtube.com", app="chrome.exe",
        )
        assert ordered[0] == "uia_searchbox"

    def test_lookup_falls_back_to_global(self, tmp_path):
        """Aceptación: se ofrece resolución incluso sin scope exacto."""
        store, mem = _build(tmp_path)
        # Registramos sin domain/app (global)
        mem.record_success("widget_x", strategy="dom_input")
        # Lookup con scope inexistente debe caer al global.
        entry = mem.lookup("widget_x", domain="otro.com", app="otra.exe")
        assert entry is not None
        assert entry.semantic_target == "widget_x"

    def test_reliability_property(self, tmp_path):
        store, mem = _build(tmp_path)
        for _ in range(9):
            mem.record_success("rt", strategy="dom", domain="d", app="a")
        mem.record_failure("rt", strategy="dom", domain="d", app="a")
        entry = mem.lookup("rt", domain="d", app="a")
        assert entry is not None
        # 9 ok / 1 fail → reliability ≈ 0.83
        assert 0.7 < entry.reliability < 0.95

    def test_lookup_with_fallback_dict(self, tmp_path):
        store, mem = _build(tmp_path)
        mem.record_success(
            "search_box", strategy="uia_searchbox",
            domain="youtube.com", app="chrome.exe",
            selectors=["#search-input input"],
        )
        sig = {
            "semantic_target": "search_box",
            "domain": "youtube.com",
            "app": "chrome.exe",
        }
        entry = mem.lookup_with_fallback(sig)
        assert entry is not None
        assert "#search-input input" in entry.successful_selectors
