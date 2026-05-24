"""Tests FASE 5 live environment reset + determinismo p02."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.contracts.mission import Mission
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime.fase5_deterministic_gate import (
    FASE5_ACCEPTANCE_STATUS,
    FASE5_REJECTION_STATUS,
    diff_strategy_paths,
    fase5_gate_violations,
)
from app.services.runtime.fase5_live_environment_reset import (
    ENVIRONMENT_NOT_NORMALIZED,
    derive_mission_normalization_spec,
    derive_neutral_baseline_url,
    normalize_fase5_live_replay_environment,
    open_new_tab_postcondition_satisfied,
)
from app.services.runtime.fase5_replay_runner import _execute_replay


def _mission_with_browser_flow(*, app_name: str = "chrome") -> Mission:
    mission = Mission(name="fase5-reset-test")
    mission.semantic_execution_plan = {
        "source": "test",
        "version": 1,
        "intent_status": "EXECUTABLE",
        "steps": [
            {
                "id": "m:p00",
                "type": "open_app",
                "params": {"name": app_name},
                "preferred_strategy": "open_app:os_startfile",
            },
            {
                "id": "m:p02",
                "type": "open_new_tab",
                "params": {},
                "preferred_strategy": "open_new_tab:hotkey_ctrl_t",
            },
            {
                "id": "m:p03",
                "type": "open_site",
                "params": {"alias": "youtube"},
                "preferred_strategy": "open_url:hotkey_ctrl_l",
            },
            {
                "id": "m:p04",
                "type": "search_content",
                "params": {"site": "youtube", "query": "demo"},
                "preferred_strategy": "search_content:uol_surface",
            },
        ],
    }
    return mission


def _open_tab_path(strategy: str) -> list:
    return [
        {"step_id": "m:p00", "kind": "open_app", "strategy_used": "skip:already_satisfied"},
        {"step_id": "m:p02", "kind": "open_new_tab", "strategy_used": strategy},
    ]


class TestMissionNormalizationSpec:
    def test_derives_required_app_and_barriers_from_contract(self):
        mission = _mission_with_browser_flow()
        spec, err = derive_mission_normalization_spec(mission)
        assert err is None
        assert spec.required_app_name == "chrome"
        assert "chrome.exe" in spec.required_processes
        assert "m:p00" in spec.setup_step_ids
        assert "m:p02" in spec.barrier_step_ids
        assert spec.needs_url_reset is True
        assert spec.baseline_url
        assert "youtube" in spec.forbidden_url_fragments

    def test_neutral_baseline_avoids_mission_domains(self):
        mission = _mission_with_browser_flow()
        spec, _ = derive_mission_normalization_spec(mission)
        url = derive_neutral_baseline_url(spec.forbidden_url_fragments, spec.barrier_steps)
        assert url
        assert "youtube" not in url
        assert "google" not in url


class TestOpenNewTabPostcondition:
    def test_new_tab_url_satisfied(self):
        st = StateSnapshot(
            running_processes=["chrome.exe"],
            browser_url="chrome://newtab/",
            active_window_title="New Tab - Browser",
        )
        assert open_new_tab_postcondition_satisfied(st) is True

    def test_results_page_not_new_tab_postcondition(self):
        st = StateSnapshot(
            running_processes=["chrome.exe"],
            browser_url="https://www.youtube.com/results?search_query=demo",
            active_window_title="demo - results",
        )
        assert open_new_tab_postcondition_satisfied(st) is False


class TestEnvironmentReset:
    @patch("app.services.runtime.fase5_live_environment_reset._wait_until_barriers_clear")
    @patch("app.services.runtime.fase5_live_environment_reset._navigate_address_bar", return_value=True)
    @patch("app.services.runtime.fase5_live_environment_reset._ensure_process_foreground")
    @patch("app.services.runtime.fase5_live_environment_reset.StateDetector")
    def test_clears_new_tab_surface(
        self, mock_det_cls, mock_fg, _mock_nav, mock_wait,
    ):
        mock_fg.return_value = True
        mission = _mission_with_browser_flow()
        spec, _ = derive_mission_normalization_spec(mission)
        before = StateSnapshot(
            running_processes=["chrome.exe"],
            browser_url="chrome://newtab/",
            active_window_title="New Tab",
        )
        after = StateSnapshot(
            running_processes=["chrome.exe"],
            browser_url=spec.baseline_url,
            active_window_title="Example Domain",
        )
        mock_det_cls.return_value.detect.side_effect = [before, before, after]
        mock_wait.return_value = after

        result = normalize_fase5_live_replay_environment(mission=mission)
        assert result.initial_state_normalized is True
        assert result.barrier_postconditions_before.get("m:p02") is True
        assert result.barrier_postconditions_after.get("m:p02") is False
        assert "navigate_baseline" in result.actions_taken

    @patch("app.services.runtime.fase5_live_environment_reset._wait_until_barriers_clear")
    @patch("app.services.runtime.fase5_live_environment_reset._navigate_address_bar", return_value=True)
    @patch("app.services.runtime.fase5_live_environment_reset._ensure_process_foreground")
    @patch("app.services.runtime.fase5_live_environment_reset.StateDetector")
    def test_clears_downstream_results_between_replays(
        self, mock_det_cls, mock_fg, _mock_nav, mock_wait,
    ):
        mock_fg.return_value = True
        mission = _mission_with_browser_flow()
        spec, _ = derive_mission_normalization_spec(mission)
        downstream = StateSnapshot(
            running_processes=["chrome.exe"],
            browser_url="https://www.youtube.com/results?search_query=demo",
            active_window_title="demo - results",
        )
        after = StateSnapshot(
            running_processes=["chrome.exe"],
            browser_url=spec.baseline_url,
            active_window_title="Example Domain",
        )
        mock_det_cls.return_value.detect.side_effect = [downstream, downstream, after]
        mock_wait.return_value = after

        result = normalize_fase5_live_replay_environment(mission=mission)
        assert result.initial_state_normalized is True
        assert "navigate_baseline" in result.actions_taken
        assert result.reason == "baseline_reset_complete"

    @patch("app.services.runtime.fase5_live_environment_reset._ensure_process_foreground")
    @patch("app.services.runtime.fase5_live_environment_reset.StateDetector")
    def test_foreground_failure_reports_not_normalized(self, mock_det_cls, mock_fg):
        mock_fg.return_value = False
        mission = _mission_with_browser_flow()
        mock_det_cls.return_value.detect.return_value = StateSnapshot(
            running_processes=["chrome.exe"],
        )
        result = normalize_fase5_live_replay_environment(mission=mission)
        assert result.initial_state_normalized is False
        assert result.failure_code == ENVIRONMENT_NOT_NORMALIZED

    @patch("app.services.runtime.fase5_live_environment_reset.StateDetector")
    def test_required_app_not_running_deferred_ok(self, mock_det_cls):
        mission = _mission_with_browser_flow()
        mock_det_cls.return_value.detect.return_value = StateSnapshot(
            running_processes=["explorer.exe"],
        )
        result = normalize_fase5_live_replay_environment(mission=mission)
        assert result.initial_state_normalized is True
        assert result.reason == "required_app_not_running_deferred_to_mission"

    def test_empty_mission_fails_not_normalized(self):
        mission = Mission(name="empty")
        mission.semantic_execution_plan = {"steps": [], "version": 1}
        result = normalize_fase5_live_replay_environment(mission=mission)
        assert result.initial_state_normalized is False
        assert result.failure_code == ENVIRONMENT_NOT_NORMALIZED


class TestStrategyPathDiff:
    def test_skip_vs_hotkey_still_diverges_without_reset(self):
        a = _open_tab_path("skip:already_satisfied")
        b = _open_tab_path("open_tab:uol_hotkey")
        diffs = diff_strategy_paths([a, b])
        assert diffs

    def test_same_hotkey_no_divergence(self):
        a = _open_tab_path("open_tab:uol_hotkey")
        b = _open_tab_path("open_tab:uol_hotkey")
        assert diff_strategy_paths([a, b, a]) == []

    def test_three_identical_paths_after_reset(self):
        path = _open_tab_path("open_tab:uol_hotkey")
        assert diff_strategy_paths([path, path, path]) == []


class TestFase5GateBlocksCoordsAndSmartRoute:
    def test_coords_block_gate(self):
        report = {
            "runs": [{"run_index": 1, "player_used": "smart", "coords_used": True, "smart_route_count": 0, "legacy_fork": False}],
            "strategy_path_diff": [],
            "coords_used": True,
            "smart_route_count": 0,
            "runtime_entrypoint": "smart_runner_bridge",
            "forbidden_preferred_count": 0,
            "environment_reset_failures": 0,
            "determinism_status": FASE5_ACCEPTANCE_STATUS,
        }
        v = fase5_gate_violations(report)
        assert any("coords" in x for x in v)

    def test_smart_route_block_gate(self):
        report = {
            "runs": [{"run_index": 1, "player_used": "smart", "coords_used": False, "smart_route_count": 1, "legacy_fork": False}],
            "strategy_path_diff": [],
            "coords_used": False,
            "smart_route_count": 1,
            "runtime_entrypoint": "smart_runner_bridge",
            "forbidden_preferred_count": 0,
            "environment_reset_failures": 0,
            "determinism_status": FASE5_ACCEPTANCE_STATUS,
        }
        v = fase5_gate_violations(report)
        assert any("smart_route" in x for x in v)

    def test_environment_reset_failures_block_gate(self):
        report = {
            "runs": [],
            "strategy_path_diff": [],
            "coords_used": False,
            "smart_route_count": 0,
            "runtime_entrypoint": "smart_runner_bridge",
            "forbidden_preferred_count": 0,
            "environment_reset_failures": 1,
            "determinism_status": FASE5_REJECTION_STATUS,
        }
        v = fase5_gate_violations(report)
        assert any("environment_reset_failures" in x for x in v)


class TestFase5ReplayWithReset:
    @patch("app.services.runtime.fase5_live_environment_reset.normalize_fase5_live_replay_environment")
    @patch("app.services.runtime.fase5_replay_runner.run_mission_smart")
    @patch("app.services.runtime.fase5_replay_runner.record_execution_run_audit")
    def test_execute_replay_calls_reset_on_live(
        self, mock_audit, mock_smart, mock_reset,
    ):
        from app.services.runtime.fase5_live_environment_reset import Fase5EnvironmentResetResult

        mock_reset.return_value = Fase5EnvironmentResetResult(
            initial_state_normalized=True,
            reason="barrier_action_required",
        )
        mock_smart.return_value = MagicMock()
        mock_audit.return_value = MagicMock(
            status="success",
            duration_ms=1,
            coords_used=False,
            run_id="r1",
            step_strategies=[],
        )

        mission = MagicMock(id="m1")
        report = _execute_replay(mission, mode="live", run_index=1, settings=MagicMock())
        mock_reset.assert_called_once_with(mission=mission)
        assert report.initial_state_normalized is True
