"""
Strategy pack: Windows Search → Chrome → Profile → New tab → YouTube → Search
-------------------------------------------------------------------------------
Identity-first strategies with verification hooks — no coords as primary.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

from app.services.missions.execution_contracts import MissionStep

__all__ = [
    "PACK_ID",
    "matches_windows_chrome_youtube_flow",
    "enrich_windows_chrome_youtube_flow",
]

PACK_ID = "windows_chrome_youtube"

_FLOW_SIGNATURE: Tuple[Tuple[str, Optional[str]], ...] = (
    ("open_app", "chrome"),
    ("select_profile", None),
    ("open_new_tab", None),
    ("open_url", None),
    ("search_youtube", None),
)

_SEARCH_ALTERNATES = frozenset({
    "search_youtube",
    "search_content",
    "search_site",
})

_STEP_STRATEGIES = {
    "open_app": (
        "open_app:windows_search",
        ("open_app:os_startfile", "open_app:start_command"),
        {"surface": "windows_search", "method": "windows_search"},
    ),
    "select_profile": (
        "select_profile:uia_text_match",
        (
            "select_profile:visible_text_ocr",
            "select_profile:chrome_profile_alias",
            "select_profile:skip_if_loaded",
        ),
        {"surface": "chrome_profile_picker"},
    ),
    "open_new_tab": (
        "open_new_tab:hotkey_ctrl_t",
        ("open_new_tab:uia_button",),
        {},
    ),
    "open_url": (
        "open_url:hotkey_ctrl_l",
        ("open_url:playwright_goto", "open_url:bookmark_uia"),
        {"alias": "youtube", "url": "https://www.youtube.com"},
    ),
    "search_youtube": (
        "search_youtube:dom_input",
        (
            "search_youtube:dom_search_input",
            "search_youtube:uia_searchbox",
            "search_youtube:ocr_buscar",
            "search_youtube:keyboard_focus_then_type",
        ),
        {"site": "youtube", "target": "youtube_search_box", "provider": "youtube"},
    ),
    "search_content": (
        "search_content:uol_surface",
        (
            "search_youtube:dom_input",
            "search_youtube:uia_searchbox",
            "search_youtube:ocr_buscar",
        ),
        {"site": "youtube", "target": "youtube_search_box", "provider": "youtube"},
    ),
}


def matches_windows_chrome_youtube_flow(
    steps: Sequence[MissionStep],
    *,
    mission: Optional[Any] = None,
) -> bool:
    """True when step kinds match the canonical Windows→Chrome→YouTube flow."""
    if len(steps) < 4:
        return False

    kinds = [str(s.kind or "") for s in steps]
    if mission is not None:
        meta = getattr(mission, "metadata", None) or {}
        if isinstance(meta, dict) and meta.get("strategy_pack") == PACK_ID:
            return True
        name = str(getattr(mission, "name", "") or "").lower()
        if "chrome" in name and "youtube" in name:
            return True

    if kinds[0] != "open_app":
        return False
    app = str((steps[0].params or {}).get("name") or (steps[0].params or {}).get("app") or "").lower()
    if app and "chrome" not in app:
        return False

    if "select_profile" not in kinds and "use_selected_profile" not in kinds:
        return False
    if "open_new_tab" not in kinds:
        return False

    has_youtube_nav = any(
        k == "open_url"
        and "youtube" in str((s.params or {}).get("alias") or (s.params or {}).get("url") or "").lower()
        for k, s in zip(kinds, steps)
    )
    has_search = any(k in _SEARCH_ALTERNATES for k in kinds)
    return has_youtube_nav and has_search


def enrich_windows_chrome_youtube_flow(steps: List[MissionStep]) -> List[MissionStep]:
    """Apply identity-first strategies for the Windows→Chrome→YouTube flow."""
    out: List[MissionStep] = []
    for step in steps:
        kind = str(step.kind or "")
        cfg = _STEP_STRATEGIES.get(kind)
        if kind in _SEARCH_ALTERNATES and cfg is None:
            cfg = _STEP_STRATEGIES.get("search_youtube")
        if cfg is None:
            out.append(step)
            continue

        preferred, fallbacks, extra_params = cfg
        params = dict(step.params or {})
        params.update(extra_params)
        params["preferred_strategy"] = preferred
        params["fallback_strategies"] = list(fallbacks)
        params["strategy_pack"] = PACK_ID
        params.setdefault("identity_first", True)

        out.append(
            MissionStep(
                id=step.id,
                kind=step.kind,
                params=params,
                human_label=step.human_label,
            )
        )
    return out


def build_flow_step_sequence() -> List[MissionStep]:
    """Canonical step list for tests (mocked surfaces)."""
    return [
        MissionStep(
            id="s1",
            kind="open_app",
            params={"name": "chrome", "method": "windows_search"},
            human_label="Abrir Chrome",
        ),
        MissionStep(
            id="s2",
            kind="select_profile",
            params={"profile_name": "Daniel Arturo Ramos"},
            human_label="Seleccionar perfil",
        ),
        MissionStep(
            id="s3",
            kind="open_new_tab",
            params={},
            human_label="Nueva pestaña",
        ),
        MissionStep(
            id="s4",
            kind="open_url",
            params={"alias": "youtube", "url": "https://www.youtube.com"},
            human_label="Ir a YouTube",
        ),
        MissionStep(
            id="s5",
            kind="search_youtube",
            params={"query": "Devuélveme el amor de Luis Miguel"},
            human_label="Buscar en YouTube",
        ),
    ]
