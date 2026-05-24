"""Candados runtime: grabador / player / scheduler nunca colisionan.

Bug real que motiva estos tests:
    El usuario abrió Nevlan y empezó a grabar una nueva misión. Al mismo
    tiempo el scheduler tenía configurada "Prueba 2504" cada 10 min con
    anchor 19:00. El primer tick disparó la misión, abrió Chrome+YouTube
    y la grabación capturó esos inputs sintéticos como si fueran del
    usuario. Adicionalmente, la app cerró durante la revisión por una
    nueva ejecución pisando la UI.

Estos tests fijan el contrato:
    - Solo UN player puede correr a la vez.
    - El grabador NO arranca si hay un player activo.
    - El player NO arranca si el grabador está activo.
    - El scheduler ``can_scheduler_run_now`` reporta:
        warmup → durante los primeros segundos tras arrancar la app
        recording → si el grabador está activo
        player_busy → si ya hay un player corriendo
        ok → camino feliz
"""
from __future__ import annotations

import pytest

from app.services.missions.runtime_locks import locks, publish_blocked_event


@pytest.fixture(autouse=True)
def _reset_locks():
    locks.reset_for_tests()
    yield
    locks.reset_for_tests()


# ── Player slot ────────────────────────────────────────────────────────

class TestPlayerSlot:
    def test_acquire_player_first_time_succeeds(self):
        assert locks.acquire_player(mission_id="m1") is True
        snap = locks.snapshot()
        assert snap.player_active is True
        assert snap.player_mission_id == "m1"

    def test_acquire_player_twice_fails(self):
        assert locks.acquire_player(mission_id="m1") is True
        # Segundo player intenta entrar — debe ser rechazado.
        assert locks.acquire_player(mission_id="m2") is False
        # El primer player sigue dueño del slot.
        assert locks.snapshot().player_mission_id == "m1"

    def test_release_player_frees_slot(self):
        assert locks.acquire_player(mission_id="m1") is True
        locks.release_player(mission_id="m1")
        assert locks.snapshot().player_active is False
        # Otro player ya puede entrar.
        assert locks.acquire_player(mission_id="m2") is True

    def test_release_with_wrong_mission_id_is_ignored(self):
        assert locks.acquire_player(mission_id="m1") is True
        locks.release_player(mission_id="m2")  # caller incorrecto
        assert locks.snapshot().player_active is True
        assert locks.snapshot().player_mission_id == "m1"

    def test_acquire_player_blocked_while_recording(self):
        assert locks.acquire_recorder() is True
        assert locks.acquire_player(mission_id="m1") is False
        # Al detener la grabación, el player ya puede arrancar.
        locks.release_recorder()
        assert locks.acquire_player(mission_id="m1") is True


# ── Recorder slot ──────────────────────────────────────────────────────

class TestRecorderSlot:
    def test_acquire_recorder_succeeds(self):
        assert locks.acquire_recorder() is True
        assert locks.is_recording() is True

    def test_acquire_recorder_blocked_while_player_active(self):
        assert locks.acquire_player(mission_id="m1", origin="scheduler") is True
        # Esto es exactamente el bug: el usuario quería grabar, pero
        # ya había un player programado corriendo.
        assert locks.acquire_recorder() is False

    def test_release_recorder_frees_slot(self):
        assert locks.acquire_recorder() is True
        locks.release_recorder()
        assert locks.is_recording() is False


# ── Scheduler gate ─────────────────────────────────────────────────────

class TestSchedulerGate:
    def test_warmup_blocks_initial_runs(self):
        # Simulamos que la app acaba de arrancar.
        locks.force_app_warmup_for_tests()
        ok, reason = locks.can_scheduler_run_now(app_warmup_s=30.0)
        assert ok is False
        assert reason == "warmup"

    def test_warmup_passes_with_zero_seconds(self):
        # En tests permitimos saltar el warmup pasando 0.0.
        locks.force_app_warmup_for_tests()
        ok, reason = locks.can_scheduler_run_now(app_warmup_s=0.0)
        assert ok is True
        assert reason == "ok"

    def test_recording_blocks_scheduler(self):
        assert locks.acquire_recorder() is True
        ok, reason = locks.can_scheduler_run_now(app_warmup_s=0.0)
        assert ok is False
        assert reason == "recording"

    def test_player_busy_blocks_scheduler(self):
        assert locks.acquire_player(mission_id="m1") is True
        ok, reason = locks.can_scheduler_run_now(app_warmup_s=0.0)
        assert ok is False
        assert reason == "player_busy"

    def test_idle_returns_ok(self):
        ok, reason = locks.can_scheduler_run_now(app_warmup_s=0.0)
        assert ok is True
        assert reason == "ok"


# ── publish_blocked_event no rompe en headless ─────────────────────────

class TestPublishBlockedEvent:
    def test_does_not_raise_without_bus(self):
        # Smoke: en CI sin bus configurado, la llamada se traga el error.
        publish_blocked_event(
            kind="recorder",
            reason="player_busy",
            message="msg",
        )
