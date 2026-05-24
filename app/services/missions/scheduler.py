"""
Nevlan — Scheduler Service
---------------------------
Ejecuta automatizaciones programadas en background.

Modelo actual:
- Cada misión tiene UN `ScheduleEntry` con `enabled` global y una LISTA de
  `TriggerConfig` (disparadores). Cada trigger es "Diario / Semanal / Cada X
  minutos / Por evento / Bajo demanda" con sus parámetros propios y su
  propio `last_run`. La misión se ejecuta cuando CUALQUIER trigger esté due.

Retro-compatibilidad:
- El formato antiguo (un solo trigger aplanado en la entry) se migra
  automáticamente al cargar.

Persistencia en var/schedules.json.
"""
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional, List

from app.core.config import settings
from app.core.logger import log


SCHEDULES_FILE = settings.VAR_PATH / "schedules.json"


# ═══════════════════════════════════════════════════════════════════════
# TriggerConfig — un disparador individual
# ═══════════════════════════════════════════════════════════════════════
class TriggerConfig:
    """Un disparador individual (Diario / Semanal / Cada X minutos /
    Por evento / Bajo demanda).

    Cada trigger mantiene su propio `last_run` para no ejecutarse de más
    cuando una misión tiene varios triggers.
    """
    MODES = ("Bajo demanda (manual)", "Diario", "Semanal", "Cada X minutos", "Por evento")

    def __init__(self, data: Optional[dict] = None):
        d = data or {}
        self.mode: str = d.get("mode", "Bajo demanda (manual)")
        self.time: str = d.get("time", "09:00")
        self.days: List[str] = list(d.get("days", []) or [])
        self.interval_minutes: int = int(d.get("interval_minutes", 30) or 30)
        # Hora de referencia para "Cada X minutos": cuando está definida,
        # el trigger dispara en esa hora y luego en los múltiplos sucesivos
        # del intervalo (p. ej. anchor 09:00 + cada 10 min → 09:00, 09:10,
        # 09:20 …). Si queda en None se mantiene la lógica anterior
        # basada en `last_run` (dispara X minutos después del último run).
        self.anchor_time: Optional[str] = d.get("anchor_time") or None
        self.event_type: str = d.get("event_type", "") or ""
        self.event_param: str = d.get("event_param", "") or ""
        self.last_run: Optional[datetime] = None
        last_run_str = d.get("last_run")
        if last_run_str:
            try:
                self.last_run = datetime.fromisoformat(last_run_str)
            except Exception:
                pass

        # Evitar disparo inmediato en triggers "Cada X minutos" recién
        # creados o editados: cuando el usuario pulsa "Guardar" no debe
        # dispararse "ahora" sino en el siguiente instante programado.
        #   - Con anchor: alineamos al slot pasado más reciente, para que
        #     el próximo disparo sea el siguiente slot futuro del reloj
        #     (p. ej. 07:00 + cada 10 min guardado a las 19:05 → 19:10).
        #   - Sin anchor: fijamos last_run = ahora, para que el primer
        #     disparo ocurra cuando pase el intervalo completo.
        if self.mode == "Cada X minutos" and self.last_run is None:
            try:
                if self._parse_anchor() is not None:
                    slot = self._most_recent_slot(datetime.now())
                    if slot is not None:
                        self.last_run = slot
                else:
                    self.last_run = datetime.now()
            except Exception:
                pass

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "time": self.time,
            "days": list(self.days),
            "interval_minutes": int(self.interval_minutes),
            "anchor_time": self.anchor_time,
            "event_type": self.event_type,
            "event_param": self.event_param,
            "last_run": self.last_run.isoformat() if self.last_run else None,
        }

    def _parse_anchor(self) -> Optional[tuple]:
        """Devuelve (hora, min) del anchor si está configurado."""
        if not self.anchor_time:
            return None
        try:
            h, m = map(int, self.anchor_time.split(":"))
            if 0 <= h < 24 and 0 <= m < 60:
                return h, m
        except Exception:
            pass
        return None

    def _most_recent_slot(self, now: datetime) -> Optional[datetime]:
        """Último slot `anchor + k·interval` anterior o igual a `now`.

        Solo aplica a 'Cada X minutos' con anchor_time definido. Calcula el
        slot más reciente (pudiendo ser hoy o ayer si aún no pasó el anchor
        de hoy).
        """
        if self.mode != "Cada X minutos":
            return None
        ah = self._parse_anchor()
        if ah is None or self.interval_minutes <= 0:
            return None
        anchor_today = now.replace(
            hour=ah[0], minute=ah[1], second=0, microsecond=0
        )
        base = anchor_today if now >= anchor_today else anchor_today - timedelta(days=1)
        elapsed_min = (now - base).total_seconds() / 60.0
        n = int(elapsed_min // self.interval_minutes)
        return base + timedelta(minutes=n * self.interval_minutes)

    def is_due(self) -> bool:
        """True si este trigger debe disparar la misión ahora."""
        now = datetime.now()
        if self.mode == "Bajo demanda (manual)":
            return False

        if self.mode == "Diario":
            try:
                target_h, target_m = map(int, self.time.split(":"))
            except Exception:
                return False
            if now.hour == target_h and now.minute == target_m:
                if not self.last_run or self.last_run.date() < now.date():
                    return True
            return False

        if self.mode == "Semanal":
            day_map = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}
            today_idx = now.weekday()
            today_name = [k for k, v in day_map.items() if v == today_idx]
            if today_name and today_name[0] in self.days:
                try:
                    target_h, target_m = map(int, self.time.split(":"))
                except Exception:
                    return False
                if now.hour == target_h and now.minute == target_m:
                    if not self.last_run or self.last_run.date() < now.date():
                        return True
            return False

        if self.mode == "Cada X minutos":
            # Con anchor_time: disparamos cuando hay un slot
            # (anchor + k·interval) posterior al último run.
            if self._parse_anchor() is not None:
                slot = self._most_recent_slot(now)
                if slot is None:
                    return False
                if self.last_run is None:
                    return True
                return slot > self.last_run
            # Sin anchor: lógica clásica basada en tiempo transcurrido.
            if not self.last_run:
                return True
            elapsed = (now - self.last_run).total_seconds() / 60
            return elapsed >= self.interval_minutes

        # "Por evento" se maneja externamente (engine de triggers)
        return False

    @staticmethod
    def _fmt_remaining(seconds: float) -> str:
        """Devuelve un string compacto y útil:
          - <60s : "en Xs"
          - <1h  : "en Xm Ys"  (con segundos para el último minuto)
          - <1d  : "en Hh Mm"
          - >=1d : "en Xd Hh"
        Pensado para refrescarse cada segundo sin saturar la UI.
        """
        s = int(max(0, seconds))
        if s < 60:
            return f"en {s}s"
        if s < 3600:
            m, sec = divmod(s, 60)
            return f"en {m}m {sec:02d}s"
        if s < 86400:
            h, rem = divmod(s, 3600)
            m, _sec = divmod(rem, 60)
            return f"en {h}h {m:02d}m"
        d, rem = divmod(s, 86400)
        h, _rem = divmod(rem, 3600)
        return f"en {d}d {h:02d}h"

    def next_run_datetime(self) -> Optional[datetime]:
        """Devuelve el datetime exacto de la próxima ejecución (None si no
        aplica). Útil para mostrar en la UI la hora concreta además del
        contador regresivo."""
        now = datetime.now()
        if self.mode == "Bajo demanda (manual)":
            return None
        if self.mode == "Diario":
            try:
                target_h, target_m = map(int, self.time.split(":"))
            except Exception:
                return None
            target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
        if self.mode == "Semanal":
            try:
                target_h, target_m = map(int, self.time.split(":"))
            except Exception:
                return None
            valid_days = {"lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
                          "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5,
                          "domingo": 6}
            indices = sorted({valid_days[d.lower()] for d in self.days
                              if d.lower() in valid_days})
            if not indices:
                return None
            for offset in range(0, 8):
                cand = now + timedelta(days=offset)
                if cand.weekday() not in indices:
                    continue
                tgt = cand.replace(hour=target_h, minute=target_m,
                                   second=0, microsecond=0)
                if tgt > now:
                    return tgt
            return None
        if self.mode == "Cada X minutos":
            if self._parse_anchor() is not None:
                slot = self._most_recent_slot(now)
                if slot is None:
                    return None
                # Si el slot más reciente aún no se disparó (last_run < slot),
                # el próximo fire es justamente ese slot. Si ya se disparó,
                # el siguiente es slot + intervalo.
                if self.last_run is None or slot > self.last_run:
                    return slot
                return slot + timedelta(minutes=self.interval_minutes)
            if not self.last_run:
                return None
            return self.last_run + timedelta(minutes=self.interval_minutes)
        return None

    def seconds_until_next(self) -> Optional[float]:
        """Segundos hasta la próxima ejecución (None si no aplica)."""
        now = datetime.now()
        if self.mode == "Bajo demanda (manual)":
            return None
        if self.mode == "Diario":
            try:
                target_h, target_m = map(int, self.time.split(":"))
            except Exception:
                return None
            target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return (target - now).total_seconds()
        if self.mode == "Semanal":
            try:
                target_h, target_m = map(int, self.time.split(":"))
            except Exception:
                return None
            if not self.days:
                return None
            valid_days = {"lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
                          "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5,
                          "domingo": 6}
            indices = sorted({valid_days[d.lower()] for d in self.days
                              if d.lower() in valid_days})
            if not indices:
                return None
            for offset in range(0, 8):
                cand = now + timedelta(days=offset)
                if cand.weekday() not in indices:
                    continue
                tgt = cand.replace(hour=target_h, minute=target_m,
                                   second=0, microsecond=0)
                if tgt > now:
                    return (tgt - now).total_seconds()
            return None
        if self.mode == "Cada X minutos":
            # Si hay anchor, delegamos en next_run_datetime (alineado a reloj).
            if self._parse_anchor() is not None:
                nxt = self.next_run_datetime()
                if nxt is None:
                    return None
                return max(0.0, (nxt - now).total_seconds())
            if not self.last_run:
                return 0.0
            next_time = self.last_run + timedelta(minutes=self.interval_minutes)
            return max(0.0, (next_time - now).total_seconds())
        return None

    def next_run_str(self) -> str:
        """Descripción corta de cuándo dispara."""
        now = datetime.now()
        if self.mode == "Bajo demanda (manual)":
            return "Manual"
        if self.mode == "Diario":
            secs = self.seconds_until_next() or 0
            # < 24h: cuenta regresiva precisa al segundo.
            if 0 <= secs < 86400:
                return f"Hoy {self.time}  ·  {self._fmt_remaining(secs)}"
            return f"Mañana {self.time}"
        if self.mode == "Semanal":
            days_str = ", ".join(d.capitalize() for d in self.days) or "—"
            secs = self.seconds_until_next()
            if secs is not None and secs < 86400:
                return f"{days_str} {self.time}  ·  {self._fmt_remaining(secs)}"
            return f"{days_str} {self.time}"
        if self.mode == "Cada X minutos":
            # Con anchor_time: alineado a reloj (anchor + k·interval).
            if self._parse_anchor() is not None:
                nxt = self.next_run_datetime()
                if nxt is None:
                    return f"cada {self.interval_minutes} min · desde {self.anchor_time}"
                secs = max(0.0, (nxt - now).total_seconds())
                if secs <= 0:
                    return "ahora"
                clock = nxt.strftime("%H:%M:%S") if secs < 3600 else nxt.strftime("%H:%M")
                return f"{self._fmt_remaining(secs)} · próx. {clock}"
            # Sin anchor: basado en last_run.
            if not self.last_run:
                return f"cada {self.interval_minutes} min"
            secs = self.seconds_until_next() or 0
            if secs <= 0:
                return "ahora"
            next_dt = self.last_run + timedelta(minutes=self.interval_minutes)
            clock = next_dt.strftime("%H:%M:%S") if secs < 3600 else next_dt.strftime("%H:%M")
            return f"{self._fmt_remaining(secs)} · próx. {clock}"
        if self.mode == "Por evento":
            return f"{self.event_type}" if self.event_type else "evento"
        return "—"

    def short_label(self) -> str:
        """Nombre corto para mostrar en listas."""
        if self.mode == "Diario":
            return f"Diario · {self.time}"
        if self.mode == "Semanal":
            ds = ", ".join(d.capitalize() for d in self.days) or "—"
            return f"Semanal · {ds} · {self.time}"
        if self.mode == "Cada X minutos":
            if self._parse_anchor() is not None:
                return f"Cada {self.interval_minutes} min · desde {self.anchor_time}"
            return f"Cada {self.interval_minutes} min"
        if self.mode == "Por evento":
            return f"Evento: {self.event_type or '—'}"
        return self.mode


