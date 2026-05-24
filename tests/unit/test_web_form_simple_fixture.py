"""Tests fixture web_form_simple + URL local del formulario FASE 7."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from tests.fixtures.fase7_missions.web_form_simple import (
    build_web_form_simple_mission,
    fase7_simple_form_url,
)


def test_fase7_simple_form_url_is_local_file():
    url = fase7_simple_form_url()
    assert url.lower().startswith("file:")
    raw = unquote(urlparse(url).path)
    if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
        raw = raw[1:]
    path = Path(raw)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert 'name="name"' in text
    assert 'name="email"' in text
    assert 'name="message"' in text
    assert 'type="submit"' in text


def test_web_form_mission_uses_local_form_url():
    mission = build_web_form_simple_mission()
    plan = mission.semantic_execution_plan or {}
    steps = plan.get("steps") or []
    open_step = next(s for s in steps if s.get("type") == "open_url")
    assert str(open_step.get("params", {}).get("url", "")).lower().startswith("file:")
    fill_steps = [s for s in steps if s.get("type") == "fill_form"]
    assert len(fill_steps) == 3
    assert fill_steps[0]["params"]["field"] == "name"
