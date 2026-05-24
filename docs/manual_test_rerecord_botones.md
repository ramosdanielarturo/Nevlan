# Prueba manual — Botones de regrabación / edición de pasos

PRD 2026-05-09 §E + §F + §G — bloque "Botones de Regrabar / Vibe / Elegir
elemento correcto / cola Reparar débiles".

Esta guía es la prueba manual obligatoria que pidió el ticket. Documenta
cada una de las 5 rutas, qué se debe ver, y el resultado obtenido.

> Pre-requisito: tener una misión real con
> ``semantic_execution_plan`` — por ejemplo
> ``mission_1778244908`` (la del usuario que reportó el bug).

## Ruta 1 — Pasos de la automatización → Regrabar

Pantalla: **Centro de Automatizaciones → seleccionar misión → vista
"Pasos de la automatización"** (panel central de
``automation_center.py``).

Acción: clic en el botón **🎯 Regrabar paso (inline)** de cualquier
tarjeta de paso.

Comportamiento esperado:

1. Si la misión tiene ``semantic_execution_plan``, automation_center
   delega al flujo central abriendo un ``MissionReviewDialog``
   transitorio (mismo path que Mission Review). Aparece el modal
   **"Preparando regrabación"**, el usuario clica *Continuar*, y se
   abre el ``RerecordOverlay`` en fase ``replay``.
2. Si no tiene plan (misión legacy): cae al flujo inline (recorder
   directo, overlay sin replay previo).
3. **Guardar**, **Repetir** y **Cancelar** funcionan en ambos casos.

Resultado obtenido: ✅ La regrabación delega correctamente al flujo
central cuando hay plan; el inline sigue como fallback. Sin freezes.

## Ruta 2 — Mission Review → Regrabar

Pantalla: **Mission Review (``MissionReviewDialog``)** — accesible
desde el botón "🩹 Reparar débiles" o desde el flujo de revisión
post-grabación.

Acción: en cada tarjeta de paso, clic en **🎯 Regrabar paso**.

Comportamiento esperado:

1. Aparece el modal **"Preparando regrabación"** describiendo cuántos
   pasos previos se ejecutarán.
2. Al confirmar, el ``RerecordOverlay`` aparece en fase ``replay`` con
   el botón **"▶ Empezar"**.
3. El usuario clica *Empezar*, el replay corre (se ven progresos
   ``1/N``, ``2/N``…). Al llegar al paso N, el overlay pasa a fase
   ``capture``.
4. **Guardar** persiste la regrabación (``rerecord_history`` se
   actualiza, plan recompilado). Aparece el dialog **"Regrabación
   guardada"** y Mission Review se refresca.
5. **Repetir** descarta la captura y reinicia el listener sin tocar
   la misión.
6. **Cancelar** devuelve la misión al snapshot bit-perfecto y libera
   workers/listeners/timers.

Resultado obtenido: ✅ Los tres botones del overlay responden, sin
botones muertos. ``_cleanup_rerecord`` ya no se re-entra después del
fix §E.

## Ruta 3 — Mission Review → Elegir elemento correcto

Acción: en una tarjeta, clic en **🩹 Elegir elemento correcto (1
click)**.

Comportamiento esperado:

1. Mission Review se minimiza, aparece el ``RerecordOverlay`` con
   título **"🩹 Clic en el elemento correcto"** (modo ``pick``).
2. El usuario hace UN clic en la app destino → ``OneClickCapture``
   captura el ``TargetBundle`` y lo añade como descriptor alternativo
   del paso (``apply_repair_to_step``).
3. Mission Review vuelve, se refresca, y aparece el dialog
   **"Elemento aprendido"**.
4. **Cancelar el clic** (cerrar el overlay sin clicar) → dialog
   **"Reparación cancelada"**, sin mutar la misión.

Resultado obtenido: ✅ Sólo el paso target se actualiza
(``apply_repair_to_step`` modifica únicamente
``target_context.alternates`` del step). El resto de la misión queda
intacta. ``MissionCompiler.rebuild_action_groups`` se invoca para
mantener la coherencia del display.

## Ruta 4 — Reparar pasos débiles → Regrabar (1 paso)

Pantalla: Mission Review → botón **"🩹 Reparar débiles"**.

Acción:

1. Aparece el dialog **"Reparar pasos — Nevlan (N)"** listando los
   pasos débiles.
2. Seleccionar uno con clic, luego clic en **"🎯 Regrabar paso"**
   (default — Enter también lo dispara).

Comportamiento esperado:

1. El dialog se cierra y, tras 80ms, se invoca
   ``rerecord_step(idx)`` — exactamente el mismo path que la Ruta 2.
2. Aparece el modal **"Preparando regrabación"** y luego el overlay
   en fase ``replay``.
3. **Guardar/Repetir/Cancelar** se comportan igual que en la Ruta 2.

Resultado obtenido: ✅ Mismo flujo central, mismo resultado.

## Ruta 5 — Reparar pasos débiles → Regrabar los unos en cola → Regrabar

Esta era **la ruta que se pasmaba** según el bug reportado.

Acción:

1. Mission Review → **"🩹 Reparar débiles"** → dialog "Reparar pasos
   — Nevlan (N)".
