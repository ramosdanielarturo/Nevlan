"""
Nevlan — Coord Verifier (PRD 2026-05-12 §nivel-1)
==================================================

**Verificación estructural zero-capture** antes de bajar a visión.

Antes de tomar cualquier screenshot, Nevlan intenta confirmar que el
target grabado sigue donde se grabó **usando UIA y/o DOM**:

  * UIA hit-test contra la pre-recorded ``uia_data`` del step:
    matchea ``automation_id`` + ``control_type`` + ``bbox`` o
    ``name`` + ``control_type`` + ``click_inside_bbox``.

  * DOM hit-test contra la pre-recorded ``web_data`` del step:
    matchea ``role`` + ``aria-label`` / ``placeholder`` / ``innerText``.

Si cualquiera de los dos confirma con ``confidence >= 0.85``,
``VerificationResult.verified_strong = True`` y la capa visual
**se salta entera** (cero capturas, cero CPU de visión).

Política de targets débiles (PRD §10):
  * ``GroupControl`` / ``PaneControl`` **sin texto/automation_id** =
    insufficient. NUNCA promueve a verified_strong solo porque la
    bbox cuadre.

Diseño:
  * Pure-function, sin estado global.
  * Probes inyectables → testeable sin SO ni Playwright.
  * Tolerante a fallos: si el probe falla o no aplica, devolvemos
    ``verified=False`` con ``reason`` explícito en vez de crashear.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.logger import log


# ──────────────────────────────────────────────────────────────────────
# Tipos
# ──────────────────────────────────────────────────────────────────────


#: Probe UIA: ``(x, y) -> Optional[dict]`` con campos como
#: ``automation_id``, ``control_type``, ``name``, ``class_name``, ``bbox``.
UiaHitTestFn = Callable[[int, int], Optional[Dict[str, Any]]]

#: Probe DOM: ``(x, y) -> Optional[dict]`` con la forma estándar del
#: web_recorder: ``url``, ``role``, ``accessible_name``, ``locators``,
#: ``bbox``, ``tag``, ``placeholder``, ``inner_text``.
DomHitTestFn = Callable[[int, int], Optional[Dict[str, Any]]]


#: Umbrales por defecto. La regla del PRD: ≥ 0.85 = strong.
DEFAULT_STRONG_THRESHOLD: float = 0.85
DEFAULT_MEDIUM_THRESHOLD: float = 0.65


#: Control types que NO pueden ser verified_strong por sí solos sin
#: texto / automation_id (regla PRD §10).
WEAK_CONTROL_TYPES: Tuple[str, ...] = (
    "GroupControl", "PaneControl", "WindowControl",
    "CustomControl", "DocumentControl",
)


@dataclass
class VerificationResult:
    """Resultado estructurado de la verificación de un target en (x, y).

    Lo expone el módulo para que callers ricos (Player, debug overlay)
    sepan POR QUÉ se aceptó o rechazó la verificación. La pipeline
    visual solo usa el booleano ``verified_strong``.
    """

    verified: bool = False
    verified_strong: bool = False
    confidence: float = 0.0
    source: str = "none"       # "uia" | "dom" | "mixed" | "none"
    matched_fields: List[str] = field(default_factory=list)
    reason: str = ""
    bbox: Optional[Dict[str, int]] = None
    weak_reason: str = ""      # explica por qué quedó insufficient si aplica

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified": bool(self.verified),
            "verified_strong": bool(self.verified_strong),
            "confidence": round(float(self.confidence), 4),
            "source": str(self.source),
            "matched_fields": list(self.matched_fields),
            "reason": str(self.reason or ""),
            "bbox": dict(self.bbox) if self.bbox else None,
            "weak_reason": str(self.weak_reason or ""),
        }


# ──────────────────────────────────────────────────────────────────────
# Helpers — comparación de strings y bbox
# ──────────────────────────────────────────────────────────────────────


def _norm(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip().lower()


def _eq_norm(a: Any, b: Any) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na) and na == nb


def _bbox_compatible(
    expected: Optional[Dict[str, Any]],
    got: Optional[Dict[str, Any]],
    *,
    tolerance_px: int = 12,
) -> bool:
    """Devuelve True si las dos bboxes describen aproximadamente la
    misma región. Tolerancia generosa para acomodar re-layouts mínimos
    (DPI, fonts, sub-pixel rounding).
    """
    if not isinstance(expected, dict) or not isinstance(got, dict):
        return False
    try:
        e_w = int(expected.get("width", 0)) or (
            int(expected.get("right", 0)) - int(expected.get("left", 0))
        )
        e_h = int(expected.get("height", 0)) or (
            int(expected.get("bottom", 0)) - int(expected.get("top", 0))
        )
        g_w = int(got.get("width", 0)) or (
            int(got.get("right", 0)) - int(got.get("left", 0))
        )
        g_h = int(got.get("height", 0)) or (
            int(got.get("bottom", 0)) - int(got.get("top", 0))
        )
        if e_w <= 0 or e_h <= 0 or g_w <= 0 or g_h <= 0:
            return False
        # Tolerancia: tamaño debe estar dentro de ±tolerance + 25%.
        size_tol_w = max(tolerance_px, int(e_w * 0.25))
        size_tol_h = max(tolerance_px, int(e_h * 0.25))
        return abs(e_w - g_w) <= size_tol_w and abs(e_h - g_h) <= size_tol_h
    except Exception:
        return False


def _click_inside_bbox(
    x: int, y: int, bbox: Optional[Dict[str, Any]],
    *, slack_px: int = 4,
) -> bool:
    if not isinstance(bbox, dict):
        return False
    try:
        l = int(bbox.get("left", 0))
        t = int(bbox.get("top", 0))
        w = int(bbox.get("width", 0)) or (int(bbox.get("right", 0)) - l)
        h = int(bbox.get("height", 0)) or (int(bbox.get("bottom", 0)) - t)
        return (
            (l - slack_px) <= int(x) <= (l + w + slack_px)
            and (t - slack_px) <= int(y) <= (t + h + slack_px)
        )
    except Exception:
        return False


def _is_weak_uia_target(expected_uia: Dict[str, Any]) -> Tuple[bool, str]:
    """Heurística PRD §10: control_type tipo contenedor SIN
    automation_id NI name NI text NO es señal estructural fuerte.
    """
    ct = str(expected_uia.get("control_type", "") or "")
    aid = str(expected_uia.get("automation_id", "") or "").strip()
    name = str(expected_uia.get("name", "") or "").strip()
    if ct in WEAK_CONTROL_TYPES and not aid and not name:
        return True, f"control_type={ct!r} sin automation_id ni name"
    return False, ""


# ──────────────────────────────────────────────────────────────────────
# UIA verifier
# ──────────────────────────────────────────────────────────────────────


def verify_uia_at_xy(
    expected_uia: Dict[str, Any],
    x: int, y: int,
    *,
    uia_probe: UiaHitTestFn,
) -> VerificationResult:
    """Compara la UIA pre-grabada con la UIA actual en (x, y).

    Reglas (PRD §nivel-1):
      * ``automation_id`` exacto + ``control_type`` exacto + bbox
        compatible → confidence 0.95 → strong.
      * ``name``/``text`` exacto + ``control_type`` exacto +
        click_inside_bbox → confidence 0.88 → strong.
      * Solo bbox compatible → confidence 0.40 → weak.
      * Control_type genérico sin texto → insufficient.
    """
    res = VerificationResult(source="uia")
    if not isinstance(expected_uia, dict):
        res.reason = "no_expected_uia"
        return res

    weak, weak_reason = _is_weak_uia_target(expected_uia)
    if weak:
        res.weak_reason = weak_reason
        res.reason = "expected_target_is_weak_container"
        return res

    got: Optional[Dict[str, Any]] = None
    try:
        got = uia_probe(int(x), int(y))
    except Exception as e:
        res.reason = f"uia_probe_raised:{e}"
        return res
    if not isinstance(got, dict):
        res.reason = "uia_probe_returned_none"
        return res

    res.bbox = got.get("bbox") if isinstance(got.get("bbox"), dict) else None

    exp_aid = _norm(expected_uia.get("automation_id"))
    exp_name = _norm(expected_uia.get("name"))
    exp_ct = _norm(expected_uia.get("control_type"))

    got_aid = _norm(got.get("automation_id"))
    got_name = _norm(got.get("name"))
    got_ct = _norm(got.get("control_type"))

    bbox_compat = _bbox_compatible(expected_uia.get("bbox"), got.get("bbox"))
    inside = _click_inside_bbox(x, y, got.get("bbox"))

    matched: List[str] = []
    if exp_aid and exp_aid == got_aid:
        matched.append("automation_id")
    if exp_name and exp_name == got_name:
        matched.append("name")
    if exp_ct and exp_ct == got_ct:
        matched.append("control_type")
    if bbox_compat:
        matched.append("bbox")
    if inside:
        matched.append("click_inside_bbox")

    res.matched_fields = matched

    # Señal fuerte 1: automation_id + control_type + bbox compatible.
    if "automation_id" in matched and "control_type" in matched and bbox_compat:
        res.verified = True
        res.verified_strong = True
        res.confidence = 0.95
        res.reason = "automation_id+control_type+bbox"
        return res
    # Señal fuerte 2: name + control_type + click_inside_bbox.
    if "name" in matched and "control_type" in matched and inside:
        res.verified = True
        res.verified_strong = True
        res.confidence = 0.88
        res.reason = "name+control_type+click_inside_bbox"
        return res
    # Señal fuerte 3: automation_id solo (es un id único de por sí).
    if "automation_id" in matched:
        res.verified = True
        res.verified_strong = True
        res.confidence = 0.90
        res.reason = "automation_id_exact"
        return res
    # Señal media: control_type + (bbox o inside).
    if "control_type" in matched and (bbox_compat or inside):
        res.verified = True
        res.verified_strong = False
        res.confidence = 0.55
        res.reason = "control_type+geometry"
        return res
    # Señal débil: solo bbox.
    if bbox_compat:
        res.confidence = 0.40
        res.reason = "bbox_only_weak"
        return res
    res.reason = "no_match"
    return res


# ──────────────────────────────────────────────────────────────────────
# DOM verifier
# ──────────────────────────────────────────────────────────────────────


def verify_dom_at_xy(
    expected_web: Dict[str, Any],
    x: int, y: int,
    *,
    dom_probe: DomHitTestFn,
) -> VerificationResult:
    """Compara la huella DOM pre-grabada con el ``elementFromPoint``
    actual. Selectors estables (``aria-label``, ``placeholder``,
    ``role``+``text``) son señal fuerte; contenedores sin texto, débil.
    """
    res = VerificationResult(source="dom")
    if not isinstance(expected_web, dict):
        res.reason = "no_expected_web"
        return res

    got: Optional[Dict[str, Any]] = None
    try:
        got = dom_probe(int(x), int(y))
    except Exception as e:
        res.reason = f"dom_probe_raised:{e}"
        return res
    if not isinstance(got, dict):
        res.reason = "dom_probe_returned_none"
        return res

    res.bbox = got.get("bbox") if isinstance(got.get("bbox"), dict) else None

    exp_role = _norm(expected_web.get("role"))
    exp_aria = _norm(expected_web.get("aria_label") or expected_web.get("accessible_name"))
    exp_placeholder = _norm(expected_web.get("placeholder"))
    exp_text = _norm(expected_web.get("inner_text") or expected_web.get("accessible_name"))
    exp_tag = _norm(expected_web.get("tag"))
    exp_url = _norm(expected_web.get("url"))

    got_role = _norm(got.get("role"))
    got_aria = _norm(got.get("aria_label") or got.get("accessible_name"))
    got_placeholder = _norm(got.get("placeholder"))
    got_text = _norm(got.get("inner_text") or got.get("accessible_name"))
    got_tag = _norm(got.get("tag"))
    got_url = _norm(got.get("url"))

    inside = _click_inside_bbox(x, y, got.get("bbox"))

    matched: List[str] = []
    if exp_aria and exp_aria == got_aria:
        matched.append("aria_label")
    if exp_placeholder and exp_placeholder == got_placeholder:
        matched.append("placeholder")
    if exp_text and exp_text == got_text:
        matched.append("inner_text")
    if exp_role and exp_role == got_role:
        matched.append("role")
    if exp_tag and exp_tag == got_tag:
        matched.append("tag")
    if exp_url and exp_url and exp_url == got_url:
        matched.append("url")
    if inside:
        matched.append("click_inside_bbox")
    res.matched_fields = matched

    # URL/dominio cambió → invalida implícito (regla DOM verifier).
    if exp_url and got_url and exp_url != got_url:
        res.reason = "url_changed"
        res.confidence = 0.0
        return res

    # Señal fuerte 1: aria-label + role + click_inside_bbox.
    if "aria_label" in matched and "role" in matched and inside:
        res.verified = True
        res.verified_strong = True
        res.confidence = 0.92
        res.reason = "aria_label+role+inside"
        return res
    # Señal fuerte 2: placeholder + role.
    if "placeholder" in matched and ("role" in matched or "tag" in matched):
        res.verified = True
        res.verified_strong = True
        res.confidence = 0.90
        res.reason = "placeholder+role"
        return res
    # Señal fuerte 3: inner_text exacto + role/tag + inside.
    if "inner_text" in matched and ("role" in matched or "tag" in matched) and inside:
        res.verified = True
        res.verified_strong = True
        res.confidence = 0.88
        res.reason = "inner_text+role+inside"
        return res
    # Señal media: role + inside.
    if "role" in matched and inside:
        res.verified = True
        res.verified_strong = False
        res.confidence = 0.60
        res.reason = "role+inside_only"
        return res
    # Débil: solo inside o solo tag.
    if inside:
        res.confidence = 0.40
        res.reason = "inside_only_weak"
        return res
    res.reason = "no_match"
    return res


# ──────────────────────────────────────────────────────────────────────
# Compuesto: UIA + DOM mixto
# ──────────────────────────────────────────────────────────────────────


def verify_target_at_xy(
    *,
    expected_uia: Optional[Dict[str, Any]] = None,
    expected_web: Optional[Dict[str, Any]] = None,
    x: int, y: int,
    uia_probe: Optional[UiaHitTestFn] = None,
    dom_probe: Optional[DomHitTestFn] = None,
) -> VerificationResult:
    """Verifica que el target esperado siga en (x, y) usando todos los
    probes disponibles.

    Estrategia:
      1. Si hay ``expected_web`` y ``dom_probe`` → DOM verifier.
         DOM gana sobre UIA en navegadores porque es más preciso.
      2. Si no funcionó, o no aplica, → UIA verifier.
      3. Si ambos verifican strong, source="mixed" y confidence sube.

    Returns:
        :class:`VerificationResult`. ``verified_strong=True`` ⇒ el
        target sigue donde se grabó con alta confianza ⇒ la capa
        visual puede saltarse.
    """
    dom_res = VerificationResult()
    uia_res = VerificationResult()

    if expected_web and dom_probe is not None:
        try:
            dom_res = verify_dom_at_xy(
                expected_web, int(x), int(y), dom_probe=dom_probe,
            )
        except Exception as e:
            log.debug(f"verify_dom_at_xy raised: {e}")

    if expected_uia and uia_probe is not None:
        try:
            uia_res = verify_uia_at_xy(
                expected_uia, int(x), int(y), uia_probe=uia_probe,
            )
        except Exception as e:
            log.debug(f"verify_uia_at_xy raised: {e}")

    # Compuesto: si ambos verifican strong, máxima confianza.
    if dom_res.verified_strong and uia_res.verified_strong:
        combined = VerificationResult(
            verified=True, verified_strong=True,
            confidence=min(0.99, max(dom_res.confidence, uia_res.confidence) + 0.05),
            source="mixed",
            matched_fields=list(set(dom_res.matched_fields + uia_res.matched_fields)),
            reason=f"dom:{dom_res.reason}|uia:{uia_res.reason}",
            bbox=dom_res.bbox or uia_res.bbox,
        )
        return combined

    # Cualquier strong individual gana.
    if dom_res.verified_strong:
        return dom_res
    if uia_res.verified_strong:
        return uia_res
    # Si uno es medio y el otro débil/no-match, devolvemos el mejor.
    best = dom_res if dom_res.confidence >= uia_res.confidence else uia_res
    if not best.source or best.source == "none":
        best.source = "uia" if expected_uia else "dom" if expected_web else "none"
    if not best.reason:
        best.reason = "no_match_either"
    return best


# ──────────────────────────────────────────────────────────────────────
# Bool adapter para el progressive-vision pipeline
# ──────────────────────────────────────────────────────────────────────


def make_bool_coord_verifier(
    *,
    expected_uia: Optional[Dict[str, Any]] = None,
    expected_web: Optional[Dict[str, Any]] = None,
    uia_probe: Optional[UiaHitTestFn] = None,
    dom_probe: Optional[DomHitTestFn] = None,
    threshold: float = DEFAULT_STRONG_THRESHOLD,
) -> Callable[[int, int], bool]:
    """Devuelve un ``Callable[(x, y) -> bool]`` listo para inyectar en
    :func:`locate_with_progressive_vision`. La pipeline activa el
    nivel-1 zero-capture cuando esta función devuelve ``True``.
    """
    def _verifier(x: int, y: int) -> bool:
        try:
            r = verify_target_at_xy(
                expected_uia=expected_uia,
                expected_web=expected_web,
                x=int(x), y=int(y),
                uia_probe=uia_probe,
                dom_probe=dom_probe,
            )
            return bool(r.verified_strong and r.confidence >= threshold)
        except Exception:
            return False
    return _verifier


__all__ = [
    "DEFAULT_STRONG_THRESHOLD",
    "DEFAULT_MEDIUM_THRESHOLD",
    "WEAK_CONTROL_TYPES",
    "VerificationResult",
    "verify_uia_at_xy",
    "verify_dom_at_xy",
    "verify_target_at_xy",
    "make_bool_coord_verifier",
]
