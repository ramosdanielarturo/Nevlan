# Entregable — Regrabación de paso con replay previo

> Bloque cerrado contra el contrato `docs/plan_regrabacion_paso_replay.md`
> y la PRD 2026-05-08c §C–§K.
>
> Cifra oficial única de suite completa (`pytest tests`):
> **1114 passed, 2 xfailed, 2 warnings**.

## 1. Archivos modificados / creados

### Modelo

- `app/contracts/mission.py`
  - Nuevo modelo `RerecordHistoryEntry` (audit trail por sesión de
    regrabación: `target_step_id`, `original_step_snapshot`,
    `replacement_step_snapshot`, `replacement_evidence`,
    `previous_steps_replayed`, `affected_steps`, `started_at`,
    `saved_at`, `source`, `status`, `note`).
  - Nuevo campo `Mission.rerecord_history: List[RerecordHistoryEntry]`.
  - `raw_trace` permanece **inmutable** (regla §3).

### Servicios (sin Qt)

- `app/services/missions/rerecord_session.py` (nuevo).
  - `RerecordSession` (dataclass) y `RerecordOrchestrator`.
  - Estados: `idle | preparing | replaying | waiting_for_user |
    captured | saved | cancelled | failed`.
  - API: `start`, `proceed_to_capture`, `skip_replay_and_capture_manually`,
    `capture_user_event`, `discard_capture`, `save`, `cancel`,
    `release`, `request_cancel`, `is_cancel_requested`.
  - Cooperative cancel (no necesita matar threads).
  - `executor_factory` inyectable → tests headless sin Smart real.

- `app/services/missions/mission_review_confirm.py`
  - Nuevo verbo `rerecord_step(...) -> (plan, replacement_snapshot,
    affected_steps)`. Idempotente por `rerecord_session_id`.
  - Sincroniza `compiled_execution_graph[idx]` + `interpreted_steps[idx]`
    para coherencia con la UI Mission Review (siguen siendo
    debug-only — regla §A.2 — pero deben reflejar el paso nuevo).
  - Recompila el plan vía `attach_intent_status_to_plan` para
    refrescar `intent_status` + `not_ready_reasons`.
  - Audit trail dual: añade entrada a `rerecord_history` y entrada
    espejo a `review_confirmations` con `field="step_replacement"`,
    `source="rerecord"`.

- `app/services/missions/mission_review_summary.py`
  - `MissionReviewSummary.rerecord_history: List[dict]` (vista experto).
  - `render_expert_view` añade el panel “Historial de regrabación”
    con previos reproducidos y afectados.
  - `render_normal_view` ignora el historial (regla normal vs experto).

### UI

- `app/interfaces/desktop/rerecord_overlay.py`
  - Constantes `PHASE_REPLAY` / `PHASE_CAPTURE`.
  - Constructor con `phase` y `total_previous_steps`.
  - Nuevo `replay_progress_lbl` y botón `▶ Empezar` (visible solo
    en `replay`).
  - API: `update_replay_progress`, `enter_capture_phase`,
    `show_replay_failed`.
  - Nueva señal `start_replay_requested`.

- `app/interfaces/desktop/mission_review.py`
  - `_ReplayWorker(QObject)` corre `proceed_to_capture()` en un
    `QThread` propio (PRD §G — UI nunca congela).
  - `rerecord_step(idx)` reescrito completo:
    - Crea `RerecordOrchestrator`.
    - Pre-diálogo `NevlanDialog` "Preparando regrabación" con
      detalles técnicos cuando hay pasos previos.
    - Caso paso 1 (sin previos): pasa directo a `enter_capture_phase`.
    - Caso paso intermedio/último: muestra overlay en `phase=replay`
      y espera al usuario.
  - Heartbeat: `QTimer` 5 s; tras 30 s sin progreso muestra
    `NevlanDialog show_warning("Sigue esperando | Cancelar")`.
  - Manejo de fallo del replay (PRD §6): `NevlanDialog show_warning`
    con tres botones — Reintentar / Manual / Cancelar.
  - `_on_rerecord_save`:
    - Bloqueo de doble-save con `_rerecord_save_in_flight`.
    - Validación de captura vacía vía
      `RerecordOrchestrator.capture_user_event`.
    - Llama `orch.save()`, persiste con `_auto_save()`,
      muestra `NevlanDialog` de éxito y refresca tarjetas.
  - `_on_rerecord_retry`: usa `orch.discard_capture()` y reinicia
    el `StepFragmentRecorder` sin mutar la misión.
  - `_on_rerecord_cancel`: si hay captura, pide confirmación con
    `NevlanDialog`, llama `orch.request_cancel()` + `orch.cancel()`,
    libera worker y heartbeat.
  - `_cleanup_rerecord` libera siempre `orch.release()` + worker
    + timer (idempotente).

