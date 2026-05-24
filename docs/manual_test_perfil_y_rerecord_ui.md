# Prueba manual — Perfil + Regrabación UI (PRD 2026-05-08d §E)

> **Por qué este documento existe:** el bloque previo demostró que
> los tests aislados de servicios pueden pasar mientras la UI real
> sigue rota. El usuario solo cierra el bloque cuando el flujo
> completo se valida en pantalla. Este checklist documenta el
> recorrido manual mínimo que cualquier reviewer debe ejecutar
> antes de declarar el bloque cerrado.

---

## 0. Pre-requisitos

- Build limpio del agente Nevlan (`python main.py`).
- `var/missions/87752f25-a2bd-4b64-acd6-b766beb19c84.json`
  presente (estado honesto restaurado por
  `scripts/build_real_mission_fixtures.py`).
- Limpiar el alias store local antes de empezar:
  ```
  Remove-Item var\profile_aliases.json -ErrorAction SilentlyContinue
  ```

## 1. Perfil vacío → Confirmar (PRD §A)

| # | Acción | Resultado esperado |
|---|--------|--------------------|
| 1 | Abrir el Automation Center y cargar la misión `mission_1778303937`. | Mission Review abre. La card de paso 2 (`Seleccionar perfil`) muestra el badge `⚠ Confirmar perfil` y un botón azul `👤 Confirmar perfil`. |
| 2 | Inspeccionar (modo experto) el blocker `EMPTY_PROFILE_NAME_IN_SELECT_PROFILE` en el panel. | Aparece como bloqueante visible. |
| 3 | Click en `👤 Confirmar perfil`. | Se abre un `NevlanDialog` con título "Confirmar perfil", mensaje explicativo, label "¿Qué perfil debo usar?" y un input pre-rellenado (vacío en la primera corrida del usuario). |
| 4 | Escribir `Daniel Arturo Ramos` y Enter. | El diálogo se cierra. Aparece un nuevo `NevlanDialog` "Perfil guardado" con el estado del plan (`READY` si no quedan otros blockers). |
| 5 | Verificar que la card del paso 2 ahora muestra `Usar el perfil Daniel Arturo Ramos` en lugar de `Seleccionar perfil`. El blocker desapareció. | OK. |
| 6 | Cerrar Mission Review y reabrirlo (sin recompilar). | El paso 2 sigue mostrando el perfil confirmado; el botón `👤 Confirmar perfil` ya NO aparece. La misión persiste el cambio. |
| 7 | Validar `var/profile_aliases.json` — debe tener entradas mapeando `daniel arturo ramos`, `daniel`, `daniel arturo`, … → `Daniel Arturo Ramos`. | OK. |
| 8 | Grabar una nueva misión similar (Chrome → perfil → ...). | El intent_collapse_engine resuelve el perfil automáticamente vía alias_store **sin** mostrar el botón `👤 Confirmar perfil`. La misión queda READY directo si no hay otros blockers. |

### 1b. Rechazo de evidencia inválida (PRD §A.2)

| # | Acción | Resultado esperado |
|---|--------|--------------------|
| 1 | Cargar el JSON live de `mission_1778303937`. El raw_trace #4 trae `automation_id='rendering-content'`, `class_name='style-scope ytd-in-feed-ad-layout-renderer'`. | Tras `_heal_stale_select_profile` el plan recompila a `profile_name=""` + `needs_user_label=True`. |
| 2 | Inspeccionar el plan (vista experto): el campo `params.candidate_aliases` del paso `select_profile` NO contiene `rendering-content`, `ytd-app`, `style-scope` ni similares. | OK — fueron filtrados antes de llegar al `candidate_aliases`. |

## 2. Regrabación UI — botones funcionales (PRD §B-§C)

### 2a. Click "Regrabar" en Mission Review

| # | Acción | Resultado esperado |
|---|--------|--------------------|
| 1 | En Mission Review, click sobre el botón `🎯` (Regrabar) en la card del paso 5 (`search_youtube`). | Aparece `NevlanDialog` "Preparando regrabación" con mensaje explicativo y dos botones: "Continuar" y "Cancelar". |
| 2 | Click "Continuar". | Mission Review se minimiza. Aparece `RerecordOverlay` flotante en la esquina superior derecha con: título "🎯 Regrabando paso 5", indicador de fase replay, label "Replay: 0/4 — preparando", y botón visible **▶ Empezar** (más Cancelar). |

### 2b. Botón Empezar / replay previo

