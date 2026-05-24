"""Tests — ARL recovery hook para SmartExecutor / TargetResolver."""

from __future__ import annotations

import time
from types import SimpleNamespace
import pytest

from app.contracts.mission import CompiledStep, Mission, TargetContext
from app.services.missions.action_runner import StrategyResult
from app.services.missions.checkpoints import CheckpointManager
from app.services.missions.execution_contracts import MissionStep, build_contract
from app.services.missions.smart_executor import SmartMissionExecutor
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime.arl_sep_recovery import (
    ARL_UNSAFE_COORDS_ONLY,
    evaluate_sep_arl_recovery,
    filter_strategies_exclude_emergency_coords,
    maybe_adjust_postcondition_deadline_ms,
    SepArlRecoveryVerdict,
    try_target_resolver_arl_second_pass,
)

import app.services.missions.smart_executor as smart_executor_module
import app.services.runtime.arl_sep_recovery as arl_sep_recovery_mod
from app.services.runtime.arl_metrics import reset_arl_metrics


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    reset_arl_metrics()
    yield
    reset_arl_metrics()


def _cfg(monkeypatch, **kw):
    d = dict(
        ARL_ENABLED=True,
        ARL_MAX_RETRIES=2,
        ARL_SEP_RECOVERY_MAX_ROUNDS=1,
    )
    d.update(kw)

    class _S:
        pass

    for k, v in d.items():
        setattr(_S, k, v)
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: _S(),
    )


def test_filter_excludes_coord_emergency_strategies() -> None:
    r = ["click:safe", "open_new_tab:vision_coords", "open_new_tab:wheel"]
    f, blocked = filter_strategies_exclude_emergency_coords(r)
    assert "vision_coords" not in " | ".join(f)
    assert blocked is None


def test_filter_coords_only_returns_unsafe_marker() -> None:
    coords_only = ["open_new_tab:vision_coords", "open_new_tab:relative_coords"]
    f, blocked = filter_strategies_exclude_emergency_coords(coords_only)
    assert f == []
    assert blocked == "ARL_UNSAFE_COORDS_ONLY"


def test_empty_filter_no_block() -> None:
    assert filter_strategies_exclude_emergency_coords([]) == ([], None)


def test_destructive_step_blocked_audit(monkeypatch):
    _cfg(monkeypatch)

    m = Mission(name="t")
    st = MissionStep(
        id="s1",
        kind="open_new_tab",
        params={"arl_blocked_destructive": True},
    )
    c = build_contract(st)
    v = evaluate_sep_arl_recovery(
        mission=m,
        step=st,
        contract=c,
        observed_snapshot=SimpleNamespace(),
        before_state={},
        last_strategy_used="x",
        last_strategy_ok=False,
        last_error="no encontrado",
        arl_round_done=0,
        max_extra_rounds=1,
        retry_budget=2,
    )
    assert v.retry_step is False
    assert m.adaptive_runtime_audit is not None
    kinds = [e.get("skipped") for e in m.adaptive_runtime_audit["events"]]
    assert True in kinds


def test_ambiguous_failure_requests_human(monkeypatch):
    _cfg(monkeypatch)

    m = Mission(name="t")
    st = MissionStep(id="s1", kind="open_new_tab", params={})
    c = build_contract(st)
    verdict = evaluate_sep_arl_recovery(
        mission=m,
        step=st,
        contract=c,
        observed_snapshot=SimpleNamespace(),
        before_state={},
        last_strategy_used="x",
        last_strategy_ok=False,
        last_error="ambiguous candidates in UIA snapshot",
        arl_round_done=0,
        max_extra_rounds=1,
        retry_budget=2,
    )
    assert verdict.escalate_human_immediate is True
    assert verdict.retry_step is False


def test_unknown_host_fallback_no_retry(monkeypatch):
    _cfg(monkeypatch)

    m = Mission(name="u")
    st = MissionStep(
        id="s1",
        kind="open_url",
        params={"url": "https://expected.example/path"},
        human_label="open",
    )
    c = build_contract(st)

    observed = {
        "active_window_title": "x",
        "browser_url": "https://different.example/other",
        "visible_label_candidates": [],
        "dom_fingerprint_weak": "ab",
        "resolution_class": None,
        "zoom_bucket": None,
    }

    verdict = evaluate_sep_arl_recovery(
        mission=m,
        step=st,
        contract=c,
        observed_snapshot=SimpleNamespace(**{k: v for k, v in observed.items()}),
        before_state={"browser_url": "https://expected.example/path"},
        last_strategy_used="open_url:w",
        last_strategy_ok=False,
        last_error="postcondition no cumplida",
        arl_round_done=0,
        max_extra_rounds=1,
        retry_budget=2,
    )

    assert verdict.retry_step is False
    evs = [e for e in m.adaptive_runtime_audit.get("events", []) if not e.get("skipped")]
    assert evs


