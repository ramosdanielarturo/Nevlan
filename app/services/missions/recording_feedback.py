"""Builder centralizado de descripciones humanas para feedback en vivo
durante la grabación (FASE 1 §11 del PRD).

El overlay de grabación, el rerecord overlay y el post-recording review
deben pintar exactamente la misma descripción humana ("Clic en botón
'Buscar' · 88%", "Texto capturado: 'Chrome'", "Guardar (Ctrl+S)", ...).
Para evitar lógica duplicada y divergente entre pantallas, todo el
mapping ``RawEvent + TargetSignature + FieldSession → texto humano``
vive aquí.

Diseño:
  - Función pura ``build_recording_feedback(...)``.
  - No importa PyQt, no importa pyautogui, no escribe al disco.
  - Retorna ``RecordingFeedback`` con lo necesario para el overlay
    (texto humano, icono, calidad, score, detalles experto).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

from app.contracts.mission import (
    RawEvent, EventType, MouseAction, KeyboardAction,
)
from app.services.missions.field_commit import (
    is_browser_internal_url,
)


# ──────────────────────────────────────────────────────────────────
# Tipos
# ──────────────────────────────────────────────────────────────────

QualityLevel = str   # "good" | "medium" | "weak"

ICON_CLICK = "🖱"
ICON_DBL = "🖱🖱"
ICON_RIGHT = "🖱▸"
ICON_TYPE = "⌨"
ICON_FIELD = "📝"
ICON_HOTKEY = "⌨"
ICON_SAVE = "💾"
ICON_COPY = "📋"
ICON_PASTE = "📋"
ICON_CUT = "✂"
ICON_FIND = "🔎"
ICON_SCROLL = "🖲"
ICON_DRAG = "✋"


@dataclass
class RecordingFeedback:
    """Resultado renderizable del builder."""

    label_es: str
    label_en: str = ""
    icon: str = ""
    confidence_score: Optional[float] = None  # 0–100
    quality_level: QualityLevel = ""          # good/medium/weak/""
    expert_details: List[str] = field(default_factory=list)
    timestamp: Optional[datetime] = None
    event_type: str = ""
    extras: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Texto principal (locale es por defecto)."""
        return self.label_es

    def to_overlay_payload(self) -> Dict[str, Any]:
        """Forma compatible con el bus ``mission.live_step``."""
        return {
            "text": self.label_es,
            "icon": self.icon,
            "quality": self.quality_level or None,
            "confidence_score": self.confidence_score,
            "expert_details": list(self.expert_details),
            "event_type": self.event_type,
        }


# ──────────────────────────────────────────────────────────────────
# Helpers de calidad
# ──────────────────────────────────────────────────────────────────

def _quality_from_score(score: Optional[float]) -> QualityLevel:
    if score is None:
        return ""
    if score >= 75:
        return "good"
    if score >= 50:
        return "medium"
    return "weak"


def _quality_from_signals(
    *,
    uia: Optional[Dict[str, Any]],
    web: Optional[Dict[str, Any]],
    has_visual: bool,
    has_coords: bool,
) -> Tuple[QualityLevel, Optional[float]]:
    """Heurística cuando no tenemos un score pre-calculado.

    Devuelve (quality, score). El score es estimado, NO sustituye al
    cálculo real del compilador (``capture_score``).
    """
    score = 0.0
    uia = uia or {}
    web = web or {}
    aid = (uia.get("automation_id") or "").strip()
    name = (uia.get("name") or "").strip()
    ct = (uia.get("control_type") or "").strip()
    test_id = (web.get("test_id") or "").strip()

    if test_id:
        score += 35
    if aid:
        score += 30
    if name and ct:
        score += 18
    elif name:
        score += 10
    if web.get("locators"):
        score += 12
    if has_visual:
        score += 12
    if has_coords:
        score += 6

    score = max(0.0, min(100.0, score))
    return _quality_from_score(score), score


# ──────────────────────────────────────────────────────────────────
# Hotkeys legibles
# ──────────────────────────────────────────────────────────────────

_HOTKEY_LABELS_ES: Dict[Tuple[str, ...], Tuple[str, str]] = {
    ("ctrl", "s"): ("Guardar (Ctrl+S)", ICON_SAVE),
    ("ctrl", "c"): ("Copiar (Ctrl+C)", ICON_COPY),
    ("ctrl", "v"): ("Pegar (Ctrl+V)", ICON_PASTE),
    ("ctrl", "x"): ("Cortar (Ctrl+X)", ICON_CUT),
    ("ctrl", "z"): ("Deshacer (Ctrl+Z)", ICON_HOTKEY),
    ("ctrl", "y"): ("Rehacer (Ctrl+Y)", ICON_HOTKEY),
    ("ctrl", "a"): ("Seleccionar todo (Ctrl+A)", ICON_HOTKEY),
    ("ctrl", "f"): ("Buscar (Ctrl+F)", ICON_FIND),
    ("ctrl", "l"): ("Barra de direcciones (Ctrl+L)", ICON_HOTKEY),
    ("ctrl", "t"): ("Nueva pestaña (Ctrl+T)", ICON_HOTKEY),
    ("ctrl", "w"): ("Cerrar pestaña (Ctrl+W)", ICON_HOTKEY),
    ("alt", "tab"): ("Cambiar ventana (Alt+Tab)", ICON_HOTKEY),
    ("alt", "f4"): ("Cerrar ventana (Alt+F4)", ICON_HOTKEY),
    ("win", "d"): ("Mostrar escritorio (Win+D)", ICON_HOTKEY),
}

