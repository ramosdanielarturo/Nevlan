"""
Nevlan — Semantic Execution Confidence (PRD 2026-05-06b §1)
============================================================

Calcula la confianza de ejecución de un :class:`CompiledStep` basado
en la **intención semántica** del paso (kind → estrategia preferida),
NO en la calidad visual del capture. Los ``CLICK`` también combinan
tipo de control (estabilidad), identidad promovida (``semantic_target`` /
``uia_control`` vs ``click_fallback``), huella visual y historial
``last_success_score`` cuando existen — capa contextual separada de
la tabla base por ``ActionStrategy``. Esto reemplaza el viejo modelo
``capture_score`` como única señal de confianza:

  * ``open_app`` con app_name → CTRL+L / Windows Search (95%+).
  * ``open_new_tab`` con app=chrome → CTRL+T (95%+).
  * ``open_url`` con URL conocida → CTRL+L (90%+).
  * ``search_youtube`` con query → DOM/UIA/OCR robust (95%+).
  * ``select_profile`` con profile no vacío → text match (85%+).
  * ``scroll_results`` con direction → scroll dinámico (85%+).
  * ``click`` con automation_id UIA → UIA invoke (85%+).

Cuando el campo crítico falta (app_name vacío, query vacía, URL
vacía, profile vacío, target click vacío), generamos un
**hard-blocker** con ``block_code`` específico que el ApprovalGate
sabe mapear a NEEDS_REVIEW.

Side-effect: cada llamada persiste en ``step.action_payload``:

  * ``execution_confidence`` (porcentaje int)
  * ``preferred_strategy`` (ExecStrategy)
  * ``execution_level`` (ExecLevel)
  * ``execution_blocker`` (opcional, solo si is_blocking)

para que la UI y otros consumidores no tengan que recomputar.

Compatibilidad: este es el módulo "legacy" del execution confidence.
El nuevo tri-state (SAFE_TO_EXECUTE / SAFE_WITH_CONFIRMATION /
UNSAFE_DO_NOT_CLICK) vive en :mod:`execution_decision`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


__all__ = [
    "EXEC_GREEN_THRESHOLD",
    "EXEC_AMBER_THRESHOLD",
    "ExecLevel",
    "ExecStrategy",
    "ExecutionConfidenceResult",
    "MissionExecutionReport",
    "compute_execution_confidence",
    "compute_mission_execution_confidence",
    "execution_confidence_color",
]


# ──────────────────────────────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────────────────────────────

#: Umbral mínimo para nivel GREEN (confianza ≥ 80% ⇒ verde).
EXEC_GREEN_THRESHOLD: float = 0.80
#: Umbral mínimo para nivel AMBER (confianza ≥ 0.50 ⇒ ámbar).
EXEC_AMBER_THRESHOLD: float = 0.50


# ──────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────


class ExecLevel(str, Enum):
    """Nivel macro de confianza. Pintado por la UI como semáforo."""
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class ExecStrategy(str, Enum):
    """Estrategia semántica preferida para ejecutar un step.

    Las estrategias NO son "cómo clickear coordenadas" sino "cómo
    realizar la intención de forma robusta". Una sola estrategia puede
    materializarse vía atajo de teclado + UIA + DOM + scroll dinámico
    según contexto.
    """
    WINDOWS_SEARCH = "windows_search"        # open_app vía Windows Search
    PROFILE_NAME_MATCH = "profile_name_match"  # select_profile por nombre
    CTRL_T = "ctrl_t"                          # open_new_tab atajo
    CTRL_L_URL = "ctrl_l_url"                  # open_url vía Ctrl+L
    DOM_UIA_OCR = "dom_uia_ocr"                # search_site multimétodo
    SCROLL_DYNAMIC = "scroll_dynamic"          # scroll_results
    UIA_AUTOMATION_ID = "uia_automation_id"    # click con automation_id
    UIA_NAME = "uia_name"                       # click por Name UIA
    WEB_LOCATOR = "web_locator"                # click vía Playwright
    HOTKEY_DIRECT = "hotkey_direct"            # SEND_HOTKEY
    TYPE_FIELD_VALUE = "type_field_value"      # SET_FIELD_VALUE / TYPE_TEXT
    UNKNOWN = "unknown"


# ──────────────────────────────────────────────────────────────────────
# Resultado por step
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ExecutionConfidenceResult:
    """Resultado para un único step."""

    confidence: float = 0.0           # 0.0 - 1.0
    level: ExecLevel = ExecLevel.RED
    preferred_strategy: ExecStrategy = ExecStrategy.UNKNOWN
    is_executable: bool = False        # True si la estrategia puede correr
    is_blocking: bool = False          # True si bloquea el approval gate
    block_code: str = ""               # "empty_app" | "empty_url" | ...
    reasons: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def percent(self) -> int:
        return int(round(self.confidence * 100))


# ──────────────────────────────────────────────────────────────────────
# Resultado por misión
# ──────────────────────────────────────────────────────────────────────


@dataclass
class MissionExecutionReport:
    steps: List[ExecutionConfidenceResult] = field(default_factory=list)
    average: float = 0.0
    executable_count: int = 0
    blocking_count: int = 0

    @property
    def can_test(self) -> bool:
        return self.blocking_count == 0


# ──────────────────────────────────────────────────────────────────────
# Helpers de payload
# ──────────────────────────────────────────────────────────────────────


def _persist_to_payload(step: Any, r: ExecutionConfidenceResult) -> None:
    """Persiste el resultado en ``step.action_payload`` para que la UI
    y el ApprovalGate lo lean sin recomputar."""
    pl = getattr(step, "action_payload", None)
    if pl is None:
        return
    try:
        pl["execution_confidence"] = r.percent
        pl["preferred_strategy"] = r.preferred_strategy
        pl["execution_level"] = r.level
        if r.is_blocking and r.block_code:
            pl["execution_blocker"] = r.block_code
        else:
            # Si quedó sin blocker, limpiamos para no dejar stale.
            pl.pop("execution_blocker", None)
    except Exception:
        pass


def _level_from_confidence(c: float) -> ExecLevel:
    if c >= EXEC_GREEN_THRESHOLD:
        return ExecLevel.GREEN
    if c >= EXEC_AMBER_THRESHOLD:
        return ExecLevel.AMBER
    return ExecLevel.RED


def _ghost_override(payload: Dict[str, Any], r: ExecutionConfidenceResult) -> ExecutionConfidenceResult:
    """Aplica el override del Ghost Simulator (PRD anterior):

      * ``ghost_status="semantic_override"`` → forzamos GREEN aunque el
        target original sea débil. La estrategia semántica resuelve.
      * ``ghost_status="soft_warning"`` → bajamos a AMBER pero seguimos
        ejecutables; la UI muestra warning.
    """
    gs = str(payload.get("ghost_status") or "")
    if gs == "semantic_override" and r.is_executable:
        r.confidence = max(r.confidence, EXEC_GREEN_THRESHOLD)
        r.level = ExecLevel.GREEN
        r.notes.append("ghost:semantic_override")
    elif gs == "soft_warning" and r.is_executable:
        # Forzamos AMBER conservando ejecutabilidad.
        r.level = ExecLevel.AMBER
        if r.confidence < EXEC_AMBER_THRESHOLD:
            r.confidence = EXEC_AMBER_THRESHOLD
        if r.confidence >= EXEC_GREEN_THRESHOLD:
            r.confidence = EXEC_GREEN_THRESHOLD - 0.01
        r.notes.append("ghost:soft_warning")
    return r


# Factores contextualizados sólo para CLICK (identidad visual vs UIA ejecutable).
_CTL_STABILITY_BONUS: Dict[str, float] = {
    "buttoncontrol": 0.06,
    "splitbuttoncontrol": 0.052,
    "editcontrol": 0.038,
    "comboboxcontrol": 0.04,
    "checkboxcontrol": 0.032,
    "radiobuttoncontrol": 0.032,
    "hyperlinkcontrol": 0.04,
    "listitemcontrol": 0.048,
    "menuitemcontrol": 0.05,
    "tabitemcontrol": 0.036,
    "treeitemcontrol": 0.04,
    "toolbarbuttoncontrol": 0.048,
}

_BROWSER_PROCESSES_LOWER = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
    "opera.exe", "vivaldi.exe",
})


def _signature_target_lower(step: Any) -> str:
    tc = getattr(step, "target_context", None)
    if tc is None:
        return ""
    sig = getattr(tc, "signature", None) or {}
    if isinstance(sig, dict):
        return str(sig.get("target_source") or sig.get("source") or "").lower()
    return ""


def _apply_click_contextual_layer(
    step: Any,
    payload: Dict[str, Any],
    r: ExecutionConfidenceResult,
) -> None:
    """Ajusta la confianza de ejecución con señales de evidencia estable."""
    if r.is_blocking:
        return
    tc = getattr(step, "target_context", None)
    uia = getattr(tc, "uia_data", None) or {}
    if not isinstance(uia, dict):
        uia = {}
    ct_raw = str(uia.get("control_type") or "").strip().lower()
    proc = ""
    try:
        proc = (
            getattr(tc, "process_name", None) or str((uia.get("window") or {}).get("process_name") or "")
            or ""
        ).lower()
    except Exception:
        proc = ""

    bonuses: List[float] = []
    tsrc = _signature_target_lower(step)
    if tsrc == "semantic_target":
        bonuses.append(0.04)
        r.notes.append("identity:semantic_target")
    elif tsrc == "uia_control":
        bonuses.append(0.028)
        r.notes.append("identity:uia_control")

    stab = float(_CTL_STABILITY_BONUS.get(ct_raw, 0.0))
    if stab:
        bonuses.append(stab)
        r.notes.append(f"control_stability:{ct_raw}:{stab:.3f}")

    conf_blk: Dict[str, Any] = {}
    if tc is not None:
        conf_blk = dict(getattr(tc, "confidence", None) or {})
    ls = float(conf_blk.get("last_success_score") or 0.0)
    if ls >= 90.0:
        bonuses.append(0.035)
        r.notes.append("runtime:last_success≥90")
    elif ls >= 75.0:
        bonuses.append(0.022)
        r.notes.append("runtime:last_success≥75")

    vdat = getattr(tc, "vision_data", None) or {}
    if isinstance(vdat, dict):
        vfp = vdat.get("visual_fingerprint") or {}
        if isinstance(vfp, dict) and vfp.get("available"):
            bonuses.append(0.018)
            r.notes.append("evidence:visual_fingerprint")

    sig_tc = getattr(tc, "signature", None) or {}
    if isinstance(sig_tc, dict):
        if (
            sig_tc.get("trusted_pre_action_identity") is True
            and sig_tc.get("successful_state_transition") is True
        ):
            bonuses.append(0.11)
            r.notes.append("trust:pre_action_successful_transition")

    aid = str(uia.get("automation_id") or "").strip()
    nm = str(uia.get("name") or "").strip()
    penal = 0.0
    if proc in _BROWSER_PROCESSES_LOWER and ct_raw == "customcontrol":
        if not aid:
            penal += 0.07
            r.notes.append("penalty:browser_custom_without_aid")
        elif len(nm) > 52 and "-" in nm and nm.count(" ") < 3:
            penal += 0.045
            r.notes.append("penalty:long_technicalish_name_web")

    bonus = sum(bonuses) - penal
    if abs(bonus) < 1e-9:
        return
    r.confidence = min(0.985, max(0.03, float(r.confidence) + bonus))
    r.reasons.append(
        "context_adjust:{:+.4f}".format(bonus),
    )


# ──────────────────────────────────────────────────────────────────────
# Reglas por ActionStrategy
# ──────────────────────────────────────────────────────────────────────


def _rule_launch_app(payload: Dict[str, Any]) -> ExecutionConfidenceResult:
    app_name = str(payload.get("app_name") or payload.get("app") or "").strip()
    if not app_name:
        return ExecutionConfidenceResult(
            confidence=0.0, level=ExecLevel.RED,
            preferred_strategy=ExecStrategy.WINDOWS_SEARCH,
            is_executable=False, is_blocking=True, block_code="empty_app",
            reasons=["app_name vacío"],
        )
    return ExecutionConfidenceResult(
        confidence=0.95, level=ExecLevel.GREEN,
        preferred_strategy=ExecStrategy.WINDOWS_SEARCH,
        is_executable=True, is_blocking=False,
        reasons=[f"open_app('{app_name}') vía Windows Search"],
    )


def _rule_select_profile(payload: Dict[str, Any]) -> ExecutionConfidenceResult:
    profile = str(payload.get("profile") or payload.get("profile_name") or "").strip()
    needs_label = bool(payload.get("needs_user_label"))
    if not profile:
        # Falta el nombre del perfil. Es un bloqueante pero presentamos
        # AMBER al usuario para que entienda que es una etiqueta a
        # rellenar, no un error técnico.
        return ExecutionConfidenceResult(
            confidence=EXEC_AMBER_THRESHOLD, level=ExecLevel.AMBER,
            preferred_strategy=ExecStrategy.PROFILE_NAME_MATCH,
            is_executable=False, is_blocking=True, block_code="empty_profile",
            reasons=["profile vacío; el usuario debe nombrar el perfil"],
        )
    if needs_label:
        # Edge case raro: profile no vacío pero marcado needs_label.
        return ExecutionConfidenceResult(
            confidence=0.70, level=ExecLevel.AMBER,
            preferred_strategy=ExecStrategy.PROFILE_NAME_MATCH,
            is_executable=True, is_blocking=False,
            reasons=["profile presente pero marcado needs_user_label"],
        )
    return ExecutionConfidenceResult(
        confidence=0.90, level=ExecLevel.GREEN,
        preferred_strategy=ExecStrategy.PROFILE_NAME_MATCH,
        is_executable=True, is_blocking=False,
        reasons=[f"select_profile('{profile}') por texto"],
    )


def _rule_open_new_tab(payload: Dict[str, Any]) -> ExecutionConfidenceResult:
    # Ctrl+T funciona en todos los browsers sin importar el target visual.
    return ExecutionConfidenceResult(
        confidence=0.97, level=ExecLevel.GREEN,
        preferred_strategy=ExecStrategy.CTRL_T,
        is_executable=True, is_blocking=False,
        reasons=["open_new_tab vía Ctrl+T (independiente de UIA/visión)"],
    )


def _rule_open_bookmark(payload: Dict[str, Any]) -> ExecutionConfidenceResult:
    url = str(payload.get("url") or "").strip()
    alias = str(payload.get("name") or payload.get("alias") or "").strip()
    if not url and not alias:
        return ExecutionConfidenceResult(
            confidence=0.0, level=ExecLevel.RED,
            preferred_strategy=ExecStrategy.CTRL_L_URL,
            is_executable=False, is_blocking=True, block_code="empty_url",
            reasons=["open_url sin url ni alias"],
        )
    return ExecutionConfidenceResult(
        confidence=0.92, level=ExecLevel.GREEN,
        preferred_strategy=ExecStrategy.CTRL_L_URL,
        is_executable=True, is_blocking=False,
        reasons=[f"open_url('{url or alias}') vía Ctrl+L"],
    )


def _rule_submit_search(payload: Dict[str, Any]) -> ExecutionConfidenceResult:
    text = str(payload.get("text") or payload.get("query") or "").strip()
    if not text:
        return ExecutionConfidenceResult(
            confidence=0.0, level=ExecLevel.RED,
            preferred_strategy=ExecStrategy.DOM_UIA_OCR,
            is_executable=False, is_blocking=True, block_code="empty_query",
            reasons=["submit_search sin query"],
        )
    site = str(payload.get("site") or "").strip()
    return ExecutionConfidenceResult(
        confidence=0.96, level=ExecLevel.GREEN,
        preferred_strategy=ExecStrategy.DOM_UIA_OCR,
        is_executable=True, is_blocking=False,
        reasons=[f"search_site(site='{site}', query='{text[:30]}...')"],
    )


def _rule_scroll_until_visible(payload: Dict[str, Any]) -> ExecutionConfidenceResult:
    direction = str(payload.get("direction") or "down").strip()
    return ExecutionConfidenceResult(
        confidence=0.88, level=ExecLevel.GREEN,
        preferred_strategy=ExecStrategy.SCROLL_DYNAMIC,
        is_executable=True, is_blocking=False,
        reasons=[f"scroll_results(direction='{direction}')"],
    )


def _rule_navigate_or_search(payload: Dict[str, Any]) -> ExecutionConfidenceResult:
    target = (
        str(payload.get("url") or payload.get("query") or "").strip()
    )
    if not target:
        return ExecutionConfidenceResult(
            confidence=0.0, level=ExecLevel.RED,
            preferred_strategy=ExecStrategy.CTRL_L_URL,
            is_executable=False, is_blocking=True, block_code="empty_url",
            reasons=["navigate_or_search sin url/query"],
        )
    return ExecutionConfidenceResult(
        confidence=0.90, level=ExecLevel.GREEN,
        preferred_strategy=ExecStrategy.CTRL_L_URL,
        is_executable=True, is_blocking=False,
        reasons=[f"navigate_or_search('{target}')"],
    )


def _rule_set_field_value(payload: Dict[str, Any]) -> ExecutionConfidenceResult:
    text = str(payload.get("text") or payload.get("value") or "").strip()
    if not text:
        return ExecutionConfidenceResult(
            confidence=0.30, level=ExecLevel.RED,
            preferred_strategy=ExecStrategy.TYPE_FIELD_VALUE,
            is_executable=False, is_blocking=False,
            reasons=["set_field_value sin texto"],
        )
    return ExecutionConfidenceResult(
        confidence=0.85, level=ExecLevel.GREEN,
        preferred_strategy=ExecStrategy.TYPE_FIELD_VALUE,
        is_executable=True, is_blocking=False,
        reasons=[f"set_field_value('{text[:30]}...')"],
    )


def _rule_send_hotkey(payload: Dict[str, Any]) -> ExecutionConfidenceResult:
    keys = payload.get("keys") or payload.get("hotkey") or []
    if not keys:
        return ExecutionConfidenceResult(
            confidence=0.40, level=ExecLevel.AMBER,
            preferred_strategy=ExecStrategy.HOTKEY_DIRECT,
            is_executable=False, is_blocking=False,
            reasons=["send_hotkey sin keys"],
        )
    return ExecutionConfidenceResult(
        confidence=0.92, level=ExecLevel.GREEN,
        preferred_strategy=ExecStrategy.HOTKEY_DIRECT,
        is_executable=True, is_blocking=False,
        reasons=[f"send_hotkey({keys})"],
    )


def _rule_click(
    payload: Dict[str, Any],
    uia_data: Optional[Dict[str, Any]],
    web_data: Optional[Dict[str, Any]],
) -> ExecutionConfidenceResult:
    uia = uia_data or {}
    web = web_data or {}
    aid = str(uia.get("automation_id") or "").strip()
    name = str(uia.get("name") or uia.get("text") or "").strip()
    web_locators = bool(web.get("locators") or web.get("css") or web.get("xpath"))
    target_label = str(payload.get("target_label") or "").strip()

    if aid:
        return ExecutionConfidenceResult(
            confidence=0.90, level=ExecLevel.GREEN,
            preferred_strategy=ExecStrategy.UIA_AUTOMATION_ID,
            is_executable=True, is_blocking=False,
            reasons=[f"UIA automation_id='{aid}'"],
        )
    if name:
        return ExecutionConfidenceResult(
            confidence=0.85, level=ExecLevel.GREEN,
            preferred_strategy=ExecStrategy.UIA_NAME,
            is_executable=True, is_blocking=False,
            reasons=[f"UIA name='{name}'"],
        )
    if web_locators:
        return ExecutionConfidenceResult(
            confidence=0.88, level=ExecLevel.GREEN,
            preferred_strategy=ExecStrategy.WEB_LOCATOR,
            is_executable=True, is_blocking=False,
            reasons=["web_locator"],
        )
    if target_label:
        return ExecutionConfidenceResult(
            confidence=0.60, level=ExecLevel.AMBER,
            preferred_strategy=ExecStrategy.UIA_NAME,
            is_executable=True, is_blocking=False,
            reasons=[f"target_label='{target_label}' (sin AID/locator)"],
        )
    return ExecutionConfidenceResult(
        confidence=0.0, level=ExecLevel.RED,
        preferred_strategy=ExecStrategy.UNKNOWN,
        is_executable=False, is_blocking=True, block_code="empty_target",
        reasons=["click sin AID/name/locator/label"],
    )


# ──────────────────────────────────────────────────────────────────────
# Dispatcher
# ──────────────────────────────────────────────────────────────────────


def compute_execution_confidence(
    step: Any,
    *,
    mission: Optional[Any] = None,
) -> ExecutionConfidenceResult:
    """Calcula la confianza de un :class:`CompiledStep` en función de
    su :class:`ActionStrategy` y de los campos relevantes del payload
    / target_context. NO mira el ``capture_score`` — esa señal queda
    como diagnóstico visual, no como control de ejecución.

    Side-effect: persiste el resultado en ``step.action_payload``.
    """
    payload = dict(getattr(step, "action_payload", {}) or {})
    tc = getattr(step, "target_context", None)
    uia_data = getattr(tc, "uia_data", None) or {}
    web_data = getattr(tc, "web_data", None) or {}
    action = getattr(step, "action_strategy", None)

    # Importamos lazily para evitar dep circular.
    try:
        from app.contracts.mission import ActionStrategy
    except Exception:
        ActionStrategy = None  # type: ignore[assignment]

    if ActionStrategy is None:
        r = ExecutionConfidenceResult(
            confidence=0.0, level=ExecLevel.RED,
            preferred_strategy=ExecStrategy.UNKNOWN,
            is_executable=False, is_blocking=True, block_code="unknown_kind",
            reasons=["ActionStrategy enum no disponible"],
        )
    elif action == ActionStrategy.LAUNCH_APP:
        r = _rule_launch_app(payload)
    elif action == ActionStrategy.SELECT_PROFILE:
        r = _rule_select_profile(payload)
    elif action == ActionStrategy.OPEN_NEW_TAB:
        r = _rule_open_new_tab(payload)
    elif action == ActionStrategy.OPEN_BOOKMARK:
        r = _rule_open_bookmark(payload)
    elif action == ActionStrategy.SUBMIT_SEARCH:
        r = _rule_submit_search(payload)
    elif action == ActionStrategy.SCROLL_UNTIL_VISIBLE:
        r = _rule_scroll_until_visible(payload)
    elif action == ActionStrategy.NAVIGATE_OR_SEARCH:
        r = _rule_navigate_or_search(payload)
    elif action == ActionStrategy.SET_FIELD_VALUE:
        r = _rule_set_field_value(payload)
    elif action == ActionStrategy.TYPE_TEXT:
        r = _rule_set_field_value(payload)
    elif action == ActionStrategy.SEND_HOTKEY:
        r = _rule_send_hotkey(payload)
    elif action == ActionStrategy.CLICK:
        r = _rule_click(payload, uia_data, web_data)

    else:
        # Fallback general: estrategia desconocida pero no necesariamente
        # bloqueante. Si la app puede degradarse al runtime "ver qué pasa",
        # marcamos AMBER y dejamos correr.
        r = ExecutionConfidenceResult(
            confidence=0.50, level=ExecLevel.AMBER,
            preferred_strategy=ExecStrategy.UNKNOWN,
            is_executable=True, is_blocking=False,
            reasons=[f"action_strategy={action} sin regla específica"],
        )

    r = _ghost_override(payload, r)
    if ActionStrategy is not None and action == ActionStrategy.CLICK:
        _apply_click_contextual_layer(step, payload, r)
    # Recalibrar semáforo salvo cuando Ghost Simulator fija el nivel.
    gn = list(r.notes or [])
    if (
        ("ghost:semantic_override" not in gn)
        and ("ghost:soft_warning" not in gn)
        and (not r.is_blocking)
    ):
        r.level = _level_from_confidence(r.confidence)

    _persist_to_payload(step, r)
    return r


def compute_mission_execution_confidence(
    mission: Any,
) -> MissionExecutionReport:
    steps_out: List[ExecutionConfidenceResult] = []
    compiled = list(getattr(mission, "compiled_execution_graph", []) or [])
    for cs in compiled:
        try:
            r = compute_execution_confidence(cs, mission=mission)
        except Exception:
            r = ExecutionConfidenceResult()
        steps_out.append(r)
    avg = (
        sum(r.confidence for r in steps_out) / len(steps_out)
        if steps_out else 0.0
    )
    executable_count = sum(1 for r in steps_out if r.is_executable)
    blocking_count = sum(1 for r in steps_out if r.is_blocking)
    return MissionExecutionReport(
        steps=steps_out,
        average=round(avg, 4),
        executable_count=executable_count,
        blocking_count=blocking_count,
    )


# ──────────────────────────────────────────────────────────────────────
# UI helper
# ──────────────────────────────────────────────────────────────────────


_LEVEL_COLORS = {
    ExecLevel.GREEN: "#2ecc71",
    ExecLevel.AMBER: "#f39c12",
    ExecLevel.RED: "#e74c3c",
}


def execution_confidence_color(level: ExecLevel) -> str:
    """Devuelve el hex de color que la UI debe pintar para un nivel."""
    return _LEVEL_COLORS.get(level, "#95a5a6")
