"""
ArthurOS Unit Tests — TargetContext V3 (FASE 1 + FASE 12)
---------------------------------------------------------
Verifica que el contrato V3 (TargetBundle) tiene todos los bloques
requeridos por el PRD y sobrevive al roundtrip de serialización.
"""
from __future__ import annotations

import json

from app.contracts.mission import (
    TargetContext, CompiledStep, ActionStrategy,
    mission_to_dict, mission_from_dict, Mission,
)


# ──────────────────────────────────────────────────────────────────
# Bloques V3 requeridos
# ──────────────────────────────────────────────────────────────────

class TestV3Fields:
    def test_target_context_has_v3_blocks(self):
        tc = TargetContext()
        # Bloques que el PRD exige
        for attr in (
            "uia_data", "web_data", "anchor_bbox", "image_ref",
            "image_ref_mid", "fallback_coords",
            "fallback_coords_relative_to_window",
            "click_offset_within_bbox",
            "alternates", "confidence", "semantic",
            "process_name", "window_title",
        ):
            assert hasattr(tc, attr), \
                f"TargetContext debería tener el campo {attr!r} (FASE 1)"

    def test_alternates_default_empty_list(self):
        tc = TargetContext()
        assert tc.alternates == [] or tc.alternates is None

    def test_confidence_default_dict_or_none(self):
        tc = TargetContext()
        assert tc.confidence in (None, {}) or isinstance(tc.confidence, dict)

    def test_semantic_default_dict_or_none(self):
        tc = TargetContext()
        assert tc.semantic in (None, {}) or isinstance(tc.semantic, dict)


# ──────────────────────────────────────────────────────────────────
# Roundtrip de serialización con V3 fields cargados
# ──────────────────────────────────────────────────────────────────

class TestV3Roundtrip:
    def test_full_v3_target_context_roundtrips(self):
        m = Mission(name="t")
        tc = TargetContext()
        tc.uia_data = {
            "name": "Save", "automation_id": "btnSave",
            "control_type": "ButtonControl", "class_name": "Button",
        }
        tc.web_data = {
            "test_id": "save-btn",
            "role": "button",
            "accessible_name": "Save",
            "url": "https://example.com",
            "domain": "example.com",
            "locators": [
                {"kind": "test_id", "value": "save-btn"},
                {"kind": "role", "role": "button", "name": "Save"},
            ],
        }
        tc.anchor_bbox = {"x": 100, "y": 200, "w": 80, "h": 40}
        tc.image_ref_mid = "data:image/png;base64,xxx"
        tc.fallback_coords = {"x": 140, "y": 220}
        tc.fallback_coords_relative_to_window = {"fx": 0.5, "fy": 0.5}
        tc.confidence = {
            "capture_score": 92,
            "success_by_method": {"web_locator": {"ok": 5, "fail": 1}},
            "alternate_streaks": {},
            "primary_total_ok": 5,
            "primary_total_fail": 1,
        }
        tc.semantic = {
            "intent": "click_button",
            "human_label": "Save",
            "expected_state_after": "dialog_closed",
            "near_text": "Cliente",
        }
        tc.alternates = [
            {"uia_data": {"automation_id": "btnSave2"},
             "fallback_coords": {"x": 200, "y": 300}}
        ]
        tc.process_name = "chrome.exe"
        tc.window_title = "Example - Chrome"

        step = CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            target_context=tc,
        )
        m.compiled_execution_graph = [step]

        # Roundtrip
        data = mission_to_dict(m)
        json_str = json.dumps(data, default=str, ensure_ascii=False)
        loaded = mission_from_dict(json.loads(json_str))

        assert len(loaded.compiled_execution_graph) == 1
        rtc = loaded.compiled_execution_graph[0].target_context
        assert rtc.uia_data["automation_id"] == "btnSave"
        assert rtc.web_data["test_id"] == "save-btn"
        assert rtc.confidence["capture_score"] == 92
        assert rtc.semantic["intent"] == "click_button"
        assert rtc.semantic["expected_state_after"] == "dialog_closed"
        assert len(rtc.alternates) == 1
        assert rtc.alternates[0]["uia_data"]["automation_id"] == "btnSave2"
        assert rtc.process_name == "chrome.exe"
