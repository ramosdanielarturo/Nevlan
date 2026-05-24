"""
Unit tests — ``app.services.missions.semantic_adapter``
=======================================================

Cubre PRD 2026-05-03d §2 (mapeo semantic → MissionStep) + §14 (reglas
duras de bloqueo antes de ejecutar):

  * mapeos open_app / select_profile / open_new_tab / open_url /
    search / search_youtube,
  * blockers empty_profile / empty_query / empty_target / unknown_kind,
  * detección ``is_mission_semantic`` vs misión legacy,
  * adaptación directa desde ``Mission.compiled_execution_graph`` sin
    recompilar.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    InterpretedStep,
    Mission,
    MissionStatus,
    TargetContext,
    ValidationStrategy,
)
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.semantic_adapter import (
    AdaptedBlocker,
    AdaptedMission,
    adapt_mission_to_steps,
    is_mission_semantic,
)


def _tc(name: str = "x", process: str = "chrome.exe") -> TargetContext:
    return TargetContext(
        uia_data={"name": name, "automation_id": "", "control_type": "ButtonControl"},
        process_name=process,
        confidence={"quality_level": "green", "capture_score": 0.9},
    )


def _mission(compiled: list[CompiledStep]) -> Mission:
    m = Mission(id="m1", name="t", status=MissionStatus.EXECUTABLE)
    m.compiled_execution_graph = compiled
    m.interpreted_steps = [
        InterpretedStep(description="s", raw_event_ids=[])
        for _ in compiled
    ]
    return m


# ─── Lista plana (formato tests) ──────────────────────────────────────


class TestFlatInput:
    def test_open_app_maps_to_open_app_mission_step(self):
        adapted = adapt_mission_to_steps([
            {"type": "open_app", "params": {"app": "Chrome"}},
        ])
        assert adapted.ok
        assert len(adapted.steps) == 1
        s = adapted.steps[0]
        assert s.kind == "open_app"
        assert s.params == {"name": "Chrome"}

    def test_select_profile_empty_is_blocker(self):
        adapted = adapt_mission_to_steps([
            {"type": "select_profile", "params": {"profile": ""}},
        ])
        assert not adapted.ok
        assert any(b.code == "empty_profile" for b in adapted.blockers)
        assert adapted.steps == []

    def test_select_profile_needs_user_label_is_blocker(self):
        adapted = adapt_mission_to_steps([
            {
                "type": "select_profile",
                "params": {
                    "profile": "Daniel",
                    "needs_user_label": True,
                },
            },
        ])
        assert not adapted.ok
        assert any(b.code == "empty_profile" for b in adapted.blockers)

    def test_select_profile_with_name_ok(self):
        adapted = adapt_mission_to_steps([
            {
                "type": "select_profile",
                "params": {"profile": "Daniel Arturo Ramos"},
            },
        ])
        assert adapted.ok
        s = adapted.steps[0]
        assert s.kind == "select_profile"
        assert s.params["profile_name"] == "Daniel Arturo Ramos"
        assert s.params["app"]  # default chrome

    def test_open_new_tab_default_chrome(self):
        adapted = adapt_mission_to_steps([
            {"type": "open_new_tab", "params": {}},
        ])
        assert adapted.ok
        assert adapted.steps[0].kind == "open_new_tab"
        assert adapted.steps[0].params["app"] == "chrome"

    def test_open_url_alias_and_url_are_kept(self):
        adapted = adapt_mission_to_steps([
            {
                "type": "open_url",
                "params": {
                    "name": "YouTube",
                    "url": "https://www.youtube.com",
                },
            },
        ])
        assert adapted.ok
        s = adapted.steps[0]
        assert s.kind == "open_url"
        assert s.params["alias"] == "YouTube"
        assert s.params["url"] == "https://www.youtube.com"

    def test_open_url_empty_is_blocker(self):
        adapted = adapt_mission_to_steps([
            {"type": "open_url", "params": {}},
        ])
        assert any(b.code == "empty_target" for b in adapted.blockers)

    def test_search_youtube_maps_to_canonical_search_content(self):
        adapted = adapt_mission_to_steps([
            {
                "type": "search",
                "params": {
                    "val": "Luis Miguel",
                    "site": "youtube",
                    "target": "youtube_search_box",
                },
            },
        ])
        assert adapted.ok
        s = adapted.steps[0]
        assert s.kind == "search_content"
        assert s.params["query"] == "Luis Miguel"
        assert s.params["site"] == "youtube"
        assert s.params["target"] == "youtube_search_box"

    def test_search_without_query_is_blocker(self):
        adapted = adapt_mission_to_steps([
            {"type": "search", "params": {"site": "youtube"}},
        ])
        assert not adapted.ok
        assert any(b.code == "empty_query" for b in adapted.blockers)

    def test_search_without_site_is_blocker_when_generic(self):
        adapted = adapt_mission_to_steps([
            {"type": "search", "params": {"val": "hola"}},
        ])
        # PRD §14: sin sitio definido → no ejecutamos una búsqueda
        # arbitraria; el adapter bloquea con empty_target.
        assert not adapted.ok
        codes = {b.code for b in adapted.blockers}
        assert "empty_target" in codes

    def test_search_with_unsupported_site_is_blocker(self):
        adapted = adapt_mission_to_steps([
            {
                "type": "search",
                "params": {"val": "demo", "site": "amazon"},
            },
        ])
        assert not adapted.ok
        assert any(b.code == "unknown_kind" for b in adapted.blockers)

    def test_unknown_type_is_blocker(self):
        adapted = adapt_mission_to_steps([
            {"type": "raise_hand", "params": {}},
        ])
        assert any(b.code == "unknown_kind" for b in adapted.blockers)
        assert adapted.steps == []

    def test_silent_skip_types_are_ignored_not_blocking(self):
        adapted = adapt_mission_to_steps([
            {"type": "fill_field", "params": {"val": "x"}},
            {"type": "open_app", "params": {"app": "Chrome"}},
        ])
        # fill_field se ignora (no es ejecutable por Smart Executor),
        # open_app se adapta. Nada queda bloqueado.
        assert adapted.ok
        assert len(adapted.steps) == 1
        assert adapted.steps[0].kind == "open_app"
        assert any("fill_field" in s for s in adapted.skipped)


# ─── Mission.compiled_execution_graph ─────────────────────────────────


class TestFromCompiledMission:
    def test_is_semantic_detects_launch_app(self):
        m = _mission([CompiledStep(
            action_strategy=ActionStrategy.LAUNCH_APP,
            action_payload={"app_name": "Chrome"},
            target_context=_tc(),
        )])
        assert is_mission_semantic(m)

    def test_is_semantic_detects_submit_search(self):
        m = _mission([CompiledStep(
            action_strategy=ActionStrategy.SUBMIT_SEARCH,
            action_payload={"text": "x", "site": "youtube"},
            target_context=_tc(),
        )])
        assert is_mission_semantic(m)

    def test_is_semantic_detects_set_field_value_with_site_submit(self):
        m = _mission([CompiledStep(
            action_strategy=ActionStrategy.SET_FIELD_VALUE,
            action_payload={
                "text": "x",
                "submit_after": True,
                "search_site": "youtube",
            },
            target_context=_tc(),
        )])
        assert is_mission_semantic(m)

    def test_legacy_pure_click_is_not_semantic(self):
        m = _mission([CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            action_payload={},
            target_context=_tc(),
        )])
        assert not is_mission_semantic(m)

    def test_full_youtube_mission_maps_five_steps(self):
        m = _mission([
            CompiledStep(
                action_strategy=ActionStrategy.LAUNCH_APP,
                action_payload={"app_name": "Chrome"},
                target_context=_tc(),
            ),
            CompiledStep(
                action_strategy=ActionStrategy.SELECT_PROFILE,
                action_payload={"profile": "Daniel"},
                target_context=_tc(),
            ),
            CompiledStep(
                action_strategy=ActionStrategy.OPEN_NEW_TAB,
                action_payload={"app": "chrome"},
                target_context=_tc(),
            ),
            CompiledStep(
                action_strategy=ActionStrategy.OPEN_BOOKMARK,
                action_payload={
                    "name": "YouTube",
                    "url": "https://www.youtube.com",
                },
                target_context=_tc(),
            ),
            CompiledStep(
                action_strategy=ActionStrategy.SUBMIT_SEARCH,
                action_payload={
                    "text": "Luis Miguel",
                    "site": "youtube",
                    "target_label": "youtube_search_box",
                },
                target_context=_tc(),
            ),
        ])

        adapted = adapt_mission_to_steps(m)
        assert adapted.ok, adapted.blockers
        assert len(adapted.steps) == 5
        kinds = [s.kind for s in adapted.steps]
        assert kinds == [
            "open_app",
            "select_profile",
            "open_new_tab",
            "open_url",
            "search_content",
        ]
        # cada step lleva block_id (del block_id del compiled step si
        # existiera — aquí None, pero el ``mission_id`` se refleja en
        # los step.id sintetizados).
        for s in adapted.steps:
            assert isinstance(s, MissionStep)

    def test_navigate_or_search_url_maps_to_open_url(self):
        m = _mission([CompiledStep(
            action_strategy=ActionStrategy.NAVIGATE_OR_SEARCH,
            action_payload={"text": "https://www.youtube.com"},
            target_context=_tc(),
        )])
        adapted = adapt_mission_to_steps(m)
        assert adapted.ok
        assert adapted.steps[0].kind == "open_url"
        assert "youtube" in adapted.steps[0].params.get("url", "")


# ─── Dict input (semantic_interpreter output) ─────────────────────────


class TestDictInput:
    def test_blocks_format(self):
        source = {
            "mission_summary": "Abrir YouTube",
            "blocks": [
                {
                    "id": "b1",
                    "name": "Abrir entorno",
                    "steps": [
                        {
                            "type": "open_app",
                            "params": {"app": "Chrome"},
                        },
                        {
                            "type": "select_profile",
                            "params": {"profile": "Daniel"},
                        },
                    ],
                },
                {
                    "id": "b2",
                    "name": "Buscar",
                    "steps": [
                        {
                            "type": "search",
                            "params": {
                                "val": "x",
                                "site": "youtube",
                            },
                        },
                    ],
                },
            ],
        }
        adapted = adapt_mission_to_steps(source)
        assert adapted.ok
        assert len(adapted.steps) == 3
        # block_id se propaga (para checkpoints del executor).
        bids = [s.block_id for s in adapted.steps]
        assert "b1" in bids and "b2" in bids


# ─── Edge cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_source_gives_non_semantic_empty(self):
        adapted = adapt_mission_to_steps([])
        assert not adapted.is_semantic
        assert adapted.steps == []
        assert adapted.blockers == []

    def test_search_youtube_default_target_when_missing(self):
        adapted = adapt_mission_to_steps([
            {"type": "search_youtube", "params": {"query": "abc"}},
        ])
        assert adapted.ok
        assert adapted.steps[0].params["target"] == "youtube_search_box"
