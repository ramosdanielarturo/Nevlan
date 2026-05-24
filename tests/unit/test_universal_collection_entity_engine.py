"""UCES — Universal Collection & Entity Semantics Engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts.mission import (
    OperationalCollection,
    OperationalCollectionType,
    OperationalEntityType,
    RawEvent,
)
from app.services.runtime import universal_collection_entity_engine as uces


def _uia_list(names: list, *, role: str = "listitemcontrol", parent: str = "List") -> list:
    return [
        {
            "name": n,
            "control_type": role,
            "parent_chain": [parent, "Window"],
            "children": [],
        }
        for n in names
    ]


def _make_collection(names: list, ctype=OperationalCollectionType.LIST) -> OperationalCollection:
    nodes = _uia_list(names)
    colls = uces.detect_operational_collections(uia_hierarchy=nodes)
    assert colls
    c = colls[0]
    c.collection_type = ctype
    uces.extract_entities_from_collection(c, nodes)
    return c


# 1. Lista simple
def test_detect_simple_list():
    colls = uces.detect_operational_collections(
        uia_hierarchy=_uia_list(["Alice", "Bob", "Carol"]),
    )
    assert len(colls) >= 1
    assert colls[0].visible_entity_count >= 2
    assert colls[0].collection_type in (
        OperationalCollectionType.LIST,
        OperationalCollectionType.UNKNOWN,
    )


# 2. Tabla
def test_detect_table_collection():
    nodes = [
        {"name": "Row A", "control_type": "dataitem", "parent_chain": ["Table", "Grid"]},
        {"name": "Row B", "control_type": "dataitem", "parent_chain": ["Table", "Grid"]},
        {"name": "Row C", "control_type": "dataitem", "parent_chain": ["Table", "Grid"]},
    ]
    colls = uces.detect_operational_collections(uia_hierarchy=nodes)
    assert colls
    assert colls[0].collection_type == OperationalCollectionType.TABLE


# 3. Cards
def test_detect_cards_collection():
    nodes = [
        {"name": f"Card {i}", "control_type": "article", "parent_chain": ["Feed"]}
        for i in range(4)
    ]
    colls = uces.detect_operational_collections(uia_hierarchy=nodes)
    assert colls
    assert colls[0].collection_type == OperationalCollectionType.CARDS


# 4. Tabs
def test_detect_tabs_collection():
    nodes = _uia_list(["Home", "Settings", "Profile"], role="tabitemcontrol", parent="TabStrip")
    colls = uces.detect_operational_collections(uia_hierarchy=nodes)
    assert colls
    assert colls[0].collection_type == OperationalCollectionType.TABS


# 5. Search results
def test_detect_search_results():
    nodes = _uia_list(
        ["Result one", "Result two"],
        role="listitemcontrol",
        parent="SearchResults",
    )
    colls = uces.detect_operational_collections(uia_hierarchy=nodes)
    assert colls
    assert colls[0].collection_type in (
        OperationalCollectionType.SEARCH_RESULTS,
        OperationalCollectionType.LIST,
    )


# 6. Reordered collection
def test_match_entity_reordered():
    stored = _make_collection(["Daniel Arturo Ramos", "Other User"])
    stored_ent = stored.entities[0]
    runtime = _make_collection(["Other User", "Daniel Arturo Ramos"])
    match = uces.match_entity_inside_collection(
        stored_ent, runtime, collection_id=stored.collection_id, allow_reorder=True,
    )
    assert match.matched
    assert match.strategy in ("exact", "fuzzy", "fuzzy_reorder")


# 7. Scroll continuity
def test_preserve_collection_continuity_scroll():
    prev = _make_collection(["A", "B", "C", "D", "E"])
    cur = _make_collection(["C", "D", "E", "F"])  # partial overlap (scroll)
    matched, score = uces.preserve_collection_continuity(prev, [cur])
    assert matched is not None
    assert score >= uces.CONTINUITY_MATCH_MIN * 0.85


# 8. Partial visibility
def test_partial_visibility_still_matches():
    full = _make_collection(["Alpha", "Beta", "Gamma", "Delta"])
    partial = _make_collection(["Beta", "Gamma"])
    m = uces.match_collection_runtime(full, [partial])
    assert m.matched or m.confidence >= 0.45


# 9. Fuzzy entity recovery
def test_fuzzy_entity_recovery_typo():
    stored = _make_collection([
        "Daniel Chrome D Daniel Arturo Ramos",
        "Other Profile",
    ])
    ent = stored.entities[0]
    runtime = _make_collection([
        "Daniel Chrome D Daniel Arturo Ramoz",
        "Other Profile",
    ])
    match = uces.match_entity_inside_collection(ent, runtime)
    assert match.confidence >= 0.75


# 10. Unique entity — no clarification
def test_unique_entity_no_clarification():
    colls = uces.detect_operational_collections(
        uia_hierarchy=_uia_list(["Daniel Chrome D Daniel Arturo Ramos", "Guest"]),
    )
    intent = uces.infer_entity_selection_intent(
        click_label="Daniel Chrome D Daniel Arturo Ramos",
        collections=colls,
        execution_truth_confirmed=True,
    )
    assert intent.selected
    assert not intent.needs_clarification
    assert intent.candidate_count >= 1


# 11. Multiple candidates — clarification
def test_multiple_candidates_need_clarification():
    colls = uces.detect_operational_collections(
        uia_hierarchy=_uia_list(["Daniel A", "Daniel B"]),
    )
    intent = uces.infer_entity_selection_intent(
        click_label="Daniel",
        collections=colls,
        execution_truth_confirmed=False,
    )
    assert intent.candidate_count >= 2 or intent.needs_clarification or not intent.selected


# 12. Collection memory persistence
def test_collection_memory_persistence(tmp_path: Path):
  db = tmp_path / "coll_mem.sqlite"
  store = uces.CollectionMemoryStore(db_path=db)
  coll = _make_collection(["X", "Y"])
  store.upsert_collection(coll)
  store.record_selection_success(coll.collection_id, "x", "test")
  assert store.historical_selection_boost(coll.collection_id, "x") > 0


# 13. Structural continuity
def test_structural_signature_stable():
    c1 = _make_collection(["One", "Two", "Three"])
    c2 = _make_collection(["One", "Two", "Three"])
    assert (
        c1.structural_signals.get("structural_hash")
        == c2.structural_signals.get("structural_hash")
    )


# 14. No app hardcodes
def test_no_chrome_youtube_hardcode_in_module():
    src = Path(uces.__file__).read_text(encoding="utf-8").lower()
    assert "youtube.com" not in src
    assert "chrome.exe" not in src
    assert "select_profile" not in src or "legacy" in src or "_LEGACY" in Path(uces.__file__).read_text()


# 15. Entity-first runtime replay hook
def test_try_collection_entity_first_with_stored_context():
    coll = _make_collection(["Target Entity", "Other"])
    ent = coll.entities[0]
    class _Step:
        id = "s1"
        params = {
            "operational_collection": coll.model_dump(mode="json"),
            "collection_entity": ent.model_dump(mode="json"),
            "operational_entity": {
                "display_name": ent.display_name,
                "normalized_name": ent.normalized_name,
                "entity_type": ent.entity_type.value,
                "confidence": 0.9,
                "evidence": {},
            },
        }

    runtime_cands = [
        {"name": "Target Entity", "role": "listitem"},
        {"name": "Other", "role": "listitem"},
    ]
    res = uces.try_collection_entity_first_runtime(_Step(), runtime_cands)
    assert res.candidate_count >= 1


# 16. Legacy mission compatibility
def test_universalize_legacy_kinds():
    assert uces.universalize_step_kind("select_profile") == uces.UNIVERSAL_SELECT_KIND
    assert uces.universalize_step_kind("click") == "click"
    assert not uces.is_universal_collection_step("navigate")


def test_build_selection_from_capture_profile_like():
    ev = RawEvent(
        event_type="mouse_click",
        metadata={
            "uia": {
                "name": "Daniel Chrome D Daniel Arturo Ramos",
                "control_type": "ListItemControl",
                "parent_chain": ["ProfileList"],
                "children": [],
            },
            "target_identity_isolation": {
                "trusted_pre_action_identity": True,
                "target_label": "Daniel Chrome D Daniel Arturo Ramos",
                "target_identity_confidence": 0.94,
            },
        },
    )
    siblings = _uia_list(
        ["Daniel Chrome D Daniel Arturo Ramos", "Person 2"],
        parent="ProfileList",
    )
    intent = uces.build_selection_from_capture(
        raw_event=ev,
        capture_bundle={"uia_flat": siblings},
    )
    assert intent.confidence > 0 or intent.collection is not None


def test_apply_ready_relief():
    class _Row:
        def __init__(self):
            self.needs_user_label = True
            self.params = {
                "operational_collection": {"collection_type": "list"},
                "collection_entity": {"display_name": "X"},
                "collection_selection_confidence": 0.92,
                "collection_candidate_count": 1,
                "execution_truth_confirmed": True,
                "collection_continuity_score": 0.8,
            }

    row = _Row()

    class _Plan:
        steps = [row]

    uces.apply_collection_entity_ready_relief(_Plan(), None)
    assert row.needs_user_label is False