### Tests (nuevos)

- `tests/unit/test_rerecord_session.py` — 22 tests.
- `tests/unit/test_rerecord_overlay_buttons.py` — 15 tests.
- `tests/unit/test_rerecord_integration.py` — 8 tests
  (incluye prueba de que el replay no corre en el hilo de UI).
- `tests/unit/test_rerecord_real_mission_1778244908.py` — 7 tests E2E.

Total nuevos: **52 tests**.

## 2. Resumen de arquitectura

```
[Mission Review UI]
   │
   ▼
RerecordOrchestrator        ←  contrato puro (sin Qt)
   │
   ├── start(snapshot)            // bit-perfecto via model_dump
   ├── proceed_to_capture()       // SmartMissionExecutor inyectable
   ├── skip_replay_and_capture_manually()
   ├── capture_user_event(ev)     // valida y guarda en sesión
   ├── discard_capture()          // botón Repetir
   ├── save()  ──► mission_review_confirm.rerecord_step
   │                  ├── _build_replacement_plan_step (compile_fragment + ICE)
   │                  ├── reemplaza plan.steps[N] solo en N
   │                  ├── sincroniza compiled[N] + interpreted[N]
   │                  ├── attach_intent_status_to_plan (recompute)
   │                  └── audit trail (rerecord_history + review_confirmations)
   ├── cancel()                   // restaura snapshot bit-perfecto
   └── release()                  // libera referencias

[Mission Review UI: _ReplayWorker(QThread)]
   ▼
   on_progress  ─►  overlay.update_replay_progress(idx, total, label)
   on_finished  ─►  overlay.enter_capture_phase()  (success)
                    NevlanDialog 3 botones        (failed)

[NevlanDialog en cada hito: preparando, fallo, success, cancel-confirm]
```

Reglas duras del PRD respetadas:

| Regla                                         | Cómo se aplica                                                                    |
|-----------------------------------------------|------------------------------------------------------------------------------------|
| `semantic_execution_plan` único origen        | `rerecord_step` muta SOLO el plan; `compiled[N]` se sincroniza pero sigue debug   |
| `compiled_execution_graph` es debug           | `legacy_compiled_graph_purpose` no se toca; tests E2E confirman que sigue debug    |
| `run_legacy_player` prohibido                 | El executor inyectable es `SmartMissionExecutor` o stub; nunca legacy player       |
| coords solo emergencia final                  | Nuevo step se construye via `attach_semantic_plan_to_mission`, sin coords          |
| Mission Review normal sin debug               | `rerecord_history` solo aparece en `render_expert_view`                            |
| Mission Review experto con trazabilidad       | Vista experto incluye “Historial de regrabación” con previos + afectados           |
| `NevlanDialog` para todos los popups          | Análisis estático verifica que no hay `QMessageBox.*` en el flujo                  |
| Ninguna ventana sin title/message             | `NevlanDialogSpec.normalized()` rellena fallbacks; tests cubren cada popup        |
| UI no congelada                               | `_ReplayWorker` en `QThread`; test verifica `threading.get_ident() != main`        |
| Cualquier cambio con tests                    | 52 tests nuevos + 1114 totales sin regresiones                                     |

## 3. Cómo se usa `docs/plan_regrabacion_paso_replay.md`

- §1 Objetivo: implementado por `RerecordOrchestrator + rerecord_step`.
- §2.1 Verbo `rerecord_step(...)`: implementado en
  `app/services/missions/mission_review_confirm.py` con la firma exacta
  del contrato (acepta `step_index`, `new_step_evidence`,
  `rerecorded_at`, `user_action`, `rerecord_session_id`,
  `started_at`, `previous_steps_replayed`, `original_step_snapshot`).
- §2.2 Módulo `rerecord_session.py`: implementado con `RerecordSession`
  + `RerecordOrchestrator` y los métodos `start`, `proceed_to_capture`,
  `capture_user_event`, `save`, `cancel`. Más `release` para el
  `closeEvent` de la UI (regla §3.4).
