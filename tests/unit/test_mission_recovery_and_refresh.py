"""Recuperación en replay (retry/skip/manual), refresh de misión y sesión débiles."""
from __future__ import annotations

from app.contracts.mission import (
    Mission,
    RawEvent,
    EventType,
    MouseAction,
    FallbackStrategy,
)
from app.contracts.mission import ReplayStatus
from app.services.missions.player import MissionPlayer
from app.services.missions.step_criticality import infer_step_criticality
from app.services.missions.compiler import MissionCompiler
from app.services.missions.weak_step_repair_session import WeakStepRepairSession
from app.services.missions import mission_refresh


def _mission_from_clicks(n: int) -> Mission:
    m = Mission(name="t_recovery")
    evs = []
    for i in range(n):
        evs.append(
            RawEvent(
                event_type=EventType.MOUSE_CLICK,
                mouse_action=MouseAction(x=10 + i, y=10 + i, button="left"),
                metadata={"uia": {"name": f"B{i}", "control_type": "ButtonControl"}},
            )
        )
    m.raw_trace = evs
    return MissionCompiler.compile_mission(m)


def _no_coords_fallback_for_tests(m: Mission) -> None:
    for cs in m.compiled_execution_graph:
        cs.fallback_strategy = FallbackStrategy.FAIL_FAST
        try:
            cs.target_context.fallback_coords = None
        except Exception:
            pass


def test_infer_step_criticality_known_actions():
    m = _mission_from_clicks(1)
    s0 = m.compiled_execution_graph[0]
    assert infer_step_criticality(s0) in ("medium", "high", "low")


def test_weak_session_progress_label():
    s = WeakStepRepairSession(total_steps=5)
    s.mark_success()
    s.mark_success()
    assert "2 de 5" in s.progress_label_es()


def test_refresh_mission_returns_counts():
    m = _mission_from_clicks(3)
    r = mission_refresh.refresh_mission_after_step_change(m, reason="pytest", affected_step_ids=[])
    assert "action_groups_count" in r or "weak_steps" in r


def test_replay_recovery_retry_then_success(monkeypatch):
    m = _mission_from_clicks(1)
    _no_coords_fallback_for_tests(m)
    cs = m.compiled_execution_graph[0]
    cs.retry_policy.max_retries = 1

    tries = []

    def _stub_exec(self, step, variables, timing=None):
        tries.append(len(tries))
        if len(tries) == 1:
            raise RuntimeError("boom first run")
        return None

    monkeypatch.setattr(MissionPlayer, "_execute_compiled_step", _stub_exec)

    p = MissionPlayer(m)
    p.enqueue_recovery_commands_for_tests("retry")
    p.start_execution()
    ex = p.replay()
    assert ex.status == ReplayStatus.SUCCESS_WITH_RECOVERY
    assert len(tries) == 2
    assert getattr(ex, "steps_recovered_retry", 0) >= 1


def test_replay_recovery_skip(monkeypatch):
    m = _mission_from_clicks(2)
    _no_coords_fallback_for_tests(m)
    for cs in m.compiled_execution_graph:
        cs.retry_policy.max_retries = 1

    hits = []

    def _stub_exec(self, step, variables, timing=None):
        hits.append(step.id)
        if len(hits) == 1:
            raise RuntimeError("fail01")
        return None

    monkeypatch.setattr(MissionPlayer, "_execute_compiled_step", _stub_exec)

    p = MissionPlayer(m)
    p.enqueue_recovery_commands_for_tests("skip")
    ex = p.replay(auto_confirm=False)

    assert ex.steps_skipped == 1
    assert ex.steps_executed == 1
    assert len(hits) == 2


def test_replay_recovery_manual_via_fifo(monkeypatch):
    m = _mission_from_clicks(2)
    _no_coords_fallback_for_tests(m)
    sid0 = m.compiled_execution_graph[0].id
    sid1 = m.compiled_execution_graph[1].id
    for cs in m.compiled_execution_graph:
        cs.retry_policy.max_retries = 1

    calls = []

    def _stub_exec(self, step, variables, timing=None):
        calls.append(step.id)
        if step.id == sid0:
            raise RuntimeError("fail")
        return None

    monkeypatch.setattr(MissionPlayer, "_execute_compiled_step", _stub_exec)

    p = MissionPlayer(m)
    p.enqueue_recovery_commands_for_tests("manual_continue")
    ex = p.replay(auto_confirm=False)

    assert ex.steps_manual >= 1
    assert calls[0] == sid0 and calls[-1] == sid1
