"""Unit tests — Adaptive Runtime Layer (Fase 1)."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.runtime import adaptive_runtime_layer as arl
from app.services.runtime.arl_metrics import reset_arl_metrics


def setup_function() -> None:
    reset_arl_metrics()


def test_detect_no_deviation_empty_states() -> None:
    snap = arl.detect_runtime_deviation(
        recorded_signature={},
        observed_state={},
        session_context={},
    )
    assert snap["deviation_detected"] is False


def test_classify_unknown_target_is_no_safe() -> None:
    dev = {
        "deviation_detected": True,
        "deviation_types": ["unknown_target"],
        "magnitude_estimate": 0.9,
    }
    c = arl.classify_deviation(dev)
    assert c["risk"] == "NO_SAFE"
    assert c["primary_type"] == "unknown_target"


def test_multiple_ambiguous_risk_flag() -> None:
    dev = {
        "deviation_detected": True,
        "deviation_types": ["layout_shift"],
        "risk_flags": ["multiple_ambiguous_signals"],
        "ambiguous_target_count": 3,
    }
    c = arl.classify_deviation(dev)
    assert c["risk"] == "NO_SAFE"
    assert c["primary_type"] == "multiple_possible_targets"


def test_semantic_relocation_success() -> None:
    cls = {"risk": "SAFE", "primary_type": "text_variation", "kinds": ["text_variation"]}
    r = arl.attempt_semantic_relocation(
        visible_label_candidates=["Buscar contenido local", "Otro botón"],
        recorded_target_fragment="search",
        classification=cls,
    )
    assert r["success"] is True
    assert r.get("relocated_label")


def test_structural_relocation_on_minor_dom() -> None:
    cls = {"risk": "SAFE", "primary_type": "minor_dom_change", "kinds": ["minor_dom_change"]}
    r = arl.attempt_structural_relocation(
        recorded_dom_weak="open results listing item alpha beta gamma footer",
        observed_dom_weak="open results listings item alpha beta gamma footer chrome",
        classification=cls,
    )
    assert r["attempted"] is True
    assert r["success"] is True


def test_contextual_revalidation_high_ambiguity() -> None:
    out = arl.attempt_contextual_revalidation(
        truth_signals={"execution_truth_confirmed": True, "sep_alignment_score": 0.99},
        ambiguity={"count": 4},
        deviation_snapshot={"deviation_detected": True},
    )
    assert out["ok"] is False


def test_operational_continuity_host_unknown_target_bad() -> None:
    cnt = arl.validate_operational_continuity(
        mission_plan_slice={
            "invariant_host": "example.com",
            "browser_host_observed": "other.net",
            "expected_queries": [],
            "cob_action": "navigate_to_site",
        },
        classifier_primary_type="unknown_target",
        deviation_kinds=["unknown_target"],
    )
    assert cnt["ok"] is False


def test_adapt_validator_expectations_relaxes_for_text_variance() -> None:
    adapted = arl.adapt_validator_expectations({}, ["text_variation", "label_translation"])
    assert adapted.get("text_match_mode") == "semantic_loose_jaccard"
    assert "relax_text_match" in (adapted.get("adaptations_applied") or [])


def test_decide_retry_budget_empty() -> None:
    d = arl.decide_continue_vs_fallback(
        classification={"risk": "SAFE", "primary_type": "layout_shift", "kinds": ["layout_shift"]},
        semantic_relocation={"success": False, "attempted": True},
        structural_relocation={"success": False, "attempted": False},
        continuity={"ok": True},
        revalidation={"ok": False},
        ambiguity={"count": 0},
        retry_budget=0,
    )
    assert d["decision"] == arl.ArlDecision.FALLBACK_TO_SEP.value


def test_decide_human_on_unsafe_with_flag() -> None:
    d = arl.decide_continue_vs_fallback(
        classification={"risk": "NO_SAFE", "primary_type": "unknown_target", "kinds": ["unknown_target"]},
        semantic_relocation={"success": False},
        structural_relocation={"success": False},
        continuity={"ok": False},
        revalidation={"ok": False},
        ambiguity={"count": 0},
        retry_budget=2,
        human_escalation_preferred=True,
    )
    assert d["decision"] == arl.ArlDecision.REQUEST_HUMAN.value


def test_run_bundle_reach_adapt_via_relocation(monkeypatch) -> None:
    monkeypatch.setattr(
        arl,
        "detect_runtime_deviation",
        lambda **_: {
            "deviation_detected": True,
            "deviation_types": ["minor_dom_change", "text_variation"],
            "magnitude_estimate": 0.2,
        },
    )
    monkeypatch.setattr(arl, "classify_deviation", lambda _: {"risk": "SAFE", "primary_type": "text_variation", "kinds": ["text_variation", "minor_dom_change"]})

    monkeypatch.setattr(
        arl,
        "attempt_semantic_relocation",
        lambda **__: {"attempted": True, "success": True, "relocated_label": "Search", "confidence": 0.9},
    )
    monkeypatch.setattr(
        arl,
        "attempt_structural_relocation",
        lambda **__: {"attempted": True, "success": False},
    )

    b = arl.run_arl_safe_bundle(
        recorded_signature={"target_label_exact": "x", "dom_fingerprint_weak": "ab"},
        observed_state={"visible_label_candidates": ["Search"], "dom_fingerprint_weak": "abc"},
        session_context=None,
        truth_signals={"sep_alignment_score": 0.4},
        ambiguity={"count": 0},
        mission_plan_slice={"invariant_host": "", "browser_host_observed": "", "expected_queries": [], "cob_action": ""},
        validator_spec={},
        retry_budget=2,
    )
    assert b["decision"]["decision"] == arl.ArlDecision.ADAPT.value


def test_append_audit_updates_mission() -> None:
    m = SimpleNamespace(adaptive_runtime_audit=None)
    arl.arl_append_audit_event(m, {"type": "test", "k": 1})
    assert isinstance(getattr(m, "adaptive_runtime_audit"), dict)
    assert len(m.adaptive_runtime_audit["events"]) == 1
