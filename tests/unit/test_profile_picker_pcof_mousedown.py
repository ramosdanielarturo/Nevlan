"""Profile picker + PCOF mousedown — compilación SEP sin aclaración falsa."""

from __future__ import annotations

import pytest

from app.contracts.mission import RawEvent, EventType
from app.services.runtime import pre_click_operational_freeze as pcof
from app.services.runtime import universal_collection_entity_engine as uces
from app.services.runtime.pre_click_operational_freeze import (
    apply_freeze_to_metadata,
    capture_operational_freeze,
    finalize_pre_click_freeze,
    qualifies_pcof_for_sep_promotion,
)


@pytest.fixture(autouse=True)
def _enable_pcof(monkeypatch):
    monkeypatch.setattr(pcof, "pcof_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(pcof, "_eager_uia_at_point", lambda _x, _y: ({}, []))


def _profile_flat(names: list[str]) -> list[dict]:
    return [
        {
            "name": n,
            "control_type": "listitemcontrol",
            "role": "listitem",
            "parent_chain": ["ProfileList", "Picker"],
            "bbox": {"left": 100, "top": 80 + i * 40, "width": 320, "height": 36},
        }
        for i, n in enumerate(names)
    ]


def test_compiler_select_entity_from_pcof_freeze():
    names = ["Daniel Chrome D Daniel Arturo Ramos", "Invitado"]
    freeze = capture_operational_freeze(
        cursor_xy=(120, 95),
        uia_flat=_profile_flat(names),
        trigger="mouse_down_deep",
    )
    ok, _ = qualifies_pcof_for_sep_promotion(freeze)
    assert ok
    meta = apply_freeze_to_metadata({}, freeze)
    ev = RawEvent(event_type=EventType.MOUSE_CLICK, metadata=meta)
    intent = uces.build_selection_from_capture(raw_event=ev)
    assert intent.selected
    label = ""
    if intent.entity:
        label = intent.entity.display_name or ""
    elif intent.operational_entity:
        label = intent.operational_entity.display_name or ""
    ents = list((intent.collection.entities if intent.collection else []) or [])
    assert "Daniel" in label or any("Daniel" in str(e) for e in ents)


def test_transient_picker_disappears_entity_survives(monkeypatch):
    names = ["Daniel Chrome D Daniel Arturo Ramos", "Invitado"]
    early = capture_operational_freeze(
        cursor_xy=(120, 95),
        uia_flat=_profile_flat(names),
        trigger="mouse_down_deep",
    )
    monkeypatch.setattr(pcof, "_pending", pcof._PCOFPending(
        freeze_id="pcof-profile",
        cursor_xy=(120, 95),
        started_ms=1,
        window_context={"title": "Google Chrome", "hwnd": 99},
        early_snapshot=early.model_dump(mode="json"),
    ))
    meta = {
        "uia": {"name": "", "control_type": "GroupControl"},
        "target_identity_isolation": {"state_change": {"hwnd_changed": True}},
    }
    freeze = finalize_pre_click_freeze(
        cursor_xy=(120, 95),
        metadata=meta,
        window_context={"title": "YouTube", "hwnd": 100},
    )
    ok, reasons = qualifies_pcof_for_sep_promotion(freeze, require_single_candidate=False)
    assert ok, reasons
    assert "Daniel" in (freeze.best_entity_candidate.display_name or "")
