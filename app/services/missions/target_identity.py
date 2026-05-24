"""
Nevlan — TargetIdentity Model (PRD 2026-05-10j)
================================================

Modelo fuerte de identidad del objetivo capturado por el
:mod:`intent_interception_layer`. Su contrato sustituye a los dicts
sueltos que circulaban antes — ``metadata["uia"]`` / ``metadata["web"]``
/ ``metadata["vision_data"]`` — por una estructura nombrada que separa
**lo que el usuario quiso tocar** (identidad) de **lo que cambió
después** (outcome) de **lo que se observó pero no identifica**
(debug_after_state).

Reglas de producto (literal del ticket):

  * ``target_identity`` = evidencia capturada ANTES o DURANTE el click.
  * ``outcome``         = evidencia observada DESPUÉS — valida, jamás
                          identifica.
  * ``debug_after_state``= UIA / web / OCR / visión post-action que NO
                          puede inflar la identidad.
  * Ningún ``GroupControl`` / ``PaneControl`` / ``CustomControl`` /
    ``DocumentControl`` genérico (sin name y sin automation_id) puede
    promoverse a ``strong``. La promoción a ``strong`` requiere o
    ``automation_id`` o un anchor textual (UIA name / web a11y / OCR)
    + bbox razonable.
  * Coordenadas absolutas NO son evidencia de identidad. Solo viven en
    ``coordinates`` como red de emergencia. ``evidence_sources`` jamás
    incluye ``coords.absolute`` como fuente de identidad fuerte.

API pública (intencionalmente pequeña y serializable):

  * :class:`EvidenceSource` — enum string de fuentes válidas.
  * :class:`TargetIdentity` — dataclass del contrato; ``to_dict`` /
                              ``from_dict`` para persistir.
  * :class:`Ambiguity` — razón estructural por la que la identidad
                         requiere confirmación / pregunta humana.
  * :func:`build_target_identity_from_capture` — construye una identidad
    a partir del bundle de evidencia capturado por la Intent
    Interception Layer.
  * :func:`promote_or_clamp_to_strong` — aplica la regla "no promover a
    strong por coordenadas o por GroupControl genérico".

Diseño:

  * **Pure-Python**, sin Qt, sin LLM, sin red. Todo es derivable de la
    evidencia bruta — el caller decide la fuente.
  * **Sin hardcode por app**: no nombramos Chrome / YouTube / Daniel.
    Las reglas son estructurales (control_type, bbox, presencia de
    automation_id / name / OCR / anchors).
  * **Inmutable salvo update explícito**: cada mutación se hace por
    métodos nombrados (``with_outcome`` / ``with_debug_after_state``).
    Eso impide que código downstream "olvide" cuál era la verdad
    original.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ──────────────────────────────────────────────────────────────────────
# Constantes estructurales (no app-specific)
# ──────────────────────────────────────────────────────────────────────

#: Control types que son contenedores genéricos. Un click cuyo target
#: inmediato es uno de estos *sin* anchor textual ⇒ identidad débil.
GENERIC_CONTAINER_CONTROL_TYPES: frozenset = frozenset({
    "groupcontrol",
    "panecontrol",
    "customcontrol",
    "documentcontrol",
    "windowcontrol",
    "toolbarcontrol",
    "statusbarcontrol",
})


#: Fuentes de evidencia válidas, en orden estructural de fuerza.
#: La regla "coordenadas absolutas no son identidad" se aplica
#: filtrando ``COORDS_ABSOLUTE`` de la lista de fuentes que puede
#: justificar ``confidence >= 0.85``.
class EvidenceSource:
    DOM_EXACT = "dom.exact"
    UIA_EXACT = "uia.exact"
    UIA_AUTOMATION_ID = "uia.automation_id"
    WEB_LOCATORS = "web.locators"
    OCR_TEXT = "ocr.text"
    UIA_NAME = "uia.name"
    PARENT_CHAIN = "uia.parent_chain"
    VISUAL_ICON = "visual.icon"
    VISUAL_TEXT = "visual.text"
    COORDS_RELATIVE = "coords.relative"
    COORDS_ABSOLUTE = "coords.absolute"  # ← emergencia, no identidad

    #: Conjunto de fuentes que SÍ pueden contribuir a identidad fuerte.
    STRONG_ELIGIBLE: frozenset = frozenset({
        DOM_EXACT,
        UIA_EXACT,
        UIA_AUTOMATION_ID,
        WEB_LOCATORS,
        OCR_TEXT,
        UIA_NAME,
        VISUAL_ICON,
        VISUAL_TEXT,
    })

    #: Fuentes que NUNCA pueden ser únicas para una identidad fuerte:
    #: coordenadas (absolutas y relativas). Sirven solo como
    #: desambiguador cuando hay otra señal estructural.
    NEVER_ALONE_STRONG: frozenset = frozenset({
        COORDS_RELATIVE,
        COORDS_ABSOLUTE,
    })


# Umbrales de aceptación (estructura, no porcentajes mágicos):
CONFIDENCE_ACCEPT_THRESHOLD = 0.85
CONFIDENCE_CONFIRM_THRESHOLD = 0.65
# < 0.65 → preguntar al usuario, nunca clickear "a lo loco".


# ──────────────────────────────────────────────────────────────────────
# Razones de ambigüedad (para que la UI muestre algo accionable)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Ambiguity:
    """Razón estructural por la que la identidad necesita confirmación.

    No contiene texto formateado para usuario — la UI traduce el
    ``code`` a copy localizable. Esto mantiene el modelo libre de
    cadenas presentables.
    """

    code: str = ""
    description: str = ""
    candidate_count: int = 0
    delta_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": str(self.code or ""),
            "description": str(self.description or ""),
            "candidate_count": int(self.candidate_count),
            "delta_score": round(float(self.delta_score), 4),
        }


# ──────────────────────────────────────────────────────────────────────
# TargetIdentity
# ──────────────────────────────────────────────────────────────────────


@dataclass
class TargetIdentity:
    """Identidad fuerte del objetivo de un click.

    Campos requeridos por el PRD 2026-05-10j:

      * ``target_id`` — id estable derivado del contenido (no de coords).
      * ``app`` / ``process`` — proceso dueño de la ventana.
      * ``window_title`` — título exacto al momento del click.
      * ``url`` / ``domain`` — solo si es navegador.
      * ``role`` / ``control_type`` — rol semántico / UIA control type.
      * ``name`` / ``aria_label`` / ``placeholder`` — anchors textuales.
      * ``automation_id`` — id estructural si lo hay.
      * ``dom_selector_candidates`` — selectores DOM tentativos
                                       (XPath / CSS / locators).
      * ``ocr_text_anchors`` — líneas OCR cercanas al click.
      * ``visual_fingerprint`` — hash perceptual / firma visual.
      * ``icon_crop`` — ruta a crop del icono si aplica.
      * ``bounding_box`` — bbox del elemento (no de la ventana).
      * ``click_offset_ratio`` — offset relativo al bbox (centrado o no).
      * ``surrounding_labels`` — labels cercanos en el árbol (parent /
                                  siblings con texto).
      * ``parent_chain`` — cadena de ancestros relevante.
      * ``confidence`` — 0.0–1.0 según fusion engine.
      * ``evidence_sources`` — lista de :class:`EvidenceSource` usadas.
      * ``freshness_ms`` — edad de la evidencia respecto al click_ms.
      * ``ambiguity`` — :class:`Ambiguity` si requiere confirmación.

    Notas:

      * ``confidence_label`` se deriva de ``confidence`` con
        :func:`label_from_confidence` (no es un campo guardado: una
        regla pura sobre el valor).
      * ``debug_after_state`` y ``outcome`` viven **fuera** de esta
        clase — la regla es estricta: la identidad no carga lo que
        pasó después.
    """

    target_id: str = ""
    app: str = ""
    process: str = ""
    window_title: str = ""
    url: str = ""
    domain: str = ""
    role: str = ""
    control_type: str = ""
    name: str = ""
    aria_label: str = ""
    placeholder: str = ""
    automation_id: str = ""
    dom_selector_candidates: List[str] = field(default_factory=list)
    ocr_text_anchors: List[str] = field(default_factory=list)
    visual_fingerprint: str = ""
    icon_crop: str = ""
    bounding_box: Dict[str, int] = field(default_factory=dict)
    click_offset_ratio: Dict[str, float] = field(default_factory=dict)
    surrounding_labels: List[str] = field(default_factory=list)
    parent_chain: List[Dict[str, Any]] = field(default_factory=list)

    confidence: float = 0.0
    evidence_sources: List[str] = field(default_factory=list)
    freshness_ms: int = 0
    ambiguity: Optional[Ambiguity] = None

    # ── propiedades derivadas ────────────────────────────────────
    @property
    def confidence_label(self) -> str:
        return label_from_confidence(self.confidence)

    @property
    def has_strong_structural_anchor(self) -> bool:
        """¿Hay algún anchor estructural fuerte (no coordenadas)?

        Estructural ≠ contenedor genérico vacío. Las reglas:

          * ``automation_id`` no vacío.
          * ``dom_selector_candidates`` no vacío.
          * O ``name`` / ``aria_label`` / ``placeholder`` no vacíos
            **y** el ``control_type`` NO es contenedor genérico.
        """
        if (self.automation_id or "").strip():
            return True
        if self.dom_selector_candidates:
            return True
        anchor_text = (
            (self.name or "").strip()
            or (self.aria_label or "").strip()
            or (self.placeholder or "").strip()
        )
        if not anchor_text:
            return False
        ct = (self.control_type or "").strip().lower()
        if ct in GENERIC_CONTAINER_CONTROL_TYPES:
            return False
        return True

    @property
    def is_generic_container(self) -> bool:
        ct = (self.control_type or "").strip().lower()
        return ct in GENERIC_CONTAINER_CONTROL_TYPES

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        # Serializar la ambiguity de forma anidada o ``None``.
        if isinstance(self.ambiguity, Ambiguity):
            out["ambiguity"] = self.ambiguity.to_dict()
        else:
            out["ambiguity"] = None
        out["confidence_label"] = self.confidence_label
        return out

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TargetIdentity":
        data = dict(payload or {})
        amb = data.pop("ambiguity", None)
        data.pop("confidence_label", None)  # derivado
        if isinstance(amb, dict):
            ambiguity: Optional[Ambiguity] = Ambiguity(
                code=str(amb.get("code") or ""),
                description=str(amb.get("description") or ""),
                candidate_count=int(amb.get("candidate_count") or 0),
                delta_score=float(amb.get("delta_score") or 0.0),
            )
        else:
            ambiguity = None
        # Filtramos kwargs desconocidos para tolerar versiones futuras.
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in data.items() if k in valid}
        kwargs["ambiguity"] = ambiguity
        return cls(**kwargs)


# ──────────────────────────────────────────────────────────────────────
# Helpers públicos
# ──────────────────────────────────────────────────────────────────────


def label_from_confidence(confidence: float) -> str:
    """Etiqueta semántica que la UI puede traducir.

    Las fronteras coinciden con las del PRD:

      * ``confidence >= 0.85`` ⇒ ``"strong"`` (aceptar sin confirmar).
      * ``0.65 <= confidence < 0.85`` ⇒ ``"medium"`` (confirmar si
        el paso es crítico).
      * ``confidence < 0.65`` ⇒ ``"insufficient"`` (preguntar al
        usuario; jamás clickear a lo loco).
    """
    try:
        c = float(confidence)
    except Exception:
        c = 0.0
    if c >= CONFIDENCE_ACCEPT_THRESHOLD:
        return "strong"
    if c >= CONFIDENCE_CONFIRM_THRESHOLD:
        return "medium"
    return "insufficient"


def _slug(text: str) -> str:
    """Slug determinista para construir ``target_id`` estables a
    partir de anchors textuales o estructurales. Sin libs externas.
    """
    s = str(text or "").strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_\-:]", "", s)
    return s[:80]


def derive_target_id(
    *,
    automation_id: str = "",
    dom_selectors: Optional[Sequence[str]] = None,
    name: str = "",
    aria_label: str = "",
    placeholder: str = "",
    domain: str = "",
    role: str = "",
) -> str:
    """Construye un id derivable y estable.

    Prioridad (de más a menos estable):

      1. ``automation_id`` puro.
      2. Primer DOM selector listado.
      3. ``role`` + anchor textual (name / aria / placeholder).
      4. ``domain`` + anchor textual.
      5. Anchor textual solo.

    Si nada estructural existe, retorna ``""`` — caller decide qué
    hacer (probablemente preguntar al usuario).
    """
    aid = (automation_id or "").strip()
    if aid:
        return f"aid:{_slug(aid)}"
    sels = list(dom_selectors or [])
    if sels:
        first = next((str(s) for s in sels if str(s or "").strip()), "")
        if first:
            return f"sel:{_slug(first)}"
    anchor = (
        (name or "").strip()
        or (aria_label or "").strip()
        or (placeholder or "").strip()
    )
    if not anchor:
        return ""
    role_s = (role or "").strip().lower()
    if role_s:
        return f"{_slug(role_s)}:{_slug(anchor)}"
    dom = (domain or "").strip().lower()
    if dom:
        return f"{_slug(dom)}:{_slug(anchor)}"
    return f"text:{_slug(anchor)}"


def promote_or_clamp_to_strong(
    identity: TargetIdentity,
) -> TargetIdentity:
    """Aplica las reglas de promoción / clamp del PRD.

    Reglas (estructurales, no app-specific):

      1. Si la identidad es un contenedor genérico SIN anchor textual
         y SIN automation_id ⇒ confidence se capa a ``< 0.65`` y se
         marca ``Ambiguity(code="generic_container_no_anchor")``.
      2. Si ``evidence_sources`` solo contiene fuentes en
         ``NEVER_ALONE_STRONG`` (coords) ⇒ confidence se capa a
         ``< 0.65`` con ``code="coords_only"``.
      3. Si hay ``has_strong_structural_anchor`` y al menos UNA fuente
         de ``STRONG_ELIGIBLE`` ⇒ se permite la confianza calculada.
      4. Si la confianza es ``>= 0.85`` pero no cumple (3) ⇒ se baja
         a ``0.79`` para que el caller pida confirmación ligera.

    Devuelve una nueva instancia (no muta la entrada). Es idempotente.
    """
    out = TargetIdentity.from_dict(identity.to_dict())

    sources = [str(s) for s in (out.evidence_sources or []) if s]
    only_coords = bool(sources) and all(
        s in EvidenceSource.NEVER_ALONE_STRONG for s in sources
    )
    has_strong_source = any(
        s in EvidenceSource.STRONG_ELIGIBLE for s in sources
    )

    if out.is_generic_container and not out.has_strong_structural_anchor:
        # Caso 1: GroupControl pelado.
        if out.confidence >= CONFIDENCE_CONFIRM_THRESHOLD:
            out.confidence = min(
                out.confidence, CONFIDENCE_CONFIRM_THRESHOLD - 0.01
            )
        out.ambiguity = Ambiguity(
            code="generic_container_no_anchor",
            description=(
                "El target inmediato es un contenedor genérico sin "
                "texto, automation_id ni anchor estructural."
            ),
        )
        return out

    if only_coords:
        if out.confidence >= CONFIDENCE_CONFIRM_THRESHOLD:
            out.confidence = min(
                out.confidence, CONFIDENCE_CONFIRM_THRESHOLD - 0.01
            )
        out.ambiguity = Ambiguity(
            code="coords_only",
            description=(
                "La identidad solo depende de coordenadas; falta "
                "evidencia estructural (UIA / DOM / OCR / visión)."
            ),
        )
        return out

    if out.confidence >= CONFIDENCE_ACCEPT_THRESHOLD and not (
        has_strong_source and out.has_strong_structural_anchor
    ):
        # Sin anchor estructural fuerte no se permite confianza alta.
        out.confidence = CONFIDENCE_ACCEPT_THRESHOLD - 0.06
        out.ambiguity = out.ambiguity or Ambiguity(
            code="confidence_capped_no_strong_anchor",
            description=(
                "Se redujo la confianza porque las fuentes válidas "
                "no se apoyan en un anchor estructural fuerte."
            ),
        )

    return out


# ──────────────────────────────────────────────────────────────────────
# Builder desde un bundle de evidencia bruta
# ──────────────────────────────────────────────────────────────────────


def build_target_identity_from_capture(
    *,
    click_xy: Tuple[int, int],
    click_ms: int,
    window_ctx: Dict[str, Any],
    uia: Optional[Dict[str, Any]] = None,
    web: Optional[Dict[str, Any]] = None,
    ocr: Optional[Dict[str, Any]] = None,
    visual: Optional[Dict[str, Any]] = None,
    parent_chain: Optional[Dict[str, Any]] = None,
    confidence: float = 0.0,
    evidence_sources: Optional[Sequence[str]] = None,
    capture_ms: Optional[int] = None,
) -> TargetIdentity:
    """Construye una :class:`TargetIdentity` a partir de evidencia bruta.

    Esta función NO decide confianza por sí sola — recibe la que el
    fusion engine calculó. SÍ aplica
    :func:`promote_or_clamp_to_strong` antes de devolver, así nunca
    sale una identidad fuerte basada solo en coordenadas o sobre un
    contenedor genérico vacío.
    """
    uia = dict(uia or {})
    web = dict(web or {})
    ocr = dict(ocr or {})
    visual = dict(visual or {})
    parent_chain = dict(parent_chain or {})
    window_ctx = dict(window_ctx or {})

    process = str(window_ctx.get("process_name") or window_ctx.get("process") or "")
    title = str(window_ctx.get("title") or "")
    url = str(web.get("url") or "")
    domain = ""
    if url and "//" in url:
        try:
            domain = url.split("//", 1)[1].split("/", 1)[0].lower()
        except Exception:
            domain = ""

    name = str(uia.get("name") or web.get("accessible_name") or "").strip()
    automation_id = str(uia.get("automation_id") or "").strip()
    control_type = str(uia.get("control_type") or "").strip()
    role = str(web.get("role") or uia.get("control_type") or "").strip()
    aria_label = str(web.get("aria_label") or web.get("label") or "").strip()
    placeholder = str(web.get("placeholder") or "").strip()

    dom_selectors: List[str] = []
    locs = web.get("locators")
    if isinstance(locs, list):
        for loc in locs:
            if isinstance(loc, str) and loc.strip():
                dom_selectors.append(loc.strip())
            elif isinstance(loc, dict):
                for k in ("css", "xpath", "test_id", "selector"):
                    val = loc.get(k)
                    if isinstance(val, str) and val.strip():
                        dom_selectors.append(val.strip())

    bbox: Dict[str, int] = {}
    raw_bbox = uia.get("bbox") if isinstance(uia.get("bbox"), dict) else None
    if not raw_bbox and isinstance(web.get("bbox"), dict):
        raw_bbox = web.get("bbox")
    if isinstance(raw_bbox, dict):
        try:
            bbox = {
                "left": int(raw_bbox.get("left", 0)),
                "top": int(raw_bbox.get("top", 0)),
                "width": int(raw_bbox.get("width", 0)),
                "height": int(raw_bbox.get("height", 0)),
            }
        except Exception:
            bbox = {}

    click_offset_ratio: Dict[str, float] = {}
    if bbox and bbox.get("width", 0) > 0 and bbox.get("height", 0) > 0:
        cx, cy = int(click_xy[0]), int(click_xy[1])
        try:
            click_offset_ratio = {
                "fx": round((cx - bbox["left"]) / float(bbox["width"]), 4),
                "fy": round((cy - bbox["top"]) / float(bbox["height"]), 4),
            }
        except Exception:
            click_offset_ratio = {}

    ocr_anchors: List[str] = []
    for line in (ocr.get("ocr_lines") or []):
        s = str(line or "").strip()
        if s:
            ocr_anchors.append(s)

    visual_fp = str(visual.get("fingerprint") or visual.get("hash") or "")
    icon_crop = str(visual.get("icon_crop") or visual.get("crop_path") or "")

    surrounding_labels: List[str] = []
    siblings = parent_chain.get("siblings_near_click") or []
    for sib in siblings:
        if isinstance(sib, dict):
            nm = str(sib.get("name") or "").strip()
            if nm:
                surrounding_labels.append(nm)

    parent_list: List[Dict[str, Any]] = []
    for anc in (parent_chain.get("ancestors") or []):
        if isinstance(anc, dict):
            parent_list.append({
                "name": str(anc.get("name") or ""),
                "control_type": str(anc.get("control_type") or ""),
                "automation_id": str(anc.get("automation_id") or ""),
            })

    target_id = derive_target_id(
        automation_id=automation_id,
        dom_selectors=dom_selectors,
        name=name,
        aria_label=aria_label,
        placeholder=placeholder,
        domain=domain,
        role=role,
    )

    freshness_ms = 0
    if capture_ms is not None:
        try:
            freshness_ms = max(0, int(capture_ms) - int(click_ms))
        except Exception:
            freshness_ms = 0

    identity = TargetIdentity(
        target_id=target_id,
        app=process,
        process=process,
        window_title=title,
        url=url,
        domain=domain,
        role=role,
        control_type=control_type,
        name=name,
        aria_label=aria_label,
        placeholder=placeholder,
        automation_id=automation_id,
        dom_selector_candidates=dom_selectors,
        ocr_text_anchors=ocr_anchors,
        visual_fingerprint=visual_fp,
        icon_crop=icon_crop,
        bounding_box=bbox,
        click_offset_ratio=click_offset_ratio,
        surrounding_labels=surrounding_labels,
        parent_chain=parent_list,
        confidence=max(0.0, min(1.0, float(confidence or 0.0))),
        evidence_sources=list(evidence_sources or []),
        freshness_ms=int(freshness_ms),
    )
    return promote_or_clamp_to_strong(identity)


__all__ = [
    "Ambiguity",
    "EvidenceSource",
    "TargetIdentity",
    "GENERIC_CONTAINER_CONTROL_TYPES",
    "CONFIDENCE_ACCEPT_THRESHOLD",
    "CONFIDENCE_CONFIRM_THRESHOLD",
    "label_from_confidence",
    "derive_target_id",
    "promote_or_clamp_to_strong",
    "build_target_identity_from_capture",
]
