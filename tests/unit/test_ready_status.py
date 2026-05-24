"""
Nevlan — Tests del Intent Status READY (PRD 2026-05-07c §C)
============================================================

Reglas duras:

  El plan SOLO puede pasar a ``READY`` cuando TODOS estos invariantes
  se cumplen (consultar :func:`compute_ready_status` y
  :class:`ReadyBlockerCode`):

  1. validación estricta sin contaminación legacy
  2. ``select_profile`` con ``profile_name`` no vacío
  3. ``search_youtube`` con query no vacía y sin pérdida de tokens
  4. ``coords_used == false``
  5. ``legacy_graph_ignored == true``
  6. ``mission.legacy_compiled_graph_purpose == "legacy_debug_only"``
  7. Ningún step con ``needs_user_label == true``
  8. Sin paths ``/temp/`` dentro del plan ni en artifacts activos
  9. ``open_new_tab`` usa ``open_new_tab:hotkey_ctrl_t``
 10. ``open_url`` usa ``open_url:hotkey_ctrl_l``
 11. ``search_youtube`` NO usa ``coords``/``visual`` como primaria
"""
from __future__ import annotations

from typing import List

import pytest

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import CollapseStatus
from app.services.missions.semantic_execution_plan import (
    ReadyBlockerCode,
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_intent_status_to_plan,
    compute_ready_status,
)


# ──────────────────────────────────────────────────────────────────
# Helpers de construcción de plan limpio
# ──────────────────────────────────────────────────────────────────


def _clean_plan() -> SemanticExecutionPlan:
    """Plan canónico LIMPIO listo para pasar a READY (chrome+youtube)."""
    return SemanticExecutionPlan(
        steps=[
            SemanticPlanStep(
                id="m:p00:open_app",
                type="open_app",
                params={"name": "chrome", "method": "windows_search"},
                preferred_strategy="open_app:windows_search",
                fallback_strategies=[
                    "open_app:os_startfile", "open_app:start_command",
                ],
                confidence=0.95,
            ),
            SemanticPlanStep(
                id="m:p01:select_profile",
                type="select_profile",
                params={"profile_name": "Daniel Arturo Ramos",
                        "app": "chrome"},
                preferred_strategy="select_profile:uia_text_match",
                fallback_strategies=[
                    "select_profile:visible_text_ocr",
                    "select_profile:chrome_profile_alias",
                    "select_profile:skip_if_loaded",
                ],
                confidence=0.92,
            ),
            SemanticPlanStep(
                id="m:p02:open_new_tab",
                type="open_new_tab",
                params={"app": "chrome"},
                preferred_strategy="open_new_tab:hotkey_ctrl_t",
                confidence=0.90,
            ),
            SemanticPlanStep(
                id="m:p03:open_url",
                type="open_url",
                params={"url": "youtube.com", "alias": "youtube"},
                preferred_strategy="open_url:hotkey_ctrl_l",
                fallback_strategies=[
                    "open_url:playwright_goto",
                    "open_url:bookmark_uia",
                ],
                confidence=0.95,
            ),
            SemanticPlanStep(
                id="m:p04:search_youtube",
                type="search_youtube",
                params={
                    "query": "Devuélveme el amor de Luis Miguel",
                    "site": "youtube",
                    "target": "youtube_search_box",
                    "query_fidelity_status": "ok",
                },
                preferred_strategy="search_youtube:dom_input",
                fallback_strategies=[
                    "search_youtube:dom_search_input",
                    "search_youtube:uia_searchbox",
                    "search_youtube:ocr_buscar",
                    "search_youtube:keyboard_focus_then_type",
                ],
                confidence=0.95,
            ),
            SemanticPlanStep(
                id="m:p05:scroll_results",
                type="scroll_results",
                params={"direction": "down", "amount": 1,
                        "context": "youtube_results"},
                preferred_strategy="scroll_results:wheel",
                fallback_strategies=[
                    "scroll_results:keyboard",
                    "scroll_results:js_scrollby",
                ],
                confidence=0.85,
            ),
        ],
        source="intent_collapse_engine",
        version=1,
        average_confidence=0.92,
        coords_used=False,
        legacy_graph_ignored=True,
        intent_status=CollapseStatus.NEEDS_REVIEW,
    )


