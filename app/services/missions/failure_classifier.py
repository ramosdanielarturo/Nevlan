"""
ArthurOS Services - MissionFailureClassifier (Auto-Learning Executor)
---------------------------------------------------------------------
Clasifica los fallos del SmartMissionExecutor en una taxonomía de
**12 categorías** y propone una recuperación recomendada por categoría.

Este clasificador es **distinto** y complementario al de
``app/services/capabilities/failure_classifier.py`` (que trata sobre la
disponibilidad de capacidades del sistema). Aquí tomamos el contexto
completo de un step intentando cumplir su postcondition (texto de error,
``StateSnapshot`` antes/después, kind del step) y elegimos la categoría.

Categorías (alineadas con el PRD literal):
  - target_not_found        : el target esperado no apareció en pantalla
  - wrong_app               : la app activa no es la esperada
  - wrong_page              : URL/título no corresponde
  - stale_selector          : el selector funcionaba antes y ya no
  - timeout                 : se agotó el tiempo
  - focus_lost              : la ventana correcta perdió foco
  - permission_error        : permiso denegado / UAC / login required
  - layout_changed          : la UI cambió y el target ya no encaja
  - profile_not_loaded      : Chrome arrancó sin perfil / "Bienvenido"
  - network_slow            : tardanzas anómalas en navegación / red
  - user_interrupted        : el usuario abortó manualmente
  - unknown                 : no clasificable

Cada categoría devuelve un ``RecoveryRecommendation`` con:
  - acciones sugeridas (textuales — el executor las mapea a recetas)
  - severidad (info | warning | critical)
  - flag retryable
  - flag should_ask_human

Diseño:
  - Pure: no toca disco, no muta. Trabaja sobre dataclasses.
  - Determinista: facilita testing. La heurística es por keywords del
    error + reglas sobre el snapshot (active_app, url, title, etc).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ──────────────────────────────────────────────────────────────────────
# Tipos
# ──────────────────────────────────────────────────────────────────────

class MissionFailureType(str, Enum):
    TARGET_NOT_FOUND = "target_not_found"
    WRONG_APP = "wrong_app"
    WRONG_PAGE = "wrong_page"
    STALE_SELECTOR = "stale_selector"
    TIMEOUT = "timeout"
    FOCUS_LOST = "focus_lost"
    PERMISSION_ERROR = "permission_error"
    LAYOUT_CHANGED = "layout_changed"
    PROFILE_NOT_LOADED = "profile_not_loaded"
    NETWORK_SLOW = "network_slow"
    USER_INTERRUPTED = "user_interrupted"
    UNKNOWN = "unknown"


@dataclass
class RecoveryRecommendation:
    failure_type: MissionFailureType
    actions: List[str] = field(default_factory=list)   # tags semánticos
    severity: str = "warning"                           # info|warning|critical
    retryable: bool = True
    should_ask_human: bool = False
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "failure_type": self.failure_type.value,
            "actions": list(self.actions),
            "severity": self.severity,
            "retryable": self.retryable,
            "should_ask_human": self.should_ask_human,
            "rationale": self.rationale,
        }


# ──────────────────────────────────────────────────────────────────────
# Recovery por categoría (texto que el AutoLearningExecutor mapea)
# ──────────────────────────────────────────────────────────────────────

_RECOVERY_TABLE: Dict[MissionFailureType, RecoveryRecommendation] = {
    MissionFailureType.TARGET_NOT_FOUND: RecoveryRecommendation(
        failure_type=MissionFailureType.TARGET_NOT_FOUND,
        actions=["consult_target_memory", "try_alt_strategy", "scroll_search", "ocr_fallback"],
        severity="warning",
        retryable=True,
        should_ask_human=False,
        rationale="Target no apareció — probar alt strategies y memoria",
    ),
    MissionFailureType.WRONG_APP: RecoveryRecommendation(
        failure_type=MissionFailureType.WRONG_APP,
        actions=["activate_correct_window", "open_app_if_missing", "retry_step"],
        severity="warning",
        retryable=True,
        should_ask_human=False,
        rationale="App activa incorrecta — activar ventana correcta",
    ),
    MissionFailureType.WRONG_PAGE: RecoveryRecommendation(
        failure_type=MissionFailureType.WRONG_PAGE,
        actions=["open_correct_url", "validate_url", "retry_step"],
        severity="warning",
        retryable=True,
        should_ask_human=False,
        rationale="URL distinta — abrir URL correcta y reintentar",
    ),
    MissionFailureType.STALE_SELECTOR: RecoveryRecommendation(
        failure_type=MissionFailureType.STALE_SELECTOR,
        actions=["try_uia", "try_ocr", "try_vision", "update_target_memory"],
        severity="warning",
        retryable=True,
        should_ask_human=False,
        rationale="Selector obsoleto — escalar a UIA/OCR/vision y actualizar memoria",
    ),
    MissionFailureType.TIMEOUT: RecoveryRecommendation(
        failure_type=MissionFailureType.TIMEOUT,
        actions=["extend_timeout", "wait_more", "retry_step"],
        severity="warning",
        retryable=True,
        should_ask_human=False,
        rationale="Timeout — ampliar deadline y reintentar",
    ),
    MissionFailureType.FOCUS_LOST: RecoveryRecommendation(
        failure_type=MissionFailureType.FOCUS_LOST,
        actions=["activate_correct_window", "retry_step"],
        severity="warning",
        retryable=True,
        should_ask_human=False,
        rationale="Foco perdido — activar ventana correcta",
    ),
    MissionFailureType.PERMISSION_ERROR: RecoveryRecommendation(
        failure_type=MissionFailureType.PERMISSION_ERROR,
        actions=["ask_human"],
        severity="critical",
        retryable=False,
        should_ask_human=True,
        rationale="Permiso denegado — requiere intervención humana",
    ),
    MissionFailureType.LAYOUT_CHANGED: RecoveryRecommendation(
        failure_type=MissionFailureType.LAYOUT_CHANGED,
        actions=["try_uia", "try_ocr", "try_vision", "update_target_memory", "ask_human_if_persistent"],
        severity="warning",
        retryable=True,
        should_ask_human=False,
        rationale="UI cambió — escalar técnicas y actualizar memoria",
    ),
    MissionFailureType.PROFILE_NOT_LOADED: RecoveryRecommendation(
        failure_type=MissionFailureType.PROFILE_NOT_LOADED,
        actions=["select_profile", "retry_step"],
        severity="warning",
        retryable=True,
        should_ask_human=False,
        rationale="Chrome sin perfil cargado — invocar select_profile",
    ),
    MissionFailureType.NETWORK_SLOW: RecoveryRecommendation(
        failure_type=MissionFailureType.NETWORK_SLOW,
        actions=["extend_timeout", "wait_more", "retry_step"],
        severity="info",
        retryable=True,
        should_ask_human=False,
        rationale="Red lenta — esperar y reintentar con timeout extendido",
    ),
    MissionFailureType.USER_INTERRUPTED: RecoveryRecommendation(
        failure_type=MissionFailureType.USER_INTERRUPTED,
        actions=["abort_mission"],
        severity="critical",
        retryable=False,
        should_ask_human=True,
        rationale="Interrupción del usuario — no reintentar automáticamente",
    ),
    MissionFailureType.UNKNOWN: RecoveryRecommendation(
        failure_type=MissionFailureType.UNKNOWN,
        actions=["retry_step", "ask_human_if_persistent"],
        severity="warning",
        retryable=True,
        should_ask_human=False,
        rationale="Causa no clara — reintentar y escalar si persiste",
    ),
}


# ──────────────────────────────────────────────────────────────────────
# Heurísticas
# ──────────────────────────────────────────────────────────────────────

# Patrones por categoría (lower-cased): regex sueltos + substrings.
_PATTERNS: Dict[MissionFailureType, List[str]] = {
    MissionFailureType.TIMEOUT: [
        r"\btimeout\b", r"\btimed?\s*out\b", r"deadline\s*exceeded",
        r"esper[oa]\s+demasiado", r"se\s+agot[oó]\s+el\s+tiempo",
    ],
    MissionFailureType.PERMISSION_ERROR: [
        r"permission\s*denied", r"access\s*denied",
        r"unauthor[ie]zed", r"forbidden", r"\b403\b", r"\b401\b",
        r"uac", r"administrador", r"admin\s*required",
        r"login\s*required",
    ],
    MissionFailureType.STALE_SELECTOR: [
        r"stale\s+element", r"detached\s+from\s+document",
        r"no\s+such\s+element", r"selector\s+(?:no\s+)?match",
    ],
    MissionFailureType.TARGET_NOT_FOUND: [
        r"not\s*found", r"no\s+encontrado", r"no\s+existe",
        r"unable\s+to\s+locate", r"\bmissing\b", r"target\s+vacío",
    ],
    MissionFailureType.NETWORK_SLOW: [
        r"network\s+slow", r"red\s+lenta", r"net::err_", r"\bdns\b",
        r"connection\s+reset", r"connection\s+refused",
    ],
    MissionFailureType.LAYOUT_CHANGED: [
        r"layout\s+changed", r"obscured", r"intercepted",
        r"different\s+structure", r"distinto\s+layout",
    ],
    MissionFailureType.USER_INTERRUPTED: [
        r"user\s+aborted", r"cancelled\s+by\s+user", r"interrupt",
        r"keyboardinterrupt",
    ],
    MissionFailureType.PROFILE_NOT_LOADED: [
        r"perfil\s+no\s+cargado", r"profile\s+not\s+loaded",
        r"bienvenido\s+a\s+chrome",
    ],
}


# ──────────────────────────────────────────────────────────────────────
# Classifier
# ──────────────────────────────────────────────────────────────────────

class MissionFailureClassifier:
    """Clasifica fallos del SmartMissionExecutor a 12 categorías."""

    def classify(
        self,
        *,
        error: Optional[str] = None,
        step_kind: Optional[str] = None,
        expected_app: Optional[str] = None,
        expected_url_substring: Optional[str] = None,
        state_before: Optional[Dict[str, Any]] = None,
        state_after: Optional[Dict[str, Any]] = None,
        prior_success_for_strategy: bool = False,
        retries_so_far: int = 0,
        wait_was_long: bool = False,
    ) -> RecoveryRecommendation:
        """Devuelve la recomendación de recuperación.

        Heurística (en orden de prioridad):
          1) Error explícito → match contra patrones.
          2) state_after.active_app != expected_app ⇒ wrong_app
          3) state_after.browser_url no contiene expected_url_substring ⇒ wrong_page
          4) Step open/select_profile y title sigue siendo el welcome ⇒ profile_not_loaded
          5) Wait largo + url incorrecto ⇒ network_slow
          6) prior_success_for_strategy=True + ahora falla ⇒ stale_selector / layout_changed
          7) Si retries_so_far ≥ 3 sin clasificar y wait_was_long ⇒ network_slow
          8) UNKNOWN
        """
        err_str = (error or "").strip()
        err_lower = err_str.lower()

        # 1) Patrones explícitos
        for ftype, pats in _PATTERNS.items():
            for p in pats:
                try:
                    if re.search(p, err_lower):
                        return self._recommend(ftype, error=err_str)
                except re.error:
                    if p in err_lower:
                        return self._recommend(ftype, error=err_str)

        sa = state_after or {}
        sb = state_before or {}

        # 2) wrong_app
        if expected_app:
            current_app = (sa.get("active_app") or "").strip().lower()
            want = expected_app.strip().lower()
            if current_app and want and current_app != want and want not in current_app:
                return self._recommend(
                    MissionFailureType.WRONG_APP,
                    rationale=f"active_app={current_app!r} ≠ esperado={want!r}",
                )

        # 3) wrong_page
        if expected_url_substring:
            url = (sa.get("browser_url") or "").lower()
            want_u = expected_url_substring.strip().lower()
            if url and want_u and want_u not in url:
                return self._recommend(
                    MissionFailureType.WRONG_PAGE,
                    rationale=f"url={url!r} no contiene {want_u!r}",
                )
            if not url and (sa.get("active_app") or "").lower().startswith("chrome"):
                # Chrome activo pero sin URL detectable → posiblemente wrong_page
                return self._recommend(
                    MissionFailureType.WRONG_PAGE,
                    rationale="Chrome activo sin URL detectable",
                )

        # 4) profile_not_loaded (titulo welcome)
        title = (sa.get("active_window_title") or "").lower()
        if "bienvenido a chrome" in title or "welcome to chrome" in title:
            return self._recommend(
                MissionFailureType.PROFILE_NOT_LOADED,
                rationale="Pantalla de bienvenida detectada",
            )

        # 5) network_slow combinado
        if wait_was_long:
            url = (sa.get("browser_url") or "").lower()
            url_b = (sb.get("browser_url") or "").lower()
            if url == url_b:
                return self._recommend(
                    MissionFailureType.NETWORK_SLOW,
                    rationale="URL no cambió tras espera larga",
                )

        # 6) stale_selector / layout_changed por contexto
        if prior_success_for_strategy:
            if "selector" in err_lower or "element" in err_lower:
                return self._recommend(
                    MissionFailureType.STALE_SELECTOR,
                    rationale="Estrategia funcionaba antes y ahora el selector falla",
                )
            return self._recommend(
                MissionFailureType.LAYOUT_CHANGED,
                rationale="Estrategia funcionaba antes; UI parece haber cambiado",
            )

        # 7) Reintentos altos + wait largo sin clasificar
        if retries_so_far >= 3 and wait_was_long:
            return self._recommend(
                MissionFailureType.NETWORK_SLOW,
                rationale="Muchos reintentos con esperas largas",
            )

        # focus_lost: si el step expected_app coincide con el process pero
        # state_after.active_app es distinto pequeño, ya está cubierto en (2).

        return self._recommend(MissionFailureType.UNKNOWN, error=err_str)

    def is_recoverable(self, failure_type: MissionFailureType) -> bool:
        rec = _RECOVERY_TABLE.get(failure_type)
        return bool(rec and rec.retryable)

    def recommend(self, failure_type: MissionFailureType) -> RecoveryRecommendation:
        rec = _RECOVERY_TABLE.get(failure_type) or _RECOVERY_TABLE[MissionFailureType.UNKNOWN]
        return RecoveryRecommendation(
            failure_type=rec.failure_type,
            actions=list(rec.actions),
            severity=rec.severity,
            retryable=rec.retryable,
            should_ask_human=rec.should_ask_human,
            rationale=rec.rationale,
        )

    # ── Internals ─────────────────────────────────────────────
    def _recommend(
        self,
        failure_type: MissionFailureType,
        *,
        rationale: Optional[str] = None,
        error: Optional[str] = None,
    ) -> RecoveryRecommendation:
        rec = self.recommend(failure_type)
        if rationale:
            rec.rationale = rationale
        if error and not rec.rationale:
            rec.rationale = f"err: {error[:120]}"
        return rec


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────

_classifier: Optional[MissionFailureClassifier] = None


def get_failure_classifier() -> MissionFailureClassifier:
    global _classifier
    if _classifier is None:
        _classifier = MissionFailureClassifier()
    return _classifier


def reset_failure_classifier_for_tests() -> None:
    global _classifier
    _classifier = None


__all__ = [
    "MissionFailureType",
    "RecoveryRecommendation",
    "MissionFailureClassifier",
    "get_failure_classifier",
    "reset_failure_classifier_for_tests",
]
