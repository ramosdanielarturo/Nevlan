"""PCOF — Pre-Click Operational Freeze (20 escenarios obligatorios)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.contracts.mission import (
    OperationalFreezeSnapshot,
    TransitionRiskLevel,
)
from app.services.runtime import pre_click_operational_freeze as pcof
from app.services.runtime import entity_resolution_layer as erl
from app.services.runtime import universal_collection_entity_engine as uces
from app.services.runtime.pre_click_operational_freeze import (
    _PCOFPending,
    apply_freeze_to_metadata,
    begin_pre_click_freeze,
    capture_operational_freeze,
    detect_dynamic_surface,
    finalize_pre_click_freeze,
    freeze_to_capture_bundle,
    score_transition_risk,
)


@pytest.fixture(autouse=True)
def _enable_pcof(monkeypatch):
    monkeypatch.setattr(pcof, "pcof_enabled", lambda *_a, **_k: True)
    # Evita UIA real del escritorio en tests que llaman begin_pre_click_freeze.
    monkeypatch.setattr(pcof, "_eager_uia_at_point", lambda _x, _y: ({}, []))


def _profile_picker_uia(names: list[str]) -> list[dict]:
    return [
        {
            "name": n,
            "control_type": "listitemcontrol",
            "role": "listitem",
            "parent_chain": ["ProfileList", "Picker", "Window"],
            "bbox": {"left": 100, "top": 80 + i * 40, "width": 320, "height": 36},
            "children": [],
        }
        for i, n in enumerate(names)
    ]


def _click_meta(uia_flat: list, click_xy: tuple, name: str) -> dict:
    hit = next((n for n in uia_flat if n["name"] == name), uia_flat[0])
    return {
        "uia": dict(hit),
        "uia_flat": uia_flat,
        "web": {},
        "ocr_fallback": {"text": "\n".join(n["name"] for n in uia_flat)},
    }


# 1. profile picker dinámico
def test_profile_picker_dynamic_collection_freeze():
    names = [
        "Daniel Chrome D Daniel Arturo Ramos",
        "Invitado",
        "Trabajo",
    ]
    flat = _profile_picker_uia(names)
    freeze = capture_operational_freeze(
        cursor_xy=(120, 95),
        uia=flat[0],
        uia_flat=flat,
        window_context={"title": "Profile Picker", "process_name": "browser.exe"},
        trigger="mouse_down",
    )
    assert freeze.best_collection_candidate is not None
    assert freeze.best_collection_candidate.entity_count >= 2
    assert "Daniel" in " ".join(freeze.best_collection_candidate.entities)
    assert freeze.pre_transition_confirmed


# 2. launcher rápido
def test_fast_launcher_high_transition_risk():
    flat = [
        {"name": "App One", "control_type": "listitemcontrol", "parent_chain": ["Launcher"]},
        {"name": "App Two", "control_type": "listitemcontrol", "parent_chain": ["Launcher"]},
        {"name": "App Three", "control_type": "listitemcontrol", "parent_chain": ["Launcher"]},
    ]
    risk = score_transition_risk(
        uia={"control_type": "listitemcontrol", "name": "App One"},
        flat_nodes=flat,
        collection_count=1,
    )
    assert risk in (TransitionRiskLevel.MEDIUM, TransitionRiskLevel.HIGH)
    freeze = capture_operational_freeze(cursor_xy=(50, 50), uia_flat=flat, trigger="mouse_down")
    assert freeze.freeze_depth in ("deep", "standard")


# 3. lista virtualizada
def test_virtualized_list_dynamic_surface():
    flat = [
        {"name": f"Row {i}", "control_type": "dataitem", "states": ["virtualized"]}
        for i in range(8)
    ]
    assert detect_dynamic_surface(flat_nodes=flat)
    freeze = capture_operational_freeze(cursor_xy=(10, 10), uia_flat=flat)
    assert freeze.dynamic_surface_detected


# 4. async search results
def test_async_search_results_collection():
    flat = [
        {"name": "Result A", "control_type": "listitemcontrol", "parent_chain": ["SearchResults"]},
        {"name": "Result B", "control_type": "listitemcontrol", "parent_chain": ["SearchResults"]},
        {"name": "Result C", "control_type": "listitemcontrol", "parent_chain": ["SearchResults"]},
    ]
    freeze = capture_operational_freeze(
        cursor_xy=(200, 120),
        uia_flat=flat,
        dom={"url": "https://example.test/search?q=demo", "nodes": []},
    )
    assert freeze.best_collection_candidate is not None
    assert freeze.transition_risk != TransitionRiskLevel.LOW or freeze.freeze_confidence >= 0.5


# 5. tab switching
def test_tab_switching_collection():
    flat = [
        {"name": "Home", "control_type": "tabitemcontrol", "parent_chain": ["TabStrip"]},
        {"name": "Settings", "control_type": "tabitemcontrol", "parent_chain": ["TabStrip"]},
        {"name": "Profile", "control_type": "tabitemcontrol", "parent_chain": ["TabStrip"]},
    ]
    freeze = capture_operational_freeze(cursor_xy=(80, 20), uia_flat=flat)
    assert freeze.best_collection_candidate is not None
    assert freeze.best_collection_candidate.collection_type == "tabs"


# 6. menu popup
def test_menu_popup_freeze():
    flat = [
        {"name": "Copy", "control_type": "menuitemcontrol", "parent_chain": ["ContextMenu"]},
        {"name": "Paste", "control_type": "menuitemcontrol", "parent_chain": ["ContextMenu"]},
    ]
    freeze = capture_operational_freeze(cursor_xy=(30, 30), uia_flat=flat)
    assert freeze.best_entity_candidate is not None


# 7. hover-before-click
def test_hover_before_click_uses_hover_xy():
    begin_pre_click_freeze(10, 10, window_context={"title": "W"})
    pcof.update_pre_click_hover(200, 180)
    meta = _click_meta(_profile_picker_uia(["A", "B"]), (10, 10), "A")
    freeze = finalize_pre_click_freeze(
        cursor_xy=(10, 10),
        metadata=meta,
        window_context={"title": "W"},
    )
    assert freeze.cursor_xy == (200, 180)


# 8. OCR-only entity
def test_ocr_only_entity_candidate():
    freeze = capture_operational_freeze(
        cursor_xy=(100, 100),
        ocr={"text": "Unique OCR Label\nOther line"},
        uia={},
        uia_flat=[],
    )
    assert freeze.best_entity_candidate is not None
    assert "ocr" in (freeze.best_entity_candidate.evidence_sources or [])


# 9. DOM-only entity
def test_dom_only_entity_candidate():
    dom = {
        "nodes": [
            {
                "role": "button",
                "visible_text": "Submit Form",
                "bbox": {"left": 90, "top": 90, "width": 80, "height": 24},
            },
        ],
    }
    freeze = capture_operational_freeze(cursor_xy=(100, 100), dom=dom, uia_flat=[])
    assert any(t.source == "dom" for t in freeze.dom_targets)


# 10. UIA-only entity
def test_uia_only_entity_candidate():
    uia = {
        "name": "Primary Action",
        "control_type": "buttoncontrol",
        "automation_id": "btn_primary",
        "bbox": {"left": 50, "top": 50, "width": 100, "height": 30},
        "parent_chain": ["Toolbar"],
    }
    freeze = capture_operational_freeze(cursor_xy=(80, 65), uia=uia, uia_flat=[uia])
    assert freeze.best_entity_candidate is not None
    assert "uia" in freeze.best_entity_candidate.evidence_sources


# 11. multi-signal fusion
def test_multi_signal_fusion():
    flat = _profile_picker_uia(["Daniel Chrome D Daniel Arturo Ramos", "Guest"])
    freeze = capture_operational_freeze(
        cursor_xy=(120, 95),
        uia=flat[0],
        uia_flat=flat,
        ocr={"text": "Daniel Chrome D Daniel Arturo Ramos\nGuest"},
        dom={
            "nodes": [
                {
                    "visible_text": "Daniel Chrome D Daniel Arturo Ramos",
                    "bbox": {"left": 100, "top": 80, "width": 300, "height": 30},
                },
            ],
        },
    )
    srcs = set(freeze.best_entity_candidate.evidence_sources or [])
    assert len(srcs) >= 2


# 12. transition before click complete
def test_transition_before_click_preserves_identity():
    meta = _click_meta(
        _profile_picker_uia(["Daniel Chrome D Daniel Arturo Ramos", "Invitado"]),
        (120, 95),
        "Daniel Chrome D Daniel Arturo Ramos",
    )
    begin_pre_click_freeze(120, 95)
    freeze = finalize_pre_click_freeze(cursor_xy=(120, 95), metadata=meta)
    meta2 = apply_freeze_to_metadata(meta, freeze)
    iso = meta2["target_identity_isolation"]
    assert iso.get("trusted_pre_action_identity") is True
    assert "Daniel" in str(iso.get("target_label", ""))


# 13. collection freeze
def test_collection_freeze_full_entities():
    names = ["Entity A", "Entity B", "Entity C"]
    freeze = capture_operational_freeze(
        cursor_xy=(50, 50),
        uia_flat=_profile_picker_uia(names),
    )
    coll = freeze.best_collection_candidate
    assert coll is not None
    assert coll.entity_count >= 2
    assert set(names).issubset(set(coll.entities))


# 14. ambiguity resolution
def test_ambiguity_absorbed_when_close_candidates():
    flat = _profile_picker_uia(["Alpha Option", "Alpha Opt"])
    freeze = capture_operational_freeze(cursor_xy=(110, 90), uia_flat=flat)
    # Múltiples entidades en colección ⇒ contexto ambiguo resuelto por fusión/colección.
    assert len(freeze.collection_entities) >= 2 or (
        freeze.best_entity_candidate is not None
        and len(freeze.operational_collections) >= 1
    )


# 15. dynamic surface escalation
def test_dynamic_surface_escalates_depth():
    flat = [{"name": "Loading…", "control_type": "panecontrol", "states": ["busy"]}]
    freeze = capture_operational_freeze(cursor_xy=(0, 0), uia_flat=flat)
    assert freeze.dynamic_surface_detected
    assert freeze.freeze_depth == "deep"


# 16. freeze persistence
def test_freeze_persistence_in_metadata():
    freeze = capture_operational_freeze(
        cursor_xy=(1, 2),
        uia_flat=_profile_picker_uia(["X", "Y"]),
    )
    meta: dict = {}
    apply_freeze_to_metadata(meta, freeze)
    assert "operational_freeze_snapshot" in meta
    assert meta["pre_action_identity"]["freeze_id"] == freeze.freeze_id


# 17. ERL priority from freeze
def test_erl_priority_from_freeze():
    freeze = capture_operational_freeze(
        cursor_xy=(120, 95),
        uia_flat=_profile_picker_uia(["Daniel Chrome D Daniel Arturo Ramos", "Invitado"]),
    )
    bundle = freeze_to_capture_bundle(freeze)
    ent = erl.extract_operational_entity(
        capture_bundle=bundle,
        trusted_pre_action_identity=True,
        sufficient_for_execution=True,
    )
    assert ent is not None
    assert "Daniel" in ent.display_name


# 18. UCES collection build from freeze
def test_uces_collection_build_from_freeze():
    freeze = capture_operational_freeze(
        cursor_xy=(120, 95),
        uia_flat=_profile_picker_uia(["Daniel Chrome D Daniel Arturo Ramos", "Invitado"]),
    )
    from app.contracts.mission import RawEvent, EventType

    meta = apply_freeze_to_metadata({}, freeze)
    ev = RawEvent(event_type=EventType.MOUSE_CLICK, metadata=meta)
    intent = uces.build_selection_from_capture(raw_event=ev)
    assert intent.selected or intent.collection is not None


# 19. TEL truth enrichment
def test_tel_truth_enrichment():
    freeze = capture_operational_freeze(
        cursor_xy=(120, 95),
        uia_flat=_profile_picker_uia(["Daniel Chrome D Daniel Arturo Ramos", "Invitado"]),
    )
    tel = pcof.tel_truth_enrichment_from_freeze(freeze)
    assert tel.get("pcof_frozen_before_transition") is True
    assert "Daniel" in str(tel.get("operational_entity_label", ""))


# 20. no Chrome/YouTube hardcodes
def test_no_product_hardcodes_in_module():
    import inspect

    src = inspect.getsource(pcof).lower()
    assert "youtube.com" not in src
    assert "google chrome" not in src
    assert "daniel arturo" not in src
    assert "if process == " not in src


def test_mousedown_debounce_idempotent():
    fid1 = begin_pre_click_freeze(1, 1)
    fid2 = begin_pre_click_freeze(2, 2)
    assert fid1 == fid2


def test_mousedown_deep_freeze_preserved_when_late_empty(monkeypatch):
    """Ventana transitoria: mouse_down con entidad, finalize post-transición vacío."""
    names = ["Daniel Chrome D Daniel Arturo Ramos", "Invitado", "Trabajo"]
    flat = _profile_picker_uia(names)
    early = capture_operational_freeze(
        cursor_xy=(120, 95),
        uia_flat=flat,
        window_context={"title": "Google Chrome", "process_name": "chrome.exe", "hwnd": 1},
        trigger="mouse_down_deep",
    )
    assert early.pre_transition_confirmed
    assert "Daniel" in (early.best_entity_candidate.display_name or "")

    monkeypatch.setattr(pcof, "_pending", pcof._PCOFPending(
        freeze_id="test-fid",
        cursor_xy=(120, 95),
        started_ms=int(time.time() * 1000),
        window_context={"title": "Google Chrome", "process_name": "chrome.exe", "hwnd": 1},
        early_snapshot=early.model_dump(mode="json"),
    ))
    meta = {
        "uia": {"name": "", "control_type": "GroupControl", "class_name": "video-stream"},
        "target_identity_isolation": {
            "state_change": {"hwnd_changed": True, "title_changed": True},
        },
    }
    late = pcof.finalize_pre_click_freeze(
        cursor_xy=(120, 95),
        metadata=meta,
        window_context={"title": "YouTube video", "process_name": "chrome.exe", "hwnd": 2},
    )
    assert late.freeze_id == "test-fid"
    assert late.pre_transition_confirmed
    assert "Daniel" in (late.best_entity_candidate.display_name or "")
    assert any("mouse_down_deep" in r for r in late.freeze_reasons)


def test_merge_does_not_drop_entity_on_hwnd_change():
    early = capture_operational_freeze(
        cursor_xy=(100, 100),
        uia_flat=_profile_picker_uia(["Daniel Chrome D Daniel Arturo Ramos", "Invitado"]),
        trigger="mouse_down_deep",
    )
    late = capture_operational_freeze(
        cursor_xy=(100, 100),
        uia={"name": "", "control_type": "GroupControl"},
        uia_flat=[],
        trigger="mouse_up_finalize",
    )
    merged = pcof._merge_pcof_freezes(
        early, late, window_transition=True, freeze_id="fid-1",
    )
    assert merged.pre_transition_confirmed
    assert "Daniel" in (merged.best_entity_candidate.display_name or "")


def test_debounce_does_not_block_second_profile_click():
    import time as _time
    fid1 = begin_pre_click_freeze(10, 10)
    _time.sleep(0.02)
    fid2 = begin_pre_click_freeze(20, 20)
    assert fid1 != fid2


def test_finalize_preserves_mousedown_freeze_id(monkeypatch):
    early = capture_operational_freeze(
        cursor_xy=(1, 1),
        uia_flat=_profile_picker_uia(["A", "B"]),
        trigger="mouse_down_deep",
    )
    fid = "preserve-me"
    monkeypatch.setattr(pcof, "_pending", pcof._PCOFPending(
        freeze_id=fid,
        cursor_xy=(1, 1),
        started_ms=int(time.time() * 1000),
        early_snapshot=early.model_dump(mode="json"),
    ))
    out = pcof.finalize_pre_click_freeze(
        cursor_xy=(1, 1),
        metadata={"uia": {}},
    )
    assert out.freeze_id == fid


def test_pcof_disabled_skips(monkeypatch):
    monkeypatch.setattr(pcof, "pcof_enabled", lambda: False)
    assert begin_pre_click_freeze(0, 0) is None
    meta = pcof.process_click_with_pcof({}, x=0, y=0)
    assert "operational_freeze_snapshot" not in meta


def test_lone_initial_ong_node():
    freeze = capture_operational_freeze(
        cursor_xy=(120, 95),
        uia_flat=_profile_picker_uia(["Daniel Chrome D Daniel Arturo Ramos", "Invitado"]),
    )
    assert freeze.pre_transition_confirmed
    node = pcof.lone_initial_ong_node_from_freeze(freeze)
    assert node is not None
    assert node.get("visible_entities")


def test_expert_panel_lines():
    from app.contracts.mission import Mission, RawEvent, EventType

    freeze = capture_operational_freeze(
        cursor_xy=(10, 10),
        uia_flat=_profile_picker_uia(["A", "B"]),
    )
    m = Mission(name="pcof-test")
    m.raw_trace = [
        RawEvent(
            event_type=EventType.MOUSE_CLICK,
            metadata=apply_freeze_to_metadata({}, freeze),
        ),
    ]
    lines = pcof.pcof_expert_panel_lines(m)
    assert any("Pre-Click Operational Freeze" in ln for ln in lines)
