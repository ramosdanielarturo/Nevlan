"""Unit tests — COB Execution Pilot (Fase 1)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.contracts.mission import CanonicalOperationalBlock, Mission
from app.services.missions import cob_execution_pilot as cep
from app.services.missions.action_runner import ActionRunner, StrategyResult
from app.services.missions.cob_pilot_metrics import reset_cob_pilot_metrics
from app.services.missions.cob_runtime_adapter import build_cob_runtime_adapter_report
from app.services.missions.cob_adapter_dry_run_bridge import run_cob_adapter_dry_run_bridge
from app.services.missions.state_detector import StateDetector, StateSnapshot


_RS = {"mode": "hybrid", "primary": "stub_semantic_route_no_coords"}
_TRUTH_OK = {"trusted_pre_action_identity": True}


@dataclass
class _PilotCfg:
    COB_EXECUTION_PILOT_ENABLED: bool = True
    COB_EXECUTION_PILOT_SAFE_ONLY: bool = True
    COB_EXECUTION_MAX_STEPS: int = 64
    COB_EXECUTION_REQUIRE_DRY_RUN: bool = True
    ARL_ENABLED: bool = False
    ARL_MAX_RETRIES: int = 2


def _patch_cfg(monkeypatch, **kwargs) -> _PilotCfg:
    cfg = _PilotCfg()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    monkeypatch.setattr(cep, "get_settings", lambda: cfg)
    return cfg


def _make_mission_with_cobs(
    *,
    cobs,
    sep_steps_extra=None,
):
    sep = {
        "steps": [
            {"type": "open_app", "params": {"app": "Edge"}},
            {"type": "open_new_tab", "params": {}},
            {"type": "open_url", "params": {"url": "https://x.test"}},
            {"type": "search_content", "params": {"query": "q", "site": "youtube"}},
            *list(sep_steps_extra or []),
        ]
    }
    m = Mission(name="pilot_t")
    m.semantic_execution_plan = sep
    m.canonical_operational_blocks = list(cobs)
    build_cob_runtime_adapter_report(m, persist=True)
    run_cob_adapter_dry_run_bridge(m, persist=True, rebuild_adapter=False)
    return m


def _fake_runner_ok(self, strategy, step, state):
    return StrategyResult(ok=True, strategy=strategy, message="ok", duration_ms=0)


def _fake_runner_fail(self, strategy, step, state):
    return StrategyResult(ok=False, strategy=strategy, message="fail", duration_ms=0)


def _fake_detect(self, deep=False, want_url=True):
    return StateSnapshot()


@pytest.fixture(autouse=True)
def _reset_metrics():
    reset_cob_pilot_metrics()
    yield
    reset_cob_pilot_metrics()


def test_open_application_pilot_success(monkeypatch):
    _patch_cfg(monkeypatch)
    real_eval = cep.evaluate_cob_execution_eligibility

    def _open_app_eligibility(mission, cob, paired=None, settings=None):
        r = dict(
            real_eval(mission, cob, paired=paired, settings=settings),
        )
        if cob.canonical_action == "open_application":
            sc = dict(r.get("scores") or {})
            sc["dry_run_supported_score"] = 1.0
            return {
                **r,
                "eligible": True,
                "failures": [],
                "scores": sc,
                "overall_score": max(float(r.get("overall_score") or 0), 0.86),
            }
        return r

    monkeypatch.setattr(cep, "evaluate_cob_execution_eligibility", _open_app_eligibility)
    cob = CanonicalOperationalBlock(
        id="c1",
        canonical_action="open_application",
        block_type="launcher_session",
        session_id="s1",
        params={"query_preview": "Edge"},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
        ambiguity={},
        semantic_ambiguity_snapshot={},
    )
    m = _make_mission_with_cobs(cobs=[cob], sep_steps_extra=[])
    monkeypatch.setattr(ActionRunner, "run_strategy", _fake_runner_ok)
    monkeypatch.setattr(StateDetector, "detect", _fake_detect)
    out = cep.execute_cob_pilot_step(m, cob)
    assert out["ok"] is True


def test_navigate_pilot_success(monkeypatch):
    _patch_cfg(monkeypatch)
    cob = CanonicalOperationalBlock(
        id="c2",
        canonical_action="navigate_to_site",
        block_type="navigation_session",
        session_id="s2",
        params={"url_preview": "https://x.test"},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
    )
    m = _make_mission_with_cobs(cobs=[cob])
    monkeypatch.setattr(ActionRunner, "run_strategy", _fake_runner_ok)
    monkeypatch.setattr(StateDetector, "detect", _fake_detect)
    assert cep.execute_cob_pilot_step(m, cob)["ok"] is True


def test_open_new_tab_pilot_success(monkeypatch):
    _patch_cfg(monkeypatch)
    cob = CanonicalOperationalBlock(
        id="ct",
        canonical_action="open_new_tab",
        block_type="navigation_session",
        session_id="st",
        params={},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
    )
    m = _make_mission_with_cobs(cobs=[cob])
    monkeypatch.setattr(ActionRunner, "run_strategy", _fake_runner_ok)
    monkeypatch.setattr(StateDetector, "detect", _fake_detect)
    assert cep.execute_cob_pilot_step(m, cob)["ok"] is True


def test_arl_audit_on_aggregate_failure_when_enabled(monkeypatch):
    _patch_cfg(monkeypatch, ARL_ENABLED=True)
    cob = CanonicalOperationalBlock(
        id="ct_arl",
        canonical_action="open_new_tab",
        block_type="navigation_session",
        session_id="sar",
        params={},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
        ambiguity={},
        semantic_ambiguity_snapshot={},
    )
    m = Mission(name="arl_fail")
    m.semantic_execution_plan = {
        "steps": [
            {"type": "open_app", "params": {"app": "Edge"}},
            {"type": "open_new_tab", "params": {}},
        ],
    }
    m.canonical_operational_blocks = [cob]
    build_cob_runtime_adapter_report(m, persist=True)
    run_cob_adapter_dry_run_bridge(m, persist=True, rebuild_adapter=False)

    monkeypatch.setattr(ActionRunner, "run_strategy", _fake_runner_fail)
    monkeypatch.setattr(StateDetector, "detect", _fake_detect)

    out = cep.execute_cob_pilot_step(m, cob)
    assert out["ok"] is False
    aud = getattr(m, "adaptive_runtime_audit", None)
    assert isinstance(aud, dict)
    kinds = [e.get("type") for e in (aud.get("events") or [])]
    assert "cob_aggregate_failure" in kinds


def test_search_and_scroll_pilot_success(monkeypatch):
    _patch_cfg(monkeypatch)
    cob = CanonicalOperationalBlock(
        id="cs",
        canonical_action="search_content_and_browse_results",
        block_type="search_session",
        session_id="ss",
        params={"query_preview": "q", "site": "youtube"},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
    )
    m = _make_mission_with_cobs(cobs=[cob])
    monkeypatch.setattr(ActionRunner, "run_strategy", _fake_runner_ok)
    monkeypatch.setattr(StateDetector, "detect", _fake_detect)
    r = cep.execute_cob_pilot_step(m, cob)
    assert r["ok"] is True


def test_search_validator_blocks_without_site_or_provider(monkeypatch):
    _patch_cfg(monkeypatch)
    cob = CanonicalOperationalBlock(
        id="cv",
        canonical_action="search_content_and_browse_results",
        block_type="search_session",
        session_id="sv",
        params={"query_preview": "qq"},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
    )
    m = _make_mission_with_cobs(cobs=[cob])
    monkeypatch.setattr(ActionRunner, "run_strategy", _fake_runner_ok)
    monkeypatch.setattr(StateDetector, "detect", _fake_detect)
    r = cep.execute_cob_pilot_step(m, cob)
    assert r["ok"] is False or r.get("errors")


def test_missing_params_blocks_eligibility(monkeypatch):
    _patch_cfg(monkeypatch)
    cob = CanonicalOperationalBlock(
        id="cm",
        canonical_action="navigate_to_site",
        block_type="navigation_session",
        session_id="sm",
        params={},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
    )
    m = _make_mission_with_cobs(cobs=[cob])
    e = cep.evaluate_cob_execution_eligibility(m, cob)
    assert e["eligible"] is False


def test_coords_primary_blocks_dry_row(monkeypatch):
    _patch_cfg(monkeypatch)
    cob = CanonicalOperationalBlock(
        id="cx",
        canonical_action="open_application",
        block_type="launcher_session",
        session_id="sx",
        params={"query_preview": "X"},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
    )
    m = _make_mission_with_cobs(cobs=[cob])
    flat = m.cob_runtime_adapter_shadow.get("flat_translated_steps") or []
    if flat:
        flat[0]["strategy"] = {"primary": "evil:coords_absolute_only", "safe_fallbacks": []}
        flat[0]["fallback_policy"] = {
            "primary": "evil:coords_absolute_only",
            "safe_fallbacks": [],
            "rejected_coord_fallbacks": [],
        }
    run_cob_adapter_dry_run_bridge(m, persist=True, rebuild_adapter=False)
    e = cep.evaluate_cob_execution_eligibility(m, cob)
    assert e["eligible"] is False


def test_unsupported_primitive_whitelist(monkeypatch):
    _patch_cfg(monkeypatch)
    cob = CanonicalOperationalBlock(
        id="cu",
        canonical_action="select_visible_entity",
        block_type="selection_session",
        session_id="su",
        params={"label_hint": "A"},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
    )
    m = _make_mission_with_cobs(cobs=[cob])
    e = cep.evaluate_cob_execution_eligibility(m, cob)
    assert e["eligible"] is False


def test_compare_pilot_vs_sep_runtime():
    m = Mission(name="cmp")
    cob = CanonicalOperationalBlock(
        id="c1",
        canonical_action="navigate_to_site",
        block_type="navigation_session",
        session_id="s",
        replay_strategy=dict(_RS),
    )
    d = cep.compare_cob_vs_sep_runtime(
        {"c1": 500.0},
        mission=m,
        cob_sequence=[cob],
    )
    assert d.get("avg_delta_estimate_ms") is not None


def test_pilot_disabled_runtime_legacy(monkeypatch):
    _patch_cfg(monkeypatch, COB_EXECUTION_PILOT_ENABLED=False)
    m = _make_mission_with_cobs(
        cobs=[
            CanonicalOperationalBlock(
                id="c",
                canonical_action="open_application",
                block_type="launcher_session",
                session_id="s",
                params={"query_preview": "Edge"},
                execution_truth_snapshot=dict(_TRUTH_OK),
                replay_strategy=dict(_RS),
            ),
        ],
    )
    assert cep.cob_pilot_try_prefix_or_fallback(m, semantic_step_count=2) is None


def test_dry_run_required_missing_shadow(monkeypatch):
    cfg = _patch_cfg(monkeypatch, COB_EXECUTION_REQUIRE_DRY_RUN=True)
    assert cfg.COB_EXECUTION_REQUIRE_DRY_RUN is True
    cob = CanonicalOperationalBlock(
        id="cd",
        canonical_action="open_application",
        block_type="launcher_session",
        session_id="sd",
        params={"query_preview": "Edge"},
        execution_truth_snapshot=dict(_TRUTH_OK),
        replay_strategy=dict(_RS),
    )
    m = Mission(name="dry")
    m.semantic_execution_plan = {"steps": [{"type": "open_app", "params": {"app": "Edge"}}]}
    m.canonical_operational_blocks = [cob]
    build_cob_runtime_adapter_report(m, persist=True)
    run_cob_adapter_dry_run_bridge(m, persist=True, rebuild_adapter=False)
    assert m.cob_dry_run_shadow is not None
    m.cob_dry_run_shadow = None
    ev = cep.evaluate_cob_execution_eligibility(m, cob)
    assert ev["eligible"] is False


def test_orchestrate_prefix_fallback(monkeypatch):
    _patch_cfg(monkeypatch)
    cobs = [
        CanonicalOperationalBlock(
            id="a",
            canonical_action="open_application",
            block_type="launcher_session",
            session_id="s1",
            params={"query_preview": "Edge"},
            execution_truth_snapshot=dict(_TRUTH_OK),
            replay_strategy=dict(_RS),
            execution_priority=10,
        ),
        CanonicalOperationalBlock(
            id="b",
            canonical_action="navigate_to_site",
            block_type="navigation_session",
            session_id="s2",
            params={"url_preview": "https://y.test"},
            execution_truth_snapshot=dict(_TRUTH_OK),
            replay_strategy=dict(_RS),
            execution_priority=20,
        ),
    ]
    m = _make_mission_with_cobs(cobs=cobs)
    monkeypatch.setattr(ActionRunner, "run_strategy", _fake_runner_ok)
    monkeypatch.setattr(StateDetector, "detect", _fake_detect)
    pilot = cep.cob_pilot_try_prefix_or_fallback(m, semantic_step_count=4)
    assert pilot is not None
    assert pilot["audit_final"]
