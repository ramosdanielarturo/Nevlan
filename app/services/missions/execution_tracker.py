"""
Nevlan — Execution Tracker
---------------------------
Registra ejecuciones y favoritos. Persistencia en var/.

PRD 2026-05-08c §exec-lifecycle: cada entrada persiste el lifecycle
completo (``ExecutionStatus`` canónico + ``execution_trace``) además
de los campos legacy. El historial de la UI ya NO se basa en strings
libres; usa :func:`derive_history_label` sobre el lifecycle real.
"""
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.core.config import settings
from app.core.logger import log
from app.services.missions.execution_lifecycle import (
    ExecutionRun,
    ExecutionStatus,
    ExecutionTrace,
    derive_history_icon,
    derive_history_label,
    is_history_executed,
)

EXEC_LOG_FILE = settings.VAR_PATH / "execution_log.json"
FAVORITES_FILE = settings.VAR_PATH / "favorites.json"


# Mapeo legacy ``status string`` → :class:`ExecutionStatus`.
# Acepta entradas antiguas (success/error/failed/...) sin perder
# información para que el historial siga siendo legible tras
# migrar el código.
_LEGACY_STATUS_MAP = {
    "success": ExecutionStatus.SUCCEEDED,
    "succeeded": ExecutionStatus.SUCCEEDED,
    "success_with_recovery": ExecutionStatus.SUCCEEDED,
    "failed": ExecutionStatus.FAILED,
    "error": ExecutionStatus.FAILED,
    "blocked": ExecutionStatus.FAILED,
    "stopped_for_human": ExecutionStatus.FAILED,
    "cancelled": ExecutionStatus.CANCELLED,
    "timed_out": ExecutionStatus.TIMED_OUT,
    "stuck": ExecutionStatus.STUCK,
    "running": ExecutionStatus.RUNNING,
    "step_running": ExecutionStatus.STEP_RUNNING,
    "queued": ExecutionStatus.QUEUED,
    "starting": ExecutionStatus.STARTING,
}


def _canonical_status(raw: str) -> ExecutionStatus:
    if not raw:
        return ExecutionStatus.RUNNING
    try:
        return ExecutionStatus(raw)
    except Exception:
        pass
    return _LEGACY_STATUS_MAP.get(str(raw).lower(), ExecutionStatus.RUNNING)