# ═══════════════════════════════════════════════════════════════════════
# ScheduleEntry — agrupa disparadores de una misión
# ═══════════════════════════════════════════════════════════════════════
class ScheduleEntry:
    """La programación de una misión. Puede tener MÚLTIPLES disparadores."""

    def __init__(self, mission_id: str, mission_name: str, data: dict):
        self.mission_id = mission_id
        self.mission_name = mission_name
        self.enabled = bool(data.get("enabled", True))

        triggers_raw = data.get("triggers")
        if isinstance(triggers_raw, list) and triggers_raw:
            self.triggers: List[TriggerConfig] = [TriggerConfig(t) for t in triggers_raw]
        else:
            # Formato antiguo: los campos viven a primer nivel en `data`.
            # Migramos a un único trigger.
            self.triggers = [TriggerConfig(data)]

    # ── Backward-compat: propiedades "mode" / "time" / ... leen el 1er trigger
    @property
    def mode(self) -> str:
        return self.triggers[0].mode if self.triggers else "Bajo demanda (manual)"

    def to_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "mission_name": self.mission_name,
            "enabled": bool(self.enabled),
            "triggers": [t.to_dict() for t in self.triggers],
        }

    def any_active_trigger(self) -> bool:
        """True si hay al menos un trigger no-manual."""
        return any(t.mode != "Bajo demanda (manual)" for t in self.triggers)

    def is_due(self) -> Optional[TriggerConfig]:
        """Devuelve el PRIMER trigger due, o None si ninguno."""
        if not self.enabled:
            return None
        for t in self.triggers:
            if t.is_due():
                return t
        return None

    def seconds_until_next(self) -> Optional[float]:
        """Segundos hasta el primer trigger que dispare (None si no aplica)."""
        if not self.enabled:
            return None
        active = [t for t in self.triggers if t.mode != "Bajo demanda (manual)"]
        if not active:
            return None
        candidates = []
        for t in active:
            s = t.seconds_until_next()
            if s is not None:
                candidates.append(s)
        if not candidates:
            return None
        return min(candidates)

    def next_run_str(self) -> str:
        """Descripción resumida de la próxima ejecución (la más próxima)."""
        if not self.enabled:
            return "Desactivada"
        active = [t for t in self.triggers if t.mode != "Bajo demanda (manual)"]
        if not active:
            return "Manual"
        if len(active) == 1:
            return active[0].next_run_str()
        # Varios triggers: tomamos el más cercano y añadimos resumen.
        with_secs = [(t.seconds_until_next() or float("inf"), t) for t in active]
        with_secs.sort(key=lambda x: x[0])
        soonest = with_secs[0][1]
        return soonest.next_run_str()

    def triggers_summary(self) -> str:
        """Labels cortas separadas por comas — útil para tooltips/listas."""
        labels = [t.short_label() for t in self.triggers
                  if t.mode != "Bajo demanda (manual)"]
        return " + ".join(labels) if labels else "Manual"