- §2.3 UI: `rerecord_overlay.py` recibe constructor extendido
  (`phase`, `total_previous_steps`) + nuevos métodos. La integración
  con el orchestrator vive en `mission_review.py` para no inflar el
  widget (separación clara ver/orquestar).
- §3 Reglas duras: cubiertas con tests dedicados.
- §4 Audit trail: `Mission.rerecord_history[]` + `review_confirmations`
  espejo. Snapshot completo del paso original + replacement +
  evidencia + IDs de previos reproducidos.
- §5 Tests obligatorios: TODOS implementados (mapeo abajo).
- §6 Manejo de fallos: `NevlanDialog show_warning` con tres botones
  (Reintentar / Manual / Cancelar). Frase fija
  `"No pude llegar automáticamente al paso a regrabar."` aplicada.
- §7 Riesgo conocido — efectos colaterales del replay:
  cooperative cancel implementado vía
  `RerecordOrchestrator.is_cancel_requested`.

## 4. Flujo antes/después de regrabar un paso

### Antes (legacy)

1. Usuario click 🎯.
2. Overlay aparece de inmediato; user no sabe a qué punto del flujo
   está, debe re-grabar todo el contexto a mano.
3. Save reemplaza pasos directamente sobre `compiled_execution_graph`
   sin pasar por el plan semántico.
4. Cancelar usaba `QMessageBox.warning` con texto a veces invisible.
5. Sin audit trail.

### Después (PRD 2026-05-08c §C–§K)

1. Usuario click 🎯.
2. `NevlanDialog` "Preparando regrabación" explica qué va a pasar y
   ofrece "Continuar" / "Cancelar" (con detalles técnicos opcionales).
3. Si el usuario continúa, overlay aparece en fase `replay` con barra
   de progreso real (1/N → N-1/N) actualizada por `_ReplayWorker` en
   un `QThread`. UI no se congela.
4. Cuando el replay termina exitosamente, overlay transiciona a fase
   `capture`: copy nuevo (“Llegué al paso que quieres regrabar.
   Realiza la acción y presiona Guardar.”) y `StepFragmentRecorder`
   arranca.
5. Save: valida captura, llama orchestrator → `rerecord_step` →
   reemplaza solo el step `N` del plan semántico, recompila status,
   añade audit trail, persiste a disco. `NevlanDialog` "Regrabación
   guardada" muestra estado nuevo.
6. Repetir: descarta intento sin mutar misión. Reinicia el recorder.
7. Cancelar:
   - Si hay captura sin guardar: pide confirmación con
     `NevlanDialog show_confirmation`.
   - Restaura snapshot bit-perfecto (`Mission.model_validate`).
   - Libera worker, timer, listeners.
8. Si el replay falla: `NevlanDialog show_warning` con 3 botones
   (Reintentar / Manual / Cancelar). El overlay muestra el motivo
   en el label `replay_progress_lbl`.

## 5. Qué pasa con Guardar / Repetir / Cancelar

| Botón     | Estado válido        | Side effects                                                                              | Recursos liberados                        |
|-----------|----------------------|--------------------------------------------------------------------------------------------|--------------------------------------------|
| Guardar   | `phase=capture`      | Reemplaza `plan.steps[N]`; sincroniza compiled+interpreted; `attach_intent_status`; audit  | Recorder + poll timer; worker (si vivía)   |
| Repetir   | `phase=capture`      | `orch.discard_capture()`, recorder se reinicia                                              | Recorder previo                            |
| Cancelar  | cualquier ≠ saved    | `orch.request_cancel()` (cooperativo) + `orch.cancel()` (restaura)                          | Worker + heartbeat + recorder + overlay    |

Doble-save bloqueado con `_rerecord_save_in_flight` (test
`test_save_blocks_double_call`).

## 6. Cómo se garantiza que solo cambia el paso objetivo

`mission_review_confirm.rerecord_step` opera así:

1. Carga `plan = SemanticExecutionPlan.from_dict(blob)`.
2. Construye `new_sp` desde la nueva evidencia (compile_fragment +
   `attach_semantic_plan_to_mission` sobre una misión TEMPORAL — el
   `raw_trace` original no se toca).
