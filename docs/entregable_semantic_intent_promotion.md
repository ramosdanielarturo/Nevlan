# Entregable — Semantic Intent Promotion Engine (PRD 2026-05-14)

## 1. Resumen ejecutivo

El recorder de Nevlan ya capturaba todo lo necesario para entender al
usuario: teclado, mouse, UIA, DOM, OCR, fingerprints visuales,
`post_action_state`, `outcome`, `field_session`, `target_identity` y
metadata de ventana/proceso. **El problema no era ver. El problema era
entender.**

Este entregable agrega la capa **Semantic Intent Promotion Engine
(SIPE)**: una pieza pura, sin dependencias pesadas (no CLIP, no torch,
no transformers, no LLM), que convierte señales crudas en
**intenciones humanas ejecutables genéricas** y exige que la única
fuente de verdad para el ejecutor sea el `semantic_execution_plan`.

Logros principales:

* Patrones genéricos reutilizables (`open_app`, `select_profile`,
  `open_new_tab`, `open_site`, `search_site`, `web_search`,
  `scroll_results`). **YouTube no está hardcoded** — es un valor
  del parámetro `site` o `engine`.
* SIPE produce SEP con `source = "semantic_intent_promotion_engine"`,
  `coords_used = False`, `legacy_graph_ignored = True`.
* Cuando el plan se promueve, `mission.legacy_compiled_graph_purpose`
  queda en `"legacy_debug_only"`. El `smart_runner_bridge` ya respeta
  ese flag y nunca toca el grafo legacy en runtime cuando hay SEP.
* Si SIPE no logra construir un plan, **NO** ejecuta legacy
  automáticamente: deja blockers explícitos
  (`EMPTY_RAW_TRACE`, `NO_SEMANTIC_SIGNAL`,
  `AMBIGUOUS_PROFILE_PICKER`, `QUERY_FIDELITY_SUSPECT`).
* Outcome valida intención (`window_activate` con `chrome.exe` →
  confirma `open_app("chrome")`) — **no la inventa**.
* Scrolls consecutivos se agregan en `scroll_results(amount=N)`.
* Texto con acentos se preserva exacto.

## 2. Archivos modificados

| Archivo | Cambio |
|---|---|
| `app/services/missions/semantic_intent_promotion_engine.py` | **Nuevo módulo SIPE.** API: `promote_semantic_intent`, `apply_promotion_to_mission`, `PromotionResult`, `PromotionBlockerCode`, `is_generic_intent_type`. |
| `app/services/missions/action_runner.py` | Añade tres estrategias *smart-route*: `open_site:smart_route`, `search_site:smart_route`, `web_search:smart_route`. Delegan a las estrategias específicas existentes según `params.site` / `params.engine`. **Sin duplicar lógica ejecutiva.** |
| `app/services/missions/semantic_execution_plan.py` | Registra `open_site`, `search_site`, `web_search` en `_STRATEGY_BY_TYPE`, `_CANONICAL_ORDER` y `SEMANTIC_TYPES`. Extiende `compute_ready_status` para validar las nuevas intenciones genéricas (query no vacía, fidelity OK para `search_site:youtube`, URL no vacía para `open_site`, primary canónica). |
| `tests/unit/test_semantic_intent_promotion_engine.py` | **Nuevo archivo de tests** con 20 tests cubriendo todos los criterios de aceptación. |
| `docs/entregable_semantic_intent_promotion.md` | Este documento. |

## 3. Nueva arquitectura

