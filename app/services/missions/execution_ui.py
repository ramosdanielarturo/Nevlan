"""Textos amigables para ejecución (modo normal vs experto)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def friendly_resolver_summary(
    resolver: str,
    premium_meta: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """(línea modo normal, línea modo experto corta)."""
    r = (resolver or "").lower()
    pm = premium_meta if isinstance(premium_meta, dict) else {}
    score = pm.get("visual_match_score")
    reg = pm.get("region_used")
    img = pm.get("image_used")
    fb = pm.get("fallback_used")
    off = pm.get("offset_applied")

    parts_ex = []
    if score is not None:
        try:
            parts_ex.append(f"score={float(score):.2f}")
        except (TypeError, ValueError):
            parts_ex.append(f"score={score}")
    if reg:
        parts_ex.append(f"región={reg}")
    if img:
        parts_ex.append(f"img={img}")
    if fb:
        parts_ex.append(f"fallback={fb}")
    if off:
        parts_ex.append(f"offset={off}")

    expert = " · ".join(parts_ex) if parts_ex else (resolver or "—")

    if "coordinate_icon" in r or "icon_validated" in r:
        normal = "Encontrado por ícono + posición"
    elif "web_locator" in r or r == "web":
        normal = "Encontrado por elemento (web)"
    elif "uia" in r or r in ("uia_control",):
        normal = "Encontrado por elemento (escritorio)"
    elif "coords_fallback" in r or r.endswith("absolute_point") or r == "coords_fallback":
        normal = "Usó coordenada de respaldo"
    elif "visual" in r or "relative_point" in r:
        normal = "Coincidencia visual / posición relativa"
    elif r in ("", "none", "unknown"):
        normal = "Preparando paso…"
    else:
        normal = f"Resuelto ({resolver or '—'})"

    return normal, expert


def last_run_badge_from_runtime_stats(rt: Dict[str, Any]) -> str:
    """Una línea breve para tarjetas cuando hay historial de replay."""
    if not isinstance(rt, dict) or int(rt.get("runs") or 0) < 1:
        return ""
    lr = str(rt.get("last_resolver") or "")
    if "coordinate_icon" in lr.lower():
        return "Última ejec.: ícono + posición"
    if "coords_fallback" in lr.lower() or "absolute" in lr.lower():
        return "Última ejec.: coordenadas de respaldo"
    if "uia" in lr.lower() or "web" in lr.lower():
        return "Última ejec.: elemento localizado"
    return "Última ejec.: registrada"


def step_phase_label(action_strategy: str) -> str:
    a = (action_strategy or "").lower()
    if a in ("click", "double_click", "right_click", "drag_drop"):
        return "Buscando elemento…"
    if a in ("type_text", "set_field_value"):
        return "Escribiendo…"
    if a == "send_hotkey":
        return "Enviando atajo…"
    if a.startswith("wait"):
        return "Esperando estado…"
    if "validate" in a or a.startswith("scroll"):
        return "Ejecutando paso…"
    return "Ejecutando…"

