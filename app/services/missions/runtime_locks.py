"""Candados globales para prevenir colisiones grabador / reproductor / scheduler.

Problema real (visto en producción): el scheduler tenía una misión "Cada
10 min" que se disparaba mientras el usuario grababa otra cosa. Resultado:
Chrome se abría solo, la grabación capturaba inputs sintéticos del player
y la app a veces cerraba por race conditions al revisar la nueva misión.

Reglas absolutas que esta capa garantiza:
  1. Solo UN ``MissionPlayer`` puede estar reproduciendo a la vez.
  2. Si el grabador está activo, NO se permite arrancar un player.
  3. Si un player está activo, NO se permite arrancar el grabador.
  4. El scheduler debe consultar ``can_start_player()`` antes de cada
     ``_execute()`` y, si no puede, posponer el trigger (sin marcar
     ``last_run``).
  5. Cualquier rechazo se publica al bus para que la UI lo muestre,
     pero la lógica de servicio no depende del bus para funcionar.

Diseño:
  - Solo dos flags + un threading.Lock. Sin singletons complejos.
  - APIs pequeñas y testeables: ``acquire_player`` / ``release_player`` /
    ``acquire_recorder`` / ``release_recorder`` / ``snapshot()``.
  - Si el bus no está disponible (tests headless), las funciones
    siguen operando — solo no publican eventos.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RuntimeLockSnapshot:
    """Foto inmutable del estado actual (solo para introspección/tests)."""

    player_active: bool
    player_origin: str             # "scheduler" | "manual" | ""
    player_mission_id: str
    player_started_at: Optional[datetime]
    recorder_active: bool
    recorder_started_at: Optional[datetime]


class _RuntimeLocks:
    """Estado compartido entre player, recorder y scheduler.

    Todos los métodos son thread-safe (un único ``threading.Lock``).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._player_active: bool = False
        self._player_origin: str = ""
        self._player_mission_id: str = ""
        self._player_started_at: Optional[datetime] = None
        self._recorder_active: bool = False
        self._recorder_started_at: Optional[datetime] = None
        # Marca el inicio del proceso. El scheduler la consulta para
        # NO disparar nada en los primeros segundos (anti-catch-up real).
        self._app_started_at: datetime = datetime.now(timezone.utc)

    # ── Player ───────────────────────────────────────────────────
    def acquire_player(
        self,
        *,
        mission_id: str,
        origin: str = "manual",
    ) -> bool:
        """Reserva el slot único de player. Devuelve False si está ocupado
        o si la grabación está activa.
        """
        with self._lock:
            if self._player_active:
                return False
            if self._recorder_active:
                return False
            self._player_active = True
            self._player_origin = origin or "manual"
            self._player_mission_id = mission_id or ""
            self._player_started_at = datetime.now(timezone.utc)
            return True

    def release_player(self, *, mission_id: str = "") -> None:
        with self._lock:
            # Si el caller pasa mission_id y no coincide con el slot
            # actual, ignoramos el release para evitar que un caller
            # antiguo libere el slot de otro player. Pero permitimos
            # release sin mission_id (compat).
            if (
                mission_id
                and self._player_mission_id
                and mission_id != self._player_mission_id
            ):
                return
            self._player_active = False
            self._player_origin = ""
            self._player_mission_id = ""
            self._player_started_at = None

    def is_player_active(self) -> bool:
        with self._lock:
            return self._player_active

    # ── Recorder ─────────────────────────────────────────────────
    def acquire_recorder(self) -> bool:
        """Marca grabación activa. Devuelve False si hay un player
        corriendo (no podemos grabar mientras Nevlan ejecuta otra cosa).
        """
        with self._lock:
            if self._player_active:
                return False
            if self._recorder_active:
                return False
            self._recorder_active = True
            self._recorder_started_at = datetime.now(timezone.utc)
            return True

    def release_recorder(self) -> None:
        with self._lock:
            self._recorder_active = False
            self._recorder_started_at = None

    def is_recording(self) -> bool:
        with self._lock:
            return self._recorder_active

    # ── Scheduler helper ─────────────────────────────────────────
    def can_scheduler_run_now(
        self, *, app_warmup_s: float = 30.0,
    ) -> tuple[bool, str]:
        """¿El scheduler puede disparar una ejecución programada ahora?

        Devuelve (ok, reason). ``reason`` es una clave estable para logs/
        tests: "warmup" | "recording" | "player_busy" | "ok".
        """
        with self._lock:
            elapsed = (
                datetime.now(timezone.utc) - self._app_started_at
            ).total_seconds()
            if elapsed < max(0.0, float(app_warmup_s)):
                return False, "warmup"
            if self._recorder_active:
                return False, "recording"
            if self._player_active:
                return False, "player_busy"
            return True, "ok"

    # ── Test helpers ─────────────────────────────────────────────
    def reset_for_tests(self) -> None:
        """SOLO TESTS: limpia todo el estado y resetea ``app_started_at``
        para que las funciones que dependen del warmup no bloqueen.
        """
        with self._lock:
            self._player_active = False
            self._player_origin = ""
            self._player_mission_id = ""
            self._player_started_at = None
            self._recorder_active = False
            self._recorder_started_at = None
            # ``app_started_at`` lo dejamos retroactivo: warmup ya pasado.
            self._app_started_at = datetime(2000, 1, 1, tzinfo=timezone.utc)

    def force_app_warmup_for_tests(self) -> None:
        """SOLO TESTS: simula que la app acaba de arrancar."""
        with self._lock:
            self._app_started_at = datetime.now(timezone.utc)

    def snapshot(self) -> RuntimeLockSnapshot:
        with self._lock:
            return RuntimeLockSnapshot(
                player_active=self._player_active,
                player_origin=self._player_origin,
                player_mission_id=self._player_mission_id,
                player_started_at=self._player_started_at,
                recorder_active=self._recorder_active,
                recorder_started_at=self._recorder_started_at,
            )


# Instancia global (no es Singleton clase, solo módulo-singleton).
locks = _RuntimeLocks()


# ── Helpers de bus (publicación de eventos) ──────────────────────────
# Se separa del estado para que ``runtime_locks`` siga siendo importable
# en tests headless aunque el bus esté inactivo.

def publish_blocked_event(
    *, kind: str, reason: str, message: str,
) -> None:
    """Publica un evento ``mission.runtime_blocked`` no crítico.

    ``kind`` puede ser "scheduler" | "recorder" | "player".
    Si el bus o pydantic no están disponibles, se ignora silenciosamente.
    """
    try:
        from app.runtime.bus import bus
        from app.contracts.events import SystemEvent

        bus.publish(
            SystemEvent(
                name="mission.runtime_blocked",
                payload={
                    "kind": kind,
                    "reason": reason,
                    "message": message,
                },
            )
        )
    except Exception:
        pass


__all__ = [
    "RuntimeLockSnapshot",
    "locks",
    "publish_blocked_event",
]
