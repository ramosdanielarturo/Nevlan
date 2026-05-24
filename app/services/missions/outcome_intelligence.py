"""
Nevlan — Outcome Intelligence (PRD 2026-05-10g)
================================================

Capability sistémica que enseña a Nevlan a responder:

    "¿qué cambió en el sistema después de la acción?"

Hasta ahora la cadena era:

    click + OCR  → semántica  → ejecución

con el riesgo conocido: si la lectura del objeto visual era buena
pero NADA cambió tras el click, Nevlan podía declarar evidencia
fuerte aunque la acción nunca tuviera efecto. Eso da pasos falsos.

Outcome Intelligence agrega la pieza que faltaba — observación del
estado posterior — sin tocar UI, executor, recorder ni scheduler:

    before_state
        → action
            → after_state
                → observed_outcome
                    → semantic_result

API pública (intencionalmente pequeña):

  * :class:`OutcomeObservation` — dataclass del observado.
  * :func:`compute_outcome_for(event_index, raw_trace, ...)` — comparas
    el ``window_context`` y la metadata visible del evento actual con
    los siguientes ``N`` eventos hasta detectar un cambio significativo.
  * :func:`outcome_confirms_action(obs, kind=...)` — clasifica si el
    outcome es **suficiente** para una intención dada
    (``selection_or_open``, ``navigation``, ``input_focus``, ``editing``,
    ``scroll``).
  * :func:`apply_outcome_to_classification(...)` — segunda pasada que
    sube/baja ``evidence_level`` en función del outcome real, dejando
    siempre el rastro en ``reasons`` para auditoría.
  * :func:`enrich_truth_layer_with_outcomes(...)` — convenience: aplica
    el cómputo + ajuste a una lista de ``RecordedTruthEvent`` ya
    construida.

Diseño:

  * **Pure-Python**, sin Qt, sin disco, sin LLM, sin side effects.
  * **Sin hardcode de Chrome / Daniel / select_profile / app específica**:
    todo razonamiento es estructural (cambios de hwnd / title / URL /
    foco / contenido visible).
  * El módulo NUNCA promueve evidencia a ``strong`` por sí solo: solo
    el outcome confirmado puede hacerlo, y solo si la lectura del
    objeto visual fue creíble. Si el outcome no se observó, puede
    BAJAR ``strong`` → ``medium`` para que la misión bloquee honesto.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.contracts.mission import RawEvent


# ──────────────────────────────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────────────────────────────

# Ventana de búsqueda: cuántos eventos consecutivos consultamos para
# detectar el cambio post-acción. 8 cubre clicks + esperas cortas +
# carga de página sin requerir un orquestador temporal completo.
_DEFAULT_HORIZON = 8

# Cap de latencia: si el siguiente cambio observado tarda >12s, ya no
# es atribuible a la acción (puede ser otra cosa que el usuario hizo
# después). 12s es generoso para páginas web lentas / SAP / Citrix.
_DEFAULT_MAX_LATENCY_MS = 12_000

# Niveles de evidencia (mismo orden que en recorder_truth_layer).
_EVIDENCE_RANK = {
    "strong": 3,
    "medium": 2,
    "weak": 1,
    "insufficient": 0,
}
_RANK_TO_EVIDENCE = {v: k for k, v in _EVIDENCE_RANK.items()}


# ──────────────────────────────────────────────────────────────────────
# Contrato público
# ──────────────────────────────────────────────────────────────────────

@dataclass
class OutcomeObservation:
    """Observación del estado posterior a una acción.

    Campos:

      * ``has_signal``        — ``True`` si encontramos algún cambio.
      * ``window_changed``    — el ``hwnd`` del foco cambió.
      * ``title_changed``     — el título de ventana cambió.
      * ``url_changed``       — la URL del frame web cambió.
      * ``focus_changed``     — el foco del UIA / DOM cambió.
      * ``new_content_visible`` — apareció contenido nuevo (vision_data
                                 distinto, OCR distinto, nuevo bbox).
      * ``hwnd_before/after``, ``title_before/after``, ``url_before/after``
                                 — para auditoría / re-render.
      * ``latency_ms``        — distancia temporal hasta el primer cambio.
      * ``sources``           — qué señales aportaron evidencia.
    """

    has_signal: bool = False
    window_changed: bool = False
    title_changed: bool = False
    url_changed: bool = False
    focus_changed: bool = False
    new_content_visible: bool = False

    hwnd_before: Optional[int] = None
    hwnd_after: Optional[int] = None
    title_before: str = ""
    title_after: str = ""
    url_before: str = ""
    url_after: str = ""

    latency_ms: int = 0
    sources: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_signal": bool(self.has_signal),
            "window_changed": bool(self.window_changed),
            "title_changed": bool(self.title_changed),
            "url_changed": bool(self.url_changed),
            "focus_changed": bool(self.focus_changed),
            "new_content_visible": bool(self.new_content_visible),
            "hwnd_before": self.hwnd_before,
            "hwnd_after": self.hwnd_after,
            "title_before": self.title_before,
            "title_after": self.title_after,
            "url_before": self.url_before,
            "url_after": self.url_after,
            "latency_ms": int(self.latency_ms),
            "sources": list(self.sources),
        }


# ──────────────────────────────────────────────────────────────────────
# Helpers internos
# ──────────────────────────────────────────────────────────────────────

def _wc(ev: RawEvent) -> Dict[str, Any]:
    wc = ev.window_context
    if wc is None:
        return {}
    return {
        "hwnd": wc.hwnd,
        "title": str(wc.title or ""),
        "process_name": str(wc.process_name or ""),
    }


def _meta(ev: RawEvent) -> Dict[str, Any]:
    return dict(ev.metadata or {})


def _url(ev: RawEvent) -> str:
    m = _meta(ev)
    web = m.get("web_data") or m.get("web") or {}
    if isinstance(web, dict):
        return str(web.get("url") or "")
    return ""


def _ocr_signature(ev: RawEvent) -> str:
    """Firma estructural del OCR del evento para detectar cambio de
    contenido visible. NO hace OCR — usa el ya capturado.
    """
    m = _meta(ev)
    vis = m.get("vision_data") or m.get("vision") or {}
    if not isinstance(vis, dict):
        return ""
    return (
        (str(vis.get("ocr_text_around") or "") or "")
        + "|"
        + (str(vis.get("nearest_text_anchor") or "") or "")
    ).strip()


def _focus_signature(ev: RawEvent) -> str:
    """Firma del UIA actualmente enfocado para detectar focus_changed."""
    m = _meta(ev)
    uia = m.get("desktop_uia") or m.get("uia_data") or m.get("uia") or {}
    if not isinstance(uia, dict):
        return ""
    return (
        f"{uia.get('automation_id') or ''}|"
        f"{uia.get('name') or ''}|"
        f"{uia.get('control_type') or ''}"
    )


def _ts_ms(ev: RawEvent) -> int:
    try:
        return int(getattr(ev, "offset_ms", 0) or 0)
    except Exception:
        return 0


def _delta_ms(start_ev: RawEvent, end_ev: RawEvent) -> int:
    a = _ts_ms(start_ev)
    b = _ts_ms(end_ev)
    if a and b and b > a:
        return int(b - a)
    # Fallback: aproximar por timestamp si offset no está poblado.
    try:
        ta = start_ev.timestamp
        tb = end_ev.timestamp
        if ta is not None and tb is not None:
            return int((tb - ta).total_seconds() * 1000)
    except Exception:
        pass
    return 0


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────

def compute_outcome_for(
    event_index: int,
    raw_trace: List[RawEvent],
    *,
    horizon: int = _DEFAULT_HORIZON,
    max_latency_ms: int = _DEFAULT_MAX_LATENCY_MS,
) -> OutcomeObservation:
    """Observa el estado posterior al evento ``raw_trace[event_index]``.

    Camina los siguientes ``horizon`` eventos del raw_trace y reporta
    el primer cambio significativo de window/title/url/focus/contenido.
    Nunca consulta UIA / OCR / disco en runtime: todo viene del raw
    trace persistido.

    Si no encuentra cambio dentro del horizonte (o la latencia excede
    ``max_latency_ms``) retorna una observación con ``has_signal=False``.
    """
    obs = OutcomeObservation()
    if event_index < 0 or event_index >= len(raw_trace):
        return obs
    cur = raw_trace[event_index]
    cur_wc = _wc(cur)
    cur_url = _url(cur)
    cur_ocr = _ocr_signature(cur)
    cur_focus = _focus_signature(cur)

    obs.hwnd_before = cur_wc.get("hwnd")
    obs.title_before = cur_wc.get("title") or ""
    obs.url_before = cur_url

    end = min(event_index + 1 + horizon, len(raw_trace))
    for j in range(event_index + 1, end):
        nxt = raw_trace[j]
        latency = _delta_ms(cur, nxt)
        if latency and latency > max_latency_ms:
            break
        nxt_wc = _wc(nxt)
        nxt_url = _url(nxt)
        nxt_ocr = _ocr_signature(nxt)
        nxt_focus = _focus_signature(nxt)

        win = (
            nxt_wc.get("hwnd") is not None
            and cur_wc.get("hwnd") is not None
            and nxt_wc.get("hwnd") != cur_wc.get("hwnd")
        )
        title = (
            (nxt_wc.get("title") or "")
            and (nxt_wc.get("title") != cur_wc.get("title"))
        )
        url = (
            nxt_url and cur_url and nxt_url != cur_url
        )
        focus = (
            nxt_focus and cur_focus and nxt_focus != cur_focus
        )
        ocr_changed = (
            nxt_ocr and cur_ocr and nxt_ocr != cur_ocr
        )
        # Caso especial: OCR vacío antes y aparece OCR después ⇒ contenido nuevo.
        new_content = (
            ocr_changed
            or (not cur_ocr and bool(nxt_ocr))
        )

        any_signal = bool(win or title or url or focus or new_content)
        if not any_signal:
            continue

        obs.has_signal = True
        obs.window_changed = bool(win)
        obs.title_changed = bool(title)
        obs.url_changed = bool(url)
        obs.focus_changed = bool(focus)
        obs.new_content_visible = bool(new_content)
        obs.hwnd_after = nxt_wc.get("hwnd")
        obs.title_after = nxt_wc.get("title") or ""
        obs.url_after = nxt_url
        obs.latency_ms = int(latency)
        if win:
            obs.sources.append("window_context.hwnd")
        if title:
            obs.sources.append("window_context.title")
        if url:
            obs.sources.append("web.url")
        if focus:
            obs.sources.append("uia.focus")
        if new_content:
            obs.sources.append("vision.ocr")
        return obs

    return obs


def outcome_confirms_action(
    obs: OutcomeObservation,
    *,
    kind: str,
) -> bool:
    """¿El outcome observado confirma una acción de tipo ``kind``?

    ``kind`` es una etiqueta semántica genérica:

      * ``selection_or_open`` — seleccionar perfil, abrir resultado,
                                 abrir item de menú. Esperamos transición
                                 de ventana / título / URL.
      * ``navigation``        — abrir URL nueva. Esperamos URL o título
                                 cambiados.
      * ``input_focus``       — clickear sobre un campo. Aceptamos
                                 ``focus_changed`` o aparición de OCR
                                 (caret/placeholder visible).
      * ``editing``           — escribir / modificar dentro de campo
                                 abierto. NO requiere transición de
                                 ventana — basta cambio de contenido.
      * ``scroll``            — scroll/wheel. Requiere
                                 ``new_content_visible``.

    Retorna ``False`` si la observación no tiene señal o si el kind
    es desconocido — ese es el caso "no podemos demostrar el outcome".
    """
    if not obs or not obs.has_signal:
        return False
    k = (kind or "").lower()
    if k in ("selection_or_open", "selection", "open"):
        return bool(
            obs.window_changed or obs.title_changed or obs.url_changed
        )
    if k == "navigation":
        return bool(obs.url_changed or obs.title_changed)
    if k == "input_focus":
        return bool(obs.focus_changed or obs.new_content_visible)
    if k == "editing":
        return bool(
            obs.new_content_visible or obs.focus_changed
        )
    if k == "scroll":
        return bool(obs.new_content_visible)
    return False


def apply_outcome_to_classification(
    classification: Dict[str, Any],
    target_identity: Dict[str, Any],
    obs: OutcomeObservation,
    *,
    intent_kind: str = "selection_or_open",
) -> Dict[str, Any]:
    """Segunda pasada: ajusta ``evidence_level`` en función del outcome.

    Reglas:

      * Si el outcome confirma la acción **y** la lectura visual fue
        creíble (visual.primary_text + click_inside_bbox) o el target
        ya tenía identidad estructural fuerte (automation_id /
        web.locators) ⇒ subimos a ``strong``.
      * Si NO hay outcome confirmado, capamos a ``medium`` cualquier
        click que dependa SOLO del visual reader (``visual.*``) o del
        OCR cercano. Identidades estructurales (automation_id /
        web.locators) NO se penalizan: ya son estables sin outcome.
      * Nunca DEGRADA por debajo de ``weak`` aquí — esa decisión es
        de ``_classify``.

    Retorna un dict NUEVO; no muta la entrada.
    """
    out = dict(classification or {})
    visual = (target_identity or {}).get("visual") or {}
    has_visual_rescue = bool(
        visual.get("primary_text")
        and visual.get("click_inside_bbox")
    )
    has_structural = bool(
        target_identity.get("automation_id")
        or target_identity.get("has_web_locators")
    )

    current = str(out.get("evidence_level") or "insufficient")
    rank = _EVIDENCE_RANK.get(current, 0)
    reasons = list(out.get("reasons") or [])
    sources = list(out.get("outcome_sources") or [])

    confirmed = outcome_confirms_action(obs, kind=intent_kind)
    if confirmed:
        bonus = 0
        if has_structural:
            bonus = 1  # ya iban en strong/medium; outcome lo solidifica.
        elif has_visual_rescue:
            bonus = 2  # visual + outcome = full strong.
        new_rank = min(_EVIDENCE_RANK["strong"], rank + bonus)
        if new_rank > rank:
            out["evidence_level"] = _RANK_TO_EVIDENCE[new_rank]
            reasons.append(
                f"outcome.confirmed:{intent_kind}"
            )
        sources.extend(obs.sources)
    else:
        # Sin outcome: nunca strong para clicks que dependen del
        # visual reader o de OCR cercano (regla "no convertir texto
        # cercano en verdad automática").
        if current == "strong" and not has_structural:
            out["evidence_level"] = "medium"
            reasons.append("outcome.not_observed:cap_to_medium")

    if reasons:
        out["reasons"] = reasons
    if sources:
        out["outcome_sources"] = sources
    return out


def enrich_truth_layer_with_outcomes(
    truth_events: List[Any],
    raw_trace: List[RawEvent],
    *,
    horizon: int = _DEFAULT_HORIZON,
    max_latency_ms: int = _DEFAULT_MAX_LATENCY_MS,
) -> None:
    """Aplica ``compute_outcome_for`` + ``apply_outcome_to_classification``
    a una lista de ``RecordedTruthEvent`` (mutándolos in place).

    Se asume que ``truth_events[i]`` corresponde a ``raw_trace[i]`` en
    orden — eso es lo que produce ``recorder_truth_layer.build_truth_layer``.
    """
    n = min(len(truth_events), len(raw_trace))
    for i in range(n):
        obs = compute_outcome_for(
            i, raw_trace, horizon=horizon, max_latency_ms=max_latency_ms
        )
        rec = truth_events[i]
        rec.outcome = obs.to_dict()
        intent_kind = _intent_kind_for(rec)
        rec.classification = apply_outcome_to_classification(
            rec.classification,
            rec.target_identity,
            obs,
            intent_kind=intent_kind,
        )


def _intent_kind_for(rec: Any) -> str:
    """Mapea ``RecordedTruthEvent`` → kind genérico de outcome.

    No mira la app; mira el ``event_kind`` y la lista
    ``compatible_with`` que ya publicó el classifier.
    """
    kind = str(getattr(rec, "event_kind", "") or "")
    compat = []
    try:
        compat = list(rec.compatible_semantic_types)
    except Exception:
        compat = list((rec.classification or {}).get("compatible_with") or [])
    if kind == "click":
        # Cualquier "selección" o apertura → selection_or_open.
        if any(c in compat for c in (
            "select_profile", "open_search_result",
            "open_url", "open_new_tab",
        )):
            return "selection_or_open"
        return "selection_or_open"
    if kind == "type_text":
        return "editing"
    if kind == "scroll":
        return "scroll"
    if kind == "key_press":
        return "editing"
    if kind == "drag":
        return "editing"
    return "selection_or_open"


__all__ = [
    "OutcomeObservation",
    "compute_outcome_for",
    "outcome_confirms_action",
    "apply_outcome_to_classification",
    "enrich_truth_layer_with_outcomes",
]
