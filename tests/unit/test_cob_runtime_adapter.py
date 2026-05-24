"""COB Runtime Adapter shadow-safe — tests."""
from __future__ import annotations

from app.contracts.mission import (
    ActionStrategy,
    CanonicalOperationalBlock,
    Mission,
    mission_from_dict,
)
from app.services.missions.cob_replay_shadow import build_cob_replay_plan
from app.services.missions.cob_runtime_adapter import (
    build_cob_runtime_adapter_report,
    partition_coord_safe_fallbacks,
    translate_cob_replay_step,
)


class TestNavigateTranslation:
    def test_navigate_ctrl_l_url_enter_validate(self):
        cob = CanonicalOperationalBlock(
            canonical_action="navigate_to_site",
            block_type="navigation_session",
            session_id="n1",
            params={"url_preview": "https://example.com/path"},
            completion_signals=["navigation_submit"],
        )
        steps = build_cob_replay_plan(cob)
        assert len(steps) == 4
        rows = [translate_cob_replay_step(cob, s) for s in steps]
        assert rows[0]["executor_action"] == ActionStrategy.SEND_HOTKEY.value
        assert rows[0]["uic_capability_id"] == "focus_browser_address_bar"
        assert rows[0]["required_params"].get("combo") == ["ctrl", "l"]
        assert rows[0]["strategy"]["primary"] == "open_url:hotkey_ctrl_l"

        assert rows[1]["executor_action"] == ActionStrategy.TYPE_TEXT.value
        assert rows[1]["required_params"].get("url") == "https://example.com/path"
        assert rows[1]["unsupported_reason"] is None

        assert rows[2]["executor_action"] == ActionStrategy.SEND_HOTKEY.value
        assert rows[3]["executor_action"] == ActionStrategy.WAIT_FOR_STATE.value
        assert rows[3]["validator"] == "_passthrough_validator"
        assert all(r.get("unsupported_reason") is None for r in rows)


class TestSearchTranslation:
    def test_search_focus_inject_submit_validate_browse(self):
        cob = CanonicalOperationalBlock(
            canonical_action="search_content_and_browse_results",
            block_type="search_session",
            session_id="s1",
            params={"query_preview": "nevlan docs"},
            completion_signals=["search_submit"],
        )
        steps = build_cob_replay_plan(cob)
        rows = [translate_cob_replay_step(cob, s) for s in steps]
        assert rows[0]["uic_capability_id"] == "focus_search_field"
        assert rows[0]["executor_action"] == ActionStrategy.CLICK.value
        assert rows[1]["executor_action"] == ActionStrategy.TYPE_TEXT.value
        assert rows[2]["executor_action"] == ActionStrategy.SUBMIT_SEARCH.value
        assert rows[3]["uic_capability_id"] == "validate_outcome"
        assert rows[4]["uic_capability_id"] == "scroll_results"
        assert rows[4]["executor_action"] == ActionStrategy.SCROLL.value


class TestOpenApplication:
    def test_launcher_open_app_strategy_chain(self):
        cob = CanonicalOperationalBlock(
            canonical_action="open_application",
            block_type="launcher_session",
            session_id="l1",
            params={"query_preview": "Chrome", "label_hint": "Google Chrome"},
        )
        steps = build_cob_replay_plan(cob)
        rows = [translate_cob_replay_step(cob, s) for s in steps]
        assert rows[0]["executor_action"] == ActionStrategy.LAUNCH_APP.value
        assert rows[0]["strategy"]["primary"] == "open_app:windows_search"
        assert rows[0]["required_params"].get("method") == "windows_search"

        assert rows[1]["executor_action"] == ActionStrategy.TYPE_TEXT.value
        assert rows[2]["executor_action"] == ActionStrategy.CLICK.value
        assert rows[2]["uic_capability_id"] == "select_visible_option"


class TestMissingParamsUnsupported:
    def test_navigate_without_url_marks_inject_unsupported(self):
        cob = CanonicalOperationalBlock(
            canonical_action="navigate_to_site",
            block_type="navigation_session",
            session_id="x",
            params={},
        )
        steps = build_cob_replay_plan(cob)
        inject = next(s for s in steps if s.phase_id == "inject_navigation_text")
        row = translate_cob_replay_step(cob, inject)
        assert row["unsupported_reason"] == "MISSING_URL_OR_NAVIGATION_TEXT"
        assert row["executor_action"] == "UNSUPPORTED_SHADOW"


class TestCoordFallbackRejected:
    def test_partition_moves_coord_strategies_to_unsafe(self):
        safe, unsafe = partition_coord_safe_fallbacks(
            ("scroll_results:wheel", "navigate:coords_absolute", "foo_coordinate_bar"),
        )
        assert "scroll_results:wheel" in safe
        assert any("coord" in u.lower() for u in unsafe)


class TestCoverageCanonicalMission:
    def test_coverage_against_sep_open_url_search_open_app(self):
        m = Mission(name="canonical_shadow")
        m.semantic_execution_plan = {
            "steps": [
                {"type": "open_app"},
                {"type": "open_url"},
                {"type": "search_site"},
            ],
        }
        m.canonical_operational_blocks = [
            CanonicalOperationalBlock(
                id="c1",
                canonical_action="open_application",
                block_type="launcher_session",
                session_id="a",
                params={"query_preview": "Edge", "label_hint": "Microsoft Edge"},
            ),
            CanonicalOperationalBlock(
                id="c2",
                canonical_action="navigate_to_site",
                block_type="navigation_session",
                session_id="b",
                params={"url_preview": "https://x.test"},
            ),
            CanonicalOperationalBlock(
                id="c3",
                canonical_action="search_content_and_browse_results",
                block_type="search_session",
                session_id="c",
                params={"query_preview": "q"},
            ),
        ]
        report = build_cob_runtime_adapter_report(m, persist=False)
        cov = report["coverage_vs_sep"]
        assert cov.get("cob_executable_coverage_ratio", 0) >= 0.85
        assert cov.get("sep_adapter_fuzzy_overlap_ratio", 0) == 1.0
        assert len(report["limitations"]) >= 2


class TestLegacyMission:
    def test_mission_json_without_adapter_field(self):
        m = mission_from_dict({
            "name": "z",
            "raw_trace": [],
            "interpreted_steps": [],
            "compiled_execution_graph": [],
        })
        assert m.cob_runtime_adapter_shadow is None


class TestReportPersist:
    def test_build_persists_on_mission(self):
        m = Mission(name="p")
        m.canonical_operational_blocks = [
            CanonicalOperationalBlock(
                id="id1",
                canonical_action="navigate_to_site",
                block_type="navigation_session",
                session_id="s",
                params={"url_preview": "https://a.io"},
            ),
        ]
        build_cob_runtime_adapter_report(m, persist=True)
        assert m.cob_runtime_adapter_shadow is not None
        assert m.cob_runtime_adapter_shadow.get("shadow_safe") is True
