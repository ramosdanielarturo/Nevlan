"""Field commit payload — valores finales tras Field Editing Session (grabador).

El compilador espera RawEvent(KEYBOARD_TYPE_TEXT) con metadata field_session_*.

``FieldCommitResult`` documenta de forma explícita por qué eligió un valor:
si lo escribió el usuario, si fue una lectura de DOM/UIA, si lo descartó
por ser una URL interna del navegador (chrome://...), etc. Eso permite a
las pruebas y al overlay distinguir intención humana de "estado de la app".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.contracts.mission import RawEvent, EventType


# Prefijos de URLs internas de navegadores. Si la lectura UIA/DOM devuelve
# uno de estos valores Y el usuario NO los tipeó, casi seguro es
# contaminación del navegador (barra de direcciones del profile picker,
# settings, etc.) y NO el campo que el usuario editó.
BROWSER_INTERNAL_URL_PREFIXES = (
    "chrome://", "edge://", "about:", "chrome-extension://",
    "moz-extension://", "brave://", "opera://", "vivaldi://",
    "file:///", "view-source:", "devtools://",
)

# Prefijos de URLs externas (web normal). Estas SÍ pueden ser texto
# legítimo escrito por el usuario en una barra de direcciones — solo
# son sospechosas si el usuario NO las escribió (p.ej. ``page.url``
# leyéndose como valor del campo de búsqueda en YouTube). El caller
# decide cuándo aplicar este filtro: usar ``looks_like_external_url``
# junto con un check del buffer del usuario.
EXTERNAL_URL_PREFIXES = (
    "https://", "http://", "ftp://", "ftps://",
    "ws://", "wss://", "data:",
)


def is_browser_internal_url(value: Optional[str]) -> bool:
    """True si ``value`` empieza con uno de los prefijos de URL interna."""
    if not value:
        return False
    v = str(value).strip().lower()
    return any(v.startswith(p) for p in BROWSER_INTERNAL_URL_PREFIXES)


def looks_like_external_url(value: Optional[str]) -> bool:
    """True si ``value`` parece una URL externa (https://...).

    A diferencia de ``is_browser_internal_url``, una URL externa PUEDE
    ser texto legítimo (el usuario escribió la URL en la barra de
    direcciones). El caller decide si la trata como contaminación
    cruzando contra el buffer del usuario.
    """
    if not value:
        return False
    v = str(value).strip().lower()
    return any(v.startswith(p) for p in EXTERNAL_URL_PREFIXES)


def is_url_contamination(
    *,
    read_value: Optional[str],
    user_typed: Optional[str],
) -> bool:
    """True si ``read_value`` parece una URL (interna o externa) que
    NO fue tipeada por el usuario — clásico caso de contaminación
    donde el DOM/UIA estaba leyendo ``page.url`` en vez del campo.

    Casos cubiertos:
      - ``chrome://profile-picker/`` con buffer "Chrome" → contamina
      - ``https://www.youtube.com/`` con buffer "devuelveme..." → contamina
      - ``https://www.youtube.com/`` con buffer vacío (click en icono
        sin escribir nada) → contamina
      - ``https://...`` con buffer "https://..." → NO contamina
        (el usuario sí escribió la URL)
    """
    if not read_value:
        return False
    rv = str(read_value).strip()
    if not rv:
        return False
    looks_url = is_browser_internal_url(rv) or looks_like_external_url(rv)
    if not looks_url:
        return False
    user_clean = (user_typed or "").strip()
    # El usuario no tipeó nada, o tipeó algo distinto a la URL → contamina.
    if not user_clean:
        return True
    return user_clean.lower() != rv.lower()


# Etiquetas de fuente para FieldCommitResult.read_source / chosen_final_text.
READ_SOURCE_UIA = "uia"
READ_SOURCE_DOM = "dom"
READ_SOURCE_LAST_KNOWN = "last_known"
READ_SOURCE_KEYBOARD = "reconstructed_keyboard"
READ_SOURCE_KEYBOARD_URL_SANITIZED = "reconstructed_keyboard_url_sanitized"
READ_SOURCE_NONE = "none"


@dataclass
class FieldCommitResult:
    """Resultado de una Field Editing Session.

    Diccionario completo con suficiente información para que el compilador
    y los tests distingan:

      - lo que el usuario tipeó ``user_typed_text``
      - lo que se leyó del campo ``final_field_value`` (UIA/DOM)
      - cuál fue el valor elegido como verdad ``chosen_final_text``
      - banderas semánticas: si la fuente parece URL interna, estado de
        app o un valor de input legítimo
      - una razón legible (``reason``) útil para logs y la UI experta
    """

    final_text: str
    label: str
    # Histórico: uia | dom | last_known | reconstructed_keyboard | none
    read_source: str
    initial_value: Optional[str]
    anchor_uia: Dict[str, Any]
    anchor_web: Dict[str, Any]
    window_context: Optional[Any]
    anchor_snapshot_ref: Optional[str] = None

    # ── Metadata enriquecida (Fase 1, sección 2.6) ────────────────────
    user_typed_text: str = ""
    final_field_value: Optional[str] = None
    source_is_url: bool = False
    source_is_app_state: bool = False
    source_is_input_value: bool = False
    chosen_final_text: str = ""
    reason: str = ""

    def to_metadata(self) -> Dict[str, Any]:
        """Serializa los campos extra para incrustar en RawEvent.metadata."""
        return {
            "user_typed_text": self.user_typed_text,
            "final_field_value": self.final_field_value,
            "source_is_url": bool(self.source_is_url),
            "source_is_app_state": bool(self.source_is_app_state),
            "source_is_input_value": bool(self.source_is_input_value),
            "chosen_final_text": self.chosen_final_text,
            "reason": self.reason,
        }


def build_keyboard_type_text_event(
    *,
    fc: FieldCommitResult,
    timestamp=None,
    snapshot_ref: Optional[str] = None,
) -> RawEvent:
    """Un único paso compilable SET_FIELD_VALUE / TYPE_TEXT (valor humano final)."""
    from datetime import datetime, timezone

    snap = snapshot_ref or fc.anchor_snapshot_ref
    meta: Dict[str, Any] = {
        "field_session": True,
        "final_text": fc.final_text,
        "field_label": fc.label,
        "committed_read_source": fc.read_source,
        "initial_value_snapshot": fc.initial_value,
        # Metadata enriquecida: deja explícita la decisión del recorder.
        "field_commit": fc.to_metadata(),
    }
    if fc.anchor_uia:
        meta["uia"] = fc.anchor_uia
    if fc.anchor_web:
        meta["web"] = fc.anchor_web

    return RawEvent(
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        keyboard_action=None,
        window_context=fc.window_context,
        timestamp=timestamp or datetime.now(timezone.utc),
        metadata=meta,
        snapshot_ref=snap,
    )
