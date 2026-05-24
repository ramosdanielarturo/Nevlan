# Entregable — Execution Lifecycle Stabilization (PRD 2026-05-08c)

**Misión real:** `mission_1778297133` — `c77ce517-994e-4095-852d-a98dafd3b6c1`

**Bug primario:** la ejecución se marcaba como "Ejecutado" en historial,
la ventana flotante quedaba abierta y el worker no emitía señal terminal,
aunque ningún paso real arrancara.

> Aclaración importante recibida del usuario y respetada: la query
> `"devuelveme el amor luis miguel"` **no es un bug**. El usuario la
> escribió así, y el plan correctamente queda con
> `query_fidelity_status="ok"` y `intent_status="READY"` por
> `ready_provenance=raw_evidence`. Este bloque NO toca la lógica de
> query fidelity.

---

## 1. Causas raíz

### 1.1 La ventana de ejecución no cerraba
- `_run_mission_with_smart_executor` programaba el cierre con un
  `QTimer.singleShot(close_ms, _cleanup)` *solo* si llegaba al bloque
  `_finalize` y el método `mark_smart_finished` no se llamaba antes
  de que el worker dejara la `decision.result` en `None`.
- No había una **señal terminal canónica**: el cierre dependía de
  `decision.result.status` y de que el thread llegase a su `finally`.
- Si el worker fallaba silenciosamente (excepción capturada en otro
  scope, decision blocked, `decision.result is None`), `_finalize`
  podía no ejecutarse y la ventana quedaba abierta.

### 1.2 El historial mentía
- `ExecutionTracker.log_complete(status="success", steps_executed=0)`
  persistía la entrada con `status="success"` ⇒ icono ✅, etiqueta
  derivada de un string libre.
- `_run_mission_with_smart_executor` defaulteaba `final_status="success"`
  (línea inicial del bloque `_run`) y solo la sobrescribía si la
  ejecución legítimamente fallaba. Cualquier ruta del bridge que NO
  fuera "fallo explícito" caía en `success`.
- No había validación: "si execution_trace está vacío, no puede ser
  succeeded".

### 1.3 No iniciaban pasos visibles
- El bridge con `decision.result is None` (caso de
  `gate_status != EXECUTABLE` o auto_learning desactivado) devolvía
  silenciosamente. El caller no distinguía esa rama y marcaba
  `status="success"` por default.
- Los outcomes nunca se traducían a un trace cronológico
  consultable: cuando llegaban, solo actualizaban la barra de
  progreso, sin dejar evidencia auditable.

---

## 2. Modelo nuevo (PRD §A + §D)

Módulo nuevo `app/services/missions/execution_lifecycle.py`:

| Pieza | Rol |
|---|---|
| `ExecutionStatus` | Enum cerrado: `queued`, `starting`, `running`, `step_running`, `succeeded`, `failed`, `cancelled`, `timed_out`, `stuck`. |
| `StepExecutionEntry` | Un evento por step real: `step_id`, `step_type`, `status`, `started_at`, `finished_at`, `executor_used`, `strategy_used`, `evidence`, `error`, `duration_ms`. |
| `ExecutionTrace` | Lista cronológica de entries. Conoce `is_empty`, `has_any_succeeded`, `all_steps_succeeded`. |
| `ExecutionRun` | Agregado mutable. Métodos `mark_started`, `mark_step_started`, `mark_step_succeeded`, `mark_step_failed`, `mark_step_skipped`, `mark_step_needs_human`, `mark_succeeded`, `mark_failed`, `mark_failed_no_steps_started`, `mark_failed_with_exception`, `mark_cancelled`, `mark_timed_out`, `mark_stuck`, `force_terminal_for_dangling_worker`. |
| `derive_history_label(run)` | "Ejecutado" / "Falló" / "No iniciado" / "Cancelado" / "Sin respuesta" / "En ejecución" / "En cola". |
| `is_history_executed(run)` | True ⇔ status=succeeded **y** trace tiene al menos un succeeded. |

Reglas duras:

- `mark_succeeded()` lanza `ExecutionLifecycleError` si:
  - el trace está vacío,
  - el trace no tiene ningún `succeeded`,
  - hay algún step en estado distinto a `succeeded`/`skipped` (failed,
    started colgado, needs_human).
- Estados terminales son **inmutables**: una segunda transición lanza
  excepción.

Códigos de error claros:

