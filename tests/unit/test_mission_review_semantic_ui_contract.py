"""Contrato Mission Review: vista normal SIPE-first (sin ruido legacy).

Compara el modelo JSON de la misión (SEP) con lo que la capa de
presentación expone en vista normal — sin instanciar Qt.

PRD 2026-05-16: si ``semantic_execution_plan.source`` es SIPE, el usuario
no debe ver click_fallback / use_coords / capture_score en la lista
semántica ni en ``render_normal_view``.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import apply_collapse_to_mission
from app.services.missions.mission_review_summary import (
    build_mission_review_summary,
    filter_badges_for_normal_semantic_view,
    render_normal_view,
)
from app.services.missions.semantic_execution_plan import attach_semantic_plan_to_mission
from app.services.missions.semantic_intent_promotion_engine import (
    apply_promotion_to_mission,
)
from app.services.missions.semantic_review_display import (
    NORMAL_VIEW_SIPE_FORBIDDEN_SUBSTRINGS,
    is_semantic_intent_promotion_plan,
    plan_declares_coords_used,
    use_semantic_primary_normal_mission_review,
)
@pytest.fixture
def mission_sipe_canonical():
    """Misión con SEP promovido por SIPE (traza alineada con suite SIPE)."""
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parent / "test_sipe_telemetry_persistence_catalog.py"
    spec = importlib.util.spec_from_file_location("_sipe_tel_mod", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    trace_fn = getattr(mod, "_canonical_chrome_youtube_trace")

    from app.contracts.mission import MissionStatus

    m = Mission(name="mr-contract", raw_trace=trace_fn())
    apply_collapse_to_mission(m)
    attach_semantic_plan_to_mission(m, force=True)
    m.status = MissionStatus.EXECUTABLE
    apply_promotion_to_mission(m)
    assert is_semantic_intent_promotion_plan(m)
    return m


class TestSemanticUiContract:

    def test_summary_render_has_no_legacy_tokens(self, mission_sipe_canonical):
        s = build_mission_review_summary(mission_sipe_canonical)
        text = render_normal_view(s).lower()
        for bad in NORMAL_VIEW_SIPE_FORBIDDEN_SUBSTRINGS:
            assert bad.lower() not in text, f"unexpected token {bad!r} in:\n{text}"

    def test_scroll_results_collapsed_in_semantic_plan(self, mission_sipe_canonical):
        """Los scrolls consecutivos quedan en un único paso ``scroll_results``."""
        sep = mission_sipe_canonical.semantic_execution_plan or {}
        scroll_steps = [
            s for s in (sep.get("steps") or [])
            if (s or {}).get("type") == "scroll_results"
        ]
        assert len(scroll_steps) == 1, (
            "un único paso semántico para los scrolls agregados"
        )
        params = (scroll_steps[0].get("params") or {})
        assert int(params.get("amount") or 0) >= 1

    def test_use_semantic_primary_flag_matches_expert_toggle(self, mission_sipe_canonical):
        assert use_semantic_primary_normal_mission_review(
            mission_sipe_canonical, expert_mode=False,
        )
        assert not use_semantic_primary_normal_mission_review(
            mission_sipe_canonical, expert_mode=True,
        )

    def test_plan_coords_flag_exposed(self, mission_sipe_canonical):
        c = plan_declares_coords_used(mission_sipe_canonical)
        assert c is False or c is None or c is True

    def test_filter_badges_strips_visual_noise(self):
        raw = ["📸 Visual insuficiente", "🧠 Semantic", "⚡ Zero-capture"]
        out = filter_badges_for_normal_semantic_view(raw)
        assert "Visual insuficiente" not in " ".join(out)
        assert "🧠 Semantic" in " ".join(out)
