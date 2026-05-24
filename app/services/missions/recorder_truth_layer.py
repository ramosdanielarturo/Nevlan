"""
Nevlan — Recorder Truth Layer (PRD 2026-05-10c)
================================================

Capa intermedia entre ``raw_trace`` y el ``intent_collapse_engine``
que enriquece cada acción cruda con la **verdad estructural** de lo
que el usuario hizo:

  * ¿qué elemento recibió la acción? (UIA / web / OCR / bbox)
  * ¿qué tipo de control era? (HyperlinkControl, ButtonControl,
    GroupControl, PaneControl…)
  * ¿qué tan grande era el bbox? (área en px²)
  * ¿el "name" se parece a una etiqueta de UI o a contenido web?
  * ¿qué nivel de evidencia hay para inferir intención?
  * ¿con qué tipos semánticos es **estructuralmente** compatible
    este click? (un click sobre un párrafo de Banxico no puede ser
    un ``select_profile``, sin importar el título de la ventana.)

Diseño deliberado: las reglas son **estructurales**, no listas
hardcodeadas de palabras prohibidas. Una etiqueta de perfil (1-3
tokens, mayúsculas, sin dígitos) y una oración de contenido web
(>6 tokens, conectores en minúscula, fechas, números) tienen formas
distintas y eso es lo que la capa explota.

API pública:

  * :class:`RecordedTruthEvent`           — dataclass del contrato.
  * :func:`evaluate_event(raw_event)`     — produce un evento de verdad.
  * :func:`build_truth_layer(raw_trace)`  — produce la lista completa.
  * :func:`is_compatible_profile_picker_click(raw_event)`
                                          — guard estructural usado
                                            por el intent_collapse_engine
                                            antes de aceptar un click
                                            como evidencia de
                                            ``select_profile``.

Esta capa NO ejecuta nada, NO toca disco, NO llama LLM y NO muta sus
entradas. Es 100% pura.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import EventType, RawEvent
from app.services.missions.screen_element_reader import (
    VisualTargetIdentity,
    read_visual_target,
)


# ──────────────────────────────────────────────────────────────────────
# Constantes estructurales (no son listas de palabras prohibidas; son
# formas tipográficas / ergonómicas que distinguen UI de body content).
# ──────────────────────────────────────────────────────────────────────

# Bbox de un elemento de UI clickable "típico": cards de profile picker,
# botones, items de menú, links de cabecera. Se queda holgado para
# tolerar layouts grandes (4K, listas verticales) sin perder señal.
# Un párrafo / pane / contenedor de página supera fácilmente esta área.
_LARGE_BBOX_AREA_PX = 360_000  # ≈ 600x600

# Palabras "conectoras" en español/inglés. La presencia de varias
# minúsculas dentro de un name ⇒ frase natural, no etiqueta de UI.
# (Detectar conectores genéricos NO es hardcode de un caso: cualquier
# idioma natural los tiene.)
_CONNECTORS = frozenset({
    # ES
    "de", "del", "la", "el", "los", "las", "y", "o", "u", "para",
    "por", "en", "con", "sin", "al", "a", "que", "se", "lo",
    "un", "una", "unos", "unas", "su", "sus", "es", "son",
    # EN
    "of", "the", "a", "an", "and", "or", "for", "with", "to",
    "in", "on", "at", "is", "are", "be", "by", "from", "as",
})

# Control types que son contenedores semánticos genéricos (no representan
# un elemento clickable concreto). Un click cuyo target sea uno de estos
# ⇒ identidad débil; el resolver tendría que adivinar.
_GENERIC_CONTAINER_CONTROL_TYPES = frozenset({
    "groupcontrol",
    "panecontrol",
    "documentcontrol",
    "windowcontrol",
    "customcontrol",
})

# Control types que NUNCA son selectores de perfil. Un profile picker
# nunca aparece como "HyperlinkControl con párrafo de body content"
# — los selectores reales son botones / items / imágenes / texto corto.
_PROFILE_PICKER_INCOMPATIBLE_CONTROL_TYPES = frozenset({
    # Hyperlinks largos suelen ser body content (titulares, breadcrumbs).
    # Hyperlinks CORTOS sí podrían ser un picker — por eso la
    # incompatibilidad se cruza con tamaño de bbox y forma del name
    # más abajo.
})

# Hints de URL que confirman ventana = profile picker (Chrome/Edge).
_PROFILE_PICKER_URL_HINTS = (
    "chrome://profile-picker",
    "chrome://chrome-signin",
    "edge://profile-picker",
)

# Frases en el title de la ventana que confirman que SÍ es un profile
# picker real (multi-idioma, Chrome/Edge). Si el title contiene una de
# estas, NO aplicamos el guard estructural: la grabación a veces
# captura un UIA pobre (GroupControl wrapping de la card) y aún así
# ese click es válido como evidencia de profile picker.
_PROFILE_PICKER_STRONG_TITLE_HINTS = (
    "quién eres",
    "who are you",
    "elige tu perfil",
    "choose a profile",
    "select profile",
    "profile picker",
)

_DATE_LIKE_RE = re.compile(r"\b20\d{2}\b|\b\d{1,2}/\d{1,2}\b")
_DIGIT_RUN_RE = re.compile(r"\d{2,}")


# ──────────────────────────────────────────────────────────────────────
# Helpers estructurales — públicos para tests
# ──────────────────────────────────────────────────────────────────────

def bbox_area(bbox: Any) -> int:
    """Área en px² del bbox; 0 si no es un dict válido."""
    if not isinstance(bbox, dict):
        return 0
    try:
        w = int(bbox.get("width", bbox.get("w", 0)) or 0)
        h = int(bbox.get("height", bbox.get("h", 0)) or 0)
        return max(0, w) * max(0, h)
    except Exception:
        return 0


def is_large_bbox(bbox: Any) -> bool:
    """¿El bbox es demasiado grande para representar un elemento UI
    discreto (botón / card / item de lista)?

    Un picker de Chrome con DPI 200% sigue mostrando cards de ~250x250.
    Pasar 600x600 (~360k px²) ya es contenido / pane.
    """
    return bbox_area(bbox) > _LARGE_BBOX_AREA_PX


def looks_like_web_content_text(name: Optional[str]) -> bool:
    """¿El ``name`` UIA se parece a contenido web (oración natural,
    titular, párrafo) en lugar de a una etiqueta de UI corta?

    Reglas estructurales (sin hardcode de texto):

      1. Longitud > 40 chars Y ≥ 6 tokens.
      2. Presencia de conectores en minúscula entre tokens (delata
         lenguaje natural, no nombre propio o etiqueta de UI).
      3. O contiene fechas (``20XX``, ``DD/MM``) o secuencias de
         dígitos (titulares, IDs, fechas de publicación).

    No usa listas negras de palabras concretas — solo forma.
    """
    if not name:
        return False
    s = str(name).strip()
    if not s:
        return False

    # Heurística rápida: dígitos típicos de body content.
    if _DATE_LIKE_RE.search(s):
        return True
    if _DIGIT_RUN_RE.search(s):
        # Solo si además hay varios tokens — un nombre como "Pablo3"
        # no debe disparar este flag.
        if len(s.split()) >= 4:
            return True

    if len(s) <= 40:
        return False
    tokens = [t for t in re.split(r"\s+", s) if t]
    if len(tokens) < 6:
        return False
    # Conectores intermedios ⇒ oración natural.
    lowered = [t.strip(",.;:¡!¿?()[]{}\"'").lower() for t in tokens]
    connector_count = sum(1 for t in lowered if t in _CONNECTORS)
    return connector_count >= 2


def looks_like_profile_label(name: Optional[str]) -> bool:
    """¿``name`` se parece a una etiqueta de profile picker?

    Profile picker labels son:
      * 1-4 tokens.
      * Cada token comienza con mayúscula y es alfabético.
      * Sin dígitos, sin signos largos.
      * Longitud total ≤ 40 chars.

    No es una lista de nombres permitidos — describe la **forma**
    de un nombre propio / etiqueta corta de UI.
    """
    if not name:
        return False
    s = str(name).strip()
    if not s or len(s) > 40:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    tokens = [t for t in re.split(r"\s+", s) if t]
    if not (1 <= len(tokens) <= 4):
        return False
    for tok in tokens:
        clean = tok.replace("-", "").replace("'", "").replace("´", "")
        if not clean:
            return False
        if not clean[0].isupper():
            return False
        if not clean.isalpha():
            return False
    return True


# ──────────────────────────────────────────────────────────────────────
# Contrato del evento enriquecido
# ──────────────────────────────────────────────────────────────────────

EventKind = str  # "click" | "type_text" | "key_press" | "scroll" | "drag" | "wait" | "window_change"

EvidenceLevel = str  # "strong" | "medium" | "weak" | "insufficient"

_EVIDENCE_RANK = {
    "strong": 3,
    "medium": 2,
    "weak": 1,
    "insufficient": 0,
}


@dataclass
class RecordedTruthEvent:
    """Evento enriquecido y verificable derivado de un :class:`RawEvent`.

    Cada campo es un derivado puro del raw event y de su contexto. La
    capa NO inventa información: si el raw event no trae UIA, los
    campos correspondientes quedan vacíos y el ``evidence_level``
    cae a ``weak``/``insufficient`` para que el motor lo trate como
    incertidumbre real, no como confianza falsa.
    """

    event_id: str
    raw_event_id: str
    event_kind: EventKind
    sequence_id: int = 0
    # Identidad del target (si aplica al kind).
    target_identity: Dict[str, Any] = field(default_factory=dict)
    # Contexto observable (ventana, proceso, URL si la hay).
    context: Dict[str, Any] = field(default_factory=dict)
    # Outcome observado (cambios visibles tras la acción) — solo se
    # rellena cuando el caller pasó eventos posteriores.
    outcome: Dict[str, Any] = field(default_factory=dict)
    # Clasificación: con qué tipos semánticos es compatible y cuán
    # confiable es la inferencia.
    classification: Dict[str, Any] = field(default_factory=dict)

    @property
    def evidence_level(self) -> EvidenceLevel:
        return str(self.classification.get("evidence_level") or "insufficient")

    @property
    def compatible_semantic_types(self) -> List[str]:
        return list(self.classification.get("compatible_with") or [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "raw_event_id": self.raw_event_id,
            "event_kind": self.event_kind,
            "sequence_id": self.sequence_id,
            "target_identity": dict(self.target_identity),
            "context": dict(self.context),
            "outcome": dict(self.outcome),
            "classification": dict(self.classification),
        }


# ──────────────────────────────────────────────────────────────────────
# Extracción de target identity
# ──────────────────────────────────────────────────────────────────────

def _uia_from_event(ev: RawEvent) -> Dict[str, Any]:
    meta = ev.metadata or {}
    uia = meta.get("uia") or meta.get("uia_data") or meta.get("desktop_uia")
    return dict(uia) if isinstance(uia, dict) else {}


def _web_from_event(ev: RawEvent) -> Dict[str, Any]:
    meta = ev.metadata or {}
    web = meta.get("web") or meta.get("web_data")
    return dict(web) if isinstance(web, dict) else {}


def _bbox_from_event(ev: RawEvent) -> Dict[str, Any]:
    meta = ev.metadata or {}
    if isinstance(meta.get("anchor_bbox"), dict):
        return dict(meta["anchor_bbox"])
    uia = _uia_from_event(ev)
    if isinstance(uia.get("bbox"), dict):
        return dict(uia["bbox"])
    if isinstance(meta.get("target_bbox"), dict):
        return dict(meta["target_bbox"])
    return {}


def _kind_of(ev: RawEvent) -> EventKind:
    et = ev.event_type
    if et in (EventType.MOUSE_CLICK, EventType.MOUSE_DOUBLE_CLICK):
        return "click"
    if et == EventType.MOUSE_RIGHT_CLICK:
        return "click"
    if et == EventType.MOUSE_DRAG:
        return "drag"
    if et == EventType.MOUSE_SCROLL:
        return "scroll"
    if et == EventType.KEYBOARD_TYPE_TEXT:
        return "type_text"
    if et in (EventType.KEYBOARD_HOTKEY, EventType.KEYBOARD_KEY_PRESS):
        return "key_press"
    if et == EventType.WINDOW_ACTIVATE:
        return "window_change"
    return "wait"


def _build_target_identity(ev: RawEvent) -> Dict[str, Any]:
    uia = _uia_from_event(ev)
    web = _web_from_event(ev)
    bbox = _bbox_from_event(ev)
    name = str(uia.get("name") or web.get("accessible_name") or "").strip()
    control_type = str(uia.get("control_type") or "").strip()
    automation_id = str(uia.get("automation_id") or "").strip()
    role = str(web.get("role") or uia.get("control_type") or "").strip()
    locators = web.get("locators") if isinstance(web, dict) else None
    domain = ""
    url = str(web.get("url") or "").strip() if isinstance(web, dict) else ""
    if url and "//" in url:
        try:
            domain = url.split("//", 1)[1].split("/", 1)[0].lower()
        except Exception:
            domain = ""

    area = bbox_area(bbox)
    large = is_large_bbox(bbox)
    web_content = looks_like_web_content_text(name)
    profile_label = looks_like_profile_label(name)
    generic_container = control_type.lower() in _GENERIC_CONTAINER_CONTROL_TYPES

    # Screen Element Reader (PRD 2026-05-10f §A): si el target inmediato
    # es genérico (GroupControl con name vacío), el reader intenta
    # recuperar la verdad del contenedor visual (card / button / link…)
    # leyendo OCR + parent_chain + nearby anchors. Es estructural (no
    # depende de la app). Quien decida la intención (ICE) puede
    # consultar este bloque para no perder el texto de la card.
    visual: VisualTargetIdentity = read_visual_target(ev)

    return {
        "name": name,
        "name_length": len(name),
        "control_type": control_type,
        "automation_id": automation_id,
        "role": role,
        "bbox": bbox,
        "bbox_area": area,
        "is_large_bbox": large,
        "is_web_content_text": web_content,
        "looks_like_profile_label": profile_label,
        "is_generic_container": generic_container,
        "has_uia": bool(uia),
        "has_web_locators": bool(locators) if isinstance(locators, list) else False,
        "domain": domain,
        "url": url,
        "visual": visual.to_dict(),
    }


def _build_context_block(ev: RawEvent) -> Dict[str, Any]:
    wc = ev.window_context
    if wc is None:
        return {}
    title = str(wc.title or "")
    proc = str(wc.process_name or "")
    web = _web_from_event(ev)
    url = str(web.get("url") or "").strip() if isinstance(web, dict) else ""
    is_picker_url = bool(url) and any(
        h in url.lower() for h in _PROFILE_PICKER_URL_HINTS
    )
    return {
        "window_title": title,
        "process_name": proc,
        "hwnd": wc.hwnd,
        "url": url,
        "profile_picker_url": is_picker_url,
    }


# ──────────────────────────────────────────────────────────────────────
# Clasificación: evidence_level + compatible_with
# ──────────────────────────────────────────────────────────────────────

def _classify(
    kind: EventKind,
    target: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    reasons: List[str] = []
    compatible: List[str] = []
    incompatible: List[str] = []

    has_uia = bool(target.get("has_uia"))
    name = str(target.get("name") or "")
    ct = str(target.get("control_type") or "").lower()
    aid = str(target.get("automation_id") or "")
    large = bool(target.get("is_large_bbox"))
    web_content = bool(target.get("is_web_content_text"))
    generic = bool(target.get("is_generic_container"))
    profile_label = bool(target.get("looks_like_profile_label"))
    has_locators = bool(target.get("has_web_locators"))
    proc = str(context.get("process_name") or "").lower()

    # ── Identity strength ────────────────────────────────────────
    if kind == "click":
        # Screen Element Reader (PRD 2026-05-10f): el reader puede
        # rescatar la identidad del contenedor visual cuando UIA
        # devolvió un wrapper genérico vacío. Lo leemos antes de
        # bajarle score al click.
        visual = target.get("visual") or {}
        visual_type = str(visual.get("visual_type") or "").strip()
        visual_primary = str(visual.get("primary_text") or "").strip()
        visual_secondary = str(visual.get("secondary_text") or "").strip()
        visual_evidence = str(visual.get("evidence_level") or "insufficient")
        visual_inside = bool(visual.get("click_inside_bbox"))

        score = 0
        if aid:
            score += 2
            reasons.append("uia.automation_id")
        if name and not web_content:
            score += 2 if profile_label or len(name) <= 40 else 1
            reasons.append("uia.name")
        if has_locators:
            score += 2
            reasons.append("web.locators")
        if not large and target.get("bbox_area"):
            score += 1
            reasons.append("bbox.discrete")
        if generic:
            score -= 1
            reasons.append("uia.generic_container")
        if web_content:
            score -= 1
            reasons.append("uia.web_content_text")
        if large:
            score -= 1
            reasons.append("bbox.large")
        if not has_uia and not has_locators:
            score -= 2
            reasons.append("no_structured_target")

        # Recovery: contenedor visual legible compensa parcialmente al
        # UIA pobre. PRD 2026-05-10g (Outcome Intelligence): el rescate
        # visual SOLO sube hasta "medium" por sí mismo. Para llegar a
        # "strong" necesitamos outcome confirmado en una segunda pasada
        # (``apply_outcome_to_classification``). Esto evita que "leí
        # texto cerca → ya sé qué es" se traduzca en alta confianza.
        if visual_type and visual_primary and visual_inside:
            score += 1
            reasons.append(f"visual.{visual_type}_with_primary_text")
            if visual_secondary:
                score += 1
                reasons.append("visual.secondary_text")

        if score >= 3:
            evidence = "strong"
        elif score >= 1:
            evidence = "medium"
        elif score >= 0:
            evidence = "weak"
        else:
            evidence = "insufficient"

        # ── Estructural: ¿es compatible con select_profile? ─────
        # Reglas (todas deben pasar):
        #   * el target tiene una etiqueta corta (no contenido web).
        #   * el bbox es discreto (no un pane).
        #   * el control type NO es contenedor genérico, A MENOS QUE
        #     el Screen Element Reader haya identificado un visual
        #     "card"/"button"/... legible dentro del bbox (caso típico
        #     de Chrome: GroupControl wrapper sobre la card del picker).
        #   * la ventana del picker existe (chrome:// URL o el
        #     caller verificará el título — no pedimos URL aquí
        #     para no romper legacy sin metadata web).
        rescued_by_visual = (
            generic
            and visual_type in {"card", "button", "menu_item", "link"}
            and bool(visual_primary)
            and visual_inside
        )
        profile_compatible = (
            not web_content
            and not large
            and (
                (not generic and (profile_label or (name == "" and not generic)))
                or rescued_by_visual
            )
        )
        if profile_compatible:
            compatible.append("select_profile")
            if rescued_by_visual:
                reasons.append(
                    f"compatible:select_profile_via_visual_{visual_type}"
                )
        else:
            incompatible.append("select_profile")
            if web_content:
                reasons.append("incompatible:web_content_for_profile")
            if large:
                reasons.append("incompatible:large_bbox_for_profile")
            if generic and not rescued_by_visual:
                reasons.append("incompatible:generic_container_for_profile")

        # Otros tipos compatibles (heurística mínima — la decisión
        # real la sigue tomando el engine; aquí solo damos pistas).
        title = str(context.get("window_title") or "").lower()
        if "youtube" in title:
            compatible.append("open_search_result")
        if "chrome" in proc or "edge" in proc or "brave" in proc:
            compatible.append("open_url")
            compatible.append("open_search_result")
        return {
            "evidence_level": evidence,
            "compatible_with": compatible,
            "incompatible_with": incompatible,
            "reasons": reasons,
        }

    if kind == "type_text":
        # La fuerza la mide el field_session_manager — aquí solo
        # marcamos que hubo contexto.
        compatible.extend(["search_web", "search_youtube", "set_field_value"])
        evidence = "medium" if has_uia or has_locators else "weak"
        if not name and not has_locators:
            reasons.append("no_field_identity")
        return {
            "evidence_level": evidence,
            "compatible_with": compatible,
            "incompatible_with": incompatible,
            "reasons": reasons,
        }

    if kind == "scroll":
        compatible.append("scroll_page")
        compatible.append("scroll_results")
        evidence = "medium"
        return {
            "evidence_level": evidence,
            "compatible_with": compatible,
            "incompatible_with": incompatible,
            "reasons": reasons,
        }

    if kind == "key_press":
        evidence = "medium"
        return {
            "evidence_level": evidence,
            "compatible_with": compatible,
            "incompatible_with": incompatible,
            "reasons": reasons,
        }

    return {
        "evidence_level": "weak",
        "compatible_with": compatible,
        "incompatible_with": incompatible,
        "reasons": reasons,
    }


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────

def evaluate_event(ev: RawEvent) -> RecordedTruthEvent:
    """Convierte un :class:`RawEvent` en un :class:`RecordedTruthEvent`.

    Pura. Sin side effects.
    """
    kind = _kind_of(ev)
    target = _build_target_identity(ev)
    context = _build_context_block(ev)
    classification = _classify(kind, target, context)
    # PRD 2026-05-10h (Recorder Capture Contract): si el recorder
    # declaró que la captura quedó incompleta (faltan
    # pre_snapshot / post_state / OCR / parent_chain / UIA), bajamos
    # cualquier ``strong`` a ``medium`` — la regla de producto es
    # "lo que NO se puede demostrar, no se ejecuta". El downgrade
    # deja huella en reasons para auditoría.
    contract = ((ev.metadata or {}).get("capture_contract") or {})
    if isinstance(contract, dict):
        # ``capture_complete`` exige evidencia tipo "perfect_capture" (los 5
        # flags). Si aun así la señal alcanza ``sufficient_for_execution`` el
        # truth layer puede conservar ``strong``: el runtime tiene ancla UIA +
        # colateral o automation_id estable.
        incomplete = contract.get("capture_complete") is False
        sufficient = contract.get("sufficient_for_execution") is True
        if incomplete and not sufficient:
            if classification.get("evidence_level") == "strong":
                classification = dict(classification)
                classification["evidence_level"] = "medium"
                reasons = list(classification.get("reasons") or [])
                missing = ",".join(contract.get("missing") or [])
                reasons.append(
                    f"capture_contract.incomplete:cap_to_medium:{missing}"
                )
                classification["reasons"] = reasons
    return RecordedTruthEvent(
        event_id=f"truth:{ev.id}",
        raw_event_id=str(ev.id),
        event_kind=kind,
        sequence_id=int(getattr(ev, "sequence_id", 0) or 0),
        target_identity=target,
        context=context,
        outcome={},
        classification=classification,
    )


def build_truth_layer(raw_trace: List[RawEvent]) -> List[RecordedTruthEvent]:
    """Construye la capa completa para un raw_trace.

    PRD 2026-05-10g (Outcome Intelligence): cada evento se enriquece
    con la observación del estado posterior — qué cambió de
    ``window_context`` / URL / OCR / focus tras la acción. Esa segunda
    pasada también ajusta ``classification.evidence_level``: solo el
    outcome confirmado puede subir un click rescatado por el visual
    reader a ``strong``; sin outcome, queda capeado en ``medium``.

    El módulo ``outcome_intelligence`` es el dueño de esa lógica.
    Aquí solo invocamos la pasada — para mantener este módulo libre
    de detalles temporales y pure-Python.
    """
    if not raw_trace:
        return []
    truth: List[RecordedTruthEvent] = []
    for ev in raw_trace:
        truth.append(evaluate_event(ev))
    # Segunda pasada: outcomes + ajuste de evidence_level.
    # Import diferido para evitar ciclos: outcome_intelligence importa
    # ``RawEvent`` pero NO importa este módulo.
    try:
        from app.services.missions.outcome_intelligence import (
            enrich_truth_layer_with_outcomes,
        )
        enrich_truth_layer_with_outcomes(truth, raw_trace)
    except Exception:
        # Si algo falla en outcome (módulo experimental), no rompemos
        # la capa de verdad: dejamos los eventos con outcome={} y el
        # caller seguirá viendo evidence_level conservador del primer
        # pase (que ya capa visual rescue en "medium").
        pass
    return truth


def is_compatible_profile_picker_click(ev: RawEvent) -> bool:
    """Guard estructural: ¿este click puede ser parte de un profile
    picker, según la identidad del target?

    Comportamiento:

      * Si la ventana tiene título de picker **fuerte** (multi-idioma:
        "¿Quién eres?", "Choose a profile", URL ``chrome://profile-picker``…),
        SE ACEPTA aunque el UIA sea pobre (GroupControl, vacío). Es
        común que Chrome reporte el wrapper en lugar de la card.
      * Si solo hay un título "Google Chrome" plano (señal débil del
        picker), se exige que el TARGET sea estructuralmente compatible:
        no contenido web, no bbox enorme, no contenedor genérico vacío.

    Devuelve ``False`` cuando el target es contenido web (oración
    natural), bbox enorme, o GroupControl/Pane sin name **y** la
    ventana no tiene señal fuerte de picker. Es lo que evita que un
    HyperlinkControl con párrafo de Banxico se trate como item del
    picker porque el title era transitoriamente "Google Chrome".
    """
    if ev.event_type not in (
        EventType.MOUSE_CLICK,
        EventType.MOUSE_DOUBLE_CLICK,
    ):
        return False
    context = _build_context_block(ev)
    title = str(context.get("window_title") or "").lower()
    url = str(context.get("url") or "").lower()
    strong_picker = bool(context.get("profile_picker_url")) or any(
        h in title for h in _PROFILE_PICKER_STRONG_TITLE_HINTS
    )
    target = _build_target_identity(ev)
    visual = target.get("visual") or {}
    visual_type = str(visual.get("visual_type") or "")
    visual_primary = str(visual.get("primary_text") or "").strip()
    visual_inside = bool(visual.get("click_inside_bbox"))
    rescued_by_visual = (
        target.get("is_generic_container")
        and visual_type in {"card", "button", "menu_item", "link"}
        and bool(visual_primary)
        and visual_inside
    )

    # Fuerte: aceptamos siempre — la ventana ES un picker (Chrome/Edge).
    if strong_picker:
        # Aún rechazamos contenido web puro: jamás es un item de picker.
        if target.get("is_web_content_text"):
            return False
        return True

    # Débil ("Google Chrome" plano): el target tiene que ser discreto.
    if target.get("is_web_content_text"):
        return False
    if target.get("is_large_bbox"):
        return False
    if target.get("is_generic_container") and not rescued_by_visual:
        return False
    if target.get("looks_like_profile_label"):
        return True
    if target.get("automation_id"):
        return True
    name = str(target.get("name") or "")
    if name and len(name) <= 40 and not target.get("is_web_content_text"):
        return True
    # Screen Element Reader rescató una card legible: aceptamos.
    if rescued_by_visual:
        return True
    # Sin name pero sin red flags estructurales: aceptamos (el caller
    # se apoyará en otra fuente: OCR / window title / alias store).
    if not name and not target.get("is_generic_container"):
        return True
    return False


def truth_layer_summary(events: List[RecordedTruthEvent]) -> Dict[str, Any]:
    """Resumen agregado para auditoría (lo expone el plan)."""
    if not events:
        return {
            "total": 0,
            "by_kind": {},
            "by_evidence": {},
            "incompatible_select_profile": 0,
        }
    by_kind: Dict[str, int] = {}
    by_evidence: Dict[str, int] = {}
    incompatible_profile = 0
    for e in events:
        by_kind[e.event_kind] = by_kind.get(e.event_kind, 0) + 1
        ev = e.evidence_level
        by_evidence[ev] = by_evidence.get(ev, 0) + 1
        incompat = e.classification.get("incompatible_with") or []
        if "select_profile" in incompat:
            incompatible_profile += 1
    return {
        "total": len(events),
        "by_kind": by_kind,
        "by_evidence": by_evidence,
        "incompatible_select_profile": incompatible_profile,
    }


__all__ = [
    "EventKind",
    "EvidenceLevel",
    "RecordedTruthEvent",
    "bbox_area",
    "is_large_bbox",
    "looks_like_web_content_text",
    "looks_like_profile_label",
    "evaluate_event",
    "build_truth_layer",
    "is_compatible_profile_picker_click",
    "truth_layer_summary",
]
