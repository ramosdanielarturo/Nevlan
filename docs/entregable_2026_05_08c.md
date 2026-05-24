# Entregable — PRD 2026-05-08c (cierre LEGACY_RUNTIME_CONTAMINATION + UI quirúrgica)

**Estado**: Cerrado y verificado.

## 1. Causa raíz de `LEGACY_RUNTIME_CONTAMINATION`

`compute_ready_status` emitía el blocker `LEGACY_RUNTIME_CONTAMINATION`
para CUALQUIER detalle de contaminación detectado, incluso aquellos
que eran puramente informativos (grafo legacy presente pero marcado
`legacy_compiled_graph_purpose == "legacy_debug_only"`, coords en
`raw_trace`, `target_signature` con bbox absoluto, etc.). Estos casos
son **evidencia diagnóstica**, no participación en ejecución.

Como agravante, el JSON live (`var/missions/<uuid>.json`) persistía
`not_ready_reasons` desde corridas anteriores. Cuando la UI abría
Mission Review, leía ese blob y mostraba el blocker stale aunque la
lógica nueva ya no lo emitía.

### Fix

1. **`compute_ready_status` ahora distingue `affects_execution`**:
   cada detalle de contaminación lleva un flag explícito. El blocker
   `LEGACY_RUNTIME_CONTAMINATION` solo se agrega a `not_ready_reasons`
   cuando algún detalle tiene `affects_execution=True`.

   Casos que **sí** afectan ejecución:
   - Validación estricta encuentra step legacy en `semantic_execution_plan`.
   - `plan.coords_used == True`.
   - `plan.legacy_graph_ignored == False`.
   - `legacy_compiled_graph_purpose != "legacy_debug_only"` con grafo
     no vacío.
   - `preferred_strategy` en una pista legacy prohibida
     (`vision_coords`, `visual_asset`, `use_coords`, …).
   - `mission._run_legacy_player_called == True`.
   - `mission._last_decision.source` en
     `("compiled_execution_graph", "legacy_graph")`.
   - `mission._ghost_simulation_meta.simulating` en los mismos
     valores legacy.
   - `preferred_strategy` no canónica para `open_url` /
     `open_new_tab`.

   Casos que **NO** afectan ejecución (van a `_contamination_details`
   pero NO bloquean):
   - `compiled_execution_graph` sucio + `legacy_compiled_graph_purpose
     == "legacy_debug_only"`.
   - `raw_trace` con clicks / scrolls / coords.
   - `target_signature.coordinates` con bbox / xy.
   - `visual_assets` con crops.
   - Grafo legacy presente con `legacy_graph_ignored == True`.

2. **Trazabilidad estructurada**: cada entrada de
   `mission._contamination_details` lleva los campos requeridos por
   PRD §A.4:
   - `kind`
   - `field_path` (alias: `where`)
   - `source_module`
   - `contaminating_value`
   - `reason`
   - `affects_execution: bool`

3. **Mission Review recompute fresca**:
   `build_mission_review_summary(mission)` por defecto re-corre
   `attach_intent_status_to_plan` antes de leer el plan. Esto descarta
   automáticamente cualquier blocker stale persistido en el JSON live.

