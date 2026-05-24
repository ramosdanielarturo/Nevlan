"""
Tests for ExecutionTracker.delete_entry functionality.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest


class TestExecutionTrackerDelete:
    """Verify that delete_entry works correctly."""

    def _make_tracker(self, tmp_path):
        """Create a tracker instance with a temporary log file."""
        log_file = tmp_path / "execution_log.json"
        fav_file = tmp_path / "favorites.json"

        # Patch at module level so the instance uses tmp paths
        import app.services.missions.execution_tracker as tracker_mod
        old_log = tracker_mod.EXEC_LOG_FILE
        old_fav = tracker_mod.FAVORITES_FILE
        tracker_mod.EXEC_LOG_FILE = log_file
        tracker_mod.FAVORITES_FILE = fav_file

        from app.services.missions.execution_tracker import ExecutionTracker
        # Create fresh instance (not singleton)
        tracker = ExecutionTracker.__new__(ExecutionTracker)
        import threading
        tracker._lock = threading.Lock()
        tracker._entries = []
        tracker._favorites = set()

        return tracker, log_file, old_log, old_fav, tracker_mod

    def _cleanup(self, tracker_mod, old_log, old_fav):
        tracker_mod.EXEC_LOG_FILE = old_log
        tracker_mod.FAVORITES_FILE = old_fav

    def test_delete_entry_by_id(self, tmp_path):
        """Borrar una entrada por id."""
        tracker, log_file, old_log, old_fav, mod = self._make_tracker(tmp_path)
        try:
            idx = tracker.log_start("mission_1", "Proceso A", 5, "manual")
            tracker.log_complete(idx, "success", 5)

            entries = tracker.get_recent(10)
            assert len(entries) == 1
            entry_id = entries[0].id
            assert entry_id  # Should have an id

            result = tracker.delete_entry(entry_id)
            assert result is True

            entries_after = tracker.get_recent(10)
            assert len(entries_after) == 0
        finally:
            self._cleanup(mod, old_log, old_fav)

    def test_delete_does_not_remove_automation(self, tmp_path):
        """Borrar una entrada NO borra la misión/automatización."""
        tracker, log_file, old_log, old_fav, mod = self._make_tracker(tmp_path)
        try:
            tracker.log_start("mission_abc", "Mi Proceso", 3, "manual")
            entries = tracker.get_recent(10)
            entry_id = entries[0].id

            tracker.delete_entry(entry_id)

            # The mission_id should still be valid (just not in execution log)
            assert tracker.get_for_mission("mission_abc") == []
        finally:
            self._cleanup(mod, old_log, old_fav)

    def test_delete_persists(self, tmp_path):
        """El borrado se refleja en el archivo JSON."""
        tracker, log_file, old_log, old_fav, mod = self._make_tracker(tmp_path)
        try:
            tracker.log_start("m1", "Proceso 1", 2, "manual")
            tracker.log_start("m2", "Proceso 2", 3, "manual")

            entries = tracker.get_recent(10)
            assert len(entries) == 2

            # Delete the first one (most recent in get_recent = last appended)
            entry_id = entries[0].id
            tracker.delete_entry(entry_id)

            # Verify persistence
            assert log_file.exists(), f"Log file should exist at {log_file}"
            with open(log_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert len(data["entries"]) == 1
            assert data["entries"][0]["id"] != entry_id
        finally:
            self._cleanup(mod, old_log, old_fav)

    def test_delete_nonexistent_returns_false(self, tmp_path):
        """Borrar un id inexistente retorna False."""
        tracker, log_file, old_log, old_fav, mod = self._make_tracker(tmp_path)
        try:
            tracker.log_start("m1", "Proceso", 1, "manual")
            result = tracker.delete_entry("nonexistent_id_12345")
            assert result is False
        finally:
            self._cleanup(mod, old_log, old_fav)

    def test_old_entries_get_id(self, tmp_path):
        """Entradas sin id (legacy) reciben uno al cargarse."""
        log_file = tmp_path / "execution_log.json"
        fav_file = tmp_path / "favorites.json"

        # Write a legacy entry without id
        legacy_data = {
            "entries": [
                {
                    "mission_id": "old_mission",
                    "mission_name": "Legacy Process",
                    "started_at": "2025-01-01T10:00:00",
                    "completed_at": "2025-01-01T10:01:00",
                    "status": "success",
                    "steps_executed": 3,
                    "total_steps": 3,
                    "error": "",
                    "trigger": "manual",
                    # NOTE: no "id" field!
                }
            ]
        }
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        import app.services.missions.execution_tracker as tracker_mod
        old_log = tracker_mod.EXEC_LOG_FILE
        old_fav = tracker_mod.FAVORITES_FILE
        tracker_mod.EXEC_LOG_FILE = log_file
        tracker_mod.FAVORITES_FILE = fav_file
        try:
            from app.services.missions.execution_tracker import ExecutionTracker
            import threading
            tracker = ExecutionTracker.__new__(ExecutionTracker)
            tracker._lock = threading.Lock()
            tracker._entries = []
            tracker._favorites = set()
            tracker._load()

            entries = tracker.get_recent(10)
            assert len(entries) == 1
            # Should have auto-generated an id
            assert entries[0].id
            assert len(entries[0].id) > 0
        finally:
            tracker_mod.EXEC_LOG_FILE = old_log
            tracker_mod.FAVORITES_FILE = old_fav