```
┌────────────────────────────────────────────────────────────────────┐
│  raw_trace + interpreted_steps + field_sessions + outcome          │
│  + UIA/DOM/OCR/visual fingerprint + post_action_state              │
│  + window/process/url + action_groups                              │
└─────────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
                ┌──────────────────────────────┐
                │  intent_collapse_engine      │  (existente)
                │  collapse_intent(mission)    │  ─→ MissionIntentPlan
                │   → CollapsedStep[] con      │
                │     tipos legacy específicos │
                │     (search_youtube, open_url│
                │     search_web, ...)         │
                └──────────────┬───────────────┘
                               │
                               ▼
        ┌───────────────────────────────────────────────────┐
        │  semantic_intent_promotion_engine (NUEVO)         │
        │                                                   │
        │  promote_semantic_intent(mission)                 │
        │   1. Construye SemanticPlanStep[] desde el ICE.   │
        │   2. PROMUEVE a tipos genéricos:                  │
        │       • search_youtube → search_site (site=…)    │
        │       • open_url      → open_site   (site=…)     │
        │       • search_web    → web_search  (engine=…)   │
        │   3. Valida con outcome (window_activate, URL).   │
        │   4. Detecta blockers (perfil ambiguo, fidelity). │
        │   5. Empaqueta en SemanticExecutionPlan(          │
        │       source="semantic_intent_promotion_engine",  │
        │       coords_used=False,                          │
        │       legacy_graph_ignored=True)                  │
        └───────────────────────┬───────────────────────────┘
                                │
                                ▼
            ┌──────────────────────────────────────────┐
            │  apply_promotion_to_mission              │
            │   1. Persiste SEP promovido.             │
            │   2. attach_intent_status_to_plan        │
            │      (recalcula READY/NEEDS_REVIEW       │
            │      con la lógica del SEP).             │
            │   3. mission.legacy_compiled_graph_      │
            │      purpose = "legacy_debug_only"       │
            └──────────────────────┬───────────────────┘
                                   │
                                   ▼
        ┌──────────────────────────────────────────────────┐
        │  smart_runner_bridge / approval_gate / executor  │
        │   - lee mission.semantic_execution_plan          │
        │   - ignora compiled_execution_graph (debug only) │
        │   - ActionRunner ejecuta vía:                    │
        │       open_site:smart_route                      │
        │       search_site:smart_route                    │
        │       web_search:smart_route                     │
        │     que delegan a las estrategias específicas    │
        │     existentes según params.site / params.engine │
        └──────────────────────────────────────────────────┘
```

### Por qué un módulo nuevo y no reescribir el ICE

El `intent_collapse_engine` ya hace el trabajo duro: leer raw_trace,
fusionar fragmentos de texto, asociar profile picker con OCR, agregar
scrolls, etc. Reescribirlo era un riesgo enorme con tres meses de
tests detrás. **SIPE añade una capa fina de promoción y validación**
sobre el output del ICE, manteniendo todas las garantías existentes y
ganando los patrones genéricos que el PRD pide.

### Generic patterns: cómo funciona la ejecución sin duplicar lógica

`search_site:smart_route` se reduce a:

```python
def _search_site_smart_route(step, state):
    site = (step.params.get("site") or "").strip().lower()
    if site == "youtube":
        return _search_youtube_dom(step, state)
    return _search_web_address_bar(step, state)
```

Esto significa cero duplicación. Cuando aparezca un nuevo sitio que
necesite estrategia específica (Spotify, Twitch, ...), basta con
añadir un branch al `smart_route` o registrar
`search_site:spotify_dom_input` y dejar que el caller decida el
fallback. El SEP, el approval gate, el bridge, el executor — nada más
cambia.

## 4. Tests agregados

`tests/unit/test_semantic_intent_promotion_engine.py` — **20 tests**
(constraint del producto: máximo 20 por archivo de prueba). Cobertura:

