"""
Nevlan — contexto de reparación post-ejecución (PRD 2026-05-17)
==============================================================

Deriva, a partir de un :class:`ExecutionRun` y la misión, qué paso
semántico falló y cómo explicarlo sin jerga de depurador. La UI del
Centro de Automatizaciones y Mission Review consumen el mismo
:func:`infer_repair_brief_from_run` para enlazar rutas.

Inmutable respecto al runtime: sólo lectura del trace y modelos ya
cargados.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.services.missions.execution_lifecycle import (
    STEP_FAILED,
    STEP_NEEDS_HUMAN,
)
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    build_semantic_execution_plan,
)

# Copy corto por tipo semántico (evita mostrar snake_case).
_KIND_HEADLINE_ES: Dict[str, str] = {
    "open_browser": "No pude llegar al navegador o sitio esperado.",
    "open_site": "No coincidía la página donde debía actuar.",
    "select_profile": "No pude confirmar el perfil del navegador.",
    "search_site": "No encontré bien el resultado o el campo en el sitio.",
    "search_web": "La búsqueda en la web no encajó con lo esperado.",
    "search_content": "No encontré el contenido o la posición esperada.",
    "click_element": "No localicé el elemento para clicar.",
    "type_text": "No coincidía el texto o campo para escribir.",
    "wait_visible": "No apareció lo que esperaba ver.",
    "press_hotkey": "No pude ejecutar la tecla combinación pedida.",
    "scroll_into_view": "No llegué al área donde debía estar el objetivo.",
    "open_app": "No encontré bien la aplicación.",
    "open_url": "No quedó la URL cargada como esperaba.",
}


@dataclass(frozen=True)
class RepairBrief:
    """Resumen estable para overlays y Mission Review enlazados."""

    step_semantic_id: str
    step_type: str
    semantic_plan_index: int
    compiled_step_index: int
    trace_status: str
    headline: str
    subtext: str

    @property
    def resume_semantic_start_index(self) -> int:
        """Re-ejecutar desde este índice (inclusive) del plan semántico."""
        return max(0, int(self.semantic_plan_index))


def _human_headline(step_type: str, trace_status: str) -> str:
    base = _KIND_HEADLINE_ES.get(
        (step_type or "").strip(),
        "Este paso no encajó con lo que la automatización esperaba.",
    )
    if trace_status == STEP_NEEDS_HUMAN:
        return base + " Puedes enseñármelo una vez y me adapto."
    return base + " Podemos sustituir solo este paso, sin regrabar todo."


def _semantic_positions(
    mission: Any,
    step_id: str,
) -> tuple[int, str]:
    """Devuelve (índice en plan.steps, tipo) para ``step_id``."""
    try:
        plan: SemanticExecutionPlan = build_semantic_execution_plan(
            mission, use_existing=True,
        )
    except Exception:
        return -1, ""
    sid = (step_id or "").strip()
    for i, sp in enumerate(plan.steps or []):
        if sp.id == sid:
            return i, str(sp.type or "")
    return -1, ""


def _compiled_position(mission: Any, step_id: str) -> int:
    sid = (step_id or "").strip()
    graph = list(getattr(mission, "compiled_execution_graph", None) or [])
    for i, cs in enumerate(graph):
        try:
            if getattr(cs, "id", "") == sid:
                return i
        except Exception:
            continue
    return -1


def infer_repair_brief_from_run(
    run: Any,
    mission: Any,
    *,
    run_error_message: str = "",
) -> Optional[RepairBrief]:
    """Si el último problema del trace permite regrabación enfocada,
    devuelve un :class:`RepairBrief`; si no (gate, sin steps…), ``None``.
    """
    trace = getattr(run, "trace", None)
    if trace is None:
        return None
    entries = list(getattr(trace, "entries", []) or [])
    if not entries:
        return None
    cand = None
    for e in reversed(entries):
        st = str(getattr(e, "status", "") or "")
        if st in (STEP_FAILED, STEP_NEEDS_HUMAN):
            cand = e
            break
    if cand is None:
        return None
    step_id = str(getattr(cand, "step_id", "") or "").strip()
    if not step_id:
        return None
    step_kind = str(getattr(cand, "step_type", "") or "").strip()

    sem_i, sem_t = _semantic_positions(mission, step_id)
    kind = sem_t or step_kind
    cidx = _compiled_position(mission, step_id)
    if cidx < 0 and sem_i >= 0:
        graph = list(getattr(mission, "compiled_execution_graph", None) or [])
        if sem_i < len(graph):
            cidx = sem_i

    headline = _human_headline(kind, str(getattr(cand, "status", "") or ""))
    tail = ""
    msg = ""
    try:
        msg = str(getattr(cand, "error", "") or "").strip()
    except Exception:
        msg = ""
    if msg and len(msg) < 420:
        tail = msg
    elif (run_error_message or "").strip():
        tail = (run_error_message or "").strip()[:420]

    sub_lines = []
    sem_total = ""
    try:
        plan_ob = getattr(mission, "semantic_execution_plan", None) or {}
        n = len((plan_ob or {}).get("steps") or [])  # fast path
        if n:
            sem_total = str(n)
    except Exception:
        pass
    if sem_total and sem_i >= 0:
        sub_lines.append(
            f"Paso {sem_i + 1} de {sem_total} en tu plan (solo este se revisará)."
        )
    elif sem_i >= 0:
        sub_lines.append(f"Es el paso {sem_i + 1} de tu automatización (semántico).")
    sub_lines.append(
        "Nevlan volverá a preparar la ventana antes de ese paso; "
        "tú sólo muestras la acción corregida."
    )
    if tail:
        sub_lines.append("")  # respiro visual en QLabel
        sub_lines.append(("Detalle: " + tail)[:480])

    return RepairBrief(
        step_semantic_id=step_id,
        step_type=kind,
        semantic_plan_index=sem_i if sem_i >= 0 else 0,
        compiled_step_index=cidx,
        trace_status=str(getattr(cand, "status", "") or ""),
        headline=headline[:500],
        subtext="\n".join(sub_lines)[:2400],
    )


__all__ = ["RepairBrief", "infer_repair_brief_from_run"]
