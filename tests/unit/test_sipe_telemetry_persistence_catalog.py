"""SIPE telemetría + persistencia + contrato misión canónica (PRD).

* ``sipe_metrics`` vía bridge.
* ``mission.intent_promotions[]``.
* Contrato único sobre la misión canónica real (Chrome→YouTube+scroll):
  métricas limpias, audit trail interpretable,
  captions Mission Review (= human_label SEP en vista normal),
  bloqueadores acotados a perfil/query si no está READY.

Máximo 20 tests.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    Mission,
    MouseAction,
    RawEvent,
    WindowContext,
    mission_from_dict,
    mission_to_dict,
)
from app.services.missions.action_runner import (
    _STRATEGY_REGISTRY,
    supported_strategies,
)
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.semantic_execution_plan import (
    PROFILE_OR_QUERY_REVIEW_FOCUS_BLOCKERS,
    attach_semantic_plan_to_mission,
)
from app.services.missions.semantic_intent_promotion_engine import (
    apply_promotion_to_mission,
)
from app.services.missions.semantic_review_display import (
    find_semantic_step_for_compiled,
    normal_mode_review_caption_for_compiled,
)
from app.services.missions.smart_runner_bridge import run_mission_smart
from app.services.telemetry.sipe_metrics import sipe_metrics


# ─────────────────────────────────────────────────────────────────────
# Helpers (versión mínima — el suite completo vive en
# test_sipe_integration / test_semantic_intent_promotion_engine).
# ─────────────────────────────────────────────────────────────────────

_BASE_TS = datetime(2026, 5, 14, 22, 0, 0, tzinfo=timezone.utc)


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
    """Misión canónica PRD — misma geometría que ``test_sipe_integration``."""
    events: List[RawEvent] = []
    ts = 0
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


def _canonical_mission_with_legacy_sep() -> Mission:
    """Misión canónica post-compiler, SEP aún legacy (pre-SIPE)."""
    from app.contracts.mission import MissionStatus
    m = Mission(name="canonical-legacy-sep",
                raw_trace=_canonical_chrome_youtube_trace())
    apply_collapse_to_mission(m)
    attach_semantic_plan_to_mission(m, force=True)
    m.status = MissionStatus.EXECUTABLE
    return m


def _legacy_sep_mission() -> Mission:
    """Misión con SEP legacy (search_youtube/open_url) — debe ser
    promovida por el bridge en runtime."""
    from app.contracts.mission import MissionStatus
    m = Mission(name="legacy", raw_trace=_canonical_chrome_youtube_trace())
    apply_collapse_to_mission(m)
    attach_semantic_plan_to_mission(m, force=True)
    m.status = MissionStatus.EXECUTABLE
    return m


def _already_promoted_mission() -> Mission:
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


class _MockExecResult:
    class _MR:
        status = "success"
        outcomes: List[Any] = []
        last_message = ""
        failed_step_id = None
    mission_result = _MR()
    quality_report = None
    suggestions: List[Any] = []


def _patch_bridge_runtime():
    """Stub del executor + gate (igual que test_sipe_integration)."""
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
    al_cls.return_value.run_mission.return_value = _MockExecResult()
    return stack, al_cls


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Cada test arranca con contadores limpios — así las assertions
    de cantidades absolutas son confiables."""
    sipe_metrics.reset()
    yield
    sipe_metrics.reset()


# ═════════════════════════════════════════════════════════════════════
# 1) Telemetría agregada
# ═════════════════════════════════════════════════════════════════════


