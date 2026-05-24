"""
Unit tests para ``app.interfaces.desktop.quick_reinforcement`` — sólo
testeamos las funciones puras (``steps_needing_reinforcement``,
``apply_reinforcement``, ``delete_step``); el diálogo Qt necesitaría
``QApplication`` headless y se cubre en e2e.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    InterpretedStep,
    Mission,
    MissionStatus,
    TargetContext,
    ValidationStrategy,
)
from app.interfaces.desktop.quick_reinforcement import (
    apply_reinforcement,
    delete_step,
    steps_needing_reinforcement,
)
from app.services.missions.approval_gate import classify_status


def _step(action: ActionStrategy, *,
          payload: dict | None = None,
          process: str = "chrome.exe",
          ) -> CompiledStep:
    return CompiledStep(
        action_strategy=action,
        action_payload=payload or {},
        target_context=TargetContext(process_name=process),
        validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    )


def _mission(steps: list[CompiledStep]) -> Mission:
    m = Mission(name="t", id="qr1")
    m.compiled_execution_graph = steps
    m.interpreted_steps = [
        InterpretedStep(description=str(i), raw_event_ids=[]) for i in range(len(steps))
    ]
    return m


class TestStepsNeedingReinforcement:
    def test_select_profile_with_empty_profile_listed(self):
        m = _mission([
            _step(
                ActionStrategy.SELECT_PROFILE,
                payload={"profile": "", "needs_user_label": True,
                         "label_prompt": "¿Cuál?"},
            ),
        ])
        pending = steps_needing_reinforcement(m)
        assert len(pending) == 1
        i, cs, hints = pending[0]
        assert i == 0
        assert hints["prompt"] == "¿Cuál?"

    def test_search_with_site_no_target_label_listed(self):
        m = _mission([
            _step(
                ActionStrategy.SUBMIT_SEARCH,
                payload={"text": "x", "submit_after": True, "site": "youtube"},
            ),
        ])
        pending = steps_needing_reinforcement(m)
        assert len(pending) == 1
        # El placeholder es UI; debe pintar una pista no vacía y no
        # debe mencionar un sitio concreto (placeholders hardcodeados
        # como "youtube_search_box" eran un anti-patrón).
        ph = pending[0][2]["placeholder"]
        assert isinstance(ph, str) and ph.strip()
        assert "youtube" not in ph.lower()

    def test_clean_step_not_listed(self):
        m = _mission([
            _step(ActionStrategy.LAUNCH_APP, payload={"app_name": "Chrome"}),
        ])
        assert steps_needing_reinforcement(m) == []

    def test_click_with_needs_reinforcement_listed(self):
        m = _mission([
            _step(
                ActionStrategy.CLICK,
                payload={"needs_reinforcement": True,
                         "label_prompt": "¿Sobre qué?"},
            ),
        ])
        pending = steps_needing_reinforcement(m)
        assert len(pending) == 1


class TestApplyReinforcement:
    def test_label_clears_pending_flags_and_makes_executable(self):
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "", "needs_user_label": True,
                     "label_prompt": "¿Cuál?"},
        )
        cs.validation_strategy = ValidationStrategy.REQUIRE_TEXT_MATCH
        m = _mission([cs])
        # Antes: NEEDS_REVIEW
        assert classify_status(m) == MissionStatus.NEEDS_REVIEW

        apply_reinforcement(cs, label="Daniel Arturo Ramos")

        # Después: EXECUTABLE (todo limpio).
        assert classify_status(m) == MissionStatus.EXECUTABLE
        assert cs.action_payload["profile"] == "Daniel Arturo Ramos"
        assert "needs_user_label" not in cs.action_payload

    def test_target_label_for_search(self):
        cs = _step(
            ActionStrategy.SUBMIT_SEARCH,
            payload={"text": "x", "submit_after": True, "site": "youtube"},
        )
        cs.validation_strategy = ValidationStrategy.REQUIRE_FIELD_VALUE
        apply_reinforcement(cs, label="youtube_search_box")
        assert cs.action_payload["target_label"] == "youtube_search_box"

    def test_empty_label_is_noop(self):
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "", "needs_user_label": True},
        )
        apply_reinforcement(cs, label="")
        # Sigue pendiente.
        assert cs.action_payload.get("needs_user_label") is True


class TestDeleteStep:
    def test_removes_step_in_bounds(self):
        a = _step(ActionStrategy.LAUNCH_APP)
        b = _step(ActionStrategy.CLICK)
        m = _mission([a, b])
        assert delete_step(m, 1) is True
        assert m.compiled_execution_graph == [a]
        assert len(m.interpreted_steps) == 1

    def test_out_of_bounds_returns_false(self):
        m = _mission([_step(ActionStrategy.CLICK)])
        assert delete_step(m, 5) is False
        assert delete_step(m, -1) is False


# ──────────────────────────────────────────────────────────────────────
# PRD 2026-05-07 §5 — perfil genérico debe rebotar
# ──────────────────────────────────────────────────────────────────────


class TestGenericProfileLabel:
    def test_generic_label_raises(self):
        from app.interfaces.desktop.quick_reinforcement import (
            _GenericProfileLabelError,
        )
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "", "needs_user_label": True},
        )
        with pytest.raises(_GenericProfileLabelError):
            apply_reinforcement(cs, label="perfil del navegador")

    def test_default_is_rejected(self):
        from app.interfaces.desktop.quick_reinforcement import (
            _GenericProfileLabelError,
        )
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "", "needs_user_label": True},
        )
        with pytest.raises(_GenericProfileLabelError):
            apply_reinforcement(cs, label="default")

    def test_real_name_is_accepted(self):
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "", "needs_user_label": True},
        )
        cs.validation_strategy = ValidationStrategy.REQUIRE_TEXT_MATCH
        apply_reinforcement(cs, label="Daniel Arturo Ramos")
        assert cs.action_payload["profile"] == "Daniel Arturo Ramos"
        assert "needs_user_label" not in cs.action_payload


# ──────────────────────────────────────────────────────────────────────
# PRD 2026-05-07 §1 — sync del semantic_execution_plan tras refuerzo
# ──────────────────────────────────────────────────────────────────────


class TestSyncSemanticPlan:
    def test_profile_synced_into_plan(self):
        from app.interfaces.desktop.quick_reinforcement import (
            sync_semantic_plan_after_reinforcement,
        )
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "Daniel Arturo Ramos"},
        )
        m = _mission([cs])
        m.semantic_execution_plan = {
            "version": 1,
            "steps": [
                {
                    "id": "s1",
                    "type": "select_profile",
                    "params": {"app": "chrome", "profile": ""},
                    "needs_user_label": True,
                    "preferred_strategy": "select_profile:skip_if_loaded",
                    "fallback_strategies": [],
                    "confidence": 0.75,
                    "human_label": "Seleccionar perfil",
                },
            ],
        }
        sync_semantic_plan_after_reinforcement(m)
        plan_step = m.semantic_execution_plan["steps"][0]
        assert plan_step["params"]["profile"] == "Daniel Arturo Ramos"
        assert plan_step["needs_user_label"] is False

    def test_no_op_when_profile_still_empty(self):
        from app.interfaces.desktop.quick_reinforcement import (
            sync_semantic_plan_after_reinforcement,
        )
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": ""},
        )
        m = _mission([cs])
        m.semantic_execution_plan = {
            "version": 1,
            "steps": [
                {
                    "id": "s1",
                    "type": "select_profile",
                    "params": {"app": "chrome", "profile": ""},
                    "needs_user_label": True,
                    "preferred_strategy": "select_profile:skip_if_loaded",
                    "fallback_strategies": [],
                    "confidence": 0.75,
                    "human_label": "Seleccionar perfil",
                },
            ],
        }
        sync_semantic_plan_after_reinforcement(m)
        plan_step = m.semantic_execution_plan["steps"][0]
        # No tocamos el plan: aún needs_user_label.
        assert plan_step["needs_user_label"] is True
        assert plan_step["params"]["profile"] == ""