def _attach_to_mission(plan: SemanticExecutionPlan) -> Mission:
    """Helper: persiste el plan en una misión nueva con purpose=debug."""
    m = Mission(name="ready", description="ready test")
    m.legacy_compiled_graph_purpose = "legacy_debug_only"
    m.semantic_execution_plan = plan.to_dict()
    return m


# ──────────────────────────────────────────────────────────────────
# 1. Plan canónico limpio → READY
# ──────────────────────────────────────────────────────────────────


class TestReadyHappyPath:
    def test_clean_plan_reaches_ready(self):
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.READY, blockers
        assert blockers == []

    def test_attach_intent_status_persists_ready_in_plan(self):
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        attach_intent_status_to_plan(plan, mission=m)
        assert plan.intent_status == CollapseStatus.READY
        assert plan.not_ready_reasons == []


# ──────────────────────────────────────────────────────────────────
# 2. Casos que bloquean READY — un test por ReadyBlockerCode
# ──────────────────────────────────────────────────────────────────


class TestReadyBlockers:
    def test_empty_profile_name_blocks_ready(self):
        plan = _clean_plan()
        plan.steps[1].params["profile_name"] = ""
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert (
            ReadyBlockerCode.EMPTY_PROFILE_NAME_IN_SELECT_PROFILE
            in blockers
        )

    def test_generic_profile_name_blocks_ready(self):
        plan = _clean_plan()
        plan.steps[1].params["profile_name"] = "perfil del navegador"
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert (
            ReadyBlockerCode.EMPTY_PROFILE_NAME_IN_SELECT_PROFILE
            in blockers
        )

    def test_query_missing_short_token_blocks_ready(self):
        plan = _clean_plan()
        plan.steps[4].params["query_fidelity_status"] = "missing_short_token"
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.QUERY_MISSING_SHORT_TOKEN in blockers

    def test_empty_query_blocks_ready(self):
        plan = _clean_plan()
        plan.steps[4].params["query"] = ""
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.EMPTY_QUERY_IN_SEARCH in blockers

    def test_coords_used_blocks_ready(self):
        plan = _clean_plan()
        plan.coords_used = True
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION in blockers

    def test_legacy_graph_not_ignored_blocks_ready(self):
        plan = _clean_plan()
        plan.legacy_graph_ignored = False
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION in blockers

    def test_open_new_tab_must_use_ctrl_t(self):
        plan = _clean_plan()
        plan.steps[2].preferred_strategy = "open_new_tab:click_button"
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION in blockers

    def test_open_url_must_use_ctrl_l(self):
        plan = _clean_plan()
        plan.steps[3].preferred_strategy = "open_url:bookmark_uia"
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION in blockers

    def test_search_youtube_coords_primary_blocks_ready(self):
        plan = _clean_plan()
        plan.steps[4].preferred_strategy = "search_youtube:vision_coords"
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION in blockers

    def test_needs_user_label_spurious_cleared_when_carrier_is_explicit(self):
        """``needs_user_label`` heredado no bloquea si el texto SEM ya es suficiente."""
        plan = _clean_plan()
        plan.steps[1].needs_user_label = True
        plan.steps[1].label_prompt = "¿Qué perfil?"
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.READY, blockers
        assert ReadyBlockerCode.NEEDS_USER_LABEL_PENDING not in blockers

    def test_temp_path_in_plan_blocks_ready(self):
        plan = _clean_plan()
        plan.steps[4].params["locators_asset"] = "/assets/temp/yt_box.png"
        m = _attach_to_mission(plan)
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.TEMP_ASSET_IN_APPROVED_PLAN in blockers

    def test_empty_plan_yields_compiled_draft(self):
        empty = SemanticExecutionPlan()
        status, blockers = compute_ready_status(empty, mission=None)
        assert status == CollapseStatus.COMPILED_DRAFT
        assert ReadyBlockerCode.SEMANTIC_PLAN_NOT_READY in blockers


# ──────────────────────────────────────────────────────────────────
# Contamination details — PRD 2026-05-08 §G
# ──────────────────────────────────────────────────────────────────


