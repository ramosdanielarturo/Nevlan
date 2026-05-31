"""
Mandatory post-step verification — minimal viable set
-----------------------------------------------------
After every action step, require at least one confirmation signal before
continuing. Failures return typed codes for repair / needs_review.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from app.services.missions.execution_contracts import MissionStep, StepContract
from app.services.missions.recorder_capture_contract import (
    TargetAnchor,
    detect_state_change,
)
from app.services.missions.state_detector import StateSnapshot

__all__ = [
    "OUTCOME_NOT_CONFIRMED",
    "TARGET_NOT_FOUND",
    "SURFACE_NOT_READY",
    "PostStepVerification",
    "verify_step_outcome",
    "verification_required_for_kind",
]

OUTCOME_NOT_CONFIRMED = "OUTCOME_NOT_CONFIRMED"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
SURFACE_NOT_READY = "SURFACE_NOT_READY"

VERIFICATION_REQUIRED_KINDS = frozenset({
    "click_named_item",
    "type_text",
    "scroll_page",
    "open_url",
    "open_app",
    "open_site",
    "open_new_tab",
    "search_youtube",
    "search_content",
    "search_site",
    "search_web",
    "select_profile",
})


@dataclass(frozen=True)
class PostStepVerification:
    passed: bool
    signal: str = ""
    failure_code: str = ""
    human_message: str = ""


def verification_required_for_kind(kind: str) -> bool:
    return str(kind or "") in VERIFICATION_REQUIRED_KINDS


def verify_step_outcome(
    step: MissionStep,
    contract: StepContract,
    state_before: Union[Dict[str, Any], StateSnapshot, None],
    state_after: Union[Dict[str, Any], StateSnapshot, None],
    *,
    strategy_used: Optional[str] = None,
) -> PostStepVerification:
    """Run minimal post-step verification for ``step``."""
    kind = str(step.kind or "")
    if not verification_required_for_kind(kind):
        return PostStepVerification(passed=True, signal="exempt")

    after = _as_snapshot(state_after)
    before = _as_snapshot(state_before)

    if after is not None and contract.postcondition(after):
        return PostStepVerification(passed=True, signal="already_satisfied")

    flags = detect_state_change(_to_anchor(before), _to_anchor(after))
    if flags.get("any_changed"):
        return PostStepVerification(passed=True, signal="visual_state_change")

    if kind in ("type_text", "search_youtube", "search_content", "search_site", "search_web"):
        if _verify_typed_value(step, after):
            return PostStepVerification(passed=True, signal="value_confirmed")

    if kind in ("open_url", "open_site"):
        if _verify_browser_url(step, after):
            return PostStepVerification(passed=True, signal="browser_url_confirmed")
        return PostStepVerification(
            passed=False,
            failure_code=SURFACE_NOT_READY,
            human_message="No confirmé que la URL del navegador cambió.",
        )

    if kind == "open_app":
        if _verify_app_foreground(step, after):
            return PostStepVerification(passed=True, signal="app_foreground_confirmed")
        return PostStepVerification(
            passed=False,
            failure_code=TARGET_NOT_FOUND,
            human_message="No confirmé que la aplicación quedó en primer plano.",
        )

    if kind == "open_new_tab":
        if flags.get("title_changed") or flags.get("url_changed"):
            return PostStepVerification(passed=True, signal="tab_context_change")
        if strategy_used and "ctrl_t" in str(strategy_used).lower():
            return PostStepVerification(passed=True, signal="hotkey_assumed_ok")

    if kind == "select_profile":
        if _verify_app_foreground(step, after, default_proc="chrome.exe"):
            return PostStepVerification(passed=True, signal="profile_surface_ready")

    if kind == "click_named_item" and after is not None:
        uia = getattr(after, "uia_targets", None) or []
        if uia:
            return PostStepVerification(passed=True, signal="element_state_available")

    label = step.human_label or step.describe()
    return PostStepVerification(
        passed=False,
        failure_code=OUTCOME_NOT_CONFIRMED,
        human_message=f"No confirmé el resultado de «{label}».",
    )


def _as_snapshot(
    state: Union[Dict[str, Any], StateSnapshot, None],
) -> Optional[StateSnapshot]:
    if state is None:
        return None
    if isinstance(state, StateSnapshot):
        return state
    if isinstance(state, dict):
        return StateSnapshot(
            active_app=state.get("active_app"),
            active_window_title=state.get("active_window_title"),
            browser_url=state.get("browser_url"),
            browser_title=state.get("browser_title"),
            running_processes=list(state.get("running_processes") or []),
            uia_targets=list(state.get("uia_targets") or []),
        )
    return None


def _to_anchor(state: Optional[StateSnapshot]) -> Optional[TargetAnchor]:
    if state is None:
        return None
    return TargetAnchor(
        title=str(state.active_window_title or ""),
        process=str(state.active_app or ""),
        url=str(state.browser_url or ""),
    )


def _verify_typed_value(step: MissionStep, state: Optional[StateSnapshot]) -> bool:
    params = step.params or {}
    expected = str(
        params.get("text")
        or params.get("query")
        or params.get("value")
        or ""
    ).strip()
    if not expected:
        return False
    try:
        from app.services.missions.uol_action_handlers import _validate_field_value
    except Exception:
        return False
    haystack = " ".join(
        filter(
            None,
            [
                getattr(state, "visible_text", None) if state else None,
                getattr(state, "browser_title", None) if state else None,
                getattr(state, "browser_url", None) if state else None,
            ],
        )
    )
    return _validate_field_value(expected, haystack)


def _verify_browser_url(step: MissionStep, state: Optional[StateSnapshot]) -> bool:
    if state is None:
        return False
    url = str(state.browser_url or "").lower()
    if not url:
        return False
    params = step.params or {}
    alias = str(params.get("alias") or "").lower()
    raw = str(params.get("url") or params.get("href") or "").lower()
    needle = alias or raw
    if not needle:
        return "http" in url
    return needle.replace("https://", "").replace("http://", "").split("/")[0] in url


def _verify_app_foreground(
    step: MissionStep,
    state: Optional[StateSnapshot],
    *,
    default_proc: str = "",
) -> bool:
    if state is None:
        return False
    params = step.params or {}
    name = str(params.get("name") or params.get("app") or default_proc).lower()
    if not name:
        return bool(state.active_app)
    active = str(state.active_app or "").lower()
    procs = [str(p).lower() for p in (state.running_processes or [])]
    stem = name.replace(".exe", "")
    if stem and active and stem in active:
        return True
    return any(stem in p for p in procs)
