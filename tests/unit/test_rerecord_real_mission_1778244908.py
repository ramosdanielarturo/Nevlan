"""
Nevlan — Tests E2E de regrabación con la grabación REAL ``mission_1778244908``
================================================================================

PRD 2026-05-08c §J. Cubre:

  * Replay previo ejecuta exactamente los pasos correctos (open_app /
    select_profile / open_new_tab / open_url) antes del paso a regrabar.
  * Reemplazo del paso ``search_youtube`` no muta los pasos previos
    (open_app, select_profile, open_new_tab, open_url) ni el posterior
    ``scroll_results``.
  * Reemplazo del paso ``scroll_results`` no muta los pasos previos.
  * Cancelar deja la misión idéntica a la fixture confirmada.
  * Save recompute correctamente el ``intent_status``.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

import pytest

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.mission_review_confirm import (
    confirm_profile, confirm_query,
)
from app.services.missions.rerecord_session import (
    RerecordOrchestrator,
    STATE_SAVED,
    STATE_WAITING_FOR_USER,
)
from app.services.missions import profile_resolver

FIXTURES_DIR = Path("tests/fixtures/real_missions")
RAW_FIXTURE = FIXTURES_DIR / "mission_1778244908_raw.json"


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


def _sep_step_executor_kind(st: dict) -> str:
    """``MissionStep.kind`` iguala el tipo SEP (UIC persistido)."""
    return str(st.get("type") or "")


def _index_of_type(mission: Mission, target_type: str) -> int:
    plan = mission.semantic_execution_plan or {}
    for i, st in enumerate(plan.get("steps") or []):
        if st.get("type") == target_type:
            return i
    raise AssertionError(
        f"No encontré ningún step de tipo {target_type!r} en el plan; "
        f"tipos: {[s.get('type') for s in (plan.get('steps') or [])]}"
    )


def _evidence_first_n(mission: Mission, n: int = 6) -> List[dict]:
    return [ev.model_dump(mode="json") for ev in (mission.raw_trace or [])[:n]]


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


# ─────────────────────────────────────────────────────────────────────
# §J — Tests E2E con la misión real
# ─────────────────────────────────────────────────────────────────────


class TestRerecordRealMission:

    def test_search_step_replays_previous_steps(self, confirmed_mission):
        """Replay previo de los pasos open_app/select_profile/open_new_tab/
        open_url antes del paso ``search_youtube``."""
        target_idx = _index_of_type(confirmed_mission, "search_content")
        stub = _make_stub_executor()
        orch = RerecordOrchestrator(executor_factory=lambda: stub)
        orch.start(confirmed_mission, target_step_index=target_idx)
        ok = orch.proceed_to_capture()
        assert ok is True
        assert orch.session.state == STATE_WAITING_FOR_USER
        # El replay debió ejecutar EXACTAMENTE los pasos previos del
        # plan, en orden, sin tocar el target ni los posteriores.
        executed_kinds = [s.kind for s in stub.calls[0]]
        plan_exec_kinds = [
            _sep_step_executor_kind(st)
            for st in confirmed_mission.semantic_execution_plan["steps"]
        ]
        assert executed_kinds == plan_exec_kinds[:target_idx]
        # Nunca ejecuta el target.
        assert plan_exec_kinds[target_idx] not in executed_kinds, (
            "El executor incluyó el step a regrabar — debe detenerse antes."
        )

    def test_search_step_replaces_only_search(self, confirmed_mission):
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        target_idx = _index_of_type(confirmed_mission, "search_content")
        orch = RerecordOrchestrator(executor_factory=lambda: _make_stub_executor())
        orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        ev = _evidence_first_n(confirmed_mission, 6)
        orch.capture_user_event(ev)
        orch.save()
        plan_after = confirmed_mission.semantic_execution_plan
        # Cardinalidad preservada.
        assert len(plan_after["steps"]) == len(plan_before["steps"])
        # Pasos previos: idénticos.
        for i in range(target_idx):
            assert plan_after["steps"][i] == plan_before["steps"][i], (
                f"paso previo {i} fue mutado durante regrabación de search"
            )
        # Pasos posteriores: idénticos.
        for i in range(target_idx + 1, len(plan_after["steps"])):
            assert plan_after["steps"][i] == plan_before["steps"][i], (
                f"paso posterior {i} fue mutado durante regrabación de search"
            )
        # El target sí cambió: lleva audit del rerecord.
        assert (
            plan_after["steps"][target_idx]["params"].get("_user_action")
            == "rerecord"
        )

    def test_scroll_step_replaces_only_scroll(self, confirmed_mission):
        # Sólo aplicamos si el plan tiene un paso scroll_results.
        plan = confirmed_mission.semantic_execution_plan or {}
        types_in_plan = [s.get("type") for s in plan.get("steps") or []]
        if "scroll_results" not in types_in_plan:
            pytest.skip("plan no contiene scroll_results")
        plan_before = copy.deepcopy(plan)
        target_idx = types_in_plan.index("scroll_results")
        orch = RerecordOrchestrator(executor_factory=lambda: _make_stub_executor())
        orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        ev = _evidence_first_n(confirmed_mission, 6)
        orch.capture_user_event(ev)
        orch.save()
        plan_after = confirmed_mission.semantic_execution_plan
        # Pasos previos: idénticos.
        for i in range(target_idx):
            assert plan_after["steps"][i] == plan_before["steps"][i]

    def test_cancel_keeps_fixture_identical(self, confirmed_mission):
        before = confirmed_mission.model_dump(mode="json")
        target_idx = _index_of_type(confirmed_mission, "search_content")
        orch = RerecordOrchestrator(executor_factory=lambda: _make_stub_executor())
        orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        ev = _evidence_first_n(confirmed_mission, 6)
        orch.capture_user_event(ev)
        orch.cancel()
        after = confirmed_mission.model_dump(mode="json")
        # Plan idéntico, audit vacío, sin tags nuevos.
        assert before["semantic_execution_plan"] == after["semantic_execution_plan"]
        assert before["review_confirmations"] == after["review_confirmations"]
        assert before.get("rerecord_history", []) == after.get("rerecord_history", [])
        assert before["tags"] == after["tags"]

    def test_save_recomputes_ready_status(self, confirmed_mission):
        target_idx = _index_of_type(confirmed_mission, "search_content")
        before_status = confirmed_mission.semantic_execution_plan["intent_status"]
        # Estado de partida es READY (plan confirmado).
        assert before_status == "READY"
        orch = RerecordOrchestrator(executor_factory=lambda: _make_stub_executor())
        orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        ev = _evidence_first_n(confirmed_mission, 6)
        orch.capture_user_event(ev)
        plan = orch.save()
        # Tras save el status fue recomputado.
        sep = confirmed_mission.semantic_execution_plan
        assert sep["intent_status"] == plan.intent_status
        assert isinstance(sep["not_ready_reasons"], list)
        # Plan sigue sin LEGACY_RUNTIME_CONTAMINATION (regla §A).
        assert "LEGACY_RUNTIME_CONTAMINATION" not in sep["not_ready_reasons"]

    def test_save_persists_audit_trail_full(self, confirmed_mission):
        target_idx = _index_of_type(confirmed_mission, "search_content")
        target_id = confirmed_mission.semantic_execution_plan["steps"][target_idx]["id"]
        orch = RerecordOrchestrator(executor_factory=lambda: _make_stub_executor())
        session = orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        ev = _evidence_first_n(confirmed_mission, 6)
        orch.capture_user_event(ev)
        orch.save()
        # rerecord_history.
        assert confirmed_mission.rerecord_history
        entry = confirmed_mission.rerecord_history[-1]
        assert entry.target_step_id == target_id
        assert entry.target_step_index == target_idx
        assert entry.status == "saved"
        assert entry.replacement_evidence
        assert entry.previous_steps_replayed
        # Tag review_confirmed:rerecord:* aplicado.
        assert any(
            str(t).startswith("review_confirmed:rerecord:")
            for t in confirmed_mission.tags
        )

    def test_save_does_not_touch_raw_trace(self, confirmed_mission):
        """Regla §3 del contrato: raw_trace SIEMPRE se conserva intacto."""
        raw_before = [ev.model_dump(mode="json") for ev in confirmed_mission.raw_trace]
        target_idx = _index_of_type(confirmed_mission, "search_content")
        orch = RerecordOrchestrator(executor_factory=lambda: _make_stub_executor())
        orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        ev = _evidence_first_n(confirmed_mission, 6)
        orch.capture_user_event(ev)
        orch.save()
        raw_after = [ev.model_dump(mode="json") for ev in confirmed_mission.raw_trace]
        assert raw_before == raw_after, (
            "rerecord_step modificó raw_trace; viola la regla §3 del PRD."
        )
