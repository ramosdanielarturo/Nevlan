"""Scheduler ≠ pisa al usuario.

Bug real:
    El usuario tenía "Prueba 2504" cada 10 min. Al abrir la app, el
    scheduler disparó la misión y abrió Chrome+YouTube mientras el
    usuario intentaba grabar otra cosa. La grabación capturó esos
    inputs sintéticos como del usuario.

Fix:
    ``NevlanScheduler._check_and_execute`` ahora consulta
    ``runtime_locks.can_scheduler_run_now()`` antes de marcar
    ``last_run`` o disparar ``_execute``. Si la grabación está activa,
    o ya hay un player corriendo, el trigger queda PENDIENTE para el
    siguiente tick.

Estos tests aíslan ``_check_and_execute`` con una entrada due y
mockean ``_execute`` para verificar:
    - Si está grabando: NO se llama ``_execute``, NO se marca last_run.
    - Si hay player corriendo: NO se llama ``_execute``.
    - Si todo está libre: SÍ se llama ``_execute``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.services.missions.runtime_locks import locks
from app.services.missions.scheduler import (
    NevlanScheduler,
    ScheduleEntry,
    TriggerConfig,
)


@pytest.fixture(autouse=True)
def _reset_locks():
    locks.reset_for_tests()
    yield
    locks.reset_for_tests()


def _scheduler_with_due_entry(monkeypatch: pytest.MonkeyPatch) -> NevlanScheduler:
    """Construye un scheduler en memoria con UNA entry vencida.

    Evitamos pisar ``var/schedules.json`` real: parchamos ``_save`` y
    creamos la entry programáticamente.
    """
    sched = NevlanScheduler()
    sched._schedules.clear()
    monkeypatch.setattr(sched, "_save", lambda: None)

    trigger = TriggerConfig({
        "mode": "Cada X minutos",
        "interval_minutes": 1,
        "last_run": (datetime.now() - timedelta(minutes=10)).isoformat(),
    })
    entry = ScheduleEntry(
        mission_id="m-bug-2504",
        mission_name="Prueba 2504",
        data={"enabled": True, "triggers": [trigger.to_dict()]},
    )
    # Forzamos last_run viejo para que ``is_due`` devuelva True.
    entry.triggers[0].last_run = datetime.now() - timedelta(minutes=10)
    sched._schedules[entry.mission_id] = entry
    return sched


class TestSchedulerRuntimeLock:
    def test_skips_execution_while_recording(self, monkeypatch):
        sched = _scheduler_with_due_entry(monkeypatch)

        # Simula que el usuario está grabando.
        assert locks.acquire_recorder() is True

        executed: list = []
        monkeypatch.setattr(
            sched, "_execute", lambda *a, **kw: executed.append(a)
        )

        sched._check_and_execute()

        assert executed == [], (
            "El scheduler ejecutó una misión mientras el usuario grababa "
            "— ese era el bug original que abría Chrome encima de la "
            "grabación."
        )
        # ``last_run`` no debe haberse actualizado: queremos que el
        # trigger se evalúe de nuevo en el siguiente tick (cuando ya
        # no haya grabación).
        assert sched._schedules["m-bug-2504"].triggers[0].is_due() is True

    def test_skips_execution_when_player_busy(self, monkeypatch):
        sched = _scheduler_with_due_entry(monkeypatch)

        # Simulamos que ya hay un player en marcha.
        assert locks.acquire_player(mission_id="other-mission") is True

        executed: list = []
        monkeypatch.setattr(
            sched, "_execute", lambda *a, **kw: executed.append(a)
        )

        sched._check_and_execute()

        assert executed == []
        # Trigger sigue due (no consumido).
        assert sched._schedules["m-bug-2504"].triggers[0].is_due() is True

    def test_runs_when_idle(self, monkeypatch):
        sched = _scheduler_with_due_entry(monkeypatch)

        executed: list = []
        monkeypatch.setattr(
            sched, "_execute", lambda *a, **kw: executed.append(a)
        )

        sched._check_and_execute()

        assert len(executed) == 1, "Camino feliz: scheduler debe ejecutar."
        # ``last_run`` se actualizó → siguiente check no dispara.
        sched._check_and_execute()
        assert len(executed) == 1


class TestRecorderRefuses:
    """Doble candado: aunque el scheduler intentara saltarse el gate,
    el propio ``MissionPlayer.replay`` rechaza arrancar si el grabador
    está activo.
    """

    def test_player_replay_rejected_while_recording(self):
        from app.contracts.mission import Mission, ReplayStatus
        from app.services.missions.player import MissionPlayer

        # Recorder activo → cualquier replay debe fallar limpio.
        assert locks.acquire_recorder() is True

        m = Mission(name="x")
        player = MissionPlayer(m)
        execution = player.replay(auto_confirm=True, variables={})
        assert execution.status == ReplayStatus.FAILED
        assert "grabador" in (execution.error_details or "").lower()

    def test_player_singleton_second_replay_rejected(self):
        from app.contracts.mission import Mission, ReplayStatus
        from app.services.missions.player import MissionPlayer

        # Primer player toma el slot.
        assert locks.acquire_player(mission_id="m-other") is True

        m = Mission(name="x")
        player = MissionPlayer(m)
        execution = player.replay(auto_confirm=True, variables={})
        assert execution.status == ReplayStatus.FAILED
        msg = (execution.error_details or "").lower()
        assert "automatización" in msg or "ejecuci" in msg
