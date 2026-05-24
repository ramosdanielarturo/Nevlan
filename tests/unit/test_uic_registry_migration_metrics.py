"""UIC registry, migración SEP→search_content, métricas y ProfileResolver."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import apply_collapse_to_mission
from app.services.missions.profile_resolver import ProfileResolver
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    StrictValidationCode,
    attach_semantic_plan_to_mission,
    semantic_plan_to_mission_steps,
    validate_semantic_execution_plan_strict,
)
from app.services.missions.uic_capability_registry import (
    UIC_FAMILIES,
    get_capability,
)
from app.services.missions.uic_migration import migrate_semantic_execution_plan_dict
from app.services.telemetry.uic_metrics import UICMetrics


def test_uic_families_count():
    assert len(UIC_FAMILIES) == 16


def test_initial_capabilities_have_validators():
    for cid in (
        "open_app",
        "search_content",
        "search_site",
        "select_profile",
        "rerecord_step",
    ):
        cap = get_capability(cid)
        assert cap is not None
        assert callable(cap.outcome_validator)


def test_sep_rejects_unknown_capability():
    m = Mission(id="x", name="t")
    m.semantic_execution_plan = {
        "version": 1,
        "source": "intent_collapse_engine",
        "average_confidence": 0.9,
        "coords_used": False,
        "legacy_graph_ignored": True,
        "intent_status": "executable",
        "steps": [
            {
                "id": "s1",
                "intent": "marked_semantic_shape",
                "type": "unknown_capability_x",
                "params": {},
                "preferred_strategy": "x",
                "fallback_strategies": [],
                "confidence": 0.9,
                "human_label": "?",
            },
        ],
    }
    v = validate_semantic_execution_plan_strict(m)
    codes = [x.code for x in v.violations]
    assert StrictValidationCode.UNKNOWN_UIC_CAPABILITY in codes


def test_search_youtube_migrates_to_search_content_idempotent():
    blob = {
        "source": "intent_collapse_engine",
        "version": 1,
        "steps": [
            {
                "id": "mid:p04:search_youtube",
                "type": "search_youtube",
                "params": {"query": "q", "site": "youtube"},
                "preferred_strategy": "search_youtube:dom_input",
                "fallback_strategies": [],
                "confidence": 0.9,
                "human_label": "Buscar",
            },
        ],
    }
    b1 = copy.deepcopy(blob)
    assert migrate_semantic_execution_plan_dict(b1)
    assert b1["steps"][0]["type"] == "search_content"
    assert b1["steps"][0]["params"]["provider"] == "youtube"
    b2 = copy.deepcopy(b1)
    assert not migrate_semantic_execution_plan_dict(b2)


def test_attach_semantic_plan_migrates_search_real_fixture():
    raw_path = Path("tests/fixtures/real_missions/mission_1778244908_raw.json")
    if not raw_path.exists():
        pytest.skip("fixture ausente")
    data = json.loads(raw_path.read_text(encoding="utf-8"))
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    m = Mission(**data)
    apply_collapse_to_mission(m)
    attach_semantic_plan_to_mission(m, force=True)
    kinds = [s.get("type") for s in (m.semantic_execution_plan.get("steps") or [])]
    assert "search_youtube" not in kinds
    assert "search_content" in kinds


def test_semantic_plan_to_mission_maps_search_content_youtube():
    plan = SemanticExecutionPlan.from_dict({
        "source": "intent_collapse_engine",
        "version": 1,
        "coords_used": False,
        "legacy_graph_ignored": True,
        "intent_status": "READY",
        "steps": [
            {
                "id": "s1",
                "type": "search_content",
                "params": {"query": "x", "provider": "youtube"},
                "preferred_strategy": "search_content:smart_route",
                "fallback_strategies": [],
                "confidence": 1.0,
                "human_label": "Buscar",
            },
        ],
    })
    ms = semantic_plan_to_mission_steps(plan)
    assert ms[0].kind == "search_content"


def test_uic_metrics_capability_and_private_redaction():
    m = UICMetrics()
    m.record_capability_used("open_app", private_mode=True)
    assert m.private_mode_redactions >= 1
    m.record_event("x", {"screenshot": "no"}, private_mode=False)
    ev = m.events[-1]["payload"]
    assert "screenshot" not in ev


def test_profile_resolver_facade():
    r = ProfileResolver.skip_if_already_satisfied(profile_name="Daniel")
    assert r.profile_name == "Daniel"
    assert r.needs_user_confirmation is False
    a = ProfileResolver.needs_ambiguous_label()
    assert a.needs_user_confirmation is True


def test_profile_resolver_specific_confirmation_prompt():
    r = ProfileResolver.needs_specific_profile_confirmation(
        candidate_name="Daniel Arturo Ramos",
    )
    assert r.needs_user_confirmation is True
    assert "Daniel Arturo Ramos" in r.confirmation_prompt


def test_profile_resolver_select_visible_option():
    r = ProfileResolver.select_visible_option(
        option_type="profile",
        label="Daniel Arturo Ramos",
    )
    assert r.profile_name == "Daniel Arturo Ramos"
    assert r.needs_user_confirmation is False