class ExecutionEntry:
    def __init__(self, data: dict):
        # id único para poder borrar entradas individuales del historial.
        # Entradas antiguas sin id reciben uno al cargar (_load).
        self.id = data.get("id") or uuid.uuid4().hex[:12]
        self.mission_id = data.get("mission_id", "")
        self.mission_name = data.get("mission_name", "")
        self.started_at = data.get("started_at", "")
        self.completed_at = data.get("completed_at", "")
        # ``status`` permanece como string libre por back-compat.
        # ``lifecycle_status`` es la fuente nueva (PRD §A).
        self.status = data.get("status", "unknown")
        self.lifecycle_status: ExecutionStatus = _canonical_status(
            data.get("lifecycle_status") or data.get("status") or "",
        )
        self.error_code = str(data.get("error_code") or "")
        self.steps_executed = data.get("steps_executed", 0)
        self.total_steps = data.get("total_steps", 0)
        self.error = data.get("error", "")
        self.trigger = data.get("trigger", "manual")  # manual, scheduled, voice
        # Trace estructurado (lista de :class:`StepExecutionEntry`).
        # Permanece como ``list`` cruda para serialización; los
        # consumidores lo levantan con ``ExecutionTrace.from_list``
        # cuando lo necesitan.
        self.execution_trace: List[Dict[str, Any]] = list(
            data.get("execution_trace") or [],
        )
        self.terminal_signal_received: bool = bool(
            data.get("terminal_signal_received", False),
        )
        self.plan_source: str = str(data.get("plan_source") or "")
        self.executor_used: str = str(data.get("executor_used") or "")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "mission_id": self.mission_id,
            "mission_name": self.mission_name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "lifecycle_status": self.lifecycle_status.value,
            "error_code": self.error_code,
            "steps_executed": self.steps_executed,
            "total_steps": self.total_steps,
            "error": self.error,
            "trigger": self.trigger,
            "execution_trace": list(self.execution_trace or []),
            "terminal_signal_received": bool(self.terminal_signal_received),
            "plan_source": self.plan_source,
            "executor_used": self.executor_used,
        }

    # ─── Vista honesta (PRD §C) ──────────────────────────

    @property
    def history_label(self) -> str:
        """Etiqueta visible: "Ejecutado" / "Falló" / "No iniciado" /
        "Cancelado" / "Sin respuesta" / "En ejecución".

        Importante: NO confiamos en ``self.status`` cuando hay
        contradicción entre ``lifecycle_status="succeeded"`` y
        ``execution_trace=[]``. En ese caso reportamos "No iniciado"
        para evitar mentir al usuario (PRD §C, §D).
        """
        run = self._as_lifecycle_view()
        return derive_history_label(run)

    @property
    def history_icon(self) -> str:
        return derive_history_icon(self._as_lifecycle_view())

    @property
    def is_executed(self) -> bool:
        return is_history_executed(self._as_lifecycle_view())

    def _as_lifecycle_view(self) -> ExecutionRun:
        """Reconstruye un :class:`ExecutionRun` mínimo SOLO para
        ejecutar las funciones honestas de derive_*. Si el log
        antiguo dice "success" pero el trace está vacío, esto
        resulta en lifecycle "failed/NO_STEPS_STARTED" para la
        UI — sin tocar el JSON original (back-compat)."""
        from datetime import datetime, timezone

        run = ExecutionRun(
            mission_id=self.mission_id,
            mission_name=self.mission_name,
            total_steps=int(self.total_steps or 0),
            trigger=self.trigger,
            plan_source=self.plan_source,
            executor_used=self.executor_used,
            status=self.lifecycle_status,
            error_code=self.error_code,
            terminal_signal_received=self.terminal_signal_received,
            trace=ExecutionTrace.from_list(self.execution_trace or []),
        )
        # Defensa: lifecycle dice "succeeded" pero trace vacío ⇒
        # historial se pinta como "No iniciado". Por el lock interno,
        # al estar ya en SUCCEEDED, no podemos transicionar a FAILED;
        # devolvemos un nuevo run con SUCCEEDED reemplazado en bruto.
        if (
            run.status == ExecutionStatus.SUCCEEDED
            and not run.trace.has_any_succeeded
        ):
            run.status = ExecutionStatus.FAILED
            run.error_code = run.error_code or "NO_STEPS_STARTED"
        return run

    @property
    def started_dt(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.started_at)
        except:
            return None

    @property
    def icon(self) -> str:
        # Antes: "✅" si status=="success". El bug era exactamente ése —
        # el caller seteaba ``status="success"`` por default aunque
        # el worker no hubiera arrancado. Ahora derivamos el icono del
        # lifecycle real (PRD §C).
        return self.history_icon

    @property
    def time_ago(self) -> str:
        dt = self.started_dt
        if not dt:
            return "—"
        delta = datetime.now() - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 1:
            return "Ahora"
        if mins < 60:
            return f"Hace {mins} min"
        hours = mins // 60
        if hours < 24:
            return f"Hace {hours}h"
        days = hours // 24
        return f"Hace {days}d"

    @property
    def duration_ms(self) -> Optional[int]:
        try:
            a = datetime.fromisoformat(self.started_at)
            b = datetime.fromisoformat(self.completed_at) if self.completed_at else None
            if a and b:
                return int((b - a).total_seconds() * 1000)
        except Exception:
            pass
        return None

    @property
    def duration_human(self) -> str:
        ms = self.duration_ms
        if ms is None:
            return "—"
        if ms < 1000:
            return f"{ms} ms"
        s = ms / 1000.0
        if s < 60:
            return f"{s:.1f}s"
        m, s2 = divmod(int(s), 60)
        return f"{m}m {s2}s"

    @property
    def required_intervention(self) -> bool:
        return self.lifecycle_status in (
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.STUCK,
        )

    @property
    def datetime_human(self) -> str:
        dt = self.started_dt
        if not dt:
            return "—"
        today = datetime.now().date()
        if dt.date() == today:
            return f"Hoy · {dt.strftime('%H:%M')}"
        return dt.strftime("%d %b · %H:%M")