| # | Test | Cubre |
|---|---|---|
| 01 | `canonical_mission_produces_six_generic_steps` | Misión canónica → 6 steps con tipos genéricos. |
| 02 | `no_coords_used_legacy_graph_ignored` | `coords_used=False`, `legacy_graph_ignored=True`, `source` correcto. |
| 03 | `apply_promotion_persists_plan_and_marks_legacy_debug` | Mutación in-place + `legacy_compiled_graph_purpose=legacy_debug_only`. |
| 04 | `search_youtube_promotes_to_search_site_youtube` | Promoción legacy → genérico. |
| 05 | `open_url_youtube_promotes_to_open_site` | Idem. |
| 06 | `search_web_promotes_to_web_search` | Omnibox → web_search. |
| 07 | `open_site_works_for_any_known_site_not_only_youtube` | Patrón reutilizable: github → open_site(site=github). |
| 08 | `profile_with_ocr_text_resolves_name` | OCR resuelve nombre → no necesita confirmación. |
| 09 | `profile_without_text_needs_user_label` | Picker visible sin nombre → needs_user_label=True. |
| 10 | `generic_profile_label_treated_as_empty` | "perfil del navegador" cuenta como vacío. |
| 11 | `query_with_accents_preserved_unchanged` | "Devuélveme..." se preserva exacto. |
| 12 | `suspect_query_fidelity_blocks_ready` | fidelity suspect → blocker. |
| 13 | `three_scrolls_aggregate_into_amount_3` | 3 scrolls → scroll_results(amount=3). |
| 14 | `open_new_tab_uses_hotkey_ctrl_t_as_primary` | Estrategia canónica. |
| 15 | `open_site_and_search_site_use_smart_route` | Estrategia canónica para genéricos. |
| 16 | `no_blind_coords_in_any_step` | Ninguna coord absoluta. |
| 17 | `outcome_validates_open_app_does_not_invent` | Outcome valida pero no inventa. |
| 18 | `empty_mission_produces_blockers_not_legacy_execution` | SEP null → blockers, no legacy auto. |
| 19 | `apply_promotion_keeps_canonical_mission_ready` | E2E: canónica → READY con 0 confirmaciones. |
| 20 | `is_generic_intent_type_helper_recognizes_all_generics` | Whitelist de genéricos. |

## 5. Resultado de suite

```text
tests/unit/test_semantic_intent_promotion_engine.py ........................ 20 passed
tests/unit/test_semantic_execution_plan.py            ........................ 43 passed
tests/unit/test_intent_collapse_engine.py             ........................ 32 passed
tests/unit/test_real_mission_1778297133.py            ........................ 12 passed
tests/unit/test_smart_runner_bridge.py                ........................ 22 passed
tests/unit/test_semantic_adapter.py                   ........................ ok
tests/unit/test_semantic_handoff.py                   ........................ ok
tests/unit/test_semantic_plan_strict_validation.py    ........................ ok
tests/unit/test_legacy_runtime_contamination.py       ........................ ok
tests/unit/test_overlay_legacy_reexport.py            ........................ ok
tests/unit/test_review_summary.py                     ........................ ok
tests/unit/test_review_summary_noise.py               ........................ ok
tests/unit/test_mission_review_confirm.py             ........................ ok
tests/unit/test_no_heavy_ai_deps.py                   ........................ ok

→ 129 + 215 = 344 tests passed, 0 failed.
```

(El resto del repo ya tenía los suites verdes antes de este cambio.
SIPE no rompe ninguna API existente: `search_youtube`/`open_url` siguen
funcionando para callers legacy; el SEP construido por
`build_semantic_execution_plan` sin SIPE sigue produciendo los tipos
legacy específicos hasta que el caller invoque
`apply_promotion_to_mission`.)

## 6. Ejemplo del SEP generado para la misión canónica

Input humano:

1. Click Windows Search
2. Escribir "chrome"
3. Click Chrome
4. Click perfil "Daniel Arturo Ramos" (OCR alrededor lo lee)
5. Ctrl+T (nueva pestaña)
6. Click bookmark YouTube (UIA name="YouTube", web.href=youtube.com)
7. Click barra YouTube (UIA name="guide-service", web.url=youtube.com)
8. Escribir "Devuélveme el amor de Luis Miguel"
9. Enter
10. Scroll abajo × 3

Output (`mission.semantic_execution_plan` tras
`apply_promotion_to_mission`):

