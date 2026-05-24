"""
ArthurOS Unit Tests — Repair Learning (FASE 8 + FASE 12)
--------------------------------------------------------
Verifica que las correcciones del usuario se acumulan como ``alternates``
y que tras 3 éxitos consecutivos un alternate se promueve a primary.
"""
from __future__ import annotations

from app.contracts.mission import (
    CompiledStep, TargetContext, ActionStrategy,
)
from app.services.missions.repair import (
    apply_repair_to_step, record_method_outcome,
    record_bundle_success, PROMOTION_STREAK,
)


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _make_step_with_primary(name: str = "Original", aid: str = "btnOriginal"):
    tc = TargetContext()
    tc.uia_data = {
        "name": name, "automation_id": aid,
        "control_type": "ButtonControl", "class_name": "Button",
    }
    tc.fallback_coords = {"x": 100, "y": 200}
    return CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=tc,
    )


# ──────────────────────────────────────────────────────────────────
# apply_repair_to_step
# ──────────────────────────────────────────────────────────────────

class TestApplyRepair:
    def test_repair_adds_alternate_without_replacing_primary(self):
        step = _make_step_with_primary()
        new_bundle = {
            "uia_data": {
                "name": "New Name", "automation_id": "btnNew",
                "control_type": "ButtonControl",
            },
            "fallback_coords": {"x": 500, "y": 600},
        }

        apply_repair_to_step(step, new_bundle, replace_primary=False)

        # Primary intacto.
        assert step.target_context.uia_data["automation_id"] == "btnOriginal"
        # Alternate registrado.
        assert len(step.target_context.alternates) == 1
        assert step.target_context.alternates[0]["uia_data"]["automation_id"] == \
            "btnNew"

    def test_repair_with_replace_primary_overwrites(self):
        step = _make_step_with_primary()
        new_bundle = {
            "uia_data": {
                "name": "New", "automation_id": "btnNew",
                "control_type": "ButtonControl",
            },
            "fallback_coords": {"x": 500, "y": 600},
        }

        apply_repair_to_step(step, new_bundle, replace_primary=True)

        assert step.target_context.uia_data["automation_id"] == "btnNew"
        assert step.target_context.fallback_coords == {"x": 500, "y": 600}

    def test_alternates_are_capped_at_5(self):
        step = _make_step_with_primary()
        for i in range(8):
            apply_repair_to_step(
                step,
                {"uia_data": {"automation_id": f"alt-{i}"}},
            )

        # Solo conservamos los últimos 5.
        alts = step.target_context.alternates
        assert len(alts) == 5
        # El primero debió ser descartado (FIFO).
        ids = [a["uia_data"]["automation_id"] for a in alts]
        assert "alt-0" not in ids
        assert "alt-7" in ids


# ──────────────────────────────────────────────────────────────────
# record_method_outcome
# ──────────────────────────────────────────────────────────────────

class TestMethodOutcome:
    def test_records_success_per_method(self):
        step = _make_step_with_primary()
        record_method_outcome(step, "uia_aid", ok=True)
        record_method_outcome(step, "uia_aid", ok=True)
        record_method_outcome(step, "vision", ok=False)

        stats = step.target_context.confidence["success_by_method"]
        assert stats["uia_aid"] == {"ok": 2, "fail": 0}
        assert stats["vision"] == {"ok": 0, "fail": 1}

    def test_last_success_method_tracked(self):
        step = _make_step_with_primary()
        record_method_outcome(step, "uia_aid", ok=True)
        record_method_outcome(step, "vision", ok=True)

        assert step.target_context.confidence["last_success_method"] == "vision"


# ──────────────────────────────────────────────────────────────────
# record_bundle_success: promotion logic
# ──────────────────────────────────────────────────────────────────

class TestBundlePromotion:
    def test_alternate_promoted_after_streak(self):
        step = _make_step_with_primary("Old", "btnOld")
        # Añadimos un alternate que es el "elemento correcto" según el
        # usuario.
        apply_repair_to_step(step, {
            "uia_data": {
                "name": "Real", "automation_id": "btnReal",
                "control_type": "ButtonControl",
            },
            "fallback_coords": {"x": 300, "y": 400},
        })

        # Tres éxitos consecutivos del alternate.
        for _ in range(PROMOTION_STREAK):
            record_bundle_success(step, "alternate:0", ok=True)

        # El alternate debería haberse promovido a primary.
        assert step.target_context.uia_data["automation_id"] == "btnReal", \
            "Tras 3 éxitos seguidos, el alternate debe ser el nuevo primary"
        # Y el primary anterior debería conservarse como alternate.
        alts = step.target_context.alternates
        assert any(
            (a.get("uia_data") or {}).get("automation_id") == "btnOld"
            for a in alts
        ), "El primary anterior debe quedar como alternate (no perder lo aprendido)"

    def test_failure_resets_streak(self):
        step = _make_step_with_primary()
        apply_repair_to_step(step, {
            "uia_data": {"automation_id": "alt-1",
                         "control_type": "ButtonControl"},
        })

        record_bundle_success(step, "alternate:0", ok=True)
        record_bundle_success(step, "alternate:0", ok=True)
        record_bundle_success(step, "alternate:0", ok=False)  # ← reset
        # Dos éxitos más NO deberían promover (streak reseteado).
        record_bundle_success(step, "alternate:0", ok=True)
        record_bundle_success(step, "alternate:0", ok=True)

        # Primary sigue siendo el original.
        assert step.target_context.uia_data["automation_id"] == "btnOriginal"

    def test_primary_success_tracked_separately(self):
        step = _make_step_with_primary()
        record_bundle_success(step, "primary", ok=True)
        record_bundle_success(step, "primary", ok=True)
        record_bundle_success(step, "primary", ok=False)

        conf = step.target_context.confidence
        assert conf.get("primary_total_ok") == 2
        assert conf.get("primary_total_fail") == 1


# ──────────────────────────────────────────────────────────────────
# Integración: repair → 3 éxitos → primary nuevo
# ──────────────────────────────────────────────────────────────────

class TestEndToEndRepair:
    def test_user_correction_then_three_runs_swap_primary(self):
        # Escenario PRD: el usuario corrige el botón, y tras tres
        # ejecuciones exitosas el sistema "aprende" y usa el alternate
        # como primary.
        step = _make_step_with_primary("Wrong", "btnWrong")
        original_primary_aid = step.target_context.uia_data["automation_id"]

        apply_repair_to_step(step, {
            "uia_data": {
                "name": "Right", "automation_id": "btnRight",
                "control_type": "ButtonControl",
            },
        })

        # Antes de la promoción: primary sigue "Wrong".
        assert step.target_context.uia_data["automation_id"] == \
            original_primary_aid

        # Tres ejecuciones exitosas via alternate.
        for _ in range(PROMOTION_STREAK):
            record_bundle_success(step, "alternate:0", ok=True)

        # Tras promoción: primary es "Right".
        assert step.target_context.uia_data["automation_id"] == "btnRight"
        # Y el viejo "Wrong" está accesible como alternate (no perdido).
        all_aids = {step.target_context.uia_data["automation_id"]}
        for a in (step.target_context.alternates or []):
            uia = a.get("uia_data") or {}
            if uia.get("automation_id"):
                all_aids.add(uia["automation_id"])
        assert "btnWrong" in all_aids