_SPECIAL_KEY_LABELS_ES = {
    "tab": "Tabular",
    "enter": "Confirmar (Enter)",
    "esc": "Cancelar (Esc)",
    "escape": "Cancelar (Esc)",
    "space": "Espacio",
    "backspace": "Borrar",
    "delete": "Eliminar",
    "up": "Flecha arriba",
    "down": "Flecha abajo",
    "left": "Flecha izquierda",
    "right": "Flecha derecha",
    "pageup": "Página arriba",
    "pagedown": "Página abajo",
}


def _canon_key(k: str) -> str:
    if not k:
        return ""
    s = (k or "").strip().lower()
    if s.startswith("key."):
        s = s[4:]
    aliases = {
        "ctrl_l": "ctrl", "ctrl_r": "ctrl",
        "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
        "shift_l": "shift", "shift_r": "shift",
        "cmd": "win", "cmd_l": "win", "cmd_r": "win",
        "escape": "esc", "return": "enter",
    }
    return aliases.get(s, s)


def _hotkey_label(modifiers: List[str], key: str, repeat: int = 1) -> Tuple[str, str]:
    mods = tuple(sorted(_canon_key(m) for m in modifiers or []))
    k = _canon_key(key)
    label, icon = _HOTKEY_LABELS_ES.get((*mods, k), ("", ICON_HOTKEY))
    if not label:
        combo = "+".join(
            [m.capitalize() for m in mods] + [k.upper() if k else ""]
        ).strip("+")
        label = combo or "Tecla"
    if repeat > 1:
        label = f"{label} × {repeat}"
    return label, icon


# ──────────────────────────────────────────────────────────────────
# Texto contextual para clicks (cuando no hay UIA name)
# ──────────────────────────────────────────────────────────────────

def _color_hint_from_image_path(path: Optional[str]) -> str:
    """Hint MUY simple basado en el path de la imagen.

    No abrimos la imagen aquí (recorder ya hace muchísimo trabajo en el
    hilo crítico). Sólo respondemos un hint genérico tipo "elemento".
    El builder de target_signature ya almacena el blob completo.
    """
    if not path:
        return ""
    return "elemento"


def _process_friendly_name(process_name: Optional[str]) -> str:
    if not process_name:
        return ""
    p = process_name.lower()
    table = {
        "chrome.exe": "Chrome",
        "msedge.exe": "Edge",
        "firefox.exe": "Firefox",
        "brave.exe": "Brave",
        "excel.exe": "Excel",
        "winword.exe": "Word",
        "powerpnt.exe": "PowerPoint",
        "outlook.exe": "Outlook",
        "explorer.exe": "Explorador",
        "notepad.exe": "Bloc de notas",
        "code.exe": "VS Code",
        "spotify.exe": "Spotify",
    }
    return table.get(p, "")


# ──────────────────────────────────────────────────────────────────
# Click feedback
# ──────────────────────────────────────────────────────────────────

