"""
Nevlan — Noise Audit (PRD 2026-05-08c §F)
==========================================

Detecta y clasifica el "ruido humano" que aparece en una grabación
real pero **no debe** convertirse en un paso ejecutable. Los casos
soportados (todos observados en ``mission_1778297133``):

  * ``address_bar_click_no_submit`` — el usuario hizo clic en la
    barra de direcciones del navegador y no llegó a enviar nada.
  * ``typed_then_deleted`` — escribió algunas letras en un campo y
    luego las borró completamente antes de continuar.
  * ``noise_before_bookmark`` — visitó/intentó visitar una URL
    intermedia que después fue corregida abriendo otra (favorito,
    en este caso YouTube). La primera tentativa no produjo ningún
    paso ejecutable.

Reglas:

  * La auditoría es **read-only** sobre la misión: nunca toca
    ``raw_trace`` ni ``semantic_execution_plan``.
  * Cada entrada describe el evento en lenguaje humano (modo
    experto) sin filtrar identificadores técnicos sensibles.
  * Vista normal del review: la lista NO se muestra.
  * Vista experto del review: la lista aparece en la sección
    "Ruido descartado".

API pública:

  * :func:`audit_ignored_noise(mission)` — devuelve una lista de
    dicts serializables (uno por hallazgo).
  * :data:`IGNORED_NOISE_KINDS` — set de kinds reconocidos.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.contracts.mission import Mission


# Catálogo cerrado de kinds reconocidos (para tests y UI).
IGNORED_NOISE_KINDS = frozenset({
    "address_bar_click_no_submit",
    "typed_then_deleted",
    "noise_before_bookmark",
})


def _safe_session_dict(session: Any) -> Dict[str, Any]:
    """Convierte un ``FieldSession`` (o dict) a dict serializable."""
    if isinstance(session, dict):
        return session
    try:
        return session.as_dict()
    except Exception:
        return {}


def _detect_address_bar_clicks(sessions: List[Any]) -> List[Dict[str, Any]]:
    """Cualquier sesión sobre la barra de direcciones que NO se haya
    enviado (sin Enter / sin nav posterior) cuenta como ruido."""
    out: List[Dict[str, Any]] = []
    for s in sessions:
        d = _safe_session_dict(s)
        field_label = str(d.get("field_label") or "")
        submitted = bool(d.get("submitted", False))
        final_text = str(d.get("final_text") or "")
        if field_label != "browser_address_bar":
            continue
        if submitted:
            continue
        # No submitted: el usuario clicó / escribió en la barra y
        # luego abandonó (volvió a la app o abrió otro tab/url).
        out.append({
            "kind": "address_bar_click_no_submit",
            "human_summary": (
                "Click accidental en la barra de direcciones del "
                "navegador (no se envió ninguna URL/búsqueda)."
            ),
            "evidence": {
                "field_label": field_label,
                "final_text_preview": final_text[:32],
                "events_consumed": int(d.get("events_consumed") or 0),
                "site": d.get("site"),
            },
            "raw_event_ids": list(getattr(s, "raw_event_ids", []) or []),
        })
    return out


def _detect_typed_then_deleted(sessions: List[Any]) -> List[Dict[str, Any]]:
    """Sesiones cuya forma final es vacía o que terminaron sin
    enviarse y sin texto útil son texto temporal."""
    out: List[Dict[str, Any]] = []
    for s in sessions:
        d = _safe_session_dict(s)
        submitted = bool(d.get("submitted", False))
        if submitted:
            continue
        final_text = str(d.get("final_text") or "").strip()
        events_consumed = int(d.get("events_consumed") or 0)
        # Necesitamos al menos algunos eventos consumidos (>= 2)
        # para considerar que hubo "tipeo + borrado", de lo
        # contrario una sesión simplemente vacía no es ruido
        # significativo.
        if events_consumed < 2:
            continue
        if final_text:
            # Hubo texto residual ⇒ no es "borrado todo". Solo
            # consideramos ruido si el texto residual es 1-2
            # caracteres (claramente accidental).
            if len(final_text) > 2:
                continue
        # Address bar ya cubierto por la otra heurística — evitamos
        # doble entrada.
        if d.get("field_label") == "browser_address_bar":
            continue
        out.append({
            "kind": "typed_then_deleted",
            "human_summary": (
                "El usuario escribió algunas letras en un campo y "
                "las borró antes de continuar."
            ),
            "evidence": {
                "field_label": d.get("field_label"),
                "final_text_preview": final_text[:32],
                "events_consumed": events_consumed,
                "site": d.get("site"),
            },
            "raw_event_ids": list(getattr(s, "raw_event_ids", []) or []),
        })
    return out


def _detect_noise_before_bookmark(
    sessions: List[Any], mission: Mission,
) -> List[Dict[str, Any]]:
    """Si la misión abrió una URL via favorito (``open_url`` con
    ``via='bookmark'``) Y antes hubo un click no-enviado en la barra
    de direcciones de la misma ventana, ese click cuenta como
    "navegación corregida" y se reporta como ruido específico (más
    informativo que ``address_bar_click_no_submit`` solo).
    """
    plan = mission.semantic_execution_plan or {}
    has_bookmark_open_url = False
    for sp in (plan.get("steps") or []):
        if sp.get("type") == "open_url":
            via = (sp.get("params") or {}).get("via", "")
            if str(via).lower() == "bookmark":
                has_bookmark_open_url = True
                break
    if not has_bookmark_open_url:
        return []

    out: List[Dict[str, Any]] = []
    for s in sessions:
        d = _safe_session_dict(s)
        if d.get("field_label") != "browser_address_bar":
            continue
        if bool(d.get("submitted", False)):
            continue
        out.append({
            "kind": "noise_before_bookmark",
            "human_summary": (
                "El usuario empezó a escribir en la barra y luego "
                "abrió la URL desde un favorito; lo anterior se "
                "ignora como navegación corregida."
            ),
            "evidence": {
                "field_label": "browser_address_bar",
                "final_text_preview": str(d.get("final_text") or "")[:32],
                "events_consumed": int(d.get("events_consumed") or 0),
            },
            "raw_event_ids": list(getattr(s, "raw_event_ids", []) or []),
        })
    return out


def audit_ignored_noise(mission: Mission) -> List[Dict[str, Any]]:
    """Auditoría completa de ruido humano para una misión.

    Devuelve una lista cronológica donde cada entrada tiene:

      * ``kind``: uno de :data:`IGNORED_NOISE_KINDS`.
      * ``human_summary``: descripción legible (sin tecnicismos).
      * ``evidence``: dict con metadatos para vista experto.
      * ``raw_event_ids``: ids de eventos del raw_trace asociados.

    Si la misión no tiene ``raw_trace`` o las sesiones no se pueden
    construir, devuelve lista vacía. Tolera errores de cualquier
    nivel (es 100% best-effort).
    """
    raw_trace = list(mission.raw_trace or [])
    if not raw_trace:
        return []
    try:
        from app.services.missions.field_session_manager import (
            build_field_sessions,
        )
        sessions = build_field_sessions(raw_trace)
    except Exception:
        return []

    findings: List[Dict[str, Any]] = []
    seen_event_keys: set = set()

    def _push(items: List[Dict[str, Any]]) -> None:
        for item in items:
            key = (item.get("kind"), tuple(item.get("raw_event_ids") or ()))
            if key in seen_event_keys:
                continue
            seen_event_keys.add(key)
            findings.append(item)

    # noise_before_bookmark se detecta primero porque es más
    # específico que address_bar_click_no_submit y no queremos
    # duplicar el mismo session bajo dos kinds.
    _push(_detect_noise_before_bookmark(sessions, mission))
    if not findings:
        _push(_detect_address_bar_clicks(sessions))
    _push(_detect_typed_then_deleted(sessions))
    return findings


__all__ = [
    "IGNORED_NOISE_KINDS",
    "audit_ignored_noise",
]
