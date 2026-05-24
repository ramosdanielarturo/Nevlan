"""
ArthurOS Unit Tests — TargetResolver helpers (FASE 5 + FASE 12)
---------------------------------------------------------------
Tests aislados de los helpers puros del resolver. Las rutas que requieren
UIA/Playwright vivos no se prueban aquí (dependen de runtime real); en
cambio probamos lo que sí se puede aislar:

  - ``_success_score_for``: porcentaje histórico por método.
  - ``_bonus_history``: bonificación que se aplica a un método con buen
    historial.
  - Estructura de ``TargetResolutionResult``: ``is_high_confidence``,
    mapeo método→target_kind.
"""
from __future__ import annotations

from app.contracts.mission import CompiledStep, TargetContext, ActionStrategy
from app.services.missions.target_resolver import (
    TargetResolutionResult, _success_score_for, _bonus_history, _METHOD_KIND,
)


def _make_step(history: dict | None = None) -> CompiledStep:
    tc = TargetContext()
    if history is not None:
        tc.confidence = {"success_by_method": history}
    return CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=tc,
    )


# ──────────────────────────────────────────────────────────────────
# success_score_for: estadística histórica por método
# ──────────────────────────────────────────────────────────────────

class TestSuccessScore:
    def test_no_history_returns_zero(self):
        step = _make_step()
        assert _success_score_for(step, "uia_exact") == 0

    def test_full_success_returns_100(self):
        step = _make_step({"web_locator": {"ok": 5, "fail": 0}})
        assert _success_score_for(step, "web_locator") == 100

    def test_full_failure_returns_0(self):
        step = _make_step({"visual_anchor": {"ok": 0, "fail": 4}})
        assert _success_score_for(step, "visual_anchor") == 0

    def test_mixed_outcomes(self):
        step = _make_step({"uia_exact": {"ok": 3, "fail": 1}})
        # 3 / 4 = 75%
        assert _success_score_for(step, "uia_exact") == 75


# ──────────────────────────────────────────────────────────────────
# bonus_history: pequeño boost a métodos con buen historial
# ──────────────────────────────────────────────────────────────────

class TestBonusHistory:
    def test_no_history_no_bonus(self):
        step = _make_step()
        assert _bonus_history(step, "web_locator") == 0

    def test_low_success_no_bonus(self):
        step = _make_step({"web_locator": {"ok": 1, "fail": 4}})  # 20%
        assert _bonus_history(step, "web_locator") == 0

    def test_medium_success_small_bonus(self):
        step = _make_step({"web_locator": {"ok": 6, "fail": 4}})  # 60%
        assert _bonus_history(step, "web_locator") == 4

    def test_high_success_full_bonus(self):
        step = _make_step({"web_locator": {"ok": 9, "fail": 1}})  # 90%
        assert _bonus_history(step, "web_locator") == 8


# ──────────────────────────────────────────────────────────────────
# TargetResolutionResult invariants
# ──────────────────────────────────────────────────────────────────

class TestResultStruct:
    def test_high_confidence_threshold(self):
        # Score 64: NO high confidence
        r = TargetResolutionResult(method="uia_exact", score=64,
                                    target_kind="uia_control")
        assert not r.is_high_confidence()
        # Score 65: high confidence
        r = TargetResolutionResult(method="uia_exact", score=65,
                                    target_kind="uia_control")
        assert r.is_high_confidence()

    def test_method_none_never_high_confidence(self):
        r = TargetResolutionResult(method="none", score=99, target_kind="none")
        assert not r.is_high_confidence()

    def test_method_kind_mapping(self):
        # Regresión: el mapeo declarado en el módulo cubre TODOS los métodos
        # que el resolver puede emitir.
        for method, expected_kind in {
            "web_locator": "web_locator",
            "uia_exact": "uia_control",
            "uia_semantic": "uia_control",
            "visual_anchor": "visual_point",
            "rel_window": "relative_point",
            "coords_absolute": "absolute_point",
            "none": "none",
        }.items():
            assert _METHOD_KIND.get(method) == expected_kind, \
                f"_METHOD_KIND[{method!r}] debería ser {expected_kind!r}"

    def test_default_bundle_label(self):
        r = TargetResolutionResult(method="web_locator", score=90,
                                    target_kind="web_locator")
        assert r.bundle_used == "primary"
