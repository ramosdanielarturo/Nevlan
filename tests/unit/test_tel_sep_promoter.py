"""TEL→SEP Hybrid Promotion — Fase 2 (determinista, sin fixtures de marca)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import pytest

from app.contracts.mission import Mission, MissionStatus, OperationalTruthBlock, OperationalTruthStatus
from app.services.missions.semantic_execution_plan import SemanticExecutionPlan
from app.services.missions.tel_sep_promoter import (
    compare_tel_sep_vs_existing_sep,
    decide_tel_promotion,
    merge_tel_sep_with_existing_sep,
    run_tel_sep_hybrid_promotion,
    TelSepValidationResult,
    build_sep_from_truth_blocks,
    map_truth_block_to_sep_step,
    validate_tel_sep_ready,
)


@dataclass
class _DummySettings:
    TEL_SEP_PROMOTION_ENABLED: bool = True
    TEL_SEP_PROMOTION_MODE: str = "shadow"
    TEL_SEP_PROMOTION_MIN_CONFIDENCE: float = 0.5
    TEL_SEP_PROMOTION_REQUIRE_READY: bool = False
    TEL_SEP_MAX_AMBIGUITY_AGGREGATE: float = 0.9
    TEL_SEP_AMBIGUITY_GATE_ENABLED: bool = False


def _tb(
    truth_type: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    amb: float = 0.2,
    readiness: float = 0.9,
    human: str = "",
) -> OperationalTruthBlock:
    return OperationalTruthBlock(
        truth_type=truth_type,
        canonical_action=truth_type,
        human_intent=human or truth_type.replace("_", " "),
        stability_score=0.8,
        operational_confidence=readiness,
        ambiguity_score=amb,
        execution_readiness=readiness,
        temporal_signature={"started_at_iso": "2026-05-10T12:00:05+00:00"},
        context_window={
            "tel_derived_params": dict(params or {}),
        },
        truth_status=OperationalTruthStatus.COMMITTED_SHADOW,
    )


def test_map_open_application_to_open_app():
    m = Mission(name="x", status=MissionStatus.DRAFT)
    tb = _tb("open_application", params={"name": "ReporterSuite"}, human="Launch host")
    st = map_truth_block_to_sep_step(tb, m, last_nav_url={})
    assert st is not None and st.type == "open_app"
    assert str(st.params.get("name")).lower().find("reporter") >= 0
    banned = ("uia_text_match", "smart_route", "keypress_enter", "mouse_scroll")
    hay = st.preferred_strategy.lower()
    assert not any(b in hay for b in banned)


def test_map_navigate_to_open_url_without_brand_hardcode():
    m = Mission(name="x", status=MissionStatus.DRAFT)
    host = "https://portal.vendor.example/tasks"
    tb = _tb("navigate_to_location", params={"url": host}, human="Go runbook")
    st = map_truth_block_to_sep_step(tb, m, last_nav_url={})
    assert st.type == "open_url"
    assert host.split("//")[-1] in str(st.params.get("url"))
    ps = st.preferred_strategy.lower()
    assert "ctrl+l" not in ps and "ctrl_" not in ps


def test_search_and_browse_generates_scroll_results():
    m = Mission(name="x", status=MissionStatus.DRAFT)
    truths = (
        _tb("search_content", params={"query": "Quarterly anomalies", "site": "internal"}),
        _tb("browse_results", params={"amount": 4}, human="Scroll feed"),
    )
    plan = build_sep_from_truth_blocks(m, truths)
    kinds = [s.type for s in plan.steps]
    assert kinds[0] == "search_content"
    assert kinds[1] == "scroll_results"
    hay = kinds[0] + plan.steps[0].preferred_strategy.lower()
    assert "uia_text_match" not in hay


def test_open_tab_maps_open_new_tab_sanitized():
    m = Mission(name="x", status=MissionStatus.DRAFT)
    tb = _tb("open_tab", human="New browsing surface")
    st = map_truth_block_to_sep_step(tb, m, last_nav_url={})
    assert st.type == "open_new_tab"
    ps = st.preferred_strategy.lower()
    assert "ctrl_" not in ps


def test_fill_and_submit_mapped():
    m = Mission(name="x", status=MissionStatus.DRAFT)
    truths = (_tb("fill_form", params={"field": "a1"}), _tb("submit_form", params={}))
    plan = build_sep_from_truth_blocks(m, truths)
    assert plan.steps[0].type == "fill_form"
    assert plan.steps[1].type == "submit_form"


def test_candidate_has_no_primary_mechanics_type_field():
    m = Mission(name="y", status=MissionStatus.DRAFT)
    plan = build_sep_from_truth_blocks(m, [_tb("open_application", params={"name": "DeskHost"})])
    for s in plan.steps:
        assert s.type not in {"uia_text_match", "smart_route", "dom_click", "keypress_enter"}


def test_uic_validation_accepts_nominal_candidate():
    m = Mission(name="z", status=MissionStatus.DRAFT)
    plan = build_sep_from_truth_blocks(
        m,
        [
            _tb(
                "navigate_to_location",
                params={"url": "https://app.supplier.invalid/landing"},
            ),
            _tb(
                "search_content",
                params={"query": "PO-993", "provider": "internal"},
                human='Search "PO-993"',
            ),
        ],
    )
    vr = validate_tel_sep_ready(m, plan, settings=_DummySettings())
    assert vr.ok


def test_merge_keeps_extra_sep_when_tel_misses_middle():
    sep_blob = {
        "steps": [
            {"id": "s1", "type": "open_app", "preferred_strategy": "x", "confidence": 0.5},
            {"id": "s2", "type": "open_url", "preferred_strategy": "y", "confidence": 0.5},
        ],
    }
    tel_blob = {
        "steps": [{"id": "t1", "type": "open_app", "block_id": "b1", "human_label": "H1"}],
    }
    out = merge_tel_sep_with_existing_sep(tel_blob, sep_blob)
    kinds = [s.get("type") for s in out["steps"]]
    assert kinds == ["open_app", "open_url"]
    merged = next(s for s in out["steps"] if s.get("type") == "open_app")
    assert merged.get("merged_from") == "tel+sep_compatible"
    trailing = out["steps"][-1]
    assert trailing["type"] == "open_url" and trailing.get("merged_from") == "sep_kept_truth_gap"


def test_conflict_keeps_sep_decision(monkeypatch):
    mission = Mission(name="conf", status=MissionStatus.DRAFT)
    mission.semantic_execution_plan = {"steps": [{"type": "open_app", "confidence": 0.7}], "source": "x"}
    mission.truth_blocks = [_tb("open_application", params={"name": "DeskHost"}, amb=0.1)]

    def _fake_merge(*_: Any, **__: Any):
        return {"steps": [], "conflicts": ["forced_conflict"], "sep_only_indices_kept": []}

    import app.services.missions.tel_sep_promoter as tsp

    monkeypatch.setattr(tsp, "merge_tel_sep_with_existing_sep", _fake_merge)
    monkeypatch.setattr(tsp, "validate_tel_sep_ready", lambda *a, **k: TelSepValidationResult(ok=True, reasons=[]))

    st = _DummySettings()
    st.TEL_SEP_PROMOTION_ENABLED = True
    st.TEL_SEP_PROMOTION_MODE = "primary"
    st.TEL_SEP_PROMOTION_REQUIRE_READY = False
    run_tel_sep_hybrid_promotion(mission, st)
    dec = mission.tel_sep_promotion_decision or {}
    assert dec.get("decision") == "CONFLICT_KEEP_SEP"
    assert not dec.get("promote")


def test_shadow_mode_preserves_sep_blob():
    m = Mission(name="sh", status=MissionStatus.DRAFT)
    orig = {
        "source": "semantic_intent_promotion_engine",
        "steps": [{"type": "open_app", "id": "legacy", "confidence": 0.9}],
        "version": 1,
    }
    m.semantic_execution_plan = dict(orig)
    m.truth_blocks = [_tb("open_application", params={"name": "DeskHost"}, amb=0.1)]

    st = _DummySettings()
    st.TEL_SEP_PROMOTION_MODE = "shadow"
    run_tel_sep_hybrid_promotion(m, st)
    assert m.semantic_execution_plan == orig


def test_suggest_writes_audit_but_no_promote_even_if_ready(monkeypatch):
    m = Mission(name="su", status=MissionStatus.DRAFT)
    m.semantic_execution_plan = {"steps": [{"type": "open_app"}], "average_confidence": 0.7}
    m.truth_blocks = [_tb("open_application", params={"name": "DeskHost"}, amb=0.08)]
    monkeypatch.setattr(
        "app.services.missions.tel_sep_promoter.validate_tel_sep_ready",
        lambda *a, **k: TelSepValidationResult(ok=True, reasons=[]),
    )
    st = _DummySettings()
    st.TEL_SEP_PROMOTION_MODE = "suggest"
    st.TEL_SEP_PROMOTION_REQUIRE_READY = False
    before = dict(m.semantic_execution_plan or {})
    run_tel_sep_hybrid_promotion(m, st)
    assert m.semantic_execution_plan == before
    dec = m.tel_sep_promotion_decision or {}
    assert dec.get("promote") is False
    assert m.tel_semantic_execution_plan_candidate is not None


def test_primary_promotes_when_ready_gate_passes(monkeypatch):
    m = Mission(name="pr", status=MissionStatus.DRAFT)
    m.semantic_execution_plan = {
        "steps": [{"type": "open_app", "id": "a", "confidence": 0.4}],
        "source": "semantic_intent_promotion_engine",
        "version": 1,
    }
    m.truth_blocks = [_tb("open_application", params={"name": "DeskHost"}, amb=0.05)]

    monkeypatch.setattr(
        "app.services.missions.tel_sep_promoter.validate_tel_sep_ready",
        lambda *a, **k: TelSepValidationResult(ok=True, reasons=[]),
    )

    vs = compare_tel_sep_vs_existing_sep(
        {"steps": [{"type": "open_app"}]},
        m.semantic_execution_plan,
    )
    monkeypatch.setattr(
        "app.services.missions.tel_sep_promoter.compare_tel_sep_vs_existing_sep",
        lambda *_a, **_k: vs,
    )

    st = _DummySettings()
    st.TEL_SEP_PROMOTION_MODE = "primary"
    st.TEL_SEP_PROMOTION_REQUIRE_READY = False

    blob_before = dict(m.semantic_execution_plan or {})
    run_tel_sep_hybrid_promotion(m, st)

    blob_after = m.semantic_execution_plan or {}
    assert blob_after.get("source") == "tel_sep_promoter_primary"
    assert blob_before != blob_after


def test_ambiguity_aggregate_blocks_via_decision():
    vr = TelSepValidationResult(ok=True, reasons=[])
    dec = decide_tel_promotion(
        mode="primary",
        enabled=True,
        tel_plan=SemanticExecutionPlan(steps=[]),
        existing_blob={"steps": []},
        comparison={"tel_steps_count": 1, "existing_sep_steps_count": 1, "tel_leak_penalty": 0, "sep_leak_penalty": 1},
        validation=vr,
        truth_blocks_used=1,
        min_confidence=0.1,
        require_ready=False,
        max_truth_ambiguity=0.1,
        avg_truth_ambiguity=0.9,
        has_conflicts_in_merge_preview=False,
    )
    assert dec.get("decision") != "PROMOTE_TEL_PRIMARY"


def test_coords_primary_param_blocks_validation():
    m = Mission(name="crd", status=MissionStatus.DRAFT)
    plan = build_sep_from_truth_blocks(m, [_tb("open_application", params={})])
    # inject coords post-build
    d = plan.to_dict()
    st0 = dict((d["steps"] or [{}])[0])
    st0["params"] = {"bbox": {"x": 1}}
    d["steps"] = [st0]
    plan2 = SemanticExecutionPlan.from_dict(d)
    vr = validate_tel_sep_ready(m, plan2, settings=_DummySettings())
    assert not vr.ok
    assert any("COORDS_PRIMARY_FORBIDDEN" in x for x in vr.reasons)


def test_legacy_mission_no_truth_survives_promotion_stub():
    m = Mission(name="old", status=MissionStatus.DRAFT)
    m.semantic_execution_plan = {"steps": [{"type": "open_app"}], "average_confidence": 0.7}
    m.truth_blocks = []
    audit = run_tel_sep_hybrid_promotion(m, _DummySettings())
    assert audit.get("truth_blocks_used") == 0


def test_compare_reports_compression_ratio_when_sep_longer_than_tel():
    tel = {"steps": [{"type": "open_app"}, {"type": "open_url"}]}
    sep = {"steps": [{"type": "open_app"}, {"type": "open_url"}, {"type": "scroll_results"}]}
    r = compare_tel_sep_vs_existing_sep(tel, sep)
    assert r["compression_ratio"] == pytest.approx(1.5, rel=1e-3)
