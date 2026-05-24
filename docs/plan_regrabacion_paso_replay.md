# Plan técnico — Regrabación de paso con replay previo

> **Estado**: diseño cerrado, **implementación diferida** al siguiente bloque
> de trabajo (PRD 2026-05-08c §E + §F).
>
> Este documento es el contrato exacto de lo que se va a implementar. Al
> retomarlo, no hay decisiones abiertas; solo código a escribir contra esta
> spec.

## 1. Objetivo

Cuando el usuario pide **Regrabar paso N**, Nevlan debe:

1. Ejecutar automáticamente los pasos `1 ... N-1` del `semantic_execution_plan`
   (no del legacy graph).
2. Detenerse exactamente antes de ejecutar el paso N.
3. Poner Nevlan en modo "esperando que el usuario realice el paso N".
4. Capturar SOLO la nueva evidencia del paso N.
5. Reemplazar el paso N en el plan, recompilar, recalcular `intent_status`.
6. Preservar audit trail (`original_step_id` → `new_step_id`,
   `rerecorded_at`, `user_action`, `affected_steps`).

Nada de pasos anteriores ni posteriores debe verse alterado, salvo los
posteriores cuyas dependencias declaradas apunten al paso regrabado.

## 2. Contratos públicos

### 2.1 Nuevo verbo en `mission_review_confirm.py`

```python
def rerecord_step(
    mission: Mission,
    *,
    step_index: int,
    new_step_evidence: Dict[str, Any],
    rerecorded_at: datetime,
    user_action: str = "rerecord",
) -> SemanticExecutionPlan:
    """Reemplaza el step ``step_index`` con la nueva evidencia y
    recompila plan + intent_status. Preserva audit trail."""
```

* Idempotente respecto a `step_index` + `rerecorded_at`.
* `original_step_id` se persiste dentro del nuevo step en
  `params._original_step_id`.
* La misión adquiere una entrada en `mission.review_confirmations`
  con `field="step_replacement"`, `source="rerecord"`,
  `step_id=<new_id>`, `original_value=<original_id>`.
* `affected_steps` se persiste en `mission._rerecord_audit` con la
  lista de IDs cuya `params.depends_on` incluía al original.

### 2.2 Nuevo módulo `rerecord_session.py`

Servicio (sin Qt) que orquesta la sesión completa.

```python
@dataclass
class RerecordSession:
    mission: Mission
    target_step_index: int
    state: str  # "idle" | "replaying" | "waiting_for_user" | "captured"
                # | "saved" | "cancelled" | "failed"
    progress: float      # 0..1
    error_message: str
    captured_event: Optional[RawEvent] = None

class RerecordOrchestrator:
    """Maquina de estados aislada de la UI:

      start(mission, step_index) -> session
      proceed_to_capture()       -> bool
      capture_user_event(ev)     -> bool
      save()                     -> SemanticExecutionPlan
      cancel()                   -> Mission   # restaura estado original
    """
```

* `start` crea un snapshot inmutable de la misión (deep copy del blob)
  para poder hacer `cancel()` bit-perfecto.
* `proceed_to_capture` corre `run_steps_dry(mission, plan, end_index=N)`:
  ejecuta del semantic plan los pasos `0..N-1`. Si falla, deja
  `state="failed"` con `error_message` no vacío y `affected_steps=[]`.
  El UI debe ofrecer Reintentar / Regrabar manual / Cancelar
  (ver §6).
* `capture_user_event` recibe el RawEvent del overlay y construye
  un `SemanticPlanStep` reemplazo. Aplica `assess_step_quality` para
  rechazar capturas inválidas (sin UIA, sin coords, etc.).
* `save` invoca `mission_review_confirm.rerecord_step` con la
  evidencia capturada. Tras la mutación, llama a
  `attach_intent_status_to_plan(plan, mission=m)` para recomputar.
* `cancel` restaura el snapshot inicial sin tocar disco.

### 2.3 UI — `rerecord_overlay.py`

Estado actual del overlay: muestra ventana con "Repetir | Cancelar |
Guardar". La sesión actual NO ejecuta los pasos previos.

Cambios:

* Constructor recibe `RerecordOrchestrator` en lugar de un mission +
  index sueltos.
* Botón **"Empezar"** dispara `proceed_to_capture()` y muestra
  progreso paso a paso (1/N, 2/N, …, N-1/N).
