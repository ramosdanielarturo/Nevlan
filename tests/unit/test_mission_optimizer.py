"""
ArthurOS Unit Tests — MissionOptimizer (FASE 11 + FASE 12)
----------------------------------------------------------
Verifica las 8 reglas de optimización de misiones.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import (
    Mission, CompiledStep, InterpretedStep, TargetContext,
    ActionStrategy, FallbackStrategy, TargetResolutionStrategy,
)
from app.services.missions.optimizer import MissionOptimizer


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _make_step(
    *,
    strategy: ActionStrategy,
    payload: dict | None = None,
    uia_name: str | None = None,
    uia_aid: str | None = None,
    uia_ctype: str | None = None,
    web_locators: list | None = None,
    fallback_xy: tuple[int, int] | None = None,
    capture_score: int | None = None,
    image_ref_mid: str | None = None,
    anchor_bbox: dict | None = None,
    target_strategy: TargetResolutionStrategy = TargetResolutionStrategy.UIA_THEN_VISION,
    fallback_strategy: FallbackStrategy = FallbackStrategy.FAIL_FAST,
) -> CompiledStep:
    tc = TargetContext()
    if uia_name or uia_aid or uia_ctype:
        tc.uia_data = {
            "name": uia_name or "",
            "automation_id": uia_aid or "",
            "control_type": uia_ctype or "",
            "class_name": "",
        }
    if web_locators is not None:
        tc.web_data = {"locators": web_locators, "url": "https://example.com"}
    if fallback_xy is not None:
        tc.fallback_coords = {"x": fallback_xy[0], "y": fallback_xy[1]}
    if image_ref_mid is not None:
        tc.image_ref_mid = image_ref_mid
    if anchor_bbox is not None:
        tc.anchor_bbox = anchor_bbox
    if capture_score is not None:
        tc.confidence = {"capture_score": capture_score}
    return CompiledStep(
        action_strategy=strategy,
        action_payload=payload or {},
        target_context=tc,
        target_resolution_strategy=target_strategy,
        fallback_strategy=fallback_strategy,
    )


def _make_mission(steps: list[CompiledStep]) -> Mission:
    m = Mission(name="t")
    m.compiled_execution_graph = list(steps)
    m.interpreted_steps = [
        InterpretedStep(description=f"Step {i}") for i in range(len(steps))
    ]
    # encadenar grafo
    for i in range(len(steps) - 1):
        steps[i].next_step_id_on_success = steps[i + 1].id
    return m


# ──────────────────────────────────────────────────────────────────
# Regla 1: deduplicación
# ──────────────────────────────────────────────────────────────────

class TestRule1Dedup:
    def test_dedup_consecutive_clicks_on_same_target(self):
        s1 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="Save", uia_aid="btnSave")
        s2 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="Save", uia_aid="btnSave")
        s3 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="Cancel", uia_aid="btnCancel")
        m = _make_mission([s1, s2, s3])

        report = MissionOptimizer.optimize(m)

        assert len(m.compiled_execution_graph) == 2, \
            "El click duplicado debió eliminarse"
        assert report["removed"] >= 1

    def test_dedup_consecutive_identical_hotkeys(self):
        s1 = _make_step(strategy=ActionStrategy.SEND_HOTKEY,
                        payload={"keys": ["ctrl", "s"]})
        s2 = _make_step(strategy=ActionStrategy.SEND_HOTKEY,
                        payload={"keys": ["ctrl", "s"]})
        m = _make_mission([s1, s2])

        MissionOptimizer.optimize(m)

        assert len(m.compiled_execution_graph) == 1, \
            "Ctrl+S duplicado debió eliminarse"

    def test_drop_zero_delta_scrolls(self):
        s1 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="OK", uia_aid="btnOK")
        s2 = _make_step(strategy=ActionStrategy.SCROLL,
                        payload={"dx": 0, "dy": 0})
        s3 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="Next", uia_aid="btnNext")
        m = _make_mission([s1, s2, s3])

        MissionOptimizer.optimize(m)

        strats = [s.action_strategy for s in m.compiled_execution_graph]
        assert ActionStrategy.SCROLL not in strats, \
            "Scroll de delta 0 debió eliminarse"


# ──────────────────────────────────────────────────────────────────
# Regla 2: fusión click+texto en SET_FIELD_VALUE
# ──────────────────────────────────────────────────────────────────

class TestRule2MergeClickType:
    def test_click_on_edit_followed_by_type_text_merges(self):
        s1 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="Email", uia_aid="email",
                        uia_ctype="EditControl")
        s2 = _make_step(strategy=ActionStrategy.TYPE_TEXT,
                        payload={"text": "daniel@example.com"})
        m = _make_mission([s1, s2])

        report = MissionOptimizer.optimize(m)

        assert len(m.compiled_execution_graph) == 1
        merged = m.compiled_execution_graph[0]
        assert merged.action_strategy == ActionStrategy.SET_FIELD_VALUE
        assert merged.action_payload.get("text") == "daniel@example.com"
        assert report["merged"] >= 1


# ──────────────────────────────────────────────────────────────────
# Regla 3: TYPE_TEXT con target editable → SET_FIELD_VALUE
# ──────────────────────────────────────────────────────────────────

class TestRule3RewriteTypeText:
    def test_type_text_with_editable_target_rewrites_to_set_field_value(self):
        s = _make_step(strategy=ActionStrategy.TYPE_TEXT,
                       payload={"text": "hola"},
                       uia_name="Search", uia_aid="searchInput",
                       uia_ctype="EditControl")
        m = _make_mission([s])

        report = MissionOptimizer.optimize(m)

        assert m.compiled_execution_graph[0].action_strategy == \
            ActionStrategy.SET_FIELD_VALUE
        assert report["rewritten"] >= 1


# ──────────────────────────────────────────────────────────────────
# Regla 4: SCROLL + CLICK → SCROLL_UNTIL_VISIBLE
# ──────────────────────────────────────────────────────────────────

class TestRule4ScrollUntilVisible:
    def test_scroll_followed_by_click_promotes_scroll(self):
        s1 = _make_step(strategy=ActionStrategy.SCROLL,
                        payload={"dx": 0, "dy": -200})
        s2 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="Hidden Button", uia_aid="btnHidden")
        m = _make_mission([s1, s2])

        MissionOptimizer.optimize(m)

        # SCROLL se promueve a SCROLL_UNTIL_VISIBLE; el CLICK sigue.
        first = m.compiled_execution_graph[0]
        assert first.action_strategy == ActionStrategy.SCROLL_UNTIL_VISIBLE
        scroll_until = first.action_payload.get("scroll_until")
        assert isinstance(scroll_until, dict)
        # El click no debe perderse.
        last = m.compiled_execution_graph[-1]
        assert last.action_strategy == ActionStrategy.CLICK


# ──────────────────────────────────────────────────────────────────
# Regla 5: ráfagas de scroll → coalesce
# ──────────────────────────────────────────────────────────────────

class TestRule5CoalesceScrolls:
    def test_consecutive_scrolls_merge(self):
        s1 = _make_step(strategy=ActionStrategy.SCROLL,
                        payload={"dx": 0, "dy": -100})
        s2 = _make_step(strategy=ActionStrategy.SCROLL,
                        payload={"dx": 0, "dy": -150})
        s3 = _make_step(strategy=ActionStrategy.SCROLL,
                        payload={"dx": 0, "dy": -50})
        m = _make_mission([s1, s2, s3])

        MissionOptimizer.optimize(m)

        scrolls = [
            s for s in m.compiled_execution_graph
            if s.action_strategy in (
                ActionStrategy.SCROLL, ActionStrategy.SCROLL_UNTIL_VISIBLE
            )
        ]
        assert len(scrolls) == 1, \
            f"Tres scrolls consecutivos deberían fusionarse en uno, hay {len(scrolls)}"
        assert scrolls[0].action_payload.get("dy") == -300


# ──────────────────────────────────────────────────────────────────
# Regla 6: estrategia de coords débiles
# ──────────────────────────────────────────────────────────────────

class TestRule6StrategyUpgrade:
    def test_coords_absolute_with_image_anchor_promotes_to_icon_validated(self):
        s = _make_step(
            strategy=ActionStrategy.CLICK,
            target_strategy=TargetResolutionStrategy.COORDS_ABSOLUTE,
            fallback_xy=(100, 200),
            image_ref_mid="data:image/png;base64,xxx",
            anchor_bbox={"x": 0, "y": 0, "w": 100, "h": 50},
        )
        m = _make_mission([s])

        MissionOptimizer.optimize(m)

        assert m.compiled_execution_graph[0].target_resolution_strategy == \
            TargetResolutionStrategy.COORDINATE_ICON_VALIDATED_CLICK

    def test_coords_absolute_with_web_locator_promotes_to_uia_then_vision(self):
        s = _make_step(
            strategy=ActionStrategy.CLICK,
            target_strategy=TargetResolutionStrategy.COORDS_ABSOLUTE,
            fallback_xy=(100, 200),
            web_locators=[{"kind": "test_id", "value": "btn-x"}],
        )
        m = _make_mission([s])

        MissionOptimizer.optimize(m)

        assert m.compiled_execution_graph[0].target_resolution_strategy == \
            TargetResolutionStrategy.UIA_THEN_VISION


# ──────────────────────────────────────────────────────────────────
# Regla 7: marcar pasos peligrosos
# ──────────────────────────────────────────────────────────────────

class TestRule7DangerousSteps:
    def test_coord_only_low_score_marked_ask_human(self):
        s = _make_step(
            strategy=ActionStrategy.CLICK,
            fallback_xy=(50, 50),
            capture_score=20,  # muy bajo, sin UIA ni web
        )
        m = _make_mission([s])

        report = MissionOptimizer.optimize(m)

        assert m.compiled_execution_graph[0].fallback_strategy == \
            FallbackStrategy.ASK_HUMAN
        assert report["marked_dangerous"] >= 1

    def test_high_score_step_not_marked_dangerous(self):
        s = _make_step(
            strategy=ActionStrategy.CLICK,
            uia_name="Save", uia_aid="btnSave",
            capture_score=85,
        )
        m = _make_mission([s])

        report = MissionOptimizer.optimize(m)

        assert m.compiled_execution_graph[0].fallback_strategy != \
            FallbackStrategy.ASK_HUMAN
        assert report["marked_dangerous"] == 0


# ──────────────────────────────────────────────────────────────────
# Regla 8: mission_reliability_score
# ──────────────────────────────────────────────────────────────────

class TestRule8ReliabilityScore:
    def test_all_high_score_steps_yield_high_reliability(self):
        steps = [
            _make_step(strategy=ActionStrategy.CLICK,
                       uia_name="A", uia_aid="a", capture_score=95),
            _make_step(strategy=ActionStrategy.CLICK,
                       uia_name="B", uia_aid="b", capture_score=90),
        ]
        m = _make_mission(steps)

        report = MissionOptimizer.optimize(m)

        assert report["reliability_score"] >= 85, \
            f"Pasos sólidos deberían dar reliability ≥85, fue {report['reliability_score']}"
        # Y debería persistirse en recovery_rules.
        assert (m.recovery_rules or {}).get("mission_reliability_score") \
            == report["reliability_score"]

    def test_low_score_steps_penalize_reliability(self):
        steps = [
            _make_step(strategy=ActionStrategy.CLICK,
                       fallback_xy=(10, 10), capture_score=20),
            _make_step(strategy=ActionStrategy.CLICK,
                       fallback_xy=(20, 20), capture_score=30),
        ]
        m = _make_mission(steps)

        report = MissionOptimizer.optimize(m)

        # Con multiplicador de 0.5 cuando score<50, deberíamos quedar bajos.
        assert report["reliability_score"] < 50

    def test_empty_mission_reliability_is_100(self):
        m = _make_mission([])
        report = MissionOptimizer.optimize(m)
        assert report["reliability_score"] == 100


# ──────────────────────────────────────────────────────────────────
# Idempotencia: optimize(optimize(x)) ≈ optimize(x)
# ──────────────────────────────────────────────────────────────────

class TestIdempotence:
    def test_double_optimize_is_stable(self):
        steps = [
            _make_step(strategy=ActionStrategy.CLICK,
                       uia_name="A", uia_aid="a"),
            _make_step(strategy=ActionStrategy.CLICK,
                       uia_name="A", uia_aid="a"),  # duplicado
            _make_step(strategy=ActionStrategy.SCROLL,
                       payload={"dx": 0, "dy": -100}),
            _make_step(strategy=ActionStrategy.SCROLL,
                       payload={"dx": 0, "dy": -50}),
        ]
        m = _make_mission(steps)

        MissionOptimizer.optimize(m)
        first_count = len(m.compiled_execution_graph)
        first_score = (m.recovery_rules or {}).get("mission_reliability_score")

        MissionOptimizer.optimize(m)
        second_count = len(m.compiled_execution_graph)
        second_score = (m.recovery_rules or {}).get("mission_reliability_score")

        assert first_count == second_count
        assert first_score == second_score


# ──────────────────────────────────────────────────────────────────
# Re-link del grafo tras dedup
# ──────────────────────────────────────────────────────────────────

class TestGraphRelink:
    def test_next_step_pointers_recomputed_after_dedup(self):
        s1 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="A", uia_aid="a")
        s2 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="A", uia_aid="a")  # duplicate
        s3 = _make_step(strategy=ActionStrategy.CLICK,
                        uia_name="B", uia_aid="b")
        m = _make_mission([s1, s2, s3])

        MissionOptimizer.optimize(m)

        graph = m.compiled_execution_graph
        assert len(graph) == 2
        assert graph[0].next_step_id_on_success == graph[1].id
        assert graph[-1].next_step_id_on_success is None