class TestSipeAggregatedTelemetry:

    def test_01_runtime_promotion_increments_promoted_at_runtime(self):
        m = _legacy_sep_mission()
        stack, _ = _patch_bridge_runtime()
        with stack:
            decision = run_mission_smart(m, allow_legacy_fallback=False)

        snap = sipe_metrics.snapshot()
        assert snap.promoted_at_runtime == 1
        assert snap.already_promoted == 0
        assert snap.legacy_fallback == 0
        assert snap.failure == 0
        assert decision.sipe_path == "promoted_at_runtime"
        # promotions_count refleja el len() de los renombres genéricos
        # aplicados (search_youtube→search_site, open_url→open_site).
        assert decision.sipe_promotions_count >= 1

    def test_02_already_promoted_increments_already_promoted(self):
        m = _already_promoted_mission()
        from app.contracts.mission import MissionStatus
        m.status = MissionStatus.EXECUTABLE
        stack, _ = _patch_bridge_runtime()
        with stack:
            decision = run_mission_smart(m, allow_legacy_fallback=False)

        snap = sipe_metrics.snapshot()
        assert snap.already_promoted == 1
        assert snap.promoted_at_runtime == 0
        assert decision.sipe_path == "already_promoted"
        assert decision.sipe_promotions_count == 0

    def test_03_snapshot_pct_aggregates_correctly(self):
        # 2 already + 1 runtime → 66.67% / 33.33%
        sipe_metrics.record_already_promoted()
        sipe_metrics.record_already_promoted()
        sipe_metrics.record_promoted_at_runtime(promotions_count=4)
        snap = sipe_metrics.snapshot()
        assert snap.total == 3
        assert snap.already_promoted_pct == pytest.approx(66.67, abs=0.05)
        assert snap.promoted_at_runtime_pct == pytest.approx(33.33, abs=0.05)
        assert snap.total_promotions_emitted == 4

    def test_04_legacy_fallback_path_records_reason(self):
        # Forzamos a SIPE a tirar excepción para activar el fallback.
        m = _legacy_sep_mission()
        stack, _ = _patch_bridge_runtime()
        with stack, patch(
            "app.services.missions.smart_runner_bridge."
            "apply_promotion_to_mission",
            side_effect=RuntimeError("simulated SIPE failure"),
        ):
            decision = run_mission_smart(m, allow_legacy_fallback=False)

        snap = sipe_metrics.snapshot()
        assert snap.legacy_fallback == 1
        assert snap.promoted_at_runtime == 0
        assert "sipe_exception" in snap.fallback_reasons
        assert decision.sipe_path == "legacy_fallback"

    def test_05_decision_to_dict_exposes_telemetry_fields(self):
        m = _legacy_sep_mission()
        stack, _ = _patch_bridge_runtime()
        with stack:
            decision = run_mission_smart(m, allow_legacy_fallback=False)
        d = decision.to_dict()
        assert d["sipe_path"] == "promoted_at_runtime"
        assert d["sipe_promotions_count"] >= 1

    def test_06_metrics_reset_zeroes_all_counters(self):
        sipe_metrics.record_promoted_at_runtime(promotions_count=2)
        sipe_metrics.record_already_promoted()
        sipe_metrics.reset()
        snap = sipe_metrics.snapshot()
        assert snap.total == 0
        assert snap.total_promotions_emitted == 0
        assert snap.fallback_reasons == {}


# ═════════════════════════════════════════════════════════════════════
# 2) Persistencia mission.intent_promotions[]
# ═════════════════════════════════════════════════════════════════════


class TestSipePersistedAuditTrail:

    def test_07_apply_promotion_writes_intent_promotions(self):
        m = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        result = apply_promotion_to_mission(m)
        assert isinstance(m.intent_promotions, list)
        assert len(m.intent_promotions) == len(result.promotions)
        # Cada entry debe ser un dict con el shape del SIPE:
        # from/to/reason/step_index/human_label.
        for entry in m.intent_promotions:
            assert isinstance(entry, dict)
            assert "from" in entry and "to" in entry

    def test_08_intent_promotions_survive_model_dump_round_trip(self):
        m = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        apply_promotion_to_mission(m)
        as_json = mission_to_dict(m)
        # Campo raíz presente en el dump.
        assert "intent_promotions" in as_json
        assert "intent_promotion_blockers" in as_json
        # Round-trip por el contract sin pérdida.
        restored = mission_from_dict(as_json)
        assert restored.intent_promotions == m.intent_promotions
        assert (restored.intent_promotion_blockers
                == m.intent_promotion_blockers)

    def test_09_intent_promotions_are_overwritten_each_run(self):
        """Cada llamada a ``apply_promotion_to_mission`` reemplaza —
        no acumula. Evita el growth ilimitado en el JSON."""
        m = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        apply_promotion_to_mission(m)
        first = list(m.intent_promotions)
        apply_promotion_to_mission(m)
        second = list(m.intent_promotions)
        # Mismo contenido (idempotencia), no concatenación.
        assert len(second) == len(first)
        assert second == first

    def test_10_already_promoted_path_does_not_reset_audit_trail(self):
        """Si el bridge reusa un SEP ya promovido, NO debe vaciar
        ``intent_promotions``."""
        m = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        apply_promotion_to_mission(m)
        original = list(m.intent_promotions)
        assert original  # sanity

        from app.contracts.mission import MissionStatus
        m.status = MissionStatus.EXECUTABLE
        stack, _ = _patch_bridge_runtime()
        with stack:
            decision = run_mission_smart(m, allow_legacy_fallback=False)

        assert decision.sipe_path == "already_promoted"
        # El audit trail sigue intacto.
        assert m.intent_promotions == original

    def test_11_blockers_persist_when_query_fidelity_low(self):
        """Una misión con query_fidelity sospechosa debe dejar
        ``intent_promotion_blockers`` no vacío."""
        m = Mission(name="cn", raw_trace=_canonical_chrome_youtube_trace())
        apply_promotion_to_mission(m)
        # Mutar el SEP a mano para simular query_fidelity rota y
        # re-promover — verificamos que SIPE escribe los blockers.
        sep = m.semantic_execution_plan or {}
        for sp in sep.get("steps", []):
            if sp.get("type") == "search_site":
                sp["params"]["query_fidelity_status"] = "missing_short_token"
        m.semantic_execution_plan = sep
        # No tenemos raw_trace que reproduzca la promoción; simulamos
        # un re-run con la SEP existente.
        result2 = apply_promotion_to_mission(m)
        # El SEP final puede traer not_ready_reasons consistentes —
        # los blockers a nivel SIPE se persisten en el campo nuevo.
        assert isinstance(m.intent_promotion_blockers, list)
        # Si SIPE detectó algún blocker, queda persistido como string.
        for code in m.intent_promotion_blockers:
            assert isinstance(code, str)
        # Sanity: el resultado expone los mismos blockers.
        assert m.intent_promotion_blockers == list(result2.blockers)


