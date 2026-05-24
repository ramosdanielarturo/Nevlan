"""Four-Layer Operational Model (4LOM).

Capas (ninguna reemplaza a otra):

  1. **Goal** — propósito de la automatización.
  2. **Contract** — criterios de éxito y estado READY.
  3. **Human View** — etiquetas y orden para edición en Mission Review.
  4. **Machine Execution** — tipos, params y estrategias que ejecuta el runtime.

Reglas de mutación:

  * Editar Human View → no cambia ``semantic_type`` ni ``params`` de máquina.
  * Editar Machine → no cambia ``goal.purpose_statement``.
  * Editar Goal → no reescribe pasos de máquina.
  * Contract se **recomputa** desde SEP + validadores (espejo de éxito).

El SEP sigue siendo el transporte de ejecución; 4LOM es la vista estructurada
para edición incremental sin regrabar.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    FourLayerOperationalModel,
    HumanViewLayer,
    HumanViewStep,
    MachineExecutionLayer,
    MachineExecutionStep,
    Mission,
    OperationalContractCriterion,
    OperationalContractLayer,
    OperationalGoalLayer,
)
from app.core.logger import log


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_model(mission: Mission) -> Optional[FourLayerOperationalModel]:
    raw = mission.four_layer_operational_model
    if not isinstance(raw, dict):
        return None
    try:
        return FourLayerOperationalModel.model_validate(raw)
    except Exception:
        return None


def _save_model(mission: Mission, model: FourLayerOperationalModel) -> None:
    model.last_synced_at = utc_now_iso()
    mission.four_layer_operational_model = model.model_dump(mode="json")


def _infer_goal_purpose(mission: Mission) -> Tuple[str, str]:
    """Propósito desde descripción / interpreted_steps / nombre."""
    if (mission.description or "").strip():
        return mission.description.strip(), "mission_description"
    for ist in mission.interpreted_steps or []:
        g = str(getattr(ist, "inferred_goal", "") or "").strip()
        if g:
            return g, "interpreted_step"
    name = str(mission.name or "").strip()
    if name:
        return f"Automatizar: {name}", "mission_name"
    return "Automatización registrada", "default"


def _contract_from_sep(sep: Dict[str, Any]) -> OperationalContractLayer:
    criteria: List[OperationalContractCriterion] = []
    for code in sep.get("not_ready_reasons") or []:
        c = str(code or "").strip()
        if c:
            criteria.append(
                OperationalContractCriterion(
                    code=c,
                    description="Bloqueador activo hasta resolución",
                    required=True,
                ),
            )
    status = str(sep.get("intent_status") or "")
    if status in ("READY", "EXECUTABLE"):
        criteria.append(
            OperationalContractCriterion(
                code="INTENT_STATUS_READY",
                description="Plan semántico listo para ejecutar",
                required=True,
            ),
        )
    validators = list(sep.get("validator_codes") or [])
    if not validators and status in ("READY", "EXECUTABLE"):
        validators = ["semantic_plan_invariants"]
    return OperationalContractLayer(
        success_criteria=criteria,
        validator_codes=validators,
        intent_status=status,
        not_ready_reasons=list(sep.get("not_ready_reasons") or []),
        ready_provenance=str(sep.get("ready_provenance") or ""),
    )


def _machine_from_sep(sep: Dict[str, Any]) -> MachineExecutionLayer:
    steps: List[MachineExecutionStep] = []
    for sp in sep.get("steps") or []:
        if not isinstance(sp, dict):
            continue
        sid = str(sp.get("id") or "").strip()
        if not sid:
            continue
        pars = dict(sp.get("params") or {})
        ident_raw = pars.get("operational_element_identity") or pars.get(
            "canonical_operational_intent", {},
        )
        if isinstance(ident_raw, dict):
            coi_ident = ident_raw.get("operational_element_identity")
            if isinstance(coi_ident, dict) and not pars.get("operational_element_type"):
                pars.setdefault(
                    "operational_element_type",
                    coi_ident.get("operational_element_type", ""),
                )
                pars.setdefault(
                    "operational_element_identity_id",
                    coi_ident.get("identity_id", ""),
                )
        steps.append(
            MachineExecutionStep(
                step_id=sid,
                semantic_type=str(sp.get("type") or ""),
                params=pars,
                preferred_strategy=str(sp.get("preferred_strategy") or ""),
                fallback_strategies=list(sp.get("fallback_strategies") or []),
                confidence=float(sp.get("confidence") or 0.0),
                block_id=sp.get("block_id"),
                disabled=bool(
                    sp.get("disabled")
                    or pars.get("disabled")
                    or pars.get("four_layer_disabled"),
                ),
            ),
        )
    return MachineExecutionLayer(
        steps=steps,
        source=str(sep.get("source") or "semantic_execution_plan"),
        coords_used=bool(sep.get("coords_used")),
        legacy_graph_ignored=bool(sep.get("legacy_graph_ignored", True)),
    )


def _human_view_from_machine_and_sep(
    machine: MachineExecutionLayer,
    sep: Dict[str, Any],
    *,
    prior: Optional[HumanViewLayer] = None,
) -> HumanViewLayer:
    """Construye Human View preservando ediciones previas por ``machine_step_id``."""
    prior_by_id: Dict[str, HumanViewStep] = {}
    if prior:
        for hv in prior.steps:
            prior_by_id[str(hv.machine_step_id)] = hv

    sep_by_id = {
        str(s.get("id") or ""): s
        for s in (sep.get("steps") or [])
        if isinstance(s, dict)
    }

    steps_out: List[HumanViewStep] = []
    for i, ms in enumerate(machine.steps):
        prev = prior_by_id.get(ms.step_id)
        sep_step = sep_by_id.get(ms.step_id) or {}
        label = ""
        if prev and (prev.human_label or "").strip():
            label = prev.human_label.strip()
        else:
            label = str(sep_step.get("human_label") or "").strip()
        steps_out.append(
            HumanViewStep(
                machine_step_id=ms.step_id,
                display_index=prev.display_index if prev else (i + 1),
                human_label=label,
                needs_review=bool(
                    prev.needs_review if prev else sep_step.get("needs_user_label"),
                ),
                confirmed=bool(prev.confirmed if prev else False),
                hidden=bool(prev.hidden if prev else False),
                disabled=bool(
                    prev.disabled if prev else ms.disabled,
                ),
                user_notes=str(prev.user_notes if prev else ""),
                badges=list(prev.badges if prev else []),
            ),
        )

    steps_out.sort(key=lambda s: (s.display_index, s.machine_step_id))
    for j, st in enumerate(steps_out):
        st.display_index = j + 1

    view_id = prior.view_id if prior else str(__import__("uuid").uuid4())
    version = (prior.version + 1) if prior else 1
    return HumanViewLayer(view_id=view_id, steps=steps_out, version=version)


def build_four_layer_model(
    mission: Mission,
    *,
    preserve_goal: bool = True,
    preserve_human_edits: bool = True,
) -> FourLayerOperationalModel:
    """Sintetiza las cuatro capas desde la misión actual."""
    sep = dict(mission.semantic_execution_plan or {})
    prior = _load_model(mission) if preserve_human_edits or preserve_goal else None

    if preserve_goal and prior and prior.goal.purpose_statement:
        goal = prior.goal.model_copy(deep=True)
    else:
        purpose, source = _infer_goal_purpose(mission)
        goal = OperationalGoalLayer(
            purpose_statement=purpose,
            summary=purpose[:240],
            source=source,
        )

    machine = _machine_from_sep(sep)
    contract = _contract_from_sep(sep)
    prior_hv = prior.human_view if (preserve_human_edits and prior) else None
    human_view = _human_view_from_machine_and_sep(machine, sep, prior=prior_hv)

    model = FourLayerOperationalModel(
        goal=goal,
        contract=contract,
        human_view=human_view,
        machine_execution=machine,
        last_synced_at=utc_now_iso(),
    )
    try:
        from app.services.runtime.operational_runtime_consistency_engine import (
            attach_runtime_expectations_to_model,
        )

        attach_runtime_expectations_to_model(model)
    except Exception as exc:
        log.debug("[4lom] ORCE expectations: %s", exc)
    return model


def ensure_four_layer_model(mission: Mission) -> FourLayerOperationalModel:
    """Garantiza 4LOM presente y alineado con SEP (machine+contract)."""
    sep = mission.semantic_execution_plan or {}
    model = _load_model(mission)
    sep_ids = {
        str(s.get("id") or "")
        for s in (sep.get("steps") or [])
        if isinstance(s, dict)
    }
    if model is None:
        model = build_four_layer_model(mission)
        _save_model(mission, model)
        return model

    machine_ids = {ms.step_id for ms in model.machine_execution.steps}
    if machine_ids != sep_ids and sep_ids:
        model = build_four_layer_model(mission, preserve_goal=True, preserve_human_edits=True)
        _save_model(mission, model)
        return model

    model.contract = _contract_from_sep(sep)
    model.last_synced_at = utc_now_iso()
    _save_model(mission, model)
    return model


def project_human_view_to_sep(mission: Mission) -> None:
    """Proyecta etiquetas Human View al SEP (solo ``human_label``, overlay flag)."""
    model = ensure_four_layer_model(mission)
    sep = dict(mission.semantic_execution_plan or {})
    steps = list(sep.get("steps") or [])
    hv_by_id = {hv.machine_step_id: hv for hv in model.human_view.steps}

    new_steps: List[Dict[str, Any]] = []
    for sp in steps:
        if not isinstance(sp, dict):
            continue
        sid = str(sp.get("id") or "")
        hv = hv_by_id.get(sid)
        row = dict(sp)
        if hv:
            row["human_label"] = hv.human_label
            row["needs_user_label"] = bool(hv.needs_review and not hv.confirmed)
            pars = dict(row.get("params") or {})
            pars["human_view_overlay"] = True
            pars["four_layer_human_label"] = hv.human_label
            row["params"] = pars
        new_steps.append(row)

    visible_order = [
        hv.machine_step_id
        for hv in sorted(model.human_view.steps, key=lambda x: x.display_index)
        if not hv.hidden
    ]
    order_map = {sid: i for i, sid in enumerate(visible_order)}

    def _sort_key(sp: Dict[str, Any]) -> Tuple[int, str]:
        sid = str(sp.get("id") or "")
        return (order_map.get(sid, 10_000), sid)

    new_steps.sort(key=_sort_key)
    sep["steps"] = new_steps
    mission.semantic_execution_plan = sep


def project_machine_layer_to_sep(mission: Mission) -> None:
    """Proyecta Machine Execution Layer al SEP (sin tocar goal)."""
    model = ensure_four_layer_model(mission)
    sep = dict(mission.semantic_execution_plan or {})
    hv_by_id = {hv.machine_step_id: hv for hv in model.human_view.steps}

    steps_out: List[Dict[str, Any]] = []
    for ms in model.machine_execution.steps:
        hv = hv_by_id.get(ms.step_id)
        pars = dict(ms.params)
        if ms.disabled:
            pars["disabled"] = True
            pars["four_layer_disabled"] = True
        row: Dict[str, Any] = {
            "id": ms.step_id,
            "type": ms.semantic_type,
            "params": pars,
            "preferred_strategy": ms.preferred_strategy,
            "fallback_strategies": list(ms.fallback_strategies),
            "confidence": ms.confidence,
            "human_label": hv.human_label if hv else "",
            "needs_user_label": bool(hv.needs_review if hv else False),
            "block_id": ms.block_id,
            "disabled": bool(ms.disabled),
        }
        if hv and hv.human_label:
            row["params"]["four_layer_human_label"] = hv.human_label
        steps_out.append(row)

    sep["steps"] = steps_out
    sep["coords_used"] = model.machine_execution.coords_used
    sep["legacy_graph_ignored"] = model.machine_execution.legacy_graph_ignored
    sep["source"] = model.machine_execution.source
    mission.semantic_execution_plan = sep


def apply_human_view_label_edit(
    mission: Mission,
    machine_step_id: str,
    *,
    human_label: Optional[str] = None,
    needs_review: Optional[bool] = None,
    confirmed: Optional[bool] = None,
    user_notes: Optional[str] = None,
    hidden: Optional[bool] = None,
) -> HumanViewStep:
    """Edita solo la capa Human View; proyecta overlay al SEP."""
    model = ensure_four_layer_model(mission)
    found: Optional[HumanViewStep] = None
    for hv in model.human_view.steps:
        if hv.machine_step_id == machine_step_id:
            found = hv
            break
    if found is None:
        raise KeyError(f"machine_step_id not in human view: {machine_step_id}")

    if human_label is not None:
        found.human_label = str(human_label).strip()
    if needs_review is not None:
        found.needs_review = bool(needs_review)
    if confirmed is not None:
        found.confirmed = bool(confirmed)
        if confirmed:
            found.needs_review = False
    if user_notes is not None:
        found.user_notes = str(user_notes)
    if hidden is not None:
        found.hidden = bool(hidden)

    model.human_view.version += 1
    _save_model(mission, model)
    project_human_view_to_sep(mission)
    return found


def apply_human_view_reorder(
    mission: Mission,
    ordered_machine_step_ids: List[str],
) -> HumanViewLayer:
    """Reordena solo presentación (Human View); machine ids estables."""
    model = ensure_four_layer_model(mission)
    by_id = {hv.machine_step_id: hv for hv in model.human_view.steps}
    new_steps: List[HumanViewStep] = []
    for i, sid in enumerate(ordered_machine_step_ids):
        hv = by_id.get(sid)
        if hv is None:
            continue
        hv.display_index = i + 1
        new_steps.append(hv)
    for hv in model.human_view.steps:
        if hv.machine_step_id not in ordered_machine_step_ids:
            hv.display_index = len(new_steps) + 1
            new_steps.append(hv)
    model.human_view.steps = new_steps
    model.human_view.version += 1
    _save_model(mission, model)
    project_human_view_to_sep(mission)
    return model.human_view


def apply_machine_step_edit(
    mission: Mission,
    machine_step_id: str,
    *,
    semantic_type: Optional[str] = None,
    params_patch: Optional[Dict[str, Any]] = None,
    preferred_strategy: Optional[str] = None,
    fallback_strategies: Optional[List[str]] = None,
    confidence: Optional[float] = None,
    refresh_contract: bool = True,
) -> MachineExecutionStep:
    """Edita ejecución sin mutar Goal."""
    model = ensure_four_layer_model(mission)
    goal_snapshot = model.goal.model_copy(deep=True)

    found: Optional[MachineExecutionStep] = None
    for ms in model.machine_execution.steps:
        if ms.step_id == machine_step_id:
            found = ms
            break
    if found is None:
        raise KeyError(f"machine_step_id not in machine layer: {machine_step_id}")

    if semantic_type is not None:
        found.semantic_type = str(semantic_type)
    if params_patch:
        merged = dict(found.params)
        merged.update(params_patch)
        found.params = merged
    if preferred_strategy is not None:
        found.preferred_strategy = preferred_strategy
    if fallback_strategies is not None:
        found.fallback_strategies = list(fallback_strategies)
    if confidence is not None:
        found.confidence = float(confidence)

    model.machine_execution.version += 1
    model.goal = goal_snapshot
    _save_model(mission, model)
    project_machine_layer_to_sep(mission)

    if refresh_contract:
        try:
            from app.services.missions.semantic_execution_plan import (
                SemanticExecutionPlan,
                attach_intent_status_to_plan,
            )

            plan = SemanticExecutionPlan.from_dict(mission.semantic_execution_plan or {})
            attach_intent_status_to_plan(plan, mission=mission)
            mission.semantic_execution_plan = plan.to_dict()
            model.contract = _contract_from_sep(mission.semantic_execution_plan or {})
            _save_model(mission, model)
        except Exception as exc:
            log.debug("[4lom] contract refresh after machine edit: %s", exc)

    return found


def apply_goal_purpose_edit(
    mission: Mission,
    purpose_statement: str,
    *,
    summary: Optional[str] = None,
    source: str = "user",
) -> OperationalGoalLayer:
    """Edita solo Goal; no toca machine steps."""
    model = ensure_four_layer_model(mission)
    model.goal.purpose_statement = str(purpose_statement or "").strip()
    model.goal.summary = (summary or model.goal.purpose_statement)[:240]
    model.goal.source = source
    model.goal.version += 1
    _save_model(mission, model)
    return model.goal


def validate_four_layer_invariants(mission: Mission) -> List[str]:
    """Devuelve códigos de violación entre capas."""
    issues: List[str] = []
    model = _load_model(mission)
    sep = mission.semantic_execution_plan or {}
    if model is None:
        return ["FOUR_LAYER_MISSING"]

    sep_ids = {
        str(s.get("id") or "")
        for s in (sep.get("steps") or [])
        if isinstance(s, dict) and s.get("id")
    }
    machine_ids = {ms.step_id for ms in model.machine_execution.steps}
    hv_ids = {hv.machine_step_id for hv in model.human_view.steps}

    if machine_ids != sep_ids and sep_ids:
        issues.append("MACHINE_SEP_STEP_ID_MISMATCH")
    if hv_ids - machine_ids:
        issues.append("HUMAN_VIEW_ORPHAN_STEP")
    if machine_ids - hv_ids:
        issues.append("HUMAN_VIEW_MISSING_STEP")

    for ms in model.machine_execution.steps:
        hv = next((h for h in model.human_view.steps if h.machine_step_id == ms.step_id), None)
        if hv and hv.human_label:
            sep_step = next(
                (s for s in (sep.get("steps") or []) if str(s.get("id")) == ms.step_id),
                None,
            )
            if sep_step and str(sep_step.get("human_label") or "") != hv.human_label:
                if not (sep_step.get("params") or {}).get("human_view_overlay"):
                    issues.append(f"HUMAN_LABEL_DRIFT:{ms.step_id}")

    return issues


def four_layer_expert_lines(mission: Mission) -> List[str]:
    model = _load_model(mission)
    if model is None:
        return []
    g = model.goal
    c = model.contract
    m = model.machine_execution
    lines = [
        "Four-Layer Operational Model:",
        f"  Goal: {g.purpose_statement[:120] or '—'} (v{g.version})",
        f"  Contract: {c.intent_status or '—'} blockers={len(c.not_ready_reasons)}",
        f"  Human View: {len(model.human_view.steps)} steps (v{model.human_view.version})",
        f"  Machine: {len(m.steps)} steps (v{m.version})",
    ]
    inv = validate_four_layer_invariants(mission)
    if inv:
        lines.append(f"  invariants: {', '.join(inv[:6])}")
    return lines


# ── Reorden seguro (dependencias operacionales) ─────────────────────────────

_STEP_PHASE_RANK: Dict[str, int] = {
    "launch": 0,
    "identity": 1,
    "navigation": 2,
    "input": 3,
    "submit": 4,
    "browse": 5,
    "other": 6,
}


def semantic_type_phase(semantic_type: str) -> str:
    t = str(semantic_type or "").lower()
    if t in ("open_app", "open_application"):
        return "launch"
    if "select" in t or "profile" in t or "entity" in t:
        return "identity"
    if t in ("open_site", "open_url", "open_new_tab", "open_bookmark", "navigate"):
        return "navigation"
    if "submit" in t:
        return "submit"
    if "search" in t or "fill" in t or t == "fill_field":
        return "input"
    if "scroll" in t or "browse" in t:
        return "browse"
    return "other"


def validate_reorder_dependencies(
    machine_steps: Sequence[MachineExecutionStep],
    ordered_machine_step_ids: List[str],
) -> Tuple[bool, str]:
    """Bloquea órdenes que violan launch → identity → navigation → input → submit."""
    by_id = {ms.step_id: ms for ms in machine_steps}
    ranks: List[int] = []
    for sid in ordered_machine_step_ids:
        ms = by_id.get(sid)
        if ms is None or ms.disabled:
            continue
        ranks.append(_STEP_PHASE_RANK.get(semantic_type_phase(ms.semantic_type), 6))
    for i in range(len(ranks) - 1):
        if ranks[i] > ranks[i + 1]:
            return False, (
                f"REORDER_VIOLATION: phase {ranks[i]} before {ranks[i + 1]} "
                f"(launch/identity/nav/input/submit order)"
            )
    return True, ""


def apply_machine_layer_reorder(
    mission: Mission,
    ordered_machine_step_ids: List[str],
) -> MachineExecutionLayer:
    """Reordena Machine Layer + SEP (ids estables)."""
    model = ensure_four_layer_model(mission)
    ok, reason = validate_reorder_dependencies(
        model.machine_execution.steps,
        ordered_machine_step_ids,
    )
    if not ok:
        raise ValueError(reason)

    by_id = {ms.step_id: ms for ms in model.machine_execution.steps}
    new_machine: List[MachineExecutionStep] = []
    seen: set = set()
    for sid in ordered_machine_step_ids:
        if sid in by_id and sid not in seen:
            new_machine.append(by_id[sid])
            seen.add(sid)
    for ms in model.machine_execution.steps:
        if ms.step_id not in seen:
            new_machine.append(ms)

    model.machine_execution.steps = new_machine
    model.machine_execution.version += 1
    _save_model(mission, model)
    apply_human_view_reorder(mission, [ms.step_id for ms in new_machine])
    project_machine_layer_to_sep(mission)
    return model.machine_execution


def apply_step_disabled(
    mission: Mission,
    machine_step_id: str,
    *,
    disabled: bool = True,
) -> None:
    """Desactiva paso en Human View + Machine; runtime lo omite."""
    model = ensure_four_layer_model(mission)
    for ms in model.machine_execution.steps:
        if ms.step_id == machine_step_id:
            ms.disabled = bool(disabled)
            pars = dict(ms.params)
            if disabled:
                pars["disabled"] = True
                pars["four_layer_disabled"] = True
            else:
                pars.pop("disabled", None)
                pars.pop("four_layer_disabled", None)
            ms.params = pars
            break
    for hv in model.human_view.steps:
        if hv.machine_step_id == machine_step_id:
            hv.disabled = bool(disabled)
            break
    model.machine_execution.version += 1
    model.human_view.version += 1
    _save_model(mission, model)
    project_machine_layer_to_sep(mission)
    project_human_view_to_sep(mission)


def persist_four_layer_mission(
    mission: Mission,
    *,
    refresh_contract: bool = True,
) -> List[str]:
    """Persiste 4LOM + SEP; devuelve violaciones de invariantes."""
    ensure_four_layer_model(mission)
    project_machine_layer_to_sep(mission)
    project_human_view_to_sep(mission)
    if refresh_contract:
        try:
            from app.services.missions.semantic_execution_plan import (
                SemanticExecutionPlan,
                attach_intent_status_to_plan,
            )

            plan = SemanticExecutionPlan.from_dict(mission.semantic_execution_plan or {})
            attach_intent_status_to_plan(plan, mission=mission)
            mission.semantic_execution_plan = plan.to_dict()
            model = _load_model(mission)
            if model:
                model.contract = _contract_from_sep(mission.semantic_execution_plan or {})
                _save_model(mission, model)
        except Exception as exc:
            log.debug("[4lom] persist contract refresh: %s", exc)
    return validate_four_layer_invariants(mission)


def machine_steps_for_runtime(mission: Mission) -> List[MachineExecutionStep]:
    """Pasos activos para ejecución (capa Machine, sin desactivados)."""
    model = ensure_four_layer_model(mission)
    return [ms for ms in model.machine_execution.steps if not ms.disabled]


def human_view_steps_for_display(mission: Mission) -> List[HumanViewStep]:
    """Pasos visibles en Mission Review (incluye desactivados, excluye hidden)."""
    model = ensure_four_layer_model(mission)
    return [hv for hv in model.human_view.steps if not hv.hidden]


def expert_lineage_mappings(mission: Mission) -> List[str]:
    """Human View → Machine → SEP → COI (vista experto)."""
    model = ensure_four_layer_model(mission)
    sep = mission.semantic_execution_plan or {}
    sep_by_id = {
        str(s.get("id") or ""): s
        for s in (sep.get("steps") or [])
        if isinstance(s, dict)
    }
    lines: List[str] = ["4LOM lineage mappings:"]
    try:
        from app.services.missions.operational_truth_lineage import (
            trace_operational_step_lineage,
        )

        lineages = {ln.sep_step_id: ln for ln in trace_operational_step_lineage(mission)}
    except Exception:
        lineages = {}

    for hv in sorted(model.human_view.steps, key=lambda x: x.display_index):
        ms = next(
            (m for m in model.machine_execution.steps if m.step_id == hv.machine_step_id),
            None,
        )
        sp = sep_by_id.get(hv.machine_step_id) or {}
        ln = lineages.get(hv.machine_step_id)
        coi_id = ""
        if ln:
            coi_id = ln.coi_intent_id or "—"
        elif isinstance(sp.get("params"), dict):
            coi_raw = (sp.get("params") or {}).get("canonical_operational_intent")
            if isinstance(coi_raw, dict):
                coi_id = str(coi_raw.get("intent_id") or "—")
        flag = "OFF" if hv.disabled or (ms and ms.disabled) else "ON"
        lines.append(
            f"  [{flag}] HV «{hv.human_label[:50]}» → "
            f"M {ms.semantic_type if ms else '?'} ({hv.machine_step_id}) → "
            f"SEP → COI {coi_id or '—'}",
        )
    return lines


_EDITABLE_PARAM_KEYS: Dict[str, Tuple[str, ...]] = {
    "search_content": ("query", "timeout_ms"),
    "search_site": ("query", "site", "timeout_ms"),
    "search_youtube": ("query",),
    "open_site": ("url", "timeout_ms"),
    "open_url": ("url",),
    "select_entity_from_collection": (
        "selection_label",
        "profile_name",
        "timeout_ms",
    ),
    "select_profile": ("profile_name", "timeout_ms"),
    "open_app": ("name", "timeout_ms"),
}


def editable_param_keys_for_step(mission: Mission, machine_step_id: str) -> List[str]:
    model = ensure_four_layer_model(mission)
    ms = next((m for m in model.machine_execution.steps if m.step_id == machine_step_id), None)
    if ms is None:
        return []
    keys = list(_EDITABLE_PARAM_KEYS.get(ms.semantic_type, ()))
    for generic in ("timeout_ms", "retry_timeout_ms"):
        if generic not in keys:
            keys.append(generic)
    return keys


def sync_four_layer_after_sep_update(mission: Mission) -> FourLayerOperationalModel:
    """Tras actualizar SEP (promoción/OTP), resincroniza machine+contract, preserva goal+human."""
    model = build_four_layer_model(
        mission,
        preserve_goal=True,
        preserve_human_edits=True,
    )
    _save_model(mission, model)
    project_human_view_to_sep(mission)
    return model


__all__ = [
    "apply_goal_purpose_edit",
    "apply_human_view_label_edit",
    "apply_human_view_reorder",
    "apply_machine_layer_reorder",
    "apply_machine_step_edit",
    "apply_step_disabled",
    "build_four_layer_model",
    "editable_param_keys_for_step",
    "ensure_four_layer_model",
    "expert_lineage_mappings",
    "four_layer_expert_lines",
    "human_view_steps_for_display",
    "machine_steps_for_runtime",
    "persist_four_layer_mission",
    "project_human_view_to_sep",
    "project_machine_layer_to_sep",
    "semantic_type_phase",
    "sync_four_layer_after_sep_update",
    "validate_four_layer_invariants",
    "validate_reorder_dependencies",
]