def test_validator_partial_adds_deadline_addon(monkeypatch):
    _cfg(monkeypatch)

    m = Mission(name="vp")
    st = MissionStep(id="s2", kind="open_new_tab", params={})
    c = build_contract(st)
    verdict = evaluate_sep_arl_recovery(
        mission=m,
        step=st,
        contract=c,
        observed_snapshot=SimpleNamespace(),
        before_state={},
        last_strategy_used="open_new_tab:hotkey_ctrl_t",
        last_strategy_ok=True,
        last_error="postcondition no cumplida",
        arl_round_done=0,
        max_extra_rounds=1,
        retry_budget=2,
    )
    assert verdict.retry_step is True
    assert int(st.params.get("_arl_post_timeout_addon_ms") or 0) >= 2000


def test_label_buscar_to_search_maps_to_tab_button_names(monkeypatch):
    _cfg(monkeypatch)

    m = Mission(name="lbl")

    monkeypatch.setattr(
        "app.services.runtime.arl_sep_recovery.run_arl_safe_bundle",
        lambda **kw: {
            "semantic_relocation": {
                "success": True,
                "relocated_label": "Search",
                "confidence": 0.9,
            },
            "adapted_validator_expectations": {"adaptations_applied": []},
            "decision": {"decision": "ADAPT"},
            "classification": {"risk": "SAFE", "primary_type": "text_variation"},
        },
    )

    st = MissionStep(id="s1", kind="open_new_tab", params={}, human_label="Buscar nueva pestaña")
    c = build_contract(st)

    verdict = evaluate_sep_arl_recovery(
        mission=m,
        step=st,
        contract=c,
        observed_snapshot=SimpleNamespace(
            active_window_title="win",
            browser_url="chrome://tab",
            uia_visible_names=["Search", "Open"],
        ),
        before_state={"active_window_title": "win"},
        last_strategy_used="open_new_tab:uia_button",
        last_strategy_ok=False,
        last_error="botón nueva pestaña no encontrado",
        arl_round_done=0,
        max_extra_rounds=1,
        retry_budget=2,
    )
    assert verdict.retry_step is True
    prefs = st.params.get("arl_uia_control_names_try_first") or []
    flat = prefs if isinstance(prefs, list) else [prefs]
    assert any("Search" in str(x) for x in flat)


def test_open_new_tab_uia_tries_arl_names_first(monkeypatch):
    from app.services.missions.action_runner import _open_new_tab_uia  # noqa: SLF001
    from app.services.missions.execution_contracts import MissionStep
    from app.services.missions.state_detector import StateSnapshot

    clicked: list[str] = []

    class _Btn:
        def Exists(self, *_a, **_k):
            return True

        def Click(self, **_k):
            clicked.append(self._nm)

        def __init__(self, nm):
            self._nm = nm

    class _Win:
        def ButtonControl(self, **_kwargs):
            return _Btn(_kwargs.get("Name", ""))

    win = _Win()

    import types

    import app.services.missions.action_runner as ar

    monkeypatch.setattr(ar, "HAS_UIA", True)

    faux_auto = types.SimpleNamespace(GetForegroundControl=lambda: win)

    monkeypatch.setattr(ar, "auto", faux_auto, raising=False)

    st = MissionStep(
        id="t",
        kind="open_new_tab",
        params={"arl_uia_control_names_try_first": ["Search Tab"]},
    )
    assert _open_new_tab_uia(st, StateSnapshot()).ok is True
    assert "Search Tab" in clicked


def test_target_resolver_second_pass_mutates_labels(monkeypatch):
    _cfg(monkeypatch)

    m = Mission(name="resolver")
    tc = TargetContext(semantic={"human_label": "Buscar"}, uia_data={"Name": "Buscar"})
    cs = CompiledStep(target_context=tc)
    monkeypatch.setattr(
        "app.services.runtime.arl_sep_recovery.run_arl_safe_bundle",
        lambda **kw: {
            "semantic_relocation": {
                "success": True,
                "relocated_label": "Search",
            },
            "decision": {"decision": "ADAPT"},
        },
    )

    class _Snap:
        def detect(self, **kwargs):
            return SimpleNamespace()

    ok = try_target_resolver_arl_second_pass(
        mission=m,
        compiled_step=cs,
        resolution=SimpleNamespace(method="none", score=0, target_kind="none"),
        detector=_Snap(),
    )
    assert ok is True
    assert "Search" in str(cs.target_context.semantic.get("human_label"))