class TestContaminationDetails:
    """Cuando aparece ``LEGACY_RUNTIME_CONTAMINATION``, debe quedar
    el origen exacto del contagio en ``mission._contamination_details``.
    """

    def test_coords_used_includes_field_path(self):
        plan = _clean_plan()
        plan.coords_used = True
        m = _attach_to_mission(plan)
        compute_ready_status(plan, mission=m)
        details = getattr(m, "_contamination_details", [])
        assert any(
            d["where"] == "plan.coords_used" for d in details
        ), f"details no apuntan a plan.coords_used: {details}"

    def test_search_youtube_bad_primary_strategy_includes_step_path(
        self,
    ):
        plan = _clean_plan()
        plan.steps[4].preferred_strategy = "search_youtube:vision_coords"
        m = _attach_to_mission(plan)
        compute_ready_status(plan, mission=m)
        details = getattr(m, "_contamination_details", [])
        assert any(
            "plan.steps[4:search_youtube].preferred_strategy" in d["where"]
            and "vision_coords" in d["reason"]
            for d in details
        ), (
            f"details no identifican el step contaminante con "
            f"preferred_strategy=vision_coords: {details}"
        )

    def test_open_new_tab_off_canon_includes_step_path(self):
        plan = _clean_plan()
        plan.steps[2].preferred_strategy = "open_new_tab:click_button"
        m = _attach_to_mission(plan)
        compute_ready_status(plan, mission=m)
        details = getattr(m, "_contamination_details", [])
        assert any(
            "plan.steps[2:open_new_tab].preferred_strategy" in d["where"]
            for d in details
        )

    def test_clean_plan_has_no_contamination_details(self):
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        compute_ready_status(plan, mission=m)
        details = getattr(m, "_contamination_details", [])
        assert details == [] or details is None or details == ()


# ──────────────────────────────────────────────────────────────────
# PRD 2026-05-08c §A — affects_execution: distinguir contaminación
# que bloquea ejecución vs. evidencia debug que no afecta READY.
# ──────────────────────────────────────────────────────────────────


