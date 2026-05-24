"""
Tests for MissionFailureClassifier (Auto-Learning Executor).

Cubre casos PRD:
  6. wrong_page recupera abriendo URL correcta (clasificación → wrong_page)
  7. stale_selector actualiza selector
  + matriz general de 12 categorías y `is_recoverable`.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def classifier():
    from app.services.missions.failure_classifier import (
        MissionFailureClassifier,
    )
    return MissionFailureClassifier()


class TestMissionFailureClassifier:
    def test_classify_timeout_by_keyword(self, classifier):
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(error="Timeout waiting for selector")
        assert rec.failure_type == MissionFailureType.TIMEOUT
        assert rec.retryable is True
        assert "extend_timeout" in rec.actions

    def test_classify_permission_denied(self, classifier):
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(error="Access denied: 403 Forbidden")
        assert rec.failure_type == MissionFailureType.PERMISSION_ERROR
        assert rec.should_ask_human is True
        assert rec.retryable is False

    def test_classify_wrong_page_via_state_after(self, classifier):
        """Caso aceptación #6: state_after.url incorrecta → wrong_page."""
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(
            error="postcondition no cumplida",
            step_kind="open_url",
            expected_url_substring="youtube.com",
            state_after={"browser_url": "https://www.google.com/"},
        )
        assert rec.failure_type == MissionFailureType.WRONG_PAGE
        assert "open_correct_url" in rec.actions

    def test_classify_wrong_app(self, classifier):
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(
            error="postcondition no cumplida",
            expected_app="chrome.exe",
            state_after={"active_app": "explorer.exe"},
        )
        assert rec.failure_type == MissionFailureType.WRONG_APP
        assert "activate_correct_window" in rec.actions

    def test_classify_stale_selector_when_prior_success(self, classifier):
        """Caso aceptación #7: selector que antes funcionaba ahora falla
        (prior_success_for_strategy=True + selector en error)."""
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(
            error="No such element: selector input.foo not found",
            prior_success_for_strategy=True,
        )
        assert rec.failure_type == MissionFailureType.STALE_SELECTOR
        assert "try_uia" in rec.actions
        assert "update_target_memory" in rec.actions

    def test_classify_layout_changed_when_prior_success_no_selector_keyword(self, classifier):
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(
            error="postcondition failed",
            prior_success_for_strategy=True,
        )
        assert rec.failure_type == MissionFailureType.LAYOUT_CHANGED

    def test_classify_profile_not_loaded_by_title(self, classifier):
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(
            error="postcondition no cumplida",
            state_after={"active_window_title": "Bienvenido a Chrome"},
        )
        assert rec.failure_type == MissionFailureType.PROFILE_NOT_LOADED
        assert "select_profile" in rec.actions

    def test_classify_user_interrupted(self, classifier):
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(error="Cancelled by user")
        assert rec.failure_type == MissionFailureType.USER_INTERRUPTED
        assert rec.retryable is False

    def test_classify_network_slow_via_long_wait(self, classifier):
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(
            error="postcondition no cumplida",
            wait_was_long=True,
            state_before={"browser_url": "https://www.youtube.com/"},
            state_after={"browser_url": "https://www.youtube.com/"},
        )
        assert rec.failure_type == MissionFailureType.NETWORK_SLOW

    def test_classify_unknown_default(self, classifier):
        from app.services.missions.failure_classifier import MissionFailureType
        rec = classifier.classify(error="???")
        assert rec.failure_type == MissionFailureType.UNKNOWN

    def test_is_recoverable_matrix(self, classifier):
        from app.services.missions.failure_classifier import MissionFailureType
        assert classifier.is_recoverable(MissionFailureType.TIMEOUT) is True
        assert classifier.is_recoverable(MissionFailureType.PERMISSION_ERROR) is False
        assert classifier.is_recoverable(MissionFailureType.USER_INTERRUPTED) is False
        assert classifier.is_recoverable(MissionFailureType.STALE_SELECTOR) is True
        assert classifier.is_recoverable(MissionFailureType.WRONG_PAGE) is True
