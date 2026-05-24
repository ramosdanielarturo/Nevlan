"""
Nevlan — Mission Review Confirmation API (PRD 2026-05-08)
==========================================================

Cuando el ``intent_collapse_engine`` no logra extraer el ``profile_name``
desde el raw_trace o detecta una query con conector faltante, deja el
plan en ``NEEDS_REVIEW`` con blockers ``EMPTY_PROFILE_NAME_IN_SELECT_PROFILE``
o ``QUERY_FIDELITY_SUSPECT``. La regla de producto (PRD §D) es:

    Nevlan no debe pedir REGRABAR.
    Nevlan debe pedir CONFIRMAR el dato faltante.

Este módulo expone los dos verbos que Mission Review llama cuando el
usuario confirma:

  * :func:`confirm_profile` — el usuario escribió/confirmó el nombre
    del perfil. Persistimos el alias en el alias store para que la
    PRÓXIMA grabación NO vuelva a preguntar, y recomputamos
    ``intent_status`` (que debería pasar a ``READY`` si era el único
    blocker).

  * :func:`confirm_query` — el usuario aceptó la sugerencia heurística
    o escribió la query corregida. Actualizamos ``query`` +
    ``query_fidelity_status="ok"`` y recomputamos.

Ambos son **idempotentes**: llamar dos veces con el mismo dato no
introduce drift. Devuelven el plan actualizado para que el caller (UI
o test) pueda inspeccionar el resultado.

Diseño deliberado
-----------------

* **Sin Qt.** El módulo es pure-Python para que pueda llamarse desde
  tests y desde la UI sin acoplamientos.
* **Side effect localizado.** Solo muta ``mission.semantic_execution_plan``
  + ``mission.review_confirmations`` + alias store. NUNCA toca
  ``raw_trace``, ``compiled_execution_graph`` ni ``user_annotations``.
* **Audit trail estructurado.** Cada confirmación añade una entrada
  a ``mission.review_confirmations`` (modelo ``ReviewConfirmation``)
  con ``field``, ``value``, ``source``, ``original_value``, etc. Esto
  permite saber LEYENDO el JSON de la misión que:

      1. La grabación original venía incompleta.
      2. El usuario confirmó X dato.
      3. Después de confirmación, la misión quedó READY.

  Ese campo es la pieza forense crítica — sin él, un JSON con
  ``intent_status="READY"`` sería engañoso.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import (
    Mission,
    ReviewConfirmation,
    RerecordHistoryEntry,
)
from app.core.logger import log
from app.services.missions.profile_resolver import (
    is_generic_profile_label,
    learn_profile_alias,
    resolve_canonical_profile_name,
)
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_intent_status_to_plan,
)


SOURCE_USER = "user_confirmation"
SOURCE_ALIAS_STORE = "alias_store"
SOURCE_HEURISTIC_ACCEPTED = "heuristic_suggestion_accepted"
SOURCE_USER_ALIAS_PROMOTED = "user_confirmation_alias_promoted"


def _step_matches_search_query_confirmation(sp: SemanticPlanStep) -> bool:
    """Pasos cuya *query* puede confirmarse desde Mission Review."""
    t = sp.type
    params = sp.params or {}
    if t == "search_youtube":
        return True
    if t == "search_content":
        prov = str(params.get("provider") or "").strip().lower()
        site = str(params.get("site") or "").strip().lower()
        return prov == "youtube" or site == "youtube"
    if t == "search_site":
        return str(params.get("site") or "").strip().lower() == "youtube"
    return False


def _load_plan(mission: Mission) -> Optional[SemanticExecutionPlan]:
    blob = mission.semantic_execution_plan
    if not isinstance(blob, dict) or not blob.get("steps"):
        return None
    try:
        return SemanticExecutionPlan.from_dict(blob)
    except Exception as e:
        log.warning(f"[mission_review] plan corrupto: {e}")
        return None


def _save_plan(mission: Mission, plan: SemanticExecutionPlan) -> None:
    """Persiste el plan + recomputa ``intent_status`` + sincroniza
    los flags de ejecución de la misión.
    """
    mission.semantic_execution_plan = plan.to_dict()
    attach_intent_status_to_plan(plan, mission=mission)
    mission.semantic_execution_plan = plan.to_dict()
    try:
        from app.services.missions.four_layer_operational_model import (
            sync_four_layer_after_sep_update,
        )

        sync_four_layer_after_sep_update(mission)
    except Exception as exc:
        log.debug("[mission_review] four-layer sync: %s", exc)


def _add_audit_tag(mission: Mission, kind: str, value: str) -> None:
    """Añade ``review_confirmed:<kind>:<value>`` a ``mission.tags``.

    Mantenido por compatibilidad con consumidores que listan tags;
    el audit forense de verdad vive en ``review_confirmations``.
    """
    tag = f"review_confirmed:{kind}:{value}"
    if tag in mission.tags:
        return
    mission.tags = list(mission.tags) + [tag]


def _add_review_confirmation(
    mission: Mission,
    *,
    field: str,
    value: str,
    source: str,
    step_id: Optional[str] = None,
    step_type: Optional[str] = None,
    original_value: Optional[str] = None,
    note: str = "",
) -> None:
    """Registra una entrada estructurada en ``review_confirmations``.

    Idempotente respecto a ``(field, value, step_id)`` — si la
    misma confirmación ya existe, no se duplica. Si cambia el
    ``value``, se reemplaza la entrada anterior (no acumula
    historia parcial; el usuario aprobó esto, no lo otro).
    """
    confirmations = list(mission.review_confirmations or [])
    new_entry = ReviewConfirmation(
        field=field,
        value=value,
        source=source,
        step_id=step_id,
        step_type=step_type,
        original_value=original_value,
        note=note,
    )
    out = []
    replaced = False
    for c in confirmations:
        same_target = (c.field == field) and (
            c.step_id == step_id or (not c.step_id and not step_id)
        )
        if same_target:
            if c.value == value and c.source == source:
                # Idempotente exacto — preservamos la entrada original.
                return
            replaced = True
            out.append(new_entry)
        else:
            out.append(c)
    if not replaced:
        out.append(new_entry)
    mission.review_confirmations = out


def confirm_profile(
    mission: Mission,
    *,
    profile_name: str,
    learn_alias: bool = True,
    source: str = SOURCE_USER,
) -> SemanticExecutionPlan:
    """El usuario confirmó el nombre del perfil en Mission Review.

    Reglas:

      * ``profile_name`` no puede ser vacío ni un label genérico
        ("perfil del navegador", "default", …) — levantamos
        ``ValueError`` para que la UI vuelva a pedirlo.
      * Persistimos el canonical + aliases en el alias store
        (próximas misiones se autorrepararán).
      * Actualizamos cada step ``select_profile`` del plan con el
        nuevo ``profile_name`` y limpiamos ``needs_user_label``.
      * Recomputamos ``intent_status`` — debe quedar en ``READY``
        si éste era el único blocker.

    Devuelve el plan resultante.
    """
    raw_user_input = (profile_name or "").strip()
    if not raw_user_input:
        raise ValueError("profile_name no puede ser vacío.")
    if is_generic_profile_label(raw_user_input):
        raise ValueError(
            f"'{raw_user_input}' es una etiqueta genérica, no un nombre real."
        )

    # Canonicalización (fix PRD 2026-05-09): si el usuario escribió
    # "Daniel" pero el alias_store ya conoce "Daniel Arturo Ramos",
    # promovemos al canonical más completo. Nunca persistimos el
    # alias corto cuando existe uno más rico.
    resolution = resolve_canonical_profile_name(raw_user_input)
    canonical = (resolution.get("canonical") or "").strip()
    if not canonical:
        raise ValueError(
            f"'{raw_user_input}' no es un nombre de perfil válido."
        )
    promoted = bool(resolution.get("promoted"))
    if promoted and (resolution.get("source") in ("alias_store",)):
        effective_source = SOURCE_USER_ALIAS_PROMOTED
    else:
        effective_source = source

    name = canonical

    plan = _load_plan(mission)
    if plan is None:
        raise ValueError(
            "La misión no tiene semantic_execution_plan; "
            "no hay nada que confirmar."
        )

    candidate_aliases = []
    touched = 0
    for sp in plan.steps:
        if sp.type != "select_profile":
            continue
        # Recolectamos cualquier candidato visto previamente para
        # convertirlo en alias permanente.
        for c in (sp.params.get("candidate_aliases") or []):
            if c and c not in candidate_aliases:
                candidate_aliases.append(c)
        prior_step_value = (sp.params.get("profile_name")
                            or sp.params.get("profile") or "")
        sp.params["profile_name"] = name
        sp.params["app"] = (sp.params.get("app") or "chrome").strip() or "chrome"
        sp.params["profile_confirmed_via"] = "mission_review"
        # El step ya no necesita confirmación humana.
        sp.needs_user_label = False
        sp.label_prompt = ""
        # Subimos confianza — ahora el dato viene del usuario.
        sp.confidence = max(float(sp.confidence or 0.0), 0.95)
        # Mejoramos el label humano para Mission Review:
        #   antes: "Seleccionar perfil"
        #   ahora: "Usar el perfil Daniel Arturo Ramos"
        sp.human_label = f"Usar el perfil {name}"
        try:
            from app.services.missions.four_layer_operational_model import (
                apply_human_view_label_edit,
            )

            apply_human_view_label_edit(
                mission,
                sp.id,
                human_label=sp.human_label,
                confirmed=True,
                needs_review=False,
            )
        except Exception as exc:
            log.debug("[mission_review] human view profile: %s", exc)
        # original_value: si hubo promoción alias_store, preferimos
        # capturar el valor que el USUARIO escribió ("Daniel") porque
        # esa es la pieza forense interesante. Si no hubo promoción,
        # mantenemos el valor que ya estaba en el step (legacy).
        if promoted:
            original_for_audit = raw_user_input
        else:
            original_for_audit = prior_step_value or raw_user_input
        # Audit trail estructurado por step.
        _add_review_confirmation(
            mission,
            field="profile_name",
            value=name,
            source=effective_source,
            step_id=sp.id,
            step_type=sp.type,
            original_value=(original_for_audit or None),
            note=(
                "raw_trace no contenía evidencia robusta del perfil; "
                "Mission Review aplicó la confirmación humana."
                + (" Promovido vía alias_store." if promoted else "")
            ),
        )
        touched += 1

    if learn_alias:
        try:
            # Pasamos también el valor crudo del usuario como alias;
            # learn_profile_alias respeta la regla anti-degradación,
            # así que aunque el usuario haya escrito "Daniel" el
            # store NO se va a contaminar con "daniel": "Daniel".
            extra_aliases = list(candidate_aliases)
            if raw_user_input and raw_user_input not in extra_aliases:
                extra_aliases.append(raw_user_input)
            learn_profile_alias(name, aliases=extra_aliases)
        except Exception as e:
            log.warning(f"[mission_review] no pude persistir alias: {e}")

    _add_audit_tag(mission, "profile", name)
    _save_plan(mission, plan)
    log.info(
        f"[mission_review] profile_confirmed={name!r} "
        f"steps_updated={touched} source={effective_source} "
        f"promoted={promoted} original={raw_user_input!r}"
    )
    return plan


def confirm_query(
    mission: Mission,
    *,
    query: str,
    step_id: Optional[str] = None,
    source: str = SOURCE_USER,
) -> SemanticExecutionPlan:
    """El usuario confirmó (o corrigió) la query de búsqueda.

    Reglas:

      * ``query`` no puede ser vacía.
      * Aplica al paso de búsqueda YouTube (``search_youtube`` legacy,
        ``search_content`` con ``provider``/``site`` youtube, o
        ``search_site`` con ``site=youtube``) con ``id == step_id``;
        si ``step_id`` es ``None``, aplica a todos los pasos de ese
        tipo en el plan.
      * Marca ``query_fidelity_status="ok"`` y limpia
        ``needs_user_label``.
      * Recomputamos ``intent_status``.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("query no puede ser vacía.")

    plan = _load_plan(mission)
    if plan is None:
        raise ValueError(
            "La misión no tiene semantic_execution_plan; "
            "no hay nada que confirmar."
        )

    touched = 0
    for sp in plan.steps:
        if not _step_matches_search_query_confirmation(sp):
            continue
        if step_id and sp.id != step_id:
            continue
        original_value = sp.params.get("query") or ""
        suggested = sp.params.get("suggested_query") or ""
        # Si el usuario aceptó textualmente la sugerencia heurística,
        # registramos esa procedencia en la entrada (es información
        # útil para análisis futuro: ¿qué tan buenas son nuestras
        # sugerencias?).
        effective_source = source
        if (source == SOURCE_USER
                and suggested
                and q.strip() == suggested.strip()):
            effective_source = SOURCE_HEURISTIC_ACCEPTED
        sp.params["query"] = q
        sp.params["query_fidelity_status"] = "ok"
        sp.params.pop("suggested_query", None)
        sp.params.pop("suspected_missing_connector", None)
        sp.params["query_confirmed_via"] = "mission_review"
        sp.needs_user_label = False
        sp.label_prompt = ""
        sp.confidence = max(float(sp.confidence or 0.0), 0.95)
        sp.human_label = f"Buscar en YouTube: {q}"
        _add_review_confirmation(
            mission,
            field="query",
            value=q,
            source=effective_source,
            step_id=sp.id,
            step_type=sp.type,
            original_value=(original_value or None),
            note=(
                "query original tenía query_fidelity_status="
                "'suspect'; Mission Review aplicó la corrección."
            ),
        )
        touched += 1

    _add_audit_tag(mission, "query", q)
    _save_plan(mission, plan)
    log.info(
        f"[mission_review] query_confirmed={q!r} "
        f"steps_updated={touched} source={source}"
    )
    return plan