class TestAffectsExecutionFlag:
    """``LEGACY_RUNTIME_CONTAMINATION`` solo se emite cuando la
    contaminación detectada AFECTA ejecución (``affects_execution=True``).
    Casos meramente diagnósticos (debug-only) quedan en
    ``contamination_details`` pero NO bloquean READY.
    """

    def test_dirty_compiled_graph_debug_only_does_not_trigger_legacy_runtime_contamination(self):
        """Grafo legacy sucio + ``legacy_compiled_graph_purpose ==
        'legacy_debug_only'`` → registra detalle pero NO bloquea."""
        from app.contracts.mission import (
            CompiledStep, ActionStrategy, TargetContext, ValidationStrategy,
        )
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        # Inyectamos un grafo legacy crudo (8 steps con coords basura).
        m.compiled_execution_graph = [
            CompiledStep(
                id=f"legacy_{i}",
                action_strategy=ActionStrategy.CLICK,
                action_payload={"x": 100 + i, "y": 200 + i,
                                "fallback_strategy": "use_coords"},
                target_context=TargetContext(),
                validation_strategy=ValidationStrategy.NONE,
            )
            for i in range(8)
        ]
        m.legacy_compiled_graph_purpose = "legacy_debug_only"
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.READY, (
            f"Plan limpio + grafo legacy debug-only debería ser "
            f"READY. blockers={blockers}"
        )
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION not in blockers
        details = getattr(m, "_contamination_details", []) or []
        debug_entries = [
            d for d in details
            if d.get("kind") == "compiled_graph_debug_only"
        ]
        assert len(debug_entries) == 1, (
            f"Esperaba 1 detalle debug-only, hay {len(debug_entries)}"
        )
        assert debug_entries[0]["affects_execution"] is False

    def test_raw_trace_coords_do_not_trigger_legacy_runtime_contamination(self):
        """Coordenadas en ``raw_trace`` son evidencia humana, no
        afectan la decisión de ejecución → no contamina."""
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        # raw_trace con clicks crudos + coords + scrolls (ruido típico
        # de captura).
        m.raw_trace = [
            {
                "id": "evt0",
                "event_type": "mouse_click",
                "mouse_action": {"x": 100, "y": 200, "button": "left"},
                "metadata": {
                    "click_xy": [100, 200],
                    "rel_win": {"fx": 0.1, "fy": 0.2},
                },
            },
            {
                "id": "evt1",
                "event_type": "mouse_scroll",
                "mouse_action": {"x": 1000, "y": 500},
            },
        ]
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.READY, blockers
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION not in blockers

    def test_target_signature_coords_do_not_trigger_legacy_runtime_contamination(self):
        """``target_signature`` puede guardar coordenadas absolutas
        como evidencia visual — no afecta el plan."""
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        # Inyectamos en un step semántico una target_signature con bbox
        # absoluto (es solo evidencia, no estrategia primaria).
        plan.steps[1].params["target_signature"] = {
            "coordinates": {
                "absolute": {"x": 485, "y": 2118},
                "bbox_element": {
                    "left": 144, "top": 2040,
                    "width": 1032, "height": 120,
                },
            },
            "visual_assets": {
                "ref_mid": "C:/.../step_mid.png",
            },
        }
        m.semantic_execution_plan = plan.to_dict()
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.READY, blockers
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION not in blockers

    def test_run_legacy_player_called_triggers_legacy_runtime_contamination(self):
        """Si ``run_legacy_player`` fue invocado, hay contaminación
        REAL — el bridge cayó a legacy."""
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        m._run_legacy_player_called = True
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION in blockers
        details = getattr(m, "_contamination_details", []) or []
        assert any(
            d.get("kind") == "run_legacy_player_called"
            and d.get("affects_execution") is True
            for d in details
        ), f"details no incluye run_legacy_player_called: {details}"

    def test_decision_source_compiled_graph_triggers_legacy_runtime_contamination(self):
        """``mission._last_decision.source == 'compiled_execution_graph'``
        es prueba directa de que el bridge tomó decisión sobre legacy."""
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        m._last_decision = {
            "source": "compiled_execution_graph",
            "step_id": "legacy_3",
            "reason": "fallback to legacy graph",
        }
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION in blockers
        details = getattr(m, "_contamination_details", []) or []
        assert any(
            d.get("kind") == "decision_source_legacy"
            and d.get("affects_execution") is True
            for d in details
        )

    def test_ghost_simulating_legacy_graph_triggers_legacy_runtime_contamination(self):
        """Ghost Simulation simulando el grafo legacy en lugar del
        plan semántico → contaminación."""
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        m._ghost_simulation_meta = {
            "simulating": "compiled_execution_graph",
            "step_count": 8,
        }
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION in blockers

    def test_compiled_graph_with_executable_purpose_triggers_contamination(self):
        """Grafo legacy con purpose != 'legacy_debug_only' y no vacío =
        riesgo de doble ejecución → bloquea."""
        from app.contracts.mission import (
            CompiledStep, ActionStrategy, TargetContext, ValidationStrategy,
        )
        plan = _clean_plan()
        m = _attach_to_mission(plan)
        m.compiled_execution_graph = [
            CompiledStep(
                id="legacy_0",
                action_strategy=ActionStrategy.CLICK,
                action_payload={"x": 100, "y": 200},
                target_context=TargetContext(),
                validation_strategy=ValidationStrategy.NONE,
            ),
        ]
        m.legacy_compiled_graph_purpose = "execution"
        status, blockers = compute_ready_status(plan, mission=m)
        assert status == CollapseStatus.NEEDS_REVIEW
        assert ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION in blockers

    def test_contamination_details_have_full_traceability(self):
        """Cada entrada en ``contamination_details`` debe tener los
        cinco campos requeridos por PRD §A.4."""
        plan = _clean_plan()
        plan.coords_used = True
        m = _attach_to_mission(plan)
        compute_ready_status(plan, mission=m)
        details = getattr(m, "_contamination_details", []) or []
        assert details
        required = {
            "kind", "field_path", "source_module",
            "contaminating_value", "reason", "affects_execution",
        }
        for d in details:
            missing = required - set(d.keys())
            assert not missing, (
                f"detalle {d!r} le faltan campos: {missing}"
            )