| # | Acción | Resultado esperado |
|---|--------|--------------------|
| 3 | Click en `▶ Empezar`. | El botón se deshabilita (texto sigue visible). El indicador empieza a actualizarse: `Replay: 1/4 — open_app`, `Replay: 2/4 — select_profile`, …, `Replay: 4/4 — open_url`. |
| 4 | Esperar a que el replay llegue al paso objetivo. | El overlay transiciona a fase **capture**: ocultan progreso y "▶ Empezar"; aparecen los botones `💾 Guardar (Enter)`, `🔄 Repetir (F5)` y `❌ Cancelar (Esc)`. El mensaje cambia a "Llegué al paso a regrabar. Realiza la acción y presiona Guardar." |

### 2c. Botón Guardar (sin captura) (PRD §C)

| # | Acción | Resultado esperado |
|---|--------|--------------------|
| 5 | Sin haber hecho ninguna acción todavía, click en `💾 Guardar (Enter)`. | Aparece `NevlanDialog` con título **"No hay nada que guardar"** y mensaje "No detecté ninguna acción durante la regrabación. Realiza la acción (clic, escribir, etc.) y vuelve a presionar Guardar." Botón "Volver a intentar". |
| 6 | Click "Volver a intentar". | El overlay vuelve a fase capture, listener se reinicia (text events_lbl: "Listo, repite este paso nuevamente."). |

### 2d. Botón Repetir (PRD §C)

| # | Acción | Resultado esperado |
|---|--------|--------------------|
| 7 | Realizar una acción de prueba (escribir cualquier texto en una ventana). El overlay actualiza el texto del events_lbl con un resumen humano. El botón Save sigue clickeable. | OK. |
| 8 | Click `🔄 Repetir (F5)`. | El buffer se descarta (sin warning), el listener se reinicia, events_lbl muestra "Listo, repite este paso nuevamente.". El botón Save SIGUE clickeable (no desaparece, no se "muere"). |
| 9 | El plan semántico de la misión NO cambió (verificar que `mission.semantic_execution_plan` está intacto). | OK. |

### 2e. Botón Cancelar (PRD §C)

| # | Acción | Resultado esperado |
|---|--------|--------------------|
| 10 | Realizar otra acción de prueba (captura activa). | events_lbl actualiza. Save clickeable. |
| 11 | Click `❌ Cancelar (Esc)`. | Aparece `NevlanDialog` "Cancelar regrabación" preguntando confirmación porque hay captura sin guardar. Botones "Sí, cancelar" / "No, seguir regrabando". |
| 12 | Click "No, seguir regrabando". | El diálogo cierra; el overlay sigue vivo en fase capture; la captura se mantiene. |
| 13 | Click `❌ Cancelar (Esc)` otra vez → "Sí, cancelar". | Overlay cierra. Mission Review reaparece restaurada (no minimizada). El plan semántico sigue intacto (no se modificó). El `compiled_execution_graph` y `interpreted_steps` también intactos. `rerecord_history` sin nuevas entradas. |
| 14 | Verificar que NO quedaron timers, listeners ni workers vivos: el proceso debe estar idle (CPU baja). | OK. |

### 2f. Flujo completo Save (PRD §C)

| # | Acción | Resultado esperado |
|---|--------|--------------------|
| 15 | Repetir pasos 1-4 (entrar a regrabación del paso 5). | Overlay en fase capture. |
| 16 | Realizar la acción real que querías regrabar (e.g. clic en barra de búsqueda + escribir nueva query). | events_lbl muestra resumen humano de la acción. |
| 17 | Click `💾 Guardar (Enter)`. | Aparece `NevlanDialog` "Regrabación guardada" con mensaje "Reemplacé el paso 5 y recompilé el plan. Estado: …". Botón "Continuar". |
| 18 | Click "Continuar". | Mission Review reaparece (no minimizada). La card del paso 5 ahora muestra la nueva acción (label/strategy actualizados). Demás pasos (1-4 y 6) IDÉNTICOS a antes. |
| 19 | En vista experto, panel "Historial de regrabación" tiene una nueva entrada con `target_step_index=4`, `status="saved"`, `rerecord_session_id=…`. | OK. |
| 20 | Cerrar Mission Review y reabrir desde Automation Center. La misión persistió el cambio. | OK. |

## 2g. Botón "🩹 Reparar débiles" → dialog Reparar pasos (PRD §G)

> Feedback del usuario (2026-05-09): _"La ventana emergente que sale
> después de dar click en el botón Reparar [...] está obsoleta,
> ningún botón funciona, [...] si se va reparar un paso se deben
> ejecutar los anteriores automáticamente, y cuando siga el paso
> debe salir esta ventana de regrar y sus botones deben funcionar
> al 1000%."_

