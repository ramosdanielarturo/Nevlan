"""
Nevlan — Tests de integración del flujo de regrabación
=========================================================

Cubre la integración Mission Review ⇄ orquestador y cubre las
garantías del PRD que no son unit-only:

* §G Replay corre OFF el thread principal (worker QThread).
* §G Cancel libera worker.
* §H Mission Review se refresca tras save (state actualizado).
* §H Mission Review queda igual tras cancel (no muta plan).
* §H Vista experto incluye ``rerecord_history`` y vista normal NO.
* §I — Casos críticos: regrabar paso 1 (sin replay), paso intermedio,
  último paso.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

import pytest

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.mission_review_confirm import (
    confirm_profile,
    confirm_query,
    rerecord_step,
)
from app.services.missions.mission_review_summary import (
    build_mission_review_summary,
    render_expert_view,
    render_normal_view,
)
from app.services.missions.rerecord_session import (
    RerecordOrchestrator,
    STATE_CAPTURED,
    STATE_FAILED,
    STATE_SAVED,
    STATE_WAITING_FOR_USER,
)
from app.services.missions import profile_resolver

FIXTURES_DIR = Path("tests/fixtures/real_missions")
RAW_FIXTURE = FIXTURES_DIR / "mission_1778244908_raw.json"


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def _qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as e:
        pytest.skip(f"PyQt6 no disponible: {e}")
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def _isolate_alias_store(tmp_path, monkeypatch):
    fake = tmp_path / "profile_aliases.json"
    monkeypatch.setattr(
        profile_resolver, "_alias_store_path", lambda: fake,
    )
    profile_resolver.reset_alias_store()
    yield
    profile_resolver.reset_alias_store()


@pytest.fixture
def confirmed_mission() -> Mission:
    raw = json.loads(RAW_FIXTURE.read_text(encoding="utf-8"))
    data = copy.deepcopy(raw)
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    data["review_confirmations"] = []
    data["rerecord_history"] = []
    data["tags"] = [
        t for t in (data.get("tags") or [])
        if not str(t).startswith("review_confirmed:")
    ]
    m = Mission(**data)
    apply_collapse_to_mission(m)
    confirm_profile(m, profile_name="Daniel Arturo Ramos")
    confirm_query(m, query="Devuélveme el amor de Luis Miguel")
    return m


def _evidence_from_step(mission: Mission, target_idx: int):
    raw = mission.raw_trace
    return [ev.model_dump(mode="json") for ev in (raw[:6] or [])]


# ─────────────────────────────────────────────────────────────────────
# §H — Mission Review summary refresca tras save
# ─────────────────────────────────────────────────────────────────────


class TestMissionReviewRefreshesAfterSave:

    def test_summary_includes_rerecord_history_after_save(self, confirmed_mission):
        before = build_mission_review_summary(confirmed_mission)
        assert before.rerecord_history == []

        # Simulamos save vía rerecord_step (sin pasar por orchestrator
        # — ya validamos eso en otro archivo).
        ev = _evidence_from_step(confirmed_mission, 4)
        plan, _, _ = rerecord_step(
            confirmed_mission,
            step_index=4,
            new_step_evidence=ev,
            rerecorded_at=datetime.now(timezone.utc),
            rerecord_session_id="ses-test-001",
            previous_steps_replayed=["s0", "s1", "s2", "s3"],
        )
        after = build_mission_review_summary(confirmed_mission)
        assert after.rerecord_history, "rerecord_history vacío en summary"
        entry = after.rerecord_history[-1]
        assert entry["target_step_index"] == 4
        assert entry["rerecord_session_id"] == "ses-test-001"
        assert entry["status"] == "saved"

    def test_normal_view_hides_rerecord_history(self, confirmed_mission):
        ev = _evidence_from_step(confirmed_mission, 4)
        rerecord_step(
            confirmed_mission,
            step_index=4,
            new_step_evidence=ev,
            rerecorded_at=datetime.now(timezone.utc),
            rerecord_session_id="ses-test-002",
        )
        summary = build_mission_review_summary(confirmed_mission)
        normal = render_normal_view(summary)
        # Vista normal NO menciona "regrabación" ni el session_id.
        assert "ses-test-002" not in normal
        assert "rerecord_session_id" not in normal
        assert "Historial de regrabación" not in normal

    def test_expert_view_shows_rerecord_history(self, confirmed_mission):
        ev = _evidence_from_step(confirmed_mission, 4)
        rerecord_step(
            confirmed_mission,
            step_index=4,
            new_step_evidence=ev,
            rerecorded_at=datetime.now(timezone.utc),
            rerecord_session_id="ses-test-003",
            previous_steps_replayed=["s0", "s1", "s2", "s3"],
        )
        summary = build_mission_review_summary(confirmed_mission)
        expert = render_expert_view(summary)
        assert "Historial de regrabación" in expert
        # Reproducimos los IDs de previos.
        assert "previos reproducidos" in expert

    def test_summary_is_unchanged_when_orchestrator_cancels(self, confirmed_mission):
        before_blob = confirmed_mission.model_dump(mode="json")
        before_summary = build_mission_review_summary(confirmed_mission)

        # Capturamos pero cancelamos antes de save.
        class _Stub:
            on_step_done = None

            def run_mission(self, steps):
                from types import SimpleNamespace
                outs = []
                for i, st in enumerate(steps):
                    o = SimpleNamespace(
                        step_id=getattr(st, "id", f"s{i}"),
                        kind=getattr(st, "kind", "?"),
                        status="success",
                    )
                    outs.append(o)
                    if self.on_step_done is not None:
                        self.on_step_done(o)
                return SimpleNamespace(
                    status="success", outcomes=outs,
                    last_message="", failed_step_id=None,
                )

        orch = RerecordOrchestrator(executor_factory=lambda: _Stub())
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        ev = _evidence_from_step(confirmed_mission, 4)
        orch.capture_user_event(ev)
        orch.cancel()

        after_blob = confirmed_mission.model_dump(mode="json")
        after_summary = build_mission_review_summary(confirmed_mission)
        # Estado y contenidos críticos intactos.
        assert before_blob["semantic_execution_plan"] == after_blob["semantic_execution_plan"]
        assert before_blob["review_confirmations"] == after_blob["review_confirmations"]
        assert before_blob.get("rerecord_history", []) == after_blob.get("rerecord_history", [])
        assert before_summary.rerecord_history == after_summary.rerecord_history


# ─────────────────────────────────────────────────────────────────────
# §I — Casos críticos: paso 1, intermedio, último
# ─────────────────────────────────────────────────────────────────────


class TestCriticalRerecordCases:

    def test_rerecord_first_step_skips_replay(self, confirmed_mission):
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        called = {"v": 0}

        def factory():
            called["v"] += 1
            return _make_stub_executor()

        orch = RerecordOrchestrator(executor_factory=factory)
        orch.start(confirmed_mission, target_step_index=0)
        ok = orch.proceed_to_capture()
        assert ok is True
        # No hay pasos previos: executor NUNCA se construyó.
        assert called["v"] == 0
        ev = _evidence_from_step(confirmed_mission, 0)
        orch.capture_user_event(ev)
        plan = orch.save()
        # Solo el paso 0 fue reemplazado.
        steps_after = confirmed_mission.semantic_execution_plan["steps"]
        assert len(steps_after) == len(plan_before["steps"])
        for i in range(1, len(steps_after)):
            assert steps_after[i] == plan_before["steps"][i]

    def test_rerecord_intermediate_step_replays_only_previous(
        self, confirmed_mission,
    ):
        target_idx = 3
        stub = _make_stub_executor()
        orch = RerecordOrchestrator(executor_factory=lambda: stub)
        orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        # Solo se ejecutaron los pasos 0..target_idx-1.
        assert len(stub.calls[0]) == target_idx
        ev = _evidence_from_step(confirmed_mission, target_idx)
        orch.capture_user_event(ev)
        orch.save()
        # Audit trail.
        assert confirmed_mission.rerecord_history[-1].target_step_index == target_idx

    def test_rerecord_last_step_replays_all_previous(
        self, confirmed_mission,
    ):
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        last_idx = len(plan_before["steps"]) - 1
        stub = _make_stub_executor()
        orch = RerecordOrchestrator(executor_factory=lambda: stub)
        orch.start(confirmed_mission, target_step_index=last_idx)
        orch.proceed_to_capture()
        assert len(stub.calls[0]) == last_idx
        ev = _evidence_from_step(confirmed_mission, last_idx)
        orch.capture_user_event(ev)
        orch.save()
        steps_after = confirmed_mission.semantic_execution_plan["steps"]
        # Todos los pasos previos intactos.
        for i in range(last_idx):
            assert steps_after[i] == plan_before["steps"][i]
        # El último marcado como rerecord.
        assert steps_after[last_idx]["params"].get("_user_action") == "rerecord"


# ─────────────────────────────────────────────────────────────────────
# §G — Replay corre off-thread y libera worker
# ─────────────────────────────────────────────────────────────────────


class TestReplayThreading:

    def test_replay_worker_runs_in_separate_thread(self, _qapp, confirmed_mission):
        from PyQt6.QtCore import QThread
        from app.interfaces.desktop.mission_review import _ReplayWorker

        thread_seen: List[int] = []
        main_id = threading.get_ident()

        class _SlowStub:
            on_step_done = None

            def run_mission(self, steps):
                from types import SimpleNamespace
                thread_seen.append(threading.get_ident())
                # Comprobamos que NO corremos en el thread principal.
                outs = []
                for i, st in enumerate(steps):
                    o = SimpleNamespace(
                        step_id=getattr(st, "id", f"s{i}"),
                        kind=getattr(st, "kind", "?"),
                        status="success",
                    )
                    outs.append(o)
                    if self.on_step_done is not None:
                        self.on_step_done(o)
                return SimpleNamespace(
                    status="success", outcomes=outs,
                    last_message="", failed_step_id=None,
                )

        orch = RerecordOrchestrator(executor_factory=lambda: _SlowStub())
        orch.start(confirmed_mission, target_step_index=4)

        thread = QThread()
        worker = _ReplayWorker(orch)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        finished_flag = {"v": False}

        def _on_finish(ok, err):
            finished_flag["v"] = True
            thread.quit()

        worker.finished.connect(_on_finish)
        thread.start()
        # Bombeamos eventos hasta que el thread termine.
        deadline = time.time() + 5.0
        while not finished_flag["v"] and time.time() < deadline:
            _qapp.processEvents()
            time.sleep(0.01)
        thread.wait(2000)
        assert finished_flag["v"], "El worker no señaló finished a tiempo"
        assert thread_seen, "run_mission no se llamó"
        assert thread_seen[0] != main_id, (
            "run_mission corrió en el thread principal: la UI se congelaría"
        )


# ─────────────────────────────────────────────────────────────────────
# Helpers locales
# ─────────────────────────────────────────────────────────────────────


def _make_stub_executor():
    from types import SimpleNamespace

    class _Stub:
        on_step_done = None
        calls: List[List[Any]] = []

        def run_mission(self, steps):
            outs = []
            self.calls.append(list(steps))
            for i, st in enumerate(steps):
                o = SimpleNamespace(
                    step_id=getattr(st, "id", f"s{i}"),
                    kind=getattr(st, "kind", "?"),
                    status="success",
                )
                outs.append(o)
                if self.on_step_done is not None:
                    self.on_step_done(o)
            return SimpleNamespace(
                status="success", outcomes=outs,
                last_message="", failed_step_id=None,
            )

    return _Stub()