```
NO_STEPS_STARTED                       — el caso del bug
EXECUTOR_NOT_INVOKED                   — decision.result is None
WORKER_DID_NOT_START                   — thread no arrancó
PLAN_EMPTY_AT_RUNTIME                  — runtime sin pasos
SEMANTIC_PLAN_NOT_PASSED_TO_EXECUTOR
WORKER_FINISHED_WITHOUT_TERMINAL_SIGNAL
PROGRESS_DIALOG_TIMEOUT
USER_CANCELLED
WORKER_EXCEPTION
GATE_BLOCKED
```

---

## 3. Orquestación honesta (PRD §B)

Módulo nuevo `app/services/missions/execution_orchestration.py`:

- `evaluate_decision(run, decision, consume_outcomes=True)` aplica el
  lifecycle a un `RunnerDecision` (real o mock). Cubre las rutas:
  - `decision.result is None` and not blocked → `EXECUTOR_NOT_INVOKED`.
  - `decision.blocked` → `GATE_BLOCKED`.
  - outcomes vacíos pero status="success" → `NO_STEPS_STARTED`.
  - cualquier step `failed` → `WORKER_FAILED`.
  - todos `succeeded`/`skipped` con al menos uno `succeeded`
    → `mark_succeeded`.
- `execute_with_lifecycle(mission, run, runner=run_mission_smart, ...)`
  envuelve al runner real, garantiza que `mark_started` ocurre en el
  primer status, captura cualquier excepción del worker
  (`WORKER_EXCEPTION`) y deja el `run` en estado terminal **siempre**.

`automation_center._run_mission_with_smart_executor` ahora:

1. Construye `ExecutionRun` con `plan_source="semantic_execution_plan"`
   y `executor_used="smart"`.
2. `tracker.log_start` registra `lifecycle_status=queued` (NO running,
   NO success).
3. `widget.bind_lifecycle(run)` enciende el watchdog.
4. El thread llama a `execute_with_lifecycle` (delegando en
   `run_mission_smart`).
5. En `finally`: `tracker.log_run_finished(idx, run)` persiste trace +
   lifecycle real, y `mark_smart_finished(status=run.status.value)`
   cierra el panel emitiendo `lifecycle_terminal`.

---

## 4. Ventana flotante (PRD §B)

`ExecutionControlWidget` ahora expone:

- `pyqtSignal lifecycle_terminal(str)` — payload con el
  `ExecutionStatus.value` (`succeeded`, `failed`, `cancelled`,
  `timed_out`, `stuck`).
- `bind_lifecycle(run, *, stuck_timeout_seconds=90)` — asocia un
  `ExecutionRun` y arranca un watchdog (QTimer cada 2s) que:
  - termina con éxito si `run.is_terminal`,
  - termina con `mark_stuck` si pasa `stuck_timeout_seconds` sin
    `last_progress_at` cambiando.
- `_handle_run_terminal(status)` — idempotente: emite la señal
  **una sola vez** y programa `_cleanup` con 900 ms.
- `_cancel()` — si hay run bindeado, llama a `run.mark_cancelled()`
  ANTES del cleanup (PRD §B punto 5: cancel funcional incluso sin
  `player`).
- `mark_smart_finished(status=...)` mapea ahora todos los terminales
  canónicos:
  - `success` / `succeeded` → ✓ Completado.
  - `cancelled` → ⏹ Cancelado.
  - `timed_out` / `stuck` → ⏱ Sin respuesta.
  - `failed` / `error` / `blocked` / `stopped_for_human` → ✕ Errores.
- `shutdown_synchronously()` ahora también detiene el watchdog.

---

## 5. Historial honesto (PRD §C)

`ExecutionTracker`:

- `ExecutionEntry` tiene ahora `lifecycle_status: ExecutionStatus`,
  `error_code: str`, `execution_trace: List[Dict]`,
  `terminal_signal_received: bool`, `plan_source: str`,
  `executor_used: str`.
- Propiedades nuevas:
  - `history_label` → "Ejecutado" / "Falló" / "No iniciado" / etc.
  - `history_icon` → ✅ ❌ ⏹ ⏱ ▶.
  - `is_executed` → True solo si status=succeeded **y** trace tiene
    succeeded.
- `log_start` registra `queued` (NO `running`, NO `success`).
- `log_run_started(idx, run)` mueve a `running` con metadatos del
  executor.