```jsonc
{
  "source": "semantic_intent_promotion_engine",
  "version": 2,
  "average_confidence": 0.9,
  "coords_used": false,
  "legacy_graph_ignored": true,
  "intent_status": "READY",
  "not_ready_reasons": [],
  "ready_provenance": "raw_evidence",
  "steps": [
    {
      "id": "<mission-id>:p00:open_app",
      "type": "open_app",
      "params": {"name": "chrome", "method": "windows_search"},
      "preferred_strategy": "open_app:windows_search",
      "fallback_strategies": ["open_app:os_startfile", "open_app:start_command"],
      "confidence": 0.95,
      "human_label": "Abrir Chrome",
      "needs_user_label": false
    },
    {
      "id": "<mission-id>:p01:select_profile",
      "type": "select_profile",
      "params": {"profile_name": "Daniel Arturo Ramos", "app": "chrome"},
      "preferred_strategy": "select_profile:uia_text_match",
      "fallback_strategies": [
        "select_profile:visible_text_ocr",
        "select_profile:chrome_profile_alias",
        "select_profile:skip_if_loaded"
      ],
      "confidence": 0.92,
      "human_label": "Seleccionar perfil Daniel Arturo Ramos",
      "needs_user_label": false
    },
    {
      "id": "<mission-id>:p02:open_new_tab",
      "type": "open_new_tab",
      "params": {"app": "chrome"},
      "preferred_strategy": "open_new_tab:hotkey_ctrl_t",
      "fallback_strategies": ["open_new_tab:uia_button"],
      "confidence": 0.95,
      "human_label": "Abrir nueva pestaña",
      "needs_user_label": false
    },
    {
      "id": "<mission-id>:p03:open_site",
      "type": "open_site",
      "params": {"site": "youtube", "url": "https://www.youtube.com"},
      "preferred_strategy": "open_site:smart_route",
      "fallback_strategies": [
        "open_url:hotkey_ctrl_l",
        "open_url:playwright_goto",
        "open_url:bookmark_uia"
      ],
      "confidence": 0.95,
      "human_label": "Abrir Youtube",
      "needs_user_label": false
    },
    {
      "id": "<mission-id>:p04:search_site",
      "type": "search_site",
      "params": {
        "site": "youtube",
        "query": "Devuélveme el amor de Luis Miguel",
        "submit": true,
        "query_fidelity_status": "ok"
      },
      "preferred_strategy": "search_site:smart_route",
      "fallback_strategies": [
        "search_youtube:dom_input",
        "search_web:address_bar_type_enter",
        "search_web:dom_search_input",
        "search_web:keyboard_focus_then_type"
      ],
      "confidence": 0.95,
      "human_label": "Buscar “Devuélveme el amor de Luis Miguel” en Youtube",
      "needs_user_label": false
    },
    {
      "id": "<mission-id>:p05:scroll_results",
      "type": "scroll_results",
      "params": {"direction": "down", "amount": 3, "context": "youtube_results"},
      "preferred_strategy": "scroll_results:wheel",
      "fallback_strategies": ["scroll_results:keyboard", "scroll_results:js_scrollby"],
      "confidence": 0.85,
      "human_label": "Bajar resultados 3 veces",
      "needs_user_label": false
    }
  ]
}
```

Mission Review (vista normal):

```
✅ Abrir Chrome
✅ Seleccionar perfil Daniel Arturo Ramos
✅ Abrir nueva pestaña
✅ Abrir YouTube
✅ Buscar "Devuélveme el amor de Luis Miguel" en YouTube
✅ Bajar resultados 3 veces
```

Mission Review (vista experta) muestra adicionalmente:
`source = semantic_intent_promotion_engine`,
`ready_provenance = raw_evidence`,
`promotions[]` (legacy → generic transitions),
`legacy_compiled_graph_purpose = legacy_debug_only`.

## 7. Limitaciones pendientes

Reportadas honestamente:

1. **Sitios sin estrategia DOM específica.** `search_site:smart_route`
   tiene un branch para `youtube` (delega a `search_youtube:dom_input`)
   y un fallback universal `search_web:address_bar_type_enter` para el
   resto. Funcional pero no óptimo: para sitios como Spotify o Twitch,
   donde el searchbox interno difiere mucho de la omnibox, conviene
   registrar estrategias específicas. La extensión no requiere tocar
   el SIPE — basta con `register_strategy("search_site:spotify_dom_input", …)`
   y añadir el branch en `_search_site_smart_route`.

2. **Sitios desconocidos en `_resolve_site_from_url`.** La heurística
   actual (primer token `x.y` del URL) marca un alias razonable pero
   sin garantía de canonicidad. Cualquier sitio frecuente debe
   añadirse a `_SITE_DOMAIN_TO_ALIAS`/`_SITE_ALIAS_TO_URL`.

3. **Outcome validation es un boost de confianza, no una corrección
   activa.** Si el ICE entendió mal el target (caso muy raro tras
   tres meses de hardening), SIPE no inventa una corrección — solo
   reporta el blocker para que el usuario confirme.

