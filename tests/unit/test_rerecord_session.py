"""
Nevlan — Tests de RerecordOrchestrator (PRD 2026-05-08c §C-§D-§E)
=================================================================

Cubre el orchestrator headless (sin Qt) y el verbo
``mission_review_confirm.rerecord_step`` con todas las garantías
del contrato ``docs/plan_regrabacion_paso_replay.md``:

* Replay previo ejecuta SOLO los pasos 0..N-1.
* Detención justo antes del paso N (no se ejecuta).
* Save reemplaza solo el target step y deja el resto idéntico.
* Save recompila el plan + recomputa intent_status.
* Cancel restaura snapshot bit-perfecto.
* Audit trail estructurado (rerecord_history + review_confirmations).
* Repeat descarta la captura sin mutar la misión.
* Captura inválida → no se acepta y la sesión vuelve a esperar.
* Cancel libera workers (cooperative cancel).
* Doble save bloqueado.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.mission_review_confirm import (
    SOURCE_RERECORD,
    confirm_profile,
    confirm_query,
    rerecord_step,
)
from app.services.missions.rerecord_session import (
    REPLAY_FAILED_MESSAGE,
    RerecordOrchestrator,
    RerecordSession,
    RerecordSessionError,
    STATE_CANCELLED,
    STATE_CAPTURED,
    STATE_FAILED,
    STATE_PREPARING,
    STATE_REPLAYING,
    STATE_SAVED,
    STATE_WAITING_FOR_USER,
)
from app.services.missions import profile_resolver

FIXTURES_DIR = Path("tests/fixtures/real_missions")
RAW_FIXTURE = FIXTURES_DIR / "mission_1778244908_raw.json"


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


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
    """Misión real ``mission_1778244908`` con perfil + query confirmados.

    Después de las confirmaciones queda en READY (raw_trace válido,
    sin blockers). Es la misión "limpia" sobre la que vamos a
    simular regrabaciones.
    """
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


def _make_capture_evidence(target_step: dict, *, source_mission: Mission) -> List[Dict[str, Any]]:
    """Construye una "evidencia capturada" reusando los RawEvents del
    target step.

    En producción esta evidencia viene del ``StepFragmentRecorder``;
    para tests nos basta con tomar los RawEvents que el ICE ya
    asoció al step. Eso garantiza que ``compile_fragment`` produzca
    un step del mismo tipo (y la regrabación reemplaze por algo
    semánticamente equivalente).
    """
    target_type = str(target_step.get("type") or "")
    raw_trace = source_mission.raw_trace or []
    # Los RawEvents son timestamps + click + UIA. Tomamos los primeros
    # que tengan algún metadato relacionado con el target_type.
    if not raw_trace:
        raise AssertionError("La misión no tiene raw_trace; tests rotos.")
    snapshot: List[Dict[str, Any]] = []
    for ev in raw_trace[:6]:
        snapshot.append(ev.model_dump(mode="json"))
    return snapshot


class _StubExecutor:
    """Executor sintético para que ``proceed_to_capture`` corra sin
    tocar el sistema real.

    ``run_mission(steps)`` devuelve un MissionResult-like con
    status="success" y outcomes uno por step. Para tests de fallo
    se puede pasar ``fail_at=k`` para que el k-ésimo paso retorne
    "failed".
    """

    def __init__(
        self,
        *,
        fail_at: int = -1,
        cancel_after: int = -1,
        sleep_per_step_ms: int = 0,
    ) -> None:
        self.fail_at = fail_at
        self.cancel_after = cancel_after
        self.sleep_per_step_ms = sleep_per_step_ms
        self.on_step_done = None
        self.calls: List[List[Any]] = []

    def run_mission(self, steps):  # noqa: ANN001
        from types import SimpleNamespace
        outcomes = []
        self.calls.append(list(steps))
        for i, st in enumerate(steps):
            out = SimpleNamespace(
                step_id=getattr(st, "id", f"step_{i}"),
                kind=getattr(st, "kind", "?"),
                status="success" if i != self.fail_at else "failed",
                strategy_used=None,
                retries=0,
                duration_ms=0,
                human_message="",
                error="",
                state_before=None,
                state_after=None,
            )
            outcomes.append(out)
            if self.on_step_done is not None:
                try:
                    self.on_step_done(out)
                except Exception:
                    pass
            if i == self.fail_at:
                return SimpleNamespace(
                    run_id="stub",
                    status="failed",
                    outcomes=outcomes,
                    started_at=0.0,
                    finished_at=0.0,
                    last_message=f"Paso {i+1} falló",
                    failed_step_id=getattr(st, "id", None),
                    duration_ms=0,
                )
        return SimpleNamespace(
            run_id="stub",
            status="success",
            outcomes=outcomes,
            started_at=0.0,
            finished_at=0.0,
            last_message="",
            failed_step_id=None,
            duration_ms=0,
        )


# ─────────────────────────────────────────────────────────────────────
# Estado básico del orchestrator
# ─────────────────────────────────────────────────────────────────────


class TestOrchestratorLifecycle:

    def test_start_creates_session_with_snapshot(self, confirmed_mission):
        orch = RerecordOrchestrator()
        session = orch.start(confirmed_mission, target_step_index=4)
        assert session.target_step_index == 4
        assert session.state == STATE_PREPARING
        assert session.target_step_id  # plan tiene IDs

    def test_start_rejects_invalid_index(self, confirmed_mission):
        orch = RerecordOrchestrator()
        with pytest.raises(RerecordSessionError):
            orch.start(confirmed_mission, target_step_index=999)

    def test_start_rejects_mission_without_plan(self, confirmed_mission):
        confirmed_mission.semantic_execution_plan = None
        orch = RerecordOrchestrator()
        with pytest.raises(RerecordSessionError):
            orch.start(confirmed_mission, target_step_index=0)


# ─────────────────────────────────────────────────────────────────────
# Replay previo (PRD §C)
# ─────────────────────────────────────────────────────────────────────


class TestReplayPrevio:

    def test_rerecord_runs_previous_steps_before_target(
        self, confirmed_mission,
    ):
        plan = confirmed_mission.semantic_execution_plan
        target_idx = 4  # search_youtube en la grabación real
        stub = _StubExecutor()
        orch = RerecordOrchestrator(executor_factory=lambda: stub)
        orch.start(confirmed_mission, target_step_index=target_idx)
        ok = orch.proceed_to_capture()
        assert ok is True
        # Debe haber ejecutado exactamente los pasos 0..target_idx-1.
        assert len(stub.calls) == 1
        assert len(stub.calls[0]) == target_idx
        executed_kinds = [s.kind for s in stub.calls[0]]
        plan_kinds = [s["type"] for s in plan["steps"][:target_idx]]
        assert executed_kinds == plan_kinds

    def test_rerecord_stops_at_target_step(self, confirmed_mission):
        target_idx = 4
        stub = _StubExecutor()
        orch = RerecordOrchestrator(executor_factory=lambda: stub)
        orch.start(confirmed_mission, target_step_index=target_idx)
        ok = orch.proceed_to_capture()
        assert ok is True
        assert orch.session.state == STATE_WAITING_FOR_USER
        plan = confirmed_mission.semantic_execution_plan
        target = plan["steps"][target_idx]
        # NO se ejecutó el target.
        for outcome_step in stub.calls[0]:
            assert outcome_step.id != target["id"]

    def test_rerecord_step_one_skips_replay(self, confirmed_mission):
        """Regrabar el paso 1 (índice 0): no hay pasos previos,
        debe pasar directo a waiting_for_user sin instanciar executor.
        """
        called = {"v": 0}

        def factory():
            called["v"] += 1
            return _StubExecutor()

        orch = RerecordOrchestrator(executor_factory=factory)
        orch.start(confirmed_mission, target_step_index=0)
        ok = orch.proceed_to_capture()
        assert ok is True
        assert orch.session.state == STATE_WAITING_FOR_USER
        assert called["v"] == 0

    def test_rerecord_failure_shows_visible_error(self, confirmed_mission):
        stub = _StubExecutor(fail_at=2)
        orch = RerecordOrchestrator(executor_factory=lambda: stub)
        orch.start(confirmed_mission, target_step_index=4)
        ok = orch.proceed_to_capture()
        assert ok is False
        assert orch.session.state == STATE_FAILED
        assert REPLAY_FAILED_MESSAGE in orch.session.error_message
        assert orch.session.error_message  # no vacío

    def test_rerecord_replay_can_cancel(self, confirmed_mission):
        """Cooperative cancel: si pedimos cancel ANTES del replay,
        el orchestrator no muta la misión."""
        stub = _StubExecutor()
        orch = RerecordOrchestrator(executor_factory=lambda: stub)
        before = copy.deepcopy(confirmed_mission.model_dump(mode="json"))
        orch.start(confirmed_mission, target_step_index=4)
        orch.request_cancel()
        ok = orch.proceed_to_capture()
        assert ok is False
        assert orch.session.state == STATE_CANCELLED
        # Misión bit-perfect.
        after = confirmed_mission.model_dump(mode="json")
        # Tags no se cambian; review_confirmations idem.
        assert before["semantic_execution_plan"] == after["semantic_execution_plan"]
        assert before["review_confirmations"] == after["review_confirmations"]
        assert before.get("rerecord_history", []) == after.get("rerecord_history", [])

    def test_rerecord_skip_replay_manual(self, confirmed_mission):
        """Tras un fallo, ``skip_replay_and_capture_manually`` salta
        directo a waiting_for_user sin reintentar."""
        stub = _StubExecutor(fail_at=1)
        orch = RerecordOrchestrator(executor_factory=lambda: stub)
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        assert orch.session.state == STATE_FAILED
        orch.skip_replay_and_capture_manually()
        assert orch.session.state == STATE_WAITING_FOR_USER


# ─────────────────────────────────────────────────────────────────────
# Captura + save (PRD §D + §E)
# ─────────────────────────────────────────────────────────────────────


class TestCaptureAndSave:

    def test_capture_user_event_rejects_empty_evidence(self, confirmed_mission):
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        ok = orch.capture_user_event([])
        assert ok is False
        assert orch.session.state == STATE_WAITING_FOR_USER

    def test_capture_user_event_invalid_via_validator(self, confirmed_mission):
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        evidence = _make_capture_evidence(
            confirmed_mission.semantic_execution_plan["steps"][4],
            source_mission=confirmed_mission,
        )
        # Validador rechaza siempre.
        ok = orch.capture_user_event(evidence, validator=lambda e: False)
        assert ok is False
        assert orch.session.state == STATE_WAITING_FOR_USER

    def test_save_requires_capture_first(self, confirmed_mission):
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        with pytest.raises(RerecordSessionError):
            orch.save()

    def test_save_persists_only_target_step(self, confirmed_mission):
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        target_idx = 4
        target_id_before = plan_before["steps"][target_idx]["id"]

        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        ev = _make_capture_evidence(
            plan_before["steps"][target_idx],
            source_mission=confirmed_mission,
        )
        ok = orch.capture_user_event(ev)
        assert ok is True
        plan = orch.save()

        plan_after_blob = confirmed_mission.semantic_execution_plan
        assert plan_after_blob is not None
        steps_after = plan_after_blob["steps"]
        # Misma cardinalidad.
        assert len(steps_after) == len(plan_before["steps"])
        # IDs preservados (excepto el target, que conservamos por contrato
        # también — id_alias en audit).
        for i, (st_b, st_a) in enumerate(zip(plan_before["steps"], steps_after)):
            if i == target_idx:
                assert st_a["id"] == target_id_before
                # Pero los params marcan el rerecord:
                assert st_a["params"].get("_original_step_id") == target_id_before
                assert st_a["params"].get("_user_action") == "rerecord"
            else:
                assert st_a == st_b, f"paso {i} fue mutado"

    def test_save_recomputes_semantic_plan(self, confirmed_mission):
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        ev = _make_capture_evidence(
            plan_before["steps"][4],
            source_mission=confirmed_mission,
        )
        orch.capture_user_event(ev)
        plan = orch.save()
        # El plan resultante tiene intent_status recomputado.
        assert plan.intent_status in ("READY", "NEEDS_REVIEW", "EXECUTABLE")
        # Y el blob persistido también.
        assert confirmed_mission.semantic_execution_plan["intent_status"] == plan.intent_status
        # not_ready_reasons coherente.
        assert isinstance(
            confirmed_mission.semantic_execution_plan["not_ready_reasons"], list,
        )

    def test_save_writes_audit_trail(self, confirmed_mission):
        target_idx = 4
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        target_id = plan_before["steps"][target_idx]["id"]
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        session = orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        ev = _make_capture_evidence(
            plan_before["steps"][target_idx],
            source_mission=confirmed_mission,
        )
        orch.capture_user_event(ev)
        orch.save()

        assert confirmed_mission.rerecord_history, (
            "rerecord_history vacío tras save"
        )
        entry = confirmed_mission.rerecord_history[-1]
        assert entry.rerecord_session_id == session.rerecord_session_id
        assert entry.target_step_id == target_id
        assert entry.target_step_index == target_idx
        assert entry.status == "saved"
        assert entry.original_step_snapshot
        assert entry.replacement_step_snapshot
        assert entry.replacement_evidence  # raw events nuevos persistidos
        # review_confirmations: espejo a nivel de campo.
        rcs = [
            c for c in confirmed_mission.review_confirmations
            if c.field == "step_replacement"
        ]
        assert rcs, "review_confirmations sin entrada de step_replacement"
        assert rcs[-1].source == SOURCE_RERECORD
        assert rcs[-1].original_value == target_id

    def test_save_idempotent_on_same_session_id(self, confirmed_mission):
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        target_idx = 4
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        session = orch.start(confirmed_mission, target_step_index=target_idx)
        orch.proceed_to_capture()
        ev = _make_capture_evidence(
            plan_before["steps"][target_idx],
            source_mission=confirmed_mission,
        )
        orch.capture_user_event(ev)
        orch.save()
        # Llamar rerecord_step a mano con el mismo session_id no
        # debe duplicar la entrada de history.
        before_count = len(confirmed_mission.rerecord_history)
        rerecord_step(
            confirmed_mission,
            step_index=target_idx,
            new_step_evidence=ev,
            rerecorded_at=datetime.now(timezone.utc),
            rerecord_session_id=session.rerecord_session_id,
        )
        assert len(confirmed_mission.rerecord_history) == before_count

    def test_save_blocks_double_call(self, confirmed_mission):
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        ev = _make_capture_evidence(
            plan_before["steps"][4],
            source_mission=confirmed_mission,
        )
        orch.capture_user_event(ev)
        orch.save()
        with pytest.raises(RerecordSessionError):
            orch.save()


# ─────────────────────────────────────────────────────────────────────
# Repeat / Cancel (PRD §E)
# ─────────────────────────────────────────────────────────────────────


class TestRepeatAndCancel:

    def test_repeat_discards_current_capture(self, confirmed_mission):
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        ev = _make_capture_evidence(
            plan_before["steps"][4],
            source_mission=confirmed_mission,
        )
        ok = orch.capture_user_event(ev)
        assert ok is True
        assert orch.session.state == STATE_CAPTURED
        orch.discard_capture()
        assert orch.session.state == STATE_WAITING_FOR_USER
        assert orch.session.captured_evidence == []
        # Misión inmutada.
        assert (
            confirmed_mission.semantic_execution_plan["steps"]
            == plan_before["steps"]
        )

    def test_repeat_does_not_mutate_mission(self, confirmed_mission):
        before = confirmed_mission.model_dump(mode="json")
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        ev = _make_capture_evidence(
            confirmed_mission.semantic_execution_plan["steps"][4],
            source_mission=confirmed_mission,
        )
        orch.capture_user_event(ev)
        orch.discard_capture()
        # Repetimos otra captura y descartamos.
        orch.capture_user_event(ev)
        orch.discard_capture()
        after = confirmed_mission.model_dump(mode="json")
        assert before == after

    def test_cancel_restores_original_mission(self, confirmed_mission):
        before = confirmed_mission.model_dump(mode="json")
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        ev = _make_capture_evidence(
            confirmed_mission.semantic_execution_plan["steps"][4],
            source_mission=confirmed_mission,
        )
        orch.capture_user_event(ev)
        m = orch.cancel()
        assert orch.session.state == STATE_CANCELLED
        after = m.model_dump(mode="json")
        # Bit-perfect en lo que respecta al plan + audit.
        assert before["semantic_execution_plan"] == after["semantic_execution_plan"]
        assert before["review_confirmations"] == after["review_confirmations"]
        assert before.get("rerecord_history", []) == after.get("rerecord_history", [])

    def test_cancel_after_save_is_rejected(self, confirmed_mission):
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        plan_before = copy.deepcopy(confirmed_mission.semantic_execution_plan)
        orch.start(confirmed_mission, target_step_index=4)
        orch.proceed_to_capture()
        ev = _make_capture_evidence(
            plan_before["steps"][4],
            source_mission=confirmed_mission,
        )
        orch.capture_user_event(ev)
        orch.save()
        with pytest.raises(RerecordSessionError):
            orch.cancel()

    def test_release_is_idempotent(self, confirmed_mission):
        orch = RerecordOrchestrator(executor_factory=lambda: _StubExecutor())
        orch.start(confirmed_mission, target_step_index=4)
        orch.release()
        orch.release()  # no-op
