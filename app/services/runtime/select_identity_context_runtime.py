"""Universal runtime resolver for identity / session / account selection steps.

Resolves ``select_profile``, ``select_entity_from_collection`` (identity
entities), and ``select_identity_context`` without app-specific hardcoding.
Coords are never used as the primary resolution path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import OperationalEntityType
from app.core.logger import log
from app.services.missions.action_runner import StrategyResult, _fail, _ok
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot
from app.services.runtime.entity_resolution_layer import (
    EntityMemoryStore,
    normalize_entity_name,
    _text_similarity,
)

STRATEGY_ALREADY_SATISFIED = "select_identity_context:already_satisfied"

ENTITY_CONTEXT_ALREADY_ACTIVE = "ENTITY_CONTEXT_ALREADY_ACTIVE"
ENTITY_CONTEXT_NOT_VERIFIABLE = "ENTITY_CONTEXT_NOT_VERIFIABLE"
ENTITY_SELECTOR_NOT_VISIBLE = "ENTITY_SELECTOR_NOT_VISIBLE"

# Re-export canonical ERL codes consumed by UOL/ORCE reports.
from app.services.runtime.entity_runtime_resolution import (  # noqa: E402
    ENTITY_AMBIGUOUS,
    ENTITY_NOT_FOUND,
)

_IDENTITY_STEP_KINDS = frozenset({
    "select_profile",
    "select_entity_from_collection",
    "select_identity_context",
})

_IDENTITY_ENTITY_TYPES = frozenset({
    OperationalEntityType.USER_PROFILE.value,
    OperationalEntityType.BROWSER_PROFILE.value,
    "user_profile",
    "browser_profile",
    "account",
    "session_identity",
    "identity",
    "selectable_identity",
})

# App-agnostic title/url markers for identity/account pickers (not a specific product).
_SELECTOR_TITLE_MARKERS: Tuple[str, ...] = (
    "choose your profile",
    "elige tu perfil",
    "quién está usando",
    "who's using",
    "choose a profile",
    "select an account",
    "elige una cuenta",
    "sign in to",
    "pick a profile",
    "selecciona un perfil",
    "select profile",
    "choose account",
    "elige cuenta",
    "who is using",
    "select identity",
    "seleccionar cuenta",
)

_SELECTOR_URL_MARKERS: Tuple[str, ...] = (
    "profile-picker",
    "account-picker",
    "chooseprofile",
    "select-account",
    "identity-picker",
)

_ACTIVE_VERIFY_MIN_SCORE = 0.55
_ACTIVE_VERIFY_WEAK_MIN = 0.45
_ACTIVE_VERIFY_WEAK_SIGNALS = 2


@dataclass
class ActiveIdentityVerification:
    verified: bool = False
    confidence: float = 0.0
    signals: List[str] = field(default_factory=list)
    target_name: str = ""


@dataclass
class SelectIdentityContextResult:
    resolved: bool = False
    skipped: bool = False
    strategy: str = ""
    message: str = ""
    failure_reason: str = ""
    needs_human: bool = False
    verification: Optional[ActiveIdentityVerification] = None
    selector_visible: bool = False
    selector_signal: str = ""
    resolution_path: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_strategy_result(self, *, wrap_strategy: str = "") -> StrategyResult:
        strat = wrap_strategy or self.strategy or STRATEGY_ALREADY_SATISFIED
        merged_extra = dict(self.extra)
        if self.failure_reason:
            merged_extra["entity_failure_reason"] = self.failure_reason
        if self.verification and self.verification.signals:
            merged_extra["identity_verification_signals"] = list(self.verification.signals)
        if self.resolution_path:
            merged_extra["resolution_path"] = self.resolution_path
        if self.needs_human:
            merged_extra["ask_human"] = True
        if self.skipped or self.resolved:
            return _ok(
                self.strategy or STRATEGY_ALREADY_SATISFIED,
                self.message or "contexto de identidad ya activo",
                **merged_extra,
            )
        return _fail(strat, self.message or self.failure_reason, **merged_extra)


def is_identity_selection_step(step: MissionStep) -> bool:
    kind = str(step.kind or "").strip()
    if kind in _IDENTITY_STEP_KINDS:
        if kind == "select_entity_from_collection":
            return _step_targets_identity_entity(step)
        return True
    return _step_targets_identity_entity(step)


def _step_targets_identity_entity(step: MissionStep) -> bool:
    pars = dict(step.params or {})
    ent_raw = pars.get("collection_entity") or pars.get("entity") or pars.get("operational_entity") or {}
    etype = ""
    if isinstance(ent_raw, dict):
        etype = str(
            ent_raw.get("entity_type")
            or ent_raw.get("entity_type_hint")
            or "",
        ).strip().lower()
    if etype in _IDENTITY_ENTITY_TYPES:
        return True
    if pars.get("profile_name") or pars.get("profile"):
        return True
    uol_etype = str(pars.get("uol_target_entity_type") or "").strip().lower()
    if uol_etype in _IDENTITY_ENTITY_TYPES:
        return True
    ident_type = str(pars.get("operational_element_type") or "").strip().lower()
    return ident_type in {"selectable_identity", "identity_context"}


def target_identity_display_name(step: MissionStep, mission: Any = None) -> str:
    pars = dict(step.params or {})
    for key in (
        "uol_target_display_name",
        "selection_label",
        "profile_name",
        "profile",
        "label",
        "label_hint",
    ):
        val = str(pars.get(key) or "").strip()
        if val:
            return val
    ent_raw = pars.get("collection_entity") or pars.get("entity") or {}
    if isinstance(ent_raw, dict):
        name = str(ent_raw.get("display_name") or "").strip()
        if name:
            return name
    if mission is not None:
        try:
            from app.services.runtime.entity_runtime_resolution import entity_from_mission_step

            ent = entity_from_mission_step(step, mission)
            if ent and ent.display_name:
                return str(ent.display_name).strip()
        except Exception as exc:
            log.debug("[select_identity_context] entity_from_mission_step: %s", exc)
    return ""


def _target_app_executables(step: MissionStep) -> Tuple[str, ...]:
    pars = dict(step.params or {})
    raw = str(pars.get("app") or pars.get("application") or "").strip().lower()
    if raw:
        if raw.endswith(".exe"):
            return (raw,)
        return (f"{raw}.exe", raw)
    coll = pars.get("operational_collection") or {}
    if isinstance(coll, dict):
        host = str(coll.get("host_app") or coll.get("application") or "").strip().lower()
        if host:
            return (host if host.endswith(".exe") else f"{host}.exe",)
    return ()


def _entity_in_title(target: str, title: Optional[str]) -> bool:
    if not target or not title:
        return False
    t_norm = normalize_entity_name(target)
    title_norm = normalize_entity_name(title)
    if not t_norm or not title_norm:
        return False
    if t_norm in title_norm:
        return True
    return _text_similarity(t_norm, title_norm) >= 0.82


def detect_identity_selector_visible(
    state: StateSnapshot,
    step: MissionStep,
    mission: Any = None,
) -> Tuple[bool, str]:
    """Detect identity/account picker surfaces without UIA-only false positives."""
    _ = mission
    if state.title_contains(*_SELECTOR_TITLE_MARKERS):
        return True, "window_title_selector"
    if state.url_contains(*_SELECTOR_URL_MARKERS):
        return True, "url_selector_page"
    return False, ""


def _identity_like_visible_names(names: Sequence[str]) -> List[str]:
    out: List[str] = []
    for raw in names:
        n = str(raw or "").strip()
        if not n or len(n) < 2:
            continue
        low = n.lower()
        if any(m in low for m in ("add ", "agregar", "new ", "nuevo", "guest", "invitado")):
            continue
        if low in {"profile", "perfil", "account", "cuenta", "sign in", "iniciar sesión"}:
            continue
        out.append(n)
    return out


def verify_active_identity_context(
    step: MissionStep,
    state: StateSnapshot,
    mission: Any = None,
) -> ActiveIdentityVerification:
    target = target_identity_display_name(step, mission)
    pars = dict(step.params or {})
    signals: List[str] = []
    score = 0.0

    selector_visible, _ = detect_identity_selector_visible(state, step, mission)
    exes = _target_app_executables(step)
    app_up = bool(exes and state.is_process_running(*exes))

    for exe in exes:
        if state.is_process_running(exe):
            signals.append("target_app_running")
            score += 0.2
            break

    has_session_url = bool(
        state.url_contains("http://", "https://")
        and not state.url_contains(*_SELECTOR_URL_MARKERS),
    )
    if has_session_url:
        signals.append("session_url_active")
        score += 0.25

    if not selector_visible and app_up and (
        has_session_url
        or pars.get("pre_transition_confirmed")
        or pars.get("uol_canonical_truth")
        or pars.get("canonical_truth")
    ):
        signals.append("identity_session_ready")
        score += 0.35
    elif not selector_visible and app_up:
        signals.append("app_open_no_identity_selector")
        score += 0.35

    if target and _entity_in_title(target, state.browser_title):
        signals.append("identity_in_browser_title")
        score += 0.35
    elif target and _entity_in_title(target, state.active_window_title):
        signals.append("identity_in_window_title")
        score += 0.3

    session_identity = str(
        pars.get("session_identity")
        or pars.get("user_session_id")
        or pars.get("session_id")
        or "",
    ).strip()
    if session_identity and session_identity.lower() in (state.browser_url or "").lower():
        signals.append("session_identity_match")
        score += 0.2

    if pars.get("continuity_preserved") and str(pars.get("operational_thread_id") or "").strip():
        signals.append("continuity_thread")
        score += 0.15

    if pars.get("operational_element_fingerprint") or pars.get("operational_element_identity_id"):
        signals.append("uoei_identity_present")
        score += 0.1

    if pars.get("pre_transition_confirmed") or pars.get("uol_canonical_truth"):
        if not selector_visible and app_up:
            signals.append("pre_transition_session")
            score += 0.15

    hist = _historical_entity_confidence(step, mission, target)
    if hist >= 0.5:
        signals.append("historical_entity_memory")
        score += min(0.25, hist * 0.25)

    verified = score >= _ACTIVE_VERIFY_MIN_SCORE or (
        score >= _ACTIVE_VERIFY_WEAK_MIN and len(signals) >= _ACTIVE_VERIFY_WEAK_SIGNALS
    )
    return ActiveIdentityVerification(
        verified=verified,
        confidence=min(1.0, score),
        signals=signals,
        target_name=target,
    )


def _historical_entity_confidence(step: MissionStep, mission: Any, target: str) -> float:
    if not target:
        return 0.0
    try:
        from app.services.runtime.entity_runtime_resolution import (
            entity_from_mission_step,
            _build_runtime_context,
        )

        ent = entity_from_mission_step(step, mission)
        if ent is None:
            return 0.0
        ctx = _build_runtime_context(step, StateSnapshot(), mission)
        return float(EntityMemoryStore().historical_success_score(ent.entity_id, ctx.get("context_hash", "")))
    except Exception as exc:
        log.debug("[select_identity_context] historical memory: %s", exc)
        return 0.0


def skip_if_active_identity_context(
    step: MissionStep,
    state: StateSnapshot,
    mission: Any = None,
) -> Optional[SelectIdentityContextResult]:
    """If the requested identity context is already active, return skip result."""
    if not is_identity_selection_step(step):
        return None

    selector_visible, selector_signal = detect_identity_selector_visible(state, step, mission)
    if selector_visible:
        return None

    verification = verify_active_identity_context(step, state, mission)
    if not verification.verified:
        return None

    target = verification.target_name or target_identity_display_name(step, mission)
    msg = (
        f"contexto de identidad '{target}' ya activo"
        if target
        else "contexto de identidad ya activo"
    )
    return SelectIdentityContextResult(
        resolved=True,
        skipped=True,
        strategy=STRATEGY_ALREADY_SATISFIED,
        message=msg,
        failure_reason=ENTITY_CONTEXT_ALREADY_ACTIVE,
        verification=verification,
        selector_visible=False,
        selector_signal=selector_signal,
        resolution_path="skip_if_active_identity_context",
        extra={"verified_profile": bool(target), "identity_context_skip": True},
    )


def resolve_select_identity_context(
    step: MissionStep,
    state: StateSnapshot,
    mission: Any = None,
) -> SelectIdentityContextResult:
    """Full identity-context resolution (skip, delegate, or structured failure)."""
    if not is_identity_selection_step(step):
        return SelectIdentityContextResult(resolved=False)

    skip = skip_if_active_identity_context(step, state, mission)
    if skip is not None:
        return skip

    selector_visible, selector_signal = detect_identity_selector_visible(state, step, mission)
    target = target_identity_display_name(step, mission)

    if selector_visible:
        amb = _count_ambiguous_identity_candidates(state, target)
        if amb >= 2:
            return SelectIdentityContextResult(
                resolved=False,
                needs_human=True,
                failure_reason=ENTITY_AMBIGUOUS,
                message="Hay varias identidades visibles; indica cuál elegir.",
                selector_visible=True,
                selector_signal=selector_signal,
                resolution_path="identity_selector_ambiguous",
            )
        if not target:
            return SelectIdentityContextResult(
                resolved=False,
                needs_human=True,
                failure_reason=ENTITY_NOT_FOUND,
                message="Selector visible pero falta la identidad objetivo.",
                selector_visible=True,
                selector_signal=selector_signal,
            )
        return SelectIdentityContextResult(
            resolved=False,
            selector_visible=True,
            selector_signal=selector_signal,
            resolution_path="delegate_uces_erl_uia",
            message="Selector visible; delegar a UCES/ERL/UIA.",
        )

    verification = verify_active_identity_context(step, state, mission)
    if verification.verified:
        return SelectIdentityContextResult(
            resolved=True,
            skipped=True,
            strategy=STRATEGY_ALREADY_SATISFIED,
            message="Contexto de identidad verificado sin selector visible.",
            failure_reason=ENTITY_CONTEXT_ALREADY_ACTIVE,
            verification=verification,
            resolution_path="active_context_verified",
            extra={"verified_profile": bool(verification.target_name)},
        )

    apps = _target_app_executables(step)
    if apps and not state.is_process_running(*apps):
        return SelectIdentityContextResult(
            resolved=False,
            needs_human=True,
            failure_reason=ENTITY_SELECTOR_NOT_VISIBLE,
            message="La aplicación objetivo no está activa y no hay selector de identidad visible.",
            selector_visible=False,
            verification=verification,
            resolution_path="selector_not_visible_app_missing",
        )

    return SelectIdentityContextResult(
        resolved=False,
        needs_human=True,
        failure_reason=ENTITY_CONTEXT_NOT_VERIFIABLE,
        message=(
            "No pude verificar el contexto de identidad activo "
            f"para '{target}'." if target else
            "No pude verificar el contexto de identidad activo."
        ),
        selector_visible=False,
        verification=verification,
        resolution_path="identity_context_not_verifiable",
    )


def _count_ambiguous_identity_candidates(state: StateSnapshot, target: str) -> int:
    names = _identity_like_visible_names(state.uia_visible_names or [])
    if len(names) < 2:
        return len(names)
    if not target:
        return len(names)
    t_norm = normalize_entity_name(target)
    strong = [
        n
        for n in names
        if _text_similarity(normalize_entity_name(n), t_norm) >= 0.88
    ]
    if len(strong) == 1:
        return 1
    return len(names)


def map_entity_failure_for_identity_context(
    failure_reason: str,
    *,
    selector_visible: bool,
    verification: Optional[ActiveIdentityVerification] = None,
) -> str:
    """Map generic ERL failures to identity-specific codes when appropriate."""
    reason = str(failure_reason or "").strip()
    if reason == ENTITY_NOT_FOUND and not selector_visible:
        if verification and verification.verified:
            return ENTITY_CONTEXT_ALREADY_ACTIVE
        return ENTITY_CONTEXT_NOT_VERIFIABLE
    if reason == ENTITY_NOT_FOUND and selector_visible:
        return ENTITY_NOT_FOUND
    return reason or ENTITY_CONTEXT_NOT_VERIFIABLE


__all__ = [
    "ENTITY_CONTEXT_ALREADY_ACTIVE",
    "ENTITY_CONTEXT_NOT_VERIFIABLE",
    "ENTITY_SELECTOR_NOT_VISIBLE",
    "STRATEGY_ALREADY_SATISFIED",
    "ActiveIdentityVerification",
    "SelectIdentityContextResult",
    "detect_identity_selector_visible",
    "is_identity_selection_step",
    "map_entity_failure_for_identity_context",
    "resolve_select_identity_context",
    "skip_if_active_identity_context",
    "target_identity_display_name",
    "verify_active_identity_context",
]