def _click_label(
    *,
    raw_event: RawEvent,
    target_signature: Optional[Dict[str, Any]],
) -> Tuple[str, QualityLevel, Optional[float], List[str]]:
    md = raw_event.metadata or {}
    uia = md.get("uia") or {}
    web = md.get("web") or {}
    name = (uia.get("name") or "").strip()
    aid = (uia.get("automation_id") or "").strip()
    ct = (uia.get("control_type") or "").strip()
    web_name = (web.get("accessible_name") or web.get("label")
                or web.get("placeholder") or "").strip()
    near = (web.get("nearby_text") or md.get("ocr_anchor") or "").strip()
    proc_friendly = _process_friendly_name(
        getattr(raw_event.window_context, "process_name", None)
    )
    img = raw_event.snapshot_ref or md.get("snapshot_mid")

    # Calidad / score: usa la firma si existe.
    score: Optional[float] = None
    quality: QualityLevel = ""
    if isinstance(target_signature, dict):
        conf = target_signature.get("confidence") or {}
        try:
            score = float(conf.get("confidence_score"))
        except Exception:
            score = None
        ql = (conf.get("target_quality") or "").lower()
        if ql in ("green", "yellow", "red"):
            quality = {"green": "good", "yellow": "medium", "red": "weak"}[ql]
    if quality == "":
        quality, score_est = _quality_from_signals(
            uia=uia, web=web,
            has_visual=bool(img),
            has_coords=bool(raw_event.mouse_action),
        )
        if score is None:
            score = score_est

    expert: List[str] = []
    ma = raw_event.mouse_action
    if ma:
        expert.append(f"x={ma.x}, y={ma.y}, button={ma.button}")
    if uia:
        expert.append(
            f"uia: name='{name}' aid='{aid}' ct='{ct}'"
        )
    if web:
        expert.append(
            f"web: locators={len(web.get('locators') or [])} "
            f"role='{(web.get('role') or '').strip()}'"
        )
    if proc_friendly:
        expert.append(f"process={proc_friendly}")

    # Prioridad de descripción humana.
    role_es = {
        "ButtonControl": "botón", "Button": "botón",
        "EditControl": "campo", "Edit": "campo",
        "HyperlinkControl": "enlace", "Hyperlink": "enlace",
        "TextControl": "texto", "Text": "texto",
        "ListItemControl": "elemento", "ListItem": "elemento",
        "MenuItemControl": "opción", "MenuItem": "opción",
        "TabItemControl": "pestaña", "TabItem": "pestaña",
    }.get(ct, "")

    label = ""
    if name:
        if role_es:
            label = f"Clic en {role_es} '{name}'"
        else:
            label = f"Clic en '{name}'"
    elif aid:
        label = f"Clic en '{aid}'"
    elif web_name:
        label = f"Clic en '{web_name}'"
    elif near:
        label = f"Clic cerca de '{near}'"
    elif proc_friendly:
        label = f"Clic en {proc_friendly}"
    elif img:
        label = f"Clic en {_color_hint_from_image_path(img)}"
    else:
        label = "Clic"

    if ma:
        label = f"{label} · X={ma.x}, Y={ma.y}"
    if score is not None:
        label = f"{label} · {int(round(score))}%"

    return label, quality, score, expert


# ──────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────

