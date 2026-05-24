"""
ArthurOS Services — MissionOptimizer (FASE 11)
----------------------------------------------
Auto-optimización de misiones grabadas. Toma un ``Mission`` ya
compilado y aplica transformaciones seguras y reversibles para que la
misión se ejecute más rápido, más fiable y con menos pasos.

Reglas (PRD §11):

  1. Eliminar pasos redundantes
       - Clicks duplicados consecutivos sobre el mismo target.
       - Hotkeys duplicados consecutivos.
       - Scrolls que no aportan (delta=0).

  2. Fusionar click+texto en SET_FIELD_VALUE
       - Si un CLICK sobre un campo editable es seguido de TYPE_TEXT,
         fusionarlos. La compilación V3 ya lo hace para misiones nuevas;
         aquí cubrimos misiones legacy.

  3. Reemplazar typewrite por fill/setvalue/paste
       - Convertir TYPE_TEXT con texto y target editable en
         SET_FIELD_VALUE para que el player V3 use locator.fill /
         ValuePattern.SetValue / clipboard paste.

  4. Reemplazar SCROLL manual + CLICK por SCROLL_UNTIL_VISIBLE
       - Idéntico al patrón D del compiler V3, aplicado a misiones
         legacy.

  5. Detectar esperas implícitas
       - Ráfagas de scroll consecutivos → un único SCROLL_UNTIL_VISIBLE.

  6. Convertir coords débiles cuando hay alternativa
       - Si el paso solo tiene fallback_coords pero también
         image_ref_mid/anchor_bbox, cambiamos
         target_resolution_strategy a ICON_VALIDATED_COORDS para que
         el resolver intente la pista visual antes de las coordenadas.

  7. Marcar pasos peligrosos
       - Coord-only con capture_score < 50 → fallback_strategy =
         ASK_HUMAN para que el repair entre antes de equivocarse.

  8. Calcular mission_reliability_score
       - Promedio ponderado de los capture_score por paso (acciones
         sin target cuentan como 95).

API:

    summary = MissionOptimizer.optimize(mission)

``summary`` es un dict con métricas: ``removed``, ``merged``,
``rewritten``, ``marked_dangerous``, ``reliability_score``,
``actions``: lista legible de acciones aplicadas.

La optimización es **idempotente**: re-ejecutarla sobre una misión ya
optimizada no debería cambiarla salvo recálculo del score.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import (
    Mission, CompiledStep, InterpretedStep,
    ActionStrategy, FallbackStrategy, TargetResolutionStrategy,
)
from app.core.logger import log


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

# Acciones que no necesitan target (su confidence es alta por defecto).
_NO_TARGET_ACTIONS = {
    ActionStrategy.SEND_HOTKEY, ActionStrategy.WAIT_FOR_STATE,
    ActionStrategy.SCROLL, ActionStrategy.AGENT_PROMPT,
    ActionStrategy.CONFIRM_DIALOG, ActionStrategy.SUBMIT_SEARCH,
}

# ControlTypes que indican un campo editable (UIA).
_EDIT_CONTROL_TYPES = {
    "editcontrol", "edit", "documentcontrol",
    "passwordcontrol", "comboboxcontrol",
}


def _is_edit_target(step: CompiledStep) -> bool:
    """¿El target del paso es un campo de texto editable?"""
    tc = step.target_context
    uia = tc.uia_data or {}
    if isinstance(uia, dict):
        ct = (uia.get("control_type") or "").strip().lower()
        if ct in _EDIT_CONTROL_TYPES:
            return True
        if uia.get("value_pattern_supported"):
            return True
    web = tc.web_data or {}
    role = (web.get("role") or "").strip().lower()
    if role in ("textbox", "searchbox", "combobox"):
        return True
    # Locator role textbox cuenta también
    for spec in (web.get("locators") or []):
        if (spec.get("kind") == "role"
                and (spec.get("role") or "").lower() in ("textbox", "searchbox")):
            return True
    return False


def _has_strong_signal(step: CompiledStep) -> bool:
    tc = step.target_context
    web = tc.web_data or {}
    uia = tc.uia_data or {}
    if (web.get("locators") or []):
        return True
    if isinstance(uia, dict):
        if (uia.get("automation_id") or "").strip():
            return True
        if (uia.get("name") or "").strip():
            return True
    return bool(tc.anchor_bbox)


def _capture_score(step: CompiledStep) -> int:
    """Lee el capture_score canónico (compiler V3) o lo estima por
    heurística (compatibilidad con misiones antiguas)."""
    try:
        cs = (step.target_context.confidence or {}).get("capture_score")
        if cs is not None:
            return max(0, min(100, int(round(float(cs)))))
    except Exception:
        pass

    if step.action_strategy in _NO_TARGET_ACTIONS:
        return 95
    if step.action_strategy == ActionStrategy.TYPE_TEXT:
        return 90  # depende del foco previo
    score = 0
    tc = step.target_context
    web = tc.web_data or {}
    uia = tc.uia_data or {}
    locators = web.get("locators") or []
    if locators:
        kinds = {l.get("kind") for l in locators}
        if "test_id" in kinds or "role" in kinds:
            score += 35
        else:
            score += 20
    if isinstance(uia, dict) and (uia.get("automation_id") or "").strip():
        score += 30
    elif isinstance(uia, dict) and (uia.get("name") or "").strip():
        score += 20
    if tc.anchor_bbox and tc.image_ref_mid:
        score += 15
    elif tc.image_ref:
        score += 7
    if tc.fallback_coords_relative_to_window:
        score += 10
    if tc.fallback_coords:
        score += 5
    return max(0, min(100, score))


def _step_target_signature(step: CompiledStep) -> str:
    """Firma estable para detectar 'el mismo target' entre pasos."""
    tc = step.target_context
    sem = tc.semantic or {}
    label = (sem.get("human_label") or "").strip()
    web = tc.web_data or {}
    uia = tc.uia_data or {}

    parts: List[str] = []
    if label:
        parts.append(f"label={label}")
    aid = (uia.get("automation_id") or "").strip() if isinstance(uia, dict) else ""
    if aid:
        parts.append(f"aid={aid}")
    nm = (uia.get("name") or "").strip() if isinstance(uia, dict) else ""
    if nm:
        parts.append(f"uia.name={nm}")
    test_id = (web.get("test_id") or "").strip()
    if test_id:
        parts.append(f"test_id={test_id}")
    accname = (web.get("accessible_name") or "").strip()
    if accname:
        parts.append(f"accname={accname}")
    if not parts:
        # Último recurso: coords absolutas (frágil, pero útil para detectar
        # dobles clicks en exactamente el mismo punto).
        coords = tc.fallback_coords or {}
        if coords:
            parts.append(f"xy={int(coords.get('x', 0))},{int(coords.get('y', 0))}")
    return "|".join(parts)


def _safe_payload_text(step: CompiledStep) -> str:
    return (step.action_payload or {}).get("text") or ""


# ──────────────────────────────────────────────────────────────────
# Optimizador
# ──────────────────────────────────────────────────────────────────

@dataclass
class OptimizationReport:
    removed: int = 0
    merged: int = 0
    rewritten: int = 0
    marked_dangerous: int = 0
    reliability_score: int = 0
    actions: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "removed": self.removed,
            "merged": self.merged,
            "rewritten": self.rewritten,
            "marked_dangerous": self.marked_dangerous,
            "reliability_score": self.reliability_score,
            "actions": list(self.actions),
        }


class MissionOptimizer:
    """Aplica las reglas de FASE 11 sobre una misión compilada.

    El método principal es ``optimize(mission)``. Devuelve un dict con
    el resumen y modifica la misión in-place.
    """

    @staticmethod
    def optimize(mission: Mission) -> Dict[str, Any]:
        report = OptimizationReport()
        graph = mission.compiled_execution_graph
        interp = mission.interpreted_steps
        if not graph:
            report.reliability_score = 100
            return report.as_dict()

        # 1) Pasada de fusión semántica + dedup. Se hace en una nueva
        #    lista para evitar mutar mientras iteramos.
        new_graph: List[CompiledStep] = []
        new_interp: List[InterpretedStep] = []

        for i, step in enumerate(graph):
            i_step = interp[i] if i < len(interp) else None

            # ── REGLA 1: dedup clicks consecutivos sobre el mismo target ─
            if (new_graph
                    and step.action_strategy == ActionStrategy.CLICK
                    and new_graph[-1].action_strategy == ActionStrategy.CLICK
                    and _step_target_signature(step)
                    and _step_target_signature(step) == _step_target_signature(new_graph[-1])):
                report.removed += 1
                report.actions.append(
                    f"Eliminado click duplicado #{i + 1} sobre el mismo target"
                )
                continue

            # ── REGLA 1b: dedup hotkeys consecutivos idénticos ───────────
            if (new_graph
                    and step.action_strategy == ActionStrategy.SEND_HOTKEY
                    and new_graph[-1].action_strategy == ActionStrategy.SEND_HOTKEY
                    and (step.action_payload or {}) == (new_graph[-1].action_payload or {})):
                report.removed += 1
                report.actions.append(
                    f"Eliminado hotkey duplicado #{i + 1}"
                )
                continue

            # ── REGLA 1c: scrolls de delta 0 son ruido ───────────────────
            if step.action_strategy == ActionStrategy.SCROLL:
                payload = step.action_payload or {}
                dy = int(payload.get("dy") or payload.get("delta_y") or 0)
                dx = int(payload.get("dx") or payload.get("delta_x") or 0)
                ticks = int(payload.get("ticks") or 0)
                if dy == 0 and dx == 0 and ticks == 0:
                    report.removed += 1
                    report.actions.append(f"Eliminado scroll vacío #{i + 1}")
                    continue

            # ── REGLA 5: ráfagas de scrolls consecutivos → coalesce ─────
            if (new_graph
                    and step.action_strategy == ActionStrategy.SCROLL
                    and new_graph[-1].action_strategy == ActionStrategy.SCROLL):
                # Sumar deltas en el último scroll y descartar este.
                prev = new_graph[-1]
                pp = dict(prev.action_payload or {})
                cp = step.action_payload or {}
                pp["dy"] = int(pp.get("dy") or pp.get("delta_y") or 0) + int(
                    cp.get("dy") or cp.get("delta_y") or 0
                )
                pp["dx"] = int(pp.get("dx") or pp.get("delta_x") or 0) + int(
                    cp.get("dx") or cp.get("delta_x") or 0
                )
                pp["ticks"] = int(pp.get("ticks") or 0) + int(cp.get("ticks") or 0)
                prev.action_payload = pp
                report.merged += 1
                report.actions.append(
                    f"Fusionados scrolls consecutivos en el paso #{len(new_graph)}"
                )
                continue

            # ── REGLA 2/3: CLICK sobre campo editable + TYPE_TEXT ───────
            #              → fusionar a SET_FIELD_VALUE.
            if (new_graph
                    and step.action_strategy == ActionStrategy.TYPE_TEXT
                    and new_graph[-1].action_strategy == ActionStrategy.CLICK
                    and _is_edit_target(new_graph[-1])
                    and _safe_payload_text(step)):
                prev = new_graph[-1]
                prev.action_strategy = ActionStrategy.SET_FIELD_VALUE
                prev.action_payload = dict(prev.action_payload or {})
                prev.action_payload["text"] = _safe_payload_text(step)
                # Adjuntar etiqueta humana para el player.
                sem = dict(prev.target_context.semantic or {})
                sem.setdefault("intent", "set_field_value")
                sem.setdefault(
                    "expected_state_after",
                    sem.get("expected_state_after") or "field_value_set",
                )
                prev.target_context.semantic = sem
                report.merged += 1
                report.actions.append(
                    f"Fusionados CLICK + TYPE_TEXT en SET_FIELD_VALUE "
                    f"(paso #{len(new_graph)})"
                )
                # Refrescar la descripción del paso fusionado.
                if new_interp:
                    text = _safe_payload_text(step)
                    short = text if len(text) <= 30 else text[:30] + "…"
                    label = sem.get("human_label") or "campo"
                    new_interp[-1].description = (
                        f"📝 Rellenar {label} con '{short}'"
                    )
                continue

            # ── REGLA 3 (standalone): TYPE_TEXT con target editable ──
            #     pero sin click previo → reescribir como SET_FIELD_VALUE.
            if (step.action_strategy == ActionStrategy.TYPE_TEXT
                    and _is_edit_target(step)
                    and _safe_payload_text(step)):
                step.action_strategy = ActionStrategy.SET_FIELD_VALUE
                sem = dict(step.target_context.semantic or {})
                sem.setdefault("intent", "set_field_value")
                sem.setdefault(
                    "expected_state_after",
                    sem.get("expected_state_after") or "field_value_set",
                )
                step.target_context.semantic = sem
                report.rewritten += 1
                report.actions.append(
                    f"TYPE_TEXT → SET_FIELD_VALUE (paso #{i + 1})"
                )

            # ── REGLA 4: SCROLL anterior + CLICK actual → SCROLL_UNTIL_VISIBLE
            if (new_graph
                    and step.action_strategy == ActionStrategy.CLICK
                    and new_graph[-1].action_strategy == ActionStrategy.SCROLL):
                # Promovemos el último scroll a SCROLL_UNTIL_VISIBLE
                # apuntando al target del click. NO removemos el click,
                # porque seguimos necesitándolo después de hacer visible.
                prev = new_graph[-1]
                prev.action_strategy = ActionStrategy.SCROLL_UNTIL_VISIBLE
                payload = dict(prev.action_payload or {})
                payload["scroll_until"] = {
                    "human_label": (step.target_context.semantic or {}).get(
                        "human_label", ""
                    ),
                    "web_locators": (step.target_context.web_data or {}).get(
                        "locators"
                    ) or [],
                    "uia_automation_id": (step.target_context.uia_data or {}).get(
                        "automation_id", ""
                    ) if isinstance(step.target_context.uia_data, dict) else "",
                    "uia_name": (step.target_context.uia_data or {}).get(
                        "name", ""
                    ) if isinstance(step.target_context.uia_data, dict) else "",
                }
                prev.action_payload = payload
                report.rewritten += 1
                report.actions.append(
                    f"SCROLL + CLICK fusionado a SCROLL_UNTIL_VISIBLE (#{len(new_graph)})"
                )
                # No `continue`: el click sigue como está.

            new_graph.append(step)
            if i_step is not None:
                new_interp.append(i_step)

        # Reemplazamos in-place.
        mission.compiled_execution_graph = new_graph
        # interpreted_steps se mantiene mejor con su tamaño original
        # cuando es posible; si quedó más corto por dedup, usamos
        # new_interp.
        if len(new_interp) == len(new_graph) and len(new_interp) <= len(interp):
            mission.interpreted_steps = new_interp

        # 2) Re-link grafo (next_step_id_on_success) tras dedup/coalesce.
        for i in range(len(new_graph) - 1):
            new_graph[i].next_step_id_on_success = new_graph[i + 1].id
        if new_graph:
            new_graph[-1].next_step_id_on_success = None

        # 3) Pasos peligrosos (coord-only con score < 50) y refuerzo
        #    de target_resolution_strategy donde haya señales mejores.
        for s in new_graph:
            score = _capture_score(s)
            tc = s.target_context

            # Refuerzo de strategy: si tenemos imagen + anchor pero la
            # strategy sigue siendo COORDS_ABSOLUTE, ascendemos a
            # ICON_VALIDATED_COORDS para que el resolver intente
            # validar visualmente antes de moverse en ciegas.
            if (s.target_resolution_strategy == TargetResolutionStrategy.COORDS_ABSOLUTE
                    and tc.image_ref_mid and tc.anchor_bbox):
                s.target_resolution_strategy = (
                    TargetResolutionStrategy.COORDINATE_ICON_VALIDATED_CLICK
                )
                report.rewritten += 1
                report.actions.append(
                    "Coords absolutas → COORDINATE_ICON_VALIDATED_CLICK"
                )

            # Si hay locator web pero strategy es COORDS, subimos a
            # UIA_THEN_VISION (que el resolver V2 usa como puerta a
            # web → uia → vision).
            if (s.target_resolution_strategy == TargetResolutionStrategy.COORDS_ABSOLUTE
                    and (tc.web_data or {}).get("locators")):
                s.target_resolution_strategy = TargetResolutionStrategy.UIA_THEN_VISION
                report.rewritten += 1
                report.actions.append(
                    f"Coords absolutas → UIA_THEN_VISION (hay locators web)"
                )

            # Marcar dangerous: solo coords y score bajo.
            is_coord_only = (
                not _has_strong_signal(s)
                and bool(tc.fallback_coords)
            )
            if is_coord_only and score < 50:
                if s.fallback_strategy != FallbackStrategy.ASK_HUMAN:
                    s.fallback_strategy = FallbackStrategy.ASK_HUMAN
                    report.marked_dangerous += 1
                    report.actions.append(
                        f"Paso marcado como peligroso (coord-only, score={score})"
                    )

        # 4) mission_reliability_score (promedio ponderado)
        scores: List[int] = [_capture_score(s) for s in new_graph]
        if scores:
            # Penalizamos pasos con score < 50 (multiplicador 0.5).
            weighted: List[float] = []
            for sc in scores:
                weighted.append(sc * (0.5 if sc < 50 else 1.0))
            report.reliability_score = max(
                0, min(100, int(round(sum(weighted) / len(scores))))
            )
        else:
            report.reliability_score = 100

        # Persistimos el score en mission.recovery_rules para que la UI
        # pueda leerlo sin recalcular.
        try:
            recovery = dict(mission.recovery_rules or {})
            recovery["mission_reliability_score"] = report.reliability_score
            mission.recovery_rules = recovery
        except Exception:
            pass

        log.info(
            f"MissionOptimizer: removed={report.removed} merged={report.merged} "
            f"rewritten={report.rewritten} dangerous={report.marked_dangerous} "
            f"reliability={report.reliability_score}"
        )
        return report.as_dict()
