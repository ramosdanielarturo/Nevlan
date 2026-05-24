"""FASE 7 — Chrome → perfil → pestaña → YouTube → búsqueda → scroll."""

from __future__ import annotations

from app.contracts.mission import Mission

from tests.fixtures.golden_missions.canonical_search_chrome_youtube_flow import (
    build_golden_search_chrome_youtube_mission,
)


def build_chrome_youtube_launcher_mission(*, name: str = "fase7-chrome-youtube") -> Mission:
    return build_golden_search_chrome_youtube_mission(name=name)


__all__ = ["build_chrome_youtube_launcher_mission"]