def build_recording_feedback(
    *,
    raw_event: Optional[RawEvent] = None,
    target_signature: Optional[Dict[str, Any]] = None,
    field_session: Optional[Dict[str, Any]] = None,
    quality: Optional[QualityLevel] = None,
) -> RecordingFeedback:
    """Mapea un evento crudo a una descripción humana lista para overlay.

    ``field_session`` es opcional y describe una Field Editing Session
    abierta o ya cerrada. Estructura::

        {
            "phase": "open" | "writing" | "committed",
            "label": "Buscar",
            "value": "Daniel Ramos",       # phase == committed
            "url_internal": False,         # bandera de sanity
        }
    """
    fb = RecordingFeedback(
        label_es="",
        label_en="",
        icon="",
        confidence_score=None,
        quality_level=quality or "",
        timestamp=datetime.now(timezone.utc),
    )

    # ── Field session manda sobre cualquier otra cosa ──────────
    if field_session and isinstance(field_session, dict):
        phase = (field_session.get("phase") or "").lower()
        label = (field_session.get("label") or "").strip()
        if phase == "open":
            fb.icon = ICON_FIELD
            fb.label_es = (
                f"Editando campo '{label}'…" if label
                else "Editando campo…"
            )
            fb.label_en = (
                f"Editing field '{label}'…" if label
                else "Editing field…"
            )
            fb.event_type = "field_session.open"
            fb.quality_level = fb.quality_level or "medium"
            return fb
        if phase == "writing":
            buf = (field_session.get("buffer") or "")
            short = buf if len(buf) <= 40 else buf[:40] + "…"
            fb.icon = ICON_TYPE
            fb.label_es = f"Texto capturado: '{short}'"
            fb.label_en = f"Captured text: '{short}'"
            fb.event_type = "field_session.writing"
            return fb
        if phase == "committed":
            value = (field_session.get("value") or "")
            short = value if len(value) <= 60 else value[:60] + "…"
            url_internal = bool(field_session.get("url_internal"))
            if url_internal or is_browser_internal_url(value):
                fb.icon = ICON_FIELD
                fb.label_es = (
                    f"Captura débil: el navegador devolvió '{short}'."
                )
                fb.label_en = (
                    f"Weak capture: browser returned '{short}'."
                )
                fb.quality_level = "weak"
                fb.event_type = "field_session.committed_url"
                return fb
            fb.icon = ICON_FIELD
            fb.label_es = (
                f"Rellenar {label or 'campo'} con '{short}'"
                if value else f"Editado {label or 'campo'}"
            )
            fb.label_en = (
                f"Fill {label or 'field'} with '{short}'"
                if value else f"Edited {label or 'field'}"
            )
            fb.event_type = "field_session.committed"
            return fb

    if raw_event is None:
        return fb

    et = raw_event.event_type
    fb.event_type = et.value if hasattr(et, "value") else str(et)

    # Si el caller no nos pasó la firma, intentar leerla del propio evento.
    if target_signature is None:
        ts = (raw_event.metadata or {}).get("target_signature")
        if isinstance(ts, dict):
            target_signature = ts

    # ── Click / dbl / right ──
    if et in (
        EventType.MOUSE_CLICK,
        EventType.MOUSE_DOUBLE_CLICK,
        EventType.MOUSE_RIGHT_CLICK,
    ):
        text, q, score, expert = _click_label(
            raw_event=raw_event, target_signature=target_signature,
        )
        if et == EventType.MOUSE_DOUBLE_CLICK:
            text = text.replace("Clic", "Doble clic", 1)
            fb.icon = ICON_DBL
        elif et == EventType.MOUSE_RIGHT_CLICK:
            text = text.replace("Clic", "Clic derecho", 1)
            fb.icon = ICON_RIGHT
        else:
            fb.icon = ICON_CLICK
        fb.label_es = text
        fb.confidence_score = score
        fb.quality_level = quality or q
        fb.expert_details = expert
        return fb

    # ── Drag / Scroll ──
    if et == EventType.MOUSE_DRAG:
        da = raw_event.drag_action
        fb.icon = ICON_DRAG
        if da:
            fb.label_es = (
                f"Arrastrar de ({da.start_x},{da.start_y}) "
                f"a ({da.end_x},{da.end_y})"
            )
            fb.expert_details = [
                f"duration_s={da.duration:.2f}"
            ]
        else:
            fb.label_es = "Arrastrar elemento"
        return fb

    if et == EventType.MOUSE_SCROLL:
        meta = (raw_event.metadata or {}).get("scroll") or {}
        dy = int(meta.get("dy") or 0)
        fb.icon = ICON_SCROLL
        if dy < 0:
            fb.label_es = "Scroll hacia abajo"
        elif dy > 0:
            fb.label_es = "Scroll hacia arriba"
        else:
            fb.label_es = "Scroll lateral"
        fb.expert_details = [f"dx={int(meta.get('dx') or 0)}, dy={dy}"]
        return fb

    # ── Teclado ──
    if et == EventType.KEYBOARD_KEY_PRESS and raw_event.keyboard_action:
        ka: KeyboardAction = raw_event.keyboard_action
        keys = list(ka.keys or [])
        mods = list(ka.modifiers or [])
        first = (keys[0] if keys else "")
        # Guard: CapsLock / NumLock / ScrollLock NUNCA se muestran como
        # paso visible al usuario. Bug 2026-05-03: aparecía
        # "Presionar CAPSLOCK" entre fragmentos de texto. El listener
        # nuevo ya no los emite, pero defendemos en profundidad.
        ck_first = _canon_key(first)
        if ck_first in ("capslock", "caps_lock", "numlock", "num_lock",
                         "scrolllock", "scroll_lock"):
            fb.icon = ""
            fb.label_es = ""
            fb.event_type = "absorbed_toggle"
            return fb
        non_shift = [m for m in mods if m != "shift"]
        if non_shift:
            label, icon = _hotkey_label(non_shift, first)
            fb.label_es = label
            fb.icon = icon
            fb.event_type = "hotkey"
            return fb
        if ck_first in _SPECIAL_KEY_LABELS_ES:
            fb.label_es = f"Presionar {_SPECIAL_KEY_LABELS_ES[ck_first]}"
            fb.icon = ICON_HOTKEY
            return fb

    if et == EventType.KEYBOARD_TYPE_TEXT:
        md = raw_event.metadata or {}
        if md.get("field_session"):
            value = str(md.get("final_text") or "")
            label = str(md.get("field_label") or "")
            short = value if len(value) <= 60 else value[:60] + "…"
            fb.icon = ICON_FIELD
            if label:
                fb.label_es = f"Rellenar '{label}' con '{short}'"
                fb.label_en = f"Fill '{label}' with '{short}'"
            else:
                fb.label_es = f"Texto capturado: '{short}'"
                fb.label_en = f"Captured text: '{short}'"
            fc = md.get("field_commit") or {}
            if fc:
                fb.expert_details = [
                    f"read_source={md.get('committed_read_source','')}",
                    f"reason={fc.get('reason','')}",
                    f"source_is_url={fc.get('source_is_url')}",
                    f"source_is_input_value={fc.get('source_is_input_value')}",
                ]
            return fb

    # Fallback genérico — siempre devolvemos algo.
    fb.icon = "🔹"
    fb.label_es = (
        f"Evento {fb.event_type}"
        if fb.event_type else "Evento"
    )
    return fb


__all__ = [
    "RecordingFeedback",
    "build_recording_feedback",
]
