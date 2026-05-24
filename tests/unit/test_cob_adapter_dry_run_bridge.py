"""Tests para COB Adapter Dry-Run Bridge."""
from __future__ import annotations

from app.contracts.mission import CanonicalOperationalBlock, Mission, mission_from_dict
from app.services.missions.cob_adapter_dry_run_bridge import (
    evaluate_adapter_row_dry_run,
    run_cob_adapter_dry_run_bridge,
)
from app.services.missions.cob_runtime_adapter import build_cob_runtime_adapter_report


def _canonical_mission() -> Mission:
    m = Mission(name="dry_canonical")
    m.semantic_execution_plan = {
        "steps": [
            {"type": "open_app", "params": {"app": "Edge"}},
            {"type": "open_url", "params": {"url": "https://x.test"}},
            {"type": "search_content", "params": {"query": "q"}},
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
    return m


class TestDryRunCanonical:
    def test_canonical_open_app_open_url_search_content_passes(self):
        m = _canonical_mission()
        build_cob_runtime_adapter_report(m, persist=True)
        report = run_cob_adapter_dry_run_bridge(m, persist=True)
        assert report["summary"]["supported_count"] == report["summary"]["total_rows"]
        assert all(r["supported"] for r in report["rows"])
        assert m.cob_dry_run_shadow is not None


class TestMissingLabelBlocker:
    def test_missing_label_hint_blocks_confirm_step(self):
        m = Mission(name="lbl")
        m.semantic_execution_plan = {"steps": [{"type": "open_app", "params": {}}]}
        m.canonical_operational_blocks = [
            CanonicalOperationalBlock(
                id="c1",
                canonical_action="open_application",
                block_type="launcher_session",
                session_id="a",
                params={"query_preview": "X"},
            ),
        ]
        build_cob_runtime_adapter_report(m, persist=True)
        report = run_cob_adapter_dry_run_bridge(m, persist=True)
        blocked = [r for r in report["rows"] if not r["supported"]]
        assert blocked
        reasons = " ".join(str(r.get("blocker_reason")) for r in blocked)
        assert "MISSING_SELECTION_LABEL" in reasons or "UNSUPPORTED" in reasons.upper()


class TestCoordSafetyRejected:
    def test_primary_coord_strategy_fails_safety(self):
        m = Mission(name="coord")
        row = {
            "executor_action": "type_text",
            "uic_capability_id": "type_field_value",
            "required_params": {"text": "hi", "query": "hi"},
            "canonical_action": "search_content_and_browse_results",
            "plan_step_order": 2,
            "phase_id": "inject_semantic_query",
            "strategy": {"primary": "evil:coords_absolute_nav", "safe_fallbacks": []},
            "fallback_policy": {
                "primary": "evil:coords_absolute_nav",
                "safe_fallbacks": [],
                "rejected_coord_fallbacks": ["bad:coords"],
            },
            "safety_flags": [],
        }
        ev = evaluate_adapter_row_dry_run(m, row)
        assert ev["safety_passed"] is False
        assert "COORD" in ev["blocker_reason"]


class TestUnknownCapability:
    def test_unknown_capability_fails_registry(self):
        m = Mission(name="unk")
        row = {
            "executor_action": "click",
            "uic_capability_id": "__no_such_capability__",
            "required_params": {},
            "canonical_action": "select_visible_entity",
            "plan_step_order": 1,
            "phase_id": "x",
            "strategy": {"primary": "x", "safe_fallbacks": []},
            "fallback_policy": {
                "primary": "x",
                "safe_fallbacks": [],
                "rejected_coord_fallbacks": [],
            },
            "safety_flags": [],
        }
        ev = evaluate_adapter_row_dry_run(m, row)
        assert ev["registry_valid"] is False
        assert ev["blocker_reason"] == "UNKNOWN_CAPABILITY"


class TestValidatorFailure:
    def test_search_site_validator_without_query(self):
        m = Mission(name="val")
        row = {
            "executor_action": "type_text",
            "uic_capability_id": "search_site",
            "required_params": {},
            "canonical_action": "search_content_and_browse_results",
            "plan_step_order": 1,
            "phase_id": "inject_semantic_query",
            "strategy": {"primary": "search_site:smart_route", "safe_fallbacks": []},
            "fallback_policy": {
                "primary": "search_site:smart_route",
                "safe_fallbacks": [],
                "rejected_coord_fallbacks": [],
            },
            "safety_flags": [],
        }
        ev = evaluate_adapter_row_dry_run(m, row)
        assert ev["validator_passed"] is False
        assert "VALIDATOR_FAILED" in ev["blocker_reason"] or ev["blocker_reason"] == "INCOMPLETE_PARAMS"


class TestHumanReadableDiff:
    def test_diff_mentions_cob_and_sep(self):
        m = _canonical_mission()
        build_cob_runtime_adapter_report(m, persist=True)
        report = run_cob_adapter_dry_run_bridge(m, persist=True)
        txt = " ".join(r["human_readable_diff"] for r in report["rows"])
        assert "COB[" in txt and "SEP[" in txt


class TestLegacyJson:
    def test_mission_without_dry_run_field(self):
        m = mission_from_dict({
            "name": "old",
            "raw_trace": [],
            "interpreted_steps": [],
            "compiled_execution_graph": [],
        })
        assert m.cob_dry_run_shadow is None
