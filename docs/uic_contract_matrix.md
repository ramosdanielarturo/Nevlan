# UIC ↔ SEP ↔ SIPE ↔ MissionStep ↔ Runner — matriz de contrato

Documento **humano** alineado con `app/services/missions/uic_contract_catalog.py`
(la fuente maquinable para tests). Reglas:

- **UIC `capability_id`**: nombre conceptual de producto / SEP persistido estable.
- **SEP `type`**: contrato semántico en el blob `semantic_execution_plan`.
- **SIPE promoted type**: tras `SemanticIntentPromotionEngine` (cuando aplica).
- **`MissionStep.kind`**: forma del Smart Executor; debe converger con SEP/UIC,
  no con alias legacy salvo misiones viejas.
- **Runner strategy**: primaria registrada en `uic_capability_registry` y
  despachada por `action_runner.ActionRunner`.
- **Outcome validator**: nombre de función en registry (`UICCapability.outcome_validator`).
- **Legacy aliases**: compatibilidad; nunca fuente de verdad nueva.

## Tabla principal

| UIC capability_id | SEP type | SIPE promoted type | MissionStep.kind | Runner strategy (primaria) | Outcome validator | Legacy aliases |
|-------------------|----------|-------------------|------------------|----------------------------|-------------------|----------------|
| open_app | open_app | open_app | open_app | open_app:windows_search | _passthrough_validator | — |
| open_url | open_url | open_site | open_url | open_url:hotkey_ctrl_l | _passthrough_validator | open_site |
| open_site | open_site | open_site | open_site | open_site:smart_route | _passthrough_validator | open_url |
| search_content | search_content | search_site | search_content | search_content:smart_route | _need_provider_or_site | search_youtube |
| search_site | search_site | search_site | search_site | search_site:smart_route | _need_query | search_youtube, search_content |
| search_youtube | search_youtube _(legacy SEP)_ | search_site | search_youtube | search_youtube:dom_input | _need_query | search_content |
| scroll_results | scroll_results | scroll_results | scroll_results | scroll_results:wheel | _passthrough_validator | — |
| select_profile | select_profile | select_profile | select_profile | select_profile:uia_text_match | _need_profile | — |

## Estrategias de encadenamiento (búsqueda YouTube)

Para destinos YouTube declarados en `params.provider` / `params.site`, el runner
resuelve internamente sin cambiar el `MissionStep.kind` canónico:

- `search_content:smart_route` → `search_site:smart_route` → `search_site:youtube_dom_input`
  → fallbacks `search_youtube:*` (deprecados como etiqueta, misma tubería DOM/UIA/OCR).

## Migración legacy

- SEP con `type=search_youtube` se normaliza a `search_content` en carga vía
  `uic_migration.migrate_mission_semantic_plan_uic` con auditoría `uic_migration_audit`.
- Pasos que aún conservan `MissionStep.kind=search_youtube` en fixtures antiguos
  siguen ejecutándose por contrato `_build_search_youtube`.
