"""
Nevlan Mission Approval Gate
=============================

Una misión solo puede pasar a ``MissionStatus.APPROVED`` si cumple todos
los invariantes que el equipo considera necesarios para que el player
pueda reproducirla con seguridad.

PRD §E:
    No permitir status APPROVED si:
      - hay steps rojos (quality_level == "red"),
      - hay validation_strategy NONE,
      - hay rutas /temp/ en cualquier asset,
      - hay targets inválidos (CLICK sobre contenedor genérico/técnico
        sin label),
      - hay ``keyboard_type_text`` separado que debería ser
        ``field_commit`` o ``search``.

Salida
------

``check_mission_approvable(mission)`` devuelve un ``ApprovalReport`` con:

  - ``ok``: bool — ¿se puede aprobar?
  - ``violations``: list[ApprovalViolation] — cada bloqueante con
    ``code``, ``message``, y opcionalmente ``step_id``.

El UI usa ``violations`` para construir el panel de "Para aprobar,
arregla esto" y poner la misión en ``MissionStatus.NEEDS_REVIEW`` hasta
que se resuelvan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from app.contracts.mission import (
    ActionStrategy,
    Mission,
    MissionStatus,
    ValidationStrategy,
)
from app.services.missions.asset_finalizer import has_temp_paths
from app.services.missions.execution_confidence import (
    ExecLevel,
    ExecutionConfidenceResult,
    compute_execution_confidence,
)
from app.services.missions.semantic_fusion import _is_invalid_click_target


# Códigos de violación que requieren INTERVENCIÓN HUMANA (Mission
# Review). Si el reporte contiene cualquiera de estos, la misión va a
# ``NEEDS_REVIEW``. El resto (red_step, temp_paths, validation_none…)
# son arreglables por la herramienta y van a ``COMPILED_DRAFT``.
_HUMAN_REVIEW_CODES = {
    "needs_user_label",
    "empty_profile",
    "empty_target_label",
    "low_confidence",
    # V2: el ghost simulator no encontró el elemento en pantalla.
    # Necesita que el usuario lo señale antes de poder guardar.
    "ghost_missing",
    # V2: target vacío (CLICK sin nombre/AID/locator) — equivalente a
    # empty_target_label pero para clicks genéricos.
    "empty_target",
    # V3 (PRD 2026-05-06b §4): bloqueantes "duros" del execution_confidence.
    "unknown_kind",
    "empty_app",
    "empty_url",
    "empty_query",
    # PRD 2026-05-07 follow-up: contaminación dentro del plan semántico.
    # Estos son violaciones estructurales del semantic_execution_plan
    # (click/mouse_scroll/etc colados, dicts vacíos, no-dicts) — si
    # aparecen, la misión NO puede ejecutarse hasta que se reparen.
    # NUNCA debe caer silenciosamente al grafo legacy.
    "LEGACY_STEP_INSIDE_SEMANTIC_PLAN",
    "NON_SEMANTIC_STEP_IN_SEMANTIC_PLAN",
    "EMPTY_STEP_IN_SEMANTIC_PLAN",
    "STEP_NOT_DICT_IN_SEMANTIC_PLAN",
    "SEMANTIC_PLAN_STEPS_NOT_LIST",
    "EMPTY_SEMANTIC_PLAN",
    "INVALID_EXECUTABLE_FIELD_IN_SEMANTIC_PLAN",
    "NON_CANONICAL_TYPE_IN_SEMANTIC_PLAN",
    # PRD 2026-05-07c §C/§D: bloqueantes específicos del status READY.
    # Todos son **reparables sin regrabar** — Mission Review pide una
    # confirmación mínima (perfil correcto, query correcta) y guarda
    # la respuesta como aprendizaje permanente.
    "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE",
    "QUERY_FIDELITY_SUSPECT",
    "QUERY_MISSING_SHORT_TOKEN",
    "PROFILE_RESOLUTION_REQUIRED",
    "SEMANTIC_PLAN_NOT_READY",
    "TEMP_ASSET_IN_APPROVED_PLAN",
    "LEGACY_RUNTIME_CONTAMINATION",
    "EMPTY_QUERY_IN_SEARCH",
    "EMPTY_URL_IN_OPEN_URL",
    "EMPTY_APP_IN_OPEN_APP",
    "NEEDS_USER_LABEL_PENDING",
}


# Acciones cuya "calidad de target visual" NO controla la ejecución:
# se ejecutan por nombre (LAUNCH_APP busca proceso, NAVIGATE_OR_SEARCH
# escribe URL, etc.). El ``capture_score`` que el compiler les pone es
# heredado del raw_event de origen pero NO debe disparar ``red_step`` —
# el step es fiable aunque el bbox visual sea malo.
_NON_VISUAL_ACTIONS = {
    ActionStrategy.LAUNCH_APP,
    ActionStrategy.NAVIGATE_OR_SEARCH,
    ActionStrategy.SUBMIT_SEARCH,
    ActionStrategy.SET_FIELD_VALUE,
    ActionStrategy.TYPE_TEXT,
    ActionStrategy.SEND_HOTKEY,
    ActionStrategy.SELECT_PROFILE,
    ActionStrategy.OPEN_NEW_TAB,
    ActionStrategy.OPEN_BOOKMARK,
    ActionStrategy.OPEN_MENU_ITEM,
    ActionStrategy.WAIT_FOR_STATE,
    ActionStrategy.AGENT_PROMPT,
}


# ──────────────────────────────────────────────────────────────────────
# Modelo de violación
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ApprovalViolation:
    """Cada bloqueante para aprobación.

    ``code`` es estable (los tests/UI lo usan para clasificar). ``message``
    está en español, listo para mostrar.
    """
    code: str
    message: str
    step_id: Optional[str] = None
    severity: str = "error"  # "error" bloquea aprobación; "warning" no.


@dataclass
class ApprovalReport:
    ok: bool = True
    violations: List[ApprovalViolation] = field(default_factory=list)

    def add(self, code: str, message: str, *,
            step_id: Optional[str] = None, severity: str = "error") -> None:
        self.violations.append(ApprovalViolation(
            code=code, message=message,
            step_id=step_id, severity=severity,
        ))
        if severity == "error":
            self.ok = False

    @property
    def errors(self) -> List[ApprovalViolation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> List[ApprovalViolation]:
        return [v for v in self.violations if v.severity == "warning"]


# ──────────────────────────────────────────────────────────────────────
# Reglas
# ──────────────────────────────────────────────────────────────────────

def _is_orphan_type_text(cs) -> bool:
    """``TYPE_TEXT`` que NO viene de ``field_session`` ni tiene ``submit_after``.

    Tras la fusión semántica, todos los TYPE_TEXT legítimos viven dentro de
    una field session (con ``field_session=True``) o han sido convertidos
    a ``SET_FIELD_VALUE`` con ``submit_after``. Si encontramos TYPE_TEXT
    suelto sin estos atributos, la fusión falló y la misión NO está
    lista para aprobarse.
    """
    if cs.action_strategy != ActionStrategy.TYPE_TEXT:
        return False
    pl = cs.action_payload or {}
    return not (
        pl.get("field_session")
        or pl.get("submit_after")
        or pl.get("merged_from_fragments")
    )


def _is_orphan_enter(cs) -> bool:
    """``SEND_HOTKEY`` con sólo Enter, sin texto al lado.

    Si el ``search_finalizer`` hizo bien su trabajo, todo Enter
    "post-typeo" se absorbió como ``submit_after=True`` del step
    de texto. Un Enter SUELTO en el grafo es el síntoma típico de
    una fusión rota.
    """
    if cs.action_strategy != ActionStrategy.SEND_HOTKEY:
        return False
    pl = cs.action_payload or {}
    keys = [str(k).lower() for k in (pl.get("keys") or [])]
    hk = str(pl.get("hotkey") or pl.get("key") or "").lower()
    if not keys and hk:
        keys = [hk]
    if keys != ["enter"]:
        return False
    return True


def _step_confidence(cs) -> Optional[float]:
    """Confianza normalizada [0, 1] del step a partir de
    ``confidence.capture_score``.

    Devuelve ``None`` cuando la información no está disponible — el
    caller debe interpretar "no sé" como "no bloqueante", para no
    castigar steps creados sintéticamente o por código legacy que no
    rellena el ``capture_score``.
    """
    try:
        conf = cs.target_context.confidence or {}
    except Exception:
        return None
    score = conf.get("capture_score")
    if score is None:
        return None
    try:
        s = float(score)
    except Exception:
        return None
    return max(0.0, min(1.0, s / 100.0)) if s > 1.0 else max(0.0, min(1.0, s))


def check_mission_approvable(mission: Mission) -> ApprovalReport:
    """Aplica todas las reglas del Approval Gate (PRD §E + 2026-05-06b §4).

    Cambio importante (PRD 2026-05-06b):
      Antes de pasar por los chequeos clásicos basados en
      ``capture_score``, calculamos el **execution_confidence** por step.
      Cuando el step tiene una estrategia de ejecución robusta
      (``ExecLevel.GREEN``) las reglas que penalizan capture débil
      (``red_step``, ``low_confidence``, ``empty_target_label``,
      ``invalid_click_target``, ``validation_none`` sobre acciones
      no visuales) **no bloquean** — quedan como ``severity=warning``
      o se omiten. Esto evita falsos rojos en pasos como
      ``open_new_tab`` (Ctrl+T), ``open_url`` (Ctrl+L) o
      ``search_youtube`` (DOM/UIA/OCR).
    """
    report = ApprovalReport()
    if not mission:
        report.add("missing_mission", "Misión inválida.")
        return report

    # PRD 2026-05-07 §10: si la misión tiene un ``semantic_execution_plan``
    # válido (al menos un step semántico reconocible), el grafo legacy
    # queda como debug — el gate **ignora** sus penalizaciones técnicas
    # y valida solo contra el plan canónico (rutas /temp/, perfil
    # vacío, etc.). Usamos ``has_valid_semantic_execution_plan`` (la
    # única función compartida) para que adapter / gate / bridge nunca
    # se contradigan.
    try:
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        has_valid_plan = has_valid_semantic_execution_plan(mission)
    except Exception:
        has_valid_plan = False

    if has_valid_plan:
        # Ruta semántica: solo /temp/ paths + reglas del plan canónico.
        if has_temp_paths(mission):
            report.add(
                "temp_paths",
                "La misión contiene rutas a assets/temp/. "
                "Ejecuta el Asset Finalizer antes de aprobar.",
            )
        # PRD 2026-05-07 follow-up: validación ESTRICTA del plan.
        # "La evidencia puede ser ruidosa. La intención ejecutable
        # debe ser limpia." — un plan que pasa el routing check pero
        # contiene un step legacy (``click``, ``mouse_scroll``…)
        # NUNCA debe aprobarse. Bloquea con códigos específicos para
        # que el UI/repair pueda remediarlo sin caer a legacy.
        _apply_strict_plan_validation(mission, report)
        _validate_semantic_plan(mission, report)
        return report

    compiled = mission.compiled_execution_graph or []
    if not compiled:
        report.add(
            "empty_graph",
            "La misión no tiene pasos compilados. Recompila antes de aprobar.",
        )
        return report

    # 0. Pre-cómputo: execution_confidence por step (PRD 2026-05-06b §1).
    #    Lo calculamos UNA vez y lo consultamos en cada regla. Como efecto
    #    lateral benéfico, cada step queda con
    #    ``action_payload['execution_confidence']`` listo para la UI.
    exec_results: Dict[str, ExecutionConfidenceResult] = {}
    for cs in compiled:
        try:
            exec_results[cs.id] = compute_execution_confidence(cs, mission=mission)
        except Exception:
            # Defensivo: si el evaluador peta para un step específico, no
            # tumbamos el resto del gate. El step queda sin protección de
            # la nueva lógica (fallback a reglas clásicas).
            exec_results[cs.id] = ExecutionConfidenceResult()

    def _exec_is_green(step_id: str) -> bool:
        r = exec_results.get(step_id)
        return bool(r and r.level == ExecLevel.GREEN and not r.is_blocking)

    def _exec_is_executable(step_id: str) -> bool:
        r = exec_results.get(step_id)
        return bool(r and r.is_executable and not r.is_blocking)

    # 1. /temp/ paths
    if has_temp_paths(mission):
        report.add(
            "temp_paths",
            "La misión contiene rutas a assets/temp/. "
            "Ejecuta el Asset Finalizer antes de aprobar.",
        )

    # Por cada step: validación, rojo, target inválido, type_text huérfano.
    for cs in compiled:
        sid = cs.id
        pl = cs.action_payload or {}
        # Tanto ``needs_reinforcement`` (clicks rojos / fallback) como
        # ``needs_user_label`` (perfil vacío del Chrome workflow) son
        # marcadores equivalentes de "este step necesita una etiqueta
        # humana antes de ejecutarse". Los unificamos aquí para que las
        # reglas downstream no tengan que conocer ambos.
        needs_reinforcement = bool(
            pl.get("needs_reinforcement") or pl.get("needs_user_label")
        )

        # 2. validation_strategy = NONE
        # PRD 2026-05-06b §4: una acción no-visual con estrategia
        # robusta (Ctrl+T, Ctrl+L, hotkey) NO necesita una
        # validación explícita — la postcondition del executor cubre
        # ese caso. Sólo bloqueamos cuando el step es target-driven
        # y el execution_confidence no garantiza otra ruta.
        if cs.validation_strategy == ValidationStrategy.NONE:
            if cs.action_strategy in _NON_VISUAL_ACTIONS and _exec_is_executable(sid):
                report.add(
                    "validation_none_warn",
                    f"Paso {sid}: validation_strategy=NONE; el ejecutor usará "
                    "la postcondition por estado. (informativo)",
                    step_id=sid,
                    severity="warning",
                )
            else:
                report.add(
                    "validation_none",
                    f"Paso {sid}: validation_strategy es NONE. "
                    "Cada paso debe declarar al menos una validación mínima.",
                    step_id=sid,
                )

        # 3. quality_level = red — SOLO para acciones target-driven (clicks)
        #    y SOLO si no estamos ya marcando needs_reinforcement (evitamos
        #    doble-reporte para el mismo paso).
        # PRD 2026-05-06b §4: capture débil NO bloquea si execution
        # confidence (intención) es alta — degradamos a warning.
        if not needs_reinforcement \
                and cs.action_strategy not in _NON_VISUAL_ACTIONS:
            try:
                ql = (cs.target_context.confidence or {}).get("quality_level")
            except Exception:
                ql = None
            if ql == "red":
                if _exec_is_green(sid):
                    report.add(
                        "weak_capture_strong_execution",
                        f"Paso {sid}: capture rojo, pero el executor tiene "
                        "una estrategia robusta. (informativo)",
                        step_id=sid,
                        severity="warning",
                    )
                else:
                    report.add(
                        "red_step",
                        f"Paso {sid}: quality_level=red. Refuerza target o "
                        "vuelve a grabarlo antes de aprobar.",
                        step_id=sid,
                    )

        # 4. CLICK sobre target inválido (no doble-reporte si ya
        #    es needs_reinforcement: el operador YA lo va a arreglar).
        # PRD 2026-05-06b §4: si el executor tiene cómo resolver el
        # click (locator DOM/UIA AID/etiqueta semántica fuerte) NO
        # bloqueamos. El check existente sigue protegiendo clicks
        # sobre contenedores enormes y similares.
        if not needs_reinforcement and _is_invalid_click_target(cs):
            if _exec_is_green(sid):
                report.add(
                    "weak_capture_strong_execution",
                    f"Paso {sid}: target visual frágil pero execution "
                    "confidence alta. (informativo)",
                    step_id=sid,
                    severity="warning",
                )
            else:
                report.add(
                    "invalid_click_target",
                    f"Paso {sid}: click sobre target inválido (contenedor genérico, "
                    "etiqueta técnica o área >25% de la ventana).",
                    step_id=sid,
                )

        # 5. TYPE_TEXT huérfano
        if _is_orphan_type_text(cs):
            report.add(
                "orphan_type_text",
                f"Paso {sid}: TYPE_TEXT sin field_session ni submit_after. "
                "Debería formar parte de un field_commit o search fusionado.",
                step_id=sid,
            )

        # 5b. Enter SUELTO (search_finalizer falló).
        if _is_orphan_enter(cs):
            report.add(
                "orphan_enter",
                f"Paso {sid}: Enter aislado sin texto adyacente. Debería "
                "haberse fusionado dentro de un search/submit.",
                step_id=sid,
            )

        # 6. needs_reinforcement (PRD 2026-05-03b §5)
        if needs_reinforcement:
            prompt = pl.get("label_prompt") or "etiqueta humana"
            report.add(
                "needs_user_label",
                f"Paso {sid}: el paso necesita refuerzo humano antes de "
                f"ejecutarse — pendiente: {prompt}",
                step_id=sid,
            )

        # 7. SELECT_PROFILE con perfil vacío o GENÉRICO
        # (PRD 2026-05-03c §2 + PRD 2026-05-06c §5 + PRD 2026-05-07 §4).
        #
        # PRD 2026-05-07 §4: ``profile=""`` JAMÁS puede pasar — incluso
        # si el step está marcado ``needs_reinforcement``. Sin nombre
        # real, no hay misión EXECUTABLE.
        if cs.action_strategy == ActionStrategy.SELECT_PROFILE:
            profile = str(pl.get("profile", "")).strip()
            # Import diferido: evitar ciclo (semantic_execution_plan
            # importa intent_collapse_engine que pasa por contracts).
            try:
                from app.services.missions.semantic_execution_plan import (
                    is_generic_profile_label,
                )
                profile_is_generic = is_generic_profile_label(profile)
            except Exception:
                profile_is_generic = not profile
            if not profile:
                report.add(
                    "empty_profile",
                    f"Paso {sid}: SELECT_PROFILE con profile vacío. "
                    "Confírmalo desde Mission Review.",
                    step_id=sid,
                )
            elif profile_is_generic:
                # ej. profile="perfil del navegador" / "el perfil" / "default"
                # No es un nombre real → bloquea hasta confirmación.
                report.add(
                    "empty_profile",
                    f"Paso {sid}: SELECT_PROFILE con profile genérico "
                    f"(\"{profile}\"). Confirma el nombre real del perfil "
                    "desde Mission Review.",
                    step_id=sid,
                )

        # 8. SUBMIT/SEARCH sin target_label (PRD 2026-05-03c §2).
        # PRD 2026-05-06b §4: si execution_confidence (search_youtube
        # detectado, locators canónicos, etc.) es GREEN, NO bloqueamos:
        # el target faltante se resuelve en runtime con DOM/UIA/OCR.
        if cs.action_strategy in (
            ActionStrategy.SUBMIT_SEARCH,
            ActionStrategy.SET_FIELD_VALUE,
            ActionStrategy.TYPE_TEXT,
        ) and pl.get("submit_after"):
            site = str(pl.get("site") or pl.get("search_site") or "").strip()
            target_label = str(
                pl.get("target_label") or pl.get("target") or ""
            ).strip()
            if site and not target_label:
                if _exec_is_green(sid):
                    # Informativo, no bloquea.
                    report.add(
                        "weak_capture_strong_execution",
                        f"Paso {sid}: search en {site} sin target_label "
                        "explícito; el executor lo resolverá por DOM/UIA/OCR.",
                        step_id=sid,
                        severity="warning",
                    )
                else:
                    report.add(
                        "empty_target_label",
                        f"Paso {sid}: search en {site} sin target_label. "
                        "Define la barra de búsqueda canónica.",
                        step_id=sid,
                    )

        # 9. Confianza muy baja (<= 0.5) en step target-driven sin
        #    rescate semántico — PRD 2026-05-03c §1.
        # PRD 2026-05-06b §4: la capture_confidence baja por SÍ MISMA
        # nunca debe bloquear si la execution_confidence es alta. Sólo
        # bloqueamos cuando AMBAS son débiles.
        if not needs_reinforcement \
                and cs.action_strategy not in _NON_VISUAL_ACTIONS:
            conf01 = _step_confidence(cs)
            if conf01 is not None and conf01 <= 0.5 and not _exec_is_green(sid):
                report.add(
                    "low_confidence",
                    f"Paso {sid}: confianza captura {conf01:.2f} ≤ 0.5 "
                    "y sin estrategia robusta de ejecución. Refuerza target.",
                    step_id=sid,
                )

        # 10. (V2) Ghost simulator missing — el step no se encuentra
        #     en pantalla. Lo marca ``ghost_simulation.mark_missing_for_review``.
        #
        # PRD 2026-05-06 §5: Ghost ya pasa por el Semantic Certainty
        # Score antes de marcar ``ghost_missing``. Sólo llegan aquí
        # los steps con certainty < 0.60. ``ghost_status`` puede ser
        # ``semantic_override`` o ``soft_warning``: en ambos casos NO
        # bloqueamos, y soft_warning emite ``severity=warning``.
        ghost_status = str(pl.get("ghost_status") or "").strip()
        if ghost_status == "semantic_override":
            # No bloqueante. La intención es fuerte y el executor
            # tiene cadena de resolución robusta.
            pass
        elif ghost_status == "soft_warning":
            report.add(
                "ghost_soft_warning",
                f"Paso {sid}: target no visible al guardar pero la "
                f"intención es plausible "
                f"(certainty={pl.get('ghost_certainty', '?')}). "
                "Se validará durante ejecución.",
                step_id=sid,
                severity="warning",
            )
        elif pl.get("ghost_missing"):
            report.add(
                "ghost_missing",
                f"Paso {sid}: Nevlan no encontró el target en pantalla "
                f"durante la simulación pre-guardado "
                f"({pl.get('ghost_reason') or 'razón desconocida'}). "
                "Señala el elemento o re-graba el paso.",
                step_id=sid,
            )

        # 11. (V2) target vacío en clicks genéricos — distinto de
        #     ``empty_target_label`` (que solo cubría search/submit).
        #     Aquí cubrimos CLICK / DOUBLE_CLICK / RIGHT_CLICK sin
        #     nombre/AID/locator, que el resolver no puede ejecutar.
        if cs.action_strategy in (
            ActionStrategy.CLICK,
            ActionStrategy.DOUBLE_CLICK,
            ActionStrategy.RIGHT_CLICK,
        ):
            tc = cs.target_context
            uia = (tc.uia_data or {}) if tc else {}
            web = (tc.web_data or {}) if tc else {}
            sem = (tc.semantic or {}) if tc else {}
            has_uia = bool(uia.get("name") or uia.get("automation_id"))
            has_web = bool(
                web.get("locators") or web.get("css_selector")
                or web.get("test_id")
            )
            has_sem = bool(sem.get("human_label"))
            if not (has_uia or has_web or has_sem) and not needs_reinforcement:
                report.add(
                    "empty_target",
                    f"Paso {sid}: click sin nombre, locator ni etiqueta. "
                    "El ejecutor no puede encontrar el elemento.",
                    step_id=sid,
                )

        # 13. (PRD 2026-05-06b §4) Bloqueantes "duros" del execution
        #     confidence: unknown_kind, empty_app, empty_url, empty_query,
        #     empty_hotkey. Estos son siempre bloqueantes — la intención
        #     misma está incompleta, no es cuestión de capture vs runtime.
        exec_r = exec_results.get(sid)
        if exec_r and exec_r.is_blocking and exec_r.block_code:
            # Evitamos doble-reporte: si otra regla ya cubrió este caso
            # con un código equivalente (empty_profile, empty_target,
            # etc.) no añadimos uno nuevo.
            existing_codes = {v.code for v in report.violations if v.step_id == sid}
            equivalents = {
                "empty_profile": {"empty_profile"},
                "empty_target": {"empty_target", "empty_target_label"},
                "empty_query": {"empty_query"},
                "empty_url": {"empty_url"},
                "empty_app": {"empty_app"},
                "unknown_kind": {"unknown_kind"},
                "empty_hotkey": {"empty_hotkey"},
            }.get(exec_r.block_code, {exec_r.block_code})
            if not (equivalents & existing_codes):
                report.add(
                    exec_r.block_code,
                    f"Paso {sid}: {exec_r.message}",
                    step_id=sid,
                )

    return report


def _apply_strict_plan_validation(
    mission: Mission, report: "ApprovalReport",
) -> None:
    """Aplica :func:`validate_semantic_execution_plan_strict` y
    propaga las violaciones al ``ApprovalReport`` con sus códigos
    canónicos (``LEGACY_STEP_INSIDE_SEMANTIC_PLAN``,
    ``NON_SEMANTIC_STEP_IN_SEMANTIC_PLAN``, etc.).

    Estos códigos están en ``_HUMAN_REVIEW_CODES`` así que la misión
    se marca ``NEEDS_REVIEW`` y NUNCA se aprueba a EXECUTABLE con
    contaminación legacy dentro del plan.
    """
    try:
        from app.services.missions.semantic_execution_plan import (
            validate_semantic_execution_plan_strict,
        )
    except Exception:
        return
    try:
        result = validate_semantic_execution_plan_strict(mission)
    except Exception as e:
        # Defensivo: si la validación peta por un blob exótico,
        # bloqueamos por seguridad (preferimos NEEDS_REVIEW antes que
        # ejecutar a ciegas).
        report.add(
            "NON_SEMANTIC_STEP_IN_SEMANTIC_PLAN",
            f"Falló la validación estricta del semantic_execution_plan: {e}",
        )
        return
    if result.is_valid:
        return
    for v in result.violations:
        # ``step_id`` no existe a nivel del plan (los SemanticPlanStep
        # se identifican por índice). Codificamos el índice en el
        # mensaje para que el UI pueda navegar.
        prefix = (
            f"[plan step #{v.step_index}] " if v.step_index >= 0 else ""
        )
        report.add(
            v.code,
            f"{prefix}{v.message}",
        )


def _validate_semantic_plan(mission: Mission, report: "ApprovalReport") -> None:
    """Aplica las reglas PRD 2026-05-07 §4/§5 + 2026-05-07c §C al
    ``semantic_execution_plan``.

    Bloquea con ``empty_profile`` cualquier ``select_profile`` con
    ``profile=""`` o etiqueta genérica. Bloquea con ``needs_user_label``
    cualquier step canónico con ``needs_user_label=True`` que aún no
    fue confirmado por el usuario. Además aplica los blockers de READY
    (``EMPTY_PROFILE_NAME_IN_SELECT_PROFILE``, ``QUERY_FIDELITY_SUSPECT``,
    ``QUERY_MISSING_SHORT_TOKEN``, ``TEMP_ASSET_IN_APPROVED_PLAN``…).
    """
    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict):
        return
    steps = sep.get("steps") or []
    if not isinstance(steps, list):
        return

    # Import diferido para evitar ciclo.
    try:
        from app.services.missions.semantic_execution_plan import (
            is_generic_profile_label,
        )
    except Exception:
        is_generic_profile_label = lambda s: not str(s).strip()  # type: ignore

    # Evitar duplicar códigos ya emitidos por el grafo legacy.
    existing = {(v.code, v.step_id) for v in report.violations}

    for ps in steps:
        if not isinstance(ps, dict):
            continue
        ptype = str(ps.get("type") or "")
        params = ps.get("params") or {}
        sid = str(ps.get("id") or f"plan_{ptype}")

        if ptype == "select_profile":
            # Si otro paso ya promovió entidad vía PCOF, no bloquear perfil vacío legacy.
            if any(
                isinstance(s, dict)
                and s.get("type") == "select_entity_from_collection"
                and (
                    (s.get("params") or {}).get("pcof_primary")
                    or (s.get("params") or {}).get("selection_strategy")
                    == "pre_click_operational_freeze"
                )
                for s in steps
            ):
                continue
            # El plan canónico usa ``profile_name`` (compat con
            # CollapsedStep); aceptamos ambos.
            profile = str(
                params.get("profile") or params.get("profile_name") or ""
            ).strip()
            if not profile:
                if ("empty_profile", sid) not in existing:
                    report.add(
                        "empty_profile",
                        f"Plan semántico: select_profile con "
                        "profile vacío. Confirma el perfil real "
                        "desde Mission Review.",
                        step_id=sid,
                    )
                # Código canónico READY-blocker para que UI/runner
                # bridge lo distingan claramente.
                if ("EMPTY_PROFILE_NAME_IN_SELECT_PROFILE", sid) not in existing:
                    report.add(
                        "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE",
                        f"Paso '{ptype}': profile_name está vacío "
                        "dentro del semantic_execution_plan.",
                        step_id=sid,
                    )
            elif is_generic_profile_label(profile):
                if ("empty_profile", sid) not in existing:
                    report.add(
                        "empty_profile",
                        f"Plan semántico: select_profile con perfil "
                        f"genérico (\"{profile}\"). Confirma el "
                        "nombre real del perfil.",
                        step_id=sid,
                    )

        if ptype == "search_youtube":
            query = str(params.get("query") or "").strip()
            if not query:
                if ("EMPTY_QUERY_IN_SEARCH", sid) not in existing:
                    report.add(
                        "EMPTY_QUERY_IN_SEARCH",
                        f"Paso '{ptype}': query vacía en "
                        "semantic_execution_plan.",
                        step_id=sid,
                    )
            fidelity = str(params.get("query_fidelity_status") or "ok")
            if fidelity == "missing_short_token":
                if ("QUERY_MISSING_SHORT_TOKEN", sid) not in existing:
                    report.add(
                        "QUERY_MISSING_SHORT_TOKEN",
                        f"Paso '{ptype}': la query parece haber perdido "
                        "un token corto (de/la/el…). Confirma desde "
                        "Mission Review (no regrabar).",
                        step_id=sid,
                    )
            elif fidelity not in ("ok", ""):
                if ("QUERY_FIDELITY_SUSPECT", sid) not in existing:
                    report.add(
                        "QUERY_FIDELITY_SUSPECT",
                        f"Paso '{ptype}': fidelidad de query sospechosa "
                        f"({fidelity}).",
                        step_id=sid,
                    )

        if ps.get("needs_user_label"):
            # Todo paso semántico que aún esté pendiente de etiqueta.
            if ("needs_user_label", sid) not in existing:
                report.add(
                    "needs_user_label",
                    f"Plan semántico: paso '{ptype}' necesita "
                    "confirmación humana antes de ejecutarse.",
                    step_id=sid,
                )

    # /temp/ paths dentro del semantic_execution_plan → bloqueante
    # con código canónico TEMP_ASSET_IN_APPROVED_PLAN.
    try:
        from app.services.missions.semantic_execution_plan import (
            _has_temp_paths_in_plan,
        )
        if _has_temp_paths_in_plan(sep):
            if ("TEMP_ASSET_IN_APPROVED_PLAN", None) not in existing:
                report.add(
                    "TEMP_ASSET_IN_APPROVED_PLAN",
                    "El semantic_execution_plan aprobado todavía "
                    "contiene rutas /assets/temp/. Ejecuta el "
                    "Asset Finalizer antes de aprobar.",
                )
    except Exception:
        pass


def can_approve(mission: Mission) -> bool:
    """Atajo booleano: ``True`` si la misión pasa el Approval Gate."""
    return check_mission_approvable(mission).ok


def classify_status(mission: Mission) -> MissionStatus:
    """Decide el ``MissionStatus`` que corresponde a la misión tras
    pasar por compiler + asset_finalizer.

    Regla (PRD 2026-05-03c §1):
      * Sin violaciones → ``EXECUTABLE``.
      * Hay violaciones de tipo "necesita usuario" (perfil vacío,
        ``needs_user_label``, ``target_label`` faltante, confianza
        muy baja) → ``NEEDS_REVIEW``.
      * Cualquier otra violación (rojo, /temp/, validation NONE,
        text/enter sueltos, target inválido) → ``COMPILED_DRAFT``.

    El UI lo usa para decidir si mostrar el botón de "Ejecutar" o el
    panel de "Quick Reinforcement".
    """
    report = check_mission_approvable(mission)
    if report.ok:
        return MissionStatus.EXECUTABLE
    codes = {v.code for v in report.errors}
    if codes & _HUMAN_REVIEW_CODES:
        return MissionStatus.NEEDS_REVIEW
    return MissionStatus.COMPILED_DRAFT


def is_executable(mission: Mission) -> bool:
    """Atajo: ``True`` si la misión puede ejecutarse YA. Equivalente a
    ``classify_status(mission) == MissionStatus.EXECUTABLE``."""
    return classify_status(mission) == MissionStatus.EXECUTABLE


__all__ = [
    "ApprovalReport",
    "ApprovalViolation",
    "check_mission_approvable",
    "can_approve",
    "classify_status",
    "is_executable",
]
