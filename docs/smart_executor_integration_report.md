# SmartMissionExecutor — Reporte de Integración (PRD 2026-05-03d)

## Objetivo

Dejar a Nevlan ejecutando misiones semánticas con el `SmartMissionExecutor`
(autocurable, basado en estado), NO con el `MissionPlayer` legacy, y que la
ejecución sea rápida, verificable y autocurable.

## Qué player usa cada misión y por qué

| Tipo de misión                                | Player usado         | Motivo                                                                 |
| --------------------------------------------- | -------------------- | ---------------------------------------------------------------------- |
| `status=EXECUTABLE` con bloques semánticos    | `SmartMissionExecutor` | PRD §1. Ruta oficial: gate + adapter + state machine autocurable.     |
| `status=EXECUTABLE` pero **sin** bloques semánticos (misión legacy pura) | `MissionPlayer` legacy | PRD §1. Fallback con advertencia: "Esta misión usa el ejecutor antiguo; puede ser menos precisa." |
| `status=NEEDS_REVIEW` o `COMPILED_DRAFT`      | **Ninguno** — bloqueada | PRD §3. El gate clasifica la misión y se muestra al usuario los bloqueantes + se abre Mission Review. |

La decisión la toma `app.services.missions.smart_runner_bridge.decide_player(mission)`.

## Piezas nuevas

| Archivo                                                   | Responsabilidad                                                                |
| --------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `app/services/missions/semantic_adapter.py`                | Convierte `Mission` / `interpret_mission()` → `list[MissionStep]`. Bloquea si hay perfil/target/query vacíos o kinds desconocidos. |
| `app/services/missions/smart_runner_bridge.py`             | Punto único de entrada: aplica gate, elige player, llama callbacks humanos. Sin dependencia de Qt (testeable headless). |
| `scripts/test_smart_executor_youtube.py`                   | E2E real + dry-run de la misión "Chrome → perfil → nueva pestaña → YouTube → búsqueda". Verifica PRD §5/§9/§14. |
| `tests/unit/test_semantic_adapter.py`                      | 23 tests: mapeos, blockers, detección `is_mission_semantic`, input dict/list/Mission. |
| `tests/unit/test_smart_runner_bridge.py`                   | 11 tests: routing Smart/Legacy/Blocked, gate enforcement, happy path y "no avanzar a ciegas". |

## Piezas modificadas

### `app/services/missions/execution_contracts.py`
* `select_profile` — post-condición ahora exige **picker cerrado** +
  Chrome activo + (si el nombre de perfil es conocido) título
  coincide. Añadido predicado `_picker_visible_pred`.
* `open_url` — post-condición acepta `domain_matches`, `url_contains`
  **o** `title_contains(<alias corto>)` (funciona sin Playwright CDP).
* `search_youtube` — añadido fallback DOM `search_youtube:dom_search_input`
  (`#search-input input`) antes de UIA/OCR/visual/coords, y la
  post-condición ahora también acepta que el título contenga un token de la query.

### `app/services/missions/action_runner.py`
* `_select_profile_skip_if_loaded` — PRD §6: ya no acepta "cualquier URL"
  como éxito silencioso. Si el picker sigue visible devuelve fail; si la
  URL es real y el título contiene el perfil, marca `verified_profile=True`;
  si no, skip suave con `verified_profile=False` (auditoría trazable).
* `_open_url_hotkey` — PRD §7: limpia la omnibox con `Ctrl+A` + `Del`
  antes de escribir la URL (fix de bug "URL concatenada al anterior").
* `_search_youtube_dom_search_input` (nuevo) — selector DOM alternativo
  para el caso en que YouTube use un layout A/B distinto.

### `app/services/missions/recovery_engine.py`
* `_recipe_search` — PRD §8: si no estamos en YouTube, rollback al
  checkpoint `youtube_loaded`. Mensaje humano diferenciado para rollback vs retry.

### `app/services/missions/checkpoints.py`
* `CheckpointManager.save` — registra **alias amigables** además de la key
  raw: `youtube.com_loaded` ↔ `youtube_loaded`, `google.com_loaded` ↔
  `google_loaded`, etc. Así el `RecoveryEngine` los encuentra sin tener
  que adivinar el formato.

### `app/interfaces/desktop/automation_center.py`
* `_on_execute` — primero llama a `decide_player(mission)`; si está
  bloqueada abre Mission Review (`_handle_execution_blocked`); si es
  semántica llama a `_run_mission_with_smart_executor`; si es legacy
  muestra advertencia humana + `_run_mission_with_legacy_player`.
