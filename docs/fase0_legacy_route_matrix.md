# FASE 0 — Matriz de rutas legacy

Documento interno: qué rutas de ejecución mueren, cuáles quedan como
emergencia y fecha de corte estimada. Prerrequisito de FASE 1.

**Estado:** FASE 0 activa · **Fecha de corte global legacy:** 2026-08-31  
**Última revisión:** 2026-05-20

---

## Principio rector

Un solo Nevlan. Una sola fuente ejecutiva (`semantic_execution_plan`).
Perfil runtime fijo en producción. Telemetría obligatoria por run.

---

## Matriz de rutas

| Ruta | Módulo / entrypoint | Estado FASE 0 | Post-corte | Emergencia |
|------|---------------------|---------------|------------|------------|
| **SEP → SmartRunner → SmartExecutor** | `smart_runner_bridge.run_mission_smart` | **CANÓNICA** | Mantener | — |
| **SEP → ActionRunner (UOL handlers)** | `action_runner`, `uol_action_handlers` | **CANÓNICA** | Mantener | — |
| **compiled_execution_graph → MissionPlayer** | `player.MissionPlayer` | **DEBUG ONLY** | Eliminar ejecución | Sólo si misión sin SEP y flag `ALLOW_LEGACY_EMERGENCY=1` (ops) |
| **compiled_execution_graph → adapter fallback** | `smart_runner_bridge` (source=compiled_execution_graph) | **BLOQUEADA** si SEP válido | Eliminar | Nunca en prod |
| **AutoLearningExecutor wrapper** | `auto_learning_executor` | Activa (envuelve SmartExecutor) | Mantener | Desactivable vía `use_auto_learning=False` |
| **COB Execution Pilot** | `cob_execution_pilot` | OFF en perfil convergido | Eliminar ruta paralela | No |
| **TEL → SEP primary promotion** | `tel_sep_promoter` | OFF en prod (`suggest` only) | Mantener suggest | primary sólo dev |
| **Legacy coords / vision / GroupControl** | `player`, estrategias prohibidas SEP | **PROHIBIDA** en SEP | Eliminar | Emergencia manual con audit + `coords_used=true` |
| **smart_route hidden fallback** | varios handlers | Telemetría + hardening | Eliminar | ORCE rechaza en canonical |
| **MissionPlayer directo (UI)** | `automation_center` legacy button | Fallback UI legacy | Eliminar botón | Expert-only hasta corte |
| **Re-record overlay replay→capture** | `rerecord_overlay` | Mantener | Mantener | — |
| **ARSS synthesis post-ORCE** | `adaptive_runtime_strategy_synthesis` | **OFF fijo prod** | Opt-in dev/staging | No en prod |
| **Beta profile A (sin OSUE)** | flags sueltos | **ELIMINADO** — perfil único | — | — |
| **Beta profile B (ARSS on)** | `ARSS_ENABLED=true` | **ELIMINADO prod** | staging only | No |

---

## Fuente ejecutiva

| Artefacto | Rol FASE 0 | Ejecutable |
|-----------|------------|------------|
| `semantic_execution_plan` | Fuente única | Sí |
| `compiled_execution_graph` | Debug / Mission Review experto | **No** |
| `legacy_compiled_graph_purpose` | Debe ser `legacy_debug_only` cuando hay SEP | — |
| `intent_collapse` / ICE | Input a SEP | No directamente |
| `interpreted_steps` (UI) | Display only vía truth gate | No |

Enforcement: `mission_truth_gate.gate_decision`, cinturón en
`smart_runner_bridge` (no cae a CEG si SEP válido/contaminado).

---

## Perfil runtime producción (0.1)

Flags **fijos** en `ENV=prod` (env user ignorado):

| Flag | Valor prod |
|------|------------|
| `BETA_PRIVATE_RUNTIME_PROFILE` | `true` |
| `BETA_PRIVATE_RELIABILITY_PROFILE` | `true` |
| `PCOF_ENABLED` | `true` |
| `OSUE_ENABLED` | `true` |
| `OSUE_SHADOW_ENABLED` | `true` |
| `ARSS_ENABLED` | `false` |
| `UOOL_ENABLED` | `true` |
| `OCRP_ENABLED` | `true` |

Implementación: `nevlan_runtime_convergence.apply_production_convergence_profile`.

El validador de `Settings` aplica convergencia **después** de cargar `.env` y
variables de entorno: cualquier valor contradictorio (`ARSS_ENABLED=true`,
`PCOF_ENABLED=false`, `BETA_PRIVATE_RUNTIME_PROFILE=false`, etc.) queda
sobreescrito. En prod el bloque parcial `BETA_PRIVATE_RELIABILITY_PROFILE`
no se aplica — sólo el perfil convergido completo.

---

## ARSS OFF en prod — decisión intencional

`ARSS_ENABLED=false` en producción **no es un olvido**; es política FASE 0:

1. ARSS sintetiza variantes mecánicas post-ORCE que compiten con el perfil
   único convergido (riesgo Nevlan A vs Nevlan B).
2. Requiere telemetría ORCE estable en staging antes de habilitar en prod.
3. Opt-in explícito en staging (`--arss`, `ARSS_ENABLED=true` fuera de prod)
   hasta criterio execute-always ≥ 95 %.

Referencia código: `nevlan_runtime_convergence.ARSS_DISABLED_IN_PROD_RATIONALE`.

---

## Telemetría obligatoria (0.5)

| Sink | Contenido |
|------|-----------|
| `var/runtime/execution_run_audit.jsonl` | Por run: flags, estrategia/paso, weakest_step |
| `var/runtime/uol_execution_memory_audit.jsonl` | UOL step telemetry |
| `var/runtime/osue_state_audit.jsonl` | OSUE snapshots |
| `var/runtime/orce_runtime_audit.jsonl` | ORCE checks |
| `var/runtime/golden_runner/golden_run_latest.json` | Reporte golden CI |

---

## Gate FASE 1

- [ ] `make fase0-gate` exit 0 en CI/local sin intervención
- [ ] Reporte incluye `runtime_flags` + `profile_label` (detección Nevlan A/B)
- [ ] `profile_drift` vacío en prod
- [ ] `executive_source == semantic_execution_plan` en runs dry-run/live
- [ ] `execution_run_audit.jsonl` crece en cada run smart
- [ ] **Falla** si: drift · SEP≠fuente · CEG ejecutó con SEP · sin JSONL ·
  `coords_used` · `smart_route` primaria · modo `stub`-only

Comando CI::

    make fase0-gate

Violaciones evaluadas en `nevlan_runtime_convergence.fase0_gate_violations`.

---

## Calendario de corte

| Hito | Fecha | Acción |
|------|-------|--------|
| FASE 0 merge | 2026-05-20 | Perfil único + golden runner + audit JSONL |
| FASE 1 start | Tras gate verde | Hardening pasos débiles golden |
| Legacy player UI hidden | 2026-07-15 | Botón legacy sólo expert + audit |
| CEG execution removed | 2026-08-31 | `MissionPlayer` no ejecutable en prod |
| ARSS staging opt-in | 2026-09+ | Fuera de prod hasta criterios ORCE |

---

## Contacto / ownership

Runtime convergence: `app/services/runtime/nevlan_runtime_convergence.py`  
Golden runner: `scripts/run_golden_mission.py`  
Truth gate: `app/services/missions/mission_truth_gate.py`