3. `new_steps_list = list(plan.steps)` y
   `new_steps_list[step_index] = new_sp`. Solo la posición target
   se sustituye.
4. Sincroniza `compiled[N]` + `interpreted[N]` (la UI los usa para
   renderizar tarjetas), pero nunca toca otros índices.

Tests que blindan esto:

- `TestCaptureAndSave.test_save_persists_only_target_step`.
- `TestRerecordRealMission.test_search_step_replaces_only_search`.
- `TestRerecordRealMission.test_save_does_not_touch_raw_trace`.

## 7. Cómo se conserva el audit trail

Dos colecciones:

1. `Mission.rerecord_history` — sesión por sesión, con
   `original_step_snapshot`, `replacement_step_snapshot`,
   `replacement_evidence`, `previous_steps_replayed`,
   `affected_steps`, timestamps, `status`, `note`.
   Idempotente por `rerecord_session_id`.

2. `Mission.review_confirmations` — espejo a nivel de campo con
   `field="step_replacement"`, `source="rerecord"`,
   `original_value=<original_step_id>`. Permite leer el JSON y saber
   "este step llegó por regrabación humana" sin recorrer el history.

Test que blinda:
`TestCaptureAndSave.test_save_writes_audit_trail` y
`TestRerecordRealMission.test_save_persists_audit_trail_full`.

## 8. Cómo se evita congelar la UI

- `_ReplayWorker` (QObject) vive en `QThread` propio. Test
  `TestReplayThreading.test_replay_worker_runs_in_separate_thread`
  verifica `threading.get_ident()` ≠ thread principal.
- Heartbeat: `QTimer` (5 s) suma 30 000 ms sin progreso → muestra
  `NevlanDialog show_warning("Sigue esperando | Cancelar")`.
- Cooperative cancel: el usuario puede pedir `orch.request_cancel()`
  en cualquier momento; el orchestrator lo lee tras cada paso del
  replay.
- `_cleanup_rerecord` SIEMPRE detiene timer + worker + recorder
  (idempotente).

## 9. Tests agregados (mapeo PRD §5)

| PRD                                                    | Test                                                                                       |
|--------------------------------------------------------|--------------------------------------------------------------------------------------------|
| `test_rerecord_runs_previous_steps_before_target`       | `test_rerecord_session.py::TestReplayPrevio::test_rerecord_runs_previous_steps_before_target` |
| `test_rerecord_stops_at_target_step`                    | `test_rerecord_session.py::TestReplayPrevio::test_rerecord_stops_at_target_step`            |
| `test_rerecord_only_replaces_target_step`               | `test_rerecord_session.py::TestCaptureAndSave::test_save_persists_only_target_step`         |
| `test_rerecord_does_not_mutate_previous_steps`          | `test_rerecord_real_mission_1778244908.py::test_search_step_replaces_only_search`           |
| `test_rerecord_recomputes_semantic_plan`                | `test_rerecord_session.py::TestCaptureAndSave::test_save_recomputes_semantic_plan`          |
| `test_rerecord_failure_shows_visible_error`             | `test_rerecord_session.py::TestReplayPrevio::test_rerecord_failure_shows_visible_error`     |
| `test_rerecord_cancel_restores_original_mission`        | `test_rerecord_session.py::TestRepeatAndCancel::test_cancel_restores_original_mission`      |
| `test_rerecord_save_persists_new_step`                  | `test_rerecord_session.py::TestCaptureAndSave::test_save_persists_only_target_step`         |
| `test_rerecord_repeat_discards_current_capture`         | `test_rerecord_session.py::TestRepeatAndCancel::test_repeat_discards_current_capture`       |
| `test_rerecord_buttons_enabled_only_when_valid`         | `test_rerecord_overlay_buttons.py::TestOverlayPhaseCapture::test_save_button_is_disabled_until_capture` |
| `test_rerecord_window_does_not_leave_background_listener_running` | `test_rerecord_session.py::TestOrchestratorLifecycle::test_release_is_idempotent` + `_cleanup_rerecord` cubierto en integración |

Adicionales del PRD §J (E2E con misión real):

- `test_rerecord_real_mission_search_step_replays_previous_steps`
- `test_rerecord_real_mission_search_step_replaces_only_search`
- `test_rerecord_real_mission_scroll_step_replaces_only_scroll`
- `test_rerecord_real_mission_cancel_keeps_fixture_identical`
- `test_rerecord_real_mission_save_recomputes_ready_status`
- `test_save_does_not_touch_raw_trace` (regla §3 del contrato)

