"""Entity Resolution Layer (ERL) — unit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contracts.mission import (
    Mission,
    MissionStatus,
    OperationalEntity,
    OperationalEntityEvidence,
    OperationalEntityType,
    RawEvent,
)
from app.services.runtime import entity_resolution_layer as erl


@pytest.fixture
def memory_db(tmp_path: Path) -> Path:
    db = tmp_path / "entity_memory_test.sqlite"
    return db


@pytest.fixture
def store(memory_db: Path) -> erl.EntityMemoryStore:
    return erl.EntityMemoryStore(db_path=memory_db)


# ── Normalización ───────────────────────────────────────────────────────


def test_normalize_entity_name_profile():
    raw = "Daniel Chrome D Daniel Arturo Ramos"
    assert erl.normalize_entity_name(raw) == "daniel_chrome_d_daniel_arturo_ramos"


def test_normalize_entity_name_strips_punctuation_and_spaces():
    assert erl.normalize_entity_name("  Foo!!   Bar  ") == "foo_bar"


# ── Generic label rejection ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "label",
    ["ok", "button", "item", "select", "profile", "result", ""],
)
def test_generic_labels_rejected(label: str):
    assert erl.is_generic_entity_label(label)


def test_specific_name_not_generic():
    assert not erl.is_generic_entity_label("Daniel Arturo Ramos")


# ── Profile / search / folder entities ──────────────────────────────────


def test_extract_profile_entity_from_capture():
    ev = RawEvent(
        event_type="mouse_click",
        metadata={
            "target_identity_isolation": {
                "trusted_pre_action_identity": True,
                "target_label": "Daniel Chrome D Daniel Arturo Ramos",
                "target_identity_confidence": 0.94,
                "parent_chain": ["ProfilePicker", "List"],
            },
            "uia": {"name": "Daniel Chrome D Daniel Arturo Ramos", "automation_id": "profile_3"},
        },
    )
    entity = erl.extract_operational_entity(
        raw_event=ev,
        capture_bundle={
            "selection_intent": "primary_user_profile",
            "execution_truth_confirmed": True,
            "sufficient_for_execution": True,
        },
        selection_intent="primary_user_profile",
        store_on_success=False,
    )
    assert entity is not None
    assert entity.entity_type == OperationalEntityType.USER_PROFILE
    assert "daniel" in entity.normalized_name
    assert entity.confidence >= erl.PROMOTION_ENTITY_CONFIDENCE_MIN


def test_extract_search_entity():
    entity = erl.extract_operational_entity(
        capture_bundle={
            "display_name": "automation testing best practices",
            "uia_name": "automation testing best practices",
            "trusted_pre_action_identity": True,
            "sufficient_for_execution": True,
            "step_type": "search_content",
            "nearby_text": ["Search", "automation testing best practices"],
            "target_identity_isolation": {
                "trusted_pre_action_identity": True,
                "target_identity_confidence": 0.91,
            },
        },
        step_type_hint="search_content",
        store_on_success=False,
    )
    assert entity is not None
    assert entity.entity_type == OperationalEntityType.SEARCH_QUERY


def test_extract_folder_entity():
    entity = erl.extract_operational_entity(
        capture_bundle={
            "display_name": "Q4 Reports",
            "uia_name": "Q4 Reports",
            "parent_chain": ["TreeView", "folder", "Documents"],
            "nearby_text": ["Q4 Reports", "Folders"],
            "trusted_pre_action_identity": True,
            "sufficient_for_execution": True,
            "execution_truth_confirmed": True,
            "target_identity_isolation": {
                "trusted_pre_action_identity": True,
                "target_identity_confidence": 0.88,
            },
        },
        store_on_success=False,
    )
    assert entity is not None
    assert entity.entity_type in (
        OperationalEntityType.FOLDER,
        OperationalEntityType.RESULT_ITEM,
        OperationalEntityType.UNKNOWN,
    )


# ── Promotion rules ─────────────────────────────────────────────────────


def test_ambiguity_rejection_multi_target():
    ok, reasons = erl.should_promote_to_operational_entity(
        display_name="Daniel Arturo Ramos",
        evidence=OperationalEntityEvidence(
            uia_name="Daniel Arturo Ramos",
            target_identity_confidence=0.9,
        ),
        trusted_pre_action_identity=True,
        sufficient_for_execution=True,
        target_identity_confidence=0.9,
        multi_target_ambiguity=True,
        unique_candidate_count=3,
    )
    assert not ok
    assert "multi_target_ambiguity" in reasons


def test_promotion_rejects_empty_target():
    entity = erl.extract_operational_entity(
        capture_bundle={
            "display_name": "profile",
            "trusted_pre_action_identity": True,
            "target_identity_isolation": {
                "trusted_pre_action_identity": True,
                "target_identity_confidence": 0.95,
            },
        },
        store_on_success=False,
    )
    assert entity is None


# ── Matching ────────────────────────────────────────────────────────────


def test_runtime_exact_match():
    entity = OperationalEntity(
        display_name="Daniel Arturo Ramos",
        normalized_name="daniel_arturo_ramos",
        entity_type=OperationalEntityType.USER_PROFILE,
        confidence=0.95,
    )
    candidates = [
        {"label": "Daniel Arturo Ramos", "uia_name": "Daniel Arturo Ramos"},
        {"label": "Other User"},
    ]
    match = erl.match_entity_against_runtime(entity, candidates)
    assert match.matched
    assert match.strategy == "exact"


def test_runtime_fuzzy_recovery():
    entity = OperationalEntity(
        display_name="Daniel Chrome D Daniel Arturo Ramos",
        normalized_name="daniel_chrome_d_daniel_arturo_ramos",
        confidence=0.9,
    )
    candidates = [{"label": "Daniel Chrome D Daniel Arturo Ramos"}]
    match = erl.match_entity_against_runtime(entity, candidates)
    assert match.matched
    assert match.strategy in ("exact", "fuzzy")


def test_ocr_fuzzy_match():
    entity = OperationalEntity(
        display_name="Invoices 2026",
        normalized_name="invoices_2026",
        confidence=0.88,
    )
    candidates = [{"label": "Row", "ocr_text": "Invoices 2026"}]
    match = erl.match_entity_against_runtime(entity, candidates)
    assert match.matched
    assert match.strategy == "ocr_fuzzy"


def test_alias_match():
    entity = OperationalEntity(
        display_name="Daniel Arturo Ramos",
        normalized_name="daniel_arturo_ramos",
        aliases=["D. Ramos", "daniel_arturo_ramos"],
        confidence=0.9,
    )
    candidates = [{"label": "D. Ramos"}]
    match = erl.match_entity_against_runtime(entity, candidates)
    assert match.matched
    assert match.strategy == "alias"


def test_visual_continuity_match():
    entity = OperationalEntity(
        display_name="Project Alpha",
        normalized_name="project_alpha",
        confidence=0.9,
        evidence=OperationalEntityEvidence(uia_name="Project Alpha"),
    )
    candidates = [{
        "label": "Project Alpha",
        "visual_fingerprint_available": True,
        "visual_fingerprint_score": 0.88,
    }]
    match = erl.match_entity_against_runtime(entity, candidates)
    assert match.matched
    assert match.strategy in ("visual_continuity", "exact", "fuzzy")


# ── Memory persistence ──────────────────────────────────────────────────


def test_entity_memory_persistence(store: erl.EntityMemoryStore):
    entity = OperationalEntity(
        display_name="Ticket #8842",
        normalized_name="ticket_8842",
        entity_type=OperationalEntityType.TICKET,
        aliases=["Ticket #8842", "8842"],
        confidence=0.92,
    )
    store.upsert_entity(entity)
    loaded = store.find_by_normalized_name("ticket_8842", OperationalEntityType.TICKET)
    assert len(loaded) == 1
    assert loaded[0].display_name == "Ticket #8842"
    by_alias = store.find_by_alias("8842")
    assert any(e.entity_id == entity.entity_id for e in by_alias)


def test_historical_success_boost(store: erl.EntityMemoryStore):
    eid = "ent-hist-1"
    store.record_resolution_success(eid, "exact", {"app": "host"})
    score = store.historical_success_score(eid, erl._context_hash({"app": "host"}))
    assert score > 0.0


# ── Runtime resolution pipeline ─────────────────────────────────────────


def test_resolve_entity_for_runtime_order(store: erl.EntityMemoryStore):
    entity = OperationalEntity(
        entity_id="e1",
        display_name="Shared Drive",
        normalized_name="shared_drive",
        confidence=0.93,
    )
    store.upsert_entity(entity)
    candidates = [{"label": "Shared Drive"}]
    result = erl.resolve_entity_for_runtime(entity, candidates)
    assert result.resolved
    assert result.strategy == "exact"
    assert not result.used_legacy_fallback


def test_resolve_entity_legacy_fallback():
    entity = OperationalEntity(
        display_name="Legacy Target",
        normalized_name="legacy_target",
        confidence=0.9,
    )

    def _legacy(ent, cands, context=None):
        return type("R", (), {"confidence": 0.7})()

    result = erl.resolve_entity_for_runtime(
        entity,
        [{"label": "Completely Different"}],
        legacy_resolver=_legacy,
    )
    assert result.resolved
    assert result.used_legacy_fallback


def test_historical_runtime_recovery(store: erl.EntityMemoryStore):
    entity = OperationalEntity(
        entity_id="hist-e",
        display_name="Daniel Arturo Ramos",
        normalized_name="daniel_arturo_ramos",
        entity_type=OperationalEntityType.USER_PROFILE,
        confidence=0.94,
    )
    store.upsert_entity(entity)
    runtime_entity = OperationalEntity(
        display_name="Daniel Arturo Ramos",
        normalized_name="daniel_arturo_ramos",
        confidence=0.9,
    )
    result = erl.resolve_entity_for_runtime(
        runtime_entity,
        [{"label": "Daniel Arturo Ramos"}],
    )
    assert result.resolved


# ── Confidence ──────────────────────────────────────────────────────────


def test_execution_truth_integration_confidence():
    entity = OperationalEntity(
        display_name="Acme Corp",
        normalized_name="acme_corp",
        evidence=OperationalEntityEvidence(
            uia_name="Acme Corp",
            execution_truth_confirmed=True,
            target_identity_confidence=0.92,
        ),
    )
    conf = erl.compute_entity_resolution_confidence(
        entity,
        unique_candidate_count=1,
        replay_validated=True,
    )
    assert conf >= 0.72


def test_ambiguity_penalty_lowers_confidence():
    entity = OperationalEntity(
        display_name="Acme Corp",
        normalized_name="acme_corp",
        evidence=OperationalEntityEvidence(uia_name="Acme Corp"),
    )
    high = erl.compute_entity_resolution_confidence(entity, unique_candidate_count=1)
    low = erl.compute_entity_resolution_confidence(
        entity, unique_candidate_count=5, ambiguity_penalty=0.25,
    )
    assert low < high


# ── Mission review / no app hardcode ────────────────────────────────────


def test_mission_review_entity_from_semantic_step():
    mission = Mission(name="t", status=MissionStatus.EXECUTABLE)
    mission.semantic_execution_plan = {
        "intent_status": "READY",
        "steps": [{
            "id": "s1",
            "type": "select_profile",
            "human_label": "Usar perfil Daniel Arturo Ramos",
            "params": {
                "profile": "Daniel Chrome D Daniel Arturo Ramos",
                "trusted_pre_action_identity": True,
                "target_identity_confidence": 0.94,
            },
            "needs_user_label": True,
        }],
    }
    ent = erl.extract_entity_from_semantic_step(mission.semantic_execution_plan["steps"][0], mission)
    assert ent is not None
    assert ent.confidence >= erl.PROMOTION_ENTITY_CONFIDENCE_MIN


def test_apply_entity_resolution_relief_clears_needs_label():
    from app.services.missions.semantic_execution_plan import SemanticPlanStep

    mission = Mission(name="relief", status=MissionStatus.EXECUTABLE)
    step = SemanticPlanStep(
        id="s1",
        type="select_profile",
        params={
            "profile": "Daniel Arturo Ramos",
            "trusted_pre_action_identity": True,
            "target_identity_confidence": 0.93,
        },
        needs_user_label=True,
        human_label="Perfil Daniel Arturo Ramos",
    )
    plan = type("P", (), {"steps": [step]})()
    erl.apply_entity_resolution_relief(plan, mission)
    assert step.needs_user_label is False


def test_no_app_hardcode_in_source():
    """Validación estática: ERL no debe contener if chrome/youtube."""
    src = Path(erl.__file__).read_text(encoding="utf-8").lower()
    forbidden = [
        'if app == "chrome"',
        "if browser == 'chrome'",
        "youtube",
        "chrome.exe",
    ]
    for token in forbidden:
        assert token not in src, f"hardcode detectado: {token}"


def test_sanitize_human_label_strips_expert_markers():
    raw = "Click perfil (uia_text_match) via smart_route"
    clean = erl.sanitize_human_label_for_normal_view(raw)
    assert "uia_text_match" not in clean.lower()
    assert "smart_route" not in clean.lower()


def test_entity_status_label_profile():
    mission = Mission(name="m", status=MissionStatus.EXECUTABLE)
    mission.semantic_execution_plan = {
        "steps": [{
            "type": "select_profile",
            "params": {
                "profile": "Daniel Chrome D Daniel Arturo Ramos",
                "trusted_pre_action_identity": True,
                "target_identity_confidence": 0.95,
                "selection_intent": "primary_user_profile",
            },
        }],
    }
    label = erl.entity_status_label_for_mission(mission)
    assert label == "Perfil principal identificado"