# ═══════════════════════════════════════════════════════════════════════
# NevlanScheduler — singleton background
# ═══════════════════════════════════════════════════════════════════════
class NevlanScheduler:
    """Scheduler background que verifica y ejecuta automatizaciones programadas."""

    _instance = None

    def __init__(self):
        self._lock = threading.Lock()
        self._schedules: Dict[str, ScheduleEntry] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._execution_log: List[dict] = []
        # Players actualmente ejecutándose, indexados por mission_id. Se
        # usan para poder CANCELAR una ejecución programada cuando el
        # usuario pulsa "Desactivar" en la UI. Protegido por `_lock`.
        self._running_players: Dict[str, "object"] = {}
        # Callback opcional que la UI registra para saber cuándo arranca
        # una ejecución programada y mostrar el panel flotante de control
        # (pausa / reanudar / cancelar). Firma:
        #   cb(mission_id, mission_name, total_steps, player)
        # Se invoca desde el hilo background del scheduler, así que la UI
        # es responsable de marshalar al hilo principal (vía señal Qt).
        self._on_execution_start_cb: Optional[Callable] = None
        self._load()

    def set_execution_start_callback(self, cb: Optional[Callable]) -> None:
        """Registra/limpia el callback de inicio de ejecución."""
        self._on_execution_start_cb = cb

    @classmethod
    def get_instance(cls) -> 'NevlanScheduler':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load(self):
        """Carga schedules desde disco, migrando formato viejo si hace falta."""
        try:
            if SCHEDULES_FILE.exists():
                with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry_data in data.get("schedules", []):
                    entry = ScheduleEntry(
                        entry_data["mission_id"],
                        entry_data.get("mission_name", ""),
                        entry_data,
                    )
                    self._schedules[entry.mission_id] = entry
                self._execution_log = data.get("execution_log", [])
                log.info(f"Scheduler: {len(self._schedules)} programaciones cargadas")
        except Exception as e:
            log.error(f"Scheduler: error cargando schedules: {e}")

    def _save(self):
        try:
            SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "schedules": [s.to_dict() for s in self._schedules.values()],
                "execution_log": self._execution_log[-100:],
            }
            with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Scheduler: error guardando: {e}")

    # ── API ────────────────────────────────────────────────────────────
    def set_schedule(self, mission_id: str, mission_name: str, config: dict):
        """Establece/actualiza la programación.

        `config` acepta:
        - Formato nuevo: {"enabled": bool, "triggers": [ {...}, {...} ]}
        - Formato viejo (un trigger): {"mode": ..., "time": ..., ...}
        """
        with self._lock:
            if "enabled" not in config:
                config["enabled"] = True
            entry = ScheduleEntry(mission_id, mission_name, config)
            self._schedules[mission_id] = entry
            self._save()
            log.info(
                f"Scheduler: '{mission_name}' → "
                f"{len(entry.triggers)} disparador(es), "
                f"{'ACTIVA' if entry.enabled else 'INACTIVA'}"
            )

    def set_enabled(self, mission_id: str, enabled: bool) -> bool:
        with self._lock:
            entry = self._schedules.get(mission_id)
            if not entry:
                return False
            entry.enabled = bool(enabled)
            self._save()
            log.info(
                f"Scheduler: '{entry.mission_name}' → "
                f"{'ACTIVA' if entry.enabled else 'INACTIVA'}"
            )
            new_state = entry.enabled
        # Si se desactiva y hay una ejecución en marcha de esta misión,
        # la detenemos (fuera del lock para no bloquear el player).
        if not new_state:
            self.request_stop_mission(mission_id)
        return new_state

    def toggle_enabled(self, mission_id: str) -> bool:
        with self._lock:
            entry = self._schedules.get(mission_id)
            if not entry:
                return False
            entry.enabled = not entry.enabled
            self._save()
            log.info(
                f"Scheduler: '{entry.mission_name}' → "
                f"{'ACTIVA' if entry.enabled else 'INACTIVA'}"
            )
            new_state = entry.enabled
        if not new_state:
            self.request_stop_mission(mission_id)
        return new_state

    def request_stop_mission(self, mission_id: str) -> bool:
        """Pide cancelar una ejecución programada actualmente en curso para
        `mission_id`. Devuelve True si había algo que parar.

        No borra el registro; el propio `_execute` lo limpia al terminar.
        """
        with self._lock:
            player = self._running_players.get(mission_id)
        if player is None:
            return False
        try:
            log.info(f"Scheduler: pidiendo stop a misión '{mission_id}'")
            player.request_stop()
            return True
        except Exception as e:
            log.error(f"Scheduler: request_stop falló para '{mission_id}': {e}")
            return False

    def remove_schedule(self, mission_id: str) -> bool:
        """Elimina por completo la programación. Devuelve True si había."""
        with self._lock:
            if mission_id in self._schedules:
                del self._schedules[mission_id]
                self._save()
                log.info(f"Scheduler: programación eliminada para {mission_id}")
                return True
            return False

    def update_mission_name(self, mission_id: str, new_name: str) -> bool:
        """Actualiza el nombre de la misión en el schedule cuando el usuario
        la renombra. Devuelve True si se aplicó."""
        with self._lock:
            entry = self._schedules.get(mission_id)
            if not entry:
                return False
            if entry.mission_name != new_name:
                entry.mission_name = new_name
                self._save()
                return True
            return False

    def get_schedule(self, mission_id: str) -> Optional[ScheduleEntry]:
        return self._schedules.get(mission_id)

    def get_all_schedules(self) -> List[ScheduleEntry]:
        return list(self._schedules.values())

    def get_next_execution(self) -> Optional[str]:
        """Devuelve la próxima ejecución global (compacto para el topbar)."""
        active = [s for s in self._schedules.values()
                  if s.enabled and s.any_active_trigger()]
        if not active:
            return None
        s = active[0]
        return f"{s.mission_name}: {s.next_run_str()}"

    def get_upcoming(self, limit: int = 20) -> List[Dict]:
        """Lista para el panel 'Próximas ejecuciones'.

        Cada item incluye:
          - mission_id, name, trigger, next_run, enabled, n_triggers (existentes)
          - since_human: string legible con el inicio del conteo (p. ej.
            "desde 18:30" o "desde hace 2m 15s"). Solo se completa cuando
            tiene sentido (p. ej. 'Cada X minutos' con `last_run`).
          - next_run_at: hora exacta (HH:MM:SS) de la próxima ejecución si
            se puede calcular. None si no aplica.
        """
        now = datetime.now()
        items: List[Dict] = []
        for s in self._schedules.values():
            if not s.any_active_trigger():
                continue

            # Trigger más próximo (el mismo que usa next_run_str)
            active = [t for t in s.triggers if t.mode != "Bajo demanda (manual)"]
            soonest: Optional[TriggerConfig] = None
            if active:
                with_secs = [(t.seconds_until_next() or float("inf"), t) for t in active]
                with_secs.sort(key=lambda x: x[0])
                soonest = with_secs[0][1]

            since_human: Optional[str] = None
            next_run_at: Optional[str] = None
            if soonest is not None:
                if soonest.mode == "Cada X minutos":
                    # Prioridad: anchor_time (configurado por el usuario).
                    # Así la UI muestra la hora base explícita.
                    if soonest._parse_anchor() is not None:
                        since_human = f"desde las {soonest.anchor_time}"
                    elif soonest.last_run is not None:
                        elapsed = (now - soonest.last_run).total_seconds()
                        if 0 <= elapsed < 60:
                            since_human = f"desde hace {int(elapsed)}s"
                        elif elapsed < 3600:
                            since_human = f"desde las {soonest.last_run.strftime('%H:%M:%S')}"
                        else:
                            since_human = f"desde las {soonest.last_run.strftime('%H:%M')}"
                nxt = soonest.next_run_datetime()
                if nxt is not None:
                    # Si la próxima ejecución es hoy, basta con HH:MM:SS;
                    # si es mañana o después, añadimos la fecha corta.
                    if nxt.date() == now.date():
                        next_run_at = nxt.strftime("%H:%M:%S")
                    else:
                        next_run_at = nxt.strftime("%d/%m %H:%M")

            items.append({
                "mission_id": s.mission_id,
                "name": s.mission_name,
                "trigger": s.triggers_summary(),
                "next_run": s.next_run_str(),
                "next_run_at": next_run_at,
                "since_human": since_human,
                "enabled": bool(s.enabled),
                "n_triggers": sum(
                    1 for t in s.triggers if t.mode != "Bajo demanda (manual)"
                ),
            })
        return items[:limit]

    def get_execution_log(self) -> List[dict]:
        return list(reversed(self._execution_log[-20:]))

    # ── Loop de background ────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        # Al abrir la app no queremos que una misión "Cada X minutos"
        # dispare inmediatamente por haber estado la app cerrada más que
        # su intervalo: eso robaba el foco al usuario justo al arrancar.
        # Posponemos ese trigger hasta el siguiente intervalo natural.
        self._reset_stale_timers_on_startup()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="NevlanScheduler")
        self._thread.start()
        log.info("Scheduler: iniciado")

    def stop(self):
        self._running = False
        log.info("Scheduler: detenido")

    def _reset_stale_timers_on_startup(self):
        """Evita el 'catch-up' al abrir la app.

        Para triggers "Cada X minutos":
          - si `last_run` es None (nunca ejecutado), lo fijamos a ahora
            para que el próximo disparo ocurra al completarse el intervalo.
          - si `last_run` es viejo (ya vencido), lo reseteamos a ahora
            para no arrastrar ejecuciones perdidas mientras la app estaba
            cerrada.
        """
        now = datetime.now()
        changed = False
        with self._lock:
            for entry in self._schedules.values():
                for t in entry.triggers:
                    if t.mode != "Cada X minutos":
                        continue
                    if t._parse_anchor() is not None:
                        # Anclado al reloj: si hay un slot pasado más
                        # reciente que `last_run` (porque la app estuvo
                        # cerrada uno o varios intervalos), alineamos
                        # `last_run` a ese slot para que no haya catch-up
                        # inmediato. El siguiente disparo será el slot
                        # FUTURO siguiente.
                        slot = t._most_recent_slot(now)
                        if slot is not None and (
                            t.last_run is None or t.last_run < slot
                        ):
                            t.last_run = slot
                            changed = True
                        continue
                    # Sin anchor: lógica basada en tiempo transcurrido.
                    if t.last_run is None:
                        t.last_run = now
                        changed = True
                    else:
                        elapsed_min = (now - t.last_run).total_seconds() / 60
                        if elapsed_min >= t.interval_minutes:
                            t.last_run = now
                            changed = True
            if changed:
                self._save()

    def _loop(self):
        # Pequeña pausa inicial para que la UI termine de pintarse antes
        # de la primera evaluación. El gate real anti-catch-up vive en
        # ``runtime_locks.can_scheduler_run_now`` y bloquea cualquier
        # disparo durante los primeros ``app_warmup_s`` (30s) tras el
        # arranque del proceso. Eso evita que una misión "Cada X minutos"
        # dispare al instante de abrir la app y le robe el foco al
        # usuario que probablemente ya empezó a grabar otra cosa.
        time.sleep(3)
        while self._running:
            try:
                self._check_and_execute()
            except Exception as e:
                log.error(f"Scheduler loop error: {e}")
            time.sleep(30)

    def _check_and_execute(self):
        # Tomamos una foto de los triggers vencidos bajo lock y luego lo
        # liberamos antes de ejecutar. Si mantuviéramos el lock durante la
        # ejecución (que puede tardar minutos con clicks, esperas, etc.), la
        # UI se congelaría al intentar desactivar o editar la programación,
        # porque set_enabled / toggle_enabled / remove_schedule usan el mismo
        # lock.

        # ── Candados globales ─────────────────────────────────────
        # Bloqueamos cualquier disparo si el usuario está grabando o
        # si ya hay un player corriendo. NO marcamos last_run: queremos
        # que el trigger se vuelva a evaluar en el siguiente tick una
        # vez termine la grabación / el player.
        try:
            from app.services.missions.runtime_locks import (
                locks as _runtime_locks,
                publish_blocked_event,
            )
            ok, reason = _runtime_locks.can_scheduler_run_now()
        except Exception:
            ok, reason = True, "ok"

        if not ok:
            # Logueo de diagnóstico — esto explica el bug "Chrome se abre
            # solo mientras grabo": el scheduler intentaba arrancar y
            # ahora lo posponemos en lugar de pisarle al usuario.
            try:
                # Solo logueamos si hay algo realmente vencido, para no
                # ensuciar la consola cada 30s.
                with self._lock:
                    has_due = any(
                        e.is_due() is not None for e in self._schedules.values()
                    )
                if has_due:
                    msg_map = {
                        "warmup": "Scheduler: en periodo de calentamiento de la app, posponiendo triggers.",
                        "recording": "Scheduler: grabación activa, posponiendo triggers programados.",
                        "player_busy": "Scheduler: hay otra ejecución en curso, posponiendo triggers.",
                    }
                    log.info(msg_map.get(reason, f"Scheduler: pospuesto ({reason})"))
                    try:
                        publish_blocked_event(
                            kind="scheduler",
                            reason=reason,
                            message=msg_map.get(reason, reason),
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            return

        due_pairs: List = []
        with self._lock:
            for entry in self._schedules.values():
                due_trigger = entry.is_due()
                if due_trigger is not None:
                    # Marcamos last_run de inmediato para no re-disparar en
                    # la siguiente iteración mientras aún se ejecuta.
                    due_trigger.last_run = datetime.now()
                    due_pairs.append((entry, due_trigger))
            if due_pairs:
                self._save()

        for entry, trigger in due_pairs:
            self._execute(entry, trigger)

    def _execute(self, entry: ScheduleEntry, trigger: TriggerConfig):
        """Ejecuta una misión programada. NO debe llamarse con el lock
        tomado: `replay_mission` puede tardar minutos y la UI se congelaría.
        Las escrituras a estructuras compartidas se hacen con lock breve.
        """
        log.info(
            f"Scheduler: ejecutando '{entry.mission_name}' "
            f"(trigger: {trigger.short_label()})"
        )

        log_entry = {
            "mission_id": entry.mission_id,
            "mission_name": entry.mission_name,
            "mode": trigger.mode,
            "started_at": datetime.now().isoformat(),
            "status": "running",
        }

        player = None
        try:
            from app.services.missions.store import mission_store
            mission = mission_store.load(entry.mission_id)
            if not mission:
                log_entry["status"] = "error"
                log_entry["error"] = "Automatización no encontrada"
                with self._lock:
                    self._execution_log.append(log_entry)
                    self._save()
                return

            from app.services.missions.player import MissionPlayer
            from app.services.missions.execution_tracker import tracker

            exec_idx = tracker.log_start(
                mission.id,
                mission.name,
                len(mission.compiled_execution_graph),
                "scheduled",
            )

            # Usamos MissionPlayer (en lugar de replay_mission) para poder
            # CANCELAR la ejecución desde fuera (p. ej. al desactivar la
            # programación) y permitir que la UI muestre su panel flotante
            # de control.
            player = MissionPlayer(mission)
            # Marcamos origin="scheduler" para que el log y los eventos
            # ``mission.runtime_blocked`` puedan distinguir disparos
            # programados de manuales.
            try:
                player._replay_origin = "scheduler"
            except Exception:
                pass
            player.start_execution()

            # Registramos el player para que `request_stop_mission` pueda
            # alcanzarlo.
            with self._lock:
                self._running_players[entry.mission_id] = player

            # Notificamos a la UI para que muestre el panel flotante de
            # ejecución. Este callback se invoca desde el hilo background;
            # la UI debe marshalar al hilo principal con un QSignal.
            cb = self._on_execution_start_cb
            if cb is not None:
                try:
                    cb(
                        entry.mission_id,
                        entry.mission_name,
                        len(mission.compiled_execution_graph),
                        player,
                    )
                except Exception as e:
                    log.debug(f"Scheduler: on_execution_start_cb falló: {e}")

            execution = player.replay()
            log_entry["status"] = execution.status.value
            log_entry["completed_at"] = datetime.now().isoformat()
            if exec_idx >= 0:
                err = execution.error_details or ""
                tracker.log_complete(
                    exec_idx,
                    execution.status.value,
                    steps_executed=execution.steps_executed,
                    error=str(err),
                )
            log.info(
                f"Scheduler: '{entry.mission_name}' completada — "
                f"{execution.status.value}"
            )
        except Exception as e:
            log_entry["status"] = "error"
            log_entry["error"] = str(e)
            log.error(f"Scheduler: error ejecutando '{entry.mission_name}': {e}")

        # Desregistrar el player al terminar (con éxito o error).
        with self._lock:
            if self._running_players.get(entry.mission_id) is player:
                self._running_players.pop(entry.mission_id, None)
            self._execution_log.append(log_entry)
            self._save()


# Singleton
scheduler = NevlanScheduler.get_instance()
