"""Unit tests for the Nevlan Semantic Mission Interpreter.

Validates the new block-based JSON output, ensuring:
  - Fusion of LAUNCH_APP / NAVIGATE_OR_SEARCH / SET_FIELD_VALUE+submit.
  - Noise filtering (generic UIA, technical labels, lone scrolls/keys).
  - Field commit applied.
  - Mandatory verification on every step.
  - Confidence ∈ [0,1], steps below MIN_CONFIDENCE dropped.
  - Block partitioning by phase / process change.
  - Token-efficient short keys.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import (
    Mission, RawEvent, EventType, MouseAction, KeyboardAction, WindowContext,
)
from app.services.missions.semantic_interpreter import (
    interpret_mission, MIN_CONFIDENCE, MAX_STEPS_PER_BLOCK,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _click(*, x=100, y=100, uia_name="", uia_aid="", uia_ctype="ButtonControl",
           web_role="", web_label="", web_acc="", domain="", url="",
           process_name="") -> RawEvent:
    meta: dict = {}
    if uia_name or uia_aid:
        meta["uia"] = {
            "name": uia_name, "automation_id": uia_aid,
            "control_type": uia_ctype, "class_name": "X",
        }
    if web_role or web_label or web_acc or domain:
        meta["web"] = {
            "role": web_role, "label": web_label, "accessible_name": web_acc,
            "url": url or (f"https://{domain}/" if domain else ""),
            "domain": domain, "test_id": "", "locators": [],
        }
    win = WindowContext(
        title="x", process_name=process_name or "explorer.exe",
        bounding_box={"left": 0, "top": 0, "width": 800, "height": 600},
    )
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=x, y=y, button="left"),
        metadata=meta, window_context=win,
    )


def _type_text_session(text: str, *, label="", domain="", url="",
                       process_name="chrome.exe") -> RawEvent:
    """Synthetic field session — same shape as recorder produces post-commit."""
    meta = {
        "field_session": True,
        "final_text": text,
        "field_label": label,
        "committed_read_source": "uia",
    }
    if domain or url:
        meta["web"] = {
            "domain": domain, "url": url or f"https://{domain}/",
            "role": "searchbox", "test_id": "",
            "label": label, "accessible_name": label,
        }
    win = WindowContext(
        title="x", process_name=process_name,
        bounding_box={"left": 0, "top": 0, "width": 800, "height": 600},
    )
    return RawEvent(
        event_type=EventType.KEYBOARD_TYPE_TEXT, metadata=meta,
        window_context=win, keyboard_action=None,
    )


def _key(key: str, modifiers=None, process_name="explorer.exe") -> RawEvent:
    win = WindowContext(
        title="x", process_name=process_name,
        bounding_box={"left": 0, "top": 0, "width": 800, "height": 600},
    )
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=[key], modifiers=modifiers or []),
        window_context=win,
    )


def _mission(events) -> Mission:
    m = Mission(name="t")
    m.raw_trace = events
    return m


# ──────────────────────────────────────────────────────────────────────
# Output shape
# ──────────────────────────────────────────────────────────────────────

class TestOutputShape:
    def test_empty_mission(self):
        out = interpret_mission(_mission([]))
        assert out["mission_summary"]
        assert out["blocks"] == []

    def test_top_level_keys(self):
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
        ]
        out = interpret_mission(_mission(events))
        assert set(out.keys()) == {"mission_summary", "blocks"}
        assert isinstance(out["mission_summary"], str)
        assert isinstance(out["blocks"], list)

    def test_step_required_keys(self):
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
        ]
        out = interpret_mission(_mission(events))
        assert out["blocks"], "should have at least one block"
        for b in out["blocks"]:
            assert b["id"].startswith("b")
            assert b["name"]
            assert isinstance(b["steps"], list)
            for s in b["steps"]:
                assert s["id"].startswith("s")
                assert s["type"]
                assert "params" in s
                assert s["verification"], "verification is mandatory"
                assert 0.0 <= s["confidence"] <= 1.0


# ──────────────────────────────────────────────────────────────────────
# Intent fusion
# ──────────────────────────────────────────────────────────────────────

class TestIntentFusion:
    def test_windows_search_fuses_to_open_app(self):
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
        ]
        out = interpret_mission(_mission(events))
        types = [s["type"] for b in out["blocks"] for s in b["steps"]]
        assert "open_app" in types
        # Y NO debe haber click ni type_text sueltos del Win Search.
        assert "click" not in types
        assert "type" not in types

    def test_open_app_carries_canonical_name(self):
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("Google Chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
        ]
        out = interpret_mission(_mission(events))
        first = out["blocks"][0]["steps"][0]
        assert first["type"] == "open_app"
        assert first["params"]["app"] == "chrome"

    def test_field_with_submit_becomes_search(self):
        events = [
            _type_text_session(
                "devuelveme el amor luis miguel",
                label="Buscar", domain="youtube.com",
                process_name="chrome.exe",
            ),
            _key("enter", process_name="chrome.exe"),
        ]
        out = interpret_mission(_mission(events))
        steps = [s for b in out["blocks"] for s in b["steps"]]
        # Debe haber UN solo paso semántico "search" (no fill+enter).
        searches = [s for s in steps if s["type"] == "search"]
        assert len(searches) == 1
        s = searches[0]
        assert s["params"]["val"] == "devuelveme el amor luis miguel"
        assert s["params"]["submit"] is True
        assert s["params"].get("site") == "youtube"


# ──────────────────────────────────────────────────────────────────────
# Noise filtering
# ──────────────────────────────────────────────────────────────────────

class TestNoiseFiltering:
    def test_generic_uia_click_is_dropped(self):
        events = [
            _click(uia_name="", uia_ctype="GroupControl",
                   process_name="chrome.exe"),
        ]
        out = interpret_mission(_mission(events))
        assert all(
            s["type"] != "click" for b in out["blocks"] for s in b["steps"]
        ), "click on GroupControl with no name must be dropped"

    def test_technical_label_click_is_dropped(self):
        events = [
            _click(uia_name="guide-service",
                   uia_ctype="CustomControl",
                   process_name="chrome.exe"),
        ]
        out = interpret_mission(_mission(events))
        assert all(
            s["type"] != "click" for b in out["blocks"] for s in b["steps"]
        )

    def test_lone_tab_and_capslock_are_noise(self):
        events = [
            _key("tab"),
            _key("capslock"),
        ]
        out = interpret_mission(_mission(events))
        assert out["blocks"] == []

    def test_scroll_alone_is_noise(self):
        events = [
            RawEvent(
                event_type=EventType.MOUSE_SCROLL,
                mouse_action=MouseAction(x=400, y=300, button="middle"),
                metadata={"scroll": {"dy": -200, "dx": 0}},
            ),
        ]
        out = interpret_mission(_mission(events))
        assert out["blocks"] == []


# ──────────────────────────────────────────────────────────────────────
# Verification
# ──────────────────────────────────────────────────────────────────────

class TestVerification:
    def test_every_step_has_verification(self):
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
            _type_text_session("luis miguel", label="Buscar",
                               domain="youtube.com",
                               process_name="chrome.exe"),
            _key("enter", process_name="chrome.exe"),
        ]
        out = interpret_mission(_mission(events))
        for b in out["blocks"]:
            for s in b["steps"]:
                assert s["verification"], f"{s['type']} missing verification"

    def test_open_app_verification_uses_process_name(self):
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
        ]
        out = interpret_mission(_mission(events))
        first = out["blocks"][0]["steps"][0]
        assert "chrome.exe" in first["verification"]


# ──────────────────────────────────────────────────────────────────────
# Confidence
# ──────────────────────────────────────────────────────────────────────

class TestConfidence:
    def test_confidence_in_range(self):
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
        ]
        out = interpret_mission(_mission(events))
        for b in out["blocks"]:
            for s in b["steps"]:
                assert 0.0 <= s["confidence"] <= 1.0

    def test_open_app_high_confidence(self):
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
        ]
        out = interpret_mission(_mission(events))
        first = out["blocks"][0]["steps"][0]
        assert first["confidence"] >= 0.9


# ──────────────────────────────────────────────────────────────────────
# Block partitioning
# ──────────────────────────────────────────────────────────────────────

class TestBlocks:
    def test_phase_change_creates_new_block(self):
        # open_app (environment) → search (search): debe crear 2 bloques.
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
            _type_text_session("luis miguel", label="Buscar",
                               domain="youtube.com",
                               process_name="chrome.exe"),
            _key("enter", process_name="chrome.exe"),
        ]
        out = interpret_mission(_mission(events))
        assert len(out["blocks"]) >= 2, (
            f"environment + search debe ser ≥2 bloques, "
            f"fueron {len(out['blocks'])}: "
            f"{[b['name'] for b in out['blocks']]}"
        )

    def test_block_size_capped(self):
        # 8 fill_field consecutivos del mismo proceso → debe partirse.
        events = []
        for i in range(8):
            events.append(_type_text_session(
                f"value-{i}", label=f"campo_{i}",
                process_name="chrome.exe",
            ))
        out = interpret_mission(_mission(events))
        for b in out["blocks"]:
            assert len(b["steps"]) <= MAX_STEPS_PER_BLOCK, (
                f"block '{b['name']}' has {len(b['steps'])} steps, "
                f"exceeds max {MAX_STEPS_PER_BLOCK}"
            )

    def test_block_ids_sequential(self):
        events = [
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
            _type_text_session("hola", label="Buscar",
                               domain="youtube.com",
                               process_name="chrome.exe"),
            _key("enter", process_name="chrome.exe"),
        ]
        out = interpret_mission(_mission(events))
        ids = [b["id"] for b in out["blocks"]]
        assert ids == [f"b{i}" for i in range(1, len(ids) + 1)]

        step_ids = [s["id"] for b in out["blocks"] for s in b["steps"]]
        assert step_ids == [f"s{i}" for i in range(1, len(step_ids) + 1)]


# ──────────────────────────────────────────────────────────────────────
# End-to-end canonical scenario
# ──────────────────────────────────────────────────────────────────────

class TestCanonicalYouTubeScenario:
    """Replica el caso del PRD: abrir Chrome → ir a YouTube → buscar canción."""

    def test_full_pipeline_produces_clean_output(self):
        events = [
            # Abrir Chrome via Win Search
            _click(uia_name="Type here to search",
                   process_name="searchhost.exe"),
            _type_text_session("chrome", label="Type here to search",
                               process_name="searchhost.exe"),
            _key("enter", process_name="searchhost.exe"),
            # Ruido típico: click en panel de Chrome
            _click(uia_name="", uia_ctype="PaneControl",
                   process_name="chrome.exe"),
            # Buscar en YouTube
            _type_text_session(
                "devuelveme el amor luis miguel",
                label="Buscar", domain="youtube.com",
                process_name="chrome.exe",
            ),
            _key("enter", process_name="chrome.exe"),
        ]
        out = interpret_mission(_mission(events))

        # No debe haber pasos de ruido.
        for b in out["blocks"]:
            for s in b["steps"]:
                assert s["type"] in {
                    "open_app", "select_profile", "open_url",
                    "search", "fill_field", "submit_form",
                }, f"unexpected step type: {s['type']}"

        # Mission summary debe mencionar la búsqueda.
        assert "luis miguel" in out["mission_summary"].lower() \
            or "youtube" in out["mission_summary"].lower()

        # Debe haber un open_app + una search final.
        flat = [s for b in out["blocks"] for s in b["steps"]]
        assert flat[0]["type"] == "open_app"
        assert flat[-1]["type"] == "search"
        assert flat[-1]["params"]["submit"] is True
        assert flat[-1]["params"]["val"] == "devuelveme el amor luis miguel"

    def test_short_keys(self):
        """Optimización de tokens: keys cortas (val, app, url)."""
        events = [
            _type_text_session("hola", label="Buscar",
                               domain="youtube.com",
                               process_name="chrome.exe"),
            _key("enter", process_name="chrome.exe"),
        ]
        out = interpret_mission(_mission(events))
        flat = [s for b in out["blocks"] for s in b["steps"]]
        assert flat
        params = flat[-1]["params"]
        assert "val" in params, "search params should use short 'val' key"
        assert "value" not in params
