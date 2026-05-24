"""
ArthurOS Services - Mission Player (Ultra RPA)
----------------------------------------------
Reproduce misiones evaluando el CompiledExecutionGraph.

Optimizaciones clave:
- Backoff exponencial agresivo (default 150ms, cap 900ms) en vez de sleeps fijos de 1s.
- UIA priorizado por ventana activa (no searchDepth global en toda la pantalla).
- Vision/OCR acotados al bounding-box de la ventana registrada al grabar.
- Caché por ejecución: reusa la última ventana resuelta si el siguiente paso apunta a lo mismo.
- Instrumentación: tiempos por paso, estrategia y resolver.
- Soporte expandido de teclado (hotkey / combo / secuencia / repeat).
"""
from __future__ import annotations

import time
import sys
import threading
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone

from app.contracts.mission import (
    Mission, MissionExecution, ReplayStatus, CompiledStep,
    TargetResolutionStrategy, ActionStrategy, FallbackStrategy,
    ValidationStrategy,
)
from app.core.logger import log
from app.runtime.bus import bus
from app.contracts.events import SystemEvent
from app.services.missions.perf import PerfTracker, measure
from app.services.missions.vibe_parser import normalize_keys, _normalize_token
from app.services.missions.target_resolver import (
    TargetResolver, TargetResolutionResult,
)

try:
    import uiautomation as auto
    HAS_UIA = True
except ImportError:
    HAS_UIA = False


# ─────────────────────────────────────────────────────────────────
# Win32 safety helpers — evitan clickar sobre Nevlan
# ─────────────────────────────────────────────────────────────────
try:
    import ctypes
    from ctypes import wintypes
    _user32 = ctypes.windll.user32
    _GA_ROOT = 2
    _HAS_W32 = True
except Exception:
    _user32 = None
    _GA_ROOT = 2
    _HAS_W32 = False


def _hwnd_at_point(x: int, y: int) -> int:
    """Devuelve el hwnd raíz de la ventana bajo (x, y), o 0."""
    if not _HAS_W32:
        return 0
    try:
        pt = wintypes.POINT(int(x), int(y))
        h = _user32.WindowFromPoint(pt)
        if not h:
            return 0
        root = _user32.GetAncestor(h, _GA_ROOT) or h
        return int(root)
    except Exception:
        return 0


def _pid_for_hwnd(hwnd: int) -> int:
    if not _HAS_W32 or not hwnd:
        return 0
    try:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _is_point_owned_by_us(x: int, y: int) -> bool:
    """True si el punto cae sobre una ventana del proceso actual (Nevlan)."""
    if not _HAS_W32:
        return False
    pid = _pid_for_hwnd(_hwnd_at_point(x, y))
    own = _current_pid()
    return pid != 0 and own != -1 and pid == own


# Parámetros de ejecución — FIDELIDAD > velocidad.
# Regla: más vale ejecutar en 200ms y acertar, que en 50ms y fallar.
# Estos valores son los mínimos seguros observados en apps reales (Chrome,
# Word, Excel, Teams, formularios web con JS).
_FAST_TIMEOUT_S = 0.15          # timeout rápido para Control.Exists
_UIA_SEARCH_DEPTH = 16          # profundidad razonable (vs. 0xFFFFFFFF) → mucho más rápida
_TYPE_FOCUS_PAUSE = 0.05        # pausa tras focus antes de tipear (apps web cargan listeners)
_TYPE_INTERVAL = 0.012          # intervalo/char: 0 pierde teclas en web con JS debounced
_SEQ_DEFAULT_INTERVAL = 0.07    # entre teclas en secuencia (da tiempo al redibujado de foco)
_REPEAT_DEFAULT_INTERVAL = 0.07
_WINDOW_ACTIVATE_PAUSE = 0.15   # tras activar ventana: algunas apps tardan en tomar foco
_POST_CLICK_SETTLE_S = 0.04     # breve respiro tras click para que UI reaccione


def _clamp_backoff(ms: int, attempt: int) -> float:
    """Backoff exponencial acotado. Primer reintento ~120ms, tope 600ms."""
    base = max(50, min(int(ms or 120), 250))  # misiones antiguas traen 1000ms; lo recortamos.
    d = base * (2 ** (attempt - 1))
    return min(d, 600) / 1000.0


# Acciones que NO requieren resolver un target UI (ni UIA ni visión).
_NO_TARGET_ACTIONS = {
    ActionStrategy.SEND_HOTKEY,
    ActionStrategy.WAIT_FOR_STATE,
    ActionStrategy.AGENT_PROMPT,
    ActionStrategy.SCROLL,
    ActionStrategy.DRAG_DROP,
    ActionStrategy.LAUNCH_APP,
}

# Nombres UIA genéricos — si el recorder solo capturó uno de estos como Name,
# NO lo usamos para match por nombre: coincidirían con miles de contenedores
# (incluyendo widgets de la propia Nevlan) y podríamos clickar en la app
# equivocada. Tratamos como "sin nombre" y caemos a coords/visión.
_GENERIC_UIA_NAMES = {
    "groupcontrol", "panecontrol", "customcontrol", "documentcontrol",
    "windowcontrol", "toolbarcontrol", "statusbarcontrol", "splitbuttoncontrol",
    "group", "pane", "custom", "document", "window", "toolbar", "statusbar",
    "unknown", "", "none",
}

# Tiempo máximo para esperar que aparezca la ventana objetivo antes del 1er
# paso contra ella (caso típico: "abrir Chrome" → Chrome tarda 1-3s en cargar).
_WINDOW_READY_TIMEOUT_S = 3.0
_WINDOW_READY_POLL_S = 0.15


def _is_generic_name(name: Optional[str]) -> bool:
    if not name:
        return True
    return name.strip().lower() in _GENERIC_UIA_NAMES


def _current_pid() -> int:
    try:
        import os
        return int(os.getpid())
    except Exception:
        return -1


class _ExecutionContext:
    """Caché por ejecución: ventana UIA activa, último elemento resuelto."""

    def __init__(self):
        self.window_ctrl = None          # último WindowControl UIA
        self.window_title_hint: str = ""
        self.window_bbox: Optional[Dict[str, int]] = None
        self.last_activated_title: str = ""

    def clear(self):
        self.window_ctrl = None
        self.window_title_hint = ""
        self.window_bbox = None
        self.last_activated_title = ""


