"""Magic UX — copy mínimo para vista normal (sin ruido técnico)."""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional

from app.interfaces.desktop.mission_ui_status import compute_mission_ui_execution_state


class MagicUxPhase(str, Enum):
    EXECUTING = "executing"
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    ADAPTING = "adapting"
    UNSAFE = "unsafe"
    TERMINATED = "terminated"


MAGIC_PRIMARY_LABELS = {
    MagicUxPhase.EXECUTING: "▶ Ejecutando",
    MagicUxPhase.READY: "✅ Lista para ejecutar",
    MagicUxPhase.NEEDS_CLARIFICATION: "⚠ Necesita aclaración",
    MagicUxPhase.ADAPTING: "🔧 Adaptando automáticamente",
    MagicUxPhase.UNSAFE: "❌ No es seguro continuar",
    MagicUxPhase.TERMINATED: "✅ Terminado",
}


_TECH_NOISE_PATTERNS = (
    r"\bruntime_mode\b",
    r"\bGATE_[A-Z0-9_]+\b",
    r"\buia_text_match\b",
    r"\bsmart_route\b",
    r"\bclick_fallback\b",
    r"\bcapture_score\b",
    r"\bvalidators?\b",
    r"\bSEP\b",
    r"\bSIPE\b",
    r"\bcoords_fallback\b",
    r"\bstrategy_used\b",
    r"\breplay\b",
    r"\bexecution_truth\b",
    r"\bLOP\b",
    r"\bGOOR\b",
    r"\bARL\b",
    r"\bSLL\b",
    r"\bOMPC\b",
    r"\bUOOL\b",
    r"\bUOEC\b",
    r"\bOCRP\b",
    r"\bconvergence\b",
    r"\boscillation\b",
    r"\brepeatability\b",
    r"\brecovery_storm\b",
    r"\bnavigation_oscillation\b",
    r"\bUCES\b",
    r"\bERL\b",
    r"\bLONE\b",
    r"\borg_shadow\b",
    r"\borg_audit\b",
    r"\btw_[a-z0-9_]+\b",
    r"\b(ev|event)_\d{3,}\b",
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
)


def magic_ux_primary_label(phase: MagicUxPhase) -> str:
    return MAGIC_PRIMARY_LABELS.get(phase, "✅ Listo")


def resolve_magic_ux_phase_from_mission(mission: Any) -> MagicUxPhase:
    st = compute_mission_ui_execution_state(mission)
    if st.ui_bucket == "UNSAFE":
        return MagicUxPhase.UNSAFE
    if not st.is_executable:
        if st.visible_blockers:
            return MagicUxPhase.NEEDS_CLARIFICATION
        return MagicUxPhase.NEEDS_CLARIFICATION
    return MagicUxPhase.READY


def sanitize_magic_ux_runner_message(message: str, *, expert_mode: bool) -> str:
    """Elimina tokens técnicos en modo normal."""
    if expert_mode or not message:
        return message
    out = message
    for pat in _TECH_NOISE_PATTERNS:
        out = re.sub(pat, "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out or "Procesando…"


def magic_ux_execution_banner(
    mission: Optional[Any],
    *,
    phase: Optional[MagicUxPhase] = None,
    expert_mode: bool = False,
) -> str:
    """Banner único para overlays durante ejecución / revisión."""
    if phase is not None:
        base = magic_ux_primary_label(phase)
    elif mission is None:
        base = magic_ux_primary_label(MagicUxPhase.EXECUTING)
    else:
        base = magic_ux_primary_label(resolve_magic_ux_phase_from_mission(mission))
    if expert_mode and mission is not None:
        rep = getattr(mission, "one_shot_reliability_report", None) or {}
        uool = getattr(mission, "uool_orchestration_report", None) or {}
        mh = getattr(mission, "mission_health_snapshot", None) or {}
        bits = []
        if isinstance(rep, dict) and rep.get("runtime_mode"):
            bits.append(f"rt={rep.get('runtime_mode')}")
        if isinstance(uool, dict) and uool.get("dominant_strategy"):
            bits.append(f"uool={uool.get('dominant_strategy')}")
        if isinstance(mh, dict) and mh.get("mission_health_score") is not None:
            bits.append(f"h={mh['mission_health_score']}")
        if bits:
            return f"{base} · " + " ".join(bits)
    return base


__all__ = [
    "MAGIC_PRIMARY_LABELS",
    "MagicUxPhase",
    "magic_ux_execution_banner",
    "magic_ux_primary_label",
    "resolve_magic_ux_phase_from_mission",
    "sanitize_magic_ux_runner_message",
]