SOURCE_RERECORD = "rerecord"


def _build_replacement_plan_step(
    new_step_evidence: List[Dict[str, Any]],
    *,
    base_id: str,
) -> Optional[Tuple[SemanticPlanStep, Any, Any]]:
    """Compila la evidencia capturada y produce el ``SemanticPlanStep``
    que reemplazará al step original.

    Camino:

      1. Convertimos la lista de RawEvents (en formato dict) a
         ``RawEvent`` mediante ``Mission.model_validate`` sobre una
         misión temporal — preserva la validación pydantic original.
      2. ``MissionCompiler.compile_mission`` produce
         ``compiled_execution_graph`` + ``interpreted_steps``.
      3. ``attach_semantic_plan_to_mission`` produce el plan
         semántico.
      4. Tomamos el primer step del plan resultante. Si la evidencia
         compiló a varios steps (caso poco común), usamos el primero
         y dejamos audit en log — el contrato pide "regrabar UN
         paso, no varios".

    Devuelve ``None`` si no se pudo extraer un step válido.
    Devuelve también el primer ``CompiledStep`` y ``InterpretedStep``
    para que la UI Mission Review pueda sincronizar sus listas
    (``compiled_execution_graph`` y ``interpreted_steps`` se usan
    para renderizar las tarjetas).
    """
    if not new_step_evidence:
        return None

    from app.contracts.mission import (
        Mission as _Mission,
        MissionStatus as _MissionStatus,
        RawEvent as _RawEvent,
    )
    from app.services.missions.compiler import MissionCompiler
    from app.services.missions.semantic_execution_plan import (
        attach_semantic_plan_to_mission,
    )

    # Validamos los dicts a RawEvent para que el compiler los
    # consuma como espera. Si vienen ya como RawEvent, model_validate
    # los pasa transparentemente.
    raw_events: List[_RawEvent] = []
    for ev in new_step_evidence:
        if isinstance(ev, _RawEvent):
            raw_events.append(ev)
            continue
        try:
            raw_events.append(_RawEvent.model_validate(ev))
        except Exception as e:
            log.debug(f"[rerecord] evento crudo inválido descartado: {e}")
            continue
    if not raw_events:
        return None

    temp = _Mission(
        name=f"_rerecord_temp_{base_id}",
        status=_MissionStatus.RECORDING,
    )
    temp.raw_trace = raw_events
    try:
        temp = MissionCompiler.compile_mission(temp)
    except Exception as e:
        log.warning(f"[rerecord] compile_mission falló: {e}")
        return None

    if not temp.compiled_execution_graph:
        log.info("[rerecord] evidencia no compiló a ningún step")
        return None

    try:
        plan = attach_semantic_plan_to_mission(temp, force=True)
    except Exception as e:
        log.warning(f"[rerecord] attach_semantic_plan falló: {e}")
        return None

    if not plan.steps:
        return None

    if len(plan.steps) > 1:
        log.warning(
            f"[rerecord] evidencia compiló a {len(plan.steps)} steps; "
            f"uso solo el primero (regrabamos UN paso)"
        )

    new_sp = plan.steps[0]
    new_compiled = (temp.compiled_execution_graph or [None])[0]
    new_interpreted = (temp.interpreted_steps or [None])[0]
    return new_sp, new_compiled, new_interpreted


