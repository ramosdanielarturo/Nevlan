"""
Unit tests para ``app.services.missions.approval_gate``.

Cubre los 5 invariantes (PRD 2026-05-03 §E):

  * temp_paths
  * validation_none
  * red_step
  * invalid_click_target
  * orphan_type_text
"""
from __future__ import annotations

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    InterpretedStep,
    Mission,
    RawEvent,
    EventType,
    MouseAction,
    TargetContext,
    ValidationStrategy,
)
from app.contracts.mission import MissionStatus
from app.services.missions.approval_gate import (
    ApprovalReport,
    can_approve,
    check_mission_approvable,
    classify_status,
    is_executable,
)


def _step(action: ActionStrategy, *,
          uia_name: str = "OK",
          uia_aid: str = "btnOk",
          uia_ctype: str = "ButtonControl",
          process: str = "chrome.exe",
          payload: dict | None = None,
          validation: ValidationStrategy = ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
          quality: str = "green",
          image_ref: str | None = None,
          ) -> CompiledStep:
    tc = TargetContext(
        uia_data={
            "name": uia_name, "automation_id": uia_aid,
            "control_type": uia_ctype,
        },
        process_name=process,
        image_ref=image_ref,
        confidence={"quality_level": quality, "score": 0.85},
    )
    return CompiledStep(
        action_strategy=action,
        action_payload=payload or {},
        target_context=tc,
        validation_strategy=validation,
    )


def _mission(steps: list[CompiledStep]) -> Mission:
    m = Mission(name="t", id="m1")
    m.compiled_execution_graph = steps
    m.interpreted_steps = [
        InterpretedStep(description=str(i), raw_event_ids=[]) for i in range(len(steps))
    ]
    return m


class TestApprovalGate:
    def test_approves_when_all_invariants_satisfied(self):
        m = _mission([_step(ActionStrategy.CLICK)])
        report = check_mission_approvable(m)
        assert report.ok, f"violations={[v.code for v in report.violations]}"

    def test_rejects_validation_none(self):
        m = _mission([
            _step(ActionStrategy.CLICK, validation=ValidationStrategy.NONE),
        ])
        report = check_mission_approvable(m)
        assert not report.ok
        assert any(v.code == "validation_none" for v in report.violations)

    def test_rejects_red_step_when_execution_also_weak(self):
        """PRD 2026-05-06b §4: capture rojo SOLO bloquea si la
        execution_confidence también es débil (sin name/locator)."""
        m = _mission([
            _step(
                ActionStrategy.CLICK,
                quality="red",
                uia_name="",
                uia_aid="",
                uia_ctype="GroupControl",
            ),
        ])
        report = check_mission_approvable(m)
        assert not report.ok
        codes = {v.code for v in report.violations}
        # Cae como invalid_click_target o empty_target (ambos bloquean
        # por la misma razón: el executor no puede resolver).
        assert codes & {"invalid_click_target", "empty_target", "red_step"}

    def test_red_capture_with_strong_execution_only_warns(self):
        """PRD 2026-05-06b §4: un CLICK con capture rojo pero UIA AID
        sólido NO debe bloquear; queda como warning informativo."""
        m = _mission([
            _step(
                ActionStrategy.CLICK,
                quality="red",
                uia_name="OK",
                uia_aid="btnOk",
                uia_ctype="ButtonControl",
            ),
        ])
        report = check_mission_approvable(m)
        assert report.ok, [v.code for v in report.violations]
        codes = {v.code for v in report.violations}
        assert "red_step" not in codes
        assert "weak_capture_strong_execution" in codes

    def test_rejects_invalid_click_target(self):
        m = _mission([
            _step(
                ActionStrategy.CLICK,
                uia_name="", uia_aid="", uia_ctype="GroupControl",
            ),
        ])
        report = check_mission_approvable(m)
        assert not report.ok
        assert any(v.code == "invalid_click_target" for v in report.violations)

    def test_rejects_orphan_type_text(self):
        # TYPE_TEXT sin field_session ni submit_after ni merged_from_fragments.
        m = _mission([
            _step(
                ActionStrategy.TYPE_TEXT,
                payload={"text": "hello"},
                validation=ValidationStrategy.REQUIRE_FIELD_VALUE,
            ),
        ])
        report = check_mission_approvable(m)
        assert any(v.code == "orphan_type_text" for v in report.violations)

    def test_accepts_type_text_with_submit_after(self):
        m = _mission([
            _step(
                ActionStrategy.TYPE_TEXT,
                payload={"text": "x", "submit_after": True},
                validation=ValidationStrategy.REQUIRE_FIELD_VALUE,
            ),
        ])
        report = check_mission_approvable(m)
        assert report.ok

    def test_accepts_type_text_with_field_session(self):
        m = _mission([
            _step(
                ActionStrategy.TYPE_TEXT,
                payload={"text": "x", "field_session": True},
                validation=ValidationStrategy.REQUIRE_FIELD_VALUE,
            ),
        ])
        report = check_mission_approvable(m)
        assert report.ok

    def test_rejects_temp_paths(self, tmp_path):
        ev = RawEvent(
            event_type=EventType.MOUSE_CLICK,
            mouse_action=MouseAction(x=1, y=1, button="left"),
            snapshot_ref="C:/var/missions/assets/temp/x_small.png",
        )
        m = _mission([_step(ActionStrategy.CLICK)])
        m.raw_trace = [ev]
        report = check_mission_approvable(m)
        assert not report.ok
        assert any(v.code == "temp_paths" for v in report.violations)

    def test_rejects_empty_graph(self):
        m = Mission(name="t", id="empty")
        report = check_mission_approvable(m)
        assert not report.ok
        assert any(v.code == "empty_graph" for v in report.violations)

    def test_can_approve_shortcut(self):
        m = _mission([_step(ActionStrategy.CLICK)])
        assert can_approve(m) is True
        m.compiled_execution_graph[0].validation_strategy = ValidationStrategy.NONE
        assert can_approve(m) is False

    def test_red_step_skipped_for_non_visual_actions(self):
        """LAUNCH_APP / SET_FIELD_VALUE / OPEN_NEW_TAB no se ejecutan por
        bbox visual; un quality_level=red heredado del raw_event NO debe
        bloquear su aprobación."""
        m = _mission([
            _step(ActionStrategy.LAUNCH_APP, quality="red",
                  payload={"app_name": "Chrome"}),
            _step(ActionStrategy.OPEN_NEW_TAB, quality="red",
                  payload={"app": "chrome"}),
            _step(ActionStrategy.OPEN_BOOKMARK, quality="red",
                  payload={"name": "Y", "url": "https://y/"}),
            _step(ActionStrategy.SET_FIELD_VALUE, quality="red",
                  payload={"text": "x", "submit_after": True},
                  validation=ValidationStrategy.REQUIRE_FIELD_VALUE),
        ])
        report = check_mission_approvable(m)
        assert all(v.code != "red_step" for v in report.violations), \
            f"unexpected red_step: {[v.code for v in report.violations]}"

    def test_no_double_report_red_and_needs_user_label(self):
        """Un step ya marcado needs_reinforcement no debe disparar
        adicionalmente red_step (los UI tendrían dos errores para una
        única causa)."""
        m = _mission([
            _step(
                ActionStrategy.SELECT_PROFILE,
                quality="red",
                payload={
                    "profile": "",
                    "needs_reinforcement": True,
                    "label_prompt": "¿Qué perfil seleccionaste?",
                },
                validation=ValidationStrategy.REQUIRE_TEXT_MATCH,
            ),
        ])
        report = check_mission_approvable(m)
        codes = [v.code for v in report.violations]
        assert "needs_user_label" in codes
        assert "red_step" not in codes


