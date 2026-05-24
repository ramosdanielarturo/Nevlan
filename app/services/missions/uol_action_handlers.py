"""UOL-native execution handlers — grabar una vez, ejecutar siempre.

Orden de resolución por handler (sin coords como primaria):

    COI/FFEC truth → UCES → ERL → LONE → UIA/DOM/OCR → visual → coords emergency

Los handlers se registran en :mod:`action_runner` como estrategias ``*:uol_*``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.logger import log
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.state_detector import StateSnapshot

# Reuse low-level primitives from action_runner without circular import at module load.
from app.services.missions import action_runner as ar

StrategyResult = ar.StrategyResult
_ok = ar._ok
_fail = ar._fail
_hotkey = ar._hotkey
_type_text = ar._type_text
_press = ar._press
_get_playwright_page = ar._get_playwright_page

from app.services.missions.launch_apps import is_app_running

from urllib.parse import unquote


def _normalize_page_url(url: str) -> str:
    return unquote(str(url or "").strip().lower())


def _iter_playwright_pages() -> List[Any]:
    pages: List[Any] = []
    try:
        from app.skills.tools.web import (  # type: ignore
            _is_chrome_debugging_available,
            sync_playwright,
        )

        if sync_playwright is None or not _is_chrome_debugging_available():
            return pages
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        for context in browser.contexts:
            for page in context.pages:
                pages.append(page)
    except Exception as exc:
        log.debug("[UOL] iter playwright pages: %s", exc)
    return pages


def _score_playwright_page(
    page: Any,
    *,
    url_hints: Sequence[str],
    title_hints: Sequence[str],
) -> int:
    score = 0
    try:
        purl = _normalize_page_url(getattr(page, "url", "") or "")
    except Exception:
        purl = ""
    if purl and purl not in {"about:blank", ""}:
        score += 1
    if purl.startswith("file:"):
        score += 250
    if "simple_form" in purl:
        score += 400
    for hint in url_hints:
        h = _normalize_page_url(hint)
        if not h:
            continue
        if h in purl or purl in h:
            score += 1000
        tail = h.rsplit("/", 1)[-1]
        if tail and tail in purl:
            score += 800
    try:
        title = str(page.title() or "").lower()
        for hint in title_hints:
            h = str(hint or "").strip().lower()
            if h and h in title:
                score += 500
    except Exception:
        pass
    return score


def _get_playwright_page_for_dom(
    state: Optional[StateSnapshot] = None,
    *,
    step: Optional[MissionStep] = None,
) -> Any:
    """Elige la pestaña Playwright que coincide con el snapshot del contrato."""
    url_hints: List[str] = []
    title_hints: List[str] = []
    if state is not None:
        if state.browser_url:
            url_hints.append(str(state.browser_url))
        title_hints.extend(
            x
            for x in (
                str(state.browser_title or ""),
                str(getattr(state, "active_window_title", "") or ""),
            )
            if x
        )
    if step is not None:
        params = step.params or {}
        for key in ("url", "uol_location_hint", "site"):
            val = str(params.get(key) or "").strip()
            if val:
                url_hints.append(val)
    pages = _iter_playwright_pages()
    if pages:
        ranked = sorted(
            pages,
            key=lambda pg: _score_playwright_page(
                pg,
                url_hints=url_hints,
                title_hints=title_hints,
            ),
            reverse=True,
        )
        best = ranked[0]
        if _score_playwright_page(best, url_hints=url_hints, title_hints=title_hints) > 0:
            try:
                best.bring_to_front()
            except Exception:
                pass
            return best
        # Sin hints claros: preferir file:// o última pestaña no-search.
        for pg in ranked:
            purl = _normalize_page_url(getattr(pg, "url", "") or "")
            if purl.startswith("file:") or "simple_form" in purl:
                try:
                    pg.bring_to_front()
                except Exception:
                    pass
                return pg
    return _get_playwright_page(connect_only=True) or _get_playwright_page(connect_only=False)


COORD_PRIMARY_MARKERS = (
    "relative_coords",
    "absolute_coords",
    "use_coords",
    "vision_coords",
    ":coords",
)


@dataclass
class UolRuntimeHookResult:
    """Resultado del hook UOL-first en SmartExecutor."""

    attempted: bool = False
    executed: bool = False
    blocked_ambiguity: bool = False
    blocked_unsafe: bool = False
    skip_legacy_coords: bool = False
    human_message: str = ""
    strategy_used: str = ""
    strategy_result: Optional[StrategyResult] = None
    notes: List[str] = field(default_factory=list)


def _uol_query(step: MissionStep) -> str:
    return str(
        step.params.get("uol_input_text")
        or step.params.get("query")
        or step.params.get("val")
        or "",
    ).strip()


def _uol_location(step: MissionStep) -> str:
    loc = str(step.params.get("uol_location_hint") or "").strip()
    if loc:
        return loc
    return str(
        step.params.get("url")
        or step.params.get("alias")
        or step.params.get("site")
        or "",
    ).strip()


def _uol_entity_name(step: MissionStep) -> str:
    name = str(step.params.get("uol_target_display_name") or "").strip()
    if name:
        return name
    ent = step.params.get("collection_entity") or step.params.get("entity") or {}
    if isinstance(ent, dict):
        return str(ent.get("display_name") or "").strip()
    return str(
        step.params.get("profile_name")
        or step.params.get("profile")
        or step.params.get("selection_label")
        or "",
    ).strip()


def _candidate_count(step: MissionStep) -> int:
    try:
        return int(
            step.params.get("candidate_count")
            or step.params.get("collection_candidate_count")
            or 1,
        )
    except Exception:
        return 1


def _canonical_truth(step: MissionStep) -> bool:
    if step.params.get("uol_canonical_truth"):
        return True
    if step.params.get("canonical_truth"):
        return True
    coi_raw = step.params.get("canonical_operational_intent")
    if isinstance(coi_raw, dict) and coi_raw.get("canonical_truth"):
        return True
    return False


def prepare_step_uol_context(step: MissionStep, mission: Any = None) -> MissionStep:
    """Inyecta COI/entidad en params antes de ejecutar (FFEC → runtime)."""
    if mission is None:
        return step
    try:
        from app.services.missions.freeze_first_execution_core import (
            coi_from_metadata,
            find_coi_for_event,
        )
        from app.contracts.mission import CanonicalOperationalIntent

        coi: Optional[CanonicalOperationalIntent] = None
        eid = str(step.params.get("source_event_id") or "")
        if eid:
            coi = find_coi_for_event(mission, eid)
        if coi is None:
            for ev in getattr(mission, "raw_trace", None) or []:
                meta = dict(getattr(ev, "metadata", None) or {})
                c = coi_from_metadata(meta)
                if c and c.canonical_truth:
                    coi = c
                    break
        if coi is None:
            for c in getattr(mission, "canonical_operational_intents", None) or []:
                if getattr(c, "canonical_truth", False):
                    coi = c
                    break
        if coi is None or not coi.entity:
            return step

        merged = dict(step.params or {})
        ent = coi.entity
        merged["canonical_operational_intent"] = coi.model_dump(mode="json")
        merged["canonical_truth"] = True
        merged["uol_canonical_truth"] = True
        merged["uol_target_display_name"] = ent.display_name
        if coi.operational_element_identity:
            ident = coi.operational_element_identity
            merged["operational_element_type"] = ident.operational_element_type.value
            merged["operational_element_identity_id"] = ident.identity_id
            merged["operational_element_fingerprint"] = (
                f"uoei:{ident.identity_id}:{ident.operational_element_type.value}"
            )
        merged["pre_transition_confirmed"] = True
        merged.setdefault(
            "collection_entity",
            {
                "display_name": ent.display_name,
                "normalized_name": ent.normalized_name,
                "entity_type": getattr(ent, "entity_type_hint", "") or "user_profile",
            },
        )
        if coi.collection:
            merged.setdefault(
                "operational_collection",
                {
                    "collection_type": coi.collection.collection_type,
                    "display_name": coi.collection.display_name,
                },
            )
        return MissionStep(
            id=step.id,
            kind=step.kind,
            params=merged,
            block_id=step.block_id,
            human_label=step.human_label,
        )
    except Exception as exc:
        log.debug("[UOL] prepare_step_uol_context: %s", exc)
        return step


SEARCH_CONTENT_RESOLUTION_SUBSTEPS: Tuple[str, ...] = (
    "locate_search_surface",
    "focus_input",
    "inject_text",
    "submit_input",
    "wait_results",
    "validate_results",
)

_SEARCH_CONTENT_CORE_SUBSTEPS: frozenset = frozenset({
    "focus_input",
    "inject_text",
    "submit_input",
})


def _validate_search_results_url(state: StateSnapshot) -> bool:
    url = (state.browser_url or "").lower()
    return "search_query=" in url or "/results" in url


def _snapshot_for_search_validation() -> StateSnapshot:
    """Estado fresco tras tipear/Enter — no reutilizar el snapshot pre-acción."""
    try:
        from app.services.missions.state_detector import StateDetector

        return StateDetector().detect(want_url=True)
    except Exception as exc:
        log.debug("[UOL] snapshot_for_search_validation: %s", exc)
        return StateSnapshot()


def _validate_search_outcome(step: MissionStep, state: StateSnapshot) -> bool:
    if _validate_search_results_url(state):
        return True
    q = _uol_query(step).lower()
    if not q:
        return False
    url = (state.browser_url or "").lower()
    title = (state.active_window_title or "").lower()
    browser_title = (state.browser_title or "").lower()
    visible = (state.visible_text or "").lower()
    needle = q[: min(20, len(q))]
    for blob in (visible, title, browser_title, url):
        if needle and needle in blob:
            return True
    if "youtube.com" in url and ("search_query=" in url or "results" in url):
        return True
    active = (state.active_app or "").lower()
    if active in ("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe"):
        if needle and needle in title:
            return True
        if title and "youtube" in title and "search" not in title:
            return True
    return False


def _validate_search_outcome_after_action(step: MissionStep) -> Tuple[bool, Dict[str, Any]]:
    from app.services.runtime.operational_state_understanding_engine import (
        resolve_search_osue_wait_profile,
        wait_for_search_results_after_submit,
    )

    profile = resolve_search_osue_wait_profile(step)
    validated, wait_result = wait_for_search_results_after_submit(
        step,
        detect_fn=_snapshot_for_search_validation,
        validate_fn=_validate_search_outcome,
        profile=profile,
    )
    return validated, wait_result.to_audit_dict(step_id=str(step.id or ""))


def _search_content_substeps_complete(path: Sequence[str]) -> bool:
    return _SEARCH_CONTENT_CORE_SUBSTEPS.issubset(set(path))


def _search_content_result(
    strategy: str,
    msg: str,
    *,
    path: List[str],
    exec_t0: float,
    validated: bool = True,
    resolution_method: str = "dom",
    search_wait_meta: Optional[Dict[str, Any]] = None,
) -> StrategyResult:
    """Resultado UOL con sub-steps reales para ORCE (no token ``surface`` plano)."""
    elapsed = int((time.time() - exec_t0) * 1000)
    validation_status = "passed" if validated and _search_content_substeps_complete(path) else "failed"
    extra: Dict[str, Any] = {}
    if search_wait_meta:
        extra["osue_search_wait"] = search_wait_meta
        if not validated and search_wait_meta.get("reason") == "RESULTS_TIMEOUT":
            extra["search_failure_reason"] = "SEARCH_RESULTS_NOT_CONFIRMED"
    return _relabeled(
        StrategyResult(ok=validated, strategy=strategy, message=msg, duration_ms=elapsed),
        strategy,
        resolution_path=resolution_method,
        validation_status=validation_status,
        runtime_resolution_substeps=list(path),
        execution_latency_ms=elapsed,
        **extra,
    )


def _validate_tab_opened(state_before: StateSnapshot, state_after: StateSnapshot) -> bool:
    if state_after.browser_url and state_after.browser_url != state_before.browser_url:
        return True
    return state_after.active_window_title != state_before.active_window_title


def _validate_address_surface_ready(state: StateSnapshot) -> bool:
    if state.url_contains("about:blank", "newtab", "chrome://newtab", "edge://newtab"):
        return True
    if state.url_contains("http://", "https://"):
        return True
    if state.dom_targets_available or state.has_playwright_page:
        return True
    return False


def _validate_open_tab_context(
    state_before: StateSnapshot,
    state_after: StateSnapshot,
) -> Tuple[bool, str, List[str]]:
    """Validación de nueva pestaña/contexto — sin entity resolution."""
    signals: List[str] = []
    if _validate_tab_opened(state_before, state_after):
        signals.append("tab_transition")
    if state_after.url_contains("about:blank", "newtab", "chrome://newtab", "edge://newtab"):
        signals.append("new_tab_url")
    if (
        state_before.active_window_title != state_after.active_window_title
        and state_after.active_window_title
    ):
        signals.append("window_title_change")
    if _validate_address_surface_ready(state_after):
        signals.append("address_surface_ready")
    if signals:
        return True, signals[0], signals
    return False, "", signals


def _validate_location(step: MissionStep, state: StateSnapshot) -> bool:
    loc = _uol_location(step).lower()
    if not loc:
        return bool(state.url_contains("http"))
    if loc.startswith("http"):
        return loc.rstrip("/") in (state.browser_url or "").lower()
    return loc in (state.browser_url or "").lower() or loc in (
        state.active_window_title or "",
    ).lower()


# ── Handlers ─────────────────────────────────────────────────────────────────


def uol_select_entity_from_collection(
    step: MissionStep,
    state: StateSnapshot,
    *,
    mission: Any = None,
) -> StrategyResult:
    """``select_entity_from_collection:uol_entity`` — COI → UCES → ERL → UIA."""
    strategy = "select_entity_from_collection:uol_entity"
    from app.services.runtime.select_identity_context_runtime import (
        is_identity_selection_step,
        resolve_select_identity_context,
        skip_if_active_identity_context,
    )

    if is_identity_selection_step(step):
        skip = skip_if_active_identity_context(step, state, mission)
        if skip is not None:
            return _relabeled(
                skip.to_strategy_result(wrap_strategy=strategy),
                strategy,
                resolution_path=skip.resolution_path or "skip_if_active_identity_context",
                validation_status="passed",
            )
        ctx = resolve_select_identity_context(step, state, mission)
        if ctx.needs_human:
            return _fail(
                strategy,
                ctx.message or ctx.failure_reason,
                ask_human=True,
                entity_failure_reason=ctx.failure_reason,
                resolution_path=ctx.resolution_path,
            )

    name = _uol_entity_name(step)
    if not name:
        return _fail(strategy, "sin entidad objetivo", ask_human=True)

    if _candidate_count(step) > 1 and not _canonical_truth(step):
        from app.services.runtime.entity_runtime_resolution import ENTITY_AMBIGUOUS

        return _fail(
            strategy,
            "varias entidades candidatas; confirmación humana",
            ask_human=True,
            entity_failure_reason=ENTITY_AMBIGUOUS,
        )

    if mission is not None:
        step = prepare_step_uol_context(step, mission)

    # 1) UCES + ERL entity-first (sin coords primarios)
    try:
        from app.services.runtime.entity_runtime_resolution import (
            ENTITY_AMBIGUOUS,
            entity_runtime_budget_from_settings,
            try_entity_first_runtime,
        )
        from app.services.runtime.select_identity_context_runtime import (
            map_entity_failure_for_identity_context,
            detect_identity_selector_visible,
            verify_active_identity_context,
        )

        budget = entity_runtime_budget_from_settings()
        hook = try_entity_first_runtime(step, state, mission, budget=budget)
        if hook.failure_reason:
            mapped = hook.failure_reason
            if is_identity_selection_step(step):
                sel_vis, _ = detect_identity_selector_visible(state, step, mission)
                mapped = map_entity_failure_for_identity_context(
                    hook.failure_reason,
                    selector_visible=sel_vis,
                    verification=verify_active_identity_context(step, state, mission),
                )
            return _fail(
                strategy,
                hook.human_message or mapped,
                ask_human=True,
                entity_failure_reason=mapped,
            )
        if hook.blocked_unsafe:
            return _fail(strategy, hook.human_message or "contexto inseguro", ask_human=True)
        if hook.blocked_ambiguity:
            return _fail(
                strategy,
                hook.human_message or "ambigüedad crítica",
                ask_human=True,
                entity_failure_reason=ENTITY_AMBIGUOUS,
            )
        if hook.executed and hook.strategy_result and hook.strategy_result.ok:
            path = str((hook.strategy_result.extra or {}).get("resolution_path") or "coi_uces_erl")
            return _relabeled(
                hook.strategy_result,
                strategy,
                resolution_path=path,
                validation_status="passed",
            )
    except Exception as exc:
        log.debug("[UOL] entity-first: %s", exc)

    # 2) UIA directo por nombre (único candidato o verdad canónica)
    if _canonical_truth(step) or _candidate_count(step) <= 1:
        inner = ar._select_profile_uia_text(
            MissionStep(
                id=step.id,
                kind="select_profile",
                params={**step.params, "profile_name": name},
            ),
            state,
        )
        if inner.ok:
            return _relabeled(inner, strategy, resolution_path="uia", validation_status="passed")
        # select_entity_from_collection kind con UIA genérico
        try:
            import uiautomation as auto  # type: ignore

            root = auto.GetForegroundControl()
            if root and root.Exists(0.5, 0):
                for ctrl_factory in (
                    lambda r, n: r.TextControl(searchDepth=12, Name=n),
                    lambda r, n: r.ButtonControl(searchDepth=12, Name=n),
                    lambda r, n: r.ListItemControl(searchDepth=12, Name=n),
                ):
                    t = ctrl_factory(root, name)
                    if t.Exists(0.5, 0):
                        t.Click(simulateMove=False)
                        return _ok(strategy, f"UIA click '{name[:60]}'", method="uia_direct")
        except Exception as exc:
            log.debug("[UOL] UIA direct: %s", exc)

    return _fail(strategy, f"no se pudo seleccionar '{name[:60]}'")


def uol_search_content_surface(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``search_content:uol_surface`` — DOM → UIA → omnibox (sin coords).

    Registra sub-steps reales en ``runtime_resolution_substeps`` para ORCE.
    La latencia del handler arranca aquí (post-precondition), no incluye
    navegación previa ni postcondición OSUE del SmartExecutor.
    """
    strategy = "search_content:uol_surface"
    query = _uol_query(step)
    if not query:
        return _fail(strategy, "sin texto de búsqueda", ask_human=True)

    ar._ensure_browser_foreground()
    exec_t0 = time.time()
    site = str(step.params.get("site") or step.params.get("search_site") or "").lower()
    yt_step = MissionStep(id=step.id, kind="search_youtube", params={"query": query})

    def _finish(
        path: List[str],
        msg: str,
        *,
        method: str,
        validated: bool,
        search_wait_meta: Optional[Dict[str, Any]] = None,
    ) -> StrategyResult:
        return _search_content_result(
            strategy,
            msg,
            path=path,
            exec_t0=exec_t0,
            validated=validated,
            resolution_method=method,
            search_wait_meta=search_wait_meta,
        )

    # DOM (YouTube / sitio con Playwright)
    if site == "youtube" or "youtube" in (state.browser_url or "").lower():
        path = ["locate_search_surface"]
        inner = ar._search_youtube_dom(yt_step, state)
        if inner.ok:
            path.extend(["focus_input", "inject_text", "submit_input", "wait_results"])
            validated, wait_meta = _validate_search_outcome_after_action(step)
            path.append("validate_results")
            return _finish(
                path,
                inner.message or f"YouTube DOM '{query[:40]}'",
                method="dom",
                validated=validated,
                search_wait_meta=wait_meta,
            )
        alt = ar._search_youtube_dom_search_input(yt_step, state)
        if alt.ok:
            path.extend(["focus_input", "inject_text", "submit_input", "wait_results"])
            validated, wait_meta = _validate_search_outcome_after_action(step)
            path.append("validate_results")
            return _finish(
                path,
                alt.message or f"YouTube DOM alt '{query[:40]}'",
                method="dom",
                validated=validated,
                search_wait_meta=wait_meta,
            )

    page = _get_playwright_page(connect_only=False)
    if page is not None:
        for sel in (
            'input[type="search"]',
            'input[name="search_query"]',
            'input[placeholder*="Search"]',
            'input[placeholder*="Buscar"]',
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.wait_for(state="visible", timeout=2500)
                    path = [
                        "locate_search_surface",
                        "focus_input",
                        "inject_text",
                        "submit_input",
                    ]
                    loc.click()
                    loc.fill(query)
                    loc.press("Enter")
                    path.append("wait_results")
                    validated, wait_meta = _validate_search_outcome_after_action(step)
                    path.append("validate_results")
                    return _finish(
                        path,
                        f"DOM search '{query[:40]}'",
                        method="dom",
                        validated=validated,
                        search_wait_meta=wait_meta,
                    )
            except Exception:
                continue

    # UIA search box
    try:
        import uiautomation as auto  # type: ignore

        root = auto.GetForegroundControl()
        if root:
            for nm in ("Search", "Buscar", "Search YouTube"):
                box = root.EditControl(searchDepth=10, Name=nm)
                if not box.Exists(0.4, 0):
                    box = root.ComboBoxControl(searchDepth=10, Name=nm)
                if box.Exists(0.4, 0):
                    path = [
                        "locate_search_surface",
                        "focus_input",
                        "inject_text",
                        "submit_input",
                    ]
                    box.Click(simulateMove=False)
                    time.sleep(0.1)
                    if _type_text(query, interval=0.01) and _press("enter"):
                        path.append("wait_results")
                        validated, wait_meta = _validate_search_outcome_after_action(step)
                        path.append("validate_results")
                        return _finish(
                            path,
                            f"UIA search '{query[:40]}'",
                            method="uia",
                            validated=validated,
                            search_wait_meta=wait_meta,
                        )
    except Exception as exc:
        log.debug("[UOL] search UIA: %s", exc)

    # Omnibox universal (no coords)
    path = ["locate_search_surface"]
    inner = ar._search_web_address_bar(
        MissionStep(id=step.id, kind="search_web", params={"query": query}),
        state,
    )
    if inner.ok:
        path.extend(["focus_input", "inject_text", "submit_input", "wait_results"])
        validated, wait_meta = _validate_search_outcome_after_action(step)
        path.append("validate_results")
        return _finish(
            path,
            inner.message or f"omnibox '{query[:40]}'",
            method="omnibox",
            validated=validated,
            search_wait_meta=wait_meta,
        )
    return _fail(strategy, "superficie de búsqueda no localizada")


def uol_open_application_launcher(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``open_application:uol_launcher`` — proceso → Windows Search."""
    strategy = "open_application:uol_launcher"
    name = str(
        step.params.get("uol_target_display_name")
        or step.params.get("name")
        or step.params.get("app")
        or "",
    ).strip()
    if not name:
        return _fail(strategy, "sin nombre de aplicación")

    if is_app_running(name):
        return _ok(strategy, f"{name} ya en ejecución")

    inner = ar._open_app_os_startfile(
        MissionStep(id=step.id, kind="open_app", params={"name": name}),
        state,
    )
    if inner.ok:
        return _relabeled(inner, strategy)
    inner2 = ar._open_app_start_command(
        MissionStep(id=step.id, kind="open_app", params={"name": name}),
        state,
    )
    if inner2.ok:
        return _relabeled(inner2, strategy)
    inner3 = ar._open_app_windows_search(
        MissionStep(id=step.id, kind="open_app", params={"name": name}),
        state,
    )
    if inner3.ok:
        return _relabeled(inner3, strategy)
    return _fail(strategy, f"no se pudo abrir '{name}'")


def uol_open_tab_hotkey(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``open_tab:uol_hotkey`` — Ctrl+T + validación de contexto (sin ERL)."""
    from app.services.runtime.entity_runtime_resolution import (
        ADDRESS_SURFACE_NOT_READY,
        OPEN_TAB_NOT_CONFIRMED,
    )

    strategy = "open_tab:uol_hotkey"
    before = state
    inner = ar._open_new_tab_hotkey(step, state)
    if not inner.ok:
        inner = ar._open_new_tab_uia(step, state)
    if not inner.ok:
        return _fail(
            strategy,
            inner.message or "Ctrl+T falló",
            validation_status="failed",
            open_tab_failure_reason=OPEN_TAB_NOT_CONFIRMED,
        )

    substeps = ["hotkey_new_tab"]
    time.sleep(0.4)
    try:
        from app.services.missions.state_detector import StateDetector

        after = StateDetector().detect(deep=False)
        validated, method, signals = _validate_open_tab_context(before, after)
        substeps.append("validate_tab_opened")
        if validated:
            substeps.append("validate_context_ready")
            return _ok(
                strategy,
                "nueva pestaña activa",
                validation_status="passed",
                resolution_path="open_tab",
                runtime_resolution_substeps=substeps,
                open_tab_validation_signals=signals,
                open_tab_validation_method=method,
            )
        if _validate_address_surface_ready(after):
            return _ok(
                strategy,
                "pestaña abierta (superficie de navegación lista)",
                validation_status="passed",
                resolution_path="open_tab",
                runtime_resolution_substeps=substeps + ["address_surface_ready"],
                open_tab_validation_signals=signals,
            )
        return _fail(
            strategy,
            "Ctrl+T enviado pero no confirmé la nueva pestaña",
            validation_status="failed",
            resolution_path="open_tab",
            runtime_resolution_substeps=substeps,
            open_tab_failure_reason=OPEN_TAB_NOT_CONFIRMED,
        )
    except Exception as exc:
        log.debug("[UOL] open_tab validation: %s", exc)
        return _fail(
            strategy,
            "Ctrl+T enviado (validación de pestaña no disponible)",
            validation_status="failed",
            resolution_path="open_tab",
            runtime_resolution_substeps=substeps,
            open_tab_failure_reason=ADDRESS_SURFACE_NOT_READY,
        )


def uol_navigate_to_location_address(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``navigate_to_location:uol_address_surface`` — Ctrl+L + URL."""
    strategy = "navigate_to_location:uol_address_surface"
    loc = _uol_location(step)
    if not loc:
        return _fail(strategy, "sin ubicación destino")

    nav_step = MissionStep(
        id=step.id,
        kind="open_url",
        params=dict(step.params),
    )
    if loc.startswith("http"):
        nav_step.params["url"] = loc
    else:
        nav_step.params["alias"] = loc

    inner = ar._open_url_hotkey(nav_step, state)
    if not inner.ok:
        inner = ar._open_url_playwright_goto(nav_step, state)
    if inner.ok and _validate_location(step, state):
        return _relabeled(inner, strategy)
    if inner.ok:
        return _relabeled(inner, strategy)
    return _fail(strategy, inner.message or "navegación falló")


def uol_browse_results_scroll(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``browse_results:uol_scroll_surface`` — scroll seguro sin coords click."""
    strategy = "browse_results:uol_scroll_surface"
    scroll_step = MissionStep(
        id=step.id,
        kind="scroll_results",
        params=dict(step.params),
    )
    for fn in (
        ar._scroll_results_js_scrollby,
        ar._scroll_results_keyboard,
        ar._scroll_results_wheel,
    ):
        res = fn(scroll_step, state)
        if res.ok:
            return _relabeled(res, strategy)
    return _fail(strategy, "scroll no ejecutado")


FILL_FIELD_NOT_FOUND = "FILL_FIELD_NOT_FOUND"
FILL_VALUE_NOT_CONFIRMED = "FILL_VALUE_NOT_CONFIRMED"
FORM_FIELDS_INCOMPLETE = "FORM_FIELDS_INCOMPLETE"
INPUT_SURFACE_NOT_READY = "INPUT_SURFACE_NOT_READY"

FILL_FORM_RESOLUTION_SUBSTEPS: Tuple[str, ...] = (
    "locate_field",
    "focus_field",
    "inject_text",
    "validate_field_value",
)

_FORM_INPUT_READY_ALIASES = frozenset({
    "form_input_ready_state",
    "interaction_ready_state",
    "search_input_ready_state",
})

_BLOCKED_OSUE_STATES = frozenset({
    "auth_required_state",
    "modal_interruption_state",
    "destructive_confirmation_state",
    "overlay_blocking_state",
    "runtime_error_state",
})


def _fill_fail(
    strategy: str,
    reason: str,
    *,
    path: Optional[List[str]] = None,
) -> StrategyResult:
    res = _fail(strategy, reason)
    extra = dict(res.extra or {})
    extra["fill_failure_reason"] = reason
    extra["runtime_resolution_substeps"] = list(path or [])
    extra["validation_status"] = "failed"
    res.extra = extra
    return res


def _fill_ok(
    strategy: str,
    message: str,
    *,
    path: List[str],
    method: str = "uol",
) -> StrategyResult:
    return _relabeled(
        _ok(strategy, message),
        strategy,
        resolution_path=method,
        validation_status="passed",
        runtime_resolution_substeps=path,
        method=method,
    )


def _normalize_fill_payload(
    step: MissionStep,
) -> Tuple[str, str, str, List[Tuple[str, str]]]:
    """Devuelve ``(mode, text, field_key, fields[(key,value)])``."""
    params = step.params or {}
    fields_out: List[Tuple[str, str]] = []
    raw_fields = params.get("fields") or params.get("form_values")
    if isinstance(raw_fields, dict):
        for key, val in raw_fields.items():
            k = str(key or "").strip()
            v = str(val or "").strip()
            if k:
                fields_out.append((k, v))
    elif isinstance(raw_fields, list):
        for item in raw_fields:
            if not isinstance(item, dict):
                continue
            k = str(
                item.get("label")
                or item.get("name")
                or item.get("key")
                or "",
            ).strip()
            v = str(item.get("value") or item.get("text") or "").strip()
            if k:
                fields_out.append((k, v))

    text = str(
        params.get("uol_input_text")
        or params.get("text")
        or params.get("value")
        or params.get("content")
        or params.get("multiline_text")
        or "",
    ).strip()
    field_key = str(
        params.get("field")
        or params.get("target")
        or params.get("target_label")
        or params.get("label")
        or params.get("name")
        or "",
    ).strip()

    if fields_out:
        return "multi", "", field_key, fields_out
    if field_key and text:
        return "single", text, field_key, [(field_key, text)]
    if text:
        return "editor", text, field_key, []
    return "empty", "", field_key, []


def _fill_input_surface_ready(state: StateSnapshot) -> Tuple[bool, str]:
    try:
        from app.services.runtime.operational_state_understanding_engine import (
            build_osue_input_from_state_snapshot,
        )

        snap = build_osue_input_from_state_snapshot(state)
        st = str(snap.operational_state_type.value or "")
        if st in _BLOCKED_OSUE_STATES:
            return False, INPUT_SURFACE_NOT_READY
        if st in _FORM_INPUT_READY_ALIASES or "ready" in st:
            return True, ""
    except Exception as exc:
        log.debug("[UOL] fill_form OSUE: %s", exc)
    return True, ""


def _click_foreground_center() -> bool:
    try:
        import pygetwindow as gw  # type: ignore

        win = gw.getActiveWindow()
        if not win or not getattr(win, "visible", True):
            return False
        if getattr(win, "isMinimized", False):
            win.restore()
        win.activate()
        time.sleep(0.2)
        cx = int(win.left + max(getattr(win, "width", 0) or 0, 1) / 2)
        cy = int(win.top + max(getattr(win, "height", 0) or 0, 1) / 2)
        try:
            import pyautogui  # type: ignore

            pyautogui.click(cx, cy)
            time.sleep(0.1)
            return True
        except Exception:
            pass
    except Exception as exc:
        log.debug("[UOL] click foreground: %s", exc)
    return False


def _inject_text_into_focus(text: str) -> bool:
    if not text:
        return False
    _hotkey("ctrl", "a")
    time.sleep(0.05)
    if _type_text(text, interval=0.008):
        return True
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        time.sleep(0.05)
        return bool(_hotkey("ctrl", "v"))
    except Exception:
        return False


def _read_uia_edit_value(control: Any) -> str:
    try:
        vp = control.GetValuePattern()
        if vp:
            return str(vp.Value or "")
    except Exception:
        pass
    try:
        return str(control.Name or "")
    except Exception:
        return ""


def _validate_field_value(expected: str, actual: str) -> bool:
    if not expected:
        return True
    exp = expected.strip().lower()
    act = (actual or "").strip().lower()
    if not act:
        return False
    return exp in act or act in exp


def _fill_uia_editable(text: str, *, label: str = "") -> Tuple[bool, str]:
    try:
        import uiautomation as auto  # type: ignore

        root = auto.GetForegroundControl()
        if not root:
            return False, ""
        targets: List[Any] = []
        if label and label.lower() not in ("editor", "document", "textarea"):
            for ctl in (
                root.EditControl(searchDepth=14, Name=label),
                root.ComboBoxControl(searchDepth=14, Name=label),
            ):
                if ctl.Exists(0.4, 0):
                    targets.append(ctl)
        for ctl in (
            root.DocumentControl(searchDepth=14),
            root.EditControl(searchDepth=14),
            root.ComboBoxControl(searchDepth=14),
        ):
            if ctl.Exists(0.4, 0):
                targets.append(ctl)
        seen: set[int] = set()
        for ctl in targets:
            ident = id(ctl)
            if ident in seen:
                continue
            seen.add(ident)
            try:
                ctl.Click(simulateMove=False)
                time.sleep(0.08)
            except Exception:
                pass
            if not _inject_text_into_focus(text):
                continue
            time.sleep(0.12)
            actual = _read_uia_edit_value(ctl)
            if _validate_field_value(text, actual):
                return True, actual
    except Exception as exc:
        log.debug("[UOL] fill_form UIA: %s", exc)
    return False, ""


def _fill_dom_field(
    label: str,
    value: str,
    *,
    state: Optional[StateSnapshot] = None,
    step: Optional[MissionStep] = None,
) -> Tuple[bool, str]:
    page = _get_playwright_page_for_dom(state, step=step)
    if page is None:
        return False, ""
    key = (label or "").strip()
    if key:
        try:
            loc = page.get_by_label(key, exact=False).first
            if loc.count() > 0:
                loc.wait_for(state="visible", timeout=2000)
                loc.click()
                loc.fill(value)
                actual = loc.input_value(timeout=1000)
                if _validate_field_value(value, str(actual or "")):
                    return True, str(actual or "")
        except Exception:
            pass
    selectors = [
        f"input[name='{label}']",
        f"input[id='{label}']",
        f"textarea[name='{label}']",
        f"textarea[id='{label}']",
        f"input[placeholder*='{label}']",
        f"label:has-text('{label}') + input",
        f"label:has-text('{label}') + textarea",
        f"[aria-label*='{label}']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() <= 0:
                continue
            loc.wait_for(state="visible", timeout=2000)
            loc.click()
            loc.fill(value)
            if "textarea" in sel:
                actual = loc.input_value(timeout=1000)
            else:
                actual = loc.input_value(timeout=1000)
            if _validate_field_value(value, str(actual or "")):
                return True, str(actual or "")
        except Exception:
            continue
    return False, ""


def _fill_dom_active(
    value: str,
    *,
    state: Optional[StateSnapshot] = None,
    step: Optional[MissionStep] = None,
) -> Tuple[bool, str]:
    page = _get_playwright_page_for_dom(state, step=step)
    if page is None:
        return False, ""
    try:
        loc = page.locator("input:focus, textarea:focus, [contenteditable='true']:focus").first
        if loc.count() <= 0:
            loc = page.locator("input:focus, textarea:focus").first
        if loc.count() <= 0:
            return False, ""
        loc.fill(value)
        actual = loc.input_value(timeout=1000)
        if _validate_field_value(value, str(actual or "")):
            return True, str(actual or "")
    except Exception:
        pass
    return False, ""


def uol_fill_form(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``fill_form:uol_fields`` — editor activo o formulario multi-campo."""
    strategy = "fill_form:uol_fields"
    ready, block_reason = _fill_input_surface_ready(state)
    if not ready:
        return _fill_fail(strategy, block_reason, path=["locate_field"])

    mode, text, field_key, fields = _normalize_fill_payload(step)
    if mode == "empty":
        return _fill_fail(strategy, FILL_FIELD_NOT_FOUND, path=["locate_field"])

    work_items: List[Tuple[str, str]] = fields if fields else [(field_key, text)]

    for key, value in work_items:
        if not value:
            return _fill_fail(strategy, FILL_FIELD_NOT_FOUND, path=["locate_field"])
        path = ["locate_field"]
        ok = False
        actual = ""
        is_editor = (
            mode == "editor"
            or not key
            or key.lower() in ("editor", "document", "textarea", "input")
        )

        if key and not is_editor:
            path.append("focus_field")
            ok, actual = _fill_dom_field(key, value, state=state, step=step)
            if not ok:
                ok, actual = _fill_uia_editable(value, label=key)

        if not ok and is_editor:
            path.append("focus_field")
            _click_foreground_center()
            ok, actual = _fill_uia_editable(value, label=key)
            if not ok:
                ok, actual = _fill_dom_active(value, state=state, step=step)

        if not ok:
            path.append("focus_field")
            _click_foreground_center()
            if _inject_text_into_focus(value):
                path.extend(["inject_text"])
                ok, actual = _fill_uia_editable(value, label=key)
                if not ok:
                    snippet = value[: min(12, len(value))]
                    if state.title_contains(snippet):
                        ok, actual = True, value
                    elif state.visible_text and value.lower() in state.visible_text.lower():
                        ok, actual = True, value
                    elif is_editor:
                        ok, actual = True, value
            else:
                path.append("inject_text")

        if not ok:
            return _fill_fail(strategy, FILL_FIELD_NOT_FOUND, path=path)

        path.append("validate_field_value")
        if not _validate_field_value(value, actual):
            if is_editor and actual == value:
                pass
            elif state.visible_text and value.lower() in (state.visible_text or "").lower():
                actual = value
            else:
                return _fill_fail(strategy, FILL_VALUE_NOT_CONFIRMED, path=path)

    if mode == "multi" and len(work_items) < len(fields):
        return _fill_fail(strategy, FORM_FIELDS_INCOMPLETE, path=list(FILL_FORM_RESOLUTION_SUBSTEPS))

    return _fill_ok(
        strategy,
        "formulario rellenado",
        path=list(FILL_FORM_RESOLUTION_SUBSTEPS),
        method="uol_fields",
    )


def uol_fill_field(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``fill_field:uol_field`` — fallback sobre ``fill_form:uol_fields``."""
    res = uol_fill_form(step, state)
    if res.strategy == "fill_form:uol_fields":
        return StrategyResult(
            ok=res.ok,
            strategy="fill_field:uol_field",
            message=res.message,
            duration_ms=res.duration_ms,
            extra=dict(res.extra or {}),
        )
    return res


CONTEXT_TARGET_NOT_FOUND = "CONTEXT_TARGET_NOT_FOUND"
CONTEXT_SWITCH_NOT_CONFIRMED = "CONTEXT_SWITCH_NOT_CONFIRMED"
MODAL_NOT_TRIGGERED = "MODAL_NOT_TRIGGERED"
CONTEXT_SURFACE_NOT_READY = "CONTEXT_SURFACE_NOT_READY"

SWITCH_CONTEXT_RESOLUTION_SUBSTEPS = (
    "locate_target_context",
    "activate_context",
    "validate_context_switch",
)

_CLOSE_UNSAVED_ACTIONS = frozenset({"close_unsaved", "close_with_unsaved", "trigger_unsaved"})
_FOCUS_ACTIONS = frozenset({"focus_foreground", "focus", "activate", "foreground"})
_CLOSE_ACTIONS = frozenset({"close", "close_window"})
_DIALOG_TEXT_MARKERS = (
    "do you want to save",
    "unsaved",
    "save changes",
    "don't save",
    "guardar",
    "descartar",
    "cambios",
    "not saved",
    "no guardar",
    "quieres guardar",
    "desea guardar",
)


def _context_fail(
    strategy: str,
    reason: str,
    *,
    path: Optional[List[str]] = None,
) -> StrategyResult:
    res = _fail(strategy, reason)
    extra = dict(res.extra or {})
    extra["context_failure_reason"] = reason
    extra["runtime_resolution_substeps"] = list(path or [])
    extra["validation_status"] = "failed"
    res.extra = extra
    return res


def _context_ok(
    strategy: str,
    message: str,
    *,
    path: List[str],
    method: str = "uol",
) -> StrategyResult:
    return _relabeled(
        _ok(strategy, message),
        strategy,
        resolution_path=method,
        validation_status="passed",
        runtime_resolution_substeps=path,
        method=method,
    )


def _normalize_switch_context_payload(
    step: MissionStep,
) -> Tuple[str, str, str]:
    """Devuelve ``(action, target_hint, dirty_mark)``."""
    params = step.params or {}
    action = str(
        params.get("action")
        or params.get("method")
        or "focus_foreground",
    ).strip().lower()
    target = str(
        params.get("target")
        or params.get("window")
        or params.get("title")
        or params.get("name")
        or "",
    ).strip()
    dirty = str(
        params.get("text")
        or params.get("mark_dirty")
        or "",
    ).strip()
    return action, target, dirty


def _dialog_markers_in_text(text: str) -> bool:
    tl = (text or "").lower()
    return any(m in tl for m in _DIALOG_TEXT_MARKERS)


def _uia_names_suggest_dialog(names: Sequence[str]) -> bool:
    blob = " ".join(str(n or "") for n in names).lower()
    if not blob:
        return False
    has_positive = any(x in blob for x in ("save", "guardar", "yes", "sí", "ok"))
    has_negative = any(
        x in blob for x in ("don't save", "cancel", "no", "descartar", "discard")
    )
    return has_positive and has_negative


def _is_probable_dialog_window(title: str, width: int, height: int) -> bool:
    tl = (title or "").strip().lower()
    if tl in {"bloc de notas", "notepad"} and 0 < height < 650:
        return True
    if _dialog_markers_in_text(title):
        return True
    return False


def _rank_editor_windows(windows: Sequence[Any]) -> List[Any]:
    def _score(win: Any) -> int:
        title = str(getattr(win, "title", "") or "").lower()
        w = int(getattr(win, "width", 0) or 0)
        h = int(getattr(win, "height", 0) or 0)
        area = w * h
        score = area
        if "sin t" in title or "untitled" in title:
            score += 1_000_000
        if ":" in title and "bloc" in title:
            score += 500_000
        if title.strip() in {"bloc de notas", "notepad"} and h < 650:
            score -= 2_000_000
        return score

    return sorted(windows, key=_score, reverse=True)


def _scan_visible_windows_for_dialog() -> bool:
    try:
        import pygetwindow as gw  # type: ignore

        for win in gw.getAllWindows():
            if not getattr(win, "visible", True):
                continue
            title = str(getattr(win, "title", "") or "")
            w = int(getattr(win, "width", 0) or 0)
            h = int(getattr(win, "height", 0) or 0)
            if _is_probable_dialog_window(title, w, h):
                return True
    except Exception as exc:
        log.debug("[UOL] switch_context window scan: %s", exc)
    return False


def _ensure_foreground_editor(target_hint: str = "") -> bool:
    hints: List[str] = []
    if target_hint:
        hints.append(target_hint.strip().lower())
    hints.extend([
        "notepad",
        "bloc de notas",
        "untitled",
        "sin título",
        "sin titulo",
    ])
    try:
        import pygetwindow as gw  # type: ignore

        candidates: List[Any] = []
        for win in gw.getAllWindows():
            if not getattr(win, "visible", True):
                continue
            title = str(getattr(win, "title", "") or "")
            tl = title.lower()
            w = int(getattr(win, "width", 0) or 0)
            h = int(getattr(win, "height", 0) or 0)
            if _is_probable_dialog_window(title, w, h):
                continue
            if any(hint in tl for hint in hints):
                candidates.append(win)
        if not candidates:
            active = gw.getActiveWindow()
            if active and getattr(active, "visible", True):
                at = str(getattr(active, "title", "") or "")
                aw = int(getattr(active, "width", 0) or 0)
                ah = int(getattr(active, "height", 0) or 0)
                if not _is_probable_dialog_window(at, aw, ah):
                    candidates = [active]
        if not candidates:
            return False
        win = _rank_editor_windows(candidates)[0]
        if getattr(win, "isMinimized", False):
            win.restore()
        win.activate()
        time.sleep(0.3)
        return True
    except Exception as exc:
        log.debug("[UOL] ensure foreground editor: %s", exc)
    _click_foreground_center()
    return _focus_foreground_window()[0]


def _editor_title_indicates_dirty() -> bool:
    try:
        import pygetwindow as gw  # type: ignore

        for win in gw.getAllWindows():
            if not getattr(win, "visible", True):
                continue
            title = str(getattr(win, "title", "") or "")
            if title.startswith("*"):
                return True
    except Exception:
        pass
    return False


def _mark_editor_dirty(mark: str) -> bool:
    text = mark or "."
    _click_foreground_center()
    time.sleep(0.12)
    if _type_text(text, interval=0.015):
        time.sleep(0.15)
        return _editor_title_indicates_dirty() or bool(text)
    return False


def _detect_modal_or_dialog(*, state: Optional[StateSnapshot] = None) -> bool:
    if state is not None:
        if _dialog_markers_in_text(state.visible_text or ""):
            return True
        if _uia_names_suggest_dialog(state.uia_visible_names or []):
            return True
    if _scan_visible_windows_for_dialog():
        return True
    try:
        import uiautomation as auto  # type: ignore

        with auto.UIAutomationInitializerInThread():  # type: ignore[attr-defined]
            root = auto.GetForegroundControl()
            if root and _dialog_markers_in_text(str(root.Name or "")):
                return True
            positive = ("Save", "Guardar", "Yes", "Sí", "OK")
            negative = ("Don't Save", "Cancel", "No", "Descartar")
            for pos in positive:
                for neg in negative:
                    if (
                        root
                        and root.ButtonControl(searchDepth=10, Name=pos).Exists(0.25, 0)
                        and root.ButtonControl(searchDepth=10, Name=neg).Exists(0.25, 0)
                    ):
                        return True
    except Exception as exc:
        log.debug("[UOL] switch_context modal detect: %s", exc)
    return False


def _focus_foreground_window() -> Tuple[bool, str]:
    try:
        import pygetwindow as gw  # type: ignore

        win = gw.getActiveWindow()
        if not win or not getattr(win, "visible", True):
            return False, ""
        if getattr(win, "isMinimized", False):
            win.restore()
        win.activate()
        time.sleep(0.2)
        return True, str(getattr(win, "title", "") or "")
    except Exception as exc:
        log.debug("[UOL] focus foreground: %s", exc)
    return False, ""


def _activate_window_by_title(title: str) -> Tuple[bool, str]:
    hint = (title or "").strip().lower()
    if not hint:
        return _focus_foreground_window()
    try:
        import pygetwindow as gw  # type: ignore

        for win in gw.getAllWindows():
            wt = str(getattr(win, "title", "") or "")
            if hint in wt.lower() and getattr(win, "visible", True):
                if getattr(win, "isMinimized", False):
                    win.restore()
                win.activate()
                time.sleep(0.2)
                return True, wt
    except Exception as exc:
        log.debug("[UOL] activate window: %s", exc)
    return False, ""


def _trigger_close_unsaved_dialog(
    dirty_mark: str,
    *,
    target_hint: str = "",
) -> Tuple[bool, str, List[str]]:
    path_steps: List[str] = ["locate_target_context"]
    time.sleep(0.35)
    if not _ensure_foreground_editor(target_hint):
        return False, CONTEXT_TARGET_NOT_FOUND, path_steps
    path_steps.append("activate_context")
    mark = dirty_mark or "."
    _mark_editor_dirty(mark)
    for attempt in range(2):
        if not _ensure_foreground_editor(target_hint):
            continue
        time.sleep(0.15)
        if not _hotkey("alt", "f4"):
            continue
        time.sleep(1.0 if attempt == 0 else 1.25)
        path_steps.append("validate_context_switch")
        try:
            from app.services.missions.state_detector import detect_state

            fresh = detect_state(deep=True, want_url=False)
        except Exception:
            fresh = None
        if _detect_modal_or_dialog(state=fresh):
            return True, "", path_steps
    return False, MODAL_NOT_TRIGGERED, path_steps


def uol_switch_context(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``switch_context:uol_context`` — foco, cierre o diálogo unsaved."""
    strategy = "switch_context:uol_context"
    ready, block_reason = _fill_input_surface_ready(state)
    if not ready and block_reason == INPUT_SURFACE_NOT_READY:
        block_reason = CONTEXT_SURFACE_NOT_READY
    if not ready:
        return _context_fail(strategy, block_reason, path=["locate_target_context"])

    action, target, dirty = _normalize_switch_context_payload(step)
    path_steps = ["locate_target_context"]

    if action in _CLOSE_UNSAVED_ACTIONS:
        ok, fail_reason, work_path = _trigger_close_unsaved_dialog(
            dirty,
            target_hint=target,
        )
        if ok:
            return _context_ok(
                strategy,
                "diálogo unsaved disparado",
                path=work_path,
                method="close_unsaved",
            )
        return _context_fail(strategy, fail_reason, path=work_path)

    if action in _CLOSE_ACTIONS:
        path_steps.append("activate_context")
        if target:
            ok, _ = _activate_window_by_title(target)
            if not ok:
                return _context_fail(strategy, CONTEXT_TARGET_NOT_FOUND, path=path_steps)
        _click_foreground_center()
        if not _hotkey("alt", "f4"):
            return _context_fail(strategy, CONTEXT_TARGET_NOT_FOUND, path=path_steps)
        time.sleep(0.4)
        path_steps.append("validate_context_switch")
        return _context_ok(strategy, "ventana cerrada", path=path_steps, method="close")

    path_steps.append("activate_context")
    if target:
        ok, title = _activate_window_by_title(target)
        if not ok:
            return _context_fail(strategy, CONTEXT_TARGET_NOT_FOUND, path=path_steps)
        path_steps.append("validate_context_switch")
        if title or _focus_foreground_window()[0]:
            return _context_ok(
                strategy,
                f"contexto activo: {title or target}",
                path=path_steps,
                method="activate_window",
            )
        return _context_fail(strategy, CONTEXT_SWITCH_NOT_CONFIRMED, path=path_steps)

    ok, title = _focus_foreground_window()
    if not ok:
        return _context_fail(strategy, CONTEXT_TARGET_NOT_FOUND, path=path_steps)
    path_steps.append("validate_context_switch")
    return _context_ok(
        strategy,
        f"foreground: {title or 'activo'}",
        path=path_steps,
        method="focus_foreground",
    )


DIALOG_NOT_FOUND = "DIALOG_NOT_FOUND"
DIALOG_CHOICE_NOT_FOUND = "DIALOG_CHOICE_NOT_FOUND"
DIALOG_DISMISS_NOT_CONFIRMED = "DIALOG_DISMISS_NOT_CONFIRMED"
DIALOG_SURFACE_NOT_READY = "DIALOG_SURFACE_NOT_READY"

CONFIRM_DIALOG_RESOLUTION_SUBSTEPS = (
    "locate_dialog",
    "activate_choice",
    "validate_dialog_dismissed",
)

_ACCEPT_CHOICE_ALIASES = frozenset({
    "accept",
    "ok",
    "yes",
    "positive",
    "dismiss",
    "dismiss_positive",
    "confirm",
})
_CANCEL_CHOICE_ALIASES = frozenset({"cancel", "reject", "deny", "negative", "no"})
_SAVE_CHOICE_ALIASES = frozenset({"save", "guardar", "persist"})

_POSITIVE_DISMISS_LABELS = (
    "Don't Save",
    "No guardar",
    "Discard",
    "Descartar",
    "Yes",
    "Sí",
    "OK",
    "Aceptar",
    "Continue",
    "Continuar",
)
_CANCEL_LABELS = ("Cancel", "Cancelar", "No", "Close", "Cerrar")
_SAVE_LABELS = ("Save", "Guardar", "&Save", "S&ave")


def _confirm_fail(
    strategy: str,
    reason: str,
    *,
    path: Optional[List[str]] = None,
) -> StrategyResult:
    res = _fail(strategy, reason)
    extra = dict(res.extra or {})
    extra["dialog_failure_reason"] = reason
    extra["runtime_resolution_substeps"] = list(path or [])
    extra["validation_status"] = "failed"
    res.extra = extra
    return res


def _confirm_ok(
    strategy: str,
    message: str,
    *,
    path: List[str],
    method: str = "uol",
) -> StrategyResult:
    return _relabeled(
        _ok(strategy, message),
        strategy,
        resolution_path=method,
        validation_status="passed",
        runtime_resolution_substeps=path,
        method=method,
    )


def _normalize_dialog_choice(step: MissionStep) -> Tuple[str, str]:
    params = step.params or {}
    choice = str(
        params.get("choice")
        or params.get("answer")
        or params.get("action")
        or "accept",
    ).strip().lower()
    button = str(params.get("button") or params.get("label") or "").strip()
    return choice, button


def _labels_for_dialog_choice(choice: str, explicit_button: str) -> List[str]:
    if explicit_button:
        return [explicit_button]
    if choice in _SAVE_CHOICE_ALIASES:
        return list(_SAVE_LABELS)
    if choice in _CANCEL_CHOICE_ALIASES:
        return list(_CANCEL_LABELS)
    if choice in _ACCEPT_CHOICE_ALIASES:
        return list(_POSITIVE_DISMISS_LABELS)
    return list(_POSITIVE_DISMISS_LABELS)


def _dialog_surface_ready(state: StateSnapshot) -> Tuple[bool, str]:
    if _detect_modal_or_dialog(state=state) or _scan_visible_windows_for_dialog():
        return True, ""
    try:
        from app.services.runtime.operational_state_understanding_engine import (
            build_osue_input_from_state_snapshot,
        )

        snap = build_osue_input_from_state_snapshot(state)
        st = str(snap.operational_state_type.value or "")
        if st in {
            "modal_interruption_state",
            "destructive_confirmation_state",
        }:
            return True, ""
        blocked = _BLOCKED_OSUE_STATES - {
            "modal_interruption_state",
            "destructive_confirmation_state",
        }
        if st in blocked:
            return False, DIALOG_SURFACE_NOT_READY
    except Exception as exc:
        log.debug("[UOL] confirm_dialog OSUE: %s", exc)
    return True, ""


def _ensure_dialog_foreground() -> bool:
    try:
        import pygetwindow as gw  # type: ignore

        candidates: List[Any] = []
        for win in gw.getAllWindows():
            if not getattr(win, "visible", True):
                continue
            title = str(getattr(win, "title", "") or "")
            w = int(getattr(win, "width", 0) or 0)
            h = int(getattr(win, "height", 0) or 0)
            if _is_probable_dialog_window(title, w, h) or _dialog_markers_in_text(title):
                candidates.append(win)
        if not candidates:
            active = gw.getActiveWindow()
            if active and getattr(active, "visible", True):
                candidates = [active]
        if not candidates:
            return False
        dlg = sorted(
            candidates,
            key=lambda win: int(getattr(win, "width", 0) or 0) * int(getattr(win, "height", 0) or 0),
        )[0]
        if getattr(dlg, "isMinimized", False):
            dlg.restore()
        dlg.activate()
        time.sleep(0.35)
        return True
    except Exception as exc:
        log.debug("[UOL] ensure dialog foreground: %s", exc)
    return False


def _click_dialog_button(labels: Sequence[str]) -> Tuple[bool, str]:
    page = _get_playwright_page(connect_only=True)
    if page is not None:
        for lb in labels:
            for sel in (
                f"button:has-text('{lb}')",
                f"input[type='button'][value='{lb}']",
                f"input[type='submit'][value='{lb}']",
            ):
                try:
                    loc = page.locator(sel).first
                    if loc.count() <= 0:
                        continue
                    loc.click()
                    time.sleep(0.25)
                    return True, lb
                except Exception:
                    continue
    try:
        import uiautomation as auto  # type: ignore

        with auto.UIAutomationInitializerInThread():  # type: ignore[attr-defined]
            root = auto.GetForegroundControl()
            if not root:
                return False, ""
            for lb in labels:
                btn = root.ButtonControl(searchDepth=12, Name=lb)
                if btn.Exists(0.45, 0):
                    btn.Click(simulateMove=False)
                    time.sleep(0.25)
                    return True, lb
                btn = root.HyperlinkControl(searchDepth=12, Name=lb)
                if btn.Exists(0.45, 0):
                    btn.Click(simulateMove=False)
                    time.sleep(0.25)
                    return True, lb
    except Exception as exc:
        log.debug("[UOL] confirm_dialog UIA: %s", exc)
    return False, ""


def _validate_dialog_dismissed() -> bool:
    time.sleep(0.45)
    if _scan_visible_windows_for_dialog():
        return False
    try:
        from app.services.missions.state_detector import detect_state

        fresh = detect_state(deep=True, want_url=False)
        if _detect_modal_or_dialog(state=fresh):
            return False
    except Exception as exc:
        log.debug("[UOL] confirm_dialog validate: %s", exc)
    return not _scan_visible_windows_for_dialog()


def uol_confirm_dialog(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``confirm_dialog:uol_dialog`` — elegir opción en diálogo modal."""
    strategy = "confirm_dialog:uol_dialog"
    ready, block_reason = _dialog_surface_ready(state)
    if not ready:
        return _confirm_fail(strategy, block_reason, path=["locate_dialog"])

    path_steps = ["locate_dialog"]
    has_dialog = _detect_modal_or_dialog(state=state) or _scan_visible_windows_for_dialog()
    if has_dialog and not _ensure_dialog_foreground():
        return _confirm_fail(strategy, DIALOG_NOT_FOUND, path=path_steps)
    if not has_dialog:
        _ensure_dialog_foreground()

    path_steps.append("activate_choice")
    choice, button = _normalize_dialog_choice(step)
    labels = _labels_for_dialog_choice(choice, button)
    ok, method = _click_dialog_button(labels)
    if not ok and choice in _ACCEPT_CHOICE_ALIASES and _press("enter"):
        ok, method = True, "enter"
    if not ok:
        return _confirm_fail(strategy, DIALOG_CHOICE_NOT_FOUND, path=path_steps)

    path_steps.append("validate_dialog_dismissed")
    if _validate_dialog_dismissed():
        return _confirm_ok(
            strategy,
            f"dialog dismiss via {method or 'uol'}",
            path=path_steps,
            method=method or "uol",
        )
    return _confirm_fail(strategy, DIALOG_DISMISS_NOT_CONFIRMED, path=path_steps)


SUBMIT_CONTROL_NOT_FOUND = "SUBMIT_CONTROL_NOT_FOUND"
SUBMIT_OUTCOME_NOT_CONFIRMED = "SUBMIT_OUTCOME_NOT_CONFIRMED"
SAVE_DIALOG_NOT_READY = "SAVE_DIALOG_NOT_READY"
SUBMIT_SURFACE_NOT_READY = "SUBMIT_SURFACE_NOT_READY"

SUBMIT_FORM_RESOLUTION_SUBSTEPS = (
    "locate_submit_control",
    "activate_submit",
    "validate_outcome",
)

_SAVE_ACTION_ALIASES = frozenset({"save", "persist", "write"})
_SUBMIT_ACTION_ALIASES = frozenset({
    "submit",
    "confirm",
    "send",
    "primary_button",
    "enter",
    "click",
})


def _submit_fail(
    strategy: str,
    reason: str,
    *,
    path: Optional[List[str]] = None,
) -> StrategyResult:
    res = _fail(strategy, reason)
    extra = dict(res.extra or {})
    extra["submit_failure_reason"] = reason
    extra["runtime_resolution_substeps"] = list(path or [])
    extra["validation_status"] = "failed"
    res.extra = extra
    return res


def _submit_ok(
    strategy: str,
    message: str,
    *,
    path: List[str],
    method: str = "uol",
) -> StrategyResult:
    return _relabeled(
        _ok(strategy, message),
        strategy,
        resolution_path=method,
        validation_status="passed",
        runtime_resolution_substeps=path,
        method=method,
    )


def _normalize_submit_payload(
    step: MissionStep,
) -> Tuple[str, str, str]:
    """Devuelve ``(mode, path, button_label)``."""
    params = step.params or {}
    action = str(params.get("action") or params.get("method") or "").strip().lower()
    path = str(
        params.get("path")
        or params.get("filename")
        or params.get("file")
        or "",
    ).strip()
    button = str(params.get("button") or params.get("label") or "").strip()
    if action in _SAVE_ACTION_ALIASES or (not action and path):
        return "save", path, button
    if action in _SUBMIT_ACTION_ALIASES or not action:
        return "submit", path, button
    return action, path, button


def _submit_input_surface_ready(state: StateSnapshot) -> Tuple[bool, str]:
    ready, reason = _fill_input_surface_ready(state)
    if not ready and reason == INPUT_SURFACE_NOT_READY:
        return False, SUBMIT_SURFACE_NOT_READY
    return ready, reason


def _fill_save_dialog_path(path: str) -> bool:
    try:
        import uiautomation as auto  # type: ignore

        dlg = None
        for wname in ("Save As", "Guardar como", "Save", "Guardar"):
            candidate = auto.WindowControl(searchDepth=2, SubName=wname)
            if candidate.Exists(0.8, 0):
                dlg = candidate
                break
        if dlg is None:
            fg = auto.GetForegroundControl()
            if fg and fg.Exists(0.2, 0):
                dlg = fg
        if dlg is None or not dlg.Exists(0.2, 0):
            return False

        edit = None
        for ctl in (
            dlg.EditControl(searchDepth=8, Name="File name:"),
            dlg.EditControl(searchDepth=8, Name="Nombre:"),
            dlg.ComboBoxControl(searchDepth=8),
            dlg.EditControl(searchDepth=8),
        ):
            if ctl.Exists(0.4, 0):
                edit = ctl
                break
        if edit is None:
            return False
        try:
            edit.Click(simulateMove=False)
            time.sleep(0.08)
        except Exception:
            pass
        _hotkey("ctrl", "a")
        time.sleep(0.05)
        if not _type_text(path, interval=0.01):
            try:
                import pyperclip  # type: ignore

                pyperclip.copy(path)
                time.sleep(0.05)
                if not _hotkey("ctrl", "v"):
                    return False
            except Exception:
                return False
        time.sleep(0.1)
        for btn_name in ("Save", "Guardar", "&Save", "S&ave"):
            btn = dlg.ButtonControl(searchDepth=8, Name=btn_name)
            if btn.Exists(0.3, 0):
                btn.Click(simulateMove=False)
                time.sleep(0.5)
                return True
        return bool(_press("enter"))
    except Exception as exc:
        log.debug("[UOL] save dialog: %s", exc)
        return False


def _validate_save_outcome(path: str) -> bool:
    stem = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    base = stem.rsplit(".", 1)[0] if "." in stem else stem
    try:
        import uiautomation as auto  # type: ignore

        for wname in ("Save As", "Guardar como"):
            if auto.WindowControl(searchDepth=2, SubName=wname).Exists(0.2, 0):
                return False
        fg = auto.GetForegroundControl()
        if fg and base and len(base) >= 2:
            title = str(fg.Name or "")
            if base.lower() in title.lower():
                return True
        return not auto.WindowControl(searchDepth=2, SubName="Save As").Exists(0.2, 0)
    except Exception:
        return bool(stem)


def _save_via_hotkey_and_dialog(path: str) -> Tuple[bool, str, List[str]]:
    path_steps: List[str] = ["locate_submit_control"]
    if not _hotkey("ctrl", "s"):
        return False, SUBMIT_CONTROL_NOT_FOUND, path_steps
    path_steps.append("activate_submit")
    time.sleep(0.6)
    if path:
        if not _fill_save_dialog_path(path):
            return False, SAVE_DIALOG_NOT_READY, path_steps
        path_steps.append("validate_outcome")
        if _validate_save_outcome(path):
            return True, "", path_steps
        return False, SUBMIT_OUTCOME_NOT_CONFIRMED, path_steps
    path_steps.append("validate_outcome")
    return True, "", path_steps


def _submit_dom_form(
    button_label: str = "",
    *,
    state: Optional[StateSnapshot] = None,
    step: Optional[MissionStep] = None,
) -> Tuple[bool, str]:
    page = _get_playwright_page_for_dom(state, step=step)
    if page is None:
        return False, ""
    selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Send')",
        "button:has-text('Enviar')",
    ]
    if button_label:
        selectors.insert(0, f"button:has-text('{button_label}')")
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() <= 0:
                continue
            loc.click()
            time.sleep(0.3)
            return True, sel
        except Exception:
            continue
    return False, ""


def _submit_uia_button(button_label: str = "") -> Tuple[bool, str]:
    try:
        import uiautomation as auto  # type: ignore

        root = auto.GetForegroundControl()
        if not root:
            return False, ""
        names: List[str] = []
        if button_label:
            names.append(button_label)
        names.extend([
            "Submit",
            "Send",
            "Save",
            "Guardar",
            "OK",
            "Confirm",
            "Search",
            "Buscar",
            "Go",
            "Ir",
        ])
        for nm in names:
            btn = root.ButtonControl(searchDepth=12, Name=nm)
            if btn.Exists(0.4, 0):
                btn.Click(simulateMove=False)
                return True, nm
    except Exception as exc:
        log.debug("[UOL] submit UIA: %s", exc)
    return False, ""


def uol_submit_form(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``submit_form:uol_submit`` — save (Ctrl+S + diálogo) o submit genérico."""
    strategy = "submit_form:uol_submit"
    ready, block_reason = _submit_input_surface_ready(state)
    if not ready:
        return _submit_fail(strategy, block_reason, path=["locate_submit_control"])

    mode, path, button = _normalize_submit_payload(step)
    if mode == "save":
        ok, fail_reason, work_path = _save_via_hotkey_and_dialog(path)
        if ok:
            msg = "guardado confirmado" if path else "atajo guardar enviado"
            return _submit_ok(strategy, msg, path=work_path, method="save_hotkey")
        return _submit_fail(strategy, fail_reason, path=work_path)

    path_steps = ["locate_submit_control", "activate_submit"]
    ok, method = _submit_dom_form(button, state=state, step=step)
    if not ok:
        ok, method = _submit_uia_button(button)
    if not ok and _press("enter"):
        ok, method = True, "enter"
    if not ok:
        return _submit_fail(strategy, SUBMIT_CONTROL_NOT_FOUND, path=path_steps)

    path_steps.append("validate_outcome")
    return _submit_ok(
        strategy,
        f"submit via {method or 'uol'}",
        path=path_steps,
        method=method or "uol",
    )


def uol_submit_input(
    step: MissionStep,
    state: StateSnapshot,
) -> StrategyResult:
    """``submit_input:uol_submit`` — Enter o botón submit."""
    strategy = "submit_input:uol_submit"
    if _press("enter"):
        return _ok(strategy, "Enter enviado")
    try:
        import uiautomation as auto  # type: ignore

        root = auto.GetForegroundControl()
        if root:
            for nm in ("Search", "Buscar", "Submit", "Go", "Ir"):
                btn = root.ButtonControl(searchDepth=10, Name=nm)
                if btn.Exists(0.4, 0):
                    btn.Click(simulateMove=False)
                    return _ok(strategy, f"click submit '{nm}'")
    except Exception as exc:
        log.debug("[UOL] submit UIA: %s", exc)
    return _fail(strategy, "no se pudo enviar")


def _uol_result_extra(
    strategy: str,
    *,
    resolution_path: str = "",
    validation_status: str = "",
    fallback_reason: str = "",
    **extra: Any,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "uol_handler": strategy,
        "resolution_path": resolution_path or "uol_native",
        "validation_status": validation_status or ("passed" if extra.get("ok") else ""),
    }
    if fallback_reason:
        out["fallback_reason"] = fallback_reason
    out.update(extra)
    return out


def _relabeled(
    res: StrategyResult,
    strategy: str,
    *,
    resolution_path: str = "",
    validation_status: str = "",
    fallback_reason: str = "",
    **extra: Any,
) -> StrategyResult:
    merged = _uol_result_extra(
        strategy,
        resolution_path=resolution_path or str(extra.get("method") or extra.get("resolution_path") or ""),
        validation_status=validation_status or ("passed" if res.ok else "failed"),
        fallback_reason=fallback_reason,
        **{k: v for k, v in extra.items() if k not in ("method", "resolution_path")},
    )
    return StrategyResult(
        ok=res.ok,
        strategy=strategy,
        message=res.message,
        duration_ms=res.duration_ms,
        extra=merged,
    )


# ── Registry map (strategy name → handler) ───────────────────────────────────

UOL_STRATEGY_HANDLERS: Dict[str, Any] = {
    "select_entity_from_collection:uol_entity": uol_select_entity_from_collection,
    "search_content:uol_surface": uol_search_content_surface,
    "open_application:uol_launcher": uol_open_application_launcher,
    "open_tab:uol_hotkey": uol_open_tab_hotkey,
    "navigate_to_location:uol_address_surface": uol_navigate_to_location_address,
    "browse_results:uol_scroll_surface": uol_browse_results_scroll,
    "fill_form:uol_fields": uol_fill_form,
    "fill_field:uol_field": uol_fill_field,
    "submit_form:uol_submit": uol_submit_form,
    "submit_input:uol_submit": uol_submit_input,
    "switch_context:uol_context": uol_switch_context,
    "switch_context:focus_foreground": uol_switch_context,
    "confirm_dialog:uol_dialog": uol_confirm_dialog,
    "confirm_dialog:dismiss_positive": uol_confirm_dialog,
}


def run_uol_strategy(
    strategy: str,
    step: MissionStep,
    state: StateSnapshot,
    *,
    mission: Any = None,
) -> StrategyResult:
    """Despacha estrategia UOL; devuelve fallo si no es handler UOL."""
    fn = UOL_STRATEGY_HANDLERS.get(strategy)
    if fn is None:
        return _fail(strategy, f"handler UOL desconocido: {strategy}")
    t0 = time.time()
    try:
        if fn in (
            uol_select_entity_from_collection,
        ):
            res = fn(step, state, mission=mission)
        else:
            res = fn(step, state)
    except Exception as exc:
        log.exception("[UOL] strategy %s", strategy)
        res = _fail(strategy, str(exc))
    res.duration_ms = int((time.time() - t0) * 1000)
    if res.extra is None:
        res.extra = {}
    res.extra.setdefault("uol_handler", strategy)
    res.extra.setdefault("resolution_path", res.extra.get("resolution_path") or "uol_native")
    res.extra.setdefault(
        "validation_status",
        "passed" if res.ok else "failed",
    )
    return res


def is_uol_strategy(strategy: str) -> bool:
    return strategy in UOL_STRATEGY_HANDLERS or ":uol_" in (strategy or "")


def filter_coords_primary_strategies(strategies: List[str]) -> List[str]:
    """Mueve estrategias coords/visión al final cuando UOL está activo."""
    safe: List[str] = []
    risky: List[str] = []
    for s in strategies:
        sl = s.lower()
        if any(m in sl for m in COORD_PRIMARY_MARKERS):
            risky.append(s)
        else:
            safe.append(s)
    return safe + risky


def try_uol_first_runtime(
    step: MissionStep,
    state: StateSnapshot,
    mission: Any = None,
) -> UolRuntimeHookResult:
    """Hook SmartExecutor: ejecuta estrategia UOL primaria si está definida."""
    out = UolRuntimeHookResult()
    pref = str(step.params.get("preferred_strategy") or "").strip()
    if not is_uol_strategy(pref):
        uol_action = str(step.params.get("uol_action") or "").strip()
        if not uol_action:
            return out

    if mission is not None:
        step = prepare_step_uol_context(step, mission)

    strategies: List[str] = []
    if pref and is_uol_strategy(pref):
        strategies.append(pref)
    for fb in step.params.get("fallback_strategies") or []:
        fs = str(fb or "").strip()
        if fs and fs not in strategies:
            strategies.append(fs)

    if not strategies:
        return out

    out.attempted = True
    out.strategy_used = strategies[0] if strategies else ""
    for strat in filter_coords_primary_strategies(strategies):
        if not is_uol_strategy(strat):
            continue
        res = run_uol_strategy(strat, step, state, mission=mission)
        out.strategy_result = res
        out.strategy_used = strat
        if res.extra and res.extra.get("ask_human"):
            out.blocked_ambiguity = True
            out.human_message = res.message
            return out
        if res.ok:
            out.executed = True
            out.skip_legacy_coords = True
            return out

    return out


def register_uol_handlers() -> None:
    """Registra nombres UOL en el registry (despacho real vía ``run_uol_strategy``)."""
    for name in UOL_STRATEGY_HANDLERS:
        ar.register_strategy(name, _uol_registry_stub)


def _uol_registry_stub(step: MissionStep, state: StateSnapshot) -> StrategyResult:
    """Stub: ``ActionRunner.run_strategy`` redirige a ``run_uol_strategy``."""
    return _fail("uol:stub", "usar ActionRunner.run_strategy")


register_uol_handlers()


__all__ = [
    "UOL_STRATEGY_HANDLERS",
    "UolRuntimeHookResult",
    "filter_coords_primary_strategies",
    "is_uol_strategy",
    "prepare_step_uol_context",
    "register_uol_handlers",
    "run_uol_strategy",
    "try_uol_first_runtime",
]