- `log_run_finished(idx, run)` persiste trace + lifecycle reales
  (API recomendada para los nuevos callers).
- `log_complete(...)` (legacy) sigue funcionando, pero **degrada** a
  `failed/NO_STEPS_STARTED` cuando recibe `status="success"` con
  `steps_executed=0` y trace vacía. No hay forma de mentir al
  historial desde la API legacy.
- `get_failed_mission_ids()` ahora consulta `lifecycle_status` real.

UI (`AutomationCenter._build_exec_entry` y `_build_activity_entry_rich`):

- Coloreo y etiqueta usan `is_executed` / `history_label`. El string
  legacy `status="success"` ya no influye en la UI.
- Cuando hay `error_code`, el panel lo muestra junto al mensaje
  humano (PRD §D).

---

## 6. Antes / Después usando `mission_1778297133`

### Plan (idéntico antes y después)

```text
Entendido

1. Abrir Chrome
2. Seleccionar perfil Daniel Arturo Ramos
3. Abrir nueva pestaña
4. Abrir YouTube
5. Buscar en YouTube: devuelveme el amor luis miguel
6. Bajar resultados

Estado: Lista para ejecutar
Trazabilidad: READY directo desde la grabación, sin intervención humana.
```

`ready_provenance=raw_evidence`, `legacy_graph_ignored=True`,
`coords_used=False`, `intent_status=READY`. Query
`"devuelveme el amor luis miguel"` con
`query_fidelity_status="ok"` (respetada como correcta).

### Caso bug (lifecycle ANTES) — `decision.result is None`

```json
{
  "mission_id": "c77ce517-994e-4095-852d-a98dafd3b6c1",
  "mission_name": "mission_1778297133",
  "total_steps": 6,
  "plan_source": "semantic_execution_plan",
  "executor_used": "smart",
  "status": "failed",
  "error_code": "EXECUTOR_NOT_INVOKED",
  "error_message": "El bridge no invocó el executor.",
  "terminal_signal_received": true,
  "execution_trace": [],
  "succeeded_step_ids": [],
  "failed_step_ids": []
}
```

- `derive_history_label(run)` → **"Falló"** (NO "Ejecutado").
- Sin trace ⇒ `is_history_executed` → **False**.
- `mark_smart_finished(status="failed")` cierra la ventana
  automáticamente y emite `lifecycle_terminal`.

### Caso esperado (lifecycle DESPUÉS) — 6 outcomes success

- `run.status` → `succeeded`.
- `derive_history_label(run)` → **"Ejecutado"**.
- `len(run.trace.entries)` → 12 (6 started + 6 succeeded).
- `succeeded_step_ids` cubre los 6 step_ids reales del plan.
- Ventana cierra en `success` por `lifecycle_terminal`.

### Caso `NO_STEPS_STARTED` — outcomes vacíos pero status="success"

```text
status        = failed
error_code    = NO_STEPS_STARTED
history_label = "No iniciado"
is_executed   = False
```

Esta es exactamente la regresión que el bug original producía
"silenciosamente". Hoy queda como una entrada honesta en el historial.

---

## 7. Ruido humano (PRD §F)

Módulo nuevo `app/services/missions/noise_audit.py`:

- `audit_ignored_noise(mission)` devuelve hallazgos tipados:
  - `address_bar_click_no_submit` — click accidental en barra del
    navegador (sin enter).
  - `typed_then_deleted` — el usuario tipea y borra todo.
  - `noise_before_bookmark` — sesión rota en address bar
    seguida de `open_url via=bookmark` (la primera tentativa
    queda como "navegación corregida").
- `MissionReviewSummary.ignored_noise` añadido al modelo.
- `render_normal_view` **NO** muestra la sección "Ruido descartado".
- `render_expert_view` **SÍ** muestra la sección con
  `kind: human_summary` por hallazgo.

---

## 8. Tests añadidos

74 tests nuevos, todos en verde:

| Archivo | Tests | Cobertura |
|---|---|---|
| `tests/unit/test_execution_lifecycle.py` | 19 | `ExecutionRun`, `derive_history_label`, `is_history_executed`, transiciones inválidas, `force_terminal_for_dangling_worker`, serialización del trace. |
| `tests/unit/test_execution_orchestration.py` | 11 | `evaluate_decision` con todas las rutas problemáticas + `execute_with_lifecycle` con runner mock (success / crash / step_started por outcome / propagación a UI). |
| `tests/unit/test_execution_tracker_lifecycle.py` | 10 | `log_start=queued`, `log_run_started`, `log_run_finished`, degradación de `log_complete` legacy, `history_label`, `history_icon`, `get_failed_mission_ids`. |
| `tests/unit/test_execution_widget_lifecycle.py` | 9 | `lifecycle_terminal` en success/failure/cancel/timeout, idempotencia, cancel con run, watchdog tick, verificación estática del mapeo terminal. |
| `tests/unit/test_noise_audit.py` | 12 | Cada heurística (address_bar / typed_then_deleted / before_bookmark) en aislamiento, kinds documentados, error swallowing. |
| `tests/unit/test_review_summary_noise.py` | 5 | Vista normal oculta ruido, vista experta lo muestra, builder integra audit, errores swallowed. |
| `tests/unit/test_real_mission_1778297133.py` | 8 | Fixture nueva del raw, query respetada, no false success sin steps, plan source = semantic_execution_plan, telemetría legacy_graph_ignored. |

Cobertura mapeada al PRD §E (16 tests obligatorios pedidos):

| PRD §E test | Cobertura |
|---|---|
| `test_history_does_not_mark_succeeded_on_start` | `test_history_does_not_mark_succeeded_on_start` (lifecycle.py). |
| `test_history_marks_running_when_worker_starts` | `test_history_marks_running_when_worker_starts`. |
| `test_history_marks_succeeded_only_after_all_steps_succeed` | `test_history_marks_succeeded_only_after_all_steps_succeed`. |
| `test_history_marks_failed_if_no_steps_started` | `test_history_marks_failed_if_no_steps_started`. |
| `test_history_marks_cancelled_on_user_cancel` | `test_history_marks_cancelled_on_user_cancel`. |
| `test_history_marks_timed_out_without_heartbeat` | `test_history_marks_timed_out_without_heartbeat`. |
| `test_execution_dialog_closes_on_success` | `test_execution_dialog_closes_on_success`. |
| `test_execution_dialog_closes_on_failure` | `test_execution_dialog_closes_on_failure`. |
| `test_execution_dialog_closes_on_cancel` | `test_execution_dialog_closes_on_cancel`. |
| `test_execution_dialog_closes_on_timeout` | `test_execution_dialog_closes_on_timeout`. |
| `test_worker_exception_emits_failed_signal` | `test_mark_failed_with_exception_sets_worker_exception_code` + `test_execute_with_lifecycle_runner_crash_marks_worker_exception`. |
| `test_worker_always_emits_terminal_signal` | `test_execution_dialog_terminal_signal_emitted_only_once` + `test_force_terminal_for_dangling_worker`. |
| `test_execution_trace_records_step_started` | `test_execution_trace_records_step_started_then_succeeded` + `test_execute_with_lifecycle_records_step_started_for_each_outcome`. |
| `test_no_steps_started_cannot_be_success` | `test_no_steps_started_cannot_be_success` + `test_evaluate_decision_success_no_outcomes_marks_no_steps_started`. |
| `test_mission_1778297133_execution_uses_semantic_plan` | `test_mission_1778297133_execution_uses_semantic_plan`. |
| `test_mission_1778297133_no_false_success_without_steps` | `test_mission_1778297133_no_false_success_without_steps`. |
| `test_accidental_address_bar_click_is_ignored_noise` | `test_accidental_address_bar_click_is_ignored_noise`. |
| `test_typed_then_deleted_noise_does_not_create_step` | `test_typed_then_deleted_noise_does_not_create_step`. |
| `test_noise_before_bookmark_does_not_break_open_url_youtube` | `test_noise_before_bookmark_does_not_break_open_url_youtube`. |
| `test_normal_review_hides_ignored_noise` | `test_normal_review_hides_ignored_noise`. |
| `test_expert_review_shows_ignored_noise` | `test_expert_review_shows_ignored_noise`. |

---

## 9. Suite completa (regresión)

```
1188 passed, 2 xfailed, 2 warnings in 242.94s (0:04:02)
```

74 tests nuevos sumados sobre 1114 anteriores. **Cero regresiones**.

---

## 10. Confirmaciones explícitas (PRD §G)

- ✅ La ventana cierra en `success`. (`mark_smart_finished("success")`
  emite `lifecycle_terminal="succeeded"` + cleanup. Test:
  `test_execution_dialog_closes_on_success`.)