class TestStatusClassification:
    """``classify_status`` (PRD 2026-05-03c §1)."""

    def test_executable_when_clean(self):
        m = _mission([
            _step(ActionStrategy.LAUNCH_APP, payload={"app_name": "Chrome"}),
        ])
        assert classify_status(m) == MissionStatus.EXECUTABLE
        assert is_executable(m) is True

    def test_needs_review_for_user_label(self):
        m = _mission([
            _step(
                ActionStrategy.SELECT_PROFILE,
                payload={"profile": "", "needs_user_label": True,
                         "label_prompt": "¿Cuál?"},
                validation=ValidationStrategy.REQUIRE_TEXT_MATCH,
            ),
        ])
        assert classify_status(m) == MissionStatus.NEEDS_REVIEW

    def test_compiled_draft_for_system_fixable(self):
        m = _mission([
            _step(ActionStrategy.CLICK, validation=ValidationStrategy.NONE),
        ])
        assert classify_status(m) == MissionStatus.COMPILED_DRAFT

    def test_empty_profile_routes_to_needs_review(self):
        m = _mission([
            _step(
                ActionStrategy.SELECT_PROFILE,
                payload={"profile": ""},
                validation=ValidationStrategy.REQUIRE_TEXT_MATCH,
            ),
        ])
        assert classify_status(m) == MissionStatus.NEEDS_REVIEW

    def test_orphan_enter_blocks_approval(self):
        """Un Enter suelto sin texto adyacente bloquea aprobación."""
        m = _mission([
            _step(
                ActionStrategy.SEND_HOTKEY,
                payload={"hotkey": "enter"},
                validation=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
            ),
        ])
        report = check_mission_approvable(m)
        assert any(v.code == "orphan_enter" for v in report.violations)
        assert classify_status(m) == MissionStatus.COMPILED_DRAFT

    def test_search_with_site_but_no_target_label_does_not_block(self):
        """PRD 2026-05-06b §4: target visual faltante NO bloquea cuando
        el executor tiene una cadena DOM/UIA/OCR robusta. Antes
        bloqueaba con ``empty_target_label``; ahora queda informativo
        y la misión sigue siendo EJECUTABLE.
        """
        m = _mission([
            _step(
                ActionStrategy.SET_FIELD_VALUE,
                payload={
                    "text": "Devuélveme el amor Luis Miguel",
                    "submit_after": True,
                    "site": "youtube",
                    # target_label vacío → antes bloqueaba.
                },
                validation=ValidationStrategy.REQUIRE_FIELD_VALUE,
            ),
        ])
        report = check_mission_approvable(m)
        codes = {v.code for v in report.violations}
        # No bloqueante.
        assert "empty_target_label" not in codes
        # Pero sí informa.
        assert "weak_capture_strong_execution" in codes
        assert classify_status(m) == MissionStatus.EXECUTABLE

    def test_low_confidence_only_with_real_score(self):
        """Sin capture_score real, low_confidence NO debe disparar."""
        m = _mission([_step(ActionStrategy.CLICK)])
        # _step() no rellena capture_score → no debería bloquear.
        codes = {v.code for v in check_mission_approvable(m).violations}
        assert "low_confidence" not in codes
