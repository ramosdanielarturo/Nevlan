"""Integration tests para SIPE wiring (PRD 2026-05-14 §next-step).

Cubre:

  * El ``smart_runner_bridge`` invoca ``apply_promotion_to_mission``
    automáticamente cuando construye el SEP — el plan resultante usa
    tipos genéricos (``open_site``/``search_site``) y queda persistido
    con ``source = "semantic_intent_promotion_engine"``.
  * El bridge es **idempotente**: una segunda ejecución NO re-promueve
    si ya está promovido.
  * ``bulk_recompile.sipe_migrate_missions`` migra correctamente un
    batch de misiones (mezcla de ya-migradas, nuevas y errores).
  * La migración es no destructiva por default (``in_place=False``).

Máximo 20 tests (constraint del producto).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import patch

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    Mission,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions.bulk_recompile import (
    SipeMigrationRow,
    sipe_migrate_missions,
)
from app.services.missions.intent_collapse_engine import apply_collapse_to_mission
from app.services.missions.semantic_execution_plan import (
    attach_semantic_plan_to_mission,
)
from app.services.missions.smart_runner_bridge import run_mission_smart


# ─────────────────────────────────────────────────────────────────────
# Helpers (subset reusado de test_semantic_intent_promotion_engine).
# ─────────────────────────────────────────────────────────────────────

_BASE_TS = datetime(2026, 5, 14, 21, 30, 0, tzinfo=timezone.utc)


def _click(x, y, ts, *, hwnd, title, proc, uia=None, web=None, vision=None):
    meta: Dict[str, Any] = {}
    if uia:
        meta["uia_data"] = uia
    if web:
        meta["web_data"] = web
    if vision:
        meta["vision_data"] = vision
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_CLICK,
        timestamp=_BASE_TS + timedelta(milliseconds=ts),
        mouse_action=MouseAction(x=x, y=y),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata=meta,
    )


def _type(text, ts, *, hwnd, title, proc, web=None):
    meta: Dict[str, Any] = {}
    if web:
        meta["web_data"] = web
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        timestamp=_BASE_TS + timedelta(milliseconds=ts),
        keyboard_action=KeyboardAction(keys=list(text)),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata=meta,
    )


def _enter(ts, *, hwnd, title, proc):
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_KEY_PRESS,
        timestamp=_BASE_TS + timedelta(milliseconds=ts),
        keyboard_action=KeyboardAction(keys=["enter"]),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _hotkey(keys, modifiers, ts, *, hwnd, title, proc):
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.KEYBOARD_HOTKEY,
        timestamp=_BASE_TS + timedelta(milliseconds=ts),
        keyboard_action=KeyboardAction(keys=keys, modifiers=modifiers),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _activate(ts, *, hwnd, title, proc):
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.WINDOW_ACTIVATE,
        timestamp=_BASE_TS + timedelta(milliseconds=ts),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
    )


def _scroll(dy, ts, *, hwnd, title, proc):
    return RawEvent(
        id=str(uuid.uuid4()),
        event_type=EventType.MOUSE_SCROLL,
        timestamp=_BASE_TS + timedelta(milliseconds=ts),
        window_context=WindowContext(hwnd=hwnd, title=title, process_name=proc),
        metadata={"dy": dy, "count": 1},
    )


def _canonical_chrome_youtube_trace() -> List[RawEvent]:
    """Misión canónica del PRD reducida a lo mínimo para SIPE."""
    events: List[RawEvent] = []
    ts = 0
    # Windows Search → Chrome.
    events.append(_click(200, 1050, ts, hwnd=10, title="Búsqueda",
                         proc="searchhost.exe",
                         uia={"name": "Escribe aquí para buscar",
                              "automation_id": "SearchTextBox"}))
    ts += 100
    events.append(_type("chrome", ts, hwnd=10, title="Búsqueda",
                        proc="searchhost.exe"))
    ts += 200
    events.append(_click(400, 400, ts, hwnd=10, title="Búsqueda",
                         proc="searchhost.exe",
                         uia={"name": "Google Chrome"}))
    ts += 500
    # Profile picker.
    events.append(_activate(ts, hwnd=20,
                            title="¿Quién eres? - Google Chrome",
                            proc="chrome.exe"))
    ts += 300
    events.append(_click(600, 400, ts, hwnd=20,
                         title="¿Quién eres? - Google Chrome",
                         proc="chrome.exe",
                         uia={"name": "main", "control_type": "PaneControl"},
                         vision={"ocr_text_around": "Daniel Arturo Ramos"}))
    ts += 500
    events.append(_activate(ts, hwnd=21,
                            title="Daniel Arturo Ramos - Google Chrome",
                            proc="chrome.exe"))
    ts += 100
    # Ctrl+T + bookmark YouTube + searchbox + query + Enter + scrolls.
    events.append(_hotkey(["t"], ["ctrl"], ts, hwnd=21,
                          title="Daniel Arturo Ramos - Google Chrome",
                          proc="chrome.exe"))
    ts += 200
    events.append(_activate(ts, hwnd=22,
                            title="Nueva pestaña - Google Chrome",
                            proc="chrome.exe"))
    ts += 100
    events.append(_click(300, 60, ts, hwnd=22,
                         title="Nueva pestaña - Google Chrome",
                         proc="chrome.exe",
                         uia={"name": "YouTube",
                              "control_type": "ButtonControl",
                              "class_name": "BookmarkBar"},
                         web={"url": "https://www.google.com/",
                              "href": "https://www.youtube.com/",
                              "text": "YouTube", "role": "link"}))
    ts += 500
    events.append(_activate(ts, hwnd=23,
                            title="YouTube - Google Chrome",
                            proc="chrome.exe"))
    ts += 100
    events.append(_click(900, 120, ts, hwnd=23,
                         title="YouTube - Google Chrome",
                         proc="chrome.exe",
                         uia={"name": "guide-service",
                              "control_type": "EditControl"},
                         web={"url": "https://www.youtube.com/",
                              "domain": "youtube.com", "role": "searchbox",
                              "placeholder": "Buscar"}))
    ts += 300
    events.append(_type("Devuélveme el amor de Luis Miguel", ts, hwnd=23,
                        title="YouTube - Google Chrome", proc="chrome.exe",
                        web={"url": "https://www.youtube.com/"}))
    ts += 100
    events.append(_enter(ts, hwnd=23, title="YouTube - Google Chrome",
                         proc="chrome.exe"))
    ts += 500
    events.append(_activate(ts, hwnd=23,
                            title="devuélveme luis miguel - YouTube",
                            proc="chrome.exe"))
    ts += 200
    for _ in range(3):
        events.append(_scroll(-120, ts, hwnd=23,
                              title="devuélveme luis miguel - YouTube",
                              proc="chrome.exe"))
        ts += 600
    return events


def _empty_legacy_mission() -> Mission:
    """Misión sin raw_trace y sin SEP — caso degenerate."""
    return Mission(name="empty-legacy")


def _canonical_mission_with_legacy_sep() -> Mission:
    """Misión canónica que ya pasó por compiler/ICE pero todavía
    tiene el SEP con tipos legacy (search_youtube/open_url) —
    el caso típico de una misión migrada desde antes del SIPE.
    """
    from app.contracts.mission import MissionStatus
    m = Mission(name="legacy-sep",
                raw_trace=_canonical_chrome_youtube_trace())
    # Reproducimos el flujo completo recorder → compiler → SEP, pero
    # sin pasar por SIPE. Esto deja la misión con un SEP legacy
    # (tipos search_youtube/open_url/search_web).
    apply_collapse_to_mission(m)
    attach_semantic_plan_to_mission(m, force=True)
    # Forzamos status EXECUTABLE para que el gate del bridge no
    # bloquee — el resto del pipeline (approval_gate) es ortogonal
    # a este test y ya tiene su propia cobertura.
    m.status = MissionStatus.EXECUTABLE
    return m


def _already_promoted_mission() -> Mission:
    """Misión cuyo SEP ya viene marcado con source=SIPE."""
    m = Mission(name="already")
    m.semantic_execution_plan = {
        "source": "semantic_intent_promotion_engine",
        "version": 2,
        "average_confidence": 0.9,
        "coords_used": False,
        "legacy_graph_ignored": True,
        "intent_status": "READY",
        "not_ready_reasons": [],
        "ready_provenance": "raw_evidence",
        "steps": [{
            "id": "x", "type": "open_app",
            "params": {"name": "chrome", "method": "windows_search"},
            "preferred_strategy": "open_app:windows_search",
            "fallback_strategies": [],
            "confidence": 0.95,
            "human_label": "Abrir Chrome",
            "needs_user_label": False,
            "label_prompt": "",
            "block_id": None,
        }],
    }
    return m


# ─────────────────────────────────────────────────────────────────────
# Bridge SIPE-wiring tests
# ─────────────────────────────────────────────────────────────────────


class _MockExecutorResult:
    """Resultado mock del AutoLearningExecutor para tests del bridge."""

    class _MR:
        status = "success"
        outcomes: List[Any] = []
        last_message = ""
        failed_step_id = None

    mission_result = _MR()
    quality_report = None
    suggestions: List[Any] = []


def _patch_bridge_runtime():
    """Devuelve un context manager combinado que stubea executor + gate.

    El gate (``classify_status``) y el approval-check (``check_mission_approvable``)
    son ortogonales al wiring de SIPE — ya tienen tests propios en
    ``test_smart_runner_bridge.py``. Aquí los neutralizamos para
    aislar la verificación del wiring nuevo.
    """
    from contextlib import ExitStack
    from app.contracts.mission import MissionStatus

    class _Report:
        errors: List[Any] = []

    stack = ExitStack()
    stack.enter_context(patch(
        "app.services.missions.smart_runner_bridge.classify_status",
        return_value=MissionStatus.EXECUTABLE,
    ))
    stack.enter_context(patch(
        "app.services.missions.smart_runner_bridge.check_mission_approvable",
        return_value=_Report(),
    ))
    al_cls = stack.enter_context(patch(
        "app.services.missions.smart_runner_bridge.AutoLearningExecutor",
        create=True,
    ))
    stack.enter_context(patch(
        "app.services.missions.smart_runner_bridge._AUTO_LEARNING_AVAILABLE",
        True,
    ))
    al_cls.return_value.run_mission.return_value = _MockExecutorResult()
    return stack, al_cls


class TestBridgePromotesViaSipe:
    """El ``smart_runner_bridge`` debe invocar SIPE automáticamente."""

    def test_01_bridge_persists_sipe_source_when_building_plan(self):
        """Una misión con SEP legacy (search_youtube/open_url) → tras
        run_mission_smart su SEP queda con source=SIPE."""
        m = _canonical_mission_with_legacy_sep()
        # Verificación previa: el SEP existe pero NO está promovido.
        assert (m.semantic_execution_plan or {}).get("source") != \
            "semantic_intent_promotion_engine"

        stack, _ = _patch_bridge_runtime()
        with stack:
            run_mission_smart(m, allow_legacy_fallback=False)

        sep = m.semantic_execution_plan or {}
        assert sep.get("source") == "semantic_intent_promotion_engine"
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"

    def test_02_bridge_emits_generic_step_kinds(self):
        """El SEP promovido expone tipos genéricos; ``MissionStep.kind``
        sigue al SEP (``search_site`` para YouTube promovido)."""
        m = _canonical_mission_with_legacy_sep()
        captured_steps: List[Any] = []

        stack, al_cls = _patch_bridge_runtime()
        al_cls.return_value.run_mission.side_effect = (
            lambda steps, **kw: (
                captured_steps.extend(steps),
                _MockExecutorResult(),
            )[1]
        )
        with stack:
            decision = run_mission_smart(m, allow_legacy_fallback=False)

        assert decision.player_used == "smart"
        assert decision.source == "semantic_execution_plan"
        kinds_exec = [s.kind for s in captured_steps]
        assert decision.semantic_step_kinds == [
            "open_app", "select_profile", "open_new_tab",
            "open_site", "search_site", "scroll_results",
        ]
        assert "open_site" in kinds_exec
        assert "open_url" not in kinds_exec
        assert "search_site" in kinds_exec
        assert "search_youtube" not in kinds_exec
        assert kinds_exec == [
            "open_app", "select_profile", "open_new_tab",
            "open_site", "search_site", "scroll_results",
        ]

    def test_03_bridge_is_idempotent_when_plan_already_promoted(self):
        """Si la misión ya tiene SEP source=SIPE, el bridge NO
        re-promueve (reusa)."""
        m = _already_promoted_mission()
        m.legacy_compiled_graph_purpose = "legacy_debug_only"

        stack, _ = _patch_bridge_runtime()
        with stack, patch(
            "app.services.missions.smart_runner_bridge."
            "apply_promotion_to_mission"
        ) as sipe_call:
            run_mission_smart(m, allow_legacy_fallback=False)

        assert sipe_call.call_count == 0, (
            "Bridge re-promovió un SEP ya migrado"
        )

    def test_04_decision_marks_legacy_graph_ignored_true(self):
        """``RunnerDecision.legacy_graph_ignored == True`` cuando
        SIPE construyó el plan."""
        m = _canonical_mission_with_legacy_sep()
        stack, _ = _patch_bridge_runtime()
        with stack:
            decision = run_mission_smart(m, allow_legacy_fallback=False)
        assert decision.legacy_graph_ignored is True
        assert decision.coords_used is False


# ─────────────────────────────────────────────────────────────────────
# bulk_recompile.sipe_migrate_missions tests
# ─────────────────────────────────────────────────────────────────────


class TestBulkSipeMigration:
    """Migración 1-shot del store."""

    def test_05_migrates_canonical_mission_to_generic_kinds(self):
        m = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        rows = sipe_migrate_missions([m])
        assert len(rows) == 1
        row = rows[0]
        assert row.ok is True
        assert row.already_promoted is False
        assert "open_site" in row.steps_after
        assert "search_site" in row.steps_after
        # search_youtube/open_url ya no son tipos del SEP final.
        assert "search_youtube" not in row.steps_after
        assert "open_url" not in row.steps_after
        # La misión original NO se mutó (in_place=False default).
        assert m.semantic_execution_plan is None or (
            (m.semantic_execution_plan or {}).get("source")
            != "semantic_intent_promotion_engine"
        )
        # La copia migrada SÍ tiene el SEP promovido.
        assert row.migrated_mission is not None
        assert row.migrated_mission.semantic_execution_plan["source"] == \
            "semantic_intent_promotion_engine"

    def test_06_skips_already_promoted_by_default(self):
        m = _already_promoted_mission()
        rows = sipe_migrate_missions([m])
        assert len(rows) == 1
        row = rows[0]
        assert row.already_promoted is True
        assert row.ok is True
        # No se cambian los kinds.
        assert row.steps_before == row.steps_after

    def test_07_force_repromotes_already_migrated(self):
        m = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        # Primer pase: deja la misión migrada.
        rows1 = sipe_migrate_missions([m], in_place=True)
        assert rows1[0].ok is True
        # Segundo pase con skip default: la trata como already_promoted.
        rows2 = sipe_migrate_missions([m])
        assert rows2[0].already_promoted is True
        # Tercer pase con skip=False: SÍ re-promueve.
        rows3 = sipe_migrate_missions([m], skip_already_promoted=False)
        assert rows3[0].already_promoted is False
        assert rows3[0].ok is True

    def test_08_in_place_mutates_original_mission(self):
        m = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        rows = sipe_migrate_missions([m], in_place=True)
        assert rows[0].migrated_mission is m
        assert (m.semantic_execution_plan or {}).get("source") == \
            "semantic_intent_promotion_engine"

    def test_09_handles_empty_mission_without_aborting_batch(self):
        """Una misión vacía no debe romper la migración del resto."""
        good = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        empty = _empty_legacy_mission()
        rows = sipe_migrate_missions([empty, good])
        assert len(rows) == 2
        # Empty puede quedar ok=False pero NO debe romper batch.
        assert rows[0].error is None or rows[0].error == ""
        # La buena migra correctamente.
        assert rows[1].ok is True
        assert "open_site" in rows[1].steps_after

    def test_10_migration_results_are_deterministic(self):
        """Aplicar migración dos veces sobre copias produce el mismo
        SEP — propiedad necesaria para idempotencia del store."""
        m1 = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        m2 = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        r1 = sipe_migrate_missions([m1])[0]
        r2 = sipe_migrate_missions([m2])[0]
        assert r1.steps_after == r2.steps_after
        assert r1.intent_status == r2.intent_status
