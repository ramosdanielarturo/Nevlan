"""
Strategy pack router — applies flow-specific packs when intent matches.
"""
from __future__ import annotations

from typing import Any, List, Optional

from app.services.missions.execution_contracts import MissionStep

__all__ = ["apply_strategy_packs"]


def apply_strategy_packs(
    steps: List[MissionStep],
    mission: Optional[Any] = None,
) -> List[MissionStep]:
    """Enrich ``steps`` with the first matching strategy pack."""
    if not steps:
        return steps

    from app.services.missions.strategy_packs.windows_chrome_youtube import (
        enrich_windows_chrome_youtube_flow,
        matches_windows_chrome_youtube_flow,
    )

    if matches_windows_chrome_youtube_flow(steps, mission=mission):
        return enrich_windows_chrome_youtube_flow(steps)

    return steps
