"""FASE 5 live — normalización de entorno entre replays.

Deriva el preflight del contrato de la misión (SEP + ``build_contract``).
No hardcodea app, sitio ni perfil concretos.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.logger import log
from app.services.missions import action_runner as ar
from app.services.missions.execution_contracts import (
    MissionStep,
    build_contract,
    resolve_url_alias,
)
from app.services.missions.launch_apps import process_names_for
from app.services.missions.semantic_execution_plan import (
    build_semantic_execution_plan,
    semantic_plan_to_mission_steps,
)
from app.services.missions.state_detector import StateDetector, StateSnapshot

try:
    import pygetwindow as gw  # type: ignore

    HAS_GW = True
except Exception:
    gw = None  # type: ignore
    HAS_GW = False

ENVIRONMENT_NOT_NORMALIZED = "ENVIRONMENT_NOT_NORMALIZED"

_SETUP_SKIP_OK_KINDS = frozenset({
    "open_app",
    "select_profile",
    "select_entity_from_collection",
})

_URL_RESET_BARRIER_KINDS = frozenset({
    "open_new_tab",
    "open_tab",
    "create_new_context",
    "open_url",
    "open_site",
    "navigate_to_resource",
    "search_site",
    "search_content",
    "search_web",
    "search_youtube",
    "submit_input",
    "scroll_results",
    "browse_results",
})

_NEUTRAL_BASELINE_CANDIDATES = (
    "https://example.com/",
    "https://example.org/",
    "https://www.iana.org/domains/reserved",
)

_OPEN_NEW_TAB_POST_FRAGMENTS = (
    "about:blank",
    "newtab",
    "chrome://newtab",
    "edge://newtab",
)


@dataclass
class MissionNormalizationSpec:
    mission_id: str = ""
    required_app_name: str = ""
    required_processes: Tuple[str, ...] = ()
    identity_step_ids: List[str] = field(default_factory=list)
    setup_step_ids: List[str] = field(default_factory=list)
    barrier_steps: List[MissionStep] = field(default_factory=list)
    barrier_step_ids: List[str] = field(default_factory=list)
    needs_url_reset: bool = False
    baseline_url: Optional[str] = None
    forbidden_url_fragments: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "required_app_name": self.required_app_name,
            "required_processes": list(self.required_processes),
            "identity_step_ids": list(self.identity_step_ids),
            "setup_step_ids": list(self.setup_step_ids),
            "barrier_step_ids": list(self.barrier_step_ids),
            "needs_url_reset": self.needs_url_reset,
            "baseline_url": self.baseline_url or "",
            "forbidden_url_fragments": list(self.forbidden_url_fragments),
        }


@dataclass
class Fase5EnvironmentResetResult:
    initial_state_normalized: bool = True
    reason: str = "baseline_ok"
    failure_code: str = ""
    actions_taken: List[str] = field(default_factory=list)
    required_app: str = ""
    required_processes: List[str] = field(default_factory=list)
    identity_step_ids: List[str] = field(default_factory=list)
    barrier_step_ids: List[str] = field(default_factory=list)
    already_satisfied_step_ids: List[str] = field(default_factory=list)
    baseline_url: str = ""
    barrier_postconditions_before: Dict[str, bool] = field(default_factory=dict)
    barrier_postconditions_after: Dict[str, bool] = field(default_factory=dict)
    wait_ms: int = 0
    normalization_spec: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "initial_state_normalized": self.initial_state_normalized,
            "reason": self.reason,
            "failure_code": self.failure_code,
            "actions_taken": list(self.actions_taken),
            "required_app": self.required_app,
            "required_processes": list(self.required_processes),
            "identity_step_ids": list(self.identity_step_ids),
            "barrier_step_ids": list(self.barrier_step_ids),
            "already_satisfied_step_ids": list(self.already_satisfied_step_ids),
            "baseline_url": self.baseline_url,
            "barrier_postconditions_before": dict(self.barrier_postconditions_before),
            "barrier_postconditions_after": dict(self.barrier_postconditions_after),
            "wait_ms": self.wait_ms,
            "normalization_spec": dict(self.normalization_spec),
        }


def load_mission_steps(mission: Any) -> List[MissionStep]:
    plan = build_semantic_execution_plan(mission)
    return semantic_plan_to_mission_steps(plan)


def _collect_forbidden_url_fragments(steps: Sequence[MissionStep]) -> List[str]:
    forbidden: set[str] = set(_OPEN_NEW_TAB_POST_FRAGMENTS)
    for step in steps:
        if step.kind in ("open_url", "open_site"):
            url, domain = resolve_url_alias(step)
            if domain:
                forbidden.add(domain.lower().lstrip("www."))
            if url:
                forbidden.add(url.lower())
        params = step.params or {}
        for key in ("site", "search_site", "provider", "engine", "alias"):
            value = str(params.get(key) or "").strip().lower()
            if value and len(value) >= 3:
                forbidden.add(value)
    return sorted(f for f in forbidden if f)


def derive_neutral_baseline_url(
    forbidden_fragments: Sequence[str],
    barrier_steps: Sequence[MissionStep],
) -> Optional[str]:
    """URL neutral que no activa postcondiciones de pasos barrera."""
    for candidate in _NEUTRAL_BASELINE_CANDIDATES:
        lower = candidate.lower()
        if any(frag and frag in lower for frag in forbidden_fragments):
            continue
        probe = StateSnapshot(
            running_processes=["browser.exe"],
            browser_url=candidate,
            browser_domain=lower.split("//")[-1].split("/")[0],
            active_window_title="Example Domain",
        )
        if not any(_step_postcondition_satisfied(probe, step) for step in barrier_steps):
            return candidate
    return None


def derive_mission_normalization_spec(mission: Any) -> Tuple[MissionNormalizationSpec, Optional[str]]:
    """Deriva spec desde contrato. Segundo valor = razón de fallo si no derivable."""
    mission_id = str(getattr(mission, "id", "") or "")
    try:
        steps = load_mission_steps(mission)
    except Exception as exc:
        return MissionNormalizationSpec(mission_id=mission_id), f"mission_steps_unavailable:{exc}"

    if not steps:
        return MissionNormalizationSpec(mission_id=mission_id), "mission_steps_empty"

    required_app_name = ""
    required_processes: Tuple[str, ...] = ()
    for step in steps:
        if step.kind == "open_app":
            name = str((step.params or {}).get("name") or "").strip()
            if name:
                required_app_name = name
                required_processes = process_names_for(name)
                break

    identity_step_ids = [s.id for s in steps if s.kind in (
        "select_profile",
        "select_entity_from_collection",
    )]
    setup_step_ids = [s.id for s in steps if s.kind in _SETUP_SKIP_OK_KINDS]
    barrier_steps = [s for s in steps if s.kind not in _SETUP_SKIP_OK_KINDS]
    barrier_step_ids = [s.id for s in barrier_steps]
    needs_url_reset = any(s.kind in _URL_RESET_BARRIER_KINDS for s in barrier_steps)
    forbidden = _collect_forbidden_url_fragments(steps)

    baseline_url: Optional[str] = None
    if needs_url_reset:
        baseline_url = derive_neutral_baseline_url(forbidden, barrier_steps)
        if not baseline_url:
            return (
                MissionNormalizationSpec(
                    mission_id=mission_id,
                    required_app_name=required_app_name,
                    required_processes=required_processes,
                    identity_step_ids=identity_step_ids,
                    setup_step_ids=setup_step_ids,
                    barrier_steps=barrier_steps,
                    barrier_step_ids=barrier_step_ids,
                    needs_url_reset=True,
                    forbidden_url_fragments=tuple(forbidden),
                ),
                "baseline_url_not_derivable",
            )

    return (
        MissionNormalizationSpec(
            mission_id=mission_id,
            required_app_name=required_app_name,
            required_processes=required_processes,
            identity_step_ids=identity_step_ids,
            setup_step_ids=setup_step_ids,
            barrier_steps=barrier_steps,
            barrier_step_ids=barrier_step_ids,
            needs_url_reset=needs_url_reset,
            baseline_url=baseline_url,
            forbidden_url_fragments=tuple(forbidden),
        ),
        None,
    )


def _step_postcondition_satisfied(state: StateSnapshot, step: MissionStep) -> bool:
    contract = build_contract(step)
    try:
        return bool(contract.postcondition(state))
    except Exception:
        return False


def _already_satisfied_steps(
    state: StateSnapshot,
    steps: Sequence[MissionStep],
) -> List[str]:
    return [s.id for s in steps if _step_postcondition_satisfied(state, s)]


def _barrier_postconditions(
    state: StateSnapshot,
    barrier_steps: Sequence[MissionStep],
) -> Dict[str, bool]:
    return {s.id: _step_postcondition_satisfied(state, s) for s in barrier_steps}


def _any_barrier_satisfied(state: StateSnapshot, barrier_steps: Sequence[MissionStep]) -> bool:
    return any(_step_postcondition_satisfied(state, s) for s in barrier_steps)


def _url_indicates_mission_downstream(
    state: StateSnapshot,
    forbidden_fragments: Sequence[str],
) -> bool:
    combined = " ".join(
        part
        for part in (
            (state.browser_url or "").lower(),
            (state.browser_domain or "").lower(),
            (state.active_window_title or "").lower(),
        )
        if part
    )
    if not combined:
        return False
    return any(frag and len(frag) >= 4 and frag in combined for frag in forbidden_fragments)


def _required_processes_running(
    state: StateSnapshot,
    required_processes: Sequence[str],
) -> bool:
    if not required_processes:
        return False
    return state.is_process_running(*required_processes)


def _ensure_process_foreground(process_names: Sequence[str]) -> bool:
    if not HAS_GW or not process_names:
        return False
    stems = {p.lower().replace(".exe", "") for p in process_names if p}
    try:
        for win in gw.getAllWindows():
            if not getattr(win, "visible", True):
                continue
            title = (win.title or "").lower()
            if not title:
                continue
            if not any(stem in title for stem in stems):
                continue
            try:
                if getattr(win, "isMinimized", False):
                    win.restore()
                win.activate()
                time.sleep(0.35)
                return True
            except Exception:
                continue
    except Exception as exc:
        log.debug("[Fase5 reset] focus: %s", exc)
    return False


def _navigate_address_bar(url: str, process_names: Sequence[str]) -> bool:
    if not _ensure_process_foreground(process_names):
        return False
    if not ar._hotkey("ctrl", "l"):
        return False
    time.sleep(0.2)
    ar._hotkey("ctrl", "a")
    time.sleep(0.05)
    ar._press("delete")
    time.sleep(0.06)
    if not ar._type_text(url, interval=0.005):
        return False
    time.sleep(0.1)
    return bool(ar._press("enter"))


def _wait_until_barriers_clear(
    detector: StateDetector,
    barrier_steps: Sequence[MissionStep],
    *,
    timeout_ms: int = 4000,
    poll_ms: int = 200,
) -> StateSnapshot:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last = detector.detect(deep=True)
    while time.monotonic() < deadline:
        last = detector.detect(deep=True)
        if not _any_barrier_satisfied(last, barrier_steps):
            return last
        time.sleep(max(0.05, poll_ms / 1000.0))
    return last


def open_new_tab_postcondition_satisfied(
    state: StateSnapshot,
    *,
    step: Optional[MissionStep] = None,
) -> bool:
    """Evalúa postcondición ``open_new_tab`` vía contrato (misma lógica SEP)."""
    probe = step or MissionStep(
        id="fase5:preflight:open_new_tab",
        kind="open_new_tab",
        params={},
    )
    return _step_postcondition_satisfied(state, probe)


def normalize_fase5_live_replay_environment(
    *,
    mission: Any,
) -> Fase5EnvironmentResetResult:
    """Preflight live: baseline derivado del contrato de la misión."""
    started = time.monotonic()
    result = Fase5EnvironmentResetResult()

    spec, spec_error = derive_mission_normalization_spec(mission)
    result.normalization_spec = spec.to_dict()
    result.required_app = spec.required_app_name
    result.required_processes = list(spec.required_processes)
    result.identity_step_ids = list(spec.identity_step_ids)
    result.barrier_step_ids = list(spec.barrier_step_ids)
    result.baseline_url = spec.baseline_url or ""

    if spec_error:
        result.initial_state_normalized = False
        result.failure_code = ENVIRONMENT_NOT_NORMALIZED
        result.reason = spec_error
        result.wait_ms = int((time.monotonic() - started) * 1000)
        return result

    if not spec.barrier_steps:
        result.reason = "no_barrier_steps"
        result.wait_ms = int((time.monotonic() - started) * 1000)
        return result

    detector = StateDetector()
    try:
        state = detector.detect(deep=True)
    except Exception as exc:
        log.debug("[Fase5 reset] detect: %s", exc)
        state = StateSnapshot()

    all_steps = load_mission_steps(mission)
    result.already_satisfied_step_ids = _already_satisfied_steps(state, all_steps)
    result.barrier_postconditions_before = _barrier_postconditions(state, spec.barrier_steps)

    if spec.required_processes and not _required_processes_running(state, spec.required_processes):
        result.reason = "required_app_not_running_deferred_to_mission"
        result.wait_ms = int((time.monotonic() - started) * 1000)
        return result

    if spec.required_processes and not _ensure_process_foreground(spec.required_processes):
        result.initial_state_normalized = False
        result.failure_code = ENVIRONMENT_NOT_NORMALIZED
        result.reason = "required_app_foreground_failed"
        result.wait_ms = int((time.monotonic() - started) * 1000)
        return result

    if spec.required_processes:
        result.actions_taken.append("focus_required_app")
        state = detector.detect(deep=True)
        result.already_satisfied_step_ids = _already_satisfied_steps(state, all_steps)
        result.barrier_postconditions_before = _barrier_postconditions(
            state, spec.barrier_steps,
        )

    needs_reset = _any_barrier_satisfied(state, spec.barrier_steps)
    if not needs_reset and spec.needs_url_reset:
        needs_reset = _url_indicates_mission_downstream(
            state, spec.forbidden_url_fragments,
        )

    if not needs_reset:
        result.barrier_postconditions_after = dict(result.barrier_postconditions_before)
        result.reason = "barrier_action_required"
        result.wait_ms = int((time.monotonic() - started) * 1000)
        return result

    if not spec.needs_url_reset or not spec.baseline_url:
        result.initial_state_normalized = False
        result.failure_code = ENVIRONMENT_NOT_NORMALIZED
        result.reason = "barrier_satisfied_without_reset_path"
        result.wait_ms = int((time.monotonic() - started) * 1000)
        return result

    if not _navigate_address_bar(spec.baseline_url, spec.required_processes):
        result.initial_state_normalized = False
        result.failure_code = ENVIRONMENT_NOT_NORMALIZED
        result.reason = "baseline_navigation_failed"
        result.wait_ms = int((time.monotonic() - started) * 1000)
        return result

    result.actions_taken.append("navigate_baseline")
    state = _wait_until_barriers_clear(detector, spec.barrier_steps)
    result.barrier_postconditions_after = _barrier_postconditions(state, spec.barrier_steps)
    result.already_satisfied_step_ids = _already_satisfied_steps(state, all_steps)

    if _any_barrier_satisfied(state, spec.barrier_steps):
        result.initial_state_normalized = False
        result.failure_code = ENVIRONMENT_NOT_NORMALIZED
        result.reason = "barrier_postcondition_still_satisfied"
    else:
        result.reason = "baseline_reset_complete"

    result.wait_ms = int((time.monotonic() - started) * 1000)
    return result


__all__ = [
    "ENVIRONMENT_NOT_NORMALIZED",
    "Fase5EnvironmentResetResult",
    "MissionNormalizationSpec",
    "derive_mission_normalization_spec",
    "derive_neutral_baseline_url",
    "load_mission_steps",
    "normalize_fase5_live_replay_environment",
    "open_new_tab_postcondition_satisfied",
]
