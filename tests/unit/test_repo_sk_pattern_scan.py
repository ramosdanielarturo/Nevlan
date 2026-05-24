"""Evita reaparición de material tipo OpenAI/sk- pegado en el árbol de app/."""
from __future__ import annotations

import re
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


_RE_SK = re.compile(r"\bsk-[a-zA-Z0-9_-]{14,}")
_RE_ASSIGN_KEY = re.compile(
    r"(api[_-]?key|openai[_-]?api[_-]?key)\s*=\s*['\"][^'\"]{12,}",
    re.I,
)


def test_critical_tree_has_no_live_api_material():
    root = _repo_root()
    hits: list[tuple[str, int]] = []
    for p in sorted(root.glob("app/**/*.py")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(txt.splitlines(), start=1):
            if "_RE_" in line or "re.compile" in line and "sk-" in line:
                continue
            if _RE_SK.search(line):
                hits.append((f"{p.relative_to(root)}:{i}", ))
    assert not hits, hits


def test_main_stays_without_key_assignment():
    main_py = _repo_root() / "main.py"
    txt = main_py.read_text(encoding="utf-8")
    assert _RE_ASSIGN_KEY.search(txt) is None