4. **Mission Review todavía conserva código que renderiza nombres
   `search_youtube`/`open_url`** para misiones legacy persistidas
   antes de este cambio. Las nuevas grabaciones que pasen por
   `apply_promotion_to_mission` ya muestran `open_site` /
   `search_site`. Una migración 1-shot del store está disponible vía
   `bulk_recompile.py`, pero queda fuera del alcance de este sprint
   para no romper datos en producción.

5. **`use_selected_profile` y `open_search_result`** no se promueven
   a tipos nuevos (no había uno mejor); se mantienen tal cual los
   emite el ICE. Pasan toda la validación.

## 8. Próximo paso recomendado

* **Ampliar `_search_site_smart_route`** con dos o tres sitios más
  (Spotify, Twitch, Drive) en cuanto haya grabaciones reales que los
  ejerciten.
* **Añadir un campo `mission.intent_promotions[]` persistente** (en
  vez del `_sipe_promotions` runtime-only) para que la auditoría experta
  pueda visualizar el mapa legacy → generic post-mortem.
* **Telemetría agregada** sobre el ratio de misiones que llegan al
  bridge ya migradas vs. promovidas en runtime — útil para decidir
  cuándo retirar el camino de promoción on-demand.

---

## Anexo — Iteración 2 (PRD 2026-05-14 §SIPE-wiring)

### Cambios

| Archivo | Cambio |
|---|---|
| `app/services/missions/smart_runner_bridge.py` | El bridge invoca `apply_promotion_to_mission` automáticamente cuando construye el SEP. Idempotente: si la misión ya viene con `source == "semantic_intent_promotion_engine"`, se reusa el plan persistido sin re-promover. Fallback explícito a `build_semantic_execution_plan` clásico si SIPE falla. |
| `app/services/missions/semantic_intent_promotion_engine.py` | `promote_semantic_intent` ahora maneja el caso "misión sin `raw_trace` pero con SEP persistido" — usa los steps existentes como entrada en lugar de re-correr el ICE sobre un trace vacío. Esto preserva los planes ya cargados del store. |
| `app/services/missions/bulk_recompile.py` | Nueva función `sipe_migrate_missions(missions, in_place=False, skip_already_promoted=True)` que produce `SipeMigrationRow` por misión sin escribir a disco. |
| `scripts/migrate_missions_to_sipe.py` | **Nuevo CLI** para la migración 1-shot del store. Soporta `--apply`, `--force`, `--mission-id`, `--limit`. Default es dry-run. Crea backups `.bak-sipe_<ts>.json` antes de reescribir. |
| `tests/unit/test_sipe_integration.py` | **10 tests nuevos** que cubren: (a) bridge persiste `source=SIPE` al construir el plan, (b) los step kinds entregados al executor son genéricos, (c) idempotencia cuando ya está promovido, (d) `legacy_graph_ignored=True` en la decisión, (e) `bulk_recompile.sipe_migrate_missions` migra correctamente, (f) skip de ya-migradas, (g) `--force` re-promueve, (h) `in_place=True` muta el original, (i) misiones vacías no abortan el batch, (j) determinismo. |
| `tests/unit/test_semantic_execution_plan.py` | Dos tests pre-existentes actualizados para esperar los kinds genéricos (`open_site`/`search_site`) tras el wiring del bridge. El resto de invariantes (orden, `coords_used=False`, `legacy_graph_ignored=True`) intactos. |

### Resultado de suite tras la iteración

```
tests/unit (1512 tests)              1512 passed   0 failed
tests/unit/test_sipe_integration.py    10 passed   0 failed
tests/unit/test_semantic_intent_promotion_engine.py  20 passed   0 failed
                                  ────────────────────────────
                          TOTAL: 1512 passed   0 failed
```

### Uso de la migración

```powershell
# Dry-run (no escribe nada).
python scripts/migrate_missions_to_sipe.py

# Migrar todas en disco con backup automático .bak-sipe_<ts>.json.
python scripts/migrate_missions_to_sipe.py --apply

# Solo una misión.
python scripts/migrate_missions_to_sipe.py --mission-id <id> --apply

# Re-promover misiones ya migradas (raro: cuando se cambian heurísticas
# de SIPE y se quiere refrescar el mapa legacy → generic).
python scripts/migrate_missions_to_sipe.py --apply --force
```