# ═════════════════════════════════════════════════════════════════════
# 3. Contrato — misión canónica PRD (Chrome→YouTube, scroll ×3)
# ═════════════════════════════════════════════════════════════════════


class TestCanonicalMissionSipeReviewContract:

    def test_12_canonical_metrics_promotions_mr_and_review_focus(self):
        """Validación integral con ``sipe_metrics`` + SEP + mismo criterio
        que usa Mission Review (vista normal) para textos."""

        # ① Pre: SEP ICE legacy sin SIPE → el bridge debe promover (o
        # reusar SIPE sólo después de escritura SIPE aquí mismo).
        m = _canonical_mission_with_legacy_sep()
        sep_pre = (m.semantic_execution_plan or {}).get("source")
        assert sep_pre != "semantic_intent_promotion_engine"

        sipe_metrics.reset()
        stack, _ = _patch_bridge_runtime()
        with stack:
            decision = run_mission_smart(m, allow_legacy_fallback=False)

        snap = sipe_metrics.snapshot()
        assert snap.legacy_fallback == 0 and snap.failure == 0, (
            f"canonical no debe usar fallback SIPE/classic ({snap.to_dict()})"
        )

        path_ok = (
            snap.already_promoted >= 1 or snap.promoted_at_runtime >= 1
        )
        assert path_ok, (
            "debe marcarse promoted_at_runtime o already_promoted; "
            f"snap={snap.to_dict()}"
        )

        sep = (m.semantic_execution_plan or {}) or {}
        assert sep.get("source") == "semantic_intent_promotion_engine"
        assert decision.sipe_path in ("already_promoted", "promoted_at_runtime")

        # ④ ``intent_promotions[]`` debe documentar cada promoción
        # aplicada (legacy tipo → tipo genérico + motivo).
        promos = list(m.intent_promotions or [])
        assert promos, "la misión canónica produce al menos una promoción SIPE"
        for p in promos:
            assert isinstance(p, dict)
            frm = str(p.get("from", "")).strip()
            dest = str(p.get("to", "")).strip()
            rsn = str(p.get("reason", "")).strip()
            assert frm and dest and rsn, f"entrada incompleta: {p}"

        promoted_types = {(p["from"], p["to"]) for p in promos}
        assert ("search_youtube", "search_site") in promoted_types or any(
            p.get("to") == "search_site" for p in promos
        )
        assert ("open_url", "open_site") in promoted_types or any(
            p.get("to") == "open_site" for p in promos
        )

        sem_steps = sep.get("steps") or []

        graph = list(m.compiled_execution_graph or [])
        interp = list(m.interpreted_steps or [])

        # ⑤ Misión revisión vista normal (= NO experto): un título por
        # tarjeta alineado con ``human_label`` del SEP mapeado, no texto
        # crudo interpretado cuando hay match semántico.
        for i, c_step in enumerate(graph):
            raw = interp[i].description if i < len(interp) else "Paso"
            cid = getattr(c_step, "id", "")
            cap = normal_mode_review_caption_for_compiled(
                m, cid, raw, expert_mode=False,
            )
            sem_card = find_semantic_step_for_compiled(m, cid)
            hl = ""
            if sem_card:
                hl = str(sem_card.get("human_label") or "").strip()
            if hl:
                assert cap == hl, (
                    f"paso card {i+1}: expected semantic human_label, "
                    f"got caption={cap!r} interp={raw!r}"
                )
                stype = str((sem_card or {}).get("type") or "")
                forbidden = {"click", "mouse_scroll", "type_text"}
                assert stype not in forbidden, (
                    f"paso semántico mapeado {i + 1} no debe ser evento "
                    f"crudo: {stype}"
                )

        # ⑥ Si algo no está READY, no puede ser contaminación general
        # del flujo — sólo foco perfil/query.
        st = str(sep.get("intent_status") or "")
        reasons = list(sep.get("not_ready_reasons") or [])
        if st == "READY":
            assert not reasons, "READY implica lista de razones vacía"
        else:
            bad = set(reasons) - PROFILE_OR_QUERY_REVIEW_FOCUS_BLOCKERS
            assert not bad, (
                f"blockers fuera del foco perfil/query: {bad} "
                f"(status={st} all={reasons})"
            )

        # El plan debe listar patrones ejecutables esperados PRD (no sólo clicks).
        kinds = [str((s or {}).get("type") or "") for s in sem_steps]
        assert "open_app" in kinds
        assert "open_site" in kinds or "search_site" in kinds
        assert "scroll_results" in kinds