def test_maybe_adjust_deadline_reads_step_params() -> None:
    step = MissionStep(id="z", kind="open_new_tab", params={"_arl_post_timeout_addon_ms": 500})
    assert maybe_adjust_postcondition_deadline_ms(step, 600) == 1100


class _LearningRankStub:
    def rank_strategies(self, kind: str, strategies: list[str]) -> list[str]:
        return list(strategies)

    def record_failure(self, **_kwargs) -> None:
        pass

    def record_success(self, **_kwargs) -> None:
        pass


def test_smart_executor_arl_coords_only_retry_runs_no_strategies(monkeypatch):
    """Ronda ARL con plan sólo coords emergencia → no ejecuta runner; REQUEST_HUMAN + audit."""

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    _cfg(monkeypatch, ARL_ENABLED=True, ARL_SEP_RECOVERY_MAX_ROUNDS=1)

    snap = StateSnapshot(
        running_processes=["chrome.exe"],
        active_app="chrome.exe",
        browser_url="https://example.com/old",
        active_window_title="Old tab",
    )

    class FakeDet:
        def detect(self, **_kwargs):
            return snap

    class CountingRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run_strategy(self, strategy, step, state):
            self.calls.append(str(strategy))
            return StrategyResult(
                ok=False,
                strategy=strategy,
                message="mock fail",
                duration_ms=1,
            )

    monkeypatch.setattr(
        smart_executor_module,
        "_resolve_strategies",
        lambda _step, _contract: ["open_new_tab:vision_coords", "open_new_tab:relative_coords"],
    )
    monkeypatch.setattr(
        arl_sep_recovery_mod,
        "evaluate_sep_arl_recovery",
        lambda **kw: SepArlRecoveryVerdict(retry_step=True),
    )

    mission = Mission(name="coords-plan")
    runner = CountingRunner()
    exe = SmartMissionExecutor(
        run_id="r-coords-arl",
        state_detector=FakeDet(),
        action_runner=runner,
        checkpoints=CheckpointManager(run_id="r-coords-arl"),
        learning=_LearningRankStub(),
        mission_ref=mission,
    )

    exe._await_postcondition = lambda contract, deadline_ms=0, step=None: snap  # type: ignore[method-assign]

    res = exe.run_mission([MissionStep(id="st", kind="open_new_tab", params={})])

    assert res.status == "stopped_for_human"
    # Una pasada inicial (2 estrategias coords), ronda ARL repite 0 runs (filtradas).
    assert len(runner.calls) == 2
    assert runner.calls[-1].endswith("relative_coords")

    evt_reasons = [e.get("reason") for e in (mission.adaptive_runtime_audit or {}).get("events", [])]
    assert ARL_UNSAFE_COORDS_ONLY in evt_reasons


def test_arl_disabled_does_not_call_sep_evaluate(monkeypatch):
    """ARL_ENABLED=False: no ejecuta evaluate_sep_arl_recovery antes del recovery estándar."""

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    _cfg(monkeypatch, ARL_ENABLED=False, ARL_SEP_RECOVERY_MAX_ROUNDS=2)

    snap = StateSnapshot(
        running_processes=["chrome.exe"],
        active_app="chrome.exe",
        browser_url="https://example.com/old",
        active_window_title="Old tab",
    )

    class FakeDet:
        def detect(self, **_kwargs):
            return snap

    class FailRunner:
        def run_strategy(self, strategy, step, state):
            return StrategyResult(ok=False, strategy=strategy, message="fail", duration_ms=1)

    monkeypatch.setattr(
        smart_executor_module,
        "_resolve_strategies",
        lambda _step, _contract: ["open_new_tab:hotkey_ctrl_t"],
    )
    monkeypatch.setattr(
        arl_sep_recovery_mod,
        "evaluate_sep_arl_recovery",
        lambda **kw: (_ for _ in ()).throw(AssertionError("evaluate_sep_arl_recovery must not run")),
    )

    def _short_circuit_recovery(self, step, contract, state_after, attempts):
        raise smart_executor_module._NeedsHuman("legacy test bypass", reason="legacy_test")

    monkeypatch.setattr(SmartMissionExecutor, "_recover_postcondition_loop", _short_circuit_recovery)

    exe = SmartMissionExecutor(
        run_id="r-arl-off",
        state_detector=FakeDet(),
        action_runner=FailRunner(),
        checkpoints=CheckpointManager(run_id="r-arl-off"),
        learning=_LearningRankStub(),
    )

    exe._await_postcondition = lambda contract, deadline_ms=0, step=None: snap  # type: ignore[method-assign]

    res = exe.run_mission([MissionStep(id="st", kind="open_new_tab", params={})])

    assert res.status == "stopped_for_human"
