"""
Tests V2: Recorder + Compiler + Pre-Save pipeline.
==================================================

Cubren:

  * raw_event_sanitizer  — drop ruido, dup clicks, scroll coalesce.
  * field_session_manager — anti DevueDevue, sesiones aisladas, Enter ⇒ submit.
  * invalid_target_resolver — rescates Chrome profile / new tab / YouTube.
  * wait_manager — condiciones puras (sin tocar disco).
  * intent_compiler_v2  — pipeline E2E sobre traces sintéticos.
  * approval_gate (V2) — empty_target / ghost_missing.

Los tests son puros (sin display, sin browser). Usan el stub de
``conftest.py`` para pyautogui.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from app.contracts.mission import (
    ActionStrategy, CompiledStep, EventType, KeyboardAction, Mission,
    MouseAction, RawEvent, TargetContext, ValidationStrategy, WindowContext,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers de construcción de RawEvent
# ──────────────────────────────────────────────────────────────────────

_BASE_TS = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)


def _click(x: int, y: int, ts_offset_ms: int = 0, *, hwnd: int = 1, title: str = "App", proc: str = "app.exe", uia: dict = None) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        mouse_action=MouseAction(x=x, y=y),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata={"uia_data": uia or {"name": "Botón OK"}},
    )


def _type(text: str, ts_offset_ms: int = 0, *, hwnd: int = 1, title: str = "App", proc: str = "app.exe") -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        keyboard_action=KeyboardAction(keys=list(text)),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _enter(ts_offset_ms: int = 0, *, hwnd: int = 1, title: str = "App", proc: str = "app.exe") -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        keyboard_action=KeyboardAction(keys=["enter"]),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _scroll(dy: int, ts_offset_ms: int = 0, *, hwnd: int = 1) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_SCROLL,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        window_context=WindowContext(hwnd=hwnd, title="App", process_name="app.exe"),
        metadata={"dy": dy},
    )


def _activate(ts_offset_ms: int = 0, *, hwnd: int = 1, title: str = "App", proc: str = "app.exe") -> RawEvent:
    return RawEvent(
        event_type=EventType.WINDOW_ACTIVATE,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


# ──────────────────────────────────────────────────────────────────────
# raw_event_sanitizer
# ──────────────────────────────────────────────────────────────────────

class TestRawEventSanitizer:

    def test_drops_dup_clicks(self):
        from app.services.missions.raw_event_sanitizer import sanitize_raw_trace
        # Coords exactas + mismo UIA target + dentro de la ventana → dup.
        events = [_click(100, 100, 0), _click(100, 100, 50)]
        out, rep = sanitize_raw_trace(events)
        assert rep.dropped_dup_clicks == 1
        assert len(out) == 1

    def test_keeps_clicks_with_different_uia(self):
        """Mismas coords pero diferentes UIA targets → NO duplicados."""
        from app.services.missions.raw_event_sanitizer import sanitize_raw_trace
        a = _click(100, 100, 0, uia={"name": "Botón A", "automation_id": "btnA"})
        b = _click(100, 100, 50, uia={"name": "Botón B", "automation_id": "btnB"})
        out, rep = sanitize_raw_trace([a, b])
        assert rep.dropped_dup_clicks == 0
        assert len(out) == 2

    def test_keeps_distinct_clicks(self):
        from app.services.missions.raw_event_sanitizer import sanitize_raw_trace
        events = [_click(100, 100, 0), _click(500, 500, 50)]
        out, rep = sanitize_raw_trace(events)
        assert rep.dropped_dup_clicks == 0
        assert len(out) == 2

    def test_drops_invalid_target_click(self):
        from app.services.missions.raw_event_sanitizer import sanitize_raw_trace
        bad = _click(100, 100, 0, uia={"name": "guide-service", "automation_id": "main"})
        out, rep = sanitize_raw_trace([bad])
        assert rep.dropped_invalid_target == 1
        assert len(out) == 0

    def test_keeps_invalid_uia_when_web_locator_present(self):
        from app.services.missions.raw_event_sanitizer import sanitize_raw_trace
        ev = RawEvent(
            event_type=EventType.MOUSE_CLICK,
            timestamp=_BASE_TS,
            mouse_action=MouseAction(x=100, y=100),
            window_context=WindowContext(hwnd=1, title="YT", process_name="chrome.exe"),
            metadata={
                "uia_data": {"name": "guide-service"},
                "web_data": {"locators": [{"kind": "css", "value": "input"}]},
            },
        )
        out, rep = sanitize_raw_trace([ev])
        assert rep.dropped_invalid_target == 0
        assert len(out) == 1

    def test_coalesces_scrolls(self):
        from app.services.missions.raw_event_sanitizer import sanitize_raw_trace
        events = [_scroll(120, 0), _scroll(120, 100), _scroll(120, 200)]
        out, rep = sanitize_raw_trace(events)
        assert rep.coalesced_scrolls == 2
        assert len(out) == 1
        assert out[0].metadata.get("count") == 3

    def test_drops_dup_window_activate(self):
        from app.services.missions.raw_event_sanitizer import sanitize_raw_trace
        events = [_activate(0, hwnd=42), _activate(50, hwnd=42), _activate(80, hwnd=42)]
        out, rep = sanitize_raw_trace(events)
        assert rep.dropped_dup_window_activate == 2
        assert len(out) == 1


# ──────────────────────────────────────────────────────────────────────
# field_session_manager
# ──────────────────────────────────────────────────────────────────────

class TestFieldSessionManager:

    def test_anti_devuedevue(self):
        """Múltiples TYPE_TEXT en la misma sesión NO se concatenan."""
        from app.services.missions.field_session_manager import build_field_sessions
        events = [
            _type("Devue", 0),
            _type("Devuelveme", 50),
            _type("Devuelveme el amor", 100),
        ]
        sessions = build_field_sessions(events)
        assert len(sessions) == 1
        assert sessions[0].final_text == "Devuelveme el amor"
        assert sessions[0].submitted is False

    def test_enter_marks_submit(self):
        from app.services.missions.field_session_manager import build_field_sessions
        events = [
            _type("hola", 0),
            _enter(50),
        ]
        sessions = build_field_sessions(events)
        assert len(sessions) == 1
        assert sessions[0].final_text == "hola"
        assert sessions[0].submitted is True

    def test_separate_windows_separate_sessions(self):
        """Texto en Windows Search ≠ Texto en YouTube."""
        from app.services.missions.field_session_manager import build_field_sessions
        events = [
            _type("chrome", 0, hwnd=1, title="Search", proc="explorer.exe"),
            _enter(50, hwnd=1, title="Search", proc="explorer.exe"),
            _activate(100, hwnd=2, title="YouTube", proc="chrome.exe"),
            _type("Luis Miguel", 200, hwnd=2, title="YouTube", proc="chrome.exe"),
            _enter(300, hwnd=2, title="YouTube", proc="chrome.exe"),
        ]
        sessions = build_field_sessions(events)
        assert len(sessions) == 2
        assert sessions[0].final_text == "chrome"
        assert sessions[1].final_text == "Luis Miguel"
        # Sesiones aisladas
        assert sessions[0].window.hwnd == 1
        assert sessions[1].window.hwnd == 2


# ──────────────────────────────────────────────────────────────────────
# invalid_target_resolver
# ──────────────────────────────────────────────────────────────────────

class TestInvalidTargetResolver:

    def test_is_invalid_compiled_step_detects_guide_service(self):
        from app.services.missions.invalid_target_resolver import is_invalid_compiled_step
        cs = CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            target_context=TargetContext(
                uia_data={"name": "guide-service", "automation_id": "main"},
            ),
        )
        assert is_invalid_compiled_step(cs) is True

    def test_rescue_chrome_profile(self):
        from app.services.missions.invalid_target_resolver import rescue_compiled_step
        cs = CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            target_context=TargetContext(
                uia_data={"name": "main", "control_type": "PaneControl"},
                process_name="chrome.exe",
                window_title="¿Quién eres?",
                vision_data={"ocr_text_around": "Daniel Arturo Ramos"},
            ),
        )
        result = rescue_compiled_step(cs)
        assert result is not None
        assert result.new_step.action_strategy == ActionStrategy.SELECT_PROFILE
        assert "Daniel" in result.new_step.action_payload.get("profile", "")

    def test_rescue_steps_marks_unrescuable(self):
        from app.services.missions.invalid_target_resolver import rescue_steps
        cs = CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            target_context=TargetContext(
                uia_data={"name": "guide-service"},
            ),
        )
        out, rescued = rescue_steps([cs])
        assert rescued == 0  # sin contexto Chrome no se rescata
        assert out[0].action_payload.get("needs_user_label") is True
        assert out[0].action_payload.get("needs_reinforcement") is True


# ──────────────────────────────────────────────────────────────────────
# wait_manager
# ──────────────────────────────────────────────────────────────────────

class TestWaitManager:

    def test_chrome_open_predicate(self):
        from app.services.missions.wait_manager import chrome_open
        class FakeSnap:
            running_processes = ["explorer.exe", "chrome.exe"]
            active_app = "chrome.exe"
        assert chrome_open(FakeSnap) is True

    def test_url_contains(self):
        from app.services.missions.wait_manager import url_contains
        cond = url_contains("youtube.com")
        class FakeSnap:
            browser_url = "https://www.youtube.com/results?search_query=hi"
        assert cond(FakeSnap) is True

    def test_wait_until_with_factory(self):
        from app.services.missions.wait_manager import wait_until
        calls = []

        class _Snap:
            def __init__(self, ready: bool):
                self.ready = ready

        def fake_factory():
            calls.append(1)
            # Match al 3er poll.
            return _Snap(len(calls) >= 3)

        res = wait_until(
            condition=lambda s: getattr(s, "ready", False),
            timeout_ms=2000, poll_ms=10, label="ready",
            snapshot_factory=fake_factory,
        )
        assert res.matched is True
        assert res.polled >= 3

    def test_suggest_wait_for_open_app_chrome(self):
        from app.services.missions.wait_manager import suggest_wait_for
        suggestion = suggest_wait_for("open_app", {"name": "chrome"})
        assert suggestion is not None
        assert suggestion["kind"] == "wait_for_state"
        assert suggestion["params"]["condition"] == "chrome_open"


# ──────────────────────────────────────────────────────────────────────
# intent_compiler_v2 E2E
# ──────────────────────────────────────────────────────────────────────

class TestIntentCompilerV2:

    def _build_youtube_mission(self) -> Mission:
        """Trace sintético del caso del spec:
            * Activar Chrome
            * Click en perfil "Daniel Arturo Ramos"
            * Ctrl+T (nueva pestaña)
            * Type youtube.com + Enter (address bar)
            * Type "Devuélveme el amor de Luis Miguel" + Enter (search box)
        """
        events = [
            _activate(0, hwnd=1, title="¿Quién eres?", proc="chrome.exe"),
            _click(400, 400, 100, hwnd=1, title="¿Quién eres?", proc="chrome.exe",
                   uia={"name": "main", "control_type": "PaneControl"}),
            _activate(500, hwnd=2, title="Nueva pestaña - Chrome", proc="chrome.exe"),
            RawEvent(
                event_type=EventType.KEYBOARD_HOTKEY,
                timestamp=_BASE_TS + timedelta(milliseconds=600),
                keyboard_action=KeyboardAction(keys=["t"], modifiers=["ctrl"]),
                window_context=WindowContext(hwnd=2, title="Chrome", process_name="chrome.exe"),
            ),
            _type("youtube.com", 700, hwnd=2, title="Chrome", proc="chrome.exe"),
            _enter(800, hwnd=2, title="Chrome", proc="chrome.exe"),
            _activate(1500, hwnd=3, title="YouTube - Chrome", proc="chrome.exe"),
            _type("Devuélveme el amor de Luis Miguel", 1600, hwnd=3, title="YouTube", proc="chrome.exe"),
            _enter(1700, hwnd=3, title="YouTube", proc="chrome.exe"),
        ]
        return Mission(name="test", raw_trace=events)

    def test_compile_emits_blocks(self):
        from app.services.missions.intent_compiler_v2 import compile_intent
        m = self._build_youtube_mission()
        blocks, compiled, rep = compile_intent(m)
        # Esperamos: Abrir Chrome, Abrir YouTube (con new_tab), Buscar.
        assert rep.blocks_emitted >= 2
        assert rep.steps_emitted >= 3
        # No texto duplicado en ningún payload
        for cs in compiled:
            text = cs.action_payload.get("text", "")
            if text:
                assert text != text + text, f"texto duplicado: {text}"
                # anti-DevueDevue:
                assert "DevuélvemeDevuélveme" not in text

    def test_apply_intent_to_mission_sets_action_groups(self):
        from app.services.missions.intent_compiler_v2 import apply_intent_to_mission
        m = self._build_youtube_mission()
        rep = apply_intent_to_mission(m)
        assert len(m.action_groups) == rep.blocks_emitted
        assert len(m.compiled_execution_graph) == rep.steps_emitted
        # Cada bloque tiene step_ids no vacíos.
        for grp in m.action_groups:
            assert grp.step_ids


# ──────────────────────────────────────────────────────────────────────
# approval_gate V2
# ──────────────────────────────────────────────────────────────────────

class TestApprovalGateV2:

    def test_ghost_missing_blocks(self):
        from app.services.missions.approval_gate import (
            check_mission_approvable, classify_status,
        )
        from app.contracts.mission import MissionStatus

        cs = CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            target_context=TargetContext(
                uia_data={"name": "Botón", "automation_id": "btn1"},
            ),
            action_payload={"ghost_missing": True, "needs_user_label": True,
                            "label_prompt": "señala el elemento"},
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
        )
        m = Mission(name="t", compiled_execution_graph=[cs])
        rep = check_mission_approvable(m)
        codes = {v.code for v in rep.errors}
        # El paso lleva ghost_missing: debe aparecer.
        assert "ghost_missing" in codes
        st = classify_status(m)
        assert st == MissionStatus.NEEDS_REVIEW

    def test_empty_target_blocks_clicks(self):
        from app.services.missions.approval_gate import check_mission_approvable
        cs = CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            target_context=TargetContext(),  # sin nada
            action_payload={},
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
        )
        m = Mission(name="t", compiled_execution_graph=[cs])
        rep = check_mission_approvable(m)
        codes = {v.code for v in rep.errors}
        assert "empty_target" in codes


# ──────────────────────────────────────────────────────────────────────
# ghost_simulation (modo defensivo: state_detector no disponible → uncertain)
# ──────────────────────────────────────────────────────────────────────

class TestGhostSimulation:

    def test_skipped_for_non_visual_action(self):
        from app.services.missions.ghost_simulation import simulate_mission, GhostStatus
        cs = CompiledStep(
            action_strategy=ActionStrategy.LAUNCH_APP,
            action_payload={"app_name": "chrome"},
            target_context=TargetContext(
                semantic={"intent": "open_app", "human_label": "Abrir Chrome"},
            ),
        )
        m = Mission(name="t", compiled_execution_graph=[cs])
        rep = simulate_mission(m)
        assert len(rep.steps) == 1
        assert rep.steps[0].status == GhostStatus.OK

    def test_mark_missing_for_review(self):
        from app.services.missions.ghost_simulation import (
            GhostReport, GhostStepReport, GhostStatus, mark_missing_for_review,
        )
        cs = CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            target_context=TargetContext(
                uia_data={"name": "btn"},
            ),
        )
        m = Mission(name="t", compiled_execution_graph=[cs])
        report = GhostReport(mission_id=m.id)
        report.steps.append(GhostStepReport(
            step_id=cs.id, step_index=0,
            status=GhostStatus.MISSING,
            reason="not found",
            human_label="Botón",
        ))
        n = mark_missing_for_review(m, report)
        assert n == 1
        assert m.compiled_execution_graph[0].action_payload.get("ghost_missing") is True
        assert m.compiled_execution_graph[0].action_payload.get("needs_user_label") is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
