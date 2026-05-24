"""
Nevlan — Tests de integración del ruido en MissionReviewSummary (PRD §F)
==========================================================================

Verifican que:

  * ``MissionReviewSummary.ignored_noise`` se llena con la lista
    derivada de :func:`audit_ignored_noise`.
  * ``render_normal_view`` NO incluye los hallazgos de ruido.
  * ``render_expert_view`` SÍ incluye los hallazgos de ruido.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.services.missions import mission_review_summary as mrs
from app.services.missions.mission_review_summary import (
    MissionReviewSummary,
    render_expert_view,
    render_normal_view,
)


def _summary_with_noise(noise: List[Dict[str, Any]]) -> MissionReviewSummary:
    s = MissionReviewSummary(
        status_label="Lista para ejecutar",
        status_label_expert="Lista para ejecutar (con confirmación)",
        status_ready=True,
        steps=[],
        confirmations=[],
        blockers=[],
        visible_blockers=[],
        contamination_details=[],
        rerecord_history=[],
        ignored_noise=noise,
    )
    return s


def test_normal_review_hides_ignored_noise() -> None:
    summary = _summary_with_noise([{
        "kind": "address_bar_click_no_submit",
        "human_summary": "Click accidental en la barra de direcciones.",
        "evidence": {},
        "raw_event_ids": [],
    }])
    out = render_normal_view(summary)
    assert "Ruido descartado" not in out
    assert "address_bar_click_no_submit" not in out


def test_expert_review_shows_ignored_noise() -> None:
    summary = _summary_with_noise([{
        "kind": "address_bar_click_no_submit",
        "human_summary": "Click accidental en la barra.",
        "evidence": {},
        "raw_event_ids": [],
    }, {
        "kind": "typed_then_deleted",
        "human_summary": "Texto temporal escrito y borrado.",
        "evidence": {},
        "raw_event_ids": [],
    }])
    out = render_expert_view(summary)
    assert "Ruido descartado" in out
    assert "address_bar_click_no_submit" in out
    assert "typed_then_deleted" in out


def test_expert_view_omits_noise_section_when_empty() -> None:
    summary = _summary_with_noise([])
    out = render_expert_view(summary)
    assert "Ruido descartado" not in out


def test_build_summary_calls_audit_ignored_noise(monkeypatch) -> None:
    """Sanity: el builder integra el audit y no falla si devuelve []."""
    sentinel = [{
        "kind": "address_bar_click_no_submit",
        "human_summary": "demo",
        "evidence": {},
        "raw_event_ids": [],
    }]
    monkeypatch.setattr(
        "app.services.missions.noise_audit.audit_ignored_noise",
        lambda mission: sentinel,
    )
    from app.contracts.mission import Mission
    m = Mission(
        id="x", name="demo",
        raw_trace=[], compiled_execution_graph=[],
        semantic_execution_plan={
            "intent_status": "READY",
            "steps": [],
            "ready_provenance": "raw_evidence",
            "not_ready_reasons": [],
        },
    )
    summary = mrs.build_mission_review_summary(m)
    assert summary.ignored_noise == sentinel


def test_build_summary_swallows_audit_errors(monkeypatch) -> None:
    def _exploder(_):
        raise RuntimeError("boom")
    monkeypatch.setattr(
        "app.services.missions.noise_audit.audit_ignored_noise",
        _exploder,
    )
    from app.contracts.mission import Mission
    m = Mission(
        id="x", name="demo",
        raw_trace=[], compiled_execution_graph=[],
        semantic_execution_plan={
            "intent_status": "READY", "steps": [],
            "ready_provenance": "raw_evidence",
            "not_ready_reasons": [],
        },
    )
    summary = mrs.build_mission_review_summary(m)
    assert summary.ignored_noise == []
