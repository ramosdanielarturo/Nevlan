"""FASE 1 — Pre-Click Capture: contrato de fases de TargetAnchor."""
from __future__ import annotations

from app.services.missions.recorder_capture_contract import (
    CAPTURE_PHASE_POST_ACTION,
    CAPTURE_PHASE_PRE_CLICK,
    ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
    TargetAnchor,
    aggregate_precapture_metrics,
    attach_precapture_diagnostics,
    capture_target_anchor,
    is_identity_anchor,
    resolve_identity_anchor,
)


def test_preclick_anchor_phase_is_set() -> None:
    anchor = capture_target_anchor(
        {"hwnd": 42, "title": "Bloc de notas", "process_name": "notepad.exe"},
        phase=CAPTURE_PHASE_PRE_CLICK,
        source=ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
        clock_ms=lambda: 1000,
        monotonic_ns=9_000_000,
    )
    assert anchor.phase == CAPTURE_PHASE_PRE_CLICK
    assert anchor.source == ANCHOR_SOURCE_PYNPUT_MOUSEDOWN
    assert anchor.captured_at_ms == 1000
    assert anchor.ts_monotonic_ns == 9_000_000
    blob = anchor.to_dict()
    assert blob["phase"] == CAPTURE_PHASE_PRE_CLICK
    restored = TargetAnchor.from_dict(blob)
    assert restored is not None
    assert restored.phase == CAPTURE_PHASE_PRE_CLICK


def test_preclick_anchor_used_for_identity_not_after() -> None:
    before = capture_target_anchor(
        {"hwnd": 1, "title": "Pre", "process_name": "a.exe"},
        phase=CAPTURE_PHASE_PRE_CLICK,
        source=ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
    )
    after = capture_target_anchor(
        {"hwnd": 2, "title": "Post", "process_name": "b.exe"},
        {"url": "https://example.com"},
        phase=CAPTURE_PHASE_POST_ACTION,
        source="pynput/post_capture",
    )
    identity = resolve_identity_anchor(before, after)
    assert identity is before
    assert identity is not None
    assert identity.title == "Pre"
    assert identity.hwnd == 1


def test_after_anchor_never_used_for_identity() -> None:
    after_only = capture_target_anchor(
        {"hwnd": 99, "title": "Rich UIA window", "process_name": "chrome.exe"},
        {"url": "https://youtube.com"},
        phase=CAPTURE_PHASE_POST_ACTION,
    )
    assert resolve_identity_anchor(None, after_only) is None
    assert not is_identity_anchor(after_only)

    legacy_before = TargetAnchor(
        hwnd=1, title="Legacy", process="app.exe", captured_at_ms=1,
    )
    assert resolve_identity_anchor(legacy_before, after_only) is None


def test_post_action_phase_default_on_capture() -> None:
    anchor = capture_target_anchor({"hwnd": 5, "title": "T", "process_name": "x.exe"})
    assert anchor.phase == CAPTURE_PHASE_POST_ACTION


def test_aggregate_precapture_metrics_serializes_report_fields() -> None:
    diags = [
        {"preclick_captured": True, "target_precapture_weak": False},
        {"preclick_captured": True, "target_precapture_weak": False},
        {"preclick_captured": False, "target_precapture_weak": True, "missing_reason": "TARGET_PRECAPTURE_MISSING"},
    ]
    report = aggregate_precapture_metrics(diags)
    assert report["click_count"] == 3
    assert report["preclick_missing_count"] == 1
    assert report["target_precapture_weak_count"] == 1
    assert abs(report["preclick_capture_rate"] - (2 / 3)) < 0.001


def test_attach_precapture_diagnostics_marks_weak_without_pending() -> None:
    meta: dict = {}
    anchor = capture_target_anchor(
        {"hwnd": 1, "title": "A", "process_name": "a.exe"},
        phase=CAPTURE_PHASE_PRE_CLICK,
        source=ANCHOR_SOURCE_PYNPUT_MOUSEDOWN,
    )
    attach_precapture_diagnostics(meta, before_anchor=anchor, had_pending=False)
    assert meta["precapture_diagnostics"]["target_precapture_weak"] is True
    attach_precapture_diagnostics(meta, before_anchor=anchor, had_pending=True)
    assert meta["precapture_diagnostics"]["preclick_captured"] is True