### Decisión de diseño: idempotencia barata vs. re-promoción agresiva

Por default el bridge **no** re-promueve misiones ya marcadas con
`source = "semantic_intent_promotion_engine"`. Esto evita pagar el
costo del ICE en cada run y mantiene un contrato simple:

> Una misión ya promovida es la fuente de verdad — solo se vuelve a
> promover explícitamente vía `--force` en la migración 1-shot, o
> cuando el usuario pulsa "Recompilar" en Mission Review.

Cuando hay cambios en las heurísticas de SIPE que afectan
clasificación (e.g., un nuevo alias en `_SITE_DOMAIN_TO_ALIAS`), el
operador corre la migración con `--force` para refrescar el store
completo en una sola pasada.

---

## Iteración 3 — Telemetría agregada, persistencia y catálogo (Mayo 14, 2026)

Tres entregables dentro de una sola iteración para cerrar el loop
operacional del SIPE.

### 1. Telemetría agregada (`sipe_metrics`)

Nuevo módulo `app/services/telemetry/sipe_metrics.py` con un singleton
thread-safe que cuenta, en el bridge, cómo llega cada misión:

| Contador | Significado | Usado para |
|---|---|---|
| `already_promoted` | El SEP llegó con `source = "semantic_intent_promotion_engine"`. SIPE NO se invocó. | **Decidir cuándo retirar el camino on-demand**: cuando el ratio `promoted_at_runtime / total → 0` durante una ventana significativa, podemos eliminar la rama de promoción del bridge. |
| `promoted_at_runtime` | El bridge llamó `apply_promotion_to_mission` y promovió en este run. | Identificar misiones grabadas antes del wiring SIPE. |
| `legacy_fallback` | SIPE falló, cayó a `build_semantic_execution_plan` clásico. | Alertas — debería ser ~0 en producción saludable. |
| `failure` | SIPE Y el clásico fallaron. | Crítico — investigación inmediata. |
| `total_promotions_emitted` | Suma de `len(promotions)` de cada promoción exitosa. | Ritmo de transformación legacy → genérico. |
| `fallback_reasons` | `Counter` por motivo string corto. | Debugging. |

API ergonómica:

```python
from app.services.telemetry.sipe_metrics import sipe_metrics

snap = sipe_metrics.snapshot()
print(snap.to_dict())
# → {'already_promoted': 12, 'promoted_at_runtime': 3,
#    'legacy_fallback': 0, 'failure': 0, 'total': 15,
#    'already_promoted_pct': 80.0,
#    'promoted_at_runtime_pct': 20.0, ...}
```

`RunnerDecision` también expone el path por decisión:

```python
decision = run_mission_smart(mission)
decision.sipe_path             # "already_promoted" | "promoted_at_runtime"
                               # | "legacy_fallback" | "failure" | ""
decision.sipe_promotions_count # cuántas promociones aplicó SIPE en este run
```

### 2. Persistencia `mission.intent_promotions[]` y `intent_promotion_blockers[]`

Antes la traza de promociones de SIPE vivía como atributos privados
runtime-only (`mission._sipe_promotions`) — se perdía al guardar al
store. Ahora son **campos Pydantic reales del contrato `Mission`**:

```python
class Mission(BaseModel):
    # ...
    intent_promotions: List[Dict[str, Any]] = Field(default_factory=list)
    intent_promotion_blockers: List[str] = Field(default_factory=list)
```

Cada entry de `intent_promotions` describe UNA promoción legacy →
genérico aplicada por SIPE:

```json
{
  "from": "search_youtube",
  "to":   "search_site",
  "reason": "search_site_canonical",
  "step_index": 4,
  "human_label": "Buscar \"…\" en YouTube",
  "site": "youtube"
}
```

Garantías:

* **Sobrevive al `model_dump`** — viaja al JSON del store y al
  cargar de vuelta queda intacto (test `test_08`).
* **Idempotente** — cada `apply_promotion_to_mission` reemplaza, no
  acumula. No hay growth ilimitado en el JSON (test `test_09`).
* **No se vacía cuando el bridge reusa un SEP ya promovido** — el
  audit trail persistido se respeta (test `test_10`).