- ✅ La ventana cierra en `failure`. (`mark_smart_finished("failed")`.
  Test: `test_execution_dialog_closes_on_failure`.)
- ✅ La ventana cierra en `cancel`. (`_cancel` con run bindeado +
  `mark_smart_finished("cancelled")`. Tests:
  `test_execution_dialog_closes_on_cancel` y
  `test_cancel_button_marks_run_cancelled_and_emits_signal`.)
- ✅ La ventana cierra en `timeout`. (Watchdog → `mark_stuck` /
  `mark_timed_out`. Test: `test_execution_dialog_closes_on_timeout`.)
- ✅ El historial **no miente**. (`history_label` deriva de
  `lifecycle_status`; `log_complete` legacy degrada a
  `failed/NO_STEPS_STARTED`. Tests:
  `test_log_complete_legacy_success_with_empty_trace_degrades`,
  `test_history_label_for_*`.)
- ✅ Si ningún paso inicia, **no** se marca como ejecutado.
  (`mark_succeeded` con trace vacía lanza
  `ExecutionLifecycleError`; `evaluate_decision` con outcomes vacíos
  marca `NO_STEPS_STARTED`. Tests:
  `test_no_steps_started_cannot_be_success`,
  `test_evaluate_decision_success_no_outcomes_marks_no_steps_started`,
  `test_mission_1778297133_no_false_success_without_steps`.)
- ✅ Se usa `semantic_execution_plan`, no `compiled_execution_graph`
  legacy. (`run.plan_source="semantic_execution_plan"` por construcción
  cuando hay plan; tests:
  `test_mission_1778297133_execution_uses_semantic_plan`,
  `test_mission_1778297133_legacy_graph_ignored_telemetry`.)
- ✅ La query `"devuelveme el amor luis miguel"` se respeta como
  correcta. (Plan tiene `query_fidelity_status="ok"`; test:
  `test_mission_1778297133_query_devuelveme_amor_is_accepted`.)
- ✅ El ruido accidental se ignora como noise. (`audit_ignored_noise`
  + `MissionReviewSummary.ignored_noise`; tests en
  `test_noise_audit.py` y `test_review_summary_noise.py`.)
- ✅ No hay popup vacío. (`mark_smart_finished` siempre setea texto
  en `phase_lbl` con fallback humano; ya cubierto por
  `test_execution_dialogs_use_nevlan.py` previamente.)
- ✅ No hay spinner infinito. (Watchdog 90 s + cierre garantizado en
  cualquier terminal del run; tests
  `test_bind_lifecycle_with_terminal_run_triggers_terminal`,
  `test_force_terminal_for_dangling_worker`.)

---

## 11. Archivos nuevos / modificados

**Nuevos**

- `app/services/missions/execution_lifecycle.py`
- `app/services/missions/execution_orchestration.py`
- `app/services/missions/noise_audit.py`
- `tests/fixtures/real_missions/mission_1778297133_raw.json`
- `tests/unit/test_execution_lifecycle.py`
- `tests/unit/test_execution_orchestration.py`
- `tests/unit/test_execution_tracker_lifecycle.py`
- `tests/unit/test_execution_widget_lifecycle.py`
- `tests/unit/test_noise_audit.py`
- `tests/unit/test_review_summary_noise.py`
- `tests/unit/test_real_mission_1778297133.py`
- `docs/entregable_exec_lifecycle.md` (este documento)

**Modificados**

- `app/services/missions/execution_tracker.py` — lifecycle real en cada
  entrada, API `log_run_started`/`log_run_finished`, defensa
  anti-mentira en `log_complete`.
- `app/services/missions/mission_review_summary.py` — campo
  `ignored_noise`, sección "Ruido descartado" en expert view.
- `app/interfaces/desktop/automation_center.py` — `ExecutionControlWidget`
  con señal `lifecycle_terminal`, `bind_lifecycle`, watchdog,
  cancel-con-run; `_run_mission_with_smart_executor` usa
  `execute_with_lifecycle`; UI del historial usa `history_label`.
- `scripts/build_real_mission_fixtures.py` — soporta
  `expects_ready_from_raw=True` para `mission_1778297133`.
- `var/missions/c77ce517-994e-4095-852d-a98dafd3b6c1.json` — restaurado
  a su estado honesto (READY raw_evidence).
