"""Asegura que el entrypoint no contiene claves en claro."""
from __future__ import annotations

import re
from pathlib import Path


def test_main_py_has_no_hardcoded_key_material():
    root = Path(__file__).resolve().parents[2]
    text = (root / "main.py").read_text(encoding="utf-8")
    assert not re.search(r"\bsk-[a-zA-Z0-9_-]{12,}", text)
    assert 'api_key="' not in text.replace(" ", "").lower()


def test_bootstrap_llm_env_module_loads():
    import app.bootstrap_llm_env as blm

    assert hasattr(blm, "bootstrap_llm_environment")
    assert hasattr(blm, "ensure_cloud_llm_configured")
