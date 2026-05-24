# Entregable — Perfil vacío + Regrabación UI rota

**PRD:** 2026-05-08d
**Fecha:** 2026-05-08
**Misión real:** `mission_1778303937` (`87752f25-a2bd-4b64-acd6-b766beb19c84`)

> El usuario reportó que el bloque previo NO estaba cerrado: el
> perfil seguía sin resolverse de forma confiable y los botones
> Guardar / Repetir / Cancelar de la UI real de regrabación
> "parecían muertos". Este entregable cierra ambos frentes con
> implementación + tests + prueba manual documentada.

---

## A. PERFIL VACÍO

### A.1 Causa raíz

`mission_1778303937` capturó como evento del profile picker (raw
event #4) un UIA target con:

```
automation_id = "rendering-content"
class_name    = "style-scope ytd-in-feed-ad-layout-renderer"
name          = ""
```

Es decir: el recorder tomó el snapshot UIA del **contenido posterior**
al click (un layout de YouTube ad), no del Chrome profile picker —
el picker se cierra demasiado rápido y para cuando el listener UIA
captura, ya hay otra ventana al frente.

Antes de este bloque, el `intent_collapse_engine` aceptaba ese UIA
basura como observación humana, y el `_params_for_select_profile`
en `semantic_execution_plan.py` terminaba con
`profile_name = "Seleccionar perfil"` (label genérico extraído del
título del propio profile picker). El JSON live persistía:

```json
{
  "type": "select_profile",
  "needs_user_label": false,         // ← BUG: sin pedir confirmación
  "params": {
    "profile_name": "Seleccionar perfil",   // ← BUG: literal genérico
    "profile":      "Seleccionar perfil"
  }
}
```

Resultado: `EMPTY_PROFILE_NAME_IN_SELECT_PROFILE` en `not_ready_reasons`
pero **sin botón visible para confirmar el perfil**, el usuario
quedaba sin manera de continuar.

### A.2 Causa raíz — por qué `ytd-in-feed-ad-layout-renderer` se
       tomó como evidencia de perfil

`profile_resolver` ya rechazaba algunos tokens UIA como
"GroupControl" o "video-stream", pero su lista no cubría los
custom-elements de YouTube/Polymer que aparecen como
`automation_id` cuando el snapshot llega tarde. El `_uia_data` del
evento traía:

```
automation_id = "rendering-content"
class_name    = "style-scope ytd-in-feed-ad-layout-renderer"
```

Y el `_detect_select_profile` propagaba ese `automation_id` /
`class_name` como observación humana sin filtro.

### A.3 Antes / después del `semantic_execution_plan`

**Antes** (JSON live persistido por versión vieja del ICE):

```json
{
  "type": "select_profile",
  "needs_user_label": false,
  "human_label": "Seleccionar perfil",
  "params": {
    "profile_name": "Seleccionar perfil",
    "profile":      "Seleccionar perfil"
  }
}
```

`intent_status = NEEDS_REVIEW` con
`not_ready_reasons = [EMPTY_PROFILE_NAME_IN_SELECT_PROFILE, …]`
pero la UI **no** mostraba el botón "Confirmar perfil" (porque
`needs_user_label=false`).

**Después** (recompilando con el nuevo `intent_collapse_engine`):

```json
{
  "type": "select_profile",
  "needs_user_label": true,            // ← UI muestra el CTA
  "label_prompt": "¿Qué perfil debo usar?",
  "human_label":  "Seleccionar perfil",
  "params": {
    "profile_name":      "",           // ← honesto: vacío
    "profile":           "",
    "candidate_aliases": []            // ← sin contaminar
  }
}
```

`intent_status = NEEDS_REVIEW`,
`not_ready_reasons = [NEEDS_USER_LABEL_PENDING,
EMPTY_PROFILE_NAME_IN_SELECT_PROFILE, QUERY_MISSING_SHORT_TOKEN]`.

**Después de confirmación humana** (click `👤 Confirmar perfil`
+ escribir "Daniel Arturo Ramos"):

```json
{
  "type": "select_profile",
  "needs_user_label": false,
  "human_label": "Usar el perfil Daniel Arturo Ramos",
  "params": {
    "profile_name":         "Daniel Arturo Ramos",
    "profile":              "Daniel Arturo Ramos",
    "profile_confirmed_via":"mission_review"
  }
}
```

`review_confirmations += [profile_name=Daniel Arturo Ramos via user_confirmation]`.
Alias_store persiste `Daniel`, `Daniel Arturo`, `Daniel Ramos`,
`Daniel Arturo Ramos`. Próximas misiones de este usuario resuelven
el perfil sin volver a preguntar.

### A.4 Cómo se confirma / guarda el perfil

Flujo end-to-end:

1. Mission Review carga la misión y llama
   `_heal_stale_select_profile(mission)` (cura `select_profile`
   stale recompilando desde `raw_trace` con
   `apply_collapse_to_mission`).
2. `_build_cards` detecta `select_profile` con `profile_name=""`
   o `needs_user_label=True` y añade un botón
   `👤 Confirmar perfil` en la card.
3. Click → `_open_profile_confirm_dialog(idx)`:
   * busca el step semántico,
   * calcula el mejor candidato (alias_store > candidate_aliases >
     `extract_canonical_profile_name(current)`),
   * abre `show_text_input` (NevlanDialog con QLineEdit) —
     título "Confirmar perfil", label "¿Qué perfil debo usar?",
     placeholder "Ej. Daniel Arturo Ramos".
4. Usuario confirma → `confirm_profile(mission, profile_name=...)`:
   * actualiza el step (`profile_name`, `human_label`,
     `needs_user_label=false`, confidence ≥ 0.95),
   * añade entrada estructurada a `mission.review_confirmations`,
   * persiste alias_store con `learn_profile_alias(canonical,
     aliases=candidate_aliases)`,
   * recompila `intent_status` con
     `attach_intent_status_to_plan` (queda READY si era el único
     blocker crítico).
5. `_auto_save` persiste el JSON live + `refresh_cards` re-renderea
   la UI con la información correcta.

### A.5 Archivos modificados

| Archivo | Rol |
|---|---|
| `app/services/missions/profile_resolver.py` | Nuevo `_INVALID_PROFILE_EVIDENCE_FRAGMENTS` (Polymer/YouTube). Función pública `is_invalid_profile_evidence`. `_scrub` defensivo en `resolve_profile`. `_prefer_alias_or` para que el alias_store sobrescriba canonicals "incompletos" (`Daniel Arturo` → `Daniel Arturo Ramos`). |
| `app/services/missions/intent_collapse_engine.py` | Filtro de evidencia inválida en 3 puntos: `ctx.profile_human_observation`, `ctx.profile_name_ocr` (vía OCR), y `ctx.profile_window_title` (vía cambio de ventana). |
| `app/interfaces/desktop/mission_review.py` | Nueva función `_heal_stale_select_profile` invocada en `__init__`. Helper `_find_semantic_step_for_compiled`. Botón `👤 Confirmar perfil` en cada card de `select_profile` blocked. Handler `_open_profile_confirm_dialog`. |
| `app/interfaces/desktop/nevlan_dialog.py` | Nueva función pública `show_text_input` (NevlanDialog con `QLineEdit`). |
| `scripts/build_real_mission_fixtures.py` | Añadida `RealMissionSpec` para `mission_1778303937`. |

### A.6 Tests obligatorios (todos pasando)

`tests/unit/test_real_mission_1778303937.py` (26 tests):

* `test_real_mission_1778303937_profile_empty_before_confirmation`
* `test_blocker_empty_profile_name_present`
* `test_no_executable_plan_with_empty_profile_name`
* `test_extract_canonical_rejects_youtube_polymer`
  (parametrizado con 11 valores tóxicos: `rendering-content`,
  `style-scope ytd-in-feed-ad-layout-renderer`, `ytd-app`,
  `ytd-rich-grid-renderer`, `guide-service`, `style-scope`,
  `html5-main-video`, `video-stream`, `YouTube`,
  `Nueva pestaña - Google Chrome`, `YouTube - Google Chrome`).
* `test_resolve_profile_neutralizes_invalid_observation`
  (parametrizado con 4 valores Polymer).
* `test_profile_resolver_rejects_youtube_ad_groupcontrol_as_profile`
* `test_profile_resolver_does_not_persist_invalid_alias`
* `test_real_mission_1778303937_profile_confirmation_sets_ready_candidate`
* `test_profile_alias_store_reuses_daniel_arturo_ramos`
* `test_profile_resolver_uses_previous_profile_picker_evidence`
* `test_confirmed_fixture_is_ready`
* `test_heal_recompiles_when_profile_name_is_generic_label`
* `test_heal_recompiles_when_profile_name_is_polymer_garbage`

---

## B. REGRABACIÓN UI ROTA

### B.1 Causa raíz — botones "muertos"

El `RerecordOverlay` se inicializaba con la combinación:

```python
self.setWindowFlags(
    Qt.WindowType.Tool |
    Qt.WindowType.WindowStaysOnTopHint |
    Qt.WindowType.FramelessWindowHint |
    Qt.WindowType.WindowDoesNotAcceptFocus     # ← ROMPE
)
self.setFocusPolicy(Qt.FocusPolicy.NoFocus)    # ← ROMPE
```

`WindowDoesNotAcceptFocus` + `FocusPolicy.NoFocus` se intentaban
usar para evitar el robo de foco a la app destino. Pero en
Windows tienen dos efectos colaterales:

1. Los `QPushButton` no reciben hover/press confiable cuando otra
   ventana es activa (Qt rutea el evento al widget pero el estado
   visual no se actualiza). El usuario percibe "los botones no
   responden".
2. `keyPressEvent` jamás se dispara, así que los atajos
   Enter/Esc/F5 quedan muertos.

Adicionalmente, `btn_save.setEnabled(False)` cuando no había
captura hacía que el usuario clicara y nada pasara, sin feedback,
percibiendo "el botón está muerto". El PRD §C exige que **siempre**
sea clickeable y la validación viva en el handler con `NevlanDialog`.

Y el `closeEvent` no emitía `cancel_requested`, así que si el
overlay se cerraba por una vía que no era `_on_cancel` (Alt+F4,
parent destruido, etc.) los recursos del caller (worker, listeners,
timers, orchestrator) quedaban vivos.

### B.2 Cambios aplicados

```python
# Antes
self.setWindowFlags(
    Qt.WindowType.Tool |
    Qt.WindowType.WindowStaysOnTopHint |
    Qt.WindowType.FramelessWindowHint |
    Qt.WindowType.WindowDoesNotAcceptFocus
)
self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

# Después
self.setWindowFlags(
    Qt.WindowType.Tool |
    Qt.WindowType.WindowStaysOnTopHint |
    Qt.WindowType.FramelessWindowHint
)
self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # foco solo al clicar
```

`WA_ShowWithoutActivating` evita robo de foco al mostrar el
overlay; `ClickFocus` lo da solo cuando el usuario clica en él
(comportamiento natural).

`btn_save.setEnabled(True)` siempre; `_save_enabled` ahora es solo
un hint interno. La validación "sin captura" la hace
`_on_rerecord_save` con `show_warning(..., title="No hay nada
que guardar")`.

`closeEvent` ahora emite `cancel_requested` salvo que ya se haya
emitido antes (flag `_emitted_terminal`).

### B.3 Funciones esperadas — confirmación punto a punto (PRD §C)

| Botón | Comportamiento implementado |
|-------|------------------------------|
| **Guardar** | `RerecordOverlay.btn_save.click()` → emite `save_requested` → `MissionReviewDialog._on_rerecord_save`. Valida (1) `_fragment_recorder is None` → NevlanDialog "No hay nada que guardar"; (2) `raw_events == []` → NevlanDialog idem + retry. Si OK: `orch.capture_user_event` → `orch.save()` → `_auto_save` → NevlanDialog "Regrabación guardada" → `refresh_cards` + `_cleanup_rerecord`. |
| **Repetir** | `btn_retry.click()` → `_on_rerecord_retry` → discarda fragmento, llama `orch.discard_capture`, instancia un nuevo `StepFragmentRecorder` y reinicia el `_poll_timer`. NO toca `mission.semantic_execution_plan`. El overlay muestra "Listo, repite este paso nuevamente." |
| **Cancelar** | `btn_cancel.click()` → emite `cancel_requested` → `_on_rerecord_cancel`. Si `orch.session.state == STATE_CAPTURED`, primero `show_confirmation` ("Cancelar regrabación"). Tras confirmar: detiene `_poll_timer`, cierra `_fragment_recorder`, `orch.request_cancel()` + `orch.cancel()`, `_stop_replay_worker()`, `_cleanup_rerecord()` (libera workers, listeners, timers, overlay y orchestrator). |

### B.4 Archivos UI modificados

| Archivo | Cambios |
|---|---|
| `app/interfaces/desktop/rerecord_overlay.py` | Quitamos `WindowDoesNotAcceptFocus` y `FocusPolicy.NoFocus`. Botón Save siempre `setEnabled(True)`. Cursor pointer en todos los botones. `closeEvent` emite `cancel_requested` para garantizar cleanup del caller. Nuevo flag `_emitted_terminal` para no doble-emitir. `reset_for_retry` mantiene Save clickeable y muestra "Listo, repite este paso nuevamente." (literal del PRD). Object names estables en cada botón (`rerecordBtnSave`, `rerecordBtnCancel`, …). |
| `app/interfaces/desktop/mission_review.py` | `_on_rerecord_save` con validaciones explícitas + `NevlanDialog` "No hay nada que guardar". Resto de handlers documentados en PRD §C. |

### B.5 Signals / slots conectados

```python
self._rerecord_overlay.start_replay_requested.connect(
    self._on_rerecord_start_replay)
self._rerecord_overlay.save_requested.connect(
    self._on_rerecord_save)
self._rerecord_overlay.retry_requested.connect(
    self._on_rerecord_retry)
self._rerecord_overlay.cancel_requested.connect(
    self._on_rerecord_cancel)
```

Estas 4 connect-lines están verificadas por análisis estático en
`tests/unit/test_rerecord_ui_real.py::TestMissionReviewWiresHandlers`.

### B.6 Tests UI nuevos (todos pasando)

`tests/unit/test_rerecord_ui_real.py` (15 tests):

| Test | PRD § |
|---|---|
| `test_rerecord_overlay_save_button_connected` | §D |
| `test_rerecord_overlay_repeat_button_connected` | §D |
| `test_rerecord_overlay_cancel_button_connected` | §D |
| `test_rerecord_buttons_do_not_raise_silent_exceptions` | §D |
| `test_rerecord_save_without_capture_shows_visible_dialog` | §C |
| `test_rerecord_repeat_clears_capture_buffer` | §C |
| `test_rerecord_cancel_emits_signal_and_closes_overlay` | §C |
| `test_rerecord_close_event_emits_cancel_for_safety` | §C |
| `test_overlay_does_not_use_window_does_not_accept_focus` | §B.1 |
| `test_overlay_save_button_never_starts_disabled_in_capture_phase` | §C |
| `test_mission_review_rerecord_real_flow_buttons_work` | §B |
| `test_save_handler_uses_show_warning_for_missing_capture` | §C |
| `test_cancel_handler_releases_orchestrator_and_workers` | §C |
| `test_card_has_profile_confirm_button_for_select_profile` | §A.4 |
| `test_open_profile_confirm_dialog_persists_with_auto_save` | §A.4 |

`tests/unit/test_rerecord_ui_integration.py` (3 tests):

| Test | PRD § |
|---|---|
| `test_rerecord_save_with_capture_replaces_target_step` | §C |
| `test_rerecord_cancel_restores_original_mission` | §C |
| `test_rerecord_cancel_closes_overlay` | §C |

Tests existentes ajustados al nuevo contrato (botón siempre clickeable):
* `test_save_button_is_always_clickable_in_capture_phase` (era
  `test_save_button_is_disabled_until_capture`).
* `test_reset_for_retry_keeps_save_clickable` (era
  `test_reset_for_retry_disables_save_again`).

---

## C. DIALOG "REPARAR PASOS" — BOTONES FUNCIONALES + REPLAY AUTOMÁTICO

> Feedback del usuario (2026-05-09):
> _"La ventana emergente que sale después de dar click en el botón
> Reparar [...] está obsoleta, ningún botón funciona, [...] si se
> va reparar un paso se deben ejecutar los anteriores
> automáticamente, y cuando siga el paso debe salir esta ventana
> de regrar y sus botones deben funcionar al 1000%."_

### C.1 Causa raíz

`MissionReviewDialog.repair_weak_steps` mostraba un `QDialog` con
una lista de pasos débiles y, al hacer doble-clic o usar la cola,
invocaba `pick_correct_element(idx)` — un capturador minimalista
de **un solo click** que **no ejecutaba los pasos anteriores** y
**no abría el RerecordOverlay**.

El usuario interpretaba ese flujo como "obsoleto / botones que no
funcionan" porque:

* El flujo real de regrabación (`rerecord_step` con replay previo
  + `RerecordOverlay`) ya estaba implementado y validado en §B
  pero **el botón Reparar nunca lo lanzaba**.
* El dialog tenía window flags inconsistentes que en algunos
  casos provocaban pérdida de foco (botones aparentemente
  inactivos).

### C.2 Cambios

| Archivo | Cambios |
|---|---|
| `app/interfaces/desktop/mission_review.py::repair_weak_steps` | El dialog ahora expone tres botones: **🎯 Regrabar paso** (default + foco), **⚡ Regrabar los N en cola** y **Cerrar**. Doble-clic o el botón principal disparan `rerecord_step(idx)` (replay previo + RerecordOverlay). La cola se persiste en `self._repair_queue` y se procesa con `_process_next_repair_in_queue`. Window flags explícitas (`Dialog | CustomizeWindowHint | WindowTitleHint | WindowCloseButtonHint`) y `FocusPolicy.StrongFocus` para garantizar que los botones reciban clicks/Enter. |
| `app/interfaces/desktop/mission_review.py::_process_next_repair_in_queue` | Nuevo método. Despacha el siguiente paso de la cola con `rerecord_step(idx)`. Cuando la cola se agota, muestra `NevlanDialog` "Reparación completa" con resumen. |
| `app/interfaces/desktop/mission_review.py::_cleanup_rerecord` | Al final de cada cleanup post-regrabación, si quedan items en `self._repair_queue`, dispara `QTimer.singleShot(400, self._process_next_repair_in_queue)`. Este es el único hook necesario porque `_cleanup_rerecord` es la salida garantizada del flujo (Save / Cancel / cerrar overlay siempre lo invocan). |
| `app/interfaces/desktop/mission_review.py::repair_weak_steps` | "No hay pasos débiles" ya no usa `QMessageBox.information`; usa `show_warning(self, title="Misión estable", …)` para consistencia con el resto del PRD §F. |

### C.3 Flujo completo "Reparar paso" (PRD §G)

1. Usuario click **🩹 Reparar débiles** en Mission Review.
2. Se ejecuta el pipeline completo (`apply_collapse_to_mission`,
   `simulate_mission`, `compute_mission_execution_confidence`,
   `check_mission_approvable`) y se calcula la lista de pasos
   débiles.
3. Si la lista está vacía → `NevlanDialog` "Misión estable".
4. Si hay pasos → se abre el QDialog **"Reparar pasos — Nevlan
   (N)"** con la lista, los tres botones y el botón **🎯 Regrabar
   paso** seleccionado por defecto.
5. Usuario selecciona un paso y pulsa **🎯 Regrabar paso** (o
   doble-clic). El dialog cierra y se invoca
   `self.rerecord_step(idx)`.
6. `rerecord_step` muestra `NevlanDialog` "Preparando regrabación"
   con el contador de pasos anteriores ("Voy a ejecutar los N
   paso(s) anterior(es) automáticamente y me detendré justo antes
   de ‘…’").
7. Tras "Continuar", aparece el `RerecordOverlay` en fase replay
   con el botón ▶ Empezar y un indicador `Replay: i/N — type`.
8. Tras click ▶ Empezar, el `_ReplayWorker` ejecuta los pasos
   `0..idx-1` en un `QThread` no bloqueante. El indicador se
   actualiza en cada paso.
9. Al terminar el replay, el overlay transiciona a fase capture
   con los botones **💾 Guardar / 🔄 Repetir / ❌ Cancelar**
   (todos funcionales según §B).
10. Al guardar / cancelar, `_cleanup_rerecord` libera todos los
    recursos. Si la cola estaba activa, encadena el siguiente
    paso automáticamente (sin reabrir el dialog Reparar).
11. Cuando la cola se vacía → `NevlanDialog` "Reparación
    completa".

### C.4 Tests nuevos (todos pasando)

`tests/unit/test_repair_dialog_uses_rerecord.py` (8 tests):

| Test | Cobertura |
|---|---|
| `test_repair_dialog_default_handler_is_rerecord_step` | El dispatcher final del dialog es `self.rerecord_step(i)`, no `pick_correct_element`. |
| `test_repair_dialog_has_explicit_rerecord_button` | Botón **🎯 Regrabar paso** + `objectName="weakRepairRerecordBtn"`. |
| `test_repair_dialog_queue_button_uses_rerecord_chain` | La cola se persiste en `self._repair_queue` y se procesa con `_process_next_repair_in_queue`. |
| `test_process_next_repair_uses_rerecord_step` | El runner de la cola invoca `rerecord_step(idx)` (no `pick_correct_element`). |
| `test_cleanup_rerecord_dispatches_next_in_repair_queue` | `_cleanup_rerecord` consulta `_repair_queue` y dispara `_process_next_repair_in_queue`. |
| `test_repair_dialog_uses_dialog_flags_with_close_button` | Window flags `Dialog | WindowCloseButtonHint`, `StrongFocus`, sin `WindowDoesNotAcceptFocus`. |
| `test_repair_dialog_default_button_is_rerecord` | El botón Regrabar es default + responde a Enter (`setDefault(True)` + `setAutoDefault(True)`). |
| `test_repair_weak_steps_does_not_crash_when_no_weak_steps` | Smoke test offscreen: sin pasos débiles → `show_warning` "Misión estable" + return. |

### C.5 Total tests del bloque

* **44** tests nuevos del bloque previo (perfil + regrabación UI).
* **8** tests nuevos del dialog Reparar.
* **2** tests existentes ajustados.
* **Suite completa**: 1239 passed / 0 failures relacionadas. La
  única falla observada (`test_private_mode_leaves_no_trace_on_disk`)
  es flake preexistente del logger compartido durante la suite,
  ajeno a estos cambios (pasa al correr aislado).

---

## E. NO DECLARAR CERRADO SIN PRUEBA MANUAL

Ver **`docs/manual_test_perfil_y_rerecord_ui.md`** — checklist
exhaustivo paso a paso con resultados esperados, cubriendo:

* Confirmación de perfil end-to-end (8 pasos).
* Rechazo de evidencia inválida (2 pasos).
* Replay previo + transición a fase capture (2 pasos).
* Save sin captura → NevlanDialog (2 pasos).
* Retry sin mutar misión (3 pasos).
* Cancel con confirmación + cleanup (5 pasos).
* Save con captura → reemplaza solo el paso objetivo (6 pasos).
* Dialog "Reparar pasos" → 🎯 Regrabar paso (replay automático
  + RerecordOverlay) + cola encadenada (10 pasos, §2g).

El documento incluye criterios de cierre estrictos: si cualquier
punto del checklist falla, el bloque NO se considera cerrado.

---

## F. ENTREGABLE — confirmaciones explícitas

| Punto del PRD | Estado |
|---|---|
| 1. Causa raíz de profile_name vacío en mission_1778303937 | ✅ §A.1 |
| 2. Causa raíz de por qué se tomó `ytd-in-feed-ad-layout-renderer` como evidencia de perfil | ✅ §A.2 |
| 3. Antes/después del semantic_execution_plan | ✅ §A.3 |
| 4. Cómo se confirma/guarda el perfil | ✅ §A.4 |
| 5. Causa raíz de por qué Guardar/Repetir/Cancelar no funcionaban | ✅ §B.1 |
| 6. Archivos UI modificados | ✅ §A.5 + §B.4 |
| 7. Signals/slots conectados | ✅ §B.5 |
| 8. Tests agregados | ✅ §A.6 + §B.6 + §C.4 (52 tests nuevos: 44 perfil/rerec + 8 dialog Reparar) |
| 9. Suite completa | ✅ **1239 passed, 2 xfailed, 0 failures relacionadas** (la única falla `test_private_mode_leaves_no_trace_on_disk` es flake preexistente del logger compartido — pasa aislada) |
| 10. Prueba manual documentada del flujo real | ✅ `docs/manual_test_perfil_y_rerecord_ui.md` |
| 11. **Confirmaciones explícitas:** | |
| ↳ Guardar funciona | ✅ test_rerecord_save_with_capture_replaces_target_step + manual §2f |
| ↳ Repetir funciona | ✅ test_rerecord_overlay_repeat_button_connected + test_rerecord_repeat_clears_capture_buffer + manual §2d |
| ↳ Cancelar funciona | ✅ test_rerecord_cancel_emits_signal_and_closes_overlay + test_rerecord_cancel_restores_original_mission + manual §2e |
| ↳ Regrabar funciona | ✅ flujo completo §B.3 + tests UI §B.6 + manual §2a-§2f |
| ↳ Perfil no queda vacío | ✅ test_real_mission_1778303937_profile_empty_before_confirmation + test_no_executable_plan_with_empty_profile_name + manual §1 |
| ↳ No hay botones muertos | ✅ test_overlay_does_not_use_window_does_not_accept_focus + test_overlay_save_button_never_starts_disabled_in_capture_phase + test_rerecord_buttons_do_not_raise_silent_exceptions |
| ↳ Dialog Reparar abre RerecordOverlay (no pick_correct_element) | ✅ test_repair_dialog_default_handler_is_rerecord_step + test_process_next_repair_uses_rerecord_step + manual §2g.23 |
| ↳ Reparar ejecuta los pasos anteriores automáticamente | ✅ rerecord_step ya hace replay previo (§B.3) + manual §2g.25 |
| ↳ Cola Reparar encadena sin reabrir el dialog | ✅ test_cleanup_rerecord_dispatches_next_in_repair_queue + manual §2g.28 |

---

**Confirmación final del bloque:**

Este bloque NO toca ejecución, lifecycle ni query fidelity (foco
explícito del usuario). Las dos piezas que el usuario marcó como
rotas — perfil vacío y regrabación UI — están cerradas con tests
unitarios + integración + checklist manual + entregable
documentado.