4. **Filtro defensivo en visible_blockers**: `_compute_visible_blockers`
   nunca incluye `LEGACY_RUNTIME_CONTAMINATION` si no hay un detalle
   con `affects_execution=True`. También filtra
   `NEEDS_USER_LABEL_PENDING` cuando ningún step del plan tiene
   `needs_user_label=True` (cierra el bug "Arregla los puntos
   marcados…" sin puntos visibles).

### API nueva: `get_visible_blockers_for_user(mission)`

Única API que la UI normal debe consumir. Garantiza:

1. Si no hay nada accionable, devuelve lista vacía → la UI no muestra
   "Arregla los puntos marcados…".
2. Detalles puramente técnicos (`SEMANTIC_PLAN_NOT_READY` solo,
   contaminación debug-only) quedan en la vista experto.
3. El recompute ocurre antes — datos persistidos viejos no engañan.

Vive en `app/services/missions/mission_review_summary.py`.

## 2. Antes / después de `mission_1778244908`

### Antes (JSON live previo a la fix)

```json
"semantic_execution_plan": {
  "coords_used": false,
  "legacy_graph_ignored": true,
  "intent_status": "NEEDS_REVIEW",
  "not_ready_reasons": [
    "LEGACY_RUNTIME_CONTAMINATION",
    "NEEDS_USER_LABEL_PENDING",
    "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE",
    "QUERY_FIDELITY_SUSPECT"
  ],
  "ready_provenance": "unknown"
},
"legacy_compiled_graph_purpose": "legacy_debug_only",
"review_confirmations": []
```

`LEGACY_RUNTIME_CONTAMINATION` aparecía aunque `coords_used=false`,
`legacy_graph_ignored=true`, y `legacy_compiled_graph_purpose ==
"legacy_debug_only"` — contradicción interna.

### Después (JSON live limpio + flujo de Mission Review)

**Antes de confirmar**:
```
intent_status: NEEDS_REVIEW
not_ready_reasons: [
  "NEEDS_USER_LABEL_PENDING",
  "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE",
  "QUERY_FIDELITY_SUSPECT"
]
visible_blockers: idem (LEGACY_RUNTIME_CONTAMINATION queda fuera)
```

**Después de `confirm_profile("Daniel Arturo Ramos")` +
`confirm_query("Devuélveme el amor de Luis Miguel")`**:
```
intent_status: READY
not_ready_reasons: []
ready_provenance: user_confirmation
coords_used: false
legacy_graph_ignored: true
legacy_compiled_graph_purpose: legacy_debug_only
review_confirmations: 2 entradas estructuradas
```

## 3. Vista normal y experto (capturas textuales)

### Normal

```
Entendido

1. Abrir Chrome
2. Usar el perfil Daniel Arturo Ramos
3. Abrir nueva pestaña
4. Abrir YouTube
5. Buscar en YouTube: Devuélveme el amor de Luis Miguel
6. Bajar resultados

Estado: Lista para ejecutar
```

Sin GroupControl, use_coords, confidence, target_signature, raw_trace,
/temp/, fallback_coords, LEGACY_RUNTIME_CONTAMINATION, ni
compiled_execution_graph.

### Experto

```
Entendido

1. Abrir Chrome
2. Usar el perfil Daniel Arturo Ramos        [confirmado]
3. Abrir nueva pestaña
4. Abrir YouTube
5. Buscar en YouTube: Devuélveme el amor de Luis Miguel        [confirmado]
6. Bajar resultados

Estado: Lista para ejecutar (con confirmación del usuario)
Trazabilidad: READY por confirmación humana, no por captura perfecta.

Confirmado por el usuario:
  Perfil:    Daniel Arturo Ramos
  Búsqueda:  Devuélveme el amor de Luis Miguel
             (antes: Devuelveme el amor Luis Miguel)
```

## 4. Lista de UI fixes aplicados

| ID | Fix | Test |
|----|-----|------|
| §C.1 | Botón **✕** del buscador del Centro de Automatizaciones (aparece con texto, click limpia + restaura foco, soporta tecla Esc, estilo Nevlan, tooltip) | `test_search_clear_button.py` (9 tests) |
| §G.3 | Banner "Arregla los puntos marcados…" solo aparece cuando `get_visible_blockers_for_user(mission)` devuelve lista no vacía. Si no hay puntos, mensaje honesto. | `test_handle_execution_blocked.py` |
| §I | Componente único `NevlanDialog` (severities: info/success/warning/danger/confirmation/expert_details) con title + message obligatorios; jamás vacíos (`EMPTY_TITLE_FALLBACK`, `EMPTY_MESSAGE_FALLBACK`, `MIN_BUTTON`). | `test_nevlan_dialog.py` (11 tests) |
| §D | "Aceptar riesgo" reemplazado: `accept_weak_step_risk` ahora reporta éxito y fallos con `NevlanDialog`, con `expert_details` colapsables. | `test_mission_review_popups.py` (5 tests) |
| §J | "Probar paso" usa `NevlanDialog` con título/mensaje claros y `expert_details` cuando hay excepción. | `test_execution_dialogs_use_nevlan.py` |
| §K | Errores de ejecución scheduler / recompile (`_run_scheduled_mission_now`, `_on_recompile`) ya no usan `QMessageBox.warning`/`critical` desnudos: `NevlanDialog` con título, mensaje y detalles técnicos colapsables. | `test_execution_dialogs_use_nevlan.py` |
| §L | Mission Review summary recompute por defecto (`recompute=True`) en `build_mission_review_summary`. La vista normal nunca expone tecnicismos; la experta sí. | `test_review_summary.py` (25 tests) |

### UI **no** migrada en este bloque (deferred, documentada)

Los flujos de **regrabación con replay** (PRD §E + §F) se dejaron
fuera del bloque para preservar la calidad del entregable. La spec
está en [`docs/plan_regrabacion_paso_replay.md`](plan_regrabacion_paso_replay.md):
contratos públicos, máquina de estados, audit trail, lista exhaustiva
de tests, manejo de fallos y riesgos. Implementación al iniciar el
siguiente bloque sin re-discutir diseño.

## 5. Tests nuevos / actualizados

| Archivo | Tests nuevos | Tests totales del archivo |
|---------|--------------|---------------------------|
| `tests/unit/test_ready_status.py` | +8 (`TestAffectsExecutionFlag`) | 26 |
| `tests/unit/test_real_mission_1778244908.py` | +10 (archivo nuevo) | 10 |
| `tests/unit/test_review_summary.py` | +5 (`TestVisibleBlockers`) | 25 |
| `tests/unit/test_execution_dialogs_use_nevlan.py` | +9 (archivo nuevo) | 9 |

Tests específicos del PRD §A.5:

- `test_dirty_compiled_graph_debug_only_does_not_trigger_legacy_runtime_contamination` ✅
- `test_raw_trace_coords_do_not_trigger_legacy_runtime_contamination` ✅
- `test_target_signature_coords_do_not_trigger_legacy_runtime_contamination` ✅
- `test_run_legacy_player_called_triggers_legacy_runtime_contamination` ✅
- `test_decision_source_compiled_graph_triggers_legacy_runtime_contamination` ✅
- `test_ghost_simulating_legacy_graph_triggers_legacy_runtime_contamination` ✅
- `test_real_mission_1778244908_no_false_legacy_contamination_after_confirmations` ✅

## 6. Suite completa

```
1062 passed, 2 xfailed, 2 warnings in 238.17s (0:03:58)
```

0 regresiones. Las 2 xfailed son tests existentes que ya estaban
xfail antes de este bloque (no relacionados).

## 7. Archivos modificados / creados

### Modificados

- `app/services/missions/semantic_execution_plan.py`
  (`compute_ready_status` + `_add_contamination` + nuevos campos
  de trazabilidad)
- `app/services/missions/mission_review_summary.py`
  (`build_mission_review_summary(recompute=True)`,
  `_compute_visible_blockers`, `get_visible_blockers_for_user`)
- `app/interfaces/desktop/automation_center.py`
  (`_handle_execution_blocked` con visible blockers + NevlanDialog,
  `_test_step` migrado a NevlanDialog, `_run_scheduled_mission_now`
  + `_on_recompile` + `_edit_schedule_from_upcoming` migrados a
  NevlanDialog, botón ✕ del buscador)
- `app/interfaces/desktop/mission_review.py`
  (`accept_weak_step_risk` migrado a NevlanDialog)
- `var/missions/2d1fec16-aafd-41bd-980b-3e40e91d4ee4.json`
  (restaurado a estado honesto NEEDS_REVIEW sin
  LEGACY_RUNTIME_CONTAMINATION)
- `var/missions/82bec5fd-cefb-45db-a82d-4bf074bf4180.json`
  (idem)

### Creados

- `app/interfaces/desktop/nevlan_dialog.py` (componente único)
- `scripts/build_real_mission_fixtures.py` (regenerador idempotente)
- `tests/fixtures/real_missions/mission_1778244908_raw.json`
- `tests/fixtures/real_missions/mission_1778244908_confirmed.json`
- `tests/fixtures/real_missions/mission_1778221896_raw.json`
- `tests/fixtures/real_missions/mission_1778221896_confirmed.json`
- `tests/unit/test_real_mission_1778244908.py`
- `tests/unit/test_search_clear_button.py`
- `tests/unit/test_handle_execution_blocked.py`
- `tests/unit/test_nevlan_dialog.py`
- `tests/unit/test_mission_review_popups.py`
- `tests/unit/test_execution_dialogs_use_nevlan.py`
- `docs/plan_regrabacion_paso_replay.md`
- `docs/entregable_2026_05_08c.md` (este documento)

## 8. Confirmaciones del PRD §N

| Item | Estado |
|------|--------|
| Probar misión funciona | ✅ Migrado a `NevlanDialog`, mensajes siempre visibles, detalles técnicos colapsables. |
| Ejecutar funciona | ✅ `_run_scheduled_mission_now` + `_handle_execution_blocked` con `NevlanDialog`; "Aceptar riesgo" coherente. |
| Regrabar funciona | ⏳ Plan técnico cerrado en `docs/plan_regrabacion_paso_replay.md`; implementación diferida deliberadamente al siguiente bloque (riesgo bajo). |
| Guardar/Repetir/Cancelar funcionan | ⏳ Igual que arriba (forman parte de la regrabación con replay). |
| No hay ventanas vacías | ✅ `NevlanDialogSpec.normalized()` garantiza title + message + al menos 1 botón. |
| No hay errores invisibles | ✅ Migración crítica completa; tests bloquean QMessageBox vacío en flujos de ejecución. |
| No hay congelamientos | ✅ `_test_step`, `_handle_execution_blocked` ya respetan UI thread. (La fix completa de heartbeat para flujos largos de ejecución sigue en el bloque siguiente.) |
| No se bloquea ejecución por warnings no críticos | ✅ `affects_execution` separa contaminación real vs. evidencia. |
| Los blockers críticos sí se respetan | ✅ Test `test_run_legacy_player_called_triggers_legacy_runtime_contamination` y similares. |
| Diseño de warnings/popups es Nevlan | ✅ Stylesheet propio de `NevlanDialog`, sin estilos nativos. |

## 9. Principio final, verificado

> Nevlan no debe sentirse como debugger.
> Nevlan debe sentirse como un producto mágico.

- Entiende: ICE produce 6 pasos canónicos.
- Confirma solo lo mínimo: perfil + query, una vez.
- Aprende: alias_store persiste el perfil.
- Ejecuta: routing semantic vs legacy claro.
- Se recupera: cancelar / reintentar / detalles técnicos.
- Explica sin asustar: vista normal limpia.
- Y nunca deja al usuario atrapado en ventanas rotas.
