"""
Nevlan — Mission Truth Gate (PRD 2026-05-10d)
==============================================

Choke-point único que decide si una misión es ejecutable y qué pasos
debe mostrar la UI. Resuelve la fuga legacy donde:

  * ``mission.status = "compiled"`` quedaba persistido aunque el
    ``semantic_execution_plan`` dijera ``NEEDS_REVIEW``.
  * El executor podía caer al ``compiled_execution_graph`` legacy
    aún teniendo plan semántico inválido.
  * La UI mostraba ``interpreted_steps`` rojos cuando el plan
    semántico ya los había desautorizado.

Reglas (PRD §):

  1. ``semantic_execution_plan`` es la única fuente ejecutable.
  2. ``compiled_execution_graph`` solo puede existir como debug.
  3. Si SEP no es READY/EXECUTABLE, la misión queda
     ``NEEDS_REVIEW`` — nunca ``compiled`` ejecutable.
  4. Pasos legacy con ``use_coords``, ``GroupControl``,
     ``capture_score=0`` o ``fallback="use_coords"`` no pueden
     desbloquear ejecución.
  5. Si el Recorder Truth Layer marcó eventos como
     ``weak``/``insufficient`` y eso se traduce en SEP no READY,
     el gate los respeta (NO los promociona a high confidence).
  6. La misión no puede guardarse como ``compiled``/``executable``
     si hay mismatch entre raw_trace, truth events, SEP y
     ``compiled_execution_graph``.

  Prioridad producto (PRD 2026-05-17b): **EXECUTION_TRUTH** (
     :mod:`execution_truth_engine`) informa el alivio de ``needs_user_label``
     espurio antes del READY — sin sustituir bloqueos humanos obligatorios
     (perfil/query ambigua).

API pública (intencionalmente pequeña):

  * :func:`reconcile_mission_status(mission)` — alinea
    ``mission.status`` con el ``semantic_execution_plan``. Es la
    función que el compiler debe llamar antes de persistir.
  * :func:`is_executable(mission)` — único bool fiable para
    "puedo ejecutar esta misión ya".
  * :func:`steps_for_ui(mission)` — único origen de pasos para la
    UI (Centro de Automatizaciones, Mission Review).
  * :func:`gate_decision(mission)` — :class:`TruthGateDecision`
    con el detalle (status, blockers, source, mismatches).

Este módulo NO ejecuta nada, NO toca disco, NO importa Qt y NO
agrega nuevos detectores. Es un wrapper / consolidación.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.contracts.mission import Mission, MissionStatus
from app.core.logger import log


# Códigos de decisión que el gate emite. Los reusa la UI / tests.
class TruthGateCode:
    NO_SEMANTIC_PLAN = "GATE_NO_SEMANTIC_PLAN"
    PLAN_NOT_READY = "GATE_PLAN_NOT_READY"
    PLAN_INVALID_STRICT = "GATE_PLAN_INVALID_STRICT"
    LAYER_MISMATCH = "GATE_LAYER_MISMATCH"
    LEGACY_GRAPH_EXECUTABLE = "GATE_LEGACY_GRAPH_EXECUTABLE_PURPOSE"


# Sources que el gate publica para auditoría / smart_runner_bridge.
SOURCE_SEMANTIC_PLAN = "semantic_execution_plan"
SOURCE_COMPILED_GRAPH_DEBUG = "compiled_execution_graph_debug_only"
SOURCE_NONE = "none"


@dataclass
class TruthGateDecision:
    """Decisión consolidada del gate sobre una misión."""

    mission_id: str = ""
    is_executable: bool = False
    canonical_status: str = MissionStatus.NEEDS_REVIEW.value
    blockers: List[str] = field(default_factory=list)
    source: str = SOURCE_NONE
    intent_status: str = ""
    not_ready_reasons: List[str] = field(default_factory=list)
    mismatches: List[Dict[str, Any]] = field(default_factory=list)
    semantic_ambiguity: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "is_executable": bool(self.is_executable),
            "canonical_status": self.canonical_status,
            "blockers": list(self.blockers),
            "source": self.source,
            "intent_status": self.intent_status,
            "not_ready_reasons": list(self.not_ready_reasons),
            "mismatches": list(self.mismatches),
            "semantic_ambiguity": dict(self.semantic_ambiguity),
        }


# ──────────────────────────────────────────────────────────────────────
# Helpers internos
# ──────────────────────────────────────────────────────────────────────


def _sep_blob(mission: Any) -> Dict[str, Any]:
    sep = getattr(mission, "semantic_execution_plan", None)
    return sep if isinstance(sep, dict) else {}


def _sep_steps(mission: Any) -> List[Dict[str, Any]]:
    sep = _sep_blob(mission)
    steps = sep.get("steps") or []
    return [s for s in steps if isinstance(s, dict)]


def _intent_status(mission: Any) -> str:
    return str(_sep_blob(mission).get("intent_status") or "")


def _not_ready_reasons(mission: Any) -> List[str]:
    return list(_sep_blob(mission).get("not_ready_reasons") or [])


def _refresh_intent_status(mission: Any) -> None:
    """Recompute SEP intent_status + not_ready_reasons in place.

    Idempotente: si el plan está vacío o corrupto, no hace nada.
    """
    sep = _sep_blob(mission)
    if not sep or not sep.get("steps"):
        return
    try:
        from app.services.missions.operational_continuity_reconstruction import (
            apply_ocrx_before_ready_status,
        )

        apply_ocrx_before_ready_status(mission)
        sep = _sep_blob(mission)
    except Exception as e:
        log.debug(f"[truth_gate] ocrx before refresh: {e}")
    try:
        from app.services.missions.semantic_execution_plan import (
            SemanticExecutionPlan,
            attach_intent_status_to_plan,
        )
        plan = SemanticExecutionPlan.from_dict(sep)
        attach_intent_status_to_plan(plan, mission=mission)
        mission.semantic_execution_plan = plan.to_dict()
    except Exception as e:
        log.debug(f"[truth_gate] refresh intent_status fallo: {e}")


def _detect_layer_mismatches(mission: Any) -> List[Dict[str, Any]]:
    """Detecta inconsistencias entre las capas de la misión.

    El gate NO crea detectores nuevos: reutiliza los datos ya
    publicados (``_dropped_semantic_events`` por el truth layer +
    ICE, ``_contamination_details`` por compute_ready_status,
    ``legacy_compiled_graph_purpose``).
    """
    out: List[Dict[str, Any]] = []
    dropped = list(getattr(mission, "_dropped_semantic_events", None) or [])
    if dropped:
        out.append({
            "code": "DROPPED_RELEVANT_EVENTS",
            "count": len(dropped),
            "detail": "raw_trace tiene eventos relevantes sin step semántico",
        })
    purpose = getattr(mission, "legacy_compiled_graph_purpose", "execution")
    cgraph = getattr(mission, "compiled_execution_graph", None) or []
    if cgraph and purpose not in ("legacy_debug_only", "", None):
        out.append({
            "code": TruthGateCode.LEGACY_GRAPH_EXECUTABLE,
            "purpose": str(purpose),
            "compiled_step_count": len(cgraph),
            "detail": (
                "compiled_execution_graph no marcado como "
                "legacy_debug_only; el executor podría usarlo."
            ),
        })
    return out


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────


def gate_decision(mission: Any) -> TruthGateDecision:
    """Computa la decisión del Mission Truth Gate.

    No muta la misión salvo por refrescar ``intent_status`` /
    ``not_ready_reasons`` del plan (siempre lo dejamos coherente con
    la realidad del momento — esa es la única "mutación" tolerable
    dentro del gate).
    """
    decision = TruthGateDecision(mission_id=str(getattr(mission, "id", "") or ""))

    _refresh_intent_status(mission)
    sep = _sep_blob(mission)
    steps = _sep_steps(mission)
    intent_status = _intent_status(mission)
    nrr = _not_ready_reasons(mission)
    decision.intent_status = intent_status
    decision.not_ready_reasons = list(nrr)

    # 1) Sin SEP usable ⇒ NEEDS_REVIEW.
    if not sep or not steps:
        decision.blockers.append(TruthGateCode.NO_SEMANTIC_PLAN)
        decision.canonical_status = MissionStatus.NEEDS_REVIEW.value
        decision.source = SOURCE_NONE
        return decision

    # 2) Validación estricta del plan (importación tardía para evitar
    #    ciclos: semantic_execution_plan importa de muchos sitios).
    try:
        from app.services.missions.semantic_execution_plan import (
            validate_semantic_execution_plan_strict,
        )
        strict = validate_semantic_execution_plan_strict(mission)
        if not strict.is_valid:
            decision.blockers.append(TruthGateCode.PLAN_INVALID_STRICT)
            for v in strict.violations[:5]:
                decision.blockers.append(str(v.code))
    except Exception as e:
        log.debug(f"[truth_gate] strict validation fallo: {e}")

    # 3) Mismatches entre capas.
    mismatches = _detect_layer_mismatches(mission)
    if mismatches:
        decision.mismatches = mismatches
        decision.blockers.append(TruthGateCode.LAYER_MISMATCH)
        for m in mismatches:
            code = m.get("code")
            if code and code not in decision.blockers:
                decision.blockers.append(str(code))

    # 4) intent_status como veredicto canónico.
    decision.source = SOURCE_SEMANTIC_PLAN
    sab = getattr(mission, "_semantic_ambiguity_audit", None)
    if isinstance(sab, dict):
        try:
            decision.semantic_ambiguity = {
                "requires_human_confirmation": sab.get(
                    "requires_human_confirmation",
                ),
                "is_ambiguous": (sab.get("aggregate") or {}).get(
                    "is_ambiguous"),
                "ambiguity_score": (sab.get("aggregate") or {}).get(
                    "ambiguity_score"),
                "ambiguity_reasons": list(
                    ((sab.get("aggregate") or {}).get(
                        "ambiguity_reasons",
                    )) or [],
                ),
                "promoted_to_clear_intent": (sab.get("aggregate") or {}).get(
                    "promoted_to_clear_intent",
                ),
                "suppress_review_flags": list(
                    ((sab.get("aggregate") or {}).get(
                        "suppress_review_flags",
                    )) or [],
                ),
            }
        except Exception:
            pass
    if intent_status not in ("READY", "EXECUTABLE"):
        decision.blockers.append(TruthGateCode.PLAN_NOT_READY)
        decision.canonical_status = MissionStatus.NEEDS_REVIEW.value
        decision.is_executable = False
        return decision

    # 5) Mismatches presentes ⇒ no podemos ejecutar aunque el plan
    #    "técnicamente" sea READY (regla 8 del PRD).
    if mismatches or any(b for b in decision.blockers
                         if b == TruthGateCode.PLAN_INVALID_STRICT):
        decision.canonical_status = MissionStatus.NEEDS_REVIEW.value
        decision.is_executable = False
        return decision

    # 6) Camino feliz.
    decision.canonical_status = MissionStatus.EXECUTABLE.value
    decision.is_executable = True
    return decision


def reconcile_mission_status(mission: Mission) -> str:
    """Alinea ``mission.status`` con la decisión del gate.

    Esta es la función que el compiler/Mission Review/asset finalizer
    deben invocar para CERRAR la fuga legacy. Devuelve el status
    canónico (string) que se asignó.

    Reglas (PRD §3 + §5):

      * gate.is_executable ⇒ ``EXECUTABLE``
      * en otro caso ⇒ ``NEEDS_REVIEW`` (nunca dejamos ``compiled``
        ejecutable colado en una misión cuyo SEP dice lo contrario).

    También garantiza que ``legacy_compiled_graph_purpose`` quede
    en ``"legacy_debug_only"`` cuando hay un grafo compilado y un
    plan semántico — para que ningún lector accidental lo trate
    como fuente runtime.
    """
    decision = gate_decision(mission)
    new_status = decision.canonical_status

    try:
        if new_status == MissionStatus.EXECUTABLE.value:
            mission.status = MissionStatus.EXECUTABLE
        else:
            mission.status = MissionStatus.NEEDS_REVIEW
    except Exception as e:
        log.debug(f"[truth_gate] no pude asignar mission.status: {e}")

    # Defensa: si hay grafo compilado, márcalo debug-only.
    try:
        cgraph = getattr(mission, "compiled_execution_graph", None) or []
        if cgraph and getattr(mission, "semantic_execution_plan", None):
            mission.legacy_compiled_graph_purpose = "legacy_debug_only"
    except Exception:
        pass

    # Publica la decisión sin floor a la UI/tests.
    try:
        mission._truth_gate_decision = decision.to_dict()
    except Exception:
        pass

    return new_status


def is_executable(mission: Mission) -> bool:
    """Único punto de verdad para "¿puedo ejecutar esta misión?".

    No depende de ``mission.status`` (que pudo quedar persistido
    incorrectamente en JSON viejos): consulta el gate en tiempo
    real.
    """
    return gate_decision(mission).is_executable


def steps_for_ui(mission: Mission) -> List[Dict[str, Any]]:
    """Devuelve la lista canónica de pasos para mostrar en la UI.

    Reglas:

      * Si la misión tiene un ``semantic_execution_plan`` con steps,
        esos son los únicos que mostramos — son la única fuente
        ejecutable y son la única que el usuario debe ver.
      * Si no hay plan, devolvemos lista vacía. La UI debe entonces
        mostrar el placeholder "Pendiente de revisión" y NO usar
        ``interpreted_steps`` ni ``compiled_execution_graph``.

    Esta función NO retorna pasos legacy aunque existan: ese es el
    fix de la fuga (mission_1778451248).
    """
    return list(_sep_steps(mission))


def has_semantic_view(mission: Any) -> bool:
    """``True`` si el SEP tiene pasos: la UI normal DEBE usar el gate.

    Útil como guard barato (sin construir lista) en sitios calientes
    como la lista lateral o el badge "N pasos".
    """
    return bool(_sep_steps(mission))


def visible_step_count(mission: Any) -> int:
    """Conteo canónico de pasos para mostrar en la UI.

    Reglas:

      * Si hay SEP con steps → se cuenta solo el SEP (única verdad).
      * Si NO hay SEP → fallback al ``compiled_execution_graph`` para
        misiones legacy persistidas antes del Mission Truth Gate.

    Cualquier misión nueva que pase por el compiler debe terminar con
    SEP, así que esta segunda rama solo aplica a JSONs viejos.
    """
    sep_steps = _sep_steps(mission)
    if sep_steps:
        return len(sep_steps)
    cgraph = getattr(mission, "compiled_execution_graph", None) or []
    return len(cgraph)


def step_card_models(
    mission: Any,
    *,
    expert: bool = False,
) -> List[Dict[str, Any]]:
    """Modelos UI-friendly para renderizar la lista visible de pasos.

    Cada item es un dict listo para tarjeta:

        {
          "index": int,
          "kind": str,                  # tipo semántico o action_strategy legacy
          "human_label": str,           # texto principal para el usuario
          "needs_user_label": bool,     # pill "Pendiente revisión"
          "label_prompt": str,          # texto del prompt si aplica
          "preferred_strategy": str,
          "blockers": List[str],
          "source": str,                # SEMANTIC_PLAN | LEGACY_FALLBACK | DEBUG
        }

    Reglas:

      * ``expert=False`` (vista normal):

        - Si hay SEP → solo pasos del SEP. ``compiled_execution_graph``
          NO se mezcla, aunque exista.
        - Si NO hay SEP → fallback con ``compiled_execution_graph`` +
          ``interpreted_steps`` para que misiones legacy sigan visibles.

      * ``expert=True`` (vista experto / debug-only):

        - Siempre incluye los SEP-steps si existen.
        - Adicionalmente añade los pasos del ``compiled_execution_graph``
          marcados con ``source = "compiled_execution_graph_debug_only"``.
        - Esta vista es opt-in y nunca debe ser la primaria.
    """
    out: List[Dict[str, Any]] = []
    sep_steps = _sep_steps(mission)

    if sep_steps:
        try:
            from app.services.missions.semantic_ambiguity_engine import (
                analyze_semantic_step_dict,
            )
        except Exception:
            analyze_semantic_step_dict = None

        for i, s in enumerate(sep_steps):
            needs_human = False
            if callable(analyze_semantic_step_dict):
                try:
                    sar = analyze_semantic_step_dict(
                        s, mission, step_index=i,
                    )
                    needs_human = bool(sar.requires_human_confirmation)
                except Exception:
                    needs_human = bool(s.get("needs_user_label") or False)
            else:
                needs_human = bool(s.get("needs_user_label") or False)

            out.append({
                "index": i,
                "kind": str(s.get("type") or ""),
                "human_label": str(
                    s.get("human_label") or s.get("type") or "Paso"
                ),
                "needs_user_label": needs_human,
                "label_prompt": str(s.get("label_prompt") or ""),
                "preferred_strategy": str(s.get("preferred_strategy") or ""),
                "blockers": [],
                "source": SOURCE_SEMANTIC_PLAN,
            })
        if expert:
            cgraph = getattr(mission, "compiled_execution_graph", None) or []
            for i, c in enumerate(cgraph):
                strat = getattr(c, "action_strategy", None)
                strat = getattr(strat, "value", None) or str(strat or "")
                out.append({
                    "index": i,
                    "kind": strat,
                    "human_label": f"[debug] {strat or 'paso'} #{i + 1}",
                    "needs_user_label": False,
                    "label_prompt": "",
                    "preferred_strategy": "",
                    "blockers": [],
                    "source": SOURCE_COMPILED_GRAPH_DEBUG,
                })
        return out

    # Sin SEP — fallback legacy (misiones viejas pre-gate).
    cgraph = getattr(mission, "compiled_execution_graph", None) or []
    interp = getattr(mission, "interpreted_steps", None) or []
    for i, c in enumerate(cgraph):
        strat = getattr(c, "action_strategy", None)
        strat = getattr(strat, "value", None) or str(strat or "")
        desc = ""
        if i < len(interp):
            desc = str(getattr(interp[i], "description", "") or "")
        out.append({
            "index": i,
            "kind": strat,
            "human_label": desc or f"Paso {i + 1}",
            "needs_user_label": False,
            "label_prompt": "",
            "preferred_strategy": "",
            "blockers": [],
            "source": "legacy_compiled_graph",
        })
    return out


def ui_blockers(mission: Any) -> List[str]:
    """Lista de blockers visibles cuando la misión NO es ejecutable.

    La UI normal debe mostrarlos como pills/banner en lugar de los
    pasos legacy. Cuando ``is_executable`` es ``True`` retorna ``[]``.
    """
    decision = gate_decision(mission)
    if decision.is_executable:
        return []
    return list(decision.blockers)


def assert_no_layer_mismatch(mission: Mission) -> None:
    """Lanza ``AssertionError`` si hay mismatch entre capas.

    Útil para tests y sanity checks en runtime cuando queremos
    fallar explícito en lugar de silenciosamente bajar a un grafo
    inválido.
    """
    decision = gate_decision(mission)
    if decision.mismatches:
        raise AssertionError(
            "Mission Truth Gate detectó mismatch entre capas: "
            f"{decision.mismatches}"
        )


__all__ = [
    "TruthGateCode",
    "TruthGateDecision",
    "SOURCE_SEMANTIC_PLAN",
    "SOURCE_COMPILED_GRAPH_DEBUG",
    "SOURCE_NONE",
    "gate_decision",
    "reconcile_mission_status",
    "is_executable",
    "steps_for_ui",
    "has_semantic_view",
    "visible_step_count",
    "step_card_models",
    "ui_blockers",
    "assert_no_layer_mismatch",
]
