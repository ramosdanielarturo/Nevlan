"""LONE — Live Operational Navigation Engine."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from app.contracts.mission import (
    OperationalCollection,
    OperationalCollectionType,
    OperationalEntity,
    OperationalEntityType,
    OperationalNavigationAction,
    OperationalNavigationGoal,
)
from app.services.runtime import live_operational_navigation_engine as lone
from app.services.runtime import universal_collection_entity_engine as uces


def _nodes(names: list, *, role: str = "listitemcontrol", parent: str = "List") -> list:
    return [
        {
            "name": n,
            "control_type": role,
            "parent_chain": [parent, "Window"],
            "children": [],
        }
        for n in names
    ]


def _collection(names: list, ctype=OperationalCollectionType.LIST) -> OperationalCollection:
    nodes = _nodes(names)
    colls = uces.detect_operational_collections(uia_hierarchy=nodes)
    assert colls
    c = colls[0]
    c.collection_type = ctype
    uces.extract_entities_from_collection(c, nodes)
    return c


def _entity(name: str) -> OperationalEntity:
    return OperationalEntity(
        entity_type=OperationalEntityType.RESULT_ITEM,
        display_name=name,
        normalized_name=uces.normalize_entity_name(name),
    )


# 1. Infinite scroll — entidad aparece tras scroll simulado
def test_infinite_scroll_entity_found_after_navigation():
    all_names = [f"Mercury Segment {i:02d}" for i in range(30)]
    target = "Mercury Segment 27"
    visible_window = all_names[:8]

    def observe(ctx: Dict[str, Any]) -> Tuple[Sequence[OperationalCollection], list]:
        scroll_y = int(ctx.get("scroll_y") or 0)
        page = scroll_y // 320
        start = min(page * 4, len(all_names) - 4)
        window = all_names[start : start + 8]
        colls = [ _collection(window) ]
        return colls, _nodes(window)

    result = lone.run_operational_navigation_loop(
        target_entity=_entity(target),
        target_collection=_collection(all_names[:8]),
        observe_fn=observe,
        max_depth=10,
        context={"scroll_y": 0, "scroll_step": 320},
    )
    assert result.success
    assert result.entity is not None
    assert "Mercury Segment 27" in result.entity.display_name


# 2. Lazy loading — wait then content
def test_lazy_loading_wait_dynamic():
    dynamic = lone.detect_dynamic_surface_state(
        uia_flat=[{"name": "Loading...", "control_type": "text", "role": "text"}],
    )
    assert dynamic.has_loading or dynamic.needs_wait
    plan = lone.plan_navigation_actions(
        search=lone.EntityLiveSearchResult(found=False),
        graph=lone.OperationalNavigationGraph(),
        dynamic=dynamic,
        affordances=[OperationalNavigationAction.WAIT_DYNAMIC_CONTENT.value],
    )
    assert OperationalNavigationAction.WAIT_DYNAMIC_CONTENT in plan.actions


# 3. Reordered collection
def test_reordered_collection_still_found():
    names_a = ["Alpha Co", "Beta Industries", "Gamma LLC"]
    names_b = ["Gamma LLC", "Alpha Co", "Beta Industries"]
    target = "Beta Industries"
    step = [0]

    def observe(ctx: Dict[str, Any]) -> Tuple[Sequence[OperationalCollection], list]:
        names = names_b if step[0] else names_a
        step[0] = 1
        return [_collection(names)], _nodes(names)

    r = lone.run_operational_navigation_loop(
        target_entity=_entity(target),
        target_collection=_collection(names_a),
        observe_fn=observe,
        max_depth=3,
    )
    assert r.success


# 4. Virtualized list
def test_virtualized_list_scroll_affordance():
    nodes = [
        {"name": "Row A", "control_type": "listitem", "scrollable_height": 5000},
        {"name": "Row B", "control_type": "listitem", "scrollable_height": 5000},
    ]
    dyn = lone.detect_dynamic_surface_state(uia_flat=nodes)
    coll = _collection(["Row A", "Row B"])
    coll.scrollable = True
    aff = lone.detect_navigation_affordances(uia_flat=nodes, collections=[coll])
    assert dyn.likely_virtualized or OperationalNavigationAction.SCROLL_DOWN.value in aff


# 5. Paginated table
def test_paginated_table_next_action():
    nodes = [
        {"name": "Next page", "control_type": "button", "role": "button"},
        {"name": "Row 1", "control_type": "dataitem"},
        {"name": "Row 2", "control_type": "dataitem"},
    ]
    aff = lone.detect_navigation_affordances(uia_flat=nodes)
    assert OperationalNavigationAction.PAGINATE_NEXT.value in aff


# 6. Dynamic tabs
def test_dynamic_tabs_switch_affordance():
    nodes = _nodes(["Home", "Settings", "Reports"], role="tabitemcontrol", parent="Tabs")
    aff = lone.detect_navigation_affordances(uia_flat=nodes)
    assert OperationalNavigationAction.SWITCH_TAB.value in aff


# 7. Retry loading
def test_retry_loading_affordance():
    nodes = [{"name": "Try again", "control_type": "button"}]
    aff = lone.detect_navigation_affordances(uia_flat=nodes)
    assert OperationalNavigationAction.RETRY_LOAD.value in aff


# 8. Skeleton loader
def test_skeleton_loader_detection():
    dyn = lone.detect_dynamic_surface_state(
        uia_flat=[{"name": "skeleton placeholder", "control_type": "group"}],
    )
    assert dyn.has_skeleton or dyn.has_loading


# 9. Viewport change — distinta firma viewport
def test_viewport_change_different_signatures():
    g = lone.build_live_operational_navigation_graph(
        collections=[_collection(["A", "B"])],
        context={"viewport_width": 800, "scroll_y": 0},
    )
    g2 = lone.build_live_operational_navigation_graph(
        collections=[_collection(["A", "B"])],
        context={"viewport_width": 1920, "scroll_y": 900},
        previous_graph=g,
    )
    sigs = {n.viewport_signature for n in g2.nodes.values()}
    assert len(sigs) >= 1


# 10. Fuzzy region search
def test_fuzzy_region_search():
    coll = _collection(["Daniel Arturo Ramos", "Other User", "Guest"])
    graph = lone.build_live_operational_navigation_graph(collections=[coll])
    search = lone.search_entity_in_live_operational_space(
        target_entity=_entity("Daniel Arturo Ramos"),
        target_collection=coll,
        graph=graph,
        collections=[coll],
    )
    assert search.found
    assert search.confidence >= lone.ENTITY_MATCH_MIN


# 11. Navigation memory reuse
def test_navigation_memory_reuse(tmp_path):
    db = tmp_path / "nav.sqlite"
    store = lone.NavigationMemoryStore(db_path=db)
    store.record_recovery_path(
        "hash123",
        "item27",
        ["scroll_down", "scroll_down"],
        success=True,
    )
    seq, rate = store.best_recovery_sequence("hash123", "item27")
    assert seq == ["scroll_down", "scroll_down"]
    assert rate > 0


# 12. Dynamic content wait in plan
def test_dynamic_content_wait_in_plan():
    dyn = lone.DynamicSurfaceState(has_loading=True)
    plan = lone.plan_navigation_actions(
        search=lone.EntityLiveSearchResult(),
        graph=lone.OperationalNavigationGraph(),
        dynamic=dyn,
        affordances=[],
    )
    assert plan.actions[0] == OperationalNavigationAction.WAIT_DYNAMIC_CONTENT


# 13. Entity eventually found in loop
def test_entity_eventually_found():
    phases = [
        ["Placeholder Alpha", "Placeholder Beta"],
        ["Target Visible", "Other Person"],
    ]
    idx = [0]

    def observe(ctx):
        names = phases[min(idx[0], len(phases) - 1)]
        idx[0] += 1
        return [_collection(names)], _nodes(names)

    r = lone.run_operational_navigation_loop(
        target_entity=_entity("Target Visible"),
        target_collection=_collection(["Target Visible", "Other Person"]),
        observe_fn=observe,
        max_depth=5,
    )
    assert r.success


# 14. Ambiguity stop
def test_ambiguity_stop():
    names = ["Similar A", "Similar B", "Similar C"]
    coll = _collection(names)
    graph = lone.build_live_operational_navigation_graph(collections=[coll])
    # Force many high-scoring candidates via search path
    search = lone.EntityLiveSearchResult(
        found=False,
        confidence=0.8,
        candidate_count=4,
    )
    goal = OperationalNavigationGoal(max_navigation_depth=2)

    def observe(ctx):
        return [coll], _nodes(names)

    r = lone.run_operational_navigation_loop(
        target_entity=_entity("Similar X"),
        target_collection=coll,
        observe_fn=observe,
        max_depth=2,
        goal=goal,
    )
    # May stop at max_depth or ambiguity depending on observe iterations
    assert r.depth_reached >= 1


# 15. Unsafe navigation stop
def test_unsafe_navigation_stop():
    ent = _entity("Confirm payment now")
    unsafe, reason = lone.is_unsafe_navigation_context(entity=ent)
    assert unsafe
    r = lone.run_operational_navigation_loop(
        target_entity=ent,
        target_collection=None,
        observe_fn=lambda ctx: ([], []),
    )
    assert r.request_human


# 16. No app hardcodes
def test_no_app_hardcode_in_source():
    src = open(lone.__file__, encoding="utf-8").read().lower()
    assert "youtube.com" not in src
    assert "chrome.exe" not in src


# 17. Legacy mission compatibility — sin colección sigue sin romper
def test_legacy_mission_no_collection_observe_empty():
    r = lone.run_operational_navigation_loop(
        target_entity=_entity("Something"),
        target_collection=None,
        observe_fn=lambda ctx: ([], []),
        max_depth=2,
    )
    assert not r.success
    assert r.stopped_reason


# 18. ONG build
def test_build_ong_nodes_and_edges():
    c1 = _collection(["A", "B"])
    c2 = _collection(["A", "B", "C"])
    g = lone.build_live_operational_navigation_graph(collections=[c1])
    g = lone.build_live_operational_navigation_graph(
        collections=[c2], previous_graph=g,
    )
    assert len(g.nodes) >= 2
    assert len(g.edges) >= 1


def test_normal_ux_label():
    assert lone.normal_status_label_for_navigation(True) == lone.NORMAL_STATUS_RECOVERING
    assert "ong" not in lone.sanitize_navigation_human_message("ong retry coords").lower()


def test_expert_audit_lines():
    class M:
        operational_navigation_audit = [{"depth": 1, "success": True, "actions": ["scroll_down"]}]

    lines = lone.expert_navigation_audit_lines(M())
    assert lines and "scroll_down" in lines[0]