* `_run_scheduled_mission_now` — misma lógica para ejecuciones
  programadas (sin diálogos ya que no hay usuario delante).

### `app/interfaces/desktop/mission_review.py`
* `test_full_mission` — ahora ruta por `run_mission_smart` con
  `allow_legacy_fallback=True` + `run_legacy_player=...`.

## Mensajes humanos (PRD §4)

El `SmartMissionExecutor` ya emitía frases como "Verificando Chrome...",
"Chrome listo.", "Buscando en YouTube: ...", "Resultados listos." por
cada step. El bridge las reenvía a `on_status` y la UI las muestra en la
barra de estado de AutomationCenter. Los mensajes de fallo se convierten
en un `QMessageBox` cuando `needs_human` se dispara.

## Modo experto (PRD §12)

`run_mission_smart(..., expert_mode=True)` activa:
* Logs técnicos por step: `strategy`, `retries`, `duration_ms`.
* Líneas `[expert] → kind :: try strategy (#N)` en el log de la app.

En la UI, el flag se lee de `self._expert_mode_enabled` (atributo
opcional; por defecto desactivado). El script E2E acepta `--expert`.

## Aprendizaje + prioridades (PRD §11)

Sin cambios funcionales en `LearningLogger` — ya registra cada intento
con `step_kind`, `strategy`, `success/failure`, `retries`,
`state_before`, `state_after`, `correction_applied`. El `ActionRunner`
ya consume `learning.rank_strategies(...)` al inicio y dentro del
recovery loop para reordenar fallbacks según score histórico.

La nueva estrategia `search_youtube:dom_search_input` se integra
automáticamente al ranker: empieza con score neutro 0.5 y sube/baja
según resultados reales.

## Checkpoints (PRD §10)

* `chrome_profile_loaded` — guardado tras `select_profile`.
* `youtube_loaded` / `youtube.com_loaded` — guardado tras `open_url`
  cuando el dominio es youtube.com (alias redundante).
* `search_results_visible` — guardado tras `search_youtube` exitoso.

Cada checkpoint incluye `app`, `window_title`, `url`, `visible_text_keys`,
`step_id`, `block_id`, `timestamp`, `recovery_hint`. El `RecoveryEngine`
los consume al decidir rollback (ver recipe `_recipe_search` / `_recipe_open_url`).

## Criterio final de aceptación (PRD §14)

| Requisito                                                          | Dónde se enforza                                        |
| ------------------------------------------------------------------ | ------------------------------------------------------- |
| No usa MissionPlayer legacy para misiones EXECUTABLE              | `smart_runner_bridge.decide_player()` + test E2E `assert_smart_executor_is_used`. |
| No ejecuta pasos rojos                                             | `approval_gate.classify_status` → `NEEDS_REVIEW/COMPILED_DRAFT` → bridge marca `blocked=True`. |
| No ejecuta profile vacío                                           | `semantic_adapter` emite `AdaptedBlocker(code='empty_profile')`. |
| No ejecuta target vacío                                            | `semantic_adapter` emite `AdaptedBlocker(code='empty_target')`. |
| No avanza tras fallas                                              | `SmartMissionExecutor._run_step` — `break` si `failed`/`needs_human`. Verificado por `test_failed_precondition_stops_execution` y `assert_no_blind_advance`. |
| Recupera desde checkpoints                                         | `RecoveryEngine` + `CheckpointManager` con aliases amigables. |
| Termina en resultados de búsqueda de YouTube                       | `search_youtube` post-condition: `search_query=` en URL, `/results?` en URL, o query en título. Verificado por `assert_final_result_reaches_youtube`. |

## Resultados de pruebas

```
tests/unit — 463 tests pasan (206s)
  - test_semantic_adapter.py       23 ok
  - test_smart_runner_bridge.py    11 ok
  - resto del suite (no regresión) 429 ok

scripts/test_smart_executor_youtube.py --dry-run --expert
  - 5 steps, 5 ok (1 skipped = open_new_tab, porque select_profile
    ya dejó el título en "Nueva pestaña"), duración total 5.46s
  - Player usado: smart
  - Gate status: executable
  - PRD §14 cumplido
```

## Cómo correr el E2E real

```bash
# Dry-run (CI / sin Chrome):
python scripts/test_smart_executor_youtube.py --dry-run --expert

# Real (abre Chrome + YouTube + busca):
python scripts/test_smart_executor_youtube.py --expert

# Con reporte JSON:
python scripts/test_smart_executor_youtube.py --save-report var/reports/e2e.json
```

El script imprime un reporte breve al final indicando qué player se usó
y el estado final de cada step — justo como pide el PRD §13.