class MissionPlayer:
    def __init__(self, mission: Mission):
        self.mission = mission
        self.execution: Optional[MissionExecution] = None
        self._lock = threading.Lock()
        self._running = False
        self._stop_requested = False
        # Pausa cooperativa: el bucle revisa este flag al inicio de cada paso.
        # Implementado con threading.Event para poder bloquear sin spinning.
        self._pause_event = threading.Event()
        self._pause_event.set()  # set = NO pausado (run libre)
        self._paused = False
        self.perf = PerfTracker()
        self._ctx = _ExecutionContext()
        self._recovery_cycles_for_step: Dict[str, int] = {}
        self._next_success_is_recovery = False
        # Recuperación interactiva: el hilo de replay espera comandos desde la UI/tests.
        self._recovery_event = threading.Event()
        self._recovery_cmd_result: Optional[str] = None
        self._pending_recovery_step_idx: int = -1
        self._recovery_debug_sequence: Optional[List[str]] = None  # sólo tests: cmds FIFO
        # TargetResolverV2 = único motor de resolución (Phase 5 PRD).
        # Stateless por misión: cachea ventana/página en memoria entre
        # pasos; descartamos al terminar la ejecución.
        self._resolver = TargetResolver()

    def start_execution(self) -> MissionExecution:
        with self._lock:
            self.execution = MissionExecution(
                mission_id=self.mission.id,
                mission_version=self.mission.version,
                status=ReplayStatus.IN_PROGRESS,
                executed_by="agent",
            )
            self._running = True
            self._stop_requested = False
            self._paused = False
            self._pause_event.set()
            self._recovery_cycles_for_step = {}
            self._next_success_is_recovery = False
            return self.execution

    def request_stop(self) -> None:
        self._stop_requested = True
        self._recovery_event.set()
        # Si está pausado, despertar para que detecte la cancelación.
        self._pause_event.set()
        self._paused = False
        log.info(f"Stop requested for mission {self.mission.name}")
        try:
            self._publish_event(
                "mission.replay_cancelled",
                {"mission_name": self.mission.name},
            )
        except Exception:
            pass

    def request_pause(self) -> None:
        """Pausa cooperativa: el bucle se bloqueará antes del siguiente paso."""
        if self._paused:
            return
        self._paused = True
        self._pause_event.clear()
        log.info(f"Pause requested for mission {self.mission.name}")
        try:
            self._publish_event(
                "mission.replay_paused",
                {"mission_name": self.mission.name},
            )
        except Exception:
            pass

    def request_resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        self._pause_event.set()
        log.info(f"Resume requested for mission {self.mission.name}")
        try:
            self._publish_event(
                "mission.replay_resumed",
                {"mission_name": self.mission.name},
            )
        except Exception:
            pass

    def is_running(self) -> bool:
        return self._running

    def is_paused(self) -> bool:
        return self._paused

    def submit_recovery_command(self, cmd: str) -> None:
        """Continúa un replay bloqueado en fallo (`retry`/`skip`/`manual_continue`/`cancel`)."""
        with self._lock:
            self._recovery_cmd_result = (cmd or "cancel").strip().lower()
            self._recovery_event.set()

    def retry_current_step(self) -> None:
        """Reintenta el paso donde el player está esperando recuperación."""
        self.submit_recovery_command("retry")

    def continue_after_manual_intervention(self, step_id: Optional[str] = None) -> None:
        """El usuario terminó manualmente lo que el robot no podía; continuar en siguiente paso."""
        self.submit_recovery_command("manual_continue")

    def enqueue_recovery_commands_for_tests(self, *cmds: str) -> None:
        """FIFO consumido sólo cuando el player espera comando (pytest)."""
        self._recovery_debug_sequence = [c.strip().lower() for c in cmds]

    def retry_from_step(self, step_id: str, variables: Optional[Dict[str, str]] = None) -> MissionExecution:
        """Nueva corrida desde el primer nodo cuyo id coincide."""
        graph = getattr(self.mission, "compiled_execution_graph", []) or []
        for i, st in enumerate(graph):
            if st.id == step_id:
                return self.replay(variables=variables or {}, start_step_idx=i, auto_confirm=False)
        raise ValueError(f"step id no encontrado: {step_id!r}")

    def resume_from_step(self, step_index: int, variables: Optional[Dict[str, str]] = None) -> MissionExecution:
        """Nueva corrida desde un índice de grafo compilado."""
        return self.replay(variables=variables or {}, start_step_idx=step_index, auto_confirm=False)

    # --- Ciclo principal -------------------------------------------------

    def replay(
        self,
        auto_confirm: bool = False,
        variables: Dict[str, str] = None,
        start_step_idx: int = 0,
    ) -> MissionExecution:
        if not self.execution:
            self.start_execution()

        execution = self.execution
        variables = variables or {}

        # ── Candado global: solo UN player + nunca durante grabación ──
        # Importamos aquí para evitar ciclos en inicialización temprana
        # (player.py se importa desde el scheduler antes de que el bus
        # esté listo en algunos tests).
        from app.services.missions.runtime_locks import (
            locks as _runtime_locks,
            publish_blocked_event,
        )

        origin = getattr(self, "_replay_origin", "manual") or "manual"
        acquired = _runtime_locks.acquire_player(
            mission_id=str(getattr(self.mission, "id", "") or ""),
            origin=origin,
        )
        if not acquired:
            snap = _runtime_locks.snapshot()
            if snap.recorder_active:
                msg = (
                    "No se puede reproducir: el grabador está activo. "
                    "Detén la grabación antes de ejecutar una automatización."
                )
                reason = "recording"
            else:
                msg = (
                    "Ya hay otra automatización en ejecución. "
                    "Espera a que termine antes de iniciar otra."
                )
                reason = "player_busy"
            execution.status = ReplayStatus.FAILED
            execution.error_details = msg
            execution.completed_at = datetime.now(timezone.utc)
            self._running = False
            log.warning(f"MissionPlayer.replay bloqueado: {reason} — {msg}")
            publish_blocked_event(kind="player", reason=reason, message=msg)
            try:
                self._publish_finished_summary(execution)
            except Exception:
                pass
            return execution

        try:
            self._replay_impl(variables, max(0, int(start_step_idx)), auto_confirm=auto_confirm)
        except Exception as e:
            execution.status = ReplayStatus.FAILED
            execution.error_details = f"Error no controlado: {e}"
            log.exception(e)
        finally:
            self._running = False
            execution.completed_at = datetime.now(timezone.utc)
            try:
                self._publish_finished_summary(execution)
            except Exception:
                pass
            try:
                _runtime_locks.release_player(
                    mission_id=str(getattr(self.mission, "id", "") or ""),
                )
            except Exception:
                pass

        return execution

    def _replay_impl(
        self,
        variables: Dict[str, str],
        start_step_idx: int,
        *,
        auto_confirm: bool,
    ) -> None:
        """Cuerpo de reproducción: bucle principal + recuperación interactiva en fallos."""
        execution = self.execution
        assert execution is not None

        self._publish_event("mission.replay_start", {"mission_name": self.mission.name})

        graph = self.mission.compiled_execution_graph
        total_steps = len(graph)
        execution.understanding = (
            f"Ejecutar automatización '{self.mission.name}' ({total_steps} pasos)."
        )
        execution.plan = "Resolver target (UIA/Vision) y ejecutar acción por nodo."

        mission_t0 = time.perf_counter()
        self._next_success_is_recovery = False

        step_idx = start_step_idx
        while step_idx < len(graph):
            while not self._pause_event.is_set():
                if self._stop_requested:
                    break
                self._pause_event.wait(timeout=0.2)
            if self._stop_requested:
                execution.status = ReplayStatus.CANCELLED
                execution.error_details = execution.error_details or "Cancelado por usuario"
                break

            step = graph[step_idx]
            strat_val = getattr(
                step.action_strategy, "value", str(step.action_strategy),
            )
            interp_desc = ""
            try:
                if step_idx < len(self.mission.interpreted_steps):
                    interp_desc = (
                        self.mission.interpreted_steps[step_idx].description or ""
                    )
            except Exception:
                pass
            group_title = ""
            group_id_ev = ""
            try:
                for ag in getattr(self.mission, "action_groups", []) or []:
                    if step.id in (ag.step_ids or []):
                        group_title = ag.group_title or ""
                        group_id_ev = getattr(ag, "id", "") or ""
                        break
            except Exception:
                pass

            try:
                from app.services.missions.execution_ui import step_phase_label

                phase_hint = step_phase_label(str(strat_val))
            except Exception:
                phase_hint = "Ejecutando…"

            self._publish_event(
                "mission.step_start",
                {
                    "step_index": step_idx,
                    "total_steps": total_steps,
                    "strategy": strat_val,
                    "step_description": (interp_desc or "")[:500],
                    "group_title": group_title or "",
                    "phase_hint": phase_hint,
                    "execution_id": execution.id,
                },
            )

            timing = self.perf.start_step(step_idx, strat_val)
            t_step0 = time.perf_counter()
            t_started = datetime.now(timezone.utc)
            success = False
            error_msg = ""

            max_retries = max(1, min(int(step.retry_policy.max_retries or 1), 3))
            backoff_base = step.retry_policy.backoff_ms

            for attempt in range(1, max_retries + 1):
                timing.attempts = attempt
                try:
                    self._execute_compiled_step(step, variables, timing)
                    success = True
                    break
                except Exception as e:
                    error_msg = str(e)
                    log.warning(f"Step {step_idx} intento {attempt} falló: {e}")
                    try:
                        from app.services.missions.repair import (
                            record_method_outcome,
                            record_bundle_success,
                        )

                        method_used = timing.resolver if getattr(timing, "resolver", None) else "unknown"
                        record_method_outcome(step, str(method_used), False)
                        bundle_used = getattr(timing, "bundle_used", "primary")
                        record_bundle_success(step, str(bundle_used), False)
                    except Exception:
                        pass
                    if attempt < max_retries:
                        time.sleep(_clamp_backoff(backoff_base, attempt))

            timing.total_ms = (time.perf_counter() - t_step0) * 1000.0

            try:
                from app.services.missions.runtime_quality import merge_runtime_feedback

                pm = getattr(timing, "premium_click_meta", None)
                fb_p = ""
                if isinstance(pm, dict):
                    fb_p = str(pm.get("fallback_used") or "")
                res_s = str(getattr(timing, "resolver", None) or "")
                pm_arg = pm if isinstance(pm, dict) else None
                if success:
                    merge_runtime_feedback(
                        step,
                        True,
                        resolver=res_s,
                        premium_fallback=fb_p,
                        premium_meta=pm_arg,
                        elapsed_ms=float(timing.total_ms),
                    )
                else:
                    merge_runtime_feedback(
                        step,
                        False,
                        resolver=res_s,
                        premium_fallback=fb_p,
                        premium_meta=pm_arg,
                        error_snip=(error_msg or "")[:260],
                        elapsed_ms=float(timing.total_ms),
                    )
            except Exception:
                pass

            t_done = datetime.now(timezone.utc)

            # ── Paso OK ───────────────────────────────────────────────
            if success:
                execution.steps_executed += 1
                log_status = (
                    "recovered"
                    if getattr(self, "_next_success_is_recovery", False)
                    else "success"
                )
                if getattr(self, "_next_success_is_recovery", False):
                    execution.steps_recovered_retry += 1
                    self._next_success_is_recovery = False

                try:
                    self._append_step_run_log(
                        execution,
                        step=step,
                        step_idx=step_idx,
                        started_at=t_started,
                        ended_at=t_done,
                        elapsed_ms=float(timing.total_ms),
                        status=log_status,
                        resolver=str(getattr(timing, "resolver", "") or ""),
                        error_message="",
                        group_id_ev=group_id_ev,
                        strategy_attempts=timing.attempts,
                    )
                except Exception:
                    pass

                payload_ok: Dict[str, Any] = {
                    "step_index": step_idx,
                    "ms": round(timing.total_ms, 1),
                    "resolver": timing.resolver,
                    "step_recovered": log_status == "recovered",
                }
                try:
                    from app.services.missions.execution_ui import (
                        friendly_resolver_summary,
                    )

                    pm_ok = getattr(timing, "premium_click_meta", None)
                    pm_ok = pm_ok if isinstance(pm_ok, dict) else {}
                    res_ok = str(getattr(timing, "resolver", "") or "")
                    nl, xl = friendly_resolver_summary(res_ok, pm_ok or None)
                    payload_ok["status_line_normal"] = nl
                    payload_ok["status_line_expert"] = xl
                    payload_ok["strategy_used"] = res_ok
                    payload_ok["elapsed_ms_step"] = round(float(timing.total_ms), 2)
                    if pm_ok:
                        payload_ok["premium_click_meta"] = pm_ok
                    if log_status == "recovered":
                        payload_ok["status_line_normal"] = (
                            "Paso recuperado · " + str(nl or "reintento correcto")
                        )
                except Exception:
                    pass
                vp = getattr(timing, "validation_passed", None)
                if vp is not None:
                    payload_ok["validation_passed"] = bool(vp)
                    payload_ok["validation_reason"] = (
                        getattr(timing, "validation_reason", "") or ""
                    )
                self._publish_event("mission.step_ok", payload_ok)
                step_idx += 1
                continue

            # ── Fallo: fallback coords compilado ────────────────────────
            if (step.fallback_strategy == FallbackStrategy.USE_COORDS
                    and step.target_context.fallback_coords):
                log.info("Fallback: coords absolutas")
                fb_ok = False
                try:
                    self._execute_action(
                        step.action_strategy,
                        None,
                        step.target_context.fallback_coords,
                        step.action_payload,
                        variables,
                    )
                    fb_ok = True
                except Exception as fe:
                    error_msg += f" | Coords fallback falló: {fe}"
                if fb_ok:
                    execution.steps_executed += 1
                    timing.resolver = "coords_fallback"
                    timing.total_ms = (time.perf_counter() - t_step0) * 1000.0
                    self._publish_event(
                        "mission.step_ok",
                        {
                            "step_index": step_idx,
                            "ms": round(timing.total_ms, 1),
                            "resolver": "coords_fallback",
                            "strategy_used": "coords_fallback",
                        },
                    )
                    try:
                        self._append_step_run_log(
                            execution,
                            step=step,
                            step_idx=step_idx,
                            started_at=t_started,
                            ended_at=datetime.now(timezone.utc),
                            elapsed_ms=float(timing.total_ms),
                            status="success",
                            resolver="coords_fallback",
                            error_message="",
                            fallback_used="coords_fallback",
                            group_id_ev=group_id_ev,
                            strategy_attempts=getattr(timing, "attempts", 1),
                        )
                    except Exception:
                        pass
                    step_idx += 1
                    continue

            if step.fallback_strategy == FallbackStrategy.SKIP_STEP:
                log.info(f"Step {step_idx} saltado por política")
                execution.steps_skipped += 1
                execution.had_warnings = True
                try:
                    self._append_step_run_log(
                        execution,
                        step=step,
                        step_idx=step_idx,
                        started_at=t_started,
                        ended_at=datetime.now(timezone.utc),
                        elapsed_ms=float(timing.total_ms),
                        status="skipped",
                        resolver=str(getattr(timing, "resolver", "") or ""),
                        error_message=(error_msg or "")[:500],
                        user_action="compiled_skip_policy",
                        group_id_ev=group_id_ev,
                        strategy_attempts=getattr(timing, "attempts", 1),
                    )
                except Exception:
                    pass
                step_idx += 1
                continue

            # ── Recuperación dirigida ──────────────────────────────────
            recovery_cycles = getattr(self, "_recovery_cycles_for_step", {}).get(step.id, 0)
            rpm = getattr(step.retry_policy, "recovery_user_max_cycles", None)
            rp_max = int(rpm if rpm is not None else 10)
            retry_allowed_flag = getattr(step.retry_policy, "retry_allowed", True)

            pmf = getattr(timing, "premium_click_meta", None)
            blocked_retry = (
                not bool(retry_allowed_flag)
                or (recovery_cycles >= rp_max)
            )

            try:
                self._publish_event(
                    "mission.step_failed",
                    {
                        "step_index": step_idx,
                        "step_id": step.id,
                        "total_steps": total_steps,
                        "error": (error_msg or "")[:800],
                        "step_description": (interp_desc or "")[:500],
                        "group_title": group_title or "",
                        "strategy_used": getattr(timing, "resolver", None),
                        "premium_click_meta": pmf if isinstance(pmf, dict) else {},
                        "execution_id": execution.id,
                        "recovery_cycles": recovery_cycles,
                        "recovery_max_cycles": rp_max,
                        "retry_blocked": blocked_retry,
                        "blocked_reason": ""
                        if not blocked_retry
                        else (
                            "retry_not_allowed"
                            if not retry_allowed_flag
                            else "max_recovery_cycles"
                        ),
                    },
                )
            except Exception:
                pass

            cmd = ""
            while True:
                cmd = self._wait_for_recovery_blocked(
                    step_idx=step_idx,
                    step=step,
                    error_msg=(error_msg or "")[:900],
                    recovery_cycles=recovery_cycles,
                    recovery_max=rp_max,
                    retry_blocked=blocked_retry,
                    execution_id=execution.id,
                )
                execution.user_recovery_cycles_total += 1

                if cmd == "cancel" or cmd == "":
                    execution.failed_step_id = step.id
                    execution.error_details = f"Paso {step_idx + 1} falló: {error_msg}"
                    execution.status = ReplayStatus.FAILED if not self._stop_requested else ReplayStatus.CANCELLED
                    break

                if cmd == "retry":
                    if blocked_retry:
                        continue
                    self._recovery_cycles_for_step[step.id] = recovery_cycles + 1
                    recovery_cycles += 1
                    blocked_retry = (not retry_allowed_flag) or (recovery_cycles >= rp_max)
                    self._next_success_is_recovery = True
                    time.sleep(
                        min(
                            2.5,
                            (step.retry_policy.backoff_ms or 350) / 1000.0
                            * min(3.0, 1 + recovery_cycles * 0.25),
                        )
                    )
                    break

                if cmd == "skip":
                    execution.steps_skipped += 1
                    execution.had_warnings = True
                    try:
                        self._append_step_run_log(
                            execution,
                            step=step,
                            step_idx=step_idx,
                            started_at=t_started,
                            ended_at=datetime.now(timezone.utc),
                            elapsed_ms=float(timing.total_ms),
                            status="skipped",
                            resolver=str(getattr(timing, "resolver", "") or ""),
                            error_message=(error_msg or "")[:800],
                            user_action="skip",
                            group_id_ev=group_id_ev,
                            strategy_attempts=getattr(timing, "attempts", 1),
                        )
                    except Exception:
                        pass
                    step_idx += 1
                    break

                if cmd in ("manual_continue", "manual", "manual_intervention"):
                    execution.steps_manual += 1
                    execution.had_warnings = True
                    try:
                        self._append_step_run_log(
                            execution,
                            step=step,
                            step_idx=step_idx,
                            started_at=t_started,
                            ended_at=datetime.now(timezone.utc),
                            elapsed_ms=float(timing.total_ms),
                            status="manual_intervention",
                            resolver=str(getattr(timing, "resolver", "") or ""),
                            error_message=(error_msg or "")[:800],
                            user_action="manual_continue",
                            validation_after_manual=False,
                            group_id_ev=group_id_ev,
                            strategy_attempts=getattr(timing, "attempts", 1),
                            manual_intervention=True,
                            user_continued=True,
                        )
                    except Exception:
                        pass
                    step_idx += 1
                    break

                log.warning("Comando recuperación no reconocido: %s — esperando otro comando", cmd)
                continue

            if execution.status in (ReplayStatus.FAILED, ReplayStatus.CANCELLED):
                break
            if cmd in ("skip", "manual_continue", "manual", "manual_intervention"):
                continue

            if cmd == "retry":
                continue

            if execution.status != ReplayStatus.IN_PROGRESS:
                break

        if execution.status == ReplayStatus.IN_PROGRESS:
            recovered_any = execution.steps_skipped > 0 or execution.steps_manual > 0 or (
                getattr(execution, "steps_recovered_retry", 0) or 0
            ) > 0 or execution.had_warnings
            if recovered_any:
                execution.status = ReplayStatus.SUCCESS_WITH_RECOVERY
                execution.execution_summary = (
                    f"Completado con recuperación/aviso ({execution.steps_executed}/{total_steps})."
                )
            else:
                execution.status = ReplayStatus.SUCCESS
                execution.execution_summary = (
                    f"Completado exitosamente ({execution.steps_executed}/{total_steps})."
                )

        mission_total_ms = (time.perf_counter() - mission_t0) * 1000.0
        totals = self.perf.totals()
        busy_ms = (
            totals.get("resolve_total_ms", 0)
            + totals.get("action_total_ms", 0)
            + totals.get("validate_total_ms", 0)
        )
        idle_pct = 0.0
        if mission_total_ms > 0:
            idle_pct = max(0.0, 100.0 * (1.0 - busy_ms / mission_total_ms))

        log.info(
            f"Replay {self.mission.name}: {mission_total_ms:.0f}ms "
            f"({execution.steps_executed}/{total_steps}) "
            f"avg={totals.get('avg_ms', 0)}ms idle={idle_pct:.1f}% "
            f"by_resolver={totals.get('by_resolver', {})}"
        )

        self._publish_event(
            "mission.replay_complete",
            {
                "status": execution.status.value,
                "total_ms": round(mission_total_ms, 1),
                "idle_pct": round(idle_pct, 1),
                "slowest": self.perf.slowest(3),
                "by_resolver": totals.get("by_resolver", {}),
                "steps_skipped": execution.steps_skipped,
                "steps_manual": execution.steps_manual,
                "steps_recovered_retry": execution.steps_recovered_retry,
                "had_warnings": execution.had_warnings,
            },
        )

    def _recovery_debug_pop(self) -> Optional[str]:
        seq = getattr(self, "_recovery_debug_sequence", None) or []
        if not seq:
            return None
        return seq.pop(0) if seq else None

    def _wait_for_recovery_blocked(
        self,
        *,
        step_idx: int,
        step: CompiledStep,
        error_msg: str,
        recovery_cycles: int,
        recovery_max: int,
        retry_blocked: bool,
        execution_id: str,
    ) -> str:
        """Espera un comando síncrono desde la UI/tests."""
        if self._stop_requested:
            return "cancel"
        dq = self._recovery_debug_pop()
        if dq is not None:
            return self._normalize_recovery_command(dq)
        self._recovery_event.clear()
        with self._lock:
            self._pending_recovery_step_idx = step_idx
            self._recovery_cmd_result = None
        payload = {
            "step_index": step_idx,
            "step_id": step.id,
            "error_snippet": error_msg[:500],
            "recovery_cycles": recovery_cycles,
            "recovery_max_cycles": recovery_max,
            "retry_blocked": retry_blocked,
            "execution_id": execution_id,
        }
        try:
            self._publish_event("mission.recovery_waiting", payload)
        except Exception:
            pass
        while True:
            if self._stop_requested:
                return "cancel"
            if self._recovery_event.wait(timeout=0.28):
                with self._lock:
                    cmd = self._recovery_cmd_result
                    self._recovery_cmd_result = None
                self._recovery_event.clear()
                return self._normalize_recovery_command(cmd or "cancel")

    @staticmethod
    def _normalize_recovery_command(raw: Optional[str]) -> str:
        if raw is None:
            return "cancel"
        x = raw.strip().lower()
        aliases = {
            "reintentar": "retry",
            "skip": "skip",
            "omitir": "skip",
            "saltar": "skip",
            "manual": "manual_continue",
            "manual_continue": "manual_continue",
            "manual_intervention": "manual_continue",
            "cancelar": "cancel",
            "cancel": "cancel",
            "stop": "cancel",
            "resume": "retry",
            "retry": "retry",
        }
        return aliases.get(x, x)

    def _append_step_run_log(
        self,
        execution: MissionExecution,
        *,
        step: CompiledStep,
        step_idx: int,
        started_at,
        ended_at,
        elapsed_ms: float,
        status: str,
        resolver: str,
        error_message: str,
        strategy_attempts: int = 1,
        fallback_used: str = "",
        user_action: str = "",
        group_id_ev: str = "",
        validation_after_manual: Optional[bool] = None,
        manual_intervention: bool = False,
        user_continued: bool = False,
    ) -> None:
        interp = ""
        try:
            if step_idx < len(self.mission.interpreted_steps):
                interp = (self.mission.interpreted_steps[step_idx].description or "")[:500]
        except Exception:
            pass
        grp = group_id_ev
        execution.step_run_log.append(
            {
                "step_id": step.id,
                "step_index": step_idx,
                "group_id": grp or "",
                "description": interp,
                "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else str(started_at),
                "ended_at": ended_at.isoformat() if hasattr(ended_at, "isoformat") else str(ended_at),
                "elapsed_ms": round(elapsed_ms, 2),
                "status": status,
                "strategy_used": resolver[:120] if resolver else "",
                "fallback_used": fallback_used[:80] if fallback_used else "",
                "premium_click_meta": {},
                "validation_result": validation_after_manual,
                "error_message": (error_message or "")[:900],
                "user_action": user_action[:80] if user_action else "",
                "attempts": strategy_attempts,
                "manual_intervention": manual_intervention,
                "user_continued": user_continued,
            }
        )

    def _publish_finished_summary(self, execution: MissionExecution) -> None:
        payload = execution.model_dump(mode="json")
        self._publish_event("mission.execution_finished", payload)

    # ─────────────────────────────────────────────────────────────
    # Ejecución del paso compilado
    # ─────────────────────────────────────────────────────────────

    def _execute_compiled_step(self, step: CompiledStep, vars: Dict[str, str], timing=None):
        """Ejecuta un paso compilado usando TargetResolverV2 como ÚNICO
        motor de resolución (Phase 5 PRD).

        Flujo:
          1. Trae al frente la ventana destino (no Nevlan).
          2. Si la acción requiere target ⇒ ``self._resolver.resolve(step)``.
             Esto cubre el orden completo: web → UIA → visión → relativas
             → absolutas, sobre [primary] + alternates.
          3. Ramifica por ``target_kind``:
                - web_locator   ⇒ Playwright (locator.click/fill/dblclick).
                - uia_control   ⇒ UIA.Click/Invoke + coords como fallback.
                - visual_point  ⇒ pyautogui en coords.
                - relative_point/absolute_point ⇒ pyautogui (último recurso).
          4. Registra el método y el ``bundle_used`` para el repair learning.
        """
        target: Any = None
        target_coords: Optional[Dict[str, int]] = None
        resolution: Optional[TargetResolutionResult] = None
        method_used = "unknown"
        bundle_used = "primary"

        t_strat = step.target_resolution_strategy
        needs_target = step.action_strategy in (
            ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK,
            ActionStrategy.RIGHT_CLICK, ActionStrategy.DRAG_DROP,
            ActionStrategy.SET_FIELD_VALUE,
        )
        activate_only = step.action_strategy in _NO_TARGET_ACTIONS or (
            step.action_strategy == ActionStrategy.TYPE_TEXT
        )

        premium_coords: Optional[Dict[str, int]] = None
        premium_click_meta: Dict[str, Any] = {}
        visual_resolve_ms = 0.0
        stratval = getattr(
            step.target_resolution_strategy, "value",
            str(step.target_resolution_strategy),
        )
        erl_click_done = False

        with measure() as took:
            # Siempre activamos la ventana destino primero — evita clicks
            # sobre Nevlan u otra app que tenga foco al iniciar el replay.
            self._activate_window_hint(step)

            if needs_target and stratval in (
                "coordinate_icon_validated_click", "icon_validated_coords",
            ):
                wl = step.target_context.web_data or {}
                if not (wl.get("locators")):
                    try:
                        with measure() as vz:
                            premium_coords, premium_click_meta = self._resolve_icon_validated_coords(
                                step)
                        visual_resolve_ms = vz()
                        pc_region = premium_click_meta.get("region_used")
                        log.info(
                            "[premium_click] strat=%s coords_ok=%s region=%s image=%s "
                            "fallback=%s visual_ms=%.1f meta=%s",
                            stratval,
                            bool(premium_coords),
                            pc_region,
                            premium_click_meta.get("image_used"),
                            premium_click_meta.get("fallback_used"),
                            visual_resolve_ms,
                            {k: premium_click_meta.get(k)
                             for k in ("visual_match_score", "offset_applied")},
                        )
                    except Exception as e:
                        log.debug(f"premium icon pre-pass: {e}")

            if needs_target:
                erl_resolution = None
                try:
                    from app.services.runtime.entity_runtime_resolution import (
                        try_entity_first_runtime,
                    )
                    from app.services.missions.state_detector import StateDetector

                    sem_step = self._semantic_mission_step_for_compiled(step)
                    if sem_step is not None:
                        snap = StateDetector().detect(deep=True)
                        hook = try_entity_first_runtime(
                            sem_step, snap, self.mission,
                        )
                        if hook.executed and hook.strategy_result and hook.strategy_result.ok:
                            erl_resolution = hook
                except Exception as e:
                    log.debug("[ERL] player entity-first: %s", e)

                if erl_resolution is not None:
                    erl_click_done = True
                    resolution = None
                    method_used = (
                        f"entity_resolution:{erl_resolution.strategy_used or 'resolved'}"
                    )
                    bundle_used = "entity_resolution"
                    if timing:
                        timing.resolver = method_used
                    weak = False
                else:
                    try:
                        resolution = self._resolver.resolve(step, mission=self.mission)
                    except Exception as e:
                        log.warning(f"TargetResolverV2 excepcion: {e}")
                        resolution = None
                    weak = resolution is None
                if resolution is not None:
                    _floor = (
                        35 if resolution.target_kind == "absolute_point" else 50
                    )
                    weak = (
                        weak
                        or resolution.method == "none"
                        or int(resolution.score or 0) < _floor
                    )
                if weak:
                    try:
                        from app.services.missions.state_detector import (
                            StateDetector,
                        )
                        from app.services.runtime.arl_sep_recovery import (
                            try_target_resolver_arl_second_pass,
                        )

                        if try_target_resolver_arl_second_pass(
                            mission=self.mission,
                            compiled_step=step,
                            resolution=resolution,
                            detector=StateDetector(),
                        ):
                            resolution = self._resolver.resolve(step, mission=self.mission)
                    except Exception as e:
                        log.debug(f"[ARL] target_resolver second pass omitido: {e}")

                if resolution is not None:
                    method_used = resolution.method
                    bundle_used = resolution.bundle_used
                    if timing:
                        timing.resolver = resolution.method
                        # Pasamos al outer loop para que registre fallos
                        # contra el bundle correcto (primary vs alternate:N).
                        try:
                            timing.bundle_used = bundle_used  # type: ignore[attr-defined]
                            timing.target_kind = resolution.target_kind  # type: ignore[attr-defined]
                            timing.score = resolution.score  # type: ignore[attr-defined]
                        except Exception:
                            pass

                    # Threshold dinámico (PRD §5):
                    #   ≥85 → ejecutar automático.
                    #   55-84 → ejecutar pero registrar advertencia.
                    #   <55 → fallar para que entre repair.
                    # Para mantener compat con misiones legacy (donde el
                    # primary bundle puede tener score bajo pero las coords
                    # siguen siendo útiles), bajamos el corte duro a 35
                    # cuando el target_kind es absolute_point.
                    hard_floor = 35 if resolution.target_kind == "absolute_point" else 50
                    if resolution.score < hard_floor or resolution.method == "none":
                        log.info(
                            f"resolver: score={resolution.score} method={resolution.method} "
                            f"→ insuficiente, requeriría reparación"
                        )
                        # No abortamos aquí: el bucle de retry fuera de
                        # esta función decide; pero registramos.
                    else:
                        if resolution.target_kind == "uia_control":
                            target = resolution.target
                            target_coords = resolution.coords
                        elif resolution.target_kind == "web_locator":
                            target = resolution.target  # Playwright Locator
                        else:
                            # visual_point | relative_point | absolute_point
                            target_coords = resolution.coords

        if timing:
            timing.resolve_ms = took()
            if visual_resolve_ms > 0:
                try:
                    timing.visual_match_ms = visual_resolve_ms  # type: ignore[attr-defined]
                    timing.premium_strategy = stratval  # type: ignore[attr-defined]
                    if premium_click_meta:
                        timing.premium_click_meta = premium_click_meta  # type: ignore[attr-defined]
                except Exception:
                    pass

        coords = target_coords or step.target_context.fallback_coords

        if premium_coords and needs_target:
            skip_premium = resolution and getattr(
                resolution, "target_kind", ""
            ) in ("web_locator",)
            uia_ready = (
                resolution is not None
                and getattr(resolution, "target_kind", "") == "uia_control"
                and target is not None
            )
            if not skip_premium and not uia_ready:
                coords = premium_coords
                method_used = "coordinate_icon_validated_click"
                if timing:
                    try:
                        timing.resolver = "coordinate_icon_validated_click"  # type: ignore[attr-defined]
                    except Exception:
                        pass

        # Si el resolver NO encontró nada y la acción necesita target,
        # fallback a la cadena legacy (compat con misiones viejas que
        # aún no tienen bundle V3 ni alternates).
        if needs_target and target is None and not coords and resolution is None:
            target = self._resolve_uia(step)
            if not target:
                coords = (self._absolute_coords_if_in_window(step)
                          or self._resolve_relative_window_coords(step)
                          or self._resolve_vision(step)
                          or step.target_context.fallback_coords)
                if not coords:
                    raise RuntimeError("Elemento no encontrado (resolver V2 + legacy)")

        # ── PRE-STATE snapshot (FASE 9) ───────────────────────────
        # Se captura ANTES de ejecutar la acción. Es ultra ligero
        # (URL actual + presencia del UIA target) y permite validar
        # cosas como "url_changed" o "dialog_closed" después.
        pre_state = self._capture_pre_state(step, resolution, target)

        with measure() as took_action:
            if erl_click_done:
                pass
            # Si la resolución es web_locator, atajo directo a Playwright
            # (NO mover mouse, NO usar coords).
            elif (resolution is not None
                    and resolution.target_kind == "web_locator"
                    and resolution.target is not None
                    and step.action_strategy in (
                        ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK,
                        ActionStrategy.RIGHT_CLICK,
                    )):
                self._web_action_with_locator(
                    resolution.target, step.action_strategy
                )
            elif (resolution is not None
                  and resolution.target_kind == "web_locator"
                  and resolution.target is not None
                  and step.action_strategy == ActionStrategy.SET_FIELD_VALUE):
                text = (step.action_payload or {}).get("text") or ""
                # Expansión simple de variables {{var}}.
                for k, v in (vars or {}).items():
                    text = text.replace(f"{{{{{k}}}}}", str(v))
                try:
                    resolution.target.scroll_into_view_if_needed(timeout=1500)
                    resolution.target.fill(text, timeout=2500)
                except Exception as e:
                    log.debug(f"web fill via resolver locator falló: {e}")
                    # Fallback al pipeline normal SET_FIELD_VALUE.
                    self._execute_action(step.action_strategy, target,
                                         coords, step.action_payload,
                                         vars, step)
            else:
                self._execute_action(step.action_strategy, target, coords,
                                     step.action_payload, vars, step)
        if timing:
            timing.action_ms = took_action()

        # ── Validación SEMÁNTICA por resultado (FASE 9) ──────────
        # Siempre que el compiler haya enriquecido el paso con
        # ``semantic.expected_state_after`` validamos best-effort.
        # NUNCA bloquea > ~1s. NUNCA lanza. El resultado se publica
        # para la UI y alimenta el repair learning.
        validation_passed: Optional[bool] = None
        validation_reason: str = ""
        with measure() as took_val:
            try:
                ok_val, reason_val = self._semantic_post_validate(
                    step, resolution, target, pre_state, vars
                )
                if ok_val is not None:
                    validation_passed = ok_val
                    validation_reason = reason_val
                    self._publish_event(
                        "mission.step_validated",
                        {
                            "step_id": step.id,
                            "ok": ok_val,
                            "reason": reason_val,
                            "expected": (step.target_context.semantic or {})
                                          .get("expected_state_after", ""),
                        },
                    )
            except Exception as e:
                log.debug(f"_semantic_post_validate exception: {e}")
        if timing:
            timing.validate_ms = took_val()
            try:
                timing.validation_passed = validation_passed  # type: ignore[attr-defined]
                timing.validation_reason = validation_reason  # type: ignore[attr-defined]
            except Exception:
                pass

        # Phase 8 + FASE 9: registrar éxito del método + del bundle.
        # Si la validación semántica falló, marcamos el método como fail
        # para que el repair learning lo penalice y suba alternates.
        try:
            from app.services.missions.repair import (
                record_method_outcome, record_bundle_success,
            )
            method_ok = (validation_passed is not False)
            record_method_outcome(step, str(method_used), method_ok)
            record_bundle_success(step, bundle_used, method_ok)
        except Exception:
            pass

        # ── Validación legacy por validation_strategy ────────────
        # (REQUIRE_FIELD_VALUE / REQUIRE_ELEMENT_EXISTS): se mantiene
        # para misiones antiguas. Para V3 ya cubre _semantic_post_validate.
        if step.validation_strategy != ValidationStrategy.NONE:
            try:
                self._post_validate(step, target, resolution=resolution)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────
    # Target resolvers
    # ─────────────────────────────────────────────────────────────

    def _semantic_mission_step_for_compiled(
        self, compiled_step: CompiledStep,
    ) -> Optional["MissionStep"]:
        """MissionStep semántico alineado con un CompiledStep (para ERL)."""
        from app.services.missions.execution_contracts import MissionStep
        from app.services.missions.semantic_review_display import (
            find_semantic_step_for_compiled,
        )
        from app.services.missions.semantic_execution_plan import (
            SemanticPlanStep,
            _executor_kind_for_semantic_step,
        )

        sem = find_semantic_step_for_compiled(self.mission, compiled_step.id)
        if not sem:
            return None
        try:
            sp = SemanticPlanStep.from_dict(sem)
            kind = _executor_kind_for_semantic_step(sp)
            pars = dict(sp.params or {})
            return MissionStep(
                id=str(sp.id or compiled_step.id),
                kind=kind,
                params=pars,
                human_label=sp.human_label or "",
            )
        except Exception:
            return MissionStep(
                id=str(sem.get("id") or compiled_step.id),
                kind=str(sem.get("type") or "click"),
                params=dict(sem.get("params") or {}),
                human_label=str(sem.get("human_label") or ""),
            )

    def _resolve_uia(self, step: CompiledStep):
        """Busca primero bajo la ventana activa; cae a global si hace falta.

        Reglas de seguridad:
        - Si solo tenemos un Name genérico (GroupControl, PaneControl...)
          NO buscamos por nombre — es ambiguo y puede clickar en Nevlan misma.
        - Si hay hint de ventana, NUNCA caemos a búsqueda global (seguro).
        - Descartamos matches cuyo proceso sea el de Nevlan.
        """
        if not HAS_UIA or not step.target_context.uia_data:
            return None

        uia_data = step.target_context.uia_data
        automation_id = uia_data.get("automation_id") or None
        name_raw = uia_data.get("name") or None
        class_name = uia_data.get("class_name") or None
        control_type = uia_data.get("control_type") or None

        # Filtramos nombres genéricos (Name == ControlType significa que el
        # recorder no obtuvo un nombre real y puso el tipo como relleno)
        name = name_raw
        if name and _is_generic_name(name):
            name = None
        if name and control_type and name.strip() == control_type.strip():
            name = None

        has_window_hint = bool(self._window_hint(step))

        if not name and not automation_id:
            # Sin identificador confiable → que se encargue fallback coords/vision.
            return None

        kwargs: Dict[str, Any] = {}
        if automation_id:
            kwargs["AutomationId"] = automation_id
        if name:
            kwargs["Name"] = name
        if class_name:
            kwargs["ClassName"] = class_name

        own_pid = _current_pid()

        def _match_not_us(ctrl) -> bool:
            """Descarta matches que caen en el proceso de Nevlan."""
            try:
                pid = int(getattr(ctrl, "ProcessId", 0) or 0)
                return pid == 0 or pid != own_pid
            except Exception:
                return True

        anchor_bbox = step.target_context.anchor_bbox or None

        def _is_same_element(ctrl) -> bool:
            """Si tenemos `anchor_bbox`, el control encontrado debe caer
            razonablemente cerca de donde estaba al grabar. Si hay varios
            controles con el mismo nombre (p.ej. dos botones 'Aceptar'),
            esto evita clickar en el equivocado.
            """
            if not anchor_bbox:
                return True
            try:
                r = ctrl.BoundingRectangle
                cx_now = (int(r.left) + int(r.right)) // 2
                cy_now = (int(r.top) + int(r.bottom)) // 2
                ax = int(anchor_bbox.get("left", 0)) + int(anchor_bbox.get("width", 0)) // 2
                ay = int(anchor_bbox.get("top", 0)) + int(anchor_bbox.get("height", 0)) // 2
                # Tolerancia: 60% de la dimensión mayor del anchor — suficiente
                # para capturar re-layouts menores y scrolls, rígido para
                # rechazar otro control homónimo lejos.
                max_dim = max(int(anchor_bbox.get("width", 0)),
                              int(anchor_bbox.get("height", 0)), 50)
                tol = int(max_dim * 1.5)
                if abs(cx_now - ax) > tol or abs(cy_now - ay) > tol:
                    log.debug(
                        f"UIA match descartado por distancia "
                        f"(anchor=({ax},{ay}) actual=({cx_now},{cy_now}) tol={tol})"
                    )
                    return False
                return True
            except Exception:
                return True

        win_ctrl = self._get_active_window_ctrl(step)
        if win_ctrl is not None:
            try:
                control = win_ctrl.Control(searchDepth=_UIA_SEARCH_DEPTH, **kwargs)
                if (control.Exists(0, _FAST_TIMEOUT_S)
                        and _match_not_us(control)
                        and _is_same_element(control)):
                    return control
            except Exception as e:
                log.debug(f"UIA scoped resolve error: {e}")

        # Solo permitimos búsqueda global si NO había hint de ventana
        # (si había hint y no matcheó dentro, no queremos saltar a otra app).
        if not has_window_hint:
            try:
                control = auto.Control(searchDepth=_UIA_SEARCH_DEPTH, **kwargs)
                if (control.Exists(0, _FAST_TIMEOUT_S)
                        and _match_not_us(control)
                        and _is_same_element(control)):
                    return control
            except Exception as e:
                log.debug(f"UIA global resolve error: {e}")

        return None

    def _wait_for_window(self, title: str, timeout_s: float) -> bool:
        """Espera hasta que exista una ventana cuyo título contenga `title`."""
        try:
            import pygetwindow as gw  # type: ignore
        except Exception:
            gw = None
        t0 = time.perf_counter()
        head = title.split(" - ")[0].strip()
        while time.perf_counter() - t0 < timeout_s:
            if gw is not None:
                try:
                    if gw.getWindowsWithTitle(title) or (head and head != title and gw.getWindowsWithTitle(head)):
                        return True
                except Exception:
                    pass
            if HAS_UIA:
                try:
                    w = auto.WindowControl(searchDepth=1, SubName=title)
                    if w.Exists(0, 0.1):
                        return True
                except Exception:
                    pass
            time.sleep(_WINDOW_READY_POLL_S)
        return False

    def _window_hint(self, step: CompiledStep) -> Optional[Dict[str, Any]]:
        """Extrae el hint de ventana embebido por el compilador (uia_data['window'])."""
        data = step.target_context.uia_data or {}
        w = data.get("window") if isinstance(data, dict) else None
        if isinstance(w, dict) and (w.get("title") or w.get("bbox")):
            return w
        return None

    def _activate_window_hint(self, step: CompiledStep, wait_for_it: bool = True) -> bool:
        """Trae al frente la ventana grabada (rápido vía pygetwindow).
        Si `wait_for_it` y la ventana aún no existe (p.ej. Chrome cargando)
        hace polling hasta `_WINDOW_READY_TIMEOUT_S` antes de rendirse."""
        hint = self._window_hint(step)
        if not hint:
            return False
        title = (hint.get("title") or "").strip()
        if not title:
            return False

        # No re-activar la misma ventana en cada paso.
        if self._ctx.last_activated_title == title:
            return True

        if wait_for_it:
            if not self._wait_for_window(title, _WINDOW_READY_TIMEOUT_S):
                log.debug(f"Ventana '{title}' no apareció en {_WINDOW_READY_TIMEOUT_S}s")

        # Guardamos bbox para visión acotada.
        bbox = hint.get("bbox")
        if isinstance(bbox, dict) and bbox:
            try:
                self._ctx.window_bbox = {
                    "left": int(bbox.get("left", 0)),
                    "top": int(bbox.get("top", 0)),
                    "width": int(bbox.get("width", 0)),
                    "height": int(bbox.get("height", 0)),
                }
            except Exception:
                pass

        try:
            import pygetwindow as gw  # type: ignore
        except Exception:
            gw = None

        activated = False
        if gw is not None:
            try:
                wins = gw.getWindowsWithTitle(title)
                if not wins:
                    head = title.split(" - ")[0].strip()
                    if head and head != title:
                        wins = gw.getWindowsWithTitle(head)
                for w in wins:
                    try:
                        # Defensa: nunca activar una ventana del propio Nevlan,
                        # aunque su título coincida con el grabado.
                        try:
                            hwnd = int(getattr(w, "_hWnd", 0)) or 0
                        except Exception:
                            hwnd = 0
                        if hwnd and _pid_for_hwnd(hwnd) == _current_pid():
                            log.debug(f"Ventana '{title}' es de Nevlan; ignorada.")
                            continue
                        if w.isMinimized:
                            w.restore()
                        w.activate()
                        activated = True
                        break
                    except Exception:
                        continue
            except Exception as e:
                log.debug(f"activate_window_hint (pygetwindow) error: {e}")

        # Fallback a UIA si no pudimos por pygetwindow (apps con title dinámico)
        if not activated and HAS_UIA:
            try:
                w = auto.WindowControl(searchDepth=1, SubName=title)
                if w.Exists(0, _FAST_TIMEOUT_S):
                    # Defensa: si la ventana UIA encontrada es de Nevlan, ignorarla.
                    try:
                        pid = int(getattr(w, "ProcessId", 0) or 0)
                    except Exception:
                        pid = 0
                    if pid and pid == _current_pid():
                        log.debug(f"Ventana UIA '{title}' es de Nevlan; ignorada.")
                    else:
                        try:
                            w.SetActive()
                        except Exception:
                            try:
                                w.SetFocus()
                            except Exception:
                                pass
                        activated = True
                        self._ctx.window_ctrl = w
            except Exception as e:
                log.debug(f"activate_window_hint (UIA) error: {e}")

        if activated:
            self._ctx.last_activated_title = title
            time.sleep(_WINDOW_ACTIVATE_PAUSE)
        return activated

    def _get_active_window_ctrl(self, step: CompiledStep):
        """Resuelve y cachea el WindowControl que contiene el elemento."""
        if not HAS_UIA:
            return None
        if self._ctx.window_ctrl is not None:
            # Evitamos validar con Exists() cada paso (costoso). Se invalida
            # automáticamente si una búsqueda posterior falla.
            return self._ctx.window_ctrl

        # Preferir título de la grabación para estabilidad entre apps distintas
        hint = self._window_hint(step) or {}
        title_hint = hint.get("title") if isinstance(hint, dict) else None

        own_pid = _current_pid()

        def _is_ours(ctrl) -> bool:
            try:
                return int(getattr(ctrl, "ProcessId", 0) or 0) == own_pid
            except Exception:
                return False

        try:
            if title_hint:
                w = auto.WindowControl(searchDepth=1, SubName=title_hint)
                if w.Exists(0, _FAST_TIMEOUT_S) and not _is_ours(w):
                    self._ctx.window_ctrl = w
                    self._ctx.window_title_hint = title_hint
                    try:
                        r = w.BoundingRectangle
                        self._ctx.window_bbox = {
                            "left": int(r.left), "top": int(r.top),
                            "width": int(r.right - r.left),
                            "height": int(r.bottom - r.top),
                        }
                    except Exception:
                        pass
                    return w
            w = auto.GetForegroundControl()
            if w and not _is_ours(w):
                self._ctx.window_ctrl = w
                try:
                    r = w.BoundingRectangle
                    self._ctx.window_bbox = {
                        "left": int(r.left), "top": int(r.top),
                        "width": int(r.right - r.left),
                        "height": int(r.bottom - r.top),
                    }
                except Exception:
                    pass
                return w
        except Exception as e:
            log.debug(f"get_active_window_ctrl: {e}")
        return None

    # ─────────────────────────────────────────────────────────────
    # Post-validación (barata, best-effort)
    # ─────────────────────────────────────────────────────────────

    def _post_validate(self, step: CompiledStep, uia_target: Any,
                        resolution: Optional[TargetResolutionResult] = None) -> None:
        """Validación barata tras ejecutar un paso.

        Reglas:
        - `REQUIRE_FIELD_VALUE` (post-texto): lee el valor real del campo.
          Si la resolución usó un Playwright locator, leemos
          ``locator.input_value()``. Si no, caemos a ``ValuePattern.Value``
          del UIA target. Nunca bloquea >800ms.
        - `REQUIRE_ELEMENT_EXISTS`: comprueba que el control sigue existiendo
          (verifica que el click no destruyó al target inesperadamente).
        - Otras validaciones se dejan pasar (ya las maneja el resolver).
        - `NONE`: no hace nada (modo default rápido).
        """
        strat = step.validation_strategy
        if strat == ValidationStrategy.NONE:
            return

        if strat == ValidationStrategy.REQUIRE_FIELD_VALUE:
            expected = (step.action_payload or {}).get("text") or ""
            if not expected:
                return

            # 1) Si la resolución fue web → leer locator.input_value()
            web_locator = (resolution.target
                           if (resolution is not None
                               and resolution.target_kind == "web_locator")
                           else None)
            if web_locator is not None:
                try:
                    actual = web_locator.input_value(timeout=800)
                    if (actual or "").strip() == expected.strip():
                        return
                    log.debug(
                        f"post-texto web: esperado='{expected!r}' "
                        f"actual='{actual!r}' (diferencia, no bloquea)"
                    )
                    return
                except Exception as e:
                    log.debug(f"web input_value falló: {e}")
                    # Continuar a UIA fallback.

            # 2) UIA ValuePattern
            if not HAS_UIA or not uia_target:
                # 3) último recurso: leer por web data del paso aunque no
                # haya target_kind=web (p.ej. SET_FIELD_VALUE legacy).
                web = (step.target_context.web_data or {})
                if web.get("locators"):
                    try:
                        actual = self._web_read_value(web)
                        if actual is not None:
                            if (actual or "").strip() == expected.strip():
                                return
                            log.debug(
                                f"post-texto web (legacy): esperado='{expected!r}' "
                                f"actual='{actual!r}'"
                            )
                            return
                    except Exception:
                        pass
                return
            try:
                vp = None
                try:
                    vp = uia_target.GetValuePattern()  # type: ignore[attr-defined]
                except Exception:
                    vp = None
                if vp:
                    actual = str(getattr(vp, "Value", "") or "")
                    if actual.strip() == expected.strip():
                        return
                    log.debug(
                        f"post-texto UIA: esperado='{expected!r}' "
                        f"actual='{actual!r}' (diferencia, no bloquea)"
                    )
            except Exception as e:
                log.debug(f"REQUIRE_FIELD_VALUE exception: {e}")
            return

        if strat == ValidationStrategy.REQUIRE_ELEMENT_EXISTS:
            if not HAS_UIA or not uia_target:
                return
            try:
                # Exists con timeout mínimo; no dormimos si ya existe.
                if hasattr(uia_target, "Exists") and not uia_target.Exists(0, 0.05):
                    log.debug("REQUIRE_ELEMENT_EXISTS: control ya no existe tras la acción")
            except Exception:
                pass
            return

        # REQUIRE_IMAGE_MATCH / REQUIRE_DISAPPEAR / REQUIRE_TEXT_MATCH:
        # implementaciones específicas quedan como trabajo futuro; ya se
        # usan vía annotations (se reflejan en descripción).

    # ─────────────────────────────────────────────────────────────
    # Validación SEMÁNTICA por resultado (FASE 9 PRD §9)
    # ─────────────────────────────────────────────────────────────

    def _capture_pre_state(self, step: CompiledStep,
                            resolution: Optional[TargetResolutionResult],
                            uia_target: Any) -> Dict[str, Any]:
        """Snapshot ULTRA ligero del estado antes de ejecutar la acción.

        Solo capturamos lo que nos hace falta para validar el
        ``expected_state_after`` correspondiente (no hacemos un dump
        general). Total budget objetivo: < 80ms.
        """
        pre: Dict[str, Any] = {"t0": time.perf_counter()}
        sem = step.target_context.semantic or {}
        expected = (sem.get("expected_state_after") or "").strip().lower()
        if not expected:
            return pre

        # URL actual: solo si esperamos url_changed (o navigate-like) y
        # tenemos puente Playwright.
        if expected in ("url_changed", "menu_dismissed", "dialog_closed"):
            try:
                from app.skills.tools.web import _get_page  # type: ignore
                page = _get_page()
                if page is not None:
                    try:
                        pre["url"] = page.url
                    except Exception:
                        pass
            except Exception:
                pass

        # Para dialog_closed/element_state_changed nos sirve saber si el
        # UIA target estaba vivo justo antes.
        if expected in ("dialog_closed", "element_state_changed"):
            pre["uia_target_alive_pre"] = bool(uia_target)
        return pre

    # Estados que sí validamos. El resto se ignora silenciosamente.
    _VALIDATED_STATES = {
        "url_changed", "field_value_set", "dialog_closed",
        "menu_dismissed", "target_visible", "element_state_changed",
    }

    def _semantic_post_validate(
        self,
        step: CompiledStep,
        resolution: Optional[TargetResolutionResult],
        uia_target: Any,
        pre_state: Dict[str, Any],
        vars: Dict[str, str],
    ) -> Tuple[Optional[bool], str]:
        """Valida ``target_context.semantic.expected_state_after`` tras
        la acción. Devuelve ``(ok, reason)``:

          - ``(None, "")`` si no había nada que validar (rápido).
          - ``(True, "...")`` si pasó.
          - ``(False, "...")`` si la evidencia indica que no.

        NUNCA lanza; nunca bloquea > ~1.2s sumando todos los polls.
        """
        sem = step.target_context.semantic or {}
        expected = (sem.get("expected_state_after") or "").strip().lower()
        if not expected or expected not in self._VALIDATED_STATES:
            return None, ""

        # url_changed / menu_dismissed (en web) ----------------------
        if expected in ("url_changed", "menu_dismissed"):
            pre_url = pre_state.get("url")
            if not pre_url:
                # Sin URL previa no podemos comparar — best-effort: skip.
                return None, ""
            try:
                from app.skills.tools.web import _get_page  # type: ignore
                page = _get_page()
            except Exception:
                return None, ""
            if page is None:
                return None, ""
            # Polling corto (~600ms) para dar tiempo a la SPA.
            deadline = time.time() + 0.6
            while time.time() < deadline:
                try:
                    cur = page.url
                except Exception:
                    cur = pre_url
                if cur and cur != pre_url:
                    return True, f"url cambió: {pre_url} → {cur}"
                time.sleep(0.08)
            return False, f"url no cambió tras la acción ({pre_url})"

        # field_value_set: leer valor final y comparar ---------------
        if expected == "field_value_set":
            expected_text = (step.action_payload or {}).get("text") or ""
            for k, v in (vars or {}).items():
                expected_text = expected_text.replace(f"{{{{{k}}}}}", str(v))
            if not expected_text:
                return None, ""
            actual = self._read_field_value_after_action(
                step, resolution, uia_target
            )
            if actual is None:
                # No pudimos leer (no hay API disponible) → no bloqueamos.
                return None, ""
            if actual.strip() == expected_text.strip():
                return True, "valor del campo == esperado"
            # Validación parcial: a veces la app normaliza espacios o capital.
            if actual.strip().replace(" ", "") == expected_text.strip().replace(" ", ""):
                return True, "valor del campo coincide (ignorando espacios)"
            return (
                False,
                f"valor distinto — esperado='{expected_text[:40]}' actual='{(actual or '')[:40]}'",
            )

        # dialog_closed: el target original ya no debe existir -------
        if expected == "dialog_closed":
            if not pre_state.get("uia_target_alive_pre"):
                return None, ""
            if uia_target is None or not HAS_UIA:
                return None, ""
            try:
                # Pequeño polling (~400ms) para apps que tardan en cerrar.
                deadline = time.time() + 0.4
                while time.time() < deadline:
                    try:
                        if hasattr(uia_target, "Exists") and not uia_target.Exists(0, 0.05):
                            return True, "dialog cerrado"
                    except Exception:
                        return None, ""
                    time.sleep(0.06)
                return False, "el control del diálogo sigue presente"
            except Exception:
                return None, ""

        # target_visible: para SCROLL_UNTIL_VISIBLE confiamos en que
        # el playback ya hizo scroll_into_view_if_needed o equivalente;
        # validamos best-effort que el target sea localizable.
        if expected == "target_visible":
            web = (step.target_context.web_data or {})
            if web.get("locators"):
                try:
                    from app.skills.tools.web import _get_page  # type: ignore
                    page = _get_page()
                except Exception:
                    return None, ""
                if page is None:
                    return None, ""
                for spec in (web.get("locators") or []):
                    loc = self._build_web_locator(page, spec)
                    if loc is None:
                        continue
                    try:
                        if loc.first.is_visible(timeout=400):
                            return True, "target visible en viewport"
                    except Exception:
                        continue
                return False, "target no visible tras scroll"
            # Para desktop dejamos pasar (ya validado por el resolver).
            return None, ""

        # element_state_changed: heurística suave - si tenemos UIA
        # target y ya no existe, también es señal de cambio aceptable.
        if expected == "element_state_changed":
            if not pre_state.get("uia_target_alive_pre"):
                return None, ""
            if uia_target is None or not HAS_UIA:
                return None, ""
            try:
                if hasattr(uia_target, "Exists") and not uia_target.Exists(0, 0.05):
                    return True, "elemento ya no existe (estado cambió)"
            except Exception:
                pass
            # Sin pista clara → no concluimos nada (no es failure).
            return None, ""

        return None, ""

    def _read_field_value_after_action(
        self,
        step: CompiledStep,
        resolution: Optional[TargetResolutionResult],
        uia_target: Any,
    ) -> Optional[str]:
        """Intenta leer el valor real del campo tras un SET_FIELD_VALUE.

        Orden:
          1) Si la resolución fue ``web_locator`` → ``locator.input_value()``.
          2) Si hay UIA target con ValuePattern → ``ValuePattern.Value``.
          3) Si el step trae ``web.locators`` reusables → ``_web_read_value``.

        Devuelve ``None`` si no fue posible leer en < ~1s (no penaliza).
        """
        # 1) web locator de la resolución actual
        if (resolution is not None
                and resolution.target_kind == "web_locator"
                and resolution.target is not None):
            try:
                return resolution.target.input_value(timeout=800)
            except Exception as e:
                log.debug(f"input_value (resolution): {e}")

        # 2) UIA ValuePattern
        if HAS_UIA and uia_target is not None:
            try:
                vp = uia_target.GetValuePattern()  # type: ignore[attr-defined]
                if vp is not None:
                    return str(getattr(vp, "Value", "") or "")
            except Exception:
                pass

        # 3) Locators serializados en el step
        web = (step.target_context.web_data or {})
        if web.get("locators"):
            try:
                return self._web_read_value(web)
            except Exception:
                pass
        return None

    def _resolve_icon_validated_coords(
        self, step: CompiledStep,
    ) -> tuple[Optional[Dict[str, int]], Dict[str, Any]]:
        """ICON_VALIDATED_COORDS — validación visual CONSERVADORA.

        Regla de oro (fidelidad > agresividad):
        - Solo aceptamos un template-match si está RAZONABLEMENTE CERCA del
          punto original grabado. Si el match aparece en otra esquina de la
          pantalla, casi seguro es un falso positivo (hay muchos íconos
          parecidos en Windows). En ese caso devolvemos None y dejamos que
          el resolver caiga a las coords absolutas originales.
        - Buscamos SOLO dentro de anchor_bbox expandido (ventana de certeza),
          no en toda la ventana y mucho menos en pantalla completa.
        - Exigimos alta confianza (0.92+): mejor decir "no lo vi" que clickar
          en el lugar equivocado.

        Devuelve ``(coords | None, meta)`` — ``meta`` alimenta logs/métricas.
        """
        meta: Dict[str, Any] = {
            "strategy_used": "coordinate_icon_validated_click",
            "region_used": None,
            "image_used": None,
            "visual_match_score": None,
            "offset_applied": None,
            "fallback_used": None,
            "full_screen_search": False,
        }
        tc = step.target_context
        if not (tc.image_ref_mid or tc.image_ref):
            meta["fallback_used"] = "no_image_refs"
            return None, meta
        anchor = tc.anchor_bbox or {}
        # Sin anchor_bbox no podemos acotar la búsqueda con seguridad → skip.
        if not (anchor and anchor.get("width", 0) >= 10 and anchor.get("height", 0) >= 10):
            meta["fallback_used"] = "no_anchor_bbox"
            return None, meta

        try:
            import pyautogui  # type: ignore
        except Exception:
            meta["fallback_used"] = "pyautogui_unavailable"
            return None, meta

        # Región ACOTADA: bbox original ± 1 bbox de padding (ventana de certeza).
        # Esto mantiene el costo bajo Y elimina prácticamente los falsos positivos:
        # solo aceptamos matches que aparezcan cerca de donde estuvo originalmente.
        anchor_cx = int(anchor["left"]) + int(anchor["width"]) // 2
        anchor_cy = int(anchor["top"]) + int(anchor["height"]) // 2
        pad_x = max(50, int(anchor["width"]))
        pad_y = max(50, int(anchor["height"]))
        region = (
            int(anchor["left"]) - pad_x,
            int(anchor["top"]) - pad_y,
            int(anchor["width"]) + pad_x * 2,
            int(anchor["height"]) + pad_y * 2,
        )
        meta["region_used"] = region

        def _match(image_path: Optional[str], conf: float) -> Optional[Any]:
            if not image_path:
                return None
            try:
                return pyautogui.locateOnScreen(image_path, confidence=conf, region=region)
            except Exception:
                return None

        box_mid = _match(tc.image_ref_mid, 0.92)
        box = box_mid or _match(tc.image_ref, 0.92)
        if box is None:
            meta["fallback_used"] = "template_miss_in_region"
            return None, meta

        meta["visual_match_score"] = 0.92
        meta["image_used"] = tc.image_ref_mid if box_mid else tc.image_ref

        try:
            bx, by, bw, bh = int(box.left), int(box.top), int(box.width), int(box.height)
        except Exception:
            meta["fallback_used"] = "parse_box_failed"
            return None, meta

        cx = bx + bw // 2
        cy = by + bh // 2

        # SANITY CHECK: el match no puede estar MUY lejos del anchor original.
        max_drift = max(pad_x, pad_y) * 2
        if abs(cx - anchor_cx) > max_drift or abs(cy - anchor_cy) > max_drift:
            log.debug(
                f"icon_validated: match descartado por drift excesivo "
                f"(anchor=({anchor_cx},{anchor_cy}) match=({cx},{cy}))"
            )
            meta["fallback_used"] = "match_drift_too_far"
            return None, meta

        off = tc.click_offset_within_bbox or {}
        if off and tc.anchor_bbox:
            ow = max(1, int(tc.anchor_bbox.get("width", bw)))
            oh = max(1, int(tc.anchor_bbox.get("height", bh)))
            scale_x = bw / ow
            scale_y = bh / oh
            cx += int(int(off.get("dx", 0)) * scale_x)
            cy += int(int(off.get("dy", 0)) * scale_y)
            meta["offset_applied"] = dict(off)
        return {"x": cx, "y": cy}, meta

    def _should_use_uia_native_click(self, step: Optional[CompiledStep],
                                     uia_target: Any) -> bool:
        """Decide si conviene `uia.Click()` en lugar de `pyautogui.click(x,y)`.

        Regla: UIA nativo es MÁS robusto cuando el elemento encontrado:
        - Tiene un bbox pequeño (parece un control individual, no un panel).
        - Su tamaño/posición actual es similar al bbox grabado (no hubo
          un reescalado grande de la ventana).

        En cualquier otro caso (contenedor grande, o reescalado claro),
        preferimos clickar por coordenadas exactas — UIA nativo clickaría
        en el centro del contenedor y eso cae fuera del control real.
        """
        if uia_target is None:
            return False
        try:
            r = uia_target.BoundingRectangle
            w = max(0, int(getattr(r, "right", 0)) - int(getattr(r, "left", 0)))
            h = max(0, int(getattr(r, "bottom", 0)) - int(getattr(r, "top", 0)))
        except Exception:
            # Sin bbox accesible → UIA poco fiable, mejor coords.
            return False
        if w <= 0 or h <= 0:
            return False
        # Reescalado grande respecto a lo grabado → preferir coords escaladas.
        if step is not None:
            anchor = step.target_context.anchor_bbox or {}
            try:
                aw = max(0, int(anchor.get("width") or 0))
                ah = max(0, int(anchor.get("height") or 0))
            except Exception:
                aw = ah = 0
            if aw and ah:
                def ratio(a, b):
                    return max(a, b) / max(1, min(a, b))
                if ratio(w, aw) > 1.4 or ratio(h, ah) > 1.4:
                    return False
        # Control claramente "pequeño" (botón / icono / link / celda): UIA
        # nativo hace un gran trabajo y usa el punto clickable del control.
        if w <= 200 and h <= 120:
            return True
        # Contenedor enorme: no confiar en uia.Click() al centro.
        if w > 500 or h > 350:
            return False
        # Medianos: confiamos en UIA nativo. Es raro que acierte mal a este
        # tamaño si ya descartamos reescalado fuerte.
        return w <= 400 and h <= 250

    def _exact_click_from_uia(self, step: CompiledStep, uia_target: Any) -> Optional[Dict[str, int]]:
        """Calcula el pixel exacto donde clickar usando UIA + offset grabado.

        Flujo:
        - Obtiene el BoundingRectangle ACTUAL del elemento UIA.
        - Calcula su centro actual.
        - Aplica el `click_offset_within_bbox` grabado, escalado si el bbox
          cambió de tamaño respecto a cuando se grabó.

        Si el elemento UIA actual es "razonablemente parecido" al grabado
        (centro/tamaño no absurdo), devuelve el pixel final. Si algo no
        cuadra, devuelve None para que el caller use las coords absolutas.
        """
        tc = step.target_context
        off = tc.click_offset_within_bbox
        if not off:
            return None
        try:
            r = uia_target.BoundingRectangle
            left = int(getattr(r, "left", 0))
            top = int(getattr(r, "top", 0))
            right = int(getattr(r, "right", 0))
            bottom = int(getattr(r, "bottom", 0))
            w = max(0, right - left)
            h = max(0, bottom - top)
            if w <= 0 or h <= 0:
                return None
            # Protección: si UIA devuelve un bbox absurdamente grande
            # (p.ej. una Document completa), no nos fiamos: vale más clickar
            # en las coords absolutas grabadas que en el centro de un panel.
            if w > 800 or h > 600:
                return None
            cx = left + w // 2
            cy = top + h // 2
            anchor = tc.anchor_bbox or {}
            ow = max(1, int(anchor.get("width") or w))
            oh = max(1, int(anchor.get("height") or h))
            scale_x = w / ow
            scale_y = h / oh
            dx = int(round(int(off.get("dx", 0)) * scale_x))
            dy = int(round(int(off.get("dy", 0)) * scale_y))
            return {"x": cx + dx, "y": cy + dy}
        except Exception:
            return None

    def _absolute_coords_if_in_window(self, step: CompiledStep) -> Optional[Dict[str, int]]:
        """Devuelve las coords absolutas grabadas SI siguen cayendo dentro
        del bbox de la ventana activa actual (con tolerancia razonable).

        Esto captura el caso 95%: el usuario ejecuta la automatización en la
        misma máquina, mismo monitor, misma app en la misma posición que
        cuando grabó. En ese caso, las coords grabadas son EXACTAS y no
        tenemos por qué hacer template-match ni reconstruir coordenadas.
        """
        fb = step.target_context.fallback_coords
        if not fb:
            return None
        bbox = self._ctx.window_bbox
        if not bbox:
            # Sin bbox de ventana activa, confiamos en las coords grabadas
            # (mejor clickar donde el usuario grabó que no clickar).
            return fb
        try:
            x = int(fb.get("x", 0))
            y = int(fb.get("y", 0))
            left = int(bbox.get("left", 0))
            top = int(bbox.get("top", 0))
            width = int(bbox.get("width", 0))
            height = int(bbox.get("height", 0))
            # Margen de tolerancia: 20% de la dimensión (barras de título,
            # bordes del sistema operativo).
            tol_x = max(10, width // 5)
            tol_y = max(10, height // 5)
            if (left - tol_x) <= x <= (left + width + tol_x) and (top - tol_y) <= y <= (top + height + tol_y):
                return fb
        except Exception:
            return fb
        return None

    def _resolve_relative_window_coords(self, step: CompiledStep) -> Optional[Dict[str, int]]:
        """Reconstruye coords absolutas desde `(fx, fy)` relativas al bbox de la
        ventana actual. Mucho más robusto que coords absolutas puras cuando la
        ventana se movió, maximizó o reescaló.
        """
        rel = step.target_context.fallback_coords_relative_to_window
        if not rel:
            return None
        bbox = self._ctx.window_bbox
        if not bbox or bbox.get("width", 0) <= 0 or bbox.get("height", 0) <= 0:
            return None
        try:
            fx = float(rel.get("fx", 0.0))
            fy = float(rel.get("fy", 0.0))
            x = int(bbox["left"]) + int(bbox["width"] * max(0.0, min(1.0, fx)))
            y = int(bbox["top"]) + int(bbox["height"] * max(0.0, min(1.0, fy)))
            return {"x": x, "y": y}
        except Exception:
            return None

    def _resolve_vision(self, step: CompiledStep) -> Optional[Dict[str, int]]:
        """Busca imagen(es) de referencia preferentemente dentro de la ventana.

        Orden (más barato primero):
          1) snapshot_mid en región de ventana.
          2) snapshot_small en región de ventana.
          3) snapshot_mid en pantalla completa.
          4) snapshot_small en pantalla completa.
        """
        tc = step.target_context
        images = [
            (tc.image_ref_mid, 0.85),  # contexto medio: menos falsos positivos
            (tc.image_ref, 0.85),
        ]
        images = [(p, c) for p, c in images if p]
        if not images:
            return None
        try:
            import pyautogui  # type: ignore
        except Exception:
            return None

        region = None
        bbox = self._ctx.window_bbox
        if bbox and bbox.get("width", 0) > 50 and bbox.get("height", 0) > 50:
            region = (bbox["left"], bbox["top"], bbox["width"], bbox["height"])

        # 1-2) Dentro de la ventana
        for path, conf in images:
            try:
                loc = pyautogui.locateCenterOnScreen(path, confidence=conf, region=region)
                if loc:
                    return {"x": int(loc.x), "y": int(loc.y)}
            except Exception:
                continue

        # 3-4) Último recurso: pantalla completa (solo si estábamos acotados)
        if region:
            for path, conf in images:
                try:
                    loc = pyautogui.locateCenterOnScreen(path, confidence=conf)
                    if loc:
                        return {"x": int(loc.x), "y": int(loc.y)}
                except Exception:
                    continue
        return None

    # ─────────────────────────────────────────────────────────────
    # Acciones
    # ─────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────
    # Seguridad: nunca clickar sobre Nevlan
    # ─────────────────────────────────────────────────────────────
    def _safe_click_point(self, x: int, y: int,
                          step: Optional[CompiledStep] = None) -> Tuple[int, int]:
        """Verifica que (x, y) NO pertenezca a una ventana del propio
        Nevlan. Si pertenece, reactiva la ventana destino e intenta de
        nuevo. Si sigue cayendo en Nevlan, lanza RuntimeError para que
        el bucle de retry o el modo reparación se encarguen.
        """
        try:
            if not _HAS_W32:
                return int(x), int(y)
            if _is_point_owned_by_us(int(x), int(y)):
                if step is not None:
                    try:
                        self._activate_window_hint(step, wait_for_it=False)
                        time.sleep(0.12)
                    except Exception:
                        pass
                if _is_point_owned_by_us(int(x), int(y)):
                    raise RuntimeError(
                        f"Click sobre Nevlan rechazado en ({x},{y})."
                    )
        except RuntimeError:
            raise
        except Exception:
            # No hay W32 fiable → no podemos validar; permitimos el click.
            pass
        return int(x), int(y)

    def _execute_action(self, strategy: ActionStrategy, uia_target: Any,
                        coords: Optional[Dict], payload: Dict, vars: Dict,
                        step: Optional[CompiledStep] = None):
        try:
            import pyautogui  # type: ignore
        except Exception as e:
            raise RuntimeError(f"pyautogui no disponible: {e}")

        # Expansión simple de variables {{var}}
        def _expand(s: str) -> str:
            out = s or ""
            for k, v in vars.items():
                out = out.replace(f"{{{{{k}}}}}", str(v))
            return out

        if strategy == ActionStrategy.TYPE_TEXT:
            text = _expand(payload.get("text", ""))
            if uia_target:
                try:
                    uia_target.SetFocus()
                    time.sleep(_TYPE_FOCUS_PAUSE)
                except Exception:
                    pass
            pyautogui.typewrite(text, interval=_TYPE_INTERVAL)
            return

        if strategy == ActionStrategy.SET_FIELD_VALUE:
            text = _expand(payload.get("text", ""))
            self._set_field_value(text, uia_target, coords, step, payload)
            return

        if strategy == ActionStrategy.AGENT_PROMPT:
            prompt = _expand(payload.get("text", ""))
            log.info(f"Agent prompt: {prompt}")
            try:
                from app.brain.agent import agent
                agent.process_user_input(
                    f"[VIBE STEP EN VIVO] Ejecuta ahora: '{prompt}'"
                )
            except Exception as e:
                log.error(f"Agent prompt falló: {e}")
            return

        if strategy in (ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK, ActionStrategy.RIGHT_CLICK):
            # Phase 7: si el target es claramente web y tenemos locators
            # válidos en target_context.web_data, atajamos a Playwright
            # ANTES de cualquier intento por mouse/coords. Esto cubre el
            # caso "el resolver no entró en este path" (p.ej. fallback
            # legacy) pero el target es 100% web.
            if step is not None and self._is_web_process(step):
                web = step.target_context.web_data or {}
                if web.get("locators"):
                    try:
                        if strategy == ActionStrategy.CLICK and self._web_click(web, "left"):
                            return
                        if strategy == ActionStrategy.DOUBLE_CLICK and self._web_double_click(web):
                            return
                        if strategy == ActionStrategy.RIGHT_CLICK and self._web_right_click(web):
                            return
                    except Exception as e:
                        log.debug(f"web click via locator falló, fallback: {e}")

            # Orden de prioridad (fidelidad primero, robustez siempre):
            #
            # 1) Si UIA encontró el elemento EXACTO y es pequeño (≈ botón/enlace
            #    individual) Y su bbox es similar al bbox grabado → usamos
            #    `uia.Click()`. Es el camino más robusto: UIA internamente pide
            #    `GetClickablePoint()` al control, que es donde el SO considera
            #    válido el click (evita esquinas transparentes, bordes, etc.).
            #
            # 2) Si UIA encontró el elemento pero cambió de tamaño/posición
            #    respecto a lo grabado (p.ej. ventana se reescaló), clickamos
            #    en centro+offset para replicar el mismo punto relativo.
            #
            # 3) Sin UIA válido → pyautogui.click(x,y) con las coords grabadas
            #    (o las resueltas por rel_window / icon_validated / vision).
            #
            # 4) Último recurso: uia.Click() al centro si no hay coords.
            use_uia_native = self._should_use_uia_native_click(step, uia_target)
            exact = None
            if (uia_target is not None) and (step is not None) and (not use_uia_native):
                exact = self._exact_click_from_uia(step, uia_target)

            # 1) UIA nativo — camino más seguro para elementos pequeños.
            if use_uia_native and uia_target is not None:
                try:
                    if strategy == ActionStrategy.CLICK:
                        uia_target.Click()
                    elif strategy == ActionStrategy.DOUBLE_CLICK:
                        uia_target.DoubleClick()
                    else:
                        uia_target.RightClick()
                    return
                except Exception as e:
                    log.debug(f"UIA click nativo falló, caigo a coords: {e}")

            # 2) Coords calculadas por offset (ventana se movió/reescaló).
            click_coords = exact if exact is not None else coords
            if click_coords:
                x, y = int(click_coords["x"]), int(click_coords["y"])
                # Guardia de seguridad: si el punto cae sobre Nevlan, abortar.
                x, y = self._safe_click_point(x, y, step)
                btn_name = "right" if strategy == ActionStrategy.RIGHT_CLICK else "left"
                clicks = 2 if strategy == ActionStrategy.DOUBLE_CLICK else 1
                try:
                    pyautogui.click(x=x, y=y, clicks=clicks, interval=0.05, button=btn_name)
                    return
                except Exception:
                    from pynput.mouse import Controller, Button
                    mouse = Controller()
                    mouse.position = (x, y)
                    btn = Button.right if strategy == ActionStrategy.RIGHT_CLICK else Button.left
                    mouse.click(btn, clicks)
                    return

            # 4) Sin nada: uia.Click() al centro como último intento.
            if uia_target is not None:
                try:
                    if strategy == ActionStrategy.CLICK:
                        uia_target.Click()
                    elif strategy == ActionStrategy.DOUBLE_CLICK:
                        uia_target.DoubleClick()
                    else:
                        uia_target.RightClick()
                    return
                except Exception as e:
                    log.debug(f"UIA click último recurso falló: {e}")
            raise RuntimeError("Sin target ni coordenadas para click")

        if strategy == ActionStrategy.SEND_HOTKEY:
            self._execute_hotkey(payload)
            return

        if strategy == ActionStrategy.WAIT_FOR_STATE:
            ms = int(payload.get("timeout_ms") or 500)
            time.sleep(ms / 1000.0)
            return

        if strategy == ActionStrategy.SCROLL:
            self._execute_scroll(payload)
            return

        if strategy == ActionStrategy.DRAG_DROP:
            self._execute_drag(payload)
            return

        if strategy == ActionStrategy.NAVIGATE_OR_SEARCH:
            # Compiler V3 fusiona Ctrl+L + texto + Enter en un solo paso.
            # Si tenemos una página web activa, hacemos page.goto/fill;
            # en otro caso, replay manual: ctrl+l + paste + enter.
            text = str(payload.get("text") or "")
            if not text:
                return
            ok = False
            try:
                from app.skills.tools.web import _get_page  # type: ignore
                page = _get_page()
                # Si ya parece una URL (contiene "://" o termina en TLD
                # común), navegamos; si no, lo escribimos en la barra
                # de direcciones (que en navegadores reales hace
                # búsqueda Google/Bing si no es URL).
                looks_url = "://" in text or "." in text.split("/")[0]
                if looks_url:
                    target = text if "://" in text else f"https://{text}"
                    page.goto(target, timeout=8000)
                    ok = True
                else:
                    # Sin browser CDP confiable usamos fallback teclado.
                    raise RuntimeError("not a URL")
            except Exception:
                # Fallback teclado: Ctrl+L → paste → Enter.
                try:
                    import pyautogui  # type: ignore
                    pyautogui.hotkey("ctrl", "l")
                    time.sleep(0.05)
                    if self._set_clipboard(text):
                        pyautogui.hotkey("ctrl", "v")
                    else:
                        pyautogui.typewrite(text, interval=_TYPE_INTERVAL)
                    time.sleep(0.05)
                    pyautogui.press("enter")
                    ok = True
                except Exception as e:
                    log.debug(f"NAVIGATE_OR_SEARCH fallback falló: {e}")
            if not ok:
                raise RuntimeError("NAVIGATE_OR_SEARCH no pudo ejecutarse")
            return

        if strategy == ActionStrategy.SUBMIT_SEARCH:
            # Submit standalone (no fusionado con SET_FIELD_VALUE):
            # presionamos Enter sobre el target/foco actual y, si es
            # un site conocido (YouTube, Google), validamos que la
            # URL final tenga el ``search_query`` esperado.
            try:
                import pyautogui  # type: ignore
                pyautogui.press("enter")
                self._validate_search_completed(payload)
            except Exception as e:
                raise RuntimeError(f"SUBMIT_SEARCH falló: {e}")
            return

        if strategy == ActionStrategy.CONFIRM_DIALOG:
            # answer: "accept" → Enter; "cancel" → Esc.
            answer = str(payload.get("answer") or "accept").lower()
            try:
                import pyautogui  # type: ignore
                if answer in ("cancel", "no", "deny", "esc"):
                    pyautogui.press("escape")
                else:
                    pyautogui.press("enter")
            except Exception as e:
                raise RuntimeError(f"CONFIRM_DIALOG falló: {e}")
            return

        if strategy == ActionStrategy.SCROLL_UNTIL_VISIBLE:
            # Si hay locator del target a mostrar, intentamos
            # scroll_into_view_if_needed (instantáneo). En otro caso,
            # ejecutamos un scroll proporcional al payload original.
            scroll_until = (payload or {}).get("scroll_until") or {}
            web_done = False
            if scroll_until.get("web_test_id") or scroll_until.get("web_role"):
                try:
                    from app.skills.tools.web import _get_page  # type: ignore
                    page = _get_page()
                    spec = None
                    if scroll_until.get("web_test_id"):
                        spec = {"kind": "test_id",
                                "value": scroll_until["web_test_id"]}
                    elif scroll_until.get("web_role"):
                        spec = {"kind": "role",
                                "role": scroll_until["web_role"],
                                "name": scroll_until.get("web_name")}
                    if spec:
                        loc = self._build_web_locator(page, spec)
                        if loc is not None and loc.count() > 0:
                            loc.first.scroll_into_view_if_needed(timeout=2000)
                            web_done = True
                except Exception as e:
                    log.debug(f"scroll_into_view falló: {e}")
            if not web_done:
                self._execute_scroll(payload)
            return

        if strategy == ActionStrategy.LAUNCH_APP:
            self._execute_launch_app(payload)
            return

        log.debug(f"Acción {strategy} sin ejecución específica; ignorada.")

    # ─────────────────────────────────────────────────────────────
    # LAUNCH_APP — abrir/enfocar una app
    # ─────────────────────────────────────────────────────────────
    # Estrategia (mejor a peor):
    #   1) Si la app ya corre (psutil) → enfocamos su ventana.
    #   2) ``os.startfile``/``Start-Process`` por nombre canónico —
    #      no depende de Windows Search.
    #   3) Atajo Win+R + ejecutable.exe (rápido, no usa Search).
    #   4) Fallback bruto: Win → escribir → Enter (Windows Search).
    # En todos los casos esperamos a que aparezca un proceso de la
    # app (timeout 4s, poll 200ms) antes de declarar éxito, en vez
    # de los sleeps fijos de la versión anterior.
    # ─────────────────────────────────────────────────────────────

    def _validate_search_completed(
        self, payload: Dict[str, Any], timeout_ms: int = 4000
    ) -> bool:
        """Best-effort: tras un submit (Enter en searchbox), verifica
        que el navegador navegó a una URL de resultados.

        - YouTube: URL debe contener ``/results`` o ``search_query=``.
        - Google: URL debe contener ``/search`` o ``q=``.
        - Otros sitios: si tenemos un ``site_label``, esperamos solo a
          que la página haya cambiado de URL respecto al click inicial.

        Si Playwright no está disponible o el CDP no responde, retorna
        False sin fallar (el Enter ya se ejecutó). Bug 2026-05-03:
        antes el player no validaba después del submit y silenciosamente
        ejecutaba "fragmentos" parciales como búsquedas separadas.
        """
        site = (payload or {}).get("search_site") or ""
        if not site:
            return True  # nada que validar
        try:
            from app.skills.tools.web import _get_page  # type: ignore
            page = _get_page(connect_only=True)
        except Exception:
            return False
        if page is None:
            return False
        deadline = time.time() + (timeout_ms / 1000.0)
        while time.time() < deadline:
            try:
                url = (page.url or "").lower()
                if site == "youtube" and (
                    "/results" in url or "search_query=" in url
                ):
                    return True
                if site == "google" and (
                    "/search" in url or "q=" in url.split("?", 1)[-1]
                ):
                    return True
                # Para otros sitios, cualquier cambio de URL ya cuenta.
                if site not in ("youtube", "google") and url:
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        log.debug(
            f"_validate_search_completed: timeout esperando navegación "
            f"para site={site}"
        )
        return False

    def _execute_launch_app(self, payload: Dict[str, Any]) -> None:
        from app.services.missions.launch_apps import (
            resolve_app_alias, canonical_app_name, is_app_running,
        )

        raw_name = str(payload.get("app_name") or "").strip()
        if not raw_name:
            raise RuntimeError("LAUNCH_APP sin app_name en payload")

        canonical = canonical_app_name(raw_name) or raw_name
        alias = resolve_app_alias(raw_name)
        target_procs = list(alias.process_names or ())

        # 1) ¿Ya está corriendo?
        running = is_app_running(raw_name) if target_procs else None
        if running is True:
            self._focus_running_app(target_procs)
            log.info(
                f"LAUNCH_APP: '{canonical}' ya estaba abierto, enfocando."
            )
            return

        # 2) os.startfile por nombre canónico/exe — el más confiable
        # cuando lo conocemos.
        for proc in target_procs:
            if self._try_start_executable(proc):
                if self._wait_for_process_ready(target_procs, timeout_s=4.0):
                    log.info(
                        f"LAUNCH_APP: '{canonical}' lanzado vía start_executable ({proc})."
                    )
                    return

        # 3) Win+R + exe.
        if target_procs:
            for proc in target_procs:
                if self._try_run_dialog(proc):
                    if self._wait_for_process_ready(target_procs, timeout_s=4.0):
                        log.info(
                            f"LAUNCH_APP: '{canonical}' lanzado vía Win+R."
                        )
                        return

        # 4) Fallback bruto — Windows Search.
        try:
            import pyautogui  # type: ignore
        except Exception as e:
            raise RuntimeError(
                f"LAUNCH_APP falló para '{canonical}': pyautogui no disponible ({e})"
            )

        try:
            pyautogui.hotkey("win")
            self._wait_window_search_ready(timeout_s=1.5)
            pyautogui.typewrite(canonical, interval=0.03)
            time.sleep(0.35)  # menor que antes; basta para el ranking
            pyautogui.press("enter")
            if not self._wait_for_process_ready(
                target_procs, timeout_s=4.0
            ):
                # Sin tabla de procesos confiable, esperamos mínimo
                # que la UI haya tenido tiempo de reaccionar.
                time.sleep(0.8)
            log.info(
                f"LAUNCH_APP: '{canonical}' ejecutado via Windows Search "
                f"(raw='{raw_name}')."
            )
        except Exception as e:
            raise RuntimeError(
                f"LAUNCH_APP falló para '{canonical}': {e}"
            )

    @staticmethod
    def _try_start_executable(executable: str) -> bool:
        """Intenta abrir ``executable`` (nombre o path). True si no falla."""
        try:
            import subprocess
            import os
            if os.name == "nt":
                # ``start`` busca en PATH y App Paths del registry.
                subprocess.Popen(
                    ["cmd", "/c", "start", "", executable],
                    shell=False,
                )
                return True
            subprocess.Popen([executable])
            return True
        except Exception as e:
            log.debug(f"start_executable {executable!r}: {e}")
            return False

    def _try_run_dialog(self, executable: str) -> bool:
        """Atajo Win+R + escribir + Enter. Devuelve True si llega al final
        sin error (no garantiza que la app abrió)."""
        try:
            import pyautogui  # type: ignore
            pyautogui.hotkey("win", "r")
            time.sleep(0.3)
            pyautogui.typewrite(executable, interval=0.03)
            time.sleep(0.15)
            pyautogui.press("enter")
            return True
        except Exception as e:
            log.debug(f"run_dialog {executable!r}: {e}")
            return False

    @staticmethod
    def _wait_window_search_ready(timeout_s: float = 1.5) -> None:
        """Pequeño poll esperando a que Windows Search abra (UIA cuando esté)."""
        end = time.time() + max(0.05, float(timeout_s))
        try:
            import uiautomation as auto  # type: ignore
        except Exception:
            time.sleep(min(0.5, timeout_s))
            return
        while time.time() < end:
            try:
                with auto.UIAutomationInitializerInThread():  # type: ignore[attr-defined]
                    el = auto.GetFocusedControl()  # type: ignore[attr-defined]
                    name = ""
                    try:
                        name = (el.Name or "").strip().lower()
                    except Exception:
                        pass
                    if name and (
                        "search" in name or "buscar" in name
                    ):
                        return
            except Exception:
                pass
            time.sleep(0.08)

    @staticmethod
    def _wait_for_process_ready(
        process_names: List[str], timeout_s: float = 4.0
    ) -> bool:
        """Polling para detectar que el proceso de la app aparece.

        Si no tenemos psutil o la tabla de procesos está vacía,
        devolvemos False (el caller decide qué hacer)."""
        if not process_names:
            return False
        try:
            import psutil  # type: ignore
        except Exception:
            return False
        wanted = {p.lower() for p in process_names}
        end = time.time() + max(0.1, float(timeout_s))
        while time.time() < end:
            try:
                for proc in psutil.process_iter(attrs=("name",)):
                    try:
                        pname = (proc.info.get("name") or "").lower()
                    except Exception:
                        continue
                    if pname in wanted:
                        return True
            except Exception:
                return False
            time.sleep(0.15)
        return False

    @staticmethod
    def _focus_running_app(process_names: List[str]) -> None:
        """Activa la primera ventana visible cuyo proceso matchee."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except Exception:
            return
        try:
            import psutil  # type: ignore
        except Exception:
            psutil = None  # type: ignore[assignment]

        wanted = {p.lower() for p in (process_names or ())}

        @ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            wintypes.HWND,
            wintypes.LPARAM,
        )
        def _enum(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if not pid.value:
                    return True
                if psutil is None:
                    return True
                try:
                    pname = psutil.Process(pid.value).name().lower()
                except Exception:
                    return True
                if pname in wanted:
                    user32.SetForegroundWindow(hwnd)
                    return False  # stop enum
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(_enum, 0)
        except Exception as e:
            log.debug(f"focus_running_app: {e}")

    # ─────────────────────────────────────────────────────────────
    # SET_FIELD_VALUE — escribe el valor final del campo de forma
    # atómica y robusta. Cadena de fallbacks (de más rápido/fiable
    # a más bruto):
    #   1) Web locator (Playwright fill) — si target_context.web_data
    #      tiene `locators` válidos y el process es Chrome/Edge/FF.
    #   2) UIA ValuePattern.SetValue — instantáneo, no genera teclas
    #      "fantasma" en apps web.
    #   3) Focus + Ctrl+A + Ctrl+V (clipboard paste).
    #   4) Focus + pyautogui.typewrite (último recurso).
    # Tras escribir, validamos con ValuePattern.Value cuando se puede
    # leer, y publicamos un evento amigable al bus.
    # ─────────────────────────────────────────────────────────────

    def _set_field_value(self, text: str, uia_target: Any,
                         coords: Optional[Dict[str, int]],
                         step: Optional[CompiledStep],
                         payload: Dict[str, Any]) -> None:
        import pyautogui  # type: ignore
        label = (payload or {}).get("label") or ""
        web_role = ((payload or {}).get("web_role_hint") or "").lower()
        ctype = ((payload or {}).get("control_type_hint") or "").lower()
        # Compiler V3 detecta SET_FIELD_VALUE seguido de Enter (caja de
        # búsqueda, login, etc.) y lo marca como `submit_after: True`.
        # Tras escribir el valor, pulsamos Enter una vez (preferimos
        # locator.press('Enter') si es web; pyautogui en otro caso).
        submit_after = bool((payload or {}).get("submit_after"))

        def _maybe_submit() -> None:
            if not submit_after:
                return
            # Web: usar locator.press('Enter') si tenemos web data;
            # mantiene el flujo dentro del frame correcto.
            try:
                if step is not None:
                    web = step.target_context.web_data or {}
                    if web.get("locators") and self._is_web_process(step):
                        if self._web_press_enter(web):
                            return
            except Exception as e:
                log.debug(f"web press Enter falló: {e}")
            # Desktop / fallback: pyautogui.
            try:
                pyautogui.press("enter")
            except Exception as e:
                log.debug(f"pyautogui Enter falló: {e}")

        method_used = ""
        validated = False

        try:
            self._publish_event(
                "mission.field_writing",
                {"label": label or "campo", "value_preview": text[:40]},
            )
        except Exception:
            pass

        # 1) ── Web locator (Playwright fill) ─────────────────────
        if step is not None:
            web = step.target_context.web_data or {}
            if web.get("locators") and self._is_web_process(step):
                try:
                    if self._web_fill(web, text):
                        method_used = "web_fill"
                        # Validación: leer value via DOM.
                        try:
                            actual = self._web_read_value(web)
                            if actual is not None and actual.strip() == text.strip():
                                validated = True
                        except Exception:
                            pass
                        self._record_method_outcome(step, "web_fill", True)
                        _maybe_submit()
                        self._announce_field_done(label, text, method_used, validated)
                        return
                except Exception as e:
                    log.debug(f"web_fill falló: {e}")
                    self._record_method_outcome(step, "web_fill", False)

        # 2) ── UIA ValuePattern.SetValue ─────────────────────────
        if HAS_UIA and uia_target is not None and ctype != "comboboxcontrol":
            # ComboBox suele no aceptar SetValue limpio: dejamos paste/type.
            try:
                try:
                    uia_target.SetFocus()
                except Exception:
                    pass
                vp = None
                try:
                    vp = uia_target.GetValuePattern()  # type: ignore[attr-defined]
                except Exception:
                    vp = None
                if vp and not getattr(vp, "IsReadOnly", False):
                    try:
                        vp.SetValue(text)
                        method_used = "uia_setvalue"
                        # Validamos releyendo Value.
                        try:
                            actual = str(getattr(vp, "Value", "") or "")
                            if actual.strip() == text.strip():
                                validated = True
                        except Exception:
                            pass
                        if step is not None:
                            self._record_method_outcome(step, "uia_setvalue", True)
                        _maybe_submit()
                        self._announce_field_done(label, text, method_used, validated)
                        return
                    except Exception as e:
                        log.debug(f"UIA SetValue falló: {e}")
                        if step is not None:
                            self._record_method_outcome(step, "uia_setvalue", False)
            except Exception as e:
                log.debug(f"ValuePattern path falló: {e}")

        # 3) ── Focus + Ctrl+A + Ctrl+V (clipboard paste) ─────────
        focused = False
        if uia_target is not None:
            try:
                uia_target.SetFocus()
                focused = True
            except Exception:
                pass
        if not focused and coords:
            try:
                cx, cy = self._safe_click_point(int(coords["x"]), int(coords["y"]), step)
                pyautogui.click(cx, cy)
                focused = True
            except Exception:
                pass
        time.sleep(_TYPE_FOCUS_PAUSE)

        if self._set_clipboard(text):
            try:
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.03)
                pyautogui.hotkey("ctrl", "v")
                method_used = "clipboard_paste"
                # Best-effort validación.
                if HAS_UIA and uia_target is not None:
                    try:
                        vp = uia_target.GetValuePattern()  # type: ignore[attr-defined]
                        if vp:
                            actual = str(getattr(vp, "Value", "") or "")
                            if actual.strip() == text.strip():
                                validated = True
                    except Exception:
                        pass
                if step is not None:
                    self._record_method_outcome(step, "clipboard_paste", True)
                _maybe_submit()
                self._announce_field_done(label, text, method_used, validated)
                return
            except Exception as e:
                log.debug(f"paste falló: {e}")
                if step is not None:
                    self._record_method_outcome(step, "clipboard_paste", False)

        # 4) ── pyautogui.typewrite (último recurso) ──────────────
        try:
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.03)
        except Exception:
            pass
        try:
            pyautogui.typewrite(text, interval=_TYPE_INTERVAL)
            method_used = "typewrite"
            if step is not None:
                self._record_method_outcome(step, "typewrite", True)
            _maybe_submit()
            self._announce_field_done(label, text, method_used, validated)
            return
        except Exception as e:
            if step is not None:
                self._record_method_outcome(step, "typewrite", False)
            raise RuntimeError(f"SET_FIELD_VALUE falló en todos los métodos: {e}")

    # ─────────────────────────────────────────────────────────────
    # Phase 7 — Web action methods (Playwright locator first)
    # ─────────────────────────────────────────────────────────────
    # Cuando TargetResolverV2 devuelve target_kind == web_locator NO
    # movemos el mouse ni usamos coords: ejecutamos directamente con
    # el ``Locator`` que el resolver ya validó (visible/único). Esto
    # cubre botones, links, campos, menús web, checkboxes, radios,
    # tabs y dropdowns sin frágilezas de coords.

    def _web_action_with_locator(self, locator, action: ActionStrategy) -> None:
        """Ejecuta CLICK / DOUBLE_CLICK / RIGHT_CLICK con un Locator
        Playwright pre-resuelto. Hace ``scroll_into_view`` automático
        (auto-wait) y respeta los timeouts cortos del PRD §6."""
        try:
            try:
                locator.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            if action == ActionStrategy.CLICK:
                locator.click(timeout=2500)
                return
            if action == ActionStrategy.DOUBLE_CLICK:
                locator.dblclick(timeout=2500)
                return
            if action == ActionStrategy.RIGHT_CLICK:
                locator.click(button="right", timeout=2500)
                return
        except Exception as e:
            log.debug(f"web locator action {action} falló: {e}")
            raise

    def _web_click(self, web: Dict[str, Any], click_type: str = "left") -> bool:
        """Click web genérico por web_data['locators']. Devuelve True si
        algún locator matcheó y se pudo clickar."""
        try:
            from app.skills.tools.web import _get_page  # type: ignore
            page = _get_page()
        except Exception:
            return False
        for spec in web.get("locators") or []:
            loc = self._build_web_locator(page, spec)
            if loc is None:
                continue
            try:
                if loc.count() == 0:
                    continue
                first = loc.first
                first.scroll_into_view_if_needed(timeout=1500)
                if click_type == "right":
                    first.click(button="right", timeout=2500)
                else:
                    first.click(timeout=2500)
                return True
            except Exception:
                continue
        return False

    def _web_double_click(self, web: Dict[str, Any]) -> bool:
        try:
            from app.skills.tools.web import _get_page  # type: ignore
            page = _get_page()
        except Exception:
            return False
        for spec in web.get("locators") or []:
            loc = self._build_web_locator(page, spec)
            if loc is None:
                continue
            try:
                if loc.count() == 0:
                    continue
                first = loc.first
                first.scroll_into_view_if_needed(timeout=1500)
                first.dblclick(timeout=2500)
                return True
            except Exception:
                continue
        return False

    def _web_right_click(self, web: Dict[str, Any]) -> bool:
        return self._web_click(web, click_type="right")

    def _web_wait_expected_state(self, web: Dict[str, Any],
                                  expected: Optional[str],
                                  timeout_ms: int = 1500) -> bool:
        """Validación post-acción para web. ``expected`` puede ser:
          - "url_changed"      → la URL distinta a la grabada.
          - "element_appears"  → el primer locator vuelve a ser visible.
          - "field_value_set"  → el input tiene un value no vacío.
          - "dialog_closed"    → ningún dialog/modal abierto.
        Best-effort: jamás bloquea más de ``timeout_ms``.
        """
        if not expected:
            return True
        try:
            from app.skills.tools.web import _get_page  # type: ignore
            page = _get_page()
        except Exception:
            return False
        try:
            if expected == "url_changed":
                grabbed = (web.get("url") or "")
                t0 = time.perf_counter()
                while (time.perf_counter() - t0) * 1000 < timeout_ms:
                    if page.url and page.url != grabbed:
                        return True
                    time.sleep(0.05)
                return False
            if expected == "element_appears":
                for spec in web.get("locators") or []:
                    loc = self._build_web_locator(page, spec)
                    if loc is None:
                        continue
                    try:
                        loc.first.wait_for(state="visible",
                                            timeout=timeout_ms)
                        return True
                    except Exception:
                        continue
                return False
            if expected == "field_value_set":
                v = self._web_read_value(web)
                return bool(v)
            if expected == "dialog_closed":
                # Heurística: no hay role=dialog visible.
                try:
                    return page.get_by_role("dialog").count() == 0
                except Exception:
                    return True
        except Exception as e:
            log.debug(f"_web_wait_expected_state {expected}: {e}")
        return False

    def _set_clipboard(self, text: str) -> bool:
        """Pone `text` en el portapapeles. Probamos varias bibliotecas."""
        try:
            import pyperclip  # type: ignore
            pyperclip.copy(text)
            return True
        except Exception:
            pass
        try:
            import subprocess
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            p.communicate(input=text.encode("utf-16-le"))
            return p.returncode == 0
        except Exception:
            pass
        return False

    def _is_web_process(self, step: CompiledStep) -> bool:
        proc = (step.target_context.process_name or "").lower()
        return proc in ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe")

    def _web_fill(self, web: Dict[str, Any], text: str) -> bool:
        """Ejecuta fill() vía Playwright sobre el primer locator que matchee.
        Devuelve True si se pudo escribir."""
        try:
            from app.skills.tools.web import _get_page  # type: ignore
        except Exception as e:
            log.debug(f"playwright bridge no disponible: {e}")
            return False
        try:
            page = _get_page()
        except Exception as e:
            log.debug(f"no hay página activa: {e}")
            return False
        for locator_spec in web.get("locators") or []:
            loc = self._build_web_locator(page, locator_spec)
            if loc is None:
                continue
            try:
                if loc.count() == 0:
                    continue
                first = loc.first
                first.scroll_into_view_if_needed(timeout=1500)
                first.fill(text, timeout=2500)
                return True
            except Exception:
                continue
        return False

    def _web_read_value(self, web: Dict[str, Any]) -> Optional[str]:
        try:
            from app.skills.tools.web import _get_page  # type: ignore
            page = _get_page()
        except Exception:
            return None
        for locator_spec in web.get("locators") or []:
            loc = self._build_web_locator(page, locator_spec)
            if loc is None:
                continue
            try:
                if loc.count() == 0:
                    continue
                v = loc.first.input_value(timeout=800)
                return v
            except Exception:
                continue
        return None

    def _web_press_enter(self, web: Dict[str, Any]) -> bool:
        """Presiona Enter sobre el primer locator que matchee. Útil
        para SUBMIT_SEARCH (compiler V3 marca el SET_FIELD_VALUE con
        ``submit_after: True``).
        """
        try:
            from app.skills.tools.web import _get_page  # type: ignore
            page = _get_page()
        except Exception:
            return False
        for locator_spec in web.get("locators") or []:
            loc = self._build_web_locator(page, locator_spec)
            if loc is None:
                continue
            try:
                if loc.count() == 0:
                    continue
                loc.first.press("Enter", timeout=1000)
                return True
            except Exception:
                continue
        # Fallback: Enter sobre la página entera (recoge focused).
        try:
            page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    @staticmethod
    def _build_web_locator(page, spec: Dict[str, Any]):
        """Construye un Locator a partir de un spec serializable."""
        try:
            kind = (spec.get("kind") or "").lower()
            if kind == "role":
                name = spec.get("name")
                role = spec.get("role")
                if not role:
                    return None
                if name:
                    return page.get_by_role(role, name=name)
                return page.get_by_role(role)
            if kind == "label":
                return page.get_by_label(spec.get("value", ""))
            if kind == "placeholder":
                return page.get_by_placeholder(spec.get("value", ""))
            if kind == "test_id":
                return page.get_by_test_id(spec.get("value", ""))
            if kind == "text":
                return page.get_by_text(spec.get("value", ""), exact=False)
            if kind == "css":
                return page.locator(spec.get("value", ""))
            if kind == "xpath":
                return page.locator(f"xpath={spec.get('value', '')}")
        except Exception:
            return None
        return None

    def _record_method_outcome(self, step: CompiledStep, method: str, ok: bool) -> None:
        """Aprende para futuros replays: incrementa éxito/fracaso por método."""
        try:
            tc = step.target_context
            conf = dict(tc.confidence or {})
            stats = dict(conf.get("success_by_method") or {})
            entry = dict(stats.get(method) or {"ok": 0, "fail": 0})
            if ok:
                entry["ok"] = int(entry.get("ok", 0)) + 1
            else:
                entry["fail"] = int(entry.get("fail", 0)) + 1
            stats[method] = entry
            conf["success_by_method"] = stats
            conf["last_success_method"] = method if ok else conf.get("last_success_method", "")
            tc.confidence = conf
        except Exception:
            pass

    def _announce_field_done(self, label: str, text: str, method: str,
                             validated: bool) -> None:
        try:
            short = text if len(text) <= 40 else text[:40] + "…"
            self._publish_event(
                "mission.field_done",
                {
                    "label": label or "campo",
                    "value_preview": short,
                    "method": method,
                    "validated": validated,
                },
            )
        except Exception:
            pass

    def _execute_scroll(self, payload: Dict[str, Any]):
        """Scroll vertical (dy > 0 arriba, dy < 0 abajo). pyautogui.scroll usa unidades +up/-down."""
        import pyautogui  # type: ignore
        dy = int(payload.get("dy", 0))
        dx = int(payload.get("dx", 0))
        x = payload.get("x")
        y = payload.get("y")
        try:
            if x is not None and y is not None:
                pyautogui.moveTo(int(x), int(y), duration=0.05)
        except Exception:
            pass
        # pyautogui.scroll toma clicks. Usamos factor 100 por tick.
        try:
            if dy:
                pyautogui.scroll(dy * 100)
            if dx:
                pyautogui.hscroll(dx * 100)
        except Exception as e:
            log.debug(f"scroll: {e}")

    def _execute_drag(self, payload: Dict[str, Any]):
        """Drag & drop: start → end con duración (segundos)."""
        import pyautogui  # type: ignore
        sx = int(payload.get("start_x", 0))
        sy = int(payload.get("start_y", 0))
        ex = int(payload.get("end_x", 0))
        ey = int(payload.get("end_y", 0))
        dur = float(payload.get("duration", 0.4))
        try:
            pyautogui.moveTo(sx, sy, duration=0.08)
            pyautogui.mouseDown(button="left")
            pyautogui.moveTo(ex, ey, duration=max(0.1, dur))
            pyautogui.mouseUp(button="left")
        except Exception as e:
            log.error(f"drag&drop: {e}")

    def _execute_hotkey(self, payload: Dict[str, Any]):
        """
        Soporta:
          {"combo": True, "keys": ["ctrl","s"]}        → pyautogui.hotkey
          {"keys": ["tab","tab","enter"]}               → press sequence
          {"key": "tab", "repeat": 3}                   → press repetido
          {"hotkey": "tab"}                             → legacy
        """
        import pyautogui  # type: ignore

        if not payload:
            payload = {}

        interval = float(payload.get("interval_ms", _SEQ_DEFAULT_INTERVAL * 1000)) / 1000.0
        if interval < 0:
            interval = _SEQ_DEFAULT_INTERVAL

        keys = normalize_keys(payload.get("keys") or [])
        combo = bool(payload.get("combo"))
        repeat = 0
        try:
            repeat = int(payload.get("repeat") or 0)
        except Exception:
            repeat = 0
        single_key = _normalize_token(str(payload.get("key") or ""))
        legacy = _normalize_token(str(payload.get("hotkey") or ""))

        # 1. Combo simultáneo (opcionalmente repetido)
        if combo and len(keys) >= 2:
            reps = max(1, repeat or 1)
            try:
                for i in range(reps):
                    pyautogui.hotkey(*keys)
                    if i < reps - 1:
                        time.sleep(interval or _REPEAT_DEFAULT_INTERVAL)
                return
            except Exception as e:
                log.error(f"hotkey combo fallo {keys}: {e}")
                return

        # 2. Tecla única repetida N veces
        if single_key and repeat > 1:
            for i in range(repeat):
                pyautogui.press(single_key)
                if i < repeat - 1:
                    time.sleep(interval or _REPEAT_DEFAULT_INTERVAL)
            return

        # 2b. legacy "ctrl+s" en string → convertir a combo
        if legacy and "+" in legacy:
            parts = [p.strip() for p in legacy.split("+") if p.strip()]
            parts = normalize_keys(parts)
            if len(parts) >= 2:
                reps = max(1, repeat or 1)
                try:
                    for i in range(reps):
                        pyautogui.hotkey(*parts)
                        if i < reps - 1:
                            time.sleep(interval or _REPEAT_DEFAULT_INTERVAL)
                    return
                except Exception as e:
                    log.error(f"hotkey legacy combo fallo {parts}: {e}")
                    return

        # 3. Secuencia explícita
        if keys:
            for i, k in enumerate(keys):
                pyautogui.press(k)
                if i < len(keys) - 1:
                    time.sleep(interval or _SEQ_DEFAULT_INTERVAL)
            return

        # 4. Tecla única
        if single_key:
            pyautogui.press(single_key)
            return
        if legacy:
            pyautogui.press(legacy)
            return

        log.debug("send_hotkey sin teclas — ignorado")

    def _publish_event(self, name: str, payload: Dict):
        bus.publish(SystemEvent(name=name, payload=payload, source="mission_player"))

    # ─────────────────────────────────────────────────────────────
    # Ejecución parcial / prueba de un paso
    # ─────────────────────────────────────────────────────────────

    def replay_partial(self, up_to_step: int, variables: Dict[str, str] = None):
        if not self.execution:
            self.start_execution()
        variables = variables or {}
        graph = self.mission.compiled_execution_graph
        for step_idx in range(min(up_to_step, len(graph))):
            if self._stop_requested:
                break
            step = graph[step_idx]
            self._publish_event(
                "mission.step_start",
                {"step_index": step_idx, "total_steps": up_to_step,
                 "strategy": step.action_strategy.value},
            )
            try:
                self._execute_compiled_step(step, variables)
                self._publish_event("mission.step_ok", {"step_index": step_idx})
            except Exception as e:
                log.warning(f"replay_partial falló en {step_idx}: {e}")
                raise
        self._running = False

    def replay_from(self, from_step: int, variables: Dict[str, str] = None) -> MissionExecution:
        """Reanuda desde ``from_step`` hasta el final (misma corrida/interacción que ``replay``)."""
        if not self.execution:
            self.start_execution()
        return self.replay(
            variables=variables or {}, start_step_idx=from_step, auto_confirm=False
        )

    def test_single_step(self, step_index: int, variables: Dict[str, str] = None) -> Tuple[bool, str]:
        variables = variables or {}
        graph = self.mission.compiled_execution_graph
        if step_index >= len(graph):
            return False, f"Paso {step_index} fuera de rango ({len(graph)} pasos)"
        step = graph[step_index]
        try:
            self._execute_compiled_step(step, variables)
            return True, ""
        except Exception as e:
            return False, str(e)


def replay_mission(mission: Mission, auto_confirm: bool = False,
                   variables: Dict[str, str] = None) -> MissionExecution:
    player = MissionPlayer(mission)
    return player.replay(auto_confirm=auto_confirm, variables=variables)
