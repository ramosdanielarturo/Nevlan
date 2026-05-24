"""Tests universales para :mod:`semantic_ambiguity_engine` (sin mocks por tipo de app)."""
from __future__ import annotations

from app.contracts.mission import EventType, Mission, RawEvent
from app.services.missions.semantic_ambiguity_engine import (
    RB_MULTI_TARGET,
    analyze_semantic_step_dict,
)


def _truth_event() -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        metadata={
            "capture_contract": {
                "sufficient_for_execution": True,
                "trusted_pre_action_identity": True,
                "capture_complete": True,
                "contamination": False,
            },
        },
    )


def mission_with_truth() -> Mission:
    m = Mission(name="ambiguity-test")
    m.raw_trace = [_truth_event()]
    return m


def test_profile_specific_promotes_without_review():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {
            "type": "x",
            "params": {
                "profile_name": "Daniel Chrome D Daniel Arturo Ramos",
            },
            "human_label": "Elegir perfil Daniel",
            "needs_user_label": False,
        },
        m,
    )
    assert not sar.requires_human_confirmation


def test_profile_meta_placeholder_blocked():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {"type": "x", "params": {"profile_name": "perfil"}},
        m,
    )
    assert sar.requires_human_confirmation


def test_search_specific_query_promotes():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {
            "type": "x",
            "params": {"query": "Devuélveme el amor Luis Miguel"},
        },
        m,
    )
    assert not sar.requires_human_confirmation


def test_search_generic_blocked():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {"type": "x", "params": {"query": "buscar algo"}},
        m,
    )
    assert sar.requires_human_confirmation


def test_app_name_single_token_not_placeholder():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {"type": "x", "params": {"name": "Chrome"}},
        m,
    )
    assert not sar.requires_human_confirmation


def test_site_target_minimal_but_not_placeholder():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {"type": "x", "params": {"site": "youtube.com"}},
        m,
    )
    assert not sar.requires_human_confirmation


def test_sap_style_transaction_carrier():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {
            "type": "x",
            "params": {"transaction": "VA01", "description": ""},
        },
        m,
    )
    assert not sar.requires_human_confirmation


def test_outlook_recipient_carrier():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {
            "type": "x",
            "params": {
                "recipient": "cliente.uno+demo@fabrica.invalid",
                "subject": "Entrega cotización Q4",
            },
        },
        m,
    )
    assert not sar.requires_human_confirmation


def test_explorer_path_carrier():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {
            "type": "x",
            "params": {
                "path": (
                    r"C:\Users\Dani\Downloads\Contrato_FINAL_v3 firmado.docx"
                ),
            },
        },
        m,
    )
    assert not sar.requires_human_confirmation


def test_empty_query_always_blocked():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {"type": "x", "params": {"query": ""}},
        m,
    )
    assert sar.requires_human_confirmation


def test_execution_truth_promotes_ambiguous_human_label_conflict():
    m = mission_with_truth()
    sar = analyze_semantic_step_dict(
        {
            "type": "scroll_results",
            "params": {"direction": "down"},
            "needs_user_label": True,
            "human_label": "Scrolling",
        },
        m,
    )
    assert not sar.requires_human_confirmation


def test_multiplicity_emits_specific_blocker_literal():
    m = Mission(name="plain")
    sar = analyze_semantic_step_dict(
        {
            "type": "click_x",
            "params": {"candidate_count": 3},
        },
        m,
    )
    assert sar.multiple_possible_targets
    assert RB_MULTI_TARGET in sar.readiness_blockers