class ExecutionTracker:
    """Singleton que registra ejecuciones y gestiona favoritos."""

    _instance = None

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: List[ExecutionEntry] = []
        self._favorites: Set[str] = set()
        self._load()

    @classmethod
    def get_instance(cls) -> 'ExecutionTracker':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load(self):
        try:
            if EXEC_LOG_FILE.exists():
                with open(EXEC_LOG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._entries = [ExecutionEntry(e) for e in data.get("entries", [])]
        except Exception as e:
            log.error(f"ExecutionTracker: error cargando log: {e}")

        try:
            if FAVORITES_FILE.exists():
                with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
                    self._favorites = set(json.load(f).get("ids", []))
        except Exception as e:
            log.error(f"ExecutionTracker: error cargando favoritos: {e}")

    def _save_log(self):
        try:
            EXEC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {"entries": [e.to_dict() for e in self._entries[-500:]]}
            with open(EXEC_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"ExecutionTracker: error guardando log: {e}")

    def _save_favorites(self):
        try:
            FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
                json.dump({"ids": list(self._favorites)}, f, ensure_ascii=False)
        except Exception as e:
            log.error(f"ExecutionTracker: error guardando favoritos: {e}")

    # ── Execution Log ──

    def log_start(self, mission_id: str, mission_name: str, total_steps: int, trigger: str = "manual") -> int:
        """Registra el inicio de una ejecución. Retorna el índice.

        PRD §A: el estado inicial es ``queued``; **NO** se marca como
        "running" hasta que el worker realmente arranca, ni "success"
        hasta que termine bien. El historial NUNCA debe pintarse como
        "Ejecutado" en este punto.
        """
        with self._lock:
            entry = ExecutionEntry({
                "mission_id": mission_id,
                "mission_name": mission_name,
                "started_at": datetime.now().isoformat(),
                "status": "queued",
                "lifecycle_status": ExecutionStatus.QUEUED.value,
                "total_steps": total_steps,
                "trigger": trigger,
            })
            self._entries.append(entry)
            self._save_log()
            return len(self._entries) - 1

    def log_run_started(self, index: int, run: ExecutionRun) -> None:
        """Refleja en el historial que el worker arrancó.

        Estado pasa a ``running``. La ventana flotante puede haber
        empezado a mostrar progreso, pero el historial todavía NO
        dirá "Ejecutado".
        """
        with self._lock:
            if not (0 <= index < len(self._entries)):
                return
            e = self._entries[index]
            e.status = "running"
            e.lifecycle_status = ExecutionStatus.RUNNING
            e.executor_used = run.executor_used
            e.plan_source = run.plan_source
            self._save_log()

    def log_run_finished(self, index: int, run: ExecutionRun) -> None:
        """Cierra la entrada del historial usando el ``ExecutionRun``
        canónico (lifecycle + trace). Esta es la API recomendada
        para los callers nuevos. PRD §C.
        """
        with self._lock:
            if not (0 <= index < len(self._entries)):
                return
            e = self._entries[index]
            e.lifecycle_status = run.status
            e.status = run.status.value  # back-compat field
            e.completed_at = (
                run.finished_at.isoformat() if run.finished_at
                else datetime.now().isoformat()
            )
            e.error_code = run.error_code
            e.error = run.error_message
            e.steps_executed = len(run.trace.succeeded_step_ids)
            e.execution_trace = run.trace.to_list()
            e.terminal_signal_received = bool(
                run.terminal_signal_received,
            )
            e.plan_source = run.plan_source or e.plan_source
            e.executor_used = run.executor_used or e.executor_used
            self._save_log()

    def log_complete(self, index: int, status: str, steps_executed: int = 0, error: str = ""):
        """API legacy. Convierte el ``status`` libre al lifecycle
        canónico vía :func:`_canonical_status` para que el historial
        no muestre "Ejecutado" sin trace real.
        """
        with self._lock:
            if 0 <= index < len(self._entries):
                e = self._entries[index]
                canonical = _canonical_status(status)
                e.status = status
                e.lifecycle_status = canonical
                e.completed_at = datetime.now().isoformat()
                e.steps_executed = steps_executed
                e.error = error
                # Si el caller dijo "success" pero no hay trace, hay
                # que degradar a NO_STEPS_STARTED para no mentir.
                if (
                    canonical == ExecutionStatus.SUCCEEDED
                    and not (e.execution_trace or steps_executed > 0)
                ):
                    e.lifecycle_status = ExecutionStatus.FAILED
                    e.error_code = e.error_code or "NO_STEPS_STARTED"
                self._save_log()

    def get_recent(self, limit: int = 20) -> List[ExecutionEntry]:
        return list(reversed(self._entries[-limit:]))

    def get_since(self, days: int = 30, limit: int = 500) -> List[ExecutionEntry]:
        """Historial de los últimos `days`, más reciente primero."""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=days)
        out = []
        for e in reversed(self._entries):
            dt = e.started_dt
            if dt is None:
                continue
            if dt < cutoff:
                break
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def get_for_mission(self, mission_id: str, limit: int = 50) -> List[ExecutionEntry]:
        entries = [e for e in self._entries if e.mission_id == mission_id]
        return list(reversed(entries[-limit:]))

    def update_mission_name(self, mission_id: str, new_name: str) -> int:
        """Renombra todas las entradas históricas con ese mission_id.

        Se usa cuando el usuario renombra una misión: el "Actividad reciente"
        refleja inmediatamente el nuevo nombre. Devuelve cuántas entradas
        cambiaron.
        """
        if not mission_id or not new_name:
            return 0
        changed = 0
        with self._lock:
            for e in self._entries:
                if e.mission_id == mission_id and e.mission_name != new_name:
                    e.mission_name = new_name
                    changed += 1
            if changed:
                self._save_log()
        return changed

    def delete_entry(self, entry_id: str) -> bool:
        """Elimina una entrada del historial por su id.

        NO elimina la automatización subyacente — solo el registro
        de ejecución. Devuelve True si se encontró y eliminó.
        """
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.id != entry_id]
            removed = len(self._entries) < before
            if removed:
                self._save_log()
            return removed

    def get_failed_mission_ids(self) -> Set[str]:
        """IDs de misiones cuya última ejecución falló (lifecycle real)."""
        last = {}
        for e in self._entries:
            last[e.mission_id] = e
        return {
            mid for mid, e in last.items()
            if e.lifecycle_status in (
                ExecutionStatus.FAILED,
                ExecutionStatus.TIMED_OUT,
                ExecutionStatus.STUCK,
            )
        }

    # ── Favorites ──

    def is_favorite(self, mission_id: str) -> bool:
        return mission_id in self._favorites

    def toggle_favorite(self, mission_id: str) -> bool:
        """Toggle y retorna el nuevo estado."""
        with self._lock:
            if mission_id in self._favorites:
                self._favorites.discard(mission_id)
                result = False
            else:
                self._favorites.add(mission_id)
                result = True
            self._save_favorites()
            return result

    def get_favorites(self) -> Set[str]:
        return set(self._favorites)


# Singleton
tracker = ExecutionTracker.get_instance()