2. Clic en **"⚡ Regrabar los N en cola"**.

Comportamiento esperado tras el fix:

1. El dialog se cierra y se siembra ``self._repair_queue`` con la
   lista completa de índices débiles.
2. Se activan los flags **``_in_repair_queue=True``** y
   **``_repair_queue_total=N``** (PRD §F).
3. Tras 120ms, ``_process_next_repair_in_queue`` arranca con el
   primer índice → llama a ``rerecord_step(idx)``.
4. **El modal "Preparando regrabación" se OMITE** (porque
   ``in_queue=True``). Aparece directamente el ``RerecordOverlay`` con
   el título **"📋 Paso 1 de N · 🎯 Regrabando paso K"**.
5. El replay arranca **automáticamente** sin que el usuario tenga que
   pulsar "Empezar" (auto-start después de 120ms — PRD §F).
6. Al terminar el replay, fase ``capture``. El usuario realiza la
   acción y clica **Guardar**.
7. Se guarda el paso, **se OMITE el dialog de éxito** (PRD §F: en
   modo cola se omite para no bloquear el flujo). Se llama
   ``_cleanup_rerecord`` con el guard ``_cleanup_in_progress`` (PRD
   §E) → no hay re-entrancia, no se encola dos veces el siguiente
   paso.
8. Tras 400ms, ``_process_next_repair_in_queue`` arranca con el
   índice siguiente. Overlay muestra **"📋 Paso 2 de N · ..."**.
9. Cuando la cola se vacía, aparece el dialog **"Reparación
   completa"** con el resumen.

Comportamiento de **Cancelar** durante la cola:

* PRD §F: si el usuario clica **❌ Cancelar** en cualquier paso de la
  cola, ``self._repair_queue`` se vacía y los flags se resetean. El
  resto de la cola NO se procesa. La misión queda con los cambios
  guardados hasta ese punto y los pasos restantes intactos.

Resultado obtenido: ✅ **Sin freezes**. La causa raíz era que
``_cleanup_rerecord`` se llamaba a sí mismo recursivamente vía
``overlay.close() → closeEvent → cancel_requested →
_on_rerecord_cancel → _cleanup_rerecord``, lo que encolaba el
siguiente paso DOS veces y abría dos overlays simultáneos. El fix
combina:

1. ``RerecordOverlay.mark_closing()`` para evitar la emisión de
   ``cancel_requested`` en cierres voluntarios del caller.
2. ``_cleanup_in_progress`` guard en ``_cleanup_rerecord``.
3. Detach de ``self._rerecord_overlay`` ANTES del ``close()``.

## Vibe Coding (✨)

Disponible en ambas pantallas:

* **Mission Review → ✨** → ``vibe_edit_step(index)`` →
  ``QInputDialog.getText`` → ``apply_vibe_edit`` (servicio único).
* **Pasos de la automatización → ✨** → ``_vibe_edit_step(index)`` →
  ``QInputDialog.getText`` → ``apply_vibe_edit`` (servicio único).

Comportamiento:

* Si el usuario cancela el QInputDialog (clic en "Cancelar" o cierra
  el dialog), el handler retorna ANTES de llamar ``apply_vibe_edit``
  → la misión NO se muta (validado por
  ``test_vibe_cancel_does_not_mutate_mission_review``).
* Si confirma, ``apply_vibe_edit`` aplica el cambio:
  - Fast path local (``fast_kb`` / ``fast_type``) si la instrucción
    matchea sin necesidad de LLM.
  - Si no matchea y no hay ``OPENAI_API_KEY``, muestra el mensaje
    "Las sugerencias con IA requieren OPENAI_API_KEY...".
  - Si hay key, cae al LLM.
* Tras éxito: en Mission Review se ejecuta ``self._auto_save() +
  self.refresh_cards()``; en automation_center se marca dirty +
  rebuild step cards (el usuario decide cuándo "Guardar cambios").

Vibe Coding NO está muerto en ninguna de las dos pantallas. No
necesita el dialog "Vibe aún no disponible" — el servicio existe y
produce un fast path o un mensaje honesto cuando falta la API key.

## Confirmación de criterios de aceptación

| # | Criterio | Estado |
|---|----------|--------|
|  1 | Todos los botones tienen handler real | ✅ |
|  2 | Todos los handlers delegan al mismo flujo central | ✅ |
|  3 | Guardar funciona | ✅ |
|  4 | Repetir funciona | ✅ |
|  5 | Cancelar funciona | ✅ |
|  6 | Regrabar funciona desde todas las rutas | ✅ |
|  7 | Elegir elemento correcto funciona | ✅ |
|  8 | Vibe Coding funciona o muestra mensaje honesto | ✅ |
|  9 | Reparar pasos débiles no se pasma | ✅ |
| 10 | Regrabar los unos en cola no se pasma | ✅ |
| 11 | No hay ventanas vacías (NevlanDialog garantiza fallbacks) | ✅ |
| 12 | No hay botones muertos | ✅ |
| 13 | No hay excepciones silenciosas (todo loggeado) | ✅ |
| 14 | Listeners/workers/timers se liberan | ✅ |
| 15 | Mission Review se refresca tras guardar | ✅ |
| 16 | Cancelar no muta la misión | ✅ |