def _detect_affected_steps(
    plan: SemanticExecutionPlan,
    *,
    target_step_id: str,
) -> List[str]:
    """Devuelve los ``id`` de los pasos cuyas dependencias declaradas
    apuntaban al ``target_step_id`` (PRD §D.5).

    El plan canónico actual no usa ``params.depends_on`` de forma
    sistemática; este helper deja la puerta abierta para cuando
    aparezca. Hoy retorna lista vacía salvo que un step posterior
    declare explícitamente la dependencia.
    """
    out: List[str] = []
    for sp in plan.steps:
        deps = sp.params.get("depends_on") or []
        if not isinstance(deps, (list, tuple)):
            deps = [deps]
        if target_step_id and target_step_id in {str(d) for d in deps}:
            out.append(sp.id)
    return out


def rerecord_step(
    mission: Mission,
    *,
    step_index: int,
    new_step_evidence: List[Dict[str, Any]],
    rerecorded_at: datetime,
    user_action: str = "rerecord",
    rerecord_session_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    previous_steps_replayed: Optional[List[str]] = None,
    original_step_snapshot: Optional[Dict[str, Any]] = None,
) -> Tuple[SemanticExecutionPlan, Dict[str, Any], List[str]]:
    """Reemplaza el step ``step_index`` con la nueva evidencia.

    Returns:
        ``(plan, replacement_step_snapshot, affected_steps)``

    Reglas (idénticas al contrato ``docs/plan_regrabacion_paso_replay.md``):

      * **No mutar** ``raw_trace``. La evidencia nueva queda
        preservada dentro de ``mission.rerecord_history[i].replacement_evidence``.
      * **No mutar** pasos previos ni posteriores en posición ni
        contenido — solo el step en ``step_index``.
      * **Recompilar** el plan: tras reemplazar, llamamos a
        ``attach_intent_status_to_plan`` para refrescar
        ``intent_status`` + ``not_ready_reasons``.
      * **Audit trail estructurado**: añade entrada a
        ``rerecord_history`` y entrada análoga a
        ``review_confirmations`` con ``field="step_replacement"``.
      * **Idempotente** respecto a ``rerecord_session_id``: una
        segunda llamada con el mismo session_id no duplica entradas.
    """
    plan = _load_plan(mission)
    if plan is None:
        raise ValueError(
            "La misión no tiene semantic_execution_plan; no hay nada que "
            "regrabar (PRD §A.1)."
        )
    if step_index < 0 or step_index >= len(plan.steps):
        raise ValueError(
            f"step_index={step_index} fuera de rango "
            f"(0..{len(plan.steps) - 1})."
        )
    if not new_step_evidence:
        raise ValueError("new_step_evidence vacía: nada que aplicar.")

    session_id = (rerecord_session_id or "").strip() or "rerec-anon"
    # Idempotencia: si ya existe una entrada en rerecord_history con
    # este session_id y status="saved", retornamos el snapshot ya
    # registrado sin mutar.
    for entry in (mission.rerecord_history or []):
        if (entry.rerecord_session_id == session_id
                and entry.status == "saved"):
            log.info(
                f"[rerecord] idempotente: session={session_id} ya guardada"
            )
            return (
                plan,
                dict(entry.replacement_step_snapshot or {}),
                list(entry.affected_steps or []),
            )

    original_step = plan.steps[step_index]
    original_id = original_step.id
    original_snapshot = (
        original_step_snapshot
        if original_step_snapshot is not None
        else original_step.to_dict()
    )

    built = _build_replacement_plan_step(
        new_step_evidence,
        base_id=session_id,
    )
    if built is None:
        raise ValueError(
            "La nueva captura no produjo un paso válido. "
            "Revisa la evidencia o vuelve a intentar."
        )
    new_sp, new_compiled, new_interpreted = built

    # Conservamos el id original del paso para no romper referencias
    # de UI (botones por id, etc.). Nadie debería depender del id
    # interno, pero por seguridad reusamos el original. La
    # trazabilidad al "anterior valor" vive en
    # rerecord_history[].replacement_step_snapshot.id_alias.
    new_sp.id = original_id
    new_sp.params["_original_step_id"] = original_id
    new_sp.params["_rerecord_session_id"] = session_id
    new_sp.params["_rerecorded_at"] = rerecorded_at.isoformat()
    new_sp.params["_user_action"] = str(user_action or "rerecord")
    # block_id se preserva si no vino del compile temporal:
    if not new_sp.block_id and original_step.block_id:
        new_sp.block_id = original_step.block_id

    # Reemplazo quirúrgico: solo la posición target.
    new_steps_list = list(plan.steps)
    new_steps_list[step_index] = new_sp
    plan.steps = new_steps_list

    # Sincronizamos compiled_execution_graph + interpreted_steps en
    # la posición correspondiente. Esto preserva la coherencia con
    # la UI Mission Review (que renderiza tarjetas desde compiled).
    # ``compiled_execution_graph`` sigue siendo "debug-only" según
    # la regla §A.2; aquí solo lo mantenemos consistente para que
    # un usuario que abra Mission Review tras una regrabación vea
    # el paso nuevo y no el anterior.
    if new_compiled is not None and 0 <= step_index < len(mission.compiled_execution_graph):
        try:
            mission.compiled_execution_graph[step_index] = new_compiled
        except Exception as e:
            log.debug(f"[rerecord] no pude sincronizar compiled[{step_index}]: {e}")
    if new_interpreted is not None and 0 <= step_index < len(mission.interpreted_steps):
        try:
            mission.interpreted_steps[step_index] = new_interpreted
        except Exception as e:
            log.debug(f"[rerecord] no pude sincronizar interpreted[{step_index}]: {e}")

    # Recálculo del average_confidence (el campo más visible).
    if plan.steps:
        plan.average_confidence = round(
            sum(float(s.confidence) for s in plan.steps) / float(len(plan.steps)),
            4,
        )

    # Affected steps (declarativos vía params.depends_on).
    affected = _detect_affected_steps(plan, target_step_id=original_id)

    replacement_snapshot = new_sp.to_dict()

    # Audit trail (rerecord_history) — idempotente por session_id.
    history_entry = RerecordHistoryEntry(
        rerecord_session_id=session_id,
        target_step_id=original_id,
        target_step_index=int(step_index),
        original_step_snapshot=dict(original_snapshot),
        replacement_step_snapshot=dict(replacement_snapshot),
        replacement_evidence=[
            ev if isinstance(ev, dict)
            else (ev.model_dump(mode="json")
                  if hasattr(ev, "model_dump")
                  else dict(ev))
            for ev in new_step_evidence
        ],
        previous_steps_replayed=list(previous_steps_replayed or []),
        affected_steps=list(affected),
        started_at=(started_at or rerecorded_at),
        saved_at=rerecorded_at,
        source=SOURCE_RERECORD,
        status="saved",
        note=(
            f"rerecord; affected_steps={list(affected)!r}"
        ),
    )
    history = list(mission.rerecord_history or [])
    history = [
        e for e in history if e.rerecord_session_id != session_id
    ]
    history.append(history_entry)
    mission.rerecord_history = history

    # Audit trail (review_confirmations) — espejo a nivel de campo.
    _add_review_confirmation(
        mission,
        field="step_replacement",
        value=str(new_sp.id),
        source=SOURCE_RERECORD,
        step_id=str(new_sp.id),
        step_type=str(new_sp.type),
        original_value=str(original_id),
        note=(
            f"Mission Review aplicó la regrabación; "
            f"affected_steps={list(affected)!r}"
        ),
    )
    _add_audit_tag(mission, "rerecord", str(new_sp.id))

    _save_plan(mission, plan)
    log.info(
        f"[mission_review] step_rerecorded session={session_id} "
        f"target_idx={step_index} type={new_sp.type!r} "
        f"affected={len(affected)}"
    )
    return plan, dict(replacement_snapshot), list(affected)


__all__ = [
    "confirm_profile",
    "confirm_query",
    "rerecord_step",
    "SOURCE_USER",
    "SOURCE_ALIAS_STORE",
    "SOURCE_HEURISTIC_ACCEPTED",
    "SOURCE_USER_ALIAS_PROMOTED",
    "SOURCE_RERECORD",
]
