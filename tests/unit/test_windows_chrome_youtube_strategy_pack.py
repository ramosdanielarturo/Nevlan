"""Integration-ish tests — Windows→Chrome→YouTube strategy pack."""
from __future__ import annotations

from app.services.missions.execution_contracts import MissionStep
from app.services.missions.strategy_packs.router import apply_strategy_packs
from app.services.missions.strategy_packs.windows_chrome_youtube import (
    PACK_ID,
    build_flow_step_sequence,
    enrich_windows_chrome_youtube_flow,
    matches_windows_chrome_youtube_flow,
)
from app.services.runtime.nevlan_runtime_convergence import (
    is_forbidden_primary_strategy,
)


def test_flow_matcher_recognizes_canonical_sequence():
    steps = build_flow_step_sequence()
    assert matches_windows_chrome_youtube_flow(steps) is True


def test_strategy_pack_applies_identity_first_strategies():
    steps = build_flow_step_sequence()
    enriched = enrich_windows_chrome_youtube_flow(steps)

    assert enriched[0].params["preferred_strategy"] == "open_app:windows_search"
    assert enriched[0].params.get("surface") == "windows_search"
    assert enriched[1].params["preferred_strategy"] == "select_profile:uia_text_match"
    assert enriched[2].params["preferred_strategy"] == "open_new_tab:hotkey_ctrl_t"
    assert enriched[3].params["preferred_strategy"] == "open_url:hotkey_ctrl_l"
    assert enriched[4].params["preferred_strategy"] == "search_youtube:dom_input"
    assert all(s.params.get("strategy_pack") == PACK_ID for s in enriched)


def test_router_selects_pack_and_excludes_coords_primary():
    steps = build_flow_step_sequence()
    for s in steps:
        s.params["fallback_strategies"] = [
            f"{s.kind}:relative_coords",
            f"{s.kind}:vision_coords",
        ]

    out = apply_strategy_packs(steps)
    assert out[0].params["preferred_strategy"] == "open_app:windows_search"
    assert not is_forbidden_primary_strategy(out[0].params["preferred_strategy"])
    for st in out:
        pref = str(st.params.get("preferred_strategy") or "")
        assert not is_forbidden_primary_strategy(pref), pref


def test_non_matching_flow_unchanged():
    foreign = [
        MissionStep(id="a", kind="open_app", params={"name": "notepad"}),
        MissionStep(id="b", kind="fill_form", params={"text": "x"}),
    ]
    assert matches_windows_chrome_youtube_flow(foreign) is False
    assert apply_strategy_packs(foreign) == foreign
