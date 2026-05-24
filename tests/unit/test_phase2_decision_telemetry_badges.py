"""
Tests for PRD 2026-05-12 §Fase 2 — Execution Decision tri-state,
Telemetry collector, y Mission Review badges.

Cubre:

  * ExecutionConfidence (SAFE_TO_EXECUTE | SAFE_WITH_CONFIRMATION |
    UNSAFE_DO_NOT_CLICK) derivado correctamente del
    TargetResolutionResult.
  * ExecutionDecision con human_message útil para el Player.
  * TelemetryCollector: counters por estrategia, latency p50/p95,
    zero_capture_rate, fingerprint_usage_rate, confirmation_rate,
    self_heal_success_rate, mission_ready_rate, etc.
  * TargetResolver registra automáticamente en el colector default.
  * Mission Review badges: compute_step_badges produce los chips
    correctos según execution_metrics / vision_data del step blob.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


# ──────────────────────────────────────────────────────────────────────
# ExecutionConfidence / ExecutionDecision
# ──────────────────────────────────────────────────────────────────────


from app.services.missions.execution_decision import (
    ExecutionConfidence,
    ExecutionDecision,
    compute_execution_confidence,
    compute_execution_decision,
)


def _res(**kw) -> SimpleNamespace:
    """Duck-typed TargetResolutionResult-shaped para tests."""
    defaults = dict(
        method="none", score=0, target_kind="none",
        needs_confirmation=False, safety_reason="",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestExecutionConfidence:
    def test_method_none_is_unsafe(self):
        r = _res(method="none", score=0)
        assert compute_execution_confidence(r) is ExecutionConfidence.UNSAFE_DO_NOT_CLICK

    def test_fingerprint_failed_is_unsafe(self):
        r = _res(method="uia_exact", score=80,
                 safety_reason="fingerprint_failed_no_blind_coords")
        # safety_reason fuerza UNSAFE incluso con score alto.
        assert compute_execution_confidence(r) is ExecutionConfidence.UNSAFE_DO_NOT_CLICK

    def test_structural_high_score_is_safe(self):
        r = _res(method="uia_exact", score=85, target_kind="uia_control")
        assert compute_execution_confidence(r) is ExecutionConfidence.SAFE_TO_EXECUTE

    def test_web_locator_high_score_is_safe(self):
        r = _res(method="web_locator", score=78, target_kind="web_locator")
        assert compute_execution_confidence(r) is ExecutionConfidence.SAFE_TO_EXECUTE

    def test_visual_fingerprint_strong_is_safe(self):
        r = _res(method="visual_fingerprint", score=82, target_kind="visual_point")
        assert compute_execution_confidence(r) is ExecutionConfidence.SAFE_TO_EXECUTE

    def test_visual_fingerprint_medium_requires_confirmation(self):
        r = _res(method="visual_fingerprint", score=70,
                 needs_confirmation=True,
                 safety_reason="visual_fingerprint_medium_confidence")
        assert compute_execution_confidence(r) is ExecutionConfidence.SAFE_WITH_CONFIRMATION

    def test_emergency_coords_requires_confirmation(self):
        r = _res(method="coords_absolute", score=50,
                 safety_reason="coords_emergency_no_fingerprint_available")
        assert compute_execution_confidence(r) is ExecutionConfidence.SAFE_WITH_CONFIRMATION

    def test_visual_fingerprint_below_strong_threshold_asks_confirmation(self):
        r = _res(method="visual_fingerprint", score=70)
        # Score 70 < 78 → no SAFE_TO_EXECUTE; pero ≥ 65 → confirmation.
        assert compute_execution_confidence(r) is ExecutionConfidence.SAFE_WITH_CONFIRMATION

    def test_score_below_65_is_unsafe(self):
        r = _res(method="visual_fingerprint", score=40)
        assert compute_execution_confidence(r) is ExecutionConfidence.UNSAFE_DO_NOT_CLICK


class TestExecutionConfidenceProperties:
    def test_is_safe_property(self):
        assert ExecutionConfidence.SAFE_TO_EXECUTE.is_safe
        assert ExecutionConfidence.SAFE_WITH_CONFIRMATION.is_safe
        assert not ExecutionConfidence.UNSAFE_DO_NOT_CLICK.is_safe

    def test_requires_confirmation_property(self):
        assert not ExecutionConfidence.SAFE_TO_EXECUTE.requires_confirmation
        assert ExecutionConfidence.SAFE_WITH_CONFIRMATION.requires_confirmation
        assert not ExecutionConfidence.UNSAFE_DO_NOT_CLICK.requires_confirmation


class TestExecutionDecision:
    def test_decision_safe_no_message(self):
        r = _res(method="uia_exact", score=90)
        d = compute_execution_decision(r, step_label="Abrir Chrome")
        assert d.proceed is True
        assert d.needs_confirmation is False
        assert d.human_message == ""
        assert d.block_reason == ""

    def test_decision_confirmation_includes_label(self):
        r = _res(method="visual_fingerprint", score=70,
                 needs_confirmation=True,
                 safety_reason="visual_fingerprint_medium_confidence")
        d = compute_execution_decision(r, step_label="Click YouTube")
        assert d.proceed is True
        assert d.needs_confirmation is True
        assert "Click YouTube" in d.human_message
        assert "confianza media" in d.human_message.lower()

    def test_decision_unsafe_fingerprint_failed_message(self):
        r = _res(method="none", score=0,
                 safety_reason="fingerprint_failed_no_blind_coords")
        d = compute_execution_decision(r, step_label="Buscar Chrome")
        assert d.proceed is False
        assert d.needs_confirmation is False
        assert d.block_reason == "fingerprint_failed_no_blind_coords"
        assert "Buscar Chrome" in d.human_message
        assert "regrab" in d.human_message.lower() or "confirma" in d.human_message.lower()

    def test_decision_unsafe_no_label_still_has_message(self):
        r = _res(method="none", score=0)
        d = compute_execution_decision(r)
        assert d.proceed is False
        assert d.human_message  # no vacío


# ──────────────────────────────────────────────────────────────────────
# TelemetryCollector
# ──────────────────────────────────────────────────────────────────────


from app.services.missions.execution_telemetry import (
    MissionOutcomeEvent,
    ResolutionEvent,
    StepOutcomeEvent,
    TelemetryCollector,
    get_default_collector,
    reset_default_collector,
)


class TestTelemetryCollector:
    def test_empty_collector_has_zero_rates(self):
        col = TelemetryCollector()
        d = col.dump_to_dict()
        assert d["counts"]["resolutions"] == 0
        for k, v in d["rates"].items():
            assert v == 0.0, f"{k} should be 0 in empty collector, got {v}"

    def test_zero_capture_rate(self):
        col = TelemetryCollector()
        # 3 resoluciones: 2 zero-capture, 1 con foto.
        for zero in (True, True, False):
            col.record_resolution(ResolutionEvent(
                method="uia_exact", score=85, target_kind="uia_control",
                latency_ms=10, zero_capture=zero, fingerprint_used=False,
                needs_confirmation=False,
            ))
        d = col.dump_to_dict()
        # 2/3 = 66.67%.
        assert d["rates"]["zero_capture_rate_pct"] == pytest.approx(66.67, abs=0.1)

    def test_fingerprint_usage_rate(self):
        col = TelemetryCollector()
        for fp in (True, True, True, False):
            col.record_resolution(ResolutionEvent(
                method="visual_fingerprint", score=80, target_kind="visual_point",
                latency_ms=120, zero_capture=False, fingerprint_used=fp,
                needs_confirmation=False,
            ))
        d = col.dump_to_dict()
        assert d["rates"]["fingerprint_usage_rate_pct"] == 75.0

    def test_confirmation_rate(self):
        col = TelemetryCollector()
        # 1 de 4 pide confirmación.
        for needs in (False, False, True, False):
            col.record_resolution(ResolutionEvent(
                method="visual_fingerprint", score=70, target_kind="visual_point",
                latency_ms=80, zero_capture=False, fingerprint_used=True,
                needs_confirmation=needs,
            ))
        d = col.dump_to_dict()
        assert d["rates"]["confirmation_rate_pct"] == 25.0

    def test_emergency_coords_rate(self):
        col = TelemetryCollector()
        col.record_resolution(ResolutionEvent(
            method="coords_absolute", score=50, target_kind="absolute_point",
            latency_ms=5, zero_capture=False, fingerprint_used=False,
            needs_confirmation=False,
            safety_reason="coords_emergency_no_fingerprint_available",
        ))
        col.record_resolution(ResolutionEvent(
            method="uia_exact", score=90, target_kind="uia_control",
            latency_ms=8, zero_capture=True, fingerprint_used=False,
            needs_confirmation=False,
        ))
        d = col.dump_to_dict()
        assert d["rates"]["emergency_coords_rate_pct"] == 50.0
        assert d["raw_counts"]["emergency_coords"] == 1

    def test_strategy_counter(self):
        col = TelemetryCollector()
        for m in ("uia_exact", "uia_exact", "visual_fingerprint", "coords_absolute"):
            col.record_resolution(ResolutionEvent(
                method=m, score=80, target_kind="x",
                latency_ms=10, zero_capture=False, fingerprint_used=False,
                needs_confirmation=False,
            ))
        d = col.dump_to_dict()
        assert d["strategy_used"]["uia_exact"] == 2
        assert d["strategy_used"]["visual_fingerprint"] == 1
        assert d["strategy_used"]["coords_absolute"] == 1

    def test_latency_per_strategy(self):
        col = TelemetryCollector()
        for ms in (10, 20, 30, 40, 50):
            col.record_resolution(ResolutionEvent(
                method="uia_exact", score=85, target_kind="uia_control",
                latency_ms=ms, zero_capture=True, fingerprint_used=False,
                needs_confirmation=False,
            ))
        stats = col.dump_to_dict()["latency_by_strategy_ms"]["uia_exact"]
        assert stats["count"] == 5
        assert stats["min_ms"] == 10
        assert stats["max_ms"] == 50
        assert stats["avg_ms"] == 30
        assert stats["p50_ms"] in (30, 30)  # mediana de [10,20,30,40,50]
        assert stats["p95_ms"] in (40, 50)

    def test_self_heal_success_rate(self):
        col = TelemetryCollector()
        col.record_step_outcome(StepOutcomeEvent(
            step_id="s1", kind="select_profile",
            status="skipped", strategy_used="skip:already_satisfied",
            self_healed=False,
        ))
        col.record_step_outcome(StepOutcomeEvent(
            step_id="s2", kind="click", status="success",
            self_healed=True,
        ))
        col.record_step_outcome(StepOutcomeEvent(
            step_id="s3", kind="click", status="failed",
            self_healed=True,
        ))
        d = col.dump_to_dict()
        # Intentos: s1 (skipped) + s2 (healed) + s3 (healed+failed) = 3.
        # Éxitos: s1 (skipped) + s2 (healed+success) = 2.
        assert d["rates"]["self_heal_success_rate_pct"] == pytest.approx(66.67, abs=0.1)

    def test_mission_ready_rate(self):
        col = TelemetryCollector()
        col.record_mission_outcome(MissionOutcomeEvent(
            mission_id="m1", intent_status="READY", has_sep=True,
            used_legacy=False,
        ))
        col.record_mission_outcome(MissionOutcomeEvent(
            mission_id="m2", intent_status="BLOCKED", has_sep=True,
            used_legacy=False,
        ))
        col.record_mission_outcome(MissionOutcomeEvent(
            mission_id="m3", intent_status="READY", has_sep=False,
            used_legacy=True,
        ))
        d = col.dump_to_dict()
        assert d["rates"]["mission_ready_rate_pct"] == pytest.approx(66.67, abs=0.1)
        assert d["rates"]["sep_generation_rate_pct"] == pytest.approx(66.67, abs=0.1)
        assert d["rates"]["fallback_to_legacy_rate_pct"] == pytest.approx(33.33, abs=0.1)

    def test_reset_clears_state(self):
        col = TelemetryCollector()
        col.record_resolution(ResolutionEvent(
            method="uia_exact", score=80, target_kind="uia_control",
            latency_ms=10, zero_capture=True, fingerprint_used=False,
            needs_confirmation=False,
        ))
        col.record_false_positive_click()
        col.record_repair_opened()
        col.reset()
        d = col.dump_to_dict()
        assert d["counts"]["resolutions"] == 0
        assert d["counts"]["false_positive_clicks"] == 0
        assert d["counts"]["repair_opened"] == 0

    def test_default_collector_is_singleton(self):
        reset_default_collector()
        c1 = get_default_collector()
        c2 = get_default_collector()
        assert c1 is c2


# ──────────────────────────────────────────────────────────────────────
# Resolver registra automáticamente en el colector default
# ──────────────────────────────────────────────────────────────────────


class TestResolverTelemetryIntegration:
    def test_resolver_records_event_on_resolve(self):
        from app.contracts.mission import (
            ActionStrategy, CompiledStep, TargetContext,
        )
        from app.services.missions.target_resolver import TargetResolver

        reset_default_collector()
        col = get_default_collector()
        initial = col.dump_to_dict()["counts"]["resolutions"]

        tc = TargetContext()
        tc.fallback_coords = {"x": 10, "y": 20}
        step = CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            target_context=tc,
        )
        resolver = TargetResolver()
        result = resolver.resolve(step)

        d = col.dump_to_dict()
        # Al menos +1 resolution.
        assert d["counts"]["resolutions"] >= initial + 1
        # Tiene una entrada con el método del ganador.
        assert result.method in d["strategy_used"]


# ──────────────────────────────────────────────────────────────────────
# Mission Review badges
# ──────────────────────────────────────────────────────────────────────


from app.services.missions.mission_review_summary import (
    BADGE_EMERGENCY_COORDS,
    BADGE_SELF_HEALED,
    BADGE_SEMANTIC,
    BADGE_VISUAL_INSUFFICIENT,
    BADGE_VISUAL_MEDIUM,
    BADGE_VISUAL_STRONG,
    BADGE_ZERO_CAPTURE,
    compute_step_badges,
)


class TestStepBadges:
    def test_empty_step_has_no_badges(self):
        assert compute_step_badges({}) == []
        assert compute_step_badges(None) == []  # type: ignore[arg-type]

    def test_semantic_badge_from_type(self):
        b = compute_step_badges({"type": "open_app"})
        assert BADGE_SEMANTIC in b

    def test_semantic_badge_from_intent(self):
        b = compute_step_badges({"semantic": {"intent": "select_profile"}})
        assert BADGE_SEMANTIC in b

    def test_click_only_has_no_semantic_badge(self):
        # Un step legacy tipo "click" sin intención NO debe tener
        # BADGE_SEMANTIC — eso es ruido.
        b = compute_step_badges({"type": "click", "semantic": {}})
        assert BADGE_SEMANTIC not in b

    def test_zero_capture_badge(self):
        b = compute_step_badges({
            "type": "open_app",
            "execution_metrics": {"zero_capture": True},
        })
        assert BADGE_ZERO_CAPTURE in b

    def test_visual_strong_badge(self):
        b = compute_step_badges({
            "type": "click",
            "target_context": {
                "vision_data": {
                    "visual_fingerprint": {
                        "available": True, "quality": "strong",
                    }
                }
            },
        })
        assert BADGE_VISUAL_STRONG in b

    def test_visual_medium_badge(self):
        b = compute_step_badges({
            "target_context": {
                "vision_data": {
                    "visual_fingerprint": {
                        "available": True, "quality": "medium",
                    }
                }
            },
        })
        assert BADGE_VISUAL_MEDIUM in b

    def test_visual_insufficient_badge(self):
        b = compute_step_badges({
            "target_context": {
                "vision_data": {
                    "visual_fingerprint": {
                        "available": True, "quality": "insufficient",
                    }
                }
            },
        })
        assert BADGE_VISUAL_INSUFFICIENT in b

    def test_self_healed_badge_from_strategy(self):
        b = compute_step_badges({
            "execution_metrics": {
                "strategy_used": "skip:already_satisfied",
            },
        })
        assert BADGE_SELF_HEALED in b

    def test_self_healed_badge_from_flag(self):
        b = compute_step_badges({
            "execution_metrics": {"self_healed": True},
        })
        assert BADGE_SELF_HEALED in b

    def test_emergency_coords_badge(self):
        b = compute_step_badges({
            "execution_metrics": {
                "safety_reason": "coords_emergency_no_fingerprint_available",
            },
        })
        assert BADGE_EMERGENCY_COORDS in b

    def test_full_combo_badges_present(self):
        """Step con todo: semantic + zero-capture + visual strong +
        self-healed. Verificamos que NINGÚN badge se pierda."""
        b = compute_step_badges({
            "type": "open_app",
            "execution_metrics": {
                "zero_capture": True,
                "self_healed": True,
            },
            "target_context": {
                "vision_data": {
                    "visual_fingerprint": {
                        "available": True, "quality": "strong",
                    }
                }
            },
        })
        assert BADGE_SEMANTIC in b
        assert BADGE_ZERO_CAPTURE in b
        assert BADGE_VISUAL_STRONG in b
        assert BADGE_SELF_HEALED in b
