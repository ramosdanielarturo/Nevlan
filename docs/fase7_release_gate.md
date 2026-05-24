# FASE 7 — Gate de release runtime

**Estado:** RELEASE CERTIFICADO · **ID:** `fase7-runtime-v1`  
**Fecha congelación:** 2026-05-24  
**Última revisión doc:** 2026-05-24

Baseline live congelado: suite `default`, 5 escenarios × 5 runs = **25/25 ACCEPTED**.

| Métrica batch | Valor certificado |
|---------------|-------------------|
| `release_status` | `ACCEPTED` |
| `coords_used_total` | 0 |
| `smart_route_count` | 0 |
| `runtime_timeout_count` | 0 |
| `needs_human_count` | 0 |
| `strategy_used=""` / UNSUPPORTED | 0 |

Fixture congelado: `tests/fixtures/fase7_benchmark/fase7_certified_live_baseline.json`

---

## Escenarios suite default

| ID | Categoría | Notas post-certificación |
|----|-----------|--------------------------|
| `chrome_youtube_launcher` | launcher·browser·search | weakest: `search_content:uol_surface` |
| `notepad_write_save` | launcher·editor | P1: `submit_form` cae a `submit_input:uol_submit` (fallback estable) |
| `web_form_simple` | browser·form | weakest: `submit_form:uol_submit` |
| `confirmation_dialog` | dialog·context | weakest: `open_app` / `switch_context` |
| `relayout_window` | browser·layout | weakest: `open_url` / `navigate_to_location` |

---

## Comandos

### CI / regresión infra (dry-run, sin escritorio)

```powershell
make fase7-gate
```

Equivalente manual:

```powershell
python scripts/run_fase7_benchmark.py --suite default --mode dry-run --runs 5 --report json
pytest tests/unit/test_fase7_release_benchmark.py -q
python scripts/validate_fase7_certified_release.py
```

Exit codes (`run_fase7_benchmark.py`):

| Código | Significado |
|--------|-------------|
| 0 | Gate OK — `release_status=ACCEPTED`, sin violaciones |
| 1 | Benchmark incompleto o escenario `REJECTED` |
| 2 | Violación gate (coords, smart_route, needs_human, etc.) |

### Certificación live (escritorio real, manual)

Requisitos: Windows, Chrome perfil disponible, Notepad, sesión despejada.

```powershell
make fase7-benchmark
```

Equivalente:

```powershell
python scripts/run_fase7_benchmark.py --suite default --mode live --runs 5 --timeout 600 --report json
```

Reporte escrito en:

- `var/runtime/fase7_benchmark/fase7_run_latest.json` (último run)
- `var/runtime/fase7_benchmark/fase7_run_<timestamp>.json` (histórico)

Escenario individual:

```powershell
python scripts/run_fase7_benchmark.py --mission notepad_write_save --mode live --runs 5 --timeout 600
python scripts/run_fase7_benchmark.py --list-scenarios
```

---

## Cómo leer el reporte JSON

Estructura batch (suite completa):

```json
{
  "suite": "default",
  "mode": "live",
  "required_runs": 5,
  "scenarios": [ { "scenario_id": "...", "runs": [...], "release_status": "ACCEPTED" } ],
  "aggregate": { "accepted_runs": 25, "coords_used_total": 0, ... },
  "weakest_step": { "step_id": "...", "strategy_used": "...", "duration_ms": N },
  "release_status": "ACCEPTED"
}
```

Campos clave por run:

| Campo | ACCEPTED requiere |
|-------|-------------------|
| `status` | `"success"` |
| `coords_used` | `false` |
| `smart_route_count` | `0` |
| `runtime_timeout` | `false` |
| `needs_human` / `human_retry` | `false` |
| `weakest_step` | presente (paso más lento del run) |
| `step_strategies[].strategy_used` | nunca vacío |

Gate evaluado en `app/services/runtime/fase7_release_gate.py`:

- `fase7_gate_violations` — un escenario
- `fase7_batch_gate_violations` — suite completa (≥5 escenarios)

---

## Checklist reproducibilidad

### Antes de correr live

- [ ] Perfil convergido prod (`BETA_PRIVATE_RUNTIME_PROFILE`, sin drift)
- [ ] Sin misiones colgadas en Notepad/Chrome
- [ ] `tests/fixtures/fase7_missions/assets/simple_form.html` accesible (web_form_simple)
- [ ] Timeout ≥ 600 s por run en suite completa

### Después de correr live

- [ ] `release_status == "ACCEPTED"` en raíz del JSON
- [ ] `aggregate.accepted_runs == 25` (suite default)
- [ ] `aggregate.coords_used_total == 0`
- [ ] `aggregate.smart_route_count == 0`
- [ ] `aggregate.needs_human_count == 0`
- [ ] Cada escenario: 5/5 runs con `acceptance: "ACCEPTED"`
- [ ] Sin `strategy_used: ""` en `step_strategies`

### CI local (obligatorio en PR)

- [ ] `make fase7-gate` exit 0
- [ ] `python scripts/validate_fase7_certified_release.py` exit 0

---

## P1 post-release (no blocker certificado)

Endurecer `_fill_save_dialog_path` para que `notepad_write_save` use
`submit_form:uol_submit` 5/5 sin fallback `submit_input:uol_submit`.

> El baseline certificado (`fase7_certified_live_baseline.json`) **no se altera**;
> refleja el sello histórico 25/25 al momento de congelación.

---

## Ownership

| Componente | Ubicación |
|------------|-----------|
| Runner | `scripts/run_fase7_benchmark.py` |
| Gate logic | `app/services/runtime/fase7_release_gate.py` |
| Runner core | `app/services/runtime/fase7_benchmark_runner.py` |
| Fixtures escenarios | `tests/fixtures/fase7_missions/` |
| Baseline congelado | `tests/fixtures/fase7_benchmark/fase7_certified_live_baseline.json` |
| Tests gate | `tests/unit/test_fase7_release_benchmark.py` |