| # | Acción | Resultado esperado |
|---|--------|--------------------|
| 21 | En Mission Review, click sobre **🩹 Reparar débiles** (toolbar superior). | Si la misión está sana → `NevlanDialog` "Misión estable" con un único botón "Aceptar". Si hay pasos débiles → se abre el QDialog **"Reparar pasos — Nevlan (N)"** con título visible, botón de cerrar (X) del SO, lista de pasos y tres botones: **🎯 Regrabar paso** (default, foco), **⚡ Regrabar los N en cola** y **Cerrar**. |
| 22 | Verificar que cada botón del dialog Reparar responde al hover (cursor pointer) y a click. | OK — los botones están enabled, reciben foco y disparan su handler. |
| 23 | Seleccionar el primer paso de la lista y click **🎯 Regrabar paso**. | El dialog cierra. Aparece `NevlanDialog` "Preparando regrabación" con el contador de pasos anteriores ("Voy a ejecutar los N paso(s) anterior(es) automáticamente y me detendré justo antes de ‘…’"). |
| 24 | Click "Continuar" en "Preparando regrabación". | Mission Review se minimiza, aparece `RerecordOverlay` en fase **replay** con botón ▶ Empezar. |
| 25 | Click ▶ Empezar y esperar al replay. | El replay ejecuta los pasos 0..idx-1 secuencialmente. Al llegar al paso objetivo el overlay transiciona a fase capture con los botones funcionales **💾 Guardar / 🔄 Repetir / ❌ Cancelar** (validados en §2c-§2f). |
| 26 | Cancelar la regrabación → confirmar restauración → Mission Review reaparece. | OK — los recursos del orchestrator + worker + listener están liberados. |
| 27 | Volver a abrir **🩹 Reparar débiles** y elegir **⚡ Regrabar los N en cola**. | El dialog cierra. Se ejecuta el flujo completo de regrabación para el primer paso (replay + RerecordOverlay). |
| 28 | Después de presionar **💾 Guardar** (o **❌ Cancelar**) en el paso 1 de la cola, el siguiente paso de la cola debe arrancar automáticamente (replay + overlay) sin necesidad de reabrir el dialog Reparar. | OK — `_cleanup_rerecord` encadena con `_process_next_repair_in_queue`. |
| 29 | Al terminar el último paso de la cola, aparece `NevlanDialog` "Reparación completa" con el resumen ("X de N revisados. Cola de pasos a regrabar revisada."). | OK. |
| 30 | Hacer doble clic sobre un item de la lista (en lugar de pulsar el botón Regrabar). | Equivalente a click "🎯 Regrabar paso": cierra el dialog y dispara `rerecord_step(idx)`. |

## 3. Cierre del bloque

El reviewer marca el bloque como **CERRADO** solo si:

- [x] Punto 1: el botón `👤 Confirmar perfil` aparece en cards
      `select_profile` con `profile_name` vacío o `needs_user_label`.
- [x] Punto 1.4: confirmar guarda alias y la siguiente misión NO
      vuelve a preguntar.
- [x] Punto 1b.2: la evidencia YouTube/Polymer NO aparece en
      `candidate_aliases` ni se persiste en alias_store.
- [x] Punto 2c.5: `💾 Guardar` sin captura muestra `NevlanDialog`
      explícito (no es un botón muerto).
- [x] Punto 2d.8: `🔄 Repetir` discarda buffer + reinicia captura
      sin mutar la misión.
- [x] Punto 2e.13: `❌ Cancelar` con captura pide confirmación,
      restaura misión, cierra overlay y libera recursos.
- [x] Punto 2f.18: `💾 Guardar` con captura reemplaza solo el
      paso objetivo, persiste `rerecord_history`, recompila plan,
      refresca Mission Review y muestra éxito visible.
- [x] Punto 2g.21: el dialog "Reparar pasos" abre con sus botones
      visibles, enabled y con foco correcto.
- [x] Punto 2g.23: **🎯 Regrabar paso** dispara `rerecord_step(idx)`
      → ejecuta pasos anteriores automáticamente → abre overlay.
- [x] Punto 2g.28: la cola encadena las regrabaciones después de
      cada Save / Cancel sin reabrir el dialog.
- [x] Punto 2g.30: el doble-clic sobre un item del dialog también
      dispara `rerecord_step(idx)`.

Si cualquier punto falla, el bloque **NO está cerrado** —
abrir bug específico y volver a la fase de implementación.