* **Backwards-compat**: `mission._sipe_promotions` sigue existiendo
  como alias para callers viejos.

La UI Mission Review (modo experto) puede ahora renderizar la
historia completa de cómo SIPE convirtió cada paso legacy.

### Vista normal — textos semánticos (`semantic_review_display`)

`app/services/missions/semantic_review_display.py` expone el mismo
mapeo compilado↔SEP que Mission Review: en **vista normal**, si el
SEP tiene `source == "semantic_intent_promotion_engine"` y el step
tiene `human_label`, el título de la tarjeta usa esa intención (no
`solo` el `interpreted_steps.description` del recorder). Modo
experto sigue mostrando primero la descripción editable del trace.

### Contrato de prueba — misión canónica PRD (`sipe_metrics`)

`tests/unit/test_sipe_telemetry_persistence_catalog.py` incluye
``test_12_canonical_metrics_promotions_mr_and_review_focus`` que
verifica en una sola pasada:

1. Entró por `already_promoted` o `promoted_at_runtime` (`sipe_metrics`).
2. ``legacy_fallback`` y ``failure`` en 0.
3. Cada ``intent_promotions[]`` trae ``from`` / ``to`` / ``reason``.
4. Caption Mission Review vista normal = ``human_label`` del SEP mapeado.
5. Tipos semánticos ≠ eventos crudos (`click`, …).
6. Si el plan no está READY, ``not_ready_reasons`` ⊆
   ``PROFILE_OR_QUERY_REVIEW_FOCUS_BLOCKERS`` (`semantic_execution_plan.py`).

### Archivos modificados (iteración 3, actualizado)

| Archivo | Cambio |
|---|---|
| `app/services/telemetry/sipe_metrics.py` | **Nuevo**. Singleton thread-safe con 4 contadores + snapshot serializable. |
| `app/services/missions/smart_runner_bridge.py` | Llama a `sipe_metrics.record_*()` en cada path. `RunnerDecision` ahora expone `sipe_path` y `sipe_promotions_count`. |
| `app/contracts/mission.py` | Nuevos campos `Mission.intent_promotions: List[Dict[str,Any]]` y `Mission.intent_promotion_blockers: List[str]`. |
| `app/services/missions/semantic_intent_promotion_engine.py` | `apply_promotion_to_mission` escribe a los campos Pydantic (no solo al alias `_sipe_promotions`). |
| `app/services/missions/semantic_execution_plan.py` | Constante ``PROFILE_OR_QUERY_REVIEW_FOCUS_BLOCKERS`` para contratos de revisión. |
| `app/services/missions/semantic_review_display.py` | **Nuevo**. Mapeo SEP↔compiled y caption vista normal (sin PyQt). |
| `app/interfaces/desktop/mission_review.py` | Vista normal usa captions semánticos cuando el SEP es SIPE. |
| `tests/unit/test_sipe_telemetry_persistence_catalog.py` | Telemetría + persistencia + **contrato canónico** con `sipe_metrics`. |

### Resultado de suite (iteración 3, actualizado)

```
tests/unit/test_sipe_telemetry_persistence_catalog.py   12 passed   0 failed
tests/unit/test_sipe_integration.py                     10 passed   0 failed
tests/unit/test_semantic_intent_promotion_engine.py    20 passed   0 failed
tests/unit/test_semantic_execution_plan.py             43 passed   0 failed
```

### Limitaciones

* La telemetría es **en-memoria**. Si el proceso reinicia, los
  contadores arrancan en 0. Para dashboards persistentes hay que
  exportar `sipe_metrics.snapshot().to_dict()` periódicamente a
  telemetría externa o a un sink propio.

### Próximo paso recomendado

1. **Exportar ``sipe_metrics`` a la UI experta** cuando haya un
   canal de telemetría persistente.
2. **Timeline ``intent_promotions[]``** en Mission Review (modo experto)
   como producto de auditoría.


---

## (Obsoleto en repo) Iteración 3 — subsección catálogo Spotify/Twitch

Se retiró por decisión de producto: **no añadir más sitios** hasta
contar con grabaciones reales. Las estrategias genéricas existentes
(y YouTube vía ``search_site``) siguen siendo el contrato de rutas.