* Cuando llega a `state="waiting_for_user"`, se cambia el copy a:
  `Llegué al paso que quieres regrabar. Realiza nuevamente este paso
  y presiona Guardar.`
* Botón **"Guardar"** valida (`captured_event is not None`) y llama
  `save()`. Si la captura no es válida, muestra `NevlanDialog
  show_warning` con el motivo (no error vacío).
* Botón **"Repetir"** descarta `captured_event` y vuelve a
  `state="waiting_for_user"`.
* Botón **"Cancelar"** llama `cancel()`. Cierra la ventana.
* Heartbeat: timer de 30s sin progreso muestra
  `NevlanDialog show_warning` con "Sigue esperando | Cancelar".

## 3. Reglas duras

1. **Nunca** ejecutar el paso N. La sesión termina justo antes.
2. **Nunca** mutar `raw_trace` (mismo invariante que Mission Review).
3. **Nunca** producir un overlay/dialog vacío. Toda transición de
   estado emite copy explícito.
4. **Nunca** dejar listeners/timers/threads vivos al cerrar. El
   orchestrator expone `release()` y la UI lo llama en `closeEvent`.
5. **Atajos**: `Esc` = `cancel()`. `Enter` (cuando hay captura
   válida) = `save()`.

## 4. Persistencia del audit trail

Tras `save()`, la misión debe contener:

```json
{
  "review_confirmations": [
    {
      "field": "step_replacement",
      "value": "<new_step_id>",
      "source": "rerecord",
      "step_id": "<new_step_id>",
      "step_type": "<type>",
      "original_value": "<original_step_id>",
      "note": "rerecord; affected_steps=[...]"
    }
  ],
  "_rerecord_audit": {
    "<new_step_id>": {
      "original_step_id": "...",
      "rerecorded_at": "2026-...",
      "user_action": "rerecord",
      "affected_steps": ["..."]
    }
  }
}
```

## 5. Tests obligatorios (PRD §E + §F)

Ya queda redactada la lista en `docs/plan_regrabacion_paso_replay.md`
(este archivo). Al implementar:

* `tests/unit/test_rerecord_session.py`
  * `test_rerecord_runs_previous_steps_before_target`
  * `test_rerecord_stops_at_target_step`
  * `test_rerecord_only_replaces_target_step`
  * `test_rerecord_does_not_mutate_previous_steps`
  * `test_rerecord_recomputes_semantic_plan`
  * `test_rerecord_failure_shows_visible_error`
  * `test_rerecord_cancel_restores_original_mission`
* `tests/unit/test_rerecord_overlay_buttons.py`
  * `test_rerecord_save_persists_new_step`
  * `test_rerecord_repeat_discards_current_capture`
  * `test_rerecord_cancel_restores_original_state`
  * `test_rerecord_buttons_enabled_only_when_valid`
  * `test_rerecord_window_does_not_leave_background_listener_running`

## 6. Manejo de fallos previos

Si `proceed_to_capture()` falla mientras corre `1..N-1`:

* `state = "failed"`.
* `error_message` no vacío. Frase fija: `"No pude llegar
  automáticamente al paso a regrabar."` + detalle del paso que
  falló.
* La UI usa `NevlanDialog show_warning` con tres botones:
  * `"Reintentar"` → vuelve a llamar `proceed_to_capture()`.
  * `"Regrabar desde aquí manualmente"` → cambia a
    `state="waiting_for_user"` saltando el replay.
  * `"Cancelar"` → `cancel()`.

## 7. Riesgo conocido — efectos colaterales del replay

El replay corre el plan semántico contra el sistema real (abre
Chrome, navega, etc.). Antes de ejecutar:

* Verificar que `policy.requires_human_approval_on_fail` esté
  respetado.
* Mostrar al usuario una advertencia de "esto va a controlar tu
  computadora durante 1..N-1; ¿continuar?" la primera vez (después
  recordar la elección por sesión).
* Cooperative cancel: cada step del replay debe revisar
  `orchestrator.is_cancel_requested` antes de proceder. Si está
  en `True`, retorna sin ejecutar el resto.

## 8. Decisión deliberadamente diferida

El usuario pidió no tocar regrabación completa si pone en riesgo la
entrega de este bloque. Esta sección es el contrato exacto para que
el siguiente bloque arranque sin re-discutir diseño.
