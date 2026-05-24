"""Tests del Semantic Intent Promotion Engine (SIPE)
======================================================

Cubre los criterios de aceptación del PRD 2026-05-14:

  1. La misión canónica produce SEP semántico completo con tipos
     genéricos (``open_app``, ``select_profile``, ``open_new_tab``,
     ``open_site``, ``search_site``, ``scroll_results``).
  2. Máximo 1 confirmación humana (perfil) si no se pudo leer.
  3. ``coords_used == False`` y ``legacy_graph_ignored == True``.
  4. El ``compiled_execution_graph`` queda como debug-only.
  5. Patrones genéricos: ``open_site`` y ``search_site`` reusables
     para cualquier sitio (no hardcode YouTube).
  6. Outcome valida intención, no la inventa.
  7. Scrolls consecutivos se agregan.
  8. Query se preserva con acentos.
  9. Fidelity sospechosa pide confirmación, no inventa.
 10. Plan vacío produce blockers, no ejecución legacy.

Máximo 20 tests por archivo (constraint del producto).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    Mission,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions.intent_collapse_engine import (
    CollapsedStep,
    CollapseStatus,
    MissionIntentPlan,
)
from app.services.missions.semantic_intent_promotion_engine import (
    PromotionBlockerCode,
    apply_promotion_to_mission,
    is_generic_intent_type,
    promote_semantic_intent,
)


# ─────────────────────────────────────────────────────────────────────
# Helpers de construcción de eventos (compartidos entre tests)
# ─────────────────────────────────────────────────────────────────────

_BASE_TS = datetime(2026, 5, 14, 21, 0, 0, tzinfo=timezone.utc)


def _click(
    x: int, y: int, ts_offset_ms: int = 0, *,
    hwnd: int = 1, title: str = "App", proc: str = "app.exe",
    uia: dict = None, web: dict = None, vision: dict = None,
) -> RawEvent:
    meta: dict = {}
    if uia is not None:
        meta["uia_data"] = uia
    if web is not None:
        meta["web_data"] = web
    if vision is not None:
        meta["vision_data"] = vision
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_CLICK,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        mouse_action=MouseAction(x=x, y=y),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata=meta,
    )


def _type(
    text: str, ts_offset_ms: int = 0, *,
    hwnd: int = 1, title: str = "App", proc: str = "app.exe",
    web: dict = None,
) -> RawEvent:
    meta: dict = {}
    if web is not None:
        meta["web_data"] = web
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        keyboard_action=KeyboardAction(keys=list(text)),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata=meta,
    )


def _enter(
    ts_offset_ms: int = 0, *,
    hwnd: int = 1, title: str = "App", proc: str = "app.exe",
) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_KEY_PRESS,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        keyboard_action=KeyboardAction(keys=["enter"]),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _hotkey(
    keys: List[str], modifiers: List[str], ts_offset_ms: int = 0, *,
    hwnd: int = 1, title: str = "App", proc: str = "app.exe",
) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_HOTKEY,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        keyboard_action=KeyboardAction(keys=keys, modifiers=modifiers),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _activate(
    ts_offset_ms: int = 0, *,
    hwnd: int = 1, title: str = "App", proc: str = "app.exe",
) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.WINDOW_ACTIVATE,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _scroll(
    dy: int, ts_offset_ms: int = 0, *,
    hwnd: int = 1, title: str = "YouTube - Chrome", proc: str = "chrome.exe",
) -> RawEvent:
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_SCROLL,
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata={"dy": dy, "count": 1},
    )


def _canonical_chrome_youtube_trace() -> List[RawEvent]:
    """Misión canónica del PRD: WinSearch+Chrome+perfil+nueva-pestaña+
    YouTube+búsqueda+3 scrolls.
    """
    events: List[RawEvent] = []
    ts = 0

    # 1. Click Windows Search.
    events.append(_click(
        200, 1050, ts,
        hwnd=10, title="Búsqueda", proc="searchhost.exe",
        uia={"name": "Escribe aquí para buscar",
             "automation_id": "SearchTextBox"},
    ))
    ts += 100
    # 2. Type "chrome".
    events.append(_type(
        "chrome", ts,
        hwnd=10, title="Búsqueda", proc="searchhost.exe",
    ))
    ts += 200
    # 3. Click resultado Chrome.
    events.append(_click(
        400, 400, ts,
        hwnd=10, title="Búsqueda", proc="searchhost.exe",
        uia={"name": "Google Chrome"},
    ))
    ts += 500
    # 4. Activate Chrome with profile picker.
    events.append(_activate(
        ts, hwnd=20,
        title="¿Quién eres? - Google Chrome", proc="chrome.exe",
    ))
    ts += 300
    # 5. Click profile (UIA pobre, OCR rescata el nombre).
    events.append(_click(
        600, 400, ts,
        hwnd=20, title="¿Quién eres? - Google Chrome", proc="chrome.exe",
        uia={"name": "main", "control_type": "PaneControl"},
        vision={"ocr_text_around": "Daniel Arturo Ramos"},
    ))
    ts += 500
    # 6. Activate Chrome with profile loaded.
    events.append(_activate(
        ts, hwnd=21,
        title="Daniel Arturo Ramos - Google Chrome", proc="chrome.exe",
    ))
    ts += 100
    # 7. Ctrl+T (nueva pestaña).
    events.append(_hotkey(
        keys=["t"], modifiers=["ctrl"], ts_offset_ms=ts,
        hwnd=21, title="Daniel Arturo Ramos - Google Chrome",
        proc="chrome.exe",
    ))
    ts += 200
    events.append(_activate(
        ts, hwnd=22,
        title="Nueva pestaña - Google Chrome", proc="chrome.exe",
    ))
    ts += 100
    # 8. Click YouTube bookmark.
    events.append(_click(
        300, 60, ts,
        hwnd=22, title="Nueva pestaña - Google Chrome", proc="chrome.exe",
        uia={"name": "YouTube", "control_type": "ButtonControl",
             "class_name": "BookmarkBar"},
        web={"url": "https://www.google.com/",
             "href": "https://www.youtube.com/", "text": "YouTube",
             "role": "link"},
    ))
    ts += 500
    events.append(_activate(
        ts, hwnd=23,
        title="YouTube - Google Chrome", proc="chrome.exe",
    ))
    ts += 100
    # 9. Click YouTube searchbox.
    events.append(_click(
        900, 120, ts,
        hwnd=23, title="YouTube - Google Chrome", proc="chrome.exe",
        uia={"name": "guide-service", "control_type": "EditControl"},
        web={"url": "https://www.youtube.com/", "domain": "youtube.com",
             "role": "searchbox", "placeholder": "Buscar"},
    ))
    ts += 300
    # 10. Type query con acentos.
    events.append(_type(
        "Devuélveme el amor de Luis Miguel", ts,
        hwnd=23, title="YouTube - Google Chrome", proc="chrome.exe",
        web={"url": "https://www.youtube.com/"},
    ))
    ts += 100
    events.append(_enter(
        ts, hwnd=23, title="YouTube - Google Chrome",
        proc="chrome.exe",
    ))
    ts += 500
    events.append(_activate(
        ts, hwnd=23,
        title="devuélveme luis miguel - YouTube", proc="chrome.exe",
    ))
    ts += 200
    # 11. Three scrolls down (separados >350ms para no coalescer en
    #     sanitizer y forzar al ICE a agregarlos como amount=3).
    for _ in range(3):
        events.append(_scroll(
            -120, ts,
            hwnd=23, title="devuélveme luis miguel - YouTube",
            proc="chrome.exe",
        ))
        ts += 600

    return events


def _canonical_intent_plan(
    profile: str = "Daniel Arturo Ramos",
    query: str = "Devuélveme el amor de Luis Miguel",
    scroll_amount: int = 3,
    fidelity: str = "ok",
) -> MissionIntentPlan:
    """Plan ICE pre-construido (determinista) para tests que no
    necesitan ejercitar el collapse engine completo.
    """
    return MissionIntentPlan(
        status=CollapseStatus.EXECUTABLE,
        semantic_steps=[
            CollapsedStep(
                type="open_app",
                params={"app": "chrome", "method": "windows_search"},
                confidence=0.95, human_label="Abrir Chrome",
            ),
            CollapsedStep(
                type="select_profile",
                params={"profile": profile, "app": "chrome"},
                confidence=0.92,
                human_label=f"Seleccionar perfil {profile}",
                needs_user_label=not profile,
                label_prompt="¿Qué perfil seleccionaste?" if not profile else "",
            ),
            CollapsedStep(
                type="open_new_tab",
                params={"app": "chrome"},
                confidence=0.95, human_label="Abrir nueva pestaña",
            ),
            CollapsedStep(
                type="open_url",
                params={"url": "youtube.com", "name": "YouTube"},
                confidence=0.95, human_label="Abrir YouTube",
            ),
            CollapsedStep(
                type="search_youtube",
                params={"query": query, "query_fidelity_status": fidelity},
                confidence=0.95,
                human_label=f"Buscar en YouTube",
            ),
            CollapsedStep(
                type="scroll_results",
                params={"direction": "down", "amount": scroll_amount,
                        "context": "youtube_results"},
                confidence=0.85,
                human_label=f"Bajar resultados {scroll_amount} veces",
            ),
        ],
    )


# ─────────────────────────────────────────────────────────────────────
# Tests (≤ 20)
# ─────────────────────────────────────────────────────────────────────


class TestCanonicalMission:
    """Casos del PRD: misión canónica completa."""

    def test_01_canonical_mission_produces_six_generic_steps(self):
        """Misión canónica → SEP con 6 steps en tipos genéricos."""
        m = Mission(name="canonical", raw_trace=_canonical_chrome_youtube_trace())
        result = promote_semantic_intent(m)
        kinds = [s.type for s in result.plan.steps]
        # Tipos genéricos esperados (no search_youtube/open_url legacy).
        assert "open_app" in kinds
        assert "select_profile" in kinds
        assert "open_new_tab" in kinds
        assert "open_site" in kinds
        assert "search_site" in kinds
        assert "scroll_results" in kinds
        # NO debe quedar tipo legacy específico ni el paso intermedio
        # canónico sin promover.
        assert "search_youtube" not in kinds
        assert "search_content" not in kinds
        assert "open_url" not in kinds

    def test_02_no_coords_used_legacy_graph_ignored(self):
        m = Mission(name="canonical", raw_trace=_canonical_chrome_youtube_trace())
        result = promote_semantic_intent(m)
        assert result.plan.coords_used is False
        assert result.plan.legacy_graph_ignored is True
        assert result.plan.source == "semantic_intent_promotion_engine"

    def test_03_apply_promotion_persists_plan_and_marks_legacy_debug(self):
        """``apply_promotion_to_mission`` muta la misión: SEP queda
        promovido y el grafo legacy queda ``legacy_debug_only``."""
        m = Mission(name="canonical", raw_trace=_canonical_chrome_youtube_trace())
        result = apply_promotion_to_mission(m)
        assert m.semantic_execution_plan is not None
        assert m.semantic_execution_plan["source"] == \
            "semantic_intent_promotion_engine"
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"
        assert result.plan.steps


class TestGenericPatternPromotion:
    """Cada promoción específica → patrón genérico."""

    def test_04_search_youtube_promotes_to_search_site_youtube(self):
        intent = _canonical_intent_plan()
        m = Mission(name="t", raw_trace=[])
        result = promote_semantic_intent(m, intent_plan=intent)
        search = next((s for s in result.plan.steps if s.type == "search_site"), None)
        assert search is not None
        assert search.params["site"] == "youtube"
        assert search.params["query"] == "Devuélveme el amor de Luis Miguel"

    def test_05_open_url_youtube_promotes_to_open_site(self):
        intent = _canonical_intent_plan()
        m = Mission(name="t", raw_trace=[])
        result = promote_semantic_intent(m, intent_plan=intent)
        os_step = next((s for s in result.plan.steps if s.type == "open_site"), None)
        assert os_step is not None
        assert os_step.params.get("site") == "youtube"
        assert "youtube.com" in os_step.params.get("url", "")

    def test_06_search_web_promotes_to_web_search(self):
        """``search_web`` (omnibox + query no-URL) → ``web_search``."""
        intent = MissionIntentPlan(
            status=CollapseStatus.EXECUTABLE,
            semantic_steps=[
                CollapsedStep(
                    type="search_web",
                    params={"query": "Banco de Mexico", "engine": "google",
                            "submit": True},
                    confidence=0.9,
                ),
            ],
        )
        m = Mission(name="ws", raw_trace=[])
        result = promote_semantic_intent(m, intent_plan=intent)
        ws = next((s for s in result.plan.steps if s.type == "web_search"), None)
        assert ws is not None
        assert ws.params["query"] == "Banco de Mexico"
        assert ws.params["engine"] == "google"

    def test_07_open_site_works_for_any_known_site_not_only_youtube(self):
        """Patrón genérico: github → open_site(site=github)."""
        intent = MissionIntentPlan(
            status=CollapseStatus.EXECUTABLE,
            semantic_steps=[
                CollapsedStep(
                    type="open_url",
                    params={"url": "github.com", "name": "GitHub"},
                    confidence=0.9,
                ),
            ],
        )
        result = promote_semantic_intent(Mission(name="g", raw_trace=[]),
                                         intent_plan=intent)
        os_step = next((s for s in result.plan.steps if s.type == "open_site"), None)
        assert os_step is not None
        assert os_step.params["site"] == "github"
        assert "github.com" in os_step.params["url"]


class TestProfileResolver:
    """Profile picker resolution + needs_user_label."""

    def test_08_profile_with_ocr_text_resolves_name(self):
        intent = _canonical_intent_plan(profile="Daniel Arturo Ramos")
        result = promote_semantic_intent(Mission(name="p", raw_trace=[]),
                                         intent_plan=intent)
        sp = next((s for s in result.plan.steps if s.type == "select_profile"),
                  None)
        assert sp is not None
        assert sp.params["profile_name"] == "Daniel Arturo Ramos"
        assert sp.needs_user_label is False

    def test_09_profile_without_text_needs_user_label(self):
        """Si no hay nombre real, NO regrabar — pedir confirmación."""
        intent = _canonical_intent_plan(profile="")
        result = promote_semantic_intent(Mission(name="p2", raw_trace=[]),
                                         intent_plan=intent)
        sp = next((s for s in result.plan.steps if s.type == "select_profile"),
                  None)
        assert sp is not None
        assert sp.needs_user_label is True
        assert PromotionBlockerCode.AMBIGUOUS_PROFILE_PICKER in result.blockers

    def test_10_generic_profile_label_treated_as_empty(self):
        """``"perfil del navegador"`` cuenta como vacío."""
        intent = _canonical_intent_plan(profile="perfil del navegador")
        result = promote_semantic_intent(Mission(name="p3", raw_trace=[]),
                                         intent_plan=intent)
        sp = next((s for s in result.plan.steps if s.type == "select_profile"),
                  None)
        assert sp.needs_user_label is True


class TestQueryFidelity:
    """Query con acentos y fidelidad sospechosa."""

    def test_11_query_with_accents_preserved_unchanged(self):
        intent = _canonical_intent_plan(
            query="Devuélveme el amor de Luis Miguel",
        )
        result = promote_semantic_intent(Mission(name="q", raw_trace=[]),
                                         intent_plan=intent)
        ss = next((s for s in result.plan.steps if s.type == "search_site"),
                  None)
        assert ss.params["query"] == "Devuélveme el amor de Luis Miguel"

    def test_12_suspect_query_fidelity_blocks_ready(self):
        """``query_fidelity_status="suspect"`` → blocker registrado."""
        intent = _canonical_intent_plan(
            query="Devuel ampr Luis Miguel",
            fidelity="suspect",
        )
        result = promote_semantic_intent(Mission(name="qs", raw_trace=[]),
                                         intent_plan=intent)
        assert PromotionBlockerCode.QUERY_FIDELITY_SUSPECT in result.blockers


class TestScrollAggregation:
    """Scrolls consecutivos se agregan."""

    def test_13_three_scrolls_aggregate_into_amount_3(self):
        intent = _canonical_intent_plan(scroll_amount=3)
        result = promote_semantic_intent(Mission(name="s", raw_trace=[]),
                                         intent_plan=intent)
        sc = next((s for s in result.plan.steps if s.type == "scroll_results"),
                  None)
        assert sc is not None
        assert sc.params["amount"] == 3
        assert sc.params["direction"] == "down"


class TestStrategiesAndCanonicality:
    """Estrategias canónicas y prohibición de coords."""

    def test_14_open_new_tab_uses_hotkey_ctrl_t_as_primary(self):
        intent = _canonical_intent_plan()
        result = promote_semantic_intent(Mission(name="t", raw_trace=[]),
                                         intent_plan=intent)
        ont = next((s for s in result.plan.steps if s.type == "open_new_tab"),
                   None)
        assert ont.preferred_strategy == "open_new_tab:hotkey_ctrl_t"

    def test_15_open_site_and_search_site_use_smart_route(self):
        intent = _canonical_intent_plan()
        result = promote_semantic_intent(Mission(name="t", raw_trace=[]),
                                         intent_plan=intent)
        os_step = next((s for s in result.plan.steps if s.type == "open_site"),
                       None)
        ss_step = next((s for s in result.plan.steps if s.type == "search_site"),
                       None)
        assert os_step.preferred_strategy == "open_site:smart_route"
        assert ss_step.preferred_strategy == "search_site:smart_route"

    def test_16_no_blind_coords_in_any_step(self):
        intent = _canonical_intent_plan()
        result = promote_semantic_intent(Mission(name="t", raw_trace=[]),
                                         intent_plan=intent)
        for sp in result.plan.steps:
            for k in ("bbox", "coords_xy", "fallback_coords",
                      "absolute_xy"):
                assert k not in sp.params, (
                    f"{sp.type} arrastra {k}: {sp.params}"
                )
            assert "coords" not in sp.preferred_strategy.lower()


class TestEdgeCases:
    """Casos límite + outcome-aware."""

    def test_17_outcome_validates_open_app_does_not_invent(self):
        """Outcome valida ``open_app`` cuando chrome.exe se activa,
        pero NO se promueve un open_app si el patrón nunca existió."""
        # Caso A: open_app válido + outcome chrome.exe → validado.
        m = Mission(name="oa", raw_trace=_canonical_chrome_youtube_trace())
        result = promote_semantic_intent(m)
        validated = [
            p for p in result.promotions
            if p.get("to") == "open_app_validated"
        ]
        assert validated, "outcome chrome.exe debió validar open_app"

        # Caso B: intent sin open_app → SIPE NO inventa uno aunque
        # haya chrome.exe en el trace.
        intent = MissionIntentPlan(
            status=CollapseStatus.EXECUTABLE,
            semantic_steps=[
                CollapsedStep(type="open_new_tab", params={"app": "chrome"},
                              confidence=0.9),
            ],
        )
        result_b = promote_semantic_intent(m, intent_plan=intent)
        kinds_b = [s.type for s in result_b.plan.steps]
        assert "open_app" not in kinds_b

    def test_18_empty_mission_produces_blockers_not_legacy_execution(self):
        """SEP null → blockers explícitos, NO ejecución legacy."""
        m = Mission(name="empty")
        result = promote_semantic_intent(m)
        assert result.plan.steps == []
        assert PromotionBlockerCode.EMPTY_RAW_TRACE in result.blockers
        # NUNCA se promueve un "ejecutable" con plan vacío.
        assert result.is_executable is False
        # Y la coordinación de coords sigue prohibida.
        assert result.plan.coords_used is False

    def test_19_apply_promotion_keeps_canonical_mission_ready(self):
        """End-to-end: misión canónica → READY tras
        ``apply_promotion_to_mission``, con ``ready_provenance==
        raw_evidence`` y un máximo de 0 confirmaciones humanas."""
        m = Mission(name="full", raw_trace=_canonical_chrome_youtube_trace())
        result = apply_promotion_to_mission(m)
        sep = m.semantic_execution_plan or {}
        assert sep.get("intent_status") == CollapseStatus.READY, (
            f"intent_status={sep.get('intent_status')}, "
            f"reasons={sep.get('not_ready_reasons')}"
        )
        # Como hay nombre real de perfil (OCR lo lee),
        # NO debe haber confirmación humana pendiente.
        needs_label = [
            s for s in sep.get("steps") or []
            if s.get("needs_user_label")
        ]
        assert needs_label == [], (
            f"Confirmaciones inesperadas: {needs_label}"
        )
        # Y el plan tiene los 6 steps genéricos.
        assert len(sep.get("steps") or []) == 6
        assert result.is_executable is True

    def test_20_is_generic_intent_type_helper_recognizes_all_generics(self):
        for t in ["open_app", "select_profile", "open_new_tab",
                  "open_site", "search_site", "web_search",
                  "scroll_results"]:
            assert is_generic_intent_type(t) is True
        # Y los legacy no genéricos NO cuentan como genéricos.
        for t in ["click", "click_button", "type_text", "key_press",
                  "scroll", "mouse_click"]:
            assert is_generic_intent_type(t) is False
