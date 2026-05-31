"""Unit tests — mandatory post-step verification."""
from __future__ import annotations

from unittest.mock import MagicMock

from app.services.missions.execution_contracts import MissionStep, build_contract
from app.services.missions.post_step_verifier import (
    OUTCOME_NOT_CONFIRMED,
    SURFACE_NOT_READY,
    verify_step_outcome,
)
from app.services.missions.smart_executor import SmartMissionExecutor
from app.services.missions.state_detector import StateSnapshot


def _snap(**kw) -> StateSnapshot:
    return StateSnapshot(**kw)


def test_failing_verifier_returns_typed_failure():
    step = MissionStep(id="s1", kind="click_named_item", params={"target_name": "Save"})
    contract = build_contract(step)
    contract.postcondition = MagicMock(return_value=False)

    before = _snap(active_window_title="App", active_app="app.exe")
    after = _snap(active_window_title="App", active_app="app.exe")

    result = verify_step_outcome(step, contract, before, after, strategy_used="click:uia")
    assert result.passed is False
    assert result.failure_code == OUTCOME_NOT_CONFIRMED


def test_passing_verifier_on_state_change():
    step = MissionStep(id="s2", kind="open_app", params={"name": "chrome"})
    contract = build_contract(step)
    contract.postcondition = MagicMock(return_value=False)

    before = _snap(active_window_title="Desktop", active_app="explorer.exe")
    after = _snap(
        active_window_title="Google Chrome",
        active_app="chrome.exe",
        running_processes=["chrome.exe"],
    )

    result = verify_step_outcome(step, contract, before, after)
    assert result.passed is True
    assert result.signal == "visual_state_change"


def test_open_url_verification_fails_without_url_change():
    step = MissionStep(
        id="s3",
        kind="open_url",
        params={"alias": "youtube", "url": "https://www.youtube.com"},
    )
    contract = build_contract(step)
    contract.postcondition = MagicMock(return_value=False)

    snap = _snap(browser_url="", active_app="chrome.exe")
    result = verify_step_outcome(step, contract, snap, snap)
    assert result.passed is False
    assert result.failure_code == SURFACE_NOT_READY


def test_executor_stops_on_verification_failure(monkeypatch):
    from app.services.missions.action_runner import StrategyResult
    from app.services.missions.checkpoints import CheckpointManager
    from app.services.missions.execution_learning import LearningLogger

    step = MissionStep(
        id="st",
        kind="open_new_tab",
        params={"preferred_strategy": "open_new_tab:hotkey_ctrl_t"},
    )
    snap = StateSnapshot(
        active_app="chrome.exe",
        active_window_title="Tab",
        running_processes=["chrome.exe"],
    )

    class FakeDet:
        def detect(self, **_kwargs):
            return snap

    class OkRunner:
        def run_strategy(self, strategy, _step, _state):
            return StrategyResult(ok=True, strategy=strategy, message="ok", duration_ms=1)

    monkeypatch.setattr(
        "app.services.missions.smart_executor._resolve_strategies",
        lambda _s, _c, _m=None: ["open_new_tab:hotkey_ctrl_t"],
    )

    exe = SmartMissionExecutor(
        run_id="verify-stop",
        state_detector=FakeDet(),
        action_runner=OkRunner(),
        checkpoints=CheckpointManager(run_id="verify-stop"),
        learning=LearningLogger(),
    )
    exe._await_postcondition = lambda contract, deadline_ms=0, step=None: snap  # type: ignore[method-assign]

    def _force_verify_fail(self, step, contract, before, state_after, t0, *, retries, strategy, _uol_tel):
        return self._needs_human_outcome(
            step,
            contract,
            "No confirmé.",
            before,
            t0,
            retries=retries,
            strategy=strategy or "post_verify:failed",
            state_after=state_after.to_dict(),
            error=OUTCOME_NOT_CONFIRMED,
        )

    monkeypatch.setattr(SmartMissionExecutor, "_verify_post_step_or_human", _force_verify_fail)

    res = exe.run_mission([step])
    assert res.status == "stopped_for_human"
    assert res.outcomes[0].error == OUTCOME_NOT_CONFIRMED
