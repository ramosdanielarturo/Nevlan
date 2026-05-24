"""
Nevlan — Mission Review presentation helpers (PRD 2026-05-08 rev §4)
=====================================================================

Política visual:

* **Vista normal**: limpio, una sola sección con la lista de pasos
  legibles + estado. Sin tecnicismos. El usuario común no debe
  enterarse de que hubo confirmaciones humanas — solo le importa
  que la misión esté lista.

* **Vista experto**: además, una sección "Confirmado por usuario"
  con la lista de campos que llegaron por intervención humana, su
  valor y de dónde vino la confirmación. Esto es trazabilidad para
  el usuario power, debugging y auditoría.

Internamente, READY siempre es READY. La diferencia es solo de
presentación. El campo ``semantic_execution_plan.ready_provenance``
es la fuente de verdad:

    "raw_evidence"      → captura limpia, sin humano en el loop
    "user_confirmation" → llegó a READY porque hubo Mission Review
    "alias_store"       → completado con datos aprendidos antes
    "unknown"           → no está READY todavía

Este módulo no depende de Qt — produce dataclasses que la UI
serializa como mejor le convenga (Qt, web, CLI, JSON, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from app.contracts.mission import Mission, ReviewConfirmation
from app.services.missions.execution_truth_engine import (
    should_suppress_legacy_capture_noise,
)
from app.services.missions.semantic_execution_plan import (
    READY_PROVENANCE_ALIAS,
    READY_PROVENANCE_RAW,
    READY_PROVENANCE_UNKNOWN,
    READY_PROVENANCE_USER,
    SemanticExecutionPlan,
    attach_intent_status_to_plan,
)
from app.services.missions.semantic_ambiguity_engine import (
    analyze_semantic_step_dict,
)
from app.services.missions.semantic_review_display import (
    is_semantic_intent_promotion_plan,
)
from app.services.runtime.entity_resolution_layer import (
    entity_status_label_for_mission,
    extract_entity_from_semantic_step,
    mission_has_strong_operational_entities,
    sanitize_human_label_for_normal_view,
)


# ─────────────────────────────────────────────────────────────────
# Etiquetas humanas (i18n trivial — castellano por ahora)
# ─────────────────────────────────────────────────────────────────


_FIELD_LABELS = {
    "profile_name": "Perfil",
    "query": "Búsqueda",
    "url": "URL",
    "app": "Aplicación",
}


_SOURCE_LABELS = {
    "user_confirmation": "Confirmado por el usuario",
    "heuristic_suggestion_accepted": "El usuario aceptó la sugerencia",
    "alias_store": "Aplicado automáticamente desde un run anterior",
}


def _semantic_type_fallback_normal_review(sp: dict) -> str:
    """Evita mostrar tokens legacy ``search_youtube`` en vista normal."""
    t = str(sp.get("type") or "").strip()
    if t == "search_youtube":
        return "search_content"
    return t


# Vista normal: limpia, igual para todas las procedencias READY.
# El usuario común no se entera de que hubo confirmación humana.
_PROVENANCE_LABELS_NORMAL = {
    READY_PROVENANCE_RAW: "Lista para ejecutar",
    READY_PROVENANCE_USER: "Lista para ejecutar",
    READY_PROVENANCE_ALIAS: "Lista para ejecutar",
    READY_PROVENANCE_UNKNOWN: "Necesita aclaración",
}

# Vista experto: muestra la procedencia entre paréntesis para
# trazabilidad explícita.
_PROVENANCE_LABELS_EXPERT = {
    READY_PROVENANCE_RAW: (
        "Lista para ejecutar"
    ),
    READY_PROVENANCE_USER: (
        "Lista para ejecutar (con confirmación del usuario)"
    ),
    READY_PROVENANCE_ALIAS: (
        "Lista para ejecutar (datos recuperados del historial)"
    ),
    READY_PROVENANCE_UNKNOWN: (
        "Necesita aclaración"
    ),
}


_PROVENANCE_EXPERT_NOTE = {
    READY_PROVENANCE_RAW: (
        "READY directo desde la grabación, sin intervención humana."
    ),
    READY_PROVENANCE_USER: (
        "READY por confirmación humana, no por captura perfecta."
    ),
    READY_PROVENANCE_ALIAS: (
        "READY con datos aprendidos de runs anteriores; "
        "la grabación nueva no traía evidencia suficiente."
    ),
    READY_PROVENANCE_UNKNOWN: (
        "El plan aún no está READY — revisar blockers."
    ),
}


# ─────────────────────────────────────────────────────────────────
# Modelos de presentación
# ─────────────────────────────────────────────────────────────────


@dataclass
class ConfirmationLine:
    """Una fila para el panel "Confirmado por usuario" (modo experto)."""
    field_label: str            # "Perfil", "Búsqueda"
    field_key: str              # "profile_name", "query"
    value: str                  # "Daniel Arturo Ramos"
    source_label: str           # "Confirmado por el usuario"
    source_key: str             # "user_confirmation"
    original_value: Optional[str] = None  # lo que el ICE había puesto
    note: str = ""

    @classmethod
    def from_record(cls, c: ReviewConfirmation) -> "ConfirmationLine":
        return cls(
            field_label=_FIELD_LABELS.get(c.field, c.field.replace("_", " ").title()),
            field_key=c.field,
            value=c.value,
            source_label=_SOURCE_LABELS.get(c.source, c.source),
            source_key=c.source,
            original_value=(c.original_value or None),
            note=(c.note or ""),
        )


@dataclass
class StepLine:
    """Una fila para la vista normal (lista numerada)."""
    index: int                  # 1-based
    human_label: str            # "Usar el perfil Daniel Arturo Ramos"
    machine_step_id: str = ""   # enlace 4LOM → Machine Layer
    confirmed: bool = False     # True si este step tuvo confirmación humana
    needs_review: bool = False  # True si aún requiere atención humana
    disabled: bool = False      # paso desactivado (runtime lo omite)
    # PRD §Fase 2C: badges premium para la vista normal. Lista de
    # strings cortos (cada uno con emoji + label) que la UI pinta
    # como chips/píldoras al lado del step.
    badges: List[str] = field(default_factory=list)


@dataclass
class MissionReviewSummary:
    """Estructura única que la UI consume.

    La vista normal usa ``status_label`` + ``steps`` y ``visible_blockers``.
    El modo experto usa ``status_label_expert`` + ``confirmations``
    + ``expert_note`` + ``contamination_details``.
    """
    status_label: str               # "Lista para ejecutar" (normal)
    status_label_expert: str        # "...(con confirmación del usuario)"
    status_ready: bool              # True ↔ intent_status READY o EXECUTABLE
    steps: List[StepLine] = field(default_factory=list)
    confirmations: List[ConfirmationLine] = field(default_factory=list)
    # Lista cruda de blockers (compatibilidad). Tests / UI experta los
    # usan; la vista normal debe consultar ``visible_blockers``.
    blockers: List[str] = field(default_factory=list)
    # Blockers REALES que el usuario común debe ver/arreglar. Excluye
    # códigos puramente técnicos / debug-only y nunca queda con
    # entradas que no apuntan a un step visible.
    visible_blockers: List[str] = field(default_factory=list)
    # Detalles de contaminación auditables (vista experto). Cada entrada
    # incluye ``affects_execution`` para distinguir "afecta ejecución"
    # de "solo debug".
    contamination_details: List[dict] = field(default_factory=list)
    # Audit trail de regrabación de pasos (PRD 2026-05-08c §H + §K).
    # Vista normal: oculto. Vista experto: lista cronológica de qué
    # pasos fueron regrabados, cuándo, qué se reemplazó y qué pasos
    # posteriores se vieron afectados (si los hay).
    rerecord_history: List[dict] = field(default_factory=list)
    # OTP — Truth Preservation (vista experto).
    truth_preservation_lines: List[str] = field(default_factory=list)
    # Ruido humano descartado (PRD 2026-05-08c §F). Lista de hallazgos
    # tipo ``address_bar_click_no_submit`` / ``typed_then_deleted`` /
    # ``noise_before_bookmark``. Vista normal: NO se muestra. Vista
    # experto: aparece como sección "Ruido descartado".
    ignored_noise: List[dict] = field(default_factory=list)
    provenance_key: str = READY_PROVENANCE_UNKNOWN
    expert_note: str = ""


# ─────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────


def filter_badges_for_normal_semantic_view(
    badges: List[str],
    *,
    mission: Optional[Mission] = None,
) -> List[str]:
    """Quita ruido legacy/visual cuando la vista normal es SIPE-first.

    Reglas producto: sin «referencia visual» ni lecturas que suenen a
    capture roto cuando el ejecutable ya es ``open_*`` / ``search_*``.

    Con Trusted Pre-Action Identity además ocultamos zero-capture /
    visual insuficiente cuando la misión tiene evidencia PRE-acción fuerte.
    """
    trusted = (
        mission is not None
        and should_suppress_legacy_capture_noise(mission)
    )
    out: List[str] = []
    for b in badges or []:
        bl = str(b).lower()
        if "visual" in bl and ("insuficiente" in bl or "0" in b or "📸" in b):
            continue
        if "zero-capture" in bl.lower() or "zero capture" in bl.lower():
            continue
        if trusted and b in (BADGE_ZERO_CAPTURE, BADGE_VISUAL_INSUFFICIENT):
            continue
        out.append(b)
    return out


def _build_step_lines_from_four_layer(
    mission: Mission,
    *,
    sep_steps_by_id: dict,
    confirmed_step_ids: set,
    is_sipe: bool,
) -> List[StepLine]:
    """Vista normal desde capa Human View (4LOM), sin tocar machine params."""
    if not (mission.semantic_execution_plan or {}).get("steps"):
        return []
    try:
        from app.services.missions.four_layer_operational_model import (
            ensure_four_layer_model,
            human_view_steps_for_display,
        )
        ensure_four_layer_model(mission)
        hv_steps = human_view_steps_for_display(mission)
        if not hv_steps:
            return []
        lines: List[StepLine] = []
        for hv in hv_steps:
            sp = sep_steps_by_id.get(hv.machine_step_id) or {}
            raw_badges = compute_step_badges(sp) if sp else []
            if is_sipe:
                raw_badges = filter_badges_for_normal_semantic_view(
                    raw_badges, mission=mission,
                )
            hl = sanitize_human_label_for_normal_view(hv.human_label)
            if not hl:
                continue
            lines.append(
                StepLine(
                    index=hv.display_index,
                    human_label=hl,
                    machine_step_id=hv.machine_step_id,
                    confirmed=(
                        hv.confirmed or hv.machine_step_id in confirmed_step_ids
                    ),
                    needs_review=hv.needs_review,
                    disabled=bool(hv.disabled),
                    badges=raw_badges,
                ),
            )
        return lines
    except Exception:
        return []


def build_mission_review_summary(
    mission: Mission,
    *,
    recompute: bool = True,
) -> MissionReviewSummary:
    """Construye la estructura presentacional desde la misión.

    PRD 2026-05-08c §A.4: por defecto **recomputa** ``intent_status``
    + ``not_ready_reasons`` antes de leerlos. Esto evita que el JSON
    persistido (que pudo guardarse con un código legacy más estricto)
    siga afirmando blockers que la lógica actual ya no considera.

    Args:
        mission: misión a presentar.
        recompute: si ``True`` (default), corre
            :func:`attach_intent_status_to_plan` antes de leer el plan.
            Tests que necesiten verificar el blob persistido pueden
            pasar ``False``.

    Diseño:

      * Si ``semantic_execution_plan`` no existe o está vacío,
        devuelve un summary "Pendiente" — la UI puede mostrar el
        flujo legacy o un placeholder.
      * ``StepLine.confirmed`` se enciende cuando el step_id de la
        confirmación coincide con el step (no al revés — múltiples
        confirmaciones sobre el mismo step se colapsan a un único
        flag).
      * ``confirmations`` deduplica por ``(field, step_id)`` —
        garantizamos UNA línea visible por confirmación lógica.
    """
    is_sipe = is_semantic_intent_promotion_plan(mission)
    preserved_review = None

    if recompute:
        sep_blob = mission.semantic_execution_plan or {}
        if is_sipe and sep_blob and sep_blob.get("steps"):
            try:
                from app.services.missions.operational_truth_lineage import (
                    compute_review_from_preserved_truth,
                )

                preserved_review = compute_review_from_preserved_truth(mission)
            except Exception:
                preserved_review = None
        elif sep_blob and sep_blob.get("steps"):
            try:
                plan = SemanticExecutionPlan.from_dict(sep_blob)
                attach_intent_status_to_plan(plan, mission=mission)
                mission.semantic_execution_plan = plan.to_dict()
            except Exception:
                pass

    sep = mission.semantic_execution_plan or {}
    intent_status = sep.get("intent_status", "")
    blockers = list(sep.get("not_ready_reasons") or [])
    provenance = sep.get("ready_provenance") or READY_PROVENANCE_UNKNOWN
    status_ready = intent_status in ("READY", "EXECUTABLE")
    if preserved_review is not None:
        intent_status = preserved_review.intent_status or intent_status
        blockers = list(preserved_review.blockers)
        status_ready = preserved_review.status_ready

    pcof_status = None
    try:
        from app.services.runtime.pre_click_operational_freeze import (
            pcof_status_label_for_mission,
        )

        pcof_status = pcof_status_label_for_mission(mission)
    except Exception:
        pcof_status = None

    erl_status = entity_status_label_for_mission(mission)
    uces_status = None
    try:
        from app.services.runtime.universal_collection_entity_engine import (
            collection_entity_status_label_for_mission,
            mission_has_strong_collection_selection,
        )
        uces_status = collection_entity_status_label_for_mission(mission)
    except Exception:
        uces_status = None
    status_label = _PROVENANCE_LABELS_NORMAL.get(
        provenance,
        _PROVENANCE_LABELS_NORMAL[READY_PROVENANCE_UNKNOWN],
    )
    if pcof_status and status_ready:
        status_label = pcof_status
    elif (
        uces_status
        and status_ready
        and mission_has_strong_collection_selection(mission)
    ):
        status_label = uces_status
    elif (
        erl_status
        and status_ready
        and mission_has_strong_operational_entities(mission)
    ):
        status_label = erl_status
    elif (
        uces_status
        and mission_has_strong_collection_selection(mission)
        and provenance == READY_PROVENANCE_UNKNOWN
    ):
        status_label = uces_status
    elif (
        erl_status
        and mission_has_strong_operational_entities(mission)
        and provenance == READY_PROVENANCE_UNKNOWN
    ):
        status_label = erl_status
    status_label_expert = _PROVENANCE_LABELS_EXPERT.get(
        provenance,
        _PROVENANCE_LABELS_EXPERT[READY_PROVENANCE_UNKNOWN],
    )
    expert_note = _PROVENANCE_EXPERT_NOTE.get(
        provenance, _PROVENANCE_EXPERT_NOTE[READY_PROVENANCE_UNKNOWN],
    )

    # Lista de steps para vista normal.
    confs = list(mission.review_confirmations or [])
    confirmed_step_ids = {
        c.step_id for c in confs if c.step_id
    }
    steps_out: List[StepLine] = []
    trusted_evidence = should_suppress_legacy_capture_noise(mission)
    sep_steps_by_id = {
        str(s.get("id") or ""): s
        for s in (sep.get("steps") or [])
        if isinstance(s, dict)
    }

    four_layer_steps = _build_step_lines_from_four_layer(
        mission,
        sep_steps_by_id=sep_steps_by_id,
        confirmed_step_ids=confirmed_step_ids,
        is_sipe=is_sipe,
    )
    if four_layer_steps:
        steps_out = four_layer_steps
    elif preserved_review is not None and preserved_review.steps:
        for prs in preserved_review.steps:
            sp = sep_steps_by_id.get(prs.sep_step_id) or {}
            raw_badges = compute_step_badges(sp) if sp else []
            if is_sipe:
                raw_badges = filter_badges_for_normal_semantic_view(
                    raw_badges, mission=mission,
                )
            steps_out.append(
                StepLine(
                    index=prs.index,
                    human_label=prs.human_label,
                    confirmed=(prs.sep_step_id in confirmed_step_ids),
                    needs_review=prs.needs_review,
                    badges=raw_badges,
                ),
            )
    else:
        for i, sp in enumerate(sep.get("steps") or []):
            raw_badges = compute_step_badges(sp)
            if is_sipe:
                raw_badges = filter_badges_for_normal_semantic_view(
                    raw_badges, mission=mission,
                )
            elif trusted_evidence:
                raw_badges = [
                    b for b in raw_badges
                    if b not in (BADGE_ZERO_CAPTURE, BADGE_VISUAL_INSUFFICIENT)
                ]
            sar = analyze_semantic_step_dict(sp, mission, step_index=i)
            ent = extract_entity_from_semantic_step(sp, mission)
            if ent and ent.confidence >= 0.88:
                needs_human = False
            else:
                needs_human = sar.requires_human_confirmation
            hl = str(
                sp.get("human_label") or _semantic_type_fallback_normal_review(sp) or "",
            )
            try:
                from app.services.missions.universal_operational_language import (
                    human_operational_label_for_review_step,
                    should_suppress_ask_for_mission_step,
                )

                uol_hl = human_operational_label_for_review_step(sp, mission)
                if uol_hl:
                    hl = uol_hl
                if should_suppress_ask_for_mission_step(sp, mission):
                    needs_human = False
            except Exception:
                pass
            hl = sanitize_human_label_for_normal_view(hl)
            steps_out.append(StepLine(
                index=i + 1,
                human_label=hl,
                confirmed=(sp.get("id") in confirmed_step_ids),
                needs_review=bool(needs_human),
                badges=raw_badges,
            ))

    # Confirmaciones para vista experto.
    seen_keys: set = set()
    conf_lines: List[ConfirmationLine] = []
    for c in confs:
        key = (c.field, c.step_id or "")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        conf_lines.append(ConfirmationLine.from_record(c))

    # Detalles de contaminación (vista experto). El recompute anterior
    # adjunta ``mission._contamination_details`` cuando hay algo que
    # reportar. La vista normal NUNCA los muestra.
    contamination_details = list(
        getattr(mission, "_contamination_details", None) or []
    )

    # Blockers visibles para el usuario común (vista normal). Filtramos
    # códigos puramente técnicos / debug-only que no aportan valor
    # accionable. Si el blocker es ``LEGACY_RUNTIME_CONTAMINATION``
    # solo lo dejamos cuando hay un detalle con ``affects_execution=True``
    # (la lógica de ``compute_ready_status`` ya garantiza esa relación,
    # pero filtramos defensivamente por si llega del JSON persistido).
    visible_blockers = _compute_visible_blockers(
        blockers,
        contamination_details=contamination_details,
        steps=sep.get("steps") or [],
        mission=mission,
    )

    rerecord_history_serialized: List[dict] = []
    for entry in (mission.rerecord_history or []):
        try:
            rerecord_history_serialized.append(
                entry.model_dump(mode="json")
            )
        except Exception:
            try:
                rerecord_history_serialized.append(dict(entry))  # type: ignore[arg-type]
            except Exception:
                continue

    # Ruido humano (PRD §F) — se calcula best-effort y vive en la
    # vista experto. Cualquier excepción interna se traga: no debe
    # romper Mission Review nunca.
    ignored_noise: List[dict] = []
    try:
        from app.services.missions.noise_audit import audit_ignored_noise
        ignored_noise = list(audit_ignored_noise(mission) or [])
    except Exception:
        ignored_noise = []

    try:
        from app.services.runtime.entity_runtime_resolution import expert_audit_lines

        erl_lines = expert_audit_lines(mission)
        if erl_lines:
            expert_note = (expert_note + "\n\nEntity Resolution Runtime:\n"
                           + "\n".join(erl_lines)).strip()
    except Exception:
        pass

    try:
        from app.services.runtime.universal_collection_entity_engine import (
            expert_audit_lines as uces_expert_lines,
        )
        uces_lines = uces_expert_lines(mission)
        if uces_lines:
            expert_note = (
                expert_note + "\n\nUniversal Collection & Entity Semantics:\n"
                + "\n".join(uces_lines)
            ).strip()
    except Exception:
        pass

    try:
        from app.services.runtime.unified_operational_orchestrator import expert_audit_lines as uool_lines_fn

        uool_lines = uool_lines_fn(mission)
        if uool_lines:
            expert_note = (
                expert_note + "\n\nUnified Operational Orchestration:\n"
                + "\n".join(uool_lines)
            ).strip()
    except Exception:
        pass

    try:
        from app.services.runtime.operational_convergence_program import expert_audit_lines as ocrp_lines_fn

        ocrp_lines = ocrp_lines_fn(mission)
        if ocrp_lines:
            expert_note = (
                expert_note + "\n\nOperational Convergence & Repeatability:\n"
                + "\n".join(ocrp_lines)
            ).strip()
    except Exception:
        pass

    truth_preservation_lines: List[str] = []
    try:
        from app.services.missions.operational_truth_preservation import (
            truth_preservation_expert_lines,
        )

        truth_preservation_lines = truth_preservation_expert_lines(mission)
    except Exception:
        truth_preservation_lines = []

    try:
        from app.services.missions.operational_continuity_reconstruction import (
            continuity_expert_lines,
        )

        cont_lines = continuity_expert_lines(mission)
        if cont_lines:
            truth_preservation_lines = truth_preservation_lines + [""] + cont_lines
    except Exception:
        pass

    try:
        from app.services.missions.operational_truth_lineage import (
            lineage_expert_lines,
        )

        lineage_lines = lineage_expert_lines(mission)
        if lineage_lines:
            truth_preservation_lines = truth_preservation_lines + [""] + lineage_lines
    except Exception:
        pass

    try:
        from app.services.missions.four_layer_operational_model import (
            four_layer_expert_lines,
        )

        fl_lines = four_layer_expert_lines(mission)
        if fl_lines:
            expert_note = (expert_note + "\n\n" + "\n".join(fl_lines)).strip()
    except Exception:
        pass

    return MissionReviewSummary(
        status_label=status_label,
        status_label_expert=status_label_expert,
        status_ready=status_ready,
        steps=steps_out,
        confirmations=conf_lines,
        blockers=blockers,
        visible_blockers=visible_blockers,
        contamination_details=contamination_details,
        rerecord_history=rerecord_history_serialized,
        ignored_noise=ignored_noise,
        provenance_key=provenance,
        expert_note=expert_note,
        truth_preservation_lines=truth_preservation_lines,
    )


# ─────────────────────────────────────────────────────────────────
# Visible blockers — única API consumida por la UI normal.
# ─────────────────────────────────────────────────────────────────


# Códigos que NUNCA deben mostrarse al usuario común (vista normal).
# Son técnicos / debug — viven en la vista experto pero no en el
# panel "arregla esto antes de ejecutar".
_EXPERT_ONLY_BLOCKERS: frozenset = frozenset({
    # Diagnóstico interno de validación estricta — la UI muestra el
    # blocker concreto (EMPTY_PROFILE_NAME_IN_SELECT_PROFILE, etc.) y
    # SEMANTIC_PLAN_NOT_READY ya cubre el caso de plan vacío.
    "SEMANTIC_PLAN_NOT_READY",
    # Mission Truth Gate — códigos técnicos; la vista normal usa buckets
    # («Necesita aclaración», «No ejecutable») sin exponer tokens GATE_*.
    "GATE_PLAN_NOT_READY",
    "GATE_NO_SEMANTIC_PLAN",
    "GATE_PLAN_INVALID_STRICT",
    "GATE_LAYER_MISMATCH",
    "GATE_LEGACY_GRAPH_EXECUTABLE_PURPOSE",
})


def _compute_visible_blockers(
    raw_blockers: List[str],
    *,
    contamination_details: List[dict],
    steps: List[dict],
    mission: Optional[Mission] = None,
) -> List[str]:
    """Filtra ``raw_blockers`` para la vista normal.

    Reglas (PRD 2026-05-08c §G):

      * Quitar códigos en :data:`_EXPERT_ONLY_BLOCKERS`.
      * ``LEGACY_RUNTIME_CONTAMINATION`` solo aparece cuando alguno
        de los detalles asociados tiene ``affects_execution=True``.
        (La función actual de ``compute_ready_status`` ya respeta
        esto, pero JSON persistido puede traer la stale.)
      * ``NEEDS_USER_LABEL_PENDING`` solo aparece si la ambigüedad
        semántica o el compilador marcó un paso con atención humana
        real (``needs_user_label``). Evita banners huérfanos.
      * Preserva el orden original y deduplica.
    """
    has_exec_contamination = any(
        bool(d.get("affects_execution"))
        for d in (contamination_details or [])
    )
    has_legacy_flag = any(
        bool(s.get("needs_user_label"))
        for s in (steps or [])
        if isinstance(s, dict)
    )
    semantic_need_human = False
    if mission is not None:
        try:
            for s in steps or []:
                if not isinstance(s, dict):
                    continue
                sar = analyze_semantic_step_dict(s, mission)
                if sar.requires_human_confirmation:
                    semantic_need_human = True
                    break
        except Exception:
            semantic_need_human = False

    actionable_human_hint = semantic_need_human or has_legacy_flag

    out: List[str] = []
    seen: set = set()
    for b in raw_blockers:
        if b in seen:
            continue
        if b in _EXPERT_ONLY_BLOCKERS:
            continue
        if b == "LEGACY_RUNTIME_CONTAMINATION" and not has_exec_contamination:
            continue
        if b == "NEEDS_USER_LABEL_PENDING" and not actionable_human_hint:
            continue
        seen.add(b)
        out.append(b)
    return out


def get_visible_blockers_for_user(mission: Mission) -> List[str]:
    """API única para preguntarle a la UI normal "¿qué bloquea esta
    misión que el usuario tenga que arreglar?".

    La UI **debe** llamar a esta función (no leer ``not_ready_reasons``
    directamente) para garantizar que:

      1. Si no hay nada que arreglar de verdad, no se muestra el
         banner "Arregla los puntos marcados…".
      2. Detalles puramente técnicos (debug-only, contaminación legacy
         debug) quedan en la vista experto, no en la principal.
      3. El recompute de estado ocurre antes — si el JSON persistido
         viene de un build viejo, se descarta automáticamente.

    Devuelve la lista de códigos a mostrar (puede estar vacía).
    """
    summary = build_mission_review_summary(mission)
    return list(summary.visible_blockers)


# ─────────────────────────────────────────────────────────────────
# Badges (PRD §Fase 2C — Mission Review premium)
# ─────────────────────────────────────────────────────────────────


# Catálogo de badges. La UI puede mapearlos a iconos / colores.
BADGE_VISUAL_STRONG = "📸 Visual fuerte"
BADGE_VISUAL_MEDIUM = "📸 Visual media"
BADGE_VISUAL_INSUFFICIENT = "📸 Visual insuficiente"
BADGE_ZERO_CAPTURE = "⚡ Zero-capture"
BADGE_SEMANTIC = "🧠 Semantic"
BADGE_SELF_HEALED = "🛟 Self-healed"
BADGE_EMERGENCY_COORDS = "⚠ Emergency coords"


def compute_step_badges(step_blob: dict) -> List[str]:
    """Calcula los badges premium para un step del SEP.

    Lee únicamente del dict del step (no Mission entera): así esta
    función queda pura y testeable sin construir misiones completas.

    Fuentes consideradas:
      * ``step.execution_metrics.zero_capture`` (true/false)
      * ``step.execution_metrics.self_healed`` (true/false)
      * ``step.execution_metrics.safety_reason``
      * ``step.target_context.vision_data.visual_fingerprint.quality``
        (``"strong"`` | ``"medium"`` | ``"insufficient"``)
      * ``step.semantic`` o ``step.type`` (presencia de intención
        semántica → badge SEMANTIC)

    Returns:
        Lista de strings ordenados (semantic primero, luego zero-capture,
        luego visual, luego curativos, luego emergency).
    """
    out: List[str] = []
    if not isinstance(step_blob, dict):
        return out

    # 1) Semantic — el step tiene intención clara (no es raw click).
    sem = step_blob.get("semantic") or {}
    sem_type = str(step_blob.get("type") or "").strip()
    if sem_type and sem_type not in {"click", "mouse_scroll"}:
        out.append(BADGE_SEMANTIC)
    elif isinstance(sem, dict) and sem.get("intent"):
        out.append(BADGE_SEMANTIC)

    # 2) Zero-capture (UIA/DOM validó sin foto).
    em = step_blob.get("execution_metrics") or {}
    if isinstance(em, dict) and em.get("zero_capture"):
        out.append(BADGE_ZERO_CAPTURE)

    # 3) Visual quality del fingerprint.
    tc = step_blob.get("target_context") or {}
    vd = tc.get("vision_data") if isinstance(tc, dict) else None
    fp = vd.get("visual_fingerprint") if isinstance(vd, dict) else None
    if isinstance(fp, dict) and fp.get("available"):
        q = str(fp.get("quality") or "").lower()
        if q == "strong":
            out.append(BADGE_VISUAL_STRONG)
        elif q == "medium":
            out.append(BADGE_VISUAL_MEDIUM)
        elif q == "insufficient":
            out.append(BADGE_VISUAL_INSUFFICIENT)

    # 4) Self-healed (recovery resolvió o skip:already_satisfied).
    if isinstance(em, dict):
        if em.get("self_healed") or em.get("strategy_used") == "skip:already_satisfied":
            out.append(BADGE_SELF_HEALED)

    # 5) Emergency coords.
    if isinstance(em, dict):
        sr = str(em.get("safety_reason") or "")
        if sr == "coords_emergency_no_fingerprint_available":
            out.append(BADGE_EMERGENCY_COORDS)

    return out


def render_normal_view(summary: MissionReviewSummary) -> str:
    """Render plano (CLI/test) de la vista normal — la usada por
    defecto en Mission Review.

    Salida ejemplo::

        Entendido

        1. Abrir Chrome
        2. Usar el perfil Daniel Arturo Ramos
        3. Abrir nueva pestaña
        4. Abrir YouTube
        5. Buscar "Devuélveme el amor de Luis Miguel"
        6. Bajar resultados

        Estado: Lista para ejecutar
    """
    out = ["Entendido", ""]
    for s in summary.steps:
        line = f"{s.index}. {s.human_label}"
        if s.badges:
            line += "  " + " ".join(s.badges)
        out.append(line)
    out.extend(["", f"Estado: {summary.status_label}"])
    return "\n".join(out)


_TECHNICAL_TERMS_BLOCKLIST = (
    "GroupControl", "use_coords", "compiled_execution_graph",
    "raw_trace", "/temp/", "fallback_coords", "target_signature",
    "LEGACY_RUNTIME_CONTAMINATION",
)


def render_expert_view(summary: MissionReviewSummary) -> str:
    """Render plano de la vista experto — añade el panel de
    "Confirmado por usuario" + "Detalles técnicos" cuando aplique.

    Salida ejemplo::

        Entendido

        1. Abrir Chrome
        2. Usar el perfil Daniel Arturo Ramos        [confirmado]
        ...

        Estado: Lista para ejecutar (con confirmación del usuario)
        Trazabilidad: READY por confirmación humana, no por captura perfecta.

        Confirmado por el usuario:
          Perfil:    Daniel Arturo Ramos
          Búsqueda:  Devuélveme el amor de Luis Miguel
                     (antes: Devuelveme el amor Luis Miguel)

        Detalles técnicos (no afectan ejecución):
          - compiled_graph_debug_only @ mission.compiled_execution_graph
            compiled_execution_graph (n=8) está marcado debug-only.
    """
    out = ["Entendido", ""]
    for s in summary.steps:
        suffix = "        [confirmado]" if s.confirmed else ""
        out.append(f"{s.index}. {s.human_label}{suffix}")
    out.extend([
        "", f"Estado: {summary.status_label_expert}",
        f"Trazabilidad: {summary.expert_note}",
    ])
    if summary.confirmations:
        out.extend(["", "Confirmado por el usuario:"])
        for c in summary.confirmations:
            line = f"  {c.field_label + ':':10s} {c.value}"
            out.append(line)
            if c.original_value and c.original_value != c.value:
                out.append(f"             (antes: {c.original_value})")
    # Detalles técnicos — solo en vista experto.
    if summary.contamination_details:
        active = [
            d for d in summary.contamination_details
            if d.get("affects_execution")
        ]
        debug_only = [
            d for d in summary.contamination_details
            if not d.get("affects_execution")
        ]
        if active:
            out.extend(["", "Contaminación que afecta ejecución:"])
            for d in active:
                out.append(
                    f"  - {d.get('kind', '?')} @ "
                    f"{d.get('field_path') or d.get('where', '?')}"
                )
                reason = d.get("reason", "")
                if reason:
                    out.append(f"      {reason}")
        if debug_only:
            out.extend(["", "Detalles técnicos (no afectan ejecución):"])
            for d in debug_only:
                out.append(
                    f"  - {d.get('kind', '?')} @ "
                    f"{d.get('field_path') or d.get('where', '?')}"
                )
                reason = d.get("reason", "")
                if reason:
                    out.append(f"      {reason}")
    if summary.ignored_noise:
        out.extend(["", "Ruido descartado:"])
        for n in summary.ignored_noise:
            kind = str(n.get("kind") or "?")
            summary_text = str(n.get("human_summary") or kind)
            out.append(f"  - {kind}: {summary_text}")
    if summary.truth_preservation_lines:
        out.extend(["", *summary.truth_preservation_lines])
    if summary.rerecord_history:
        out.extend(["", "Historial de regrabación:"])
        for h in summary.rerecord_history:
            tgt_idx = h.get("target_step_index")
            tgt_id = h.get("target_step_id", "?")
            saved_at = h.get("saved_at", "")
            replaced = (h.get("replacement_step_snapshot") or {}).get(
                "type", "?"
            )
            previous = h.get("previous_steps_replayed") or []
            affected = h.get("affected_steps") or []
            line = (
                f"  - paso {tgt_idx} (id={tgt_id}) → tipo {replaced!r}"
                f"  · saved_at={saved_at}"
            )
            out.append(line)
            if previous:
                out.append(
                    f"      previos reproducidos: {previous}"
                )
            if affected:
                out.append(f"      afectados: {affected}")
    return "\n".join(out)


__all__ = [
    "ConfirmationLine",
    "MissionReviewSummary",
    "StepLine",
    "BADGE_VISUAL_STRONG",
    "BADGE_VISUAL_MEDIUM",
    "BADGE_VISUAL_INSUFFICIENT",
    "BADGE_ZERO_CAPTURE",
    "BADGE_SEMANTIC",
    "BADGE_SELF_HEALED",
    "BADGE_EMERGENCY_COORDS",
    "compute_step_badges",
    "build_mission_review_summary",
    "get_visible_blockers_for_user",
    "render_expert_view",
    "render_normal_view",
]
