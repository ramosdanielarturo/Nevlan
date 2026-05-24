"""
Migración semántica UIC — alias legacy → capacidades canónicas.

Idempotente: repetir sobre pasos ya ``search_content`` no hace nada.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _audit_append(blob: Dict[str, Any], entry: Dict[str, Any]) -> None:
    audits = blob.setdefault("uic_migration_audit", [])
    if not isinstance(audits, list):
        audits = []
        blob["uic_migration_audit"] = audits
    audits.append(entry)


def migrate_semantic_execution_plan_dict(blob: Optional[Dict[str, Any]]) -> bool:
    """Mutación in-place del blob ``semantic_execution_plan``.

    Returns:
        True si hubo cambios en ``steps`` o banderas.
    """
    if not isinstance(blob, dict):
        return False
    steps = blob.get("steps")
    if not isinstance(steps, list):
        return False

    changed = False

    now = datetime.now(timezone.utc).isoformat()

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        sid = str(step.get("id") or f"idx:{idx}")
        stype = str(step.get("type") or "").strip()

        if stype == "search_youtube":
            params = dict(step.get("params") or {})
            params.setdefault("provider", "youtube")
            params.setdefault("site", params.get("site") or "youtube")
            step["type"] = "search_content"
            step["params"] = params
            blob["uic_semantic_migrated"] = True
            entry = {
                "step_id": sid,
                "from": "search_youtube",
                "to": "search_content",
                "reason": "legacy_alias_canonicalization",
                "confidence": 1.0,
                "migrated_at": now,
            }
            _audit_append(blob, entry)
            changed = True

    return changed


def migrate_mission_semantic_plan_uic(mission: Any) -> bool:
    """Aplica migración al objeto misión si tiene SEP dict."""
    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict):
        return False
    return migrate_semantic_execution_plan_dict(sep)


__all__ = [
    "migrate_semantic_execution_plan_dict",
    "migrate_mission_semantic_plan_uic",
]
