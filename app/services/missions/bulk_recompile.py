"""Vista previa de recompilación masiva — sin escritura en disco.

Incluye también :func:`sipe_migrate_missions`, una migración 1-shot
que aplica el Semantic Intent Promotion Engine sobre cada misión
recibida y devuelve el resultado por misión (sin escribir a disco —
el caller decide cuándo persistir vía ``MissionStore.save``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.contracts.mission import Mission
from app.core.logger import log
from app.services.missions.compiler import recompile_mission
from app.services.missions.semantic_intent_promotion_engine import (
    apply_promotion_to_mission,
)
from app.services.missions.step_quality import count_weak_steps


def _signature_enriched_steps(m: Mission) -> int:
    n = 0
    for s in m.compiled_execution_graph:
        sig = s.target_context.signature
        if isinstance(sig, dict) and len(sig.keys()) >= 3:
            n += 1
    return n


@dataclass
class BulkRecompilePreviewRow:
    mission_id: str
    mission_name: str
    ok: bool
    error: Optional[str]
    steps_before: int
    steps_after: int
    action_groups: int
    weak_steps: int
    signatures_hint: int
    compiled_mission: Optional[Mission] = field(default=None, repr=False, compare=False)


def preview_recompile_candidates(missions_with_trace: List[Mission]) -> List[BulkRecompilePreviewRow]:
    """Recompila en memoria cada misión — copia profunda vía modelo."""
    out: List[BulkRecompilePreviewRow] = []
    for m in missions_with_trace:
        n_before = len(m.compiled_execution_graph)
        row = BulkRecompilePreviewRow(
            mission_id=m.id,
            mission_name=m.name,
            ok=False,
            error=None,
            steps_before=n_before,
            steps_after=n_before,
            action_groups=len(getattr(m, "action_groups", []) or []),
            weak_steps=count_weak_steps(
                m.compiled_execution_graph,
                m.interpreted_steps,
            ),
            signatures_hint=_signature_enriched_steps(m),
        )
        try:
            cloned = Mission.model_validate(m.model_dump(mode="json"))
            nw = recompile_mission(cloned)
            row.ok = True
            row.steps_after = len(nw.compiled_execution_graph)
            row.action_groups = len(getattr(nw, "action_groups", []) or [])
            row.weak_steps = count_weak_steps(
                nw.compiled_execution_graph,
                nw.interpreted_steps,
            )
            row.signatures_hint = _signature_enriched_steps(nw)
            row.compiled_mission = nw
        except Exception as e:
            row.error = str(e)
            row.ok = False
        out.append(row)
    return out


# ─────────────────────────────────────────────────────────────────────
# SIPE migration (PRD 2026-05-14 §next-step)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SipeMigrationRow:
    """Resultado por misión de :func:`sipe_migrate_missions`.

    Atributos:
        mission_id: id estable.
        mission_name: nombre humano.
        ok: ``True`` si SIPE produjo un plan utilizable.
        already_promoted: ``True`` si la misión ya estaba migrada
            (``source == "semantic_intent_promotion_engine"``) y solo
            se reafirmó el flag legacy_debug_only.
        steps_before: tipos del SEP antes de la migración.
        steps_after: tipos genéricos del SEP tras la migración.
        promotions: trazas de SIPE (legacy → genérico) por step.
        blockers: códigos del SIPE que impidieron READY.
        intent_status: estado final (READY / NEEDS_REVIEW / ...).
        error: mensaje si ``ok == False``.
        migrated_mission: la ``Mission`` mutada (para que el caller
            la persista). ``None`` si hubo error.
    """

    mission_id: str
    mission_name: str
    ok: bool
    already_promoted: bool
    steps_before: List[str] = field(default_factory=list)
    steps_after: List[str] = field(default_factory=list)
    promotions: List[Dict[str, Any]] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    intent_status: str = ""
    error: Optional[str] = None
    migrated_mission: Optional[Mission] = field(
        default=None, repr=False, compare=False,
    )


def _kinds_in_sep(mission: Mission) -> List[str]:
    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict):
        return []
    return [str(s.get("type") or "") for s in (sep.get("steps") or [])]


def _is_already_promoted(mission: Mission) -> bool:
    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict):
        return False
    return (
        sep.get("source") == "semantic_intent_promotion_engine"
        and bool(sep.get("steps"))
    )


def sipe_migrate_missions(
    missions: List[Mission],
    *,
    in_place: bool = False,
    skip_already_promoted: bool = True,
) -> List[SipeMigrationRow]:
    """Aplica SIPE a cada misión.

    Args:
        missions: lista de misiones a migrar (idealmente cargadas
            desde el ``MissionStore``).
        in_place: si ``True`` muta directamente las misiones recibidas;
            si ``False`` (default) trabaja sobre una copia profunda
            vía ``Mission.model_validate(model_dump(...))``. Use
            ``in_place=True`` solo si vas a persistir inmediatamente
            con ``MissionStore.save`` y aceptas la mutación.
        skip_already_promoted: si ``True`` (default), las misiones cuyo
            ``semantic_execution_plan.source`` ya sea
            ``"semantic_intent_promotion_engine"`` se reportan como
            ``already_promoted=True`` y no se procesan otra vez.

    Returns:
        Lista de :class:`SipeMigrationRow`, una por cada misión de
        entrada (en el mismo orden). El caller decide cuándo
        persistir cada ``row.migrated_mission``.

    Notas:
        * NO escribe a disco. El caller compone el flujo
          (load → migrate → save) según su necesidad (CLI, tarea
          batch, panel admin).
        * Es idempotente: aplicar la migración a una misión ya
          migrada NO altera el SEP — ``apply_promotion_to_mission``
          regenera el plan determinísticamente.
        * Errores no abortan el batch — cada fallo queda en
          ``row.error`` y la siguiente misión se procesa.
    """
    out: List[SipeMigrationRow] = []
    for m in missions:
        mid = getattr(m, "id", "?") or "?"
        mname = getattr(m, "name", "") or ""
        already = _is_already_promoted(m)
        before_kinds = _kinds_in_sep(m)

        if already and skip_already_promoted:
            out.append(SipeMigrationRow(
                mission_id=mid,
                mission_name=mname,
                ok=True,
                already_promoted=True,
                steps_before=before_kinds,
                steps_after=before_kinds,
                promotions=[],
                blockers=[],
                intent_status=(
                    m.semantic_execution_plan.get("intent_status")
                    if isinstance(m.semantic_execution_plan, dict)
                    else ""
                ),
                migrated_mission=m,
            ))
            continue

        try:
            if in_place:
                target = m
            else:
                # Copia profunda para no mutar la entrada del caller.
                target = Mission.model_validate(m.model_dump(mode="json"))
            result = apply_promotion_to_mission(target)
            after_kinds = _kinds_in_sep(target)
            out.append(SipeMigrationRow(
                mission_id=mid,
                mission_name=mname,
                ok=bool(result.plan.steps),
                already_promoted=False,
                steps_before=before_kinds,
                steps_after=after_kinds,
                promotions=list(result.promotions),
                blockers=list(result.blockers),
                intent_status=result.plan.intent_status,
                migrated_mission=target,
            ))
        except Exception as e:
            log.exception(f"[sipe_migrate] mission {mid} falló: {e}")
            out.append(SipeMigrationRow(
                mission_id=mid,
                mission_name=mname,
                ok=False,
                already_promoted=False,
                steps_before=before_kinds,
                steps_after=before_kinds,
                error=str(e),
                migrated_mission=None,
            ))
    return out
