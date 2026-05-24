"""Semantic ambiguity resolution — decidir revisión sin reglas por app o tipo de paso.

Combina evidencia textual (params canónicos + ``human_label``) con métricas
genéricas (longitud útil, entropía, stopwords, señales de fidelidad) y con
:class:`~execution_truth_engine` cuando la captura ya está confirmada.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.missions.execution_truth_engine import is_execution_truth_confirmed

#
# Strings alineadas con semantic_execution_plan.ReadyBlockerCode (sin import
# tardío circular).
#
RB_EMPTY_PROFILE = "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE"
RB_EMPTY_QUERY = "EMPTY_QUERY_IN_SEARCH"
RB_EMPTY_URL = "EMPTY_URL_IN_OPEN_URL"
RB_EMPTY_APP = "EMPTY_APP_IN_OPEN_APP"
RB_QUERY_MISSING_SHORT_TOKEN = "QUERY_MISSING_SHORT_TOKEN"
RB_QUERY_FIDELITY_SUSPECT = "QUERY_FIDELITY_SUSPECT"
RB_UNDER_SPECIFIED = "SEMANTIC_INTENT_UNDER_SPECIFIED"
RB_MULTI_TARGET = "SEMANTIC_INTENT_MULTI_TARGET"


_INTENT_STRING_KEYS: Tuple[str, ...] = (
    "profile_name",
    "profile",
    "query",
    "search",
    "keyword",
    "url",
    "name",
    "title",
    "path",
    "filepath",
    "file",
    "recipient",
    "to",
    "cc",
    "bcc",
    "subject",
    "body",
    "message",
    "text",
    "command",
    "transaction",
    "tcode",
    "alias",
    "site",
    "doc_id",
    "document_id",
)

_IGNORE_PARAM_KEYS_FOR_AMBIGUITY: frozenset = frozenset(
    {"method", "direction", "amount", "context", "provider", "engine"},
)

_STOP: frozenset = frozenset({
    "el", "la", "los", "las", "un", "una", "de", "del", "al",
    "en", "y", "o", "que", "con", "por", "para", "sin", "sobre", "como",
    "muy", "más", "menos", "yo", "tú", "mi", "tu", "su", "esto", "ese",
    "ser", "es", "son", "estar", "está", "lo", "le", "se", "hay", "algo",
    "the", "a", "an", "to", "of", "and", "or", "in", "on", "for", "with",
    "from", "as", "at", "by", "is", "are", "it", "that", "this", "have",
    "has", "not", "so", "if",
})

_META_EXACT: frozenset = frozenset({
    "perfil",
    "el perfil",
    "usuario",
    "user",
    "archivo",
    "file",
    "fichero",
    "sitio",
    "sitio web",
    "app",
    "aplicación",
    "aplicacion",
    "algo",
    "sitio.",
    "default",
    "unknown",
    "navegador",
    "browser",
})

_UNDER_SPEC_SINGLE: frozenset = frozenset({
    "app", "file", "archivo", "fichero", "user", "usuario", "perfil",
    "profile", "sitio", "site", "web", "algo", "pagina",
})

_TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)


def _is_placeholder_profile_text(value: str) -> bool:
    """Espacio de etiquetas META para perfiles — alineado con
    ``is_generic_profile_label`` pero sin dependencia circular.
    """
    if not value or not value.strip():
        return True
    norm = " ".join(value.strip().lower().split())
    if not norm:
        return True
    generics = (
        "perfil del navegador",
        "perfil del browser",
        "el perfil",
        "perfil",
        "profile",
        "browser profile",
        "default",
        "unknown",
        "user",
        "usuario",
    )
    if norm in generics:
        return True
    if norm.startswith(("perfil ", "el perfil ")) and len(norm) <= 14:
        return True
    return False


def _norm_spaces(text: str) -> str:
    s = unicodedata.normalize("NFC", (text or "").strip().lower())
    return " ".join(s.split())


def _fold(tok: str) -> str:
    return "".join(ch for ch in tok if ch.isalnum()).lower()


def _entropy_alnum(norm: str) -> float:
    alnum = "".join(ch for ch in norm if ch.isalnum())
    if not alnum:
        return 0.0
    ctr = Counter(alnum.lower())
    n = len(alnum)
    h = 0.0
    for c in ctr.values():
        p = c / n
        h -= p * math.log2(p)
    return h


def _meaning_tokens(text: str) -> List[str]:
    raw = _TOKEN_RE.findall(_norm_spaces(text))
    out: List[str] = []
    for t in raw:
        lf = _fold(t)
        if lf and lf not in _STOP:
            out.append(lf)
    return out


def _under_spec(norm: str, tokens: List[str]) -> Tuple[float, List[str]]:
    reasons: List[str] = []
    if not norm:
        return 1.0, ["empty_carrier"]
    stripped = "".join(norm.split())
    if norm in _META_EXACT or stripped in {_fold(x) for x in _META_EXACT}:
        return 0.93, reasons + ["meta_placeholder_exact"]

    lowered = norm
    vague_phrases = (
        "buscar algo",
        "search something",
        "cualquier archivo",
        "abrir archivo",
        "abrir algo",
    )
    for vp in vague_phrases:
        if vp in lowered:
            return 0.9, reasons + ["generic_phrase_template"]

    if not tokens:
        return 0.88, reasons + ["only_stopwords"]

    if len(tokens) == 1:
        t0 = tokens[0]
        if t0 in _UNDER_SPEC_SINGLE or len(t0) <= 2:
            return 0.92, reasons + ["underspecified_singleton"]

    if len(tokens) == 1 and len(stripped) <= 3:
        return 0.86, reasons + ["very_short_singleton"]

    return 0.0, reasons


def _role_to_block(role: str) -> str:
    if role in ("profile_name", "profile"):
        return RB_EMPTY_PROFILE
    if role in ("query", "search", "keyword"):
        return RB_EMPTY_QUERY
    if role in ("url", "alias", "site"):
        return RB_EMPTY_URL
    if role == "name":
        return RB_EMPTY_APP
    return RB_UNDER_SPECIFIED


def _fidelity_block(role: str, fid_norm: str) -> List[str]:
    out: List[str] = []
    if role not in ("query", "search", "keyword"):
        return out
    if fid_norm == "missing_short_token":
        out.append(RB_QUERY_MISSING_SHORT_TOKEN)
        return out
    if fid_norm and fid_norm not in ("ok", "none"):
        out.append(RB_QUERY_FIDELITY_SUSPECT)
    return out


def _score_carrier(
    text: str,
    *,
    fidelity_for_query: Optional[str],
    role_for_fidelity: str,
) -> Tuple[float, float, List[str], bool, List[str]]:
    norm = _norm_spaces(text)
    mtoks = _meaning_tokens(text)
    uscore, ure = _under_spec(norm, mtoks)
    ent = _entropy_alnum(norm)

    specificity = 0.0
    if len(mtoks) >= 5:
        specificity += 0.2
    elif len(mtoks) >= 4:
        specificity += 0.14
    elif len(mtoks) >= 3:
        specificity += 0.07
    if any(len(x) >= 9 for x in mtoks):
        specificity += 0.08
    if ent >= 3.45:
        specificity += 0.07

    specificity = min(specificity, 0.45)

    fidelity_blocks: List[str] = []
    fid = str(fidelity_for_query or "").strip().lower()
    fidelity_penalty = 0.0
    fid_reasons: List[str] = []
    if role_for_fidelity in ("query", "search", "keyword"):
        fidelity_blocks = _fidelity_block(role_for_fidelity, fid)
        if fidelity_blocks:
            fid_reasons.extend(
                ["fidelity_signal:{}".format(fid or "unset")],
            )
        if RB_QUERY_MISSING_SHORT_TOKEN in fidelity_blocks:
            fidelity_penalty = 0.6
        elif RB_QUERY_FIDELITY_SUSPECT in fidelity_blocks:
            fidelity_penalty = 0.38

    ambiguity = max(0.0, min(1.0, uscore + fidelity_penalty - specificity))

    ambiguous = ambiguity >= 0.42 or uscore >= 0.82 or bool(fidelity_blocks)
    semantics = max(0.0, min(1.0, 1.0 - ambiguity))

    reasons = ure + fid_reasons
    return ambiguity, semantics, reasons, ambiguous, fidelity_blocks


@dataclass
class SemanticAmbiguityResult:
    is_ambiguous: bool
    ambiguity_score: float
    ambiguity_reasons: List[str]
    requires_human_confirmation: bool
    resolved_semantic_value: Optional[str]
    semantic_confidence: float
    multiple_possible_targets: bool
    target_count_estimate: Optional[int]
    execution_truth_considered: bool
    promoted_to_clear_intent: bool
    suppress_review_flags: List[str]
    ambiguous_carrier_roles: List[str] = field(default_factory=list)
    readiness_blockers: List[str] = field(default_factory=list, repr=False)

    def audit_slice(self) -> Dict[str, Any]:
        return {
            "is_ambiguous": bool(self.is_ambiguous),
            "ambiguity_score": round(float(self.ambiguity_score), 4),
            "ambiguity_reasons": list(self.ambiguity_reasons),
            "requires_human_confirmation": bool(self.requires_human_confirmation),
            "resolved_semantic_value": self.resolved_semantic_value,
            "semantic_confidence": round(float(self.semantic_confidence), 4),
            "multiple_possible_targets": bool(self.multiple_possible_targets),
            "target_count_estimate": self.target_count_estimate,
            "execution_truth_considered": bool(self.execution_truth_considered),
            "promoted_to_clear_intent": bool(self.promoted_to_clear_intent),
            "suppress_review_flags": list(self.suppress_review_flags),
            "ambiguous_carrier_roles": list(self.ambiguous_carrier_roles),
        }


def _extract_carriers(step: Dict[str, Any]) -> List[Tuple[str, str]]:
    params = step.get("params") if isinstance(step.get("params"), dict) else {}
    out: List[Tuple[str, str]] = []
    for key in sorted(_INTENT_STRING_KEYS):
        if key in _IGNORE_PARAM_KEYS_FOR_AMBIGUITY:
            continue
        if key not in params:
            continue
        v = params.get(key)
        if isinstance(v, str):
            out.append((key, v))

    hl = str(step.get("human_label") or "").strip()
    if hl:
        out.append(("_human_label", hl))
    return out


def _multiplicity(step: Dict[str, Any]) -> Tuple[bool, Optional[int]]:
    params = step.get("params") if isinstance(step.get("params"), dict) else {}
    for key in (
        "candidate_count",
        "equiv_target_count",
        "alternate_target_count",
    ):
        raw = params.get(key)
        if isinstance(raw, (int, float)) and raw > 1:
            return True, int(raw)
        if isinstance(raw, str) and raw.isdigit() and int(raw) > 1:
            return True, int(raw)
    if params.get("ambiguous_equivalence") is True:
        return True, 2
    return False, None


def _eval_carrier_slice(
    role: str,
    text: str,
    *,
    step: Dict[str, Any],
    mission: Optional[Any],
    query_fidelity: Optional[str],
) -> SemanticAmbiguityResult:
    """Analiza un único carrier (clave de param o ``_human_label``)."""
    et = bool(mission is not None and is_execution_truth_confirmed(mission))
    fidelity_role = (
        role if role in ("query", "search", "keyword") else "_none"
    )
    eff_text = (
        ""
        if role in ("profile_name", "profile")
        and _is_placeholder_profile_text(text)
        else text
    )

    ambiguity, semantics, rsns, ambig0, fidelity_blocks = _score_carrier(
        eff_text,
        fidelity_for_query=(
            query_fidelity if fidelity_role != "_none" else None
        ),
        role_for_fidelity=fidelity_role,
    )
    if (
        role in ("profile_name", "profile")
        and _is_placeholder_profile_text(text)
        and eff_text != text
    ):
        rsns = ["generic_profile_placeholder"] + list(rsns)

    empty = not (eff_text or "").strip()
    suppress: List[str] = []

    mt, mc = _multiplicity(step)

    fidelity_requires = bool(fidelity_blocks)

    ambiguous = ambig0 or empty or fidelity_requires or mt
    if mt:
        rsns.append("multi_target_signal")
        ambiguity = max(ambiguity, 0.78)

    if empty:
        ambiguity = max(ambiguity, 0.94)
        semantics = min(semantics, 0.12)

    requires = (
        fidelity_requires
        or empty
        or ambiguity >= 0.55
        or semantics < 0.36
        or mt
    )

    promoted = False
    if (
        et
        and requires
        and not empty
        and not fidelity_requires
        and not mt
        and semantics >= 0.6
        and ambiguity < 0.48
    ):
        promoted = True
        requires = False
        ambiguous = False
        suppress.append("execution_truth_overlap_promotion")

    elif et:
        suppress.extend(
            [
                "suppress_zero_capture_visual_noise",
                "suppress_legacy_hwnd_title_noise",
                "suppress_review_artificial_visual",
                "suppress_click_fallback_history_noise",
            ],
        )
        if not fidelity_requires and not empty and not promoted:
            calibrated = max(0.0, ambiguity - 0.16)
            if calibrated < ambiguity:
                rsns.append("execution_truth_calibration")
            ambiguity = calibrated
            semantics = max(semantics, 1.0 - ambiguity)
            if (
                ambiguity < 0.44
                and semantics >= 0.58
                and not mt
                and not fidelity_requires
            ):
                requires = False
                ambiguous = False

    if (
        semantics >= 0.64
        and ambiguity < 0.4
        and not fidelity_requires
        and not empty
        and not mt
    ):
        ambiguous = False
        requires = bool(fidelity_requires)

    blocker_roles: List[str] = []
    readiness: List[str] = []
    uniq_fid = list(dict.fromkeys(fidelity_blocks))

    if mt:
        readiness.append(RB_MULTI_TARGET)
        blocker_roles.append("__multiplicity__")

    if requires:
        readiness.extend(uniq_fid)
        if empty:
            readiness.append(RB_UNDER_SPECIFIED if role == "_human_label" else _role_to_block(role))
            blocker_roles.append(role)
        elif fidelity_requires:
            blocker_roles.extend(uniq_fid)
        elif ambig0 and not fidelity_requires:
            code = RB_UNDER_SPECIFIED if role == "_human_label" else _role_to_block(role)
            readiness.append(code)
            blocker_roles.append(role)

        readiness = list(dict.fromkeys(readiness))

    return SemanticAmbiguityResult(
        is_ambiguous=bool(ambiguous),
        ambiguity_score=float(ambiguity),
        ambiguity_reasons=rsns,
        requires_human_confirmation=bool(requires),
        resolved_semantic_value=(eff_text.strip() or None),
        semantic_confidence=float(semantics),
        multiple_possible_targets=bool(mt),
        target_count_estimate=mc if mt else None,
        execution_truth_considered=et,
        promoted_to_clear_intent=promoted,
        suppress_review_flags=suppress,
        ambiguous_carrier_roles=list(dict.fromkeys(blocker_roles)),
        readiness_blockers=list(dict.fromkeys(readiness)),
    )


def _merge_slices(rows: Sequence[SemanticAmbiguityResult]) -> SemanticAmbiguityResult:
    if not rows:
        return SemanticAmbiguityResult(
            is_ambiguous=False,
            ambiguity_score=0.0,
            ambiguity_reasons=[],
            requires_human_confirmation=False,
            resolved_semantic_value=None,
            semantic_confidence=1.0,
            multiple_possible_targets=False,
            target_count_estimate=None,
            execution_truth_considered=False,
            promoted_to_clear_intent=False,
            suppress_review_flags=[],
            readiness_blockers=[],
        )
    amb = max(r.ambiguity_score for r in rows)
    conf_min = min(r.semantic_confidence for r in rows)
    reqs = any(r.requires_human_confirmation for r in rows)
    et_any = any(r.execution_truth_considered for r in rows)
    prom = any(r.promoted_to_clear_intent for r in rows)
    mt = any(r.multiple_possible_targets for r in rows)
    tgt = [r.target_count_estimate for r in rows if isinstance(r.target_count_estimate, int)]
    reasons: List[str] = []
    supp: List[str] = []
    roles: List[str] = []
    ready: List[str] = []
    resolved_bits: List[str] = []

    for r in rows:
        reasons.extend(r.ambiguity_reasons)
        supp.extend(r.suppress_review_flags)
        roles.extend(r.ambiguous_carrier_roles)
        ready.extend(r.readiness_blockers)
        if r.resolved_semantic_value:
            resolved_bits.append(r.resolved_semantic_value)

    return SemanticAmbiguityResult(
        is_ambiguous=(amb >= 0.42) or reqs or mt,
        ambiguity_score=float(amb),
        ambiguity_reasons=list(dict.fromkeys(reasons)),
        requires_human_confirmation=bool(reqs),
        resolved_semantic_value=(" ".join(resolved_bits).strip() or None),
        semantic_confidence=float(conf_min),
        multiple_possible_targets=bool(mt),
        target_count_estimate=max(tgt) if tgt else None,
        execution_truth_considered=et_any,
        promoted_to_clear_intent=prom,
        suppress_review_flags=sorted(dict.fromkeys(supp)),
        ambiguous_carrier_roles=list(dict.fromkeys(roles)),
        readiness_blockers=list(dict.fromkeys(ready)),
    )


def analyze_semantic_step_dict(
    step: Dict[str, Any],
    mission: Any,
    *,
    step_index: int = -1,
) -> SemanticAmbiguityResult:
    carriers = _extract_carriers(step)

    fidelity = ""
    pars = step.get("params") if isinstance(step.get("params"), dict) else {}
    if isinstance(pars, dict):
        fidelity = str(pars.get("query_fidelity_status") or "")

    mult, _multc = _multiplicity(step)
    if mult and not carriers:
        et = mission is not None and is_execution_truth_confirmed(mission)
        return SemanticAmbiguityResult(
            is_ambiguous=True,
            ambiguity_score=0.9,
            ambiguity_reasons=["ambiguous_multiplicity_without_text"],
            requires_human_confirmation=True,
            resolved_semantic_value=None,
            semantic_confidence=0.2,
            multiple_possible_targets=True,
            target_count_estimate=_multc,
            execution_truth_considered=et,
            promoted_to_clear_intent=False,
            suppress_review_flags=[],
            ambiguous_carrier_roles=["__multiplicity__"],
            readiness_blockers=[RB_MULTI_TARGET],
        )

    if not carriers:
        et = mission is not None and is_execution_truth_confirmed(mission)
        return SemanticAmbiguityResult(
            is_ambiguous=False,
            ambiguity_score=0.04,
            ambiguity_reasons=(
                ["no_text_carrier"] if step_index >= 0 else []
            ),
            requires_human_confirmation=bool(mult),
            resolved_semantic_value=None,
            semantic_confidence=0.93 if et else 0.86,
            multiple_possible_targets=mult,
            target_count_estimate=_multc,
            execution_truth_considered=et,
            promoted_to_clear_intent=bool(et),
            suppress_review_flags=(["pure_navigation_carrier"] if not mult else []),
            ambiguous_carrier_roles=[],
            readiness_blockers=(
                [RB_MULTI_TARGET] if mult else []
            ),
        )

    slices = [
        _eval_carrier_slice(role, txt, step=step, mission=mission, query_fidelity=fidelity)
        for role, txt in carriers
    ]
    agg = _merge_slices(slices)
    agg = replace(agg, ambiguity_reasons=sorted(dict.fromkeys(agg.ambiguity_reasons)))

    ice_flag = bool(step.get("needs_user_label"))
    if ice_flag and not agg.requires_human_confirmation:
        nf = sorted(dict.fromkeys(agg.suppress_review_flags))
        nf.append("needs_user_label_suppressed_when_intent_clear")
        agg.suppress_review_flags = list(nf)

    return agg


def evaluate_plan_semantic_ambiguity(
    plan: Any,
    mission: Any,
) -> List[SemanticAmbiguityResult]:
    steps = getattr(plan, "steps", None)
    seq: Sequence[Any]
    seq = steps if isinstance(steps, list) else []
    out: List[SemanticAmbiguityResult] = []

    for i, raw in enumerate(seq):
        d: Dict[str, Any]
        if isinstance(raw, dict):
            d = raw
        else:
            try:
                d = raw.to_dict()  # type: ignore[attr-defined]
            except Exception:
                continue
        out.append(analyze_semantic_step_dict(d, mission, step_index=i))
    return out


def plan_requires_human_confirmation(plan: Any, mission: Any) -> bool:
    return any(r.requires_human_confirmation for r in evaluate_plan_semantic_ambiguity(plan, mission))


def aggregate_plan_ambiguity(rows: Sequence[SemanticAmbiguityResult]) -> SemanticAmbiguityResult:
    return _merge_slices(rows)


def attach_semantic_ambiguity_audit_to_mission(
    mission: Any,
    *,
    step_results: Sequence[SemanticAmbiguityResult],
    plan_aggregate: SemanticAmbiguityResult,
) -> Dict[str, Any]:
    payload = {
        "aggregate": plan_aggregate.audit_slice(),
        "requires_human_confirmation": bool(
            plan_aggregate.requires_human_confirmation,
        ),
        "steps": [
            {"step_index": i, **r.audit_slice()}
            for i, r in enumerate(step_results)
        ],
    }
    try:
        setattr(mission, "_semantic_ambiguity_audit", payload)
    except Exception:
        pass
    return payload


def apply_ambiguity_based_needs_label_relief(
    plan: Any,
    mission: Any,
    step_results: Sequence[SemanticAmbiguityResult],
) -> None:
    """Limpia ``needs_user_label`` espurio en :class:`SemanticPlanStep`.

    Solo muta en memoria; el llamador debe serializar si persiste."""
    seq = getattr(plan, "steps", None)
    if not isinstance(seq, list):
        return
    for row, sar in zip(seq, step_results):
        try:
            if not getattr(row, "needs_user_label", False):
                continue
        except Exception:
            continue
        if sar.requires_human_confirmation:
            continue
        try:
            row.needs_user_label = False
            row.label_prompt = ""
        except Exception:
            continue


def profile_carrier_needs_confirmation(
    step_blob: Dict[str, Any],
    mission: Any,
) -> bool:
    """UI: confirmación sólo cuando el paso porta keys de perfil y sigue ambiguo."""
    params = step_blob.get("params") if isinstance(step_blob.get("params"), dict) else {}
    touches_profile_keys = isinstance(params, dict) and (
        "profile_name" in params or "profile" in params
    )
    if not touches_profile_keys:
        return False
    sar = analyze_semantic_step_dict(step_blob, mission, step_index=0)
    return bool(sar.requires_human_confirmation)


def apply_semantic_ambiguity_relief(plan: Any, mission: Any) -> SemanticAmbiguityResult:
    """Ejecuta evaluación + alivio de ``needs_user_label``. Devuelve agregado."""
    rows = evaluate_plan_semantic_ambiguity(plan, mission)
    agg = aggregate_plan_ambiguity(rows)
    apply_ambiguity_based_needs_label_relief(plan, mission, rows)
    attach_semantic_ambiguity_audit_to_mission(
        mission,
        step_results=rows,
        plan_aggregate=agg,
    )
    return agg


__all__ = [
    "SemanticAmbiguityResult",
    "analyze_semantic_step_dict",
    "evaluate_plan_semantic_ambiguity",
    "aggregate_plan_ambiguity",
    "plan_requires_human_confirmation",
    "attach_semantic_ambiguity_audit_to_mission",
    "apply_semantic_ambiguity_relief",
    "apply_ambiguity_based_needs_label_relief",
    "profile_carrier_needs_confirmation",
    # Blocker literals (mirror ReadyBlockerCode strings)
    "RB_UNDER_SPECIFIED",
    "RB_MULTI_TARGET",
]

