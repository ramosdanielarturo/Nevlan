"""
Nevlan — Screen Element Reader / Visual Target Identity Engine
================================================================
PRD 2026-05-10f — sub-componente del Recorder Truth Layer.

Problema raíz que resuelve:

  Cuando un usuario hace click sobre una **tarjeta visible** que
  contiene textos (p. ej. una card del Chrome profile picker con
  ``Daniel Chrome`` / ``Daniel Arturo Ramos``), Nevlan recibía:

    * UIA = ``GroupControl`` con ``name=""`` (Chrome envuelve la card
      con un wrapper genérico).
    * OCR sobre un crop pequeño alrededor del pixel clickeado, que
      muchas veces NO contiene el texto principal de la card.

  Resultado: ``select_profile.profile_name = ""`` aunque la pantalla
  era totalmente legible. El sistema estaba identificando el **pixel**
  clickeado, no el **objeto visual** que recibió la acción.

Filosofía:

  Nevlan debe poder responder, por cada click:

    * ¿En qué objeto visual hizo clic el usuario?
    * ¿Qué texto contiene ese objeto?
    * ¿Qué cambió después?

  El ``Screen Element Reader`` cubre las dos primeras preguntas
  reuniendo la evidencia ya capturada por el recorder (UIA tree,
  OCR crops, anchors, web DOM si lo hay) y aplicando reglas
  **estructurales** — generales, NO Chrome-específicas — para:

    1. Localizar el contenedor visual real que envuelve el click.
    2. Leer el texto principal y secundario de ese contenedor.
    3. Reportar la confianza con la que lo logró.

Diseño:

  * **Pure-Python**, **sin side effects**, **sin disco**, **sin LLM**.
  * **No** llama OCR/UIA en runtime: consume lo que el recorder ya
    persistió en ``RawEvent.metadata`` (``vision_data`` /
    ``desktop_uia`` / ``web_data``).
  * Si la evidencia no permite leer el contenedor → marca
    ``evidence_level="insufficient"`` en lugar de inventar un texto.

Tipos visuales reconocidos (estructural, no por nombre de app):

  * ``card``       — bbox medio (≈100²-360k px²) + 2+ líneas de texto.
  * ``button``     — bbox pequeño/medio + 1 línea corta + control type
                     ``Button``/``MenuItem``/``CheckBox``.
  * ``link``       — control type ``Hyperlink`` con texto corto.
  * ``row``        — bbox horizontal extenso (anchura ≫ altura) +
                     1-2 líneas + control type ``ListItem``/``DataItem``.
  * ``cell``       — control type ``DataItem`` con bbox pequeño.
  * ``input``      — control type ``Edit``/``Combo`` con placeholder /
                     label asociado.
  * ``menu_item``  — control type ``MenuItem``.
  * ``""``         — no clasificable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import EventType, RawEvent


# ──────────────────────────────────────────────────────────────────────
# Constantes (estructurales, no listas de palabras)
# ──────────────────────────────────────────────────────────────────────

# Cualquier texto más largo que esto deja de ser una "etiqueta de UI"
# y pasa a ser potencial body text. Lo descartamos para primary_text.
_LABEL_MAX_CHARS = 80

# Cards típicas miden 80×80 a 600×600. Por debajo es icono / cell, por
# encima es contenido o pane.
_CARD_MIN_AREA_PX = 80 * 80
_CARD_MAX_AREA_PX = 600 * 600

# Filtros estructurales del texto (sin listas de palabras ni casos).
_DIGIT_HEAVY_RE = re.compile(r"\d{3,}")
_PUNCT_HEAVY_RE = re.compile(r"[\.;,\(\)\[\]\{\}]{3,}")

# Líneas de UI ruidosas que aparecen como overlay genérico de Chrome /
# Edge / Windows. Se filtran como secundarias, NUNCA primarias.
# (Es estructural: tokens de 1-2 letras o frases de control que el OCR
# capta de iconitos de la chrome bar; no son nombres propios.)
_UI_NOISE_TOKENS = frozenset({
    "x", "+", "·", "-",
})


# ──────────────────────────────────────────────────────────────────────
# Contrato público
# ──────────────────────────────────────────────────────────────────────

@dataclass
class VisualTargetIdentity:
    """Identidad visual del objeto que recibió el click.

    Campos:

      * ``visual_type`` — ``"card" | "button" | "link" | "row" | "cell"
                           | "input" | "menu_item" | ""``.
      * ``primary_text`` — texto más prominente del contenedor (lo que
                           un humano llamaría "el nombre de la card").
      * ``secondary_text`` — segunda línea representativa, si la hay.
      * ``nearby_text`` — texto observado fuera del contenedor (anchors).
      * ``role`` — rol normalizado (UIA control_type o web role).
      * ``bbox`` — bbox del contenedor inferido.
      * ``click_inside_bbox`` — ``True`` si el click cayó dentro del bbox.
      * ``evidence_level`` — ``"strong" | "medium" | "weak" | "insufficient"``.
      * ``sources_used`` — qué pistas alimentaron la identidad.
    """

    visual_type: str = ""
    primary_text: str = ""
    secondary_text: str = ""
    nearby_text: str = ""
    role: str = ""
    bbox: Dict[str, int] = field(default_factory=dict)
    click_inside_bbox: bool = False
    evidence_level: str = "insufficient"
    sources_used: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "visual_type": self.visual_type,
            "primary_text": self.primary_text,
            "secondary_text": self.secondary_text,
            "nearby_text": self.nearby_text,
            "role": self.role,
            "bbox": dict(self.bbox),
            "click_inside_bbox": bool(self.click_inside_bbox),
            "evidence_level": self.evidence_level,
            "sources_used": list(self.sources_used),
        }


# ──────────────────────────────────────────────────────────────────────
# Helpers — extracción cruda
# ──────────────────────────────────────────────────────────────────────

def _meta(ev: RawEvent) -> Dict[str, Any]:
    return dict(ev.metadata or {})


def _vision(ev: RawEvent) -> Dict[str, Any]:
    m = _meta(ev)
    v = m.get("vision_data") or m.get("vision") or {}
    return dict(v) if isinstance(v, dict) else {}


def _uia(ev: RawEvent) -> Dict[str, Any]:
    m = _meta(ev)
    u = (
        m.get("desktop_uia")
        or m.get("uia_data")
        or m.get("uia")
        or {}
    )
    return dict(u) if isinstance(u, dict) else {}


def _web(ev: RawEvent) -> Dict[str, Any]:
    m = _meta(ev)
    w = m.get("web_data") or m.get("web") or {}
    return dict(w) if isinstance(w, dict) else {}


def _click_xy(ev: RawEvent) -> Optional[Tuple[int, int]]:
    if ev.mouse_action is None:
        return None
    try:
        return (int(ev.mouse_action.x), int(ev.mouse_action.y))
    except Exception:
        return None


def _bbox_dict(b: Any) -> Dict[str, int]:
    if not isinstance(b, dict):
        return {}
    out: Dict[str, int] = {}
    for k_dst, k_srcs in (
        ("x", ("x", "left")),
        ("y", ("y", "top")),
        ("width", ("width", "w")),
        ("height", ("height", "h")),
    ):
        for k_src in k_srcs:
            if k_src in b:
                try:
                    out[k_dst] = int(b[k_src])
                except Exception:
                    pass
                break
    return out


def is_click_inside_bbox(
    click_xy: Optional[Tuple[int, int]],
    bbox: Dict[str, int],
) -> bool:
    """¿El click cayó dentro del bbox? (general, no Chrome).

    Si falta cualquier dato → ``False`` (el caller debe interpretarlo
    como "no podemos demostrar que esté dentro").
    """
    if click_xy is None or not bbox:
        return False
    try:
        x, y = click_xy
        bx = int(bbox.get("x", 0))
        by = int(bbox.get("y", 0))
        bw = int(bbox.get("width", 0))
        bh = int(bbox.get("height", 0))
    except Exception:
        return False
    if bw <= 0 or bh <= 0:
        return False
    return bx <= x <= bx + bw and by <= y <= by + bh


def _bbox_area(bbox: Dict[str, int]) -> int:
    if not bbox:
        return 0
    try:
        return max(0, int(bbox.get("width", 0))) * max(0, int(bbox.get("height", 0)))
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────────────────
# Helpers — texto del contenedor
# ──────────────────────────────────────────────────────────────────────

def extract_text_lines(raw: Any) -> List[str]:
    """Devuelve líneas de texto razonables a partir de los formatos que
    el recorder publica en ``vision_data``.

    Acepta:

      * ``str`` con saltos de línea (``ocr_text_around``).
      * ``list[str]`` (``ocr_lines`` / ``visual_lines``).
      * ``list[dict]`` con clave ``text`` (``ocr_blocks``).

    Filtra líneas vacías, de un solo carácter de control, o solo
    dígitos. NO aplica listas negras de palabras: solo formas.
    """
    raw_lines: List[str] = []
    if raw is None:
        return raw_lines
    if isinstance(raw, str):
        for ln in raw.splitlines():
            raw_lines.append(ln)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                raw_lines.append(item)
            elif isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    raw_lines.append(t)

    out: List[str] = []
    seen: set = set()
    for ln in raw_lines:
        s = (ln or "").strip()
        if not s:
            continue
        if len(s) > _LABEL_MAX_CHARS:
            # Demasiado largo para ser una etiqueta; lo recortamos pero
            # NO lo descartamos: puede contener primary + secondary
            # juntos en un solo string OCR.
            s = s[:_LABEL_MAX_CHARS].rstrip()
        if s.lower() in _UI_NOISE_TOKENS:
            continue
        if _DIGIT_HEAVY_RE.search(s):
            continue
        if _PUNCT_HEAVY_RE.search(s):
            continue
        # Splittear si vienen varias "líneas" pegadas con doble espacio.
        for piece in re.split(r"\s{2,}|\u2502|\|{2,}|\t+", s):
            p = piece.strip()
            if not p:
                continue
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def _looks_like_label(s: str) -> bool:
    """Forma de etiqueta de UI: 1-6 tokens, mayúsculas iniciales,
    sin párrafos. Estructural — no lista palabras concretas.
    """
    if not s:
        return False
    if len(s) > _LABEL_MAX_CHARS:
        return False
    tokens = [t for t in re.split(r"\s+", s.strip()) if t]
    if not (1 <= len(tokens) <= 6):
        return False
    cap_tokens = sum(1 for t in tokens if t[:1].isupper())
    return cap_tokens >= max(1, len(tokens) - 1)


def _pick_primary_secondary(lines: List[str]) -> Tuple[str, str]:
    """Selecciona ``(primary, secondary)`` de una lista de líneas.

    Heurística general (no específica a una app):

      * Primary: la primera línea con forma de etiqueta. Si no hay,
        la línea más corta válida.
      * Secondary: la siguiente línea distinta (forma de etiqueta o
        forma de "nombre completo" — 2-4 tokens, alfabético).
      * Si no hay líneas → ``("", "")``.
    """
    if not lines:
        return "", ""

    primary = ""
    for ln in lines:
        if _looks_like_label(ln):
            primary = ln
            break
    if not primary:
        # fallback: la línea más corta sin ruido.
        primary = min(lines, key=len)

    secondary = ""
    for ln in lines:
        if ln == primary:
            continue
        if _looks_like_label(ln):
            secondary = ln
            break
    if not secondary:
        for ln in lines:
            if ln == primary:
                continue
            tokens = [t for t in ln.split() if t]
            if 2 <= len(tokens) <= 5 and all(
                t[:1].isupper() and t.replace("-", "").isalpha()
                for t in tokens
            ):
                secondary = ln
                break

    return primary.strip(), secondary.strip()


# ──────────────────────────────────────────────────────────────────────
# Clasificación de visual_type
# ──────────────────────────────────────────────────────────────────────

def classify_visual_type(
    *,
    role: str,
    bbox: Dict[str, int],
    text_line_count: int,
) -> str:
    """Decide visual_type a partir de control_type + bbox + #líneas.

    Regla general (NO específica a Chrome):

      * Hyperlink + texto corto         → ``link``.
      * Button / CheckBox / RadioButton → ``button``.
      * MenuItem                        → ``menu_item``.
      * Edit / Combo / Document         → ``input``.
      * DataItem / Cell                 → ``cell``.
      * ListItem / TreeItem             → ``row``.
      * Group / Pane / Custom con bbox de card y ≥2 líneas → ``card``.
      * Group / Pane con 1 línea        → ``button`` (best-effort).
      * Otherwise                       → ``""``.
    """
    r = (role or "").strip().lower()
    area = _bbox_area(bbox)
    if "hyperlink" in r or "link" in r:
        return "link"
    if "button" in r or "checkbox" in r or "radiobutton" in r:
        return "button"
    if "menuitem" in r or "menu_item" in r:
        return "menu_item"
    if "edit" in r or "combo" in r or "spinner" in r or "textbox" in r:
        return "input"
    if "dataitem" in r or "cell" in r:
        return "cell"
    if "listitem" in r or "treeitem" in r or r == "row":
        return "row"
    # Genéricos: la card del profile picker llega con GroupControl /
    # PaneControl. Se necesita evidencia visual estructural.
    if "group" in r or "pane" in r or "custom" in r or r == "":
        if _CARD_MIN_AREA_PX <= area <= _CARD_MAX_AREA_PX and text_line_count >= 2:
            return "card"
        if _CARD_MIN_AREA_PX <= area <= _CARD_MAX_AREA_PX and text_line_count == 1:
            return "button"
    return ""


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────

def read_visual_target(ev: RawEvent) -> VisualTargetIdentity:
    """Reconstruye la identidad visual del objeto que recibió el click.

    Se aplica solo a clicks (single/double/right click). Para cualquier
    otro ``EventType`` retorna identidad vacía ``insufficient``.

    Estrategia (general, sin hardcode):

      1. Reúne UIA + web + vision del evento.
      2. Determina bbox del contenedor (``vision.anchor_bbox`` >
         ``uia.bbox`` > ``meta.target_bbox``).
      3. Extrae líneas de texto de ``vision_data.ocr_text_around``,
         ``ocr_lines``, ``ocr_blocks``, ``nearest_text_anchor``.
         Suma como secundarias el ``uia.name`` (si tiene forma de
         etiqueta) y los ``names`` de la cadena ascendente del UIA tree.
      4. Selecciona ``(primary_text, secondary_text)`` por forma.
      5. Clasifica ``visual_type`` usando control_type + bbox + #líneas.
      6. Calcula ``click_inside_bbox`` con coords del click.
      7. Asigna ``evidence_level``:

         * ``strong``    — visual_type clasificado + primary_text legible
                           + click dentro del bbox.
         * ``medium``    — primary_text legible (con o sin click_inside).
         * ``weak``      — bbox válido pero sin texto legible.
         * ``insufficient`` — sin bbox y sin texto.
    """
    if ev.event_type not in (
        EventType.MOUSE_CLICK,
        EventType.MOUSE_DOUBLE_CLICK,
        EventType.MOUSE_RIGHT_CLICK,
    ):
        return VisualTargetIdentity()

    uia = _uia(ev)
    web = _web(ev)
    vis = _vision(ev)
    meta = _meta(ev)

    # Sources
    used: List[str] = []

    # 1) Bbox del contenedor.
    bbox = _bbox_dict(
        vis.get("anchor_bbox")
        or uia.get("bbox")
        or meta.get("anchor_bbox")
        or meta.get("target_bbox")
        or {}
    )
    if bbox:
        used.append("bbox")

    # 2) Líneas de texto (texto del contenedor).
    container_lines: List[str] = []
    container_lines.extend(extract_text_lines(vis.get("ocr_lines")))
    container_lines.extend(extract_text_lines(vis.get("visual_lines")))
    container_lines.extend(extract_text_lines(vis.get("ocr_blocks")))
    container_lines.extend(extract_text_lines(vis.get("ocr_text_around")))
    if container_lines:
        used.append("ocr")

    # UIA name como pista cuando no es contenedor genérico vacío.
    uia_name = str(uia.get("name") or "").strip()
    if uia_name and _looks_like_label(uia_name):
        if uia_name.lower() not in [ln.lower() for ln in container_lines]:
            container_lines.insert(0, uia_name)
        if "uia.name" not in used:
            used.append("uia.name")

    # parent_chain del UIA: a veces el wrapper es genérico pero el padre
    # ya tiene un nombre legible (p. ej. "Profile A").
    pc = uia.get("parent_chain")
    if isinstance(pc, list):
        for parent in pc:
            if not isinstance(parent, dict):
                continue
            pn = str(parent.get("name") or "").strip()
            if pn and _looks_like_label(pn):
                if pn.lower() not in [ln.lower() for ln in container_lines]:
                    container_lines.append(pn)
                if "uia.parent_chain" not in used:
                    used.append("uia.parent_chain")

    # web.accessible_name / label como ancla extra.
    for k in ("accessible_name", "label", "placeholder"):
        v = str(web.get(k) or "").strip()
        if v and _looks_like_label(v):
            if v.lower() not in [ln.lower() for ln in container_lines]:
                container_lines.append(v)
            used.append(f"web.{k}")
            break

    # nearby_text (NO entra al contenedor — va aparte como contexto).
    nearby = ""
    nb_candidates = (
        vis.get("nearest_text_anchor"),
        web.get("nearby_text"),
        meta.get("ocr_anchor"),
    )
    for cand in nb_candidates:
        if isinstance(cand, str) and cand.strip():
            nearby = cand.strip()
            used.append("nearby_text")
            break

    primary, secondary = _pick_primary_secondary(container_lines)

    role = str(uia.get("control_type") or web.get("role") or "").strip()
    visual_type = classify_visual_type(
        role=role,
        bbox=bbox,
        text_line_count=len(container_lines),
    )

    inside = is_click_inside_bbox(_click_xy(ev), bbox)

    # Evidence level.
    has_bbox = bool(bbox)
    has_primary = bool(primary)

    if visual_type and has_primary and inside:
        evidence_level = "strong"
    elif has_primary and (visual_type or has_bbox):
        evidence_level = "medium"
    elif has_bbox and not has_primary:
        evidence_level = "weak"
    elif has_primary and not has_bbox:
        evidence_level = "weak"
    else:
        evidence_level = "insufficient"

    return VisualTargetIdentity(
        visual_type=visual_type,
        primary_text=primary,
        secondary_text=secondary,
        nearby_text=nearby,
        role=role,
        bbox=bbox,
        click_inside_bbox=inside,
        evidence_level=evidence_level,
        sources_used=used,
    )


__all__ = [
    "VisualTargetIdentity",
    "read_visual_target",
    "is_click_inside_bbox",
    "extract_text_lines",
    "classify_visual_type",
]
