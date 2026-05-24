"""
Nevlan — Candidate Fusion Engine (PRD 2026-05-10j)
====================================================

Motor que combina candidatos de target provenientes de múltiples
fuentes (UIA hit-test, DOM elementFromPoint, OCR local, visión local,
historial / alias, contexto de misión) y elige el ``chosen_target`` con
un score auditable.

Reglas del PRD (citadas literal del ticket):

  * DOM exacto bajo el cursor:     alto.
  * UIA exacto bajo el cursor:     alto.
  * OCR dentro del bbox o cercano: medio/alto.
  * Icono visual repetible:        medio.
  * Coordenada relativa:           bajo.
  * Coordenada absoluta:           emergencia.

  * confidence ≥ 0.85  ⇒ aceptar.
  * 0.65 ≤ c < 0.85    ⇒ aceptar pero pedir confirmación si crítico.
  * c < 0.65           ⇒ preguntar al usuario; jamás clickear a lo loco.

  * NUNCA convertir un ``GroupControl`` / contenedor genérico en
    target fuerte.
  * El outcome es solo validación, jamás identidad.

Diseño:

  * **Pure-Python**, sin Qt, sin LLM. El scoring es transparente:
    cada candidato lleva ``score`` + ``reasons`` legibles para audit.
  * **Sin hardcode por app**. Las reglas operan sobre presencia de
    fuentes / forma de control / contención de bbox al click.
  * **Outcome-aware solo como validación**: el caller puede pasar
    ``outcome_confirmed=True`` para subir confianza sin convertir el
    outcome en identidad. Si ``outcome_confirmed=False`` y no hay
    anchor estructural fuerte, la confianza se capa.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.missions.target_identity import (
    CONFIDENCE_ACCEPT_THRESHOLD,
    CONFIDENCE_CONFIRM_THRESHOLD,
    Ambiguity,
    EvidenceSource,
    GENERIC_CONTAINER_CONTROL_TYPES,
    TargetIdentity,
    build_target_identity_from_capture,
    promote_or_clamp_to_strong,
)


# ──────────────────────────────────────────────────────────────────────
# Datos
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    """Candidato individual proveniente de una fuente.

    ``source`` debe ser uno de :class:`EvidenceSource` (string).
    ``identity_seed`` es el bundle que :func:`build_target_identity_from_capture`
    sabe consumir (uia / web / ocr / visual / parent_chain).
    """

    source: str
    identity_seed: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    # bbox del candidato, para detectar si contiene al click.
    bbox: Optional[Dict[str, int]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": str(self.source),
            "score": round(float(self.score), 4),
            "reasons": list(self.reasons),
            "bbox": dict(self.bbox or {}) or None,
        }


@dataclass
class FusionResult:
    """Resultado del motor: target elegido + auditoría completa."""

    chosen: TargetIdentity
    confidence: float = 0.0
    decision: str = "ask"   # "accept" | "confirm" | "ask"
    ambiguity: Optional[Ambiguity] = None
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    evidence_sources: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "confidence": round(float(self.confidence), 4),
            "confidence_label": _label_for(self.confidence),
            "decision": str(self.decision),
            "candidates": list(self.candidates),
            "evidence_sources": list(self.evidence_sources),
            "reasons": list(self.reasons),
            "ambiguity": (
                self.ambiguity.to_dict() if self.ambiguity is not None else None
            ),
        }
        return out


# ──────────────────────────────────────────────────────────────────────
# Scoring estructural por fuente
# ──────────────────────────────────────────────────────────────────────

#: Score "base" cuando una fuente aporta evidencia útil y el bbox
#: contiene al click. Las cifras son ordenables, no porcentajes.
_BASE_SCORE = {
    EvidenceSource.DOM_EXACT: 0.95,
    EvidenceSource.UIA_AUTOMATION_ID: 0.95,
    EvidenceSource.UIA_EXACT: 0.85,
    EvidenceSource.WEB_LOCATORS: 0.85,
    EvidenceSource.UIA_NAME: 0.70,
    EvidenceSource.OCR_TEXT: 0.65,
    EvidenceSource.PARENT_CHAIN: 0.50,
    EvidenceSource.VISUAL_ICON: 0.55,
    EvidenceSource.VISUAL_TEXT: 0.50,
    EvidenceSource.COORDS_RELATIVE: 0.30,
    EvidenceSource.COORDS_ABSOLUTE: 0.10,
}


def _label_for(c: float) -> str:
    if c >= CONFIDENCE_ACCEPT_THRESHOLD:
        return "strong"
    if c >= CONFIDENCE_CONFIRM_THRESHOLD:
        return "medium"
    return "insufficient"


def _bbox_contains(bbox: Optional[Dict[str, int]], xy: Tuple[int, int]) -> bool:
    if not isinstance(bbox, dict):
        return False
    try:
        left = int(bbox.get("left", 0))
        top = int(bbox.get("top", 0))
        w = int(bbox.get("width", 0))
        h = int(bbox.get("height", 0))
        x, y = int(xy[0]), int(xy[1])
    except Exception:
        return False
    if w <= 0 or h <= 0:
        return False
    return (left <= x < left + w) and (top <= y < top + h)


def _is_generic_container(uia: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(uia, dict):
        return False
    ct = str(uia.get("control_type") or "").strip().lower()
    name = str(uia.get("name") or "").strip()
    aid = str(uia.get("automation_id") or "").strip()
    return ct in GENERIC_CONTAINER_CONTROL_TYPES and not (name or aid)


# ──────────────────────────────────────────────────────────────────────
# Constructores de candidatos por fuente
# ──────────────────────────────────────────────────────────────────────


def _candidate_from_dom(
    dom: Optional[Dict[str, Any]],
    click_xy: Tuple[int, int],
) -> Optional[Candidate]:
    if not isinstance(dom, dict) or not dom:
        return None
    selectors: List[str] = []
    for k in ("css", "xpath", "test_id", "selector"):
        v = dom.get(k)
        if isinstance(v, str) and v.strip():
            selectors.append(v.strip())
    locs = dom.get("locators")
    if isinstance(locs, list):
        for loc in locs:
            if isinstance(loc, str) and loc.strip():
                selectors.append(loc.strip())
            elif isinstance(loc, dict):
                for k in ("css", "xpath", "test_id", "selector"):
                    val = loc.get(k)
                    if isinstance(val, str) and val.strip():
                        selectors.append(val.strip())
    name = str(dom.get("accessible_name") or dom.get("name") or "").strip()
    aria = str(dom.get("aria_label") or dom.get("label") or "").strip()
    bbox = dom.get("bbox") if isinstance(dom.get("bbox"), dict) else None
    if not selectors and not name and not aria:
        return None
    score = _BASE_SCORE[EvidenceSource.DOM_EXACT]
    reasons = ["dom.element_from_point"]
    if not _bbox_contains(bbox, click_xy):
        # No contiene el click → no es DOM exacto bajo cursor.
        score -= 0.20
        reasons.append("dom.bbox_does_not_contain_click")
    return Candidate(
        source=EvidenceSource.DOM_EXACT,
        identity_seed={"web": {**dom, "locators": locs or selectors}},
        score=max(0.0, score),
        reasons=reasons,
        bbox=bbox,
    )


def _candidate_from_uia(
    uia: Optional[Dict[str, Any]],
    click_xy: Tuple[int, int],
) -> Optional[Candidate]:
    if not isinstance(uia, dict) or not uia:
        return None
    name = str(uia.get("name") or "").strip()
    aid = str(uia.get("automation_id") or "").strip()
    ct = str(uia.get("control_type") or "").strip().lower()
    bbox = uia.get("bbox") if isinstance(uia.get("bbox"), dict) else None
    if _is_generic_container(uia):
        # Contenedor genérico vacío → no es identidad.
        return Candidate(
            source=EvidenceSource.UIA_EXACT,
            identity_seed={"uia": uia},
            score=0.20,
            reasons=["uia.generic_container_no_anchor"],
            bbox=bbox,
        )
    if aid:
        source = EvidenceSource.UIA_AUTOMATION_ID
    elif name:
        source = (
            EvidenceSource.UIA_EXACT
            if ct and ct not in GENERIC_CONTAINER_CONTROL_TYPES
            else EvidenceSource.UIA_NAME
        )
    else:
        return None
    score = _BASE_SCORE[source]
    reasons = [source]
    if not _bbox_contains(bbox, click_xy):
        score -= 0.15
        reasons.append("uia.bbox_does_not_contain_click")
    return Candidate(
        source=source,
        identity_seed={"uia": uia},
        score=max(0.0, score),
        reasons=reasons,
        bbox=bbox,
    )


def _candidate_from_ocr(
    ocr: Optional[Dict[str, Any]],
    click_xy: Tuple[int, int],
) -> Optional[Candidate]:
    if not isinstance(ocr, dict) or not ocr:
        return None
    lines = [str(s).strip() for s in (ocr.get("ocr_lines") or []) if str(s or "").strip()]
    if not lines:
        return None
    bbox = ocr.get("ocr_region_bbox") if isinstance(ocr.get("ocr_region_bbox"), dict) else None
    score = _BASE_SCORE[EvidenceSource.OCR_TEXT]
    reasons = ["ocr.lines_present"]
    if bbox and _bbox_contains(bbox, click_xy):
        score += 0.05
        reasons.append("ocr.region_contains_click")
    # Texto razonable (no líneas de basura): al menos una línea con ≥2 chars.
    if not any(len(s) >= 2 for s in lines):
        score -= 0.20
        reasons.append("ocr.short_lines")
    return Candidate(
        source=EvidenceSource.OCR_TEXT,
        identity_seed={"ocr": {"ocr_lines": lines, "ocr_region_bbox": bbox or {}}},
        score=max(0.0, score),
        reasons=reasons,
        bbox=bbox,
    )


def _candidate_from_visual(
    visual: Optional[Dict[str, Any]],
    click_xy: Tuple[int, int],
) -> Optional[Candidate]:
    if not isinstance(visual, dict) or not visual:
        return None
    has_icon = bool(visual.get("fingerprint") or visual.get("icon_crop"))
    primary_text = str(visual.get("primary_text") or "").strip()
    if not has_icon and not primary_text:
        return None
    source = (
        EvidenceSource.VISUAL_ICON if has_icon and not primary_text
        else (EvidenceSource.VISUAL_TEXT if primary_text else EvidenceSource.VISUAL_ICON)
    )
    score = _BASE_SCORE[source]
    reasons = [source]
    bbox = visual.get("bbox") if isinstance(visual.get("bbox"), dict) else None
    if bbox and _bbox_contains(bbox, click_xy):
        score += 0.05
        reasons.append("visual.bbox_contains_click")
    return Candidate(
        source=source,
        identity_seed={"visual": visual},
        score=max(0.0, score),
        reasons=reasons,
        bbox=bbox,
    )


def _candidate_from_visual_fingerprint(
    fingerprint: Optional[Dict[str, Any]],
    click_xy: Tuple[int, int],
) -> Optional[Candidate]:
    """PRD 2026-05-12: candidato basado en visual_fingerprint determinista.

    El fingerprint NO contiene un bbox en el frame actual (se genera
    en grabación, no en ejecución). Su valor para fusion engine es
    elevar la confianza cuando hay un anchor estructural pixel-perfect
    grabado — Smart Executor lo usa luego para re-localizar el target
    si se movió. La presencia de fingerprint con ``quality="strong"``
    actúa como respaldo de identidad: aunque la app cambie de UI
    versión, podremos reencontrar visualmente el elemento.

    Solo emite candidato si ``quality in {"strong", "medium"}`` y
    ``available=True``. ``insufficient`` no contribuye.
    """
    if not isinstance(fingerprint, dict) or not fingerprint:
        return None
    if not fingerprint.get("available"):
        return None
    quality = str(fingerprint.get("quality") or "insufficient").lower()
    if quality not in ("strong", "medium"):
        return None
    base_score = (
        _BASE_SCORE[EvidenceSource.VISUAL_ICON] if quality == "strong"
        else _BASE_SCORE[EvidenceSource.VISUAL_ICON] - 0.10
    )
    return Candidate(
        source=EvidenceSource.VISUAL_ICON,
        identity_seed={"visual_fingerprint": fingerprint},
        score=max(0.0, base_score),
        reasons=[f"visual_fingerprint.{quality}",
                 f"orb_keypoints={int(fingerprint.get('orb_keypoint_count') or 0)}"],
        bbox=None,
    )


def _candidate_from_coords(
    click_xy: Tuple[int, int],
    *,
    window_bbox: Optional[Dict[str, int]] = None,
) -> Candidate:
    """Coords como emergencia. Si tenemos window_bbox calculamos
    relativas, lo cual es ligeramente mejor que solo absolutas.
    """
    if window_bbox and isinstance(window_bbox, dict):
        source = EvidenceSource.COORDS_RELATIVE
    else:
        source = EvidenceSource.COORDS_ABSOLUTE
    return Candidate(
        source=source,
        identity_seed={},
        score=_BASE_SCORE[source],
        reasons=["coords.last_resort"],
        bbox=None,
    )


# ──────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────


@dataclass
class CandidateFusionEngine:
    """Combina candidatos de distintas fuentes y elige el target.

    Uso típico::

        engine = CandidateFusionEngine()
        result = engine.fuse(
            click_xy=(312, 488),
            click_ms=1_700_000_000_000,
            window_ctx={"hwnd": 1, "process_name": "chrome.exe",
                         "title": "YouTube"},
            uia={"name": "Buscar", "control_type": "EditControl",
                  "automation_id": "search_box",
                  "bbox": {"left": 300, "top": 470, "width": 200,
                           "height": 40}},
            dom=None,
            ocr={"ocr_lines": ["Buscar"]},
            visual=None,
            outcome_confirmed=False,
        )
        result.decision   # "accept" / "confirm" / "ask"
        result.chosen     # TargetIdentity con confidence + ambiguity
    """

    # Cuánto sube la confianza una observación de outcome confirmada,
    # SOLO si ya hay anchor estructural. Outcome jamás convierte algo
    # sin anchor en identidad fuerte.
    outcome_bonus: float = 0.06
    # Penalización si solo hubo coordenadas como fuente útil.
    coords_only_penalty: float = 0.30

    def fuse(
        self,
        *,
        click_xy: Tuple[int, int],
        click_ms: int,
        window_ctx: Dict[str, Any],
        uia: Optional[Dict[str, Any]] = None,
        dom: Optional[Dict[str, Any]] = None,
        ocr: Optional[Dict[str, Any]] = None,
        visual: Optional[Dict[str, Any]] = None,
        parent_chain: Optional[Dict[str, Any]] = None,
        window_bbox: Optional[Dict[str, int]] = None,
        outcome_confirmed: bool = False,
        capture_ms: Optional[int] = None,
        visual_fingerprint: Optional[Dict[str, Any]] = None,
    ) -> FusionResult:
        # 1. Construir candidatos por fuente.
        candidates: List[Candidate] = []
        for cand in (
            _candidate_from_dom(dom, click_xy),
            _candidate_from_uia(uia, click_xy),
            _candidate_from_ocr(ocr, click_xy),
            _candidate_from_visual(visual, click_xy),
            _candidate_from_visual_fingerprint(visual_fingerprint, click_xy),
        ):
            if cand is not None:
                candidates.append(cand)

        # Coords siempre como red de emergencia, pero NUNCA único.
        coord_cand = _candidate_from_coords(click_xy, window_bbox=window_bbox)
        candidates.append(coord_cand)

        # 2. Ranking por score.
        candidates.sort(key=lambda c: c.score, reverse=True)

        # 3. Confianza final = score del primero, con boosts/penalties.
        if not candidates:
            return self._ask_result(
                click_xy=click_xy, click_ms=click_ms,
                window_ctx=window_ctx, reason="no_candidates",
            )

        top = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None

        confidence = float(top.score)
        reasons: List[str] = [f"top:{top.source}:{round(top.score,3)}"]

        # Si la única fuente útil son coords → capamos.
        non_coord_top = next(
            (c for c in candidates if c.source not in EvidenceSource.NEVER_ALONE_STRONG),
            None,
        )
        if non_coord_top is None:
            confidence = max(0.0, confidence - self.coords_only_penalty)
            reasons.append("coords_only_penalty")
        # Si el segundo candidato no-coord está MUY cerca del primero,
        # es ambigüedad: bajamos confianza un poco para forzar confirm.
        delta = 0.0
        if runner_up is not None and runner_up.source not in EvidenceSource.NEVER_ALONE_STRONG:
            delta = float(top.score - runner_up.score)
            if 0.0 <= delta < 0.05:
                confidence = max(0.0, confidence - 0.10)
                reasons.append(
                    f"ambiguity_close_runner_up:{runner_up.source}:delta={round(delta,3)}"
                )

        # Outcome confirmado SOLO suma si ya hay anchor estructural.
        has_structural_anchor = top.source in EvidenceSource.STRONG_ELIGIBLE
        if outcome_confirmed and has_structural_anchor:
            confidence = min(1.0, confidence + self.outcome_bonus)
            reasons.append("outcome.confirmed_bonus")

        # 4. Construir identidad final desde el seed del top + parent_chain.
        merged_seed = dict(top.identity_seed or {})
        # Importante: si el top NO es UIA, igual queremos exponer el
        # UIA capturado como contexto (para resolver downstream),
        # SALVO que sea un GroupControl genérico (que ensucia identidad).
        if uia and "uia" not in merged_seed and not _is_generic_container(uia):
            merged_seed["uia"] = uia
        if dom and "web" not in merged_seed:
            merged_seed["web"] = dom
        if ocr and "ocr" not in merged_seed:
            merged_seed["ocr"] = ocr
        if visual and "visual" not in merged_seed:
            merged_seed["visual"] = visual

        evidence_sources = [c.source for c in candidates if c.score > 0.0]
        # Limpiamos coords si no son la única fuente — siguen en
        # candidates para auditoría, pero no inflamos identity.
        if non_coord_top is not None:
            evidence_sources = [
                s for s in evidence_sources
                if s not in EvidenceSource.NEVER_ALONE_STRONG
            ] + [s for s in evidence_sources if s in EvidenceSource.NEVER_ALONE_STRONG]

        identity = build_target_identity_from_capture(
            click_xy=click_xy,
            click_ms=click_ms,
            window_ctx=window_ctx,
            uia=merged_seed.get("uia"),
            web=merged_seed.get("web"),
            ocr=merged_seed.get("ocr"),
            visual=merged_seed.get("visual"),
            parent_chain=parent_chain,
            confidence=confidence,
            evidence_sources=evidence_sources,
            capture_ms=capture_ms,
        )
        # ``build_target_identity_from_capture`` ya aplicó clamp por
        # contenedor genérico / coords-only. Tomamos su confianza final.
        confidence = identity.confidence

        # 5. Decisión.
        if confidence >= CONFIDENCE_ACCEPT_THRESHOLD:
            decision = "accept"
        elif confidence >= CONFIDENCE_CONFIRM_THRESHOLD:
            decision = "confirm"
        else:
            decision = "ask"

        # 6. Ambigüedad: si decision != accept y aún no hay
        # ``identity.ambiguity``, generamos una razón legible.
        ambiguity = identity.ambiguity
        if ambiguity is None and decision != "accept":
            ambiguity = Ambiguity(
                code=(
                    "low_confidence_runner_up_close"
                    if delta and delta < 0.05
                    else "low_confidence"
                ),
                description=(
                    "Confianza insuficiente: ningún candidato supera el "
                    "umbral de aceptación."
                ),
                candidate_count=len(candidates),
                delta_score=round(delta, 4),
            )

        result = FusionResult(
            chosen=identity,
            confidence=confidence,
            decision=decision,
            ambiguity=ambiguity,
            candidates=[c.to_dict() for c in candidates],
            evidence_sources=evidence_sources,
            reasons=reasons,
        )
        return result

    def _ask_result(
        self,
        *,
        click_xy: Tuple[int, int],
        click_ms: int,
        window_ctx: Dict[str, Any],
        reason: str,
    ) -> FusionResult:
        identity = build_target_identity_from_capture(
            click_xy=click_xy, click_ms=click_ms, window_ctx=window_ctx,
            confidence=0.0, evidence_sources=[],
        )
        amb = Ambiguity(
            code=reason,
            description="No se pudo construir un target con evidencia.",
            candidate_count=0,
            delta_score=0.0,
        )
        return FusionResult(
            chosen=identity, confidence=0.0, decision="ask",
            ambiguity=amb, candidates=[], evidence_sources=[],
            reasons=[f"reason:{reason}"],
        )


__all__ = [
    "Candidate",
    "CandidateFusionEngine",
    "FusionResult",
]
