"""
Unit tests — ``app.services.missions.smart_runner_bridge``
==========================================================

Cubre PRD 2026-05-03d §1 (routing Smart vs Legacy), §3 (gate de
``classify_status`` antes de ejecutar), §4 (callbacks humanos) y §14
(criterio final de aceptación).

Los tests NO tocan el sistema: parchean :class:`ActionRunner` y
:class:`StateDetector` para simular un escenario "todo sale bien" y
otros de fallo controlado.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    InterpretedStep,
    Mission,
    MissionStatus,
    TargetContext,
    ValidationStrategy,
)
from app.services.missions import action_runner as _ar
from app.services.missions import state_detector as _sd
from app.services.missions.action_runner import StrategyResult
from app.services.missions.smart_runner_bridge import (
    RunnerDecision,
    decide_player,
    run_mission_smart,
)
from app.services.missions.state_detector import StateSnapshot


class _VirtualClock:
    """Reloj coordinado para tests: ``sleep(dt)`` avanza el tiempo virtual.

    Parchear solo ``time.sleep→no-op`` NO acelera ``SmartMissionExecutor``
    porque ``_await_postcondition`` corta cuando ``time.time()`` real supera el
    deadline (bucle a máxima velocidad pero aún espera tiempo de pared).
    Aquí ambas funciones comparten la misma escala temporal.
    """

    def __init__(self) -> None:
        self._t = 0.0

    def time(self) -> float:
        return self._t

    def sleep(self, secs: float) -> None:
        self._t += float(secs)


@pytest.fixture(autouse=True)
def _virtual_clock_for_smart_executor(monkeypatch):
    """Acelera postconditions/recuperaciones sin tocar código productivo."""
    clk = _VirtualClock()
    monkeypatch.setattr(time, "time", clk.time)
    monkeypatch.setattr(time, "sleep", clk.sleep)


def _tc(name: str = "x", process: str = "chrome.exe",
        quality: str = "green") -> TargetContext:
    return TargetContext(
        uia_data={
            "name": name,
            "automation_id": "a",
            "control_type": "ButtonControl",
        },
        process_name=process,
        confidence={"quality_level": quality, "capture_score": 0.95},
    )


def _semantic_mission(steps: list[CompiledStep]) -> Mission:
    m = Mission(id="m1", name="t", status=MissionStatus.EXECUTABLE)
    m.compiled_execution_graph = steps
    m.interpreted_steps = [
        InterpretedStep(description="s", raw_event_ids=[])
        for _ in steps
    ]
    return m


def _youtube_mission(profile: str = "Daniel Arturo Ramos") -> Mission:
    return _semantic_mission([
        CompiledStep(
            action_strategy=ActionStrategy.LAUNCH_APP,
            action_payload={"app_name": "Chrome"},
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
            target_context=_tc("Chrome"),
        ),
        CompiledStep(
            action_strategy=ActionStrategy.SELECT_PROFILE,
            action_payload={"profile": profile},
            validation_strategy=ValidationStrategy.REQUIRE_TEXT_MATCH,
            target_context=_tc(profile),
        ),
        CompiledStep(
            action_strategy=ActionStrategy.OPEN_NEW_TAB,
            action_payload={"app": "chrome"},
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
            target_context=_tc("Nueva pestaña"),
        ),
        CompiledStep(
            action_strategy=ActionStrategy.OPEN_BOOKMARK,
            action_payload={
                "name": "YouTube", "url": "https://www.youtube.com",
            },
            validation_strategy=ValidationStrategy.REQUIRE_TEXT_MATCH,
            target_context=_tc("YouTube"),
        ),
        CompiledStep(
            action_strategy=ActionStrategy.SUBMIT_SEARCH,
            action_payload={
                "text": "Luis Miguel",
                "site": "youtube",
                "target_label": "youtube_search_box",
                "submit_after": True,
            },
            validation_strategy=ValidationStrategy.REQUIRE_TEXT_MATCH,
            target_context=_tc("Buscar"),
        ),
    ])


class _SimState:
    """Estado sintético que evoluciona con los kinds del executor.

    Lo compartimos entre el patch del detector y el patch del runner
    para que las postcondiciones vayan cumpliéndose conforme el
    executor "ejecuta" cada step.
    """

    def __init__(self, *, fail_kind: str | None = None) -> None:
        self.running: set = set()
        self.url: str = ""
        self.title: str = "Escritorio"
        self.domain: str = ""
        self.fail_kind = fail_kind
        self.calls: list = []

    def snapshot(self) -> StateSnapshot:
        return StateSnapshot(
            active_app="chrome.exe" if "chrome.exe" in self.running else None,
            active_pid=1,
            active_window_title=self.title,
            active_window_class="Chrome_WidgetWin_1",
            active_window_bbox={"x": 0, "y": 0, "w": 1280, "h": 800},
            running_processes=sorted(self.running),
            browser_url=self.url or None,
            browser_title=self.title,
            browser_domain=self.domain or None,
            has_playwright_page=bool(self.url),
            screen_size=(1920, 1080),
            dpi_scale=1.0,
        )

    def advance_for(self, kind: str, params: Dict[str, Any]) -> None:
        if kind == "open_app":
            self.running.add("chrome.exe")
            self.title = "Nueva pestaña - Google Chrome"
        elif kind == "select_profile":
            self.url = "chrome://newtab/"
            self.title = (
                f"{params.get('profile_name', '')} - Nueva pestaña - Google Chrome"
            )
        elif kind == "open_new_tab":
            self.url = "chrome://newtab/"
            self.title = "Nueva pestaña - Google Chrome"
        elif kind == "open_url":
            self.url = "https://www.youtube.com/"
            self.title = "YouTube"
            self.domain = "youtube.com"
        elif kind == "open_site":
            # SIPE puede emitir ``open_site(site=youtube)`` en lugar de open_url.
            p = params or {}
            site = str(p.get("site") or "").strip().lower()
            url = str(p.get("url") or "").strip().lower()
            if site == "youtube" or "youtube" in url:
                self.url = "https://www.youtube.com/"
                self.title = "YouTube"
                self.domain = "youtube.com"
        elif kind in ("search_youtube", "search_content", "search_site"):
            p = params or {}
            q = str(p.get("query") or "").strip()
            site = str(p.get("site") or p.get("search_site") or "").strip().lower()
            prov = str(p.get("provider") or "").strip().lower()
            if kind == "search_youtube" or site == "youtube" or prov == "youtube":
                self.url = (
                    "https://www.youtube.com/results?search_query="
                    + q.replace(" ", "+")
                )
                self.title = f"{q} - YouTube"
                self.domain = "www.youtube.com"


@pytest.fixture
def happy_runner(monkeypatch):
    """Parchea detector/runner para un happy path completo."""
    state = _SimState()

    def _detect(self, deep: bool = False, want_url: bool = True):
        return state.snapshot()

    def _run_strategy(self, strategy, step, snap):
        state.calls.append((strategy, step.kind))
        state.advance_for(step.kind, step.params or {})
        return StrategyResult(ok=True, strategy=strategy, message="ok")

    monkeypatch.setattr(_sd.StateDetector, "detect", _detect, raising=True)
    monkeypatch.setattr(_ar.ActionRunner, "run_strategy", _run_strategy, raising=True)
    return state


# ─── decide_player ────────────────────────────────────────────────────


class TestDecidePlayer:
    def test_semantic_executable_uses_smart(self):
        m = _youtube_mission()
        assert decide_player(m) == "smart"

    def test_legacy_executable_uses_legacy(self):
        m = _semantic_mission([CompiledStep(
            action_strategy=ActionStrategy.CLICK,
            action_payload={},
            validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
            target_context=_tc(),
        )])
        # Sólo CLICK puro = misión legacy. Gate la aprueba → legacy.
        assert decide_player(m) == "legacy"

    def test_needs_review_mission_is_blocked(self):
        # Perfil vacío ⇒ approval_gate lo marca NEEDS_REVIEW.
        m = _semantic_mission([
            CompiledStep(
                action_strategy=ActionStrategy.SELECT_PROFILE,
                action_payload={"profile": ""},  # vacío → blocker
                validation_strategy=ValidationStrategy.REQUIRE_TEXT_MATCH,
                target_context=_tc("perfil"),
            ),
        ])
        assert decide_player(m) == "blocked"


# ─── run_mission_smart happy path ─────────────────────────────────────


class TestHappyPath:
    def test_smart_executor_is_used_end_to_end(self, happy_runner):
        msgs: List[str] = []
        m = _youtube_mission()
        decision: RunnerDecision = run_mission_smart(
            m,
            on_status=msgs.append,
            allow_legacy_fallback=False,
        )
        assert decision.player_used == "smart"
        assert not decision.blocked
        assert decision.result is not None
        assert decision.result.status == "success"
        # Todos los steps terminaron OK (success o skipped).
        assert all(
            o.status in ("success", "skipped")
            for o in decision.result.outcomes
        )
        # Mensajes humanos PRD §4: "Verificando Chrome", "Listo", etc.
        joined = " | ".join(msgs)
        assert "chrome" in joined.lower()
        assert "listo" in joined.lower()

    def test_to_dict_is_serializable(self, happy_runner):
        decision = run_mission_smart(
            _youtube_mission(),
            allow_legacy_fallback=False,
        )
        data = decision.to_dict()
        assert data["player_used"] == "smart"
        assert data["status_used"] == "executable"
        assert data["result_status"] == "success"

    def test_run_legacy_callback_is_not_called(self, happy_runner):
        sentinel: List[str] = []

        def _legacy(_m):
            sentinel.append("legacy_called")

        decision = run_mission_smart(
            _youtube_mission(),
            allow_legacy_fallback=True,
            run_legacy_player=_legacy,
        )
        assert decision.player_used == "smart"
        assert sentinel == []


# ─── Gate bloqueante ──────────────────────────────────────────────────


class TestGateBlocks:
    def test_empty_profile_blocks_execution(self, happy_runner):
        """PRD §3 + §14 — no ejecutar si hay empty_profile."""
        m = _semantic_mission([
            CompiledStep(
                action_strategy=ActionStrategy.SELECT_PROFILE,
                action_payload={"profile": ""},
                validation_strategy=ValidationStrategy.REQUIRE_TEXT_MATCH,
                target_context=_tc(),
            ),
        ])
        decision = run_mission_smart(
            m,
            allow_legacy_fallback=False,
        )
        assert decision.blocked
        assert decision.player_used == "none"
        # happy_runner debería NO haber sido llamado.
        assert happy_runner.calls == []
        codes = [b.code for b in decision.blockers]
        assert "empty_profile" in codes

    def test_needs_review_status_is_enforced(self, happy_runner):
        m = _semantic_mission([
            CompiledStep(
                action_strategy=ActionStrategy.SUBMIT_SEARCH,
                action_payload={
                    "site": "youtube",
                    "submit_after": True,
                    # ojo: target_label vacío + site → blocker
                },
                validation_strategy=ValidationStrategy.REQUIRE_TEXT_MATCH,
                target_context=_tc(),
            ),
        ])
        decision = run_mission_smart(m, allow_legacy_fallback=False)
        assert decision.blocked


# ─── Fallback legacy ─────────────────────────────────────────────────


class TestLegacyFallback:
    def test_legacy_is_used_when_semantic_absent(self, happy_runner):
        m = _semantic_mission([
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                action_payload={},
                validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
                target_context=_tc(),
            ),
        ])
        called: List[bool] = []

        def _legacy(_m):
            called.append(True)
            return "ok"

        msgs: List[str] = []
        decision = run_mission_smart(
            m,
            on_status=msgs.append,
            allow_legacy_fallback=True,
            run_legacy_player=_legacy,
        )
        assert decision.player_used == "legacy"
        assert called == [True]
        # PRD §1: advertencia humana sobre legacy debe aparecer.
        assert any("antiguo" in m.lower() for m in msgs)

    def test_legacy_disabled_blocks_non_semantic(self, happy_runner):
        m = _semantic_mission([
            CompiledStep(
                action_strategy=ActionStrategy.CLICK,
                action_payload={},
                validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
                target_context=_tc(),
            ),
        ])
        decision = run_mission_smart(m, allow_legacy_fallback=False)
        assert decision.blocked
        assert decision.player_used == "none"
        codes = [b.code for b in decision.blockers]
        assert "legacy_only" in codes


# ─── No avanzar a ciegas (PRD §9) ─────────────────────────────────────


class TestNoBlindAdvance:
    def test_failed_precondition_stops_execution(self, monkeypatch):
        """PRD §9 — si la precondición (Chrome corriendo) no se puede
        cumplir, NINGÚN step posterior debe ejecutarse."""
        state = _SimState()
        # Chrome nunca se arranca por más que la estrategia diga ok.
        def _detect(self, deep: bool = False, want_url: bool = True):
            return state.snapshot()

        def _run_strategy(self, strategy, step, snap):
            state.calls.append((strategy, step.kind))
            # Nunca avanzamos: open_app no corre, y el resto falla por
            # precondition (Chrome running).
            return StrategyResult(
                ok=False,
                strategy=strategy,
                message="stub: no avanzar nunca",
            )

        monkeypatch.setattr(_sd.StateDetector, "detect", _detect)
        monkeypatch.setattr(_ar.ActionRunner, "run_strategy", _run_strategy)

        decision = run_mission_smart(
            _youtube_mission(),
            allow_legacy_fallback=False,
        )
        assert decision.result is not None
        assert decision.result.status in ("stopped_for_human", "failed")
        kinds_ejecutados = [o.kind for o in decision.result.outcomes]
        # Como la misión falla en el primer paso (open_app), los
        # siguientes pasos de navegación/búsqueda NO deben aparecer.
        assert not any(
            k in kinds_ejecutados
            for k in ("search_youtube", "search_content", "search_site", "open_url")
        )

    def test_failed_search_postcondition_stops_next(self, monkeypatch):
        """Si search_youtube nunca cumple su postcondition, el executor
        se detiene tras agotar reintentos (no hay 'siguiente' que
        ejecutar, pero sí validamos que no es 'success')."""
        state = _SimState()

        def _detect(self, deep: bool = False, want_url: bool = True):
            return state.snapshot()

        def _run_strategy(self, strategy, step, snap):
            state.calls.append((strategy, step.kind))
            # Todos los pasos avanzan, EXCEPTO búsqueda YouTube canónica
            # (no actualiza URL → la postcondition nunca se cumple).
            is_yt_search = step.kind in (
                "search_youtube",
                "search_content",
                "search_site",
            )
            if not is_yt_search:
                state.advance_for(step.kind, step.params or {})
            return StrategyResult(ok=True, strategy=strategy, message="ok")

        monkeypatch.setattr(_sd.StateDetector, "detect", _detect)
        monkeypatch.setattr(_ar.ActionRunner, "run_strategy", _run_strategy)

        decision = run_mission_smart(
            _youtube_mission(),
            allow_legacy_fallback=False,
        )
        assert decision.result is not None
        assert decision.result.status in ("stopped_for_human", "failed")
        # Y NO puede terminar con status="success".
        assert decision.result.status != "success"
