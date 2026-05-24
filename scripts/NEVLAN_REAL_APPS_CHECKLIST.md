# Nevlan — checklist en aplicaciones reales (comparable)

Usa `scripts/results_template.csv` para registrar resultados medibles. Campos obligatorios en la hoja:

| Campo | Descripción |
| --- | --- |
| app | Aplicación (Chrome, Notepad, Explorer, Excel, …) |
| caso_de_prueba | Nombre corto del flujo |
| grabó_correctamente | sí / no / parcial |
| agrupó_correctamente | grupos humanos vs lo esperado |
| ejecutó_correctamente | replay sin fallos |
| velocidad_vs_humano | más lento / similar / más rápido (perceptivo) |
| pasos_débiles_detectados | número o lista |
| pasos_reparados | cuántos pasaste a amarillo/verde |
| fallos | qué paso y síntoma |
| observaciones | libre |
| versión_nevlan | commit o versión visible en UI |
| fecha | ISO |
| tester | nombre |
| precisión_por_tipo_acción | resumen (clic / teclado / scroll) |
| velocidad_promedio_s | si mediste runtime |
| top_fallos | 1–3 patrones |
| top_acciones_débiles | 1–3 patrones |

## Resumen al cerrar la semana

- Precisión por tipo de acción (rellenar tabla agregada).
- Velocidad promedio vs humano.
- Top fallos (etiquetas cortas).
- Top acciones débiles (semáforo / coordenadas / web).

## Casos mínimos

### 1. Chrome / Edge

- Ctrl+L + búsqueda + Enter
- Clic en resultado de lista
- Scroll en página larga
- Campo web con texto corregido (backspace y espacios)

### 2. Notepad

- Escribir con correcciones (backspace)
- Guardar con Ctrl+S

### 3. Explorador de archivos

- Abrir carpeta
- Doble clic en archivo
- Seleccionar archivo y abrir

### 4. Excel (o compatible)

- Clic en celda
- Escribir valor
- Tab entre celdas
- Guardar

### 5. Automatización larga

- Misión de ~50 pasos: revisión y replay
- Misión de ~200 pasos: agrupación y scroll de revisión

## COORDINATE_ICON_VALIDATED_CLICK (evidencia operativa)

Si no puedes automatizar UI real, sigue este checklist guiado:

1. Abre una ventana con un botón o ícono conocido (p. ej. bloc de notas + menú).
2. Graba un clic sobre el ícono; deja que el compilador marque `coordinate_icon_validated_click` si aplica.
3. Mueve la ventana a otra posición en el escritorio.
4. Ejecuta el paso; en **modo experto** de la tarjeta de paso revisa `resolver`, `fallback` y tiempos en `runtime_stats`.
5. Confirma en logs estructurados del player: `strategy_used`, `region_used`, `match_score`, `offset_used`, `fallback`, `elapsed_ms` (cuando el premium path se activa).

Pruebas unitarias complementan: no buscar pantalla completa si hay región; offset; orden de fallback; metadata en el flujo de ejecución.

## Microbench

```text
python scripts/nevlan_microbench.py
```

Genera `var/performance_summary.txt` y `var/performance_summary.json`. Los avisos (WARN) son orientativos, no fallos de CI.

## Reparación (humano)

- **Regrabar**: overlay humanizado; sin microhistoria de teclas salvo modo experto.
- **Ref. imagen (1 clic)**: refuerza descriptor visual; recalcula score al enriquecer.
- **Validación**: elige estrategia en lenguaje natural en el diálogo de paso.
- **Aceptar riesgo**: no fuerza verde; muestra “riesgo aceptado” en el semáforo.

## Guía de puntuación (CSV)

Por celda: **0** = falla · **1** = parcial · **2** = correcto · **3** = excelente. La columna `score_global_0_a_3` permite una media rápida del caso.

Agregación automática:

```text
python scripts/summarize_real_app_results.py
```

→ `var/real_app_results_summary.json` y `var/real_app_results_summary.txt`.