## 10. Resultado de suite completa

```
============================ 1114 passed, 2 xfailed, 2 warnings in 243.29s (0:04:03) =============================
```

- **Cifra oficial única**: 1114 passed.
- 2 xfail estaban presentes desde antes (no introducidos por este bloque).
- 2 warnings (deprecation de `model_fields` en `tests/unit/test_mission_pipeline.py`) son
  pre-existentes; no afectan al bloque de regrabación.

## 11. Confirmación explícita (PRD §K)

- [x] **Regrabar paso 1 funciona** —
      `test_rerecord_first_step_skips_replay` (no instancia executor,
      pasa directo a `waiting_for_user`).
- [x] **Regrabar paso intermedio funciona** —
      `test_rerecord_intermediate_step_replays_only_previous`.
- [x] **Regrabar último paso funciona** —
      `test_rerecord_last_step_replays_all_previous`.
- [x] **Replay previo funciona** —
      `test_rerecord_runs_previous_steps_before_target`.
- [x] **Fallo de replay previo muestra error visible** —
      `test_rerecord_failure_shows_visible_error` (frase
      `REPLAY_FAILED_MESSAGE`) +
      `test_rerecord_failure_dialog_uses_three_buttons` (UI con 3
      botones).
- [x] **Guardar funciona** —
      `test_save_persists_only_target_step` +
      `test_save_writes_audit_trail` +
      `test_save_recomputes_semantic_plan`.
- [x] **Repetir funciona** —
      `test_repeat_discards_current_capture` +
      `test_repeat_does_not_mutate_mission`.
- [x] **Cancelar funciona** —
      `test_cancel_restores_original_mission` +
      `test_cancel_with_unsaved_capture_asks_confirmation`.
- [x] **No se mutan pasos no relacionados** —
      `test_search_step_replaces_only_search` (E2E) confirma pasos
      previos y posteriores idénticos byte a byte.
- [x] **`semantic_execution_plan` se recompila** —
      `test_save_recomputes_semantic_plan` valida `intent_status` y
      `not_ready_reasons` actualizados.
- [x] **Approval Gate se recalcula** — `attach_intent_status_to_plan`
      es lo que llama el verbo; el blob persistido se lee con el
      nuevo `intent_status` (ver `test_save_recomputes_ready_status`).
- [x] **Mission Review se refresca** —
      `test_summary_includes_rerecord_history_after_save` +
      `test_expert_view_shows_rerecord_history`.
- [x] **No hay ventanas vacías** — `NevlanDialogSpec.normalized()`
      rellena fallbacks; tests `test_rerecord_dialogs_use_nevlan_dialog`
      lo verifican vía análisis estático del flujo.
- [x] **No hay UI congelada** —
      `test_replay_worker_runs_in_separate_thread`.
- [x] **No hay fallback legacy** — el executor por defecto es
      `SmartMissionExecutor`; tests inyectan stub. Nunca se llama
      `MissionPlayer` ni `_run_legacy_player`.
- [x] **`compiled_execution_graph` sigue siendo debug-only** —
      `Mission.legacy_compiled_graph_purpose` no se modifica en
      `rerecord_step` (siempre `legacy_debug_only`); el plan
      semántico es la única fuente de verdad para la ejecución.

## 12. Decisiones de implementación que cierran ambigüedades del contrato

- El step nuevo conserva el `id` original (no rompe referencias
  externas en disco/UI). El `_original_step_id` queda en `params`
  como ancla forense.
- `compiled[N]` + `interpreted[N]` se sincronizan para coherencia
  con la UI Mission Review (que dibuja tarjetas desde
  `compiled_execution_graph`). NO se recompila el `compiled` global,
  para que las tarjetas no relacionadas no rebroten su layout.
- `affected_steps` se calcula leyendo `params.depends_on` declarado
  por steps posteriores. Hoy nadie lo declara explícitamente, así
  que el campo queda vacío en la práctica — pero el contrato quedó
  preparado para cuando aparezca.
- Si la nueva evidencia compila a más de un step (caso poco común),
  usamos el primero y dejamos warning en log. El contrato pide
  "regrabar UN paso, no varios"; preferimos no romper el orden del
  plan.
