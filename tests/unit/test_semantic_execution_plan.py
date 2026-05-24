"""Tests del SemanticExecutionPlan (PRD 2026-05-06c).

Cubren:
* Construcción del plan canónico desde un Intent Plan dado.
* Conversión a ``MissionStep`` para el SmartExecutor.
* Persistencia y reutilización en ``mission.semantic_execution_plan``.
* Bloqueo cuando el ``profile`` es genérico
  (``"perfil del navegador"``, ``"el perfil"``, ``"default"``…).
* ``preferred_strategy`` correcta por tipo (Ctrl+T, Ctrl+L, DOM, etc.).
* Filtrado de estrategias prohibidas como primaria
  (``vision_coords``, ``visual_asset``, ``relative_coords``,
  ``click_button``, ``group_control``, ``guide_service``,
  ``video_stream``, ``use_coords``).
* ``coords_used == False`` y ``legacy_graph_ignored == True`` por
  defecto.
* Que el ``smart_runner_bridge`` use el plan como fuente y bloquee
  la ejecución cuando el plan trae un step con ``needs_user_label``.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    Mission,
    TargetContext,
)
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.intent_collapse_engine import (
    CollapsedStep,
    CollapseStatus,
    MissionIntentPlan,
)
from app.services.missions.semantic_execution_plan import (
    _FORBIDDEN_PRIMARY_HINTS,
    SemanticExecutionPlan,
    SemanticPlanStep,
    attach_semantic_plan_to_mission,
    build_semantic_execution_plan,
    has_semantic_plan,
    is_generic_profile_label,
    semantic_plan_to_mission_steps,
)


# ─── Helpers ──────────────────────────────────────────────────────────


def _full_intent_plan(profile: str = "Daniel Arturo Ramos") -> MissionIntentPlan:
    """Plan ICE de la misión browser-search del PRD."""
    return MissionIntentPlan(
        status=CollapseStatus.EXECUTABLE,
        semantic_steps=[
            CollapsedStep(
                type="open_app",
                params={"app": "chrome", "method": "windows_search"},
                confidence=0.95,
                human_label="Abrir Chrome",
            ),
            CollapsedStep(
                type="select_profile",
                params={"profile": profile, "app": "chrome"},
                confidence=0.92,
                human_label="Elegir perfil del navegador",
            ),
            CollapsedStep(
                type="open_new_tab",
                params={"app": "chrome"},
                confidence=0.95,
                human_label="Abrir nueva pestaña",
            ),
            CollapsedStep(
                type="open_url",
                params={"url": "youtube.com", "name": "YouTube"},
                confidence=0.95,
                human_label="Abrir YouTube",
            ),
            CollapsedStep(
                type="search_youtube",
                params={"query": "devuelveme el amor luis miguel"},
                confidence=0.95,
                human_label="Buscar en YouTube",
            ),
            CollapsedStep(
                type="scroll_results",
                params={"direction": "down", "amount": 2},
                confidence=0.85,
                human_label="Bajar para ver más resultados",
            ),
        ],
    )


def _empty_mission(mid: str = "mission-test") -> Mission:
    return Mission(id=mid, name="test mission")


def _legacy_mission_with_garbage_graph() -> Mission:
    """Misión cuyo ``compiled_execution_graph`` contiene targets legacy
    con coords + bloques basura ('GroupControl', 'video-stream'), pero
    cuyo ``raw_trace`` no permite re-colapsar.

    Lo usamos para verificar que el bridge ignora ese grafo cuando
    inyectamos el ``semantic_execution_plan`` directamente.
    """
    return Mission(
        id="legacy-mid",
        name="legacy + plan",
        compiled_execution_graph=[
            CompiledStep(
                goal="GroupControl",
                action_strategy=ActionStrategy.CLICK,
                action_payload={
                    "target_label": "video-stream",
                    "fallback_strategy": "use_coords",
                    "coords_xy": {"x": 800, "y": 600},
                },
                target_context=TargetContext(
                    fallback_coords={"x": 800, "y": 600},
                ),
            ),
            CompiledStep(
                goal="click_button",
                action_strategy=ActionStrategy.CLICK,
                action_payload={
                    "target_label": "GroupControl",
                    "fallback_strategy": "use_coords",
                },
            ),
        ],
    )


# ─── 1. Construcción canónica del plan ────────────────────────────────


class TestBuildSemanticExecutionPlan:
    def test_six_steps_in_canonical_order(self):
        mission = _empty_mission()
        plan = build_semantic_execution_plan(
            mission, intent_plan=_full_intent_plan(),
        )
        assert plan.kinds == [
            "open_app",
            "select_profile",
            "open_new_tab",
            "open_url",
            "search_content",
            "scroll_results",
        ]
        assert plan.step_count == 6
        assert plan.legacy_graph_ignored is True
        assert plan.coords_used is False

    def test_open_app_uses_windows_search_strategy(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        step = next(s for s in plan.steps if s.type == "open_app")
        assert step.preferred_strategy == "open_app:windows_search"
        assert step.params["name"] == "chrome"
        assert step.params["method"] == "windows_search"

    def test_open_new_tab_uses_ctrl_t_only(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        step = next(s for s in plan.steps if s.type == "open_new_tab")
        assert step.preferred_strategy == "open_new_tab:hotkey_ctrl_t"
        # Ningún fallback puede ser visión/coords como primaria.
        for s in step.fallback_strategies:
            assert not any(
                h in s.lower() for h in _FORBIDDEN_PRIMARY_HINTS
            ), f"fallback prohibido: {s}"

    def test_open_url_youtube_uses_ctrl_l(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        step = next(s for s in plan.steps if s.type == "open_url")
        assert step.preferred_strategy == "open_url:hotkey_ctrl_l"
        assert step.params.get("alias") == "youtube"

    def test_search_content_youtube_uses_smart_route_first(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        step = next(s for s in plan.steps if s.type == "search_content")
        assert step.preferred_strategy == "search_content:smart_route"
        assert step.params["query"] == "devuelveme el amor luis miguel"
        assert step.params["site"] == "youtube"
        assert str(step.params.get("provider") or "").lower() == "youtube"
        assert step.params["target"] == "youtube_search_box"

    def test_scroll_results_uses_wheel_first(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        step = next(s for s in plan.steps if s.type == "scroll_results")
        assert step.preferred_strategy == "scroll_results:wheel"
        assert step.params["direction"] == "down"
        assert step.params["amount"] == 2

    def test_no_forbidden_primary_strategy(self):
        """Ningún step puede tener una estrategia prohibida como primaria."""
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        for s in plan.steps:
            assert not any(
                h in s.preferred_strategy.lower()
                for h in _FORBIDDEN_PRIMARY_HINTS
            ), f"step {s.type} con primaria prohibida: {s.preferred_strategy}"

    def test_average_confidence_over_85pct(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        assert plan.average_confidence >= 0.85

    def test_intent_status_propagates(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        assert plan.intent_status == CollapseStatus.EXECUTABLE


# ─── 2. Profile genérico bloquea EXECUTABLE ───────────────────────────


class TestGenericProfileLabel:
    @pytest.mark.parametrize("label", [
        "perfil del navegador",
        "Perfil del Navegador",
        "  perfil  ",
        "el perfil",
        "default",
        "User",
        "USUARIO",
        "profile",
        "browser profile",
        "chrome profile",
        "",
        "   ",
        None,
    ])
    def test_generic_labels_detected(self, label):
        assert is_generic_profile_label(label) is True

    @pytest.mark.parametrize("label", [
        "Daniel Arturo Ramos",
        "María José",
        "Trabajo",
        "Cuenta personal",
    ])
    def test_real_names_pass(self, label):
        assert is_generic_profile_label(label) is False

    def test_plan_with_generic_profile_marks_needs_label(self):
        plan = build_semantic_execution_plan(
            _empty_mission(),
            intent_plan=_full_intent_plan(profile="perfil del navegador"),
        )
        sp = next(s for s in plan.steps if s.type == "select_profile")
        assert sp.needs_user_label is True
        assert "perfil" in sp.label_prompt.lower() \
            or sp.label_prompt != ""
        # No es ejecutable porque hay un step necesitado de confirmación.
        assert plan.is_executable is False
        assert plan.needs_user_label is True

    def test_plan_with_real_profile_is_executable(self):
        plan = build_semantic_execution_plan(
            _empty_mission(),
            intent_plan=_full_intent_plan(profile="Daniel Arturo Ramos"),
        )
        sp = next(s for s in plan.steps if s.type == "select_profile")
        assert sp.needs_user_label is False
        assert sp.params["profile_name"] == "Daniel Arturo Ramos"
        assert plan.is_executable is True


# ─── 3. Conversión a MissionStep ──────────────────────────────────────


class TestSemanticPlanToMissionSteps:
    def test_converts_six_steps(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        steps = semantic_plan_to_mission_steps(plan)
        assert [s.kind for s in steps] == [
            "open_app", "select_profile", "open_new_tab",
            "open_url", "search_content", "scroll_results",
        ]
        assert all(isinstance(s, MissionStep) for s in steps)

    def test_no_coords_in_params(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        steps = semantic_plan_to_mission_steps(plan)
        for s in steps:
            assert "bbox" not in s.params
            assert "coords_xy" not in s.params
            assert "fallback_coords" not in s.params
            assert "absolute_xy" not in s.params

    def test_preferred_strategy_injected_in_params(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        steps = semantic_plan_to_mission_steps(plan)
        new_tab = next(s for s in steps if s.kind == "open_new_tab")
        assert new_tab.params["preferred_strategy"] == "open_new_tab:hotkey_ctrl_t"

    def test_search_content_step_has_query_and_site(self):
        plan = build_semantic_execution_plan(
            _empty_mission(), intent_plan=_full_intent_plan(),
        )
        steps = semantic_plan_to_mission_steps(plan)
        s = next(x for x in steps if x.kind == "search_content")
        assert s.params["query"] == "devuelveme el amor luis miguel"
        assert s.params["site"] == "youtube"
        assert s.params["target"] == "youtube_search_box"


# ─── 4. Persistencia en la misión ─────────────────────────────────────


class TestAttachToMission:
    def test_attach_persists_dict(self):
        mission = _empty_mission()
        plan = attach_semantic_plan_to_mission(
            mission,
            plan=build_semantic_execution_plan(
                mission, intent_plan=_full_intent_plan(),
            ),
        )
        assert mission.semantic_execution_plan is not None
        assert mission.semantic_execution_plan["source"] == "intent_collapse_engine"
        assert mission.semantic_execution_plan["legacy_graph_ignored"] is True
        assert mission.semantic_execution_plan["coords_used"] is False
        kinds = [s["type"] for s in mission.semantic_execution_plan["steps"]]
        assert kinds == [
            "open_app", "select_profile", "open_new_tab",
            "open_url", "search_content", "scroll_results",
        ]
        assert plan.step_count == 6

    def test_has_semantic_plan_helper(self):
        mission = _empty_mission()
        assert has_semantic_plan(mission) is False
        attach_semantic_plan_to_mission(
            mission,
            plan=build_semantic_execution_plan(
                mission, intent_plan=_full_intent_plan(),
            ),
        )
        assert has_semantic_plan(mission) is True

    def test_round_trip_dict(self):
        mission = _empty_mission()
        attach_semantic_plan_to_mission(
            mission,
            plan=build_semantic_execution_plan(
                mission, intent_plan=_full_intent_plan(),
            ),
        )
        plan = SemanticExecutionPlan.from_dict(mission.semantic_execution_plan)
        assert plan.step_count == 6
        assert plan.legacy_graph_ignored is True
        assert plan.coords_used is False

    def test_reuse_existing_plan(self):
        mission = _empty_mission()
        attach_semantic_plan_to_mission(
            mission,
            plan=build_semantic_execution_plan(
                mission, intent_plan=_full_intent_plan(),
            ),
        )
        # Ahora la misión tiene plan persistido. Construir otra vez sin
        # intent_plan debe devolver el mismo plan (use_existing=True).
        plan2 = build_semantic_execution_plan(mission)
        assert plan2.step_count == 6
        assert plan2.kinds == [
            "open_app", "select_profile", "open_new_tab",
            "open_url", "search_content", "scroll_results",
        ]


# ─── 5. Bridge usa el plan como fuente única ──────────────────────────


def _drop_blocking_violations(report):
    """Útil cuando queremos saber si hay errores reales bloqueantes."""
    return [v for v in (report.errors or []) if getattr(v, "severity", "error") == "error"]


class TestSmartRunnerBridgeUsesSemanticPlan:
    def _make_executable_mission(self) -> Mission:
        """Misión legacy con basura + plan semántico ya adjunto.

        Con esto verificamos que el bridge IGNORA el grafo legacy y
        usa el plan limpio. Para que `classify_status` la marque
        EXECUTABLE necesitamos que el grafo legacy también pase el
        gate; usamos directamente un grafo CON pasos semánticos
        canónicos (los que produciría el ICE) para evitar bloqueos
        del gate.
        """
        from app.services.missions.intent_collapse_engine import (
            apply_collapse_to_mission,
        )

        mission = _empty_mission()
        # Necesitamos compiled_execution_graph para que el adapter
        # legacy considere "semántica" la misión. Lo poblamos con el
        # ICE.
        mission.compiled_execution_graph = [
            CompiledStep(
                goal="Abrir Chrome",
                action_strategy=ActionStrategy.LAUNCH_APP,
                action_payload={"app_name": "chrome", "method": "windows_search"},
                target_context=TargetContext(
                    semantic={"intent": "open_app"},
                    confidence={"capture_score": 95, "quality_level": "green"},
                ),
            ),
            CompiledStep(
                goal="Perfil",
                action_strategy=ActionStrategy.SELECT_PROFILE,
                action_payload={"profile": "Daniel Arturo Ramos", "app": "chrome"},
                target_context=TargetContext(
                    semantic={"intent": "select_profile"},
                    confidence={"capture_score": 92, "quality_level": "green"},
                ),
            ),
            CompiledStep(
                goal="Nueva pestaña",
                action_strategy=ActionStrategy.OPEN_NEW_TAB,
                action_payload={"app": "chrome", "preferred_strategy": "ctrl_t"},
                target_context=TargetContext(
                    semantic={"intent": "open_new_tab"},
                    confidence={"capture_score": 95, "quality_level": "green"},
                ),
            ),
            CompiledStep(
                goal="YouTube",
                action_strategy=ActionStrategy.OPEN_BOOKMARK,
                action_payload={"name": "YouTube", "url": "youtube.com"},
                target_context=TargetContext(
                    semantic={"intent": "open_url"},
                    confidence={"capture_score": 95, "quality_level": "green"},
                ),
            ),
            CompiledStep(
                goal="Buscar en YouTube",
                action_strategy=ActionStrategy.SUBMIT_SEARCH,
                action_payload={
                    "site": "youtube",
                    "text": "devuelveme el amor luis miguel",
                    "submit_after": True,
                    "target_label": "youtube_search_box",
                },
                target_context=TargetContext(
                    semantic={"intent": "search_youtube"},
                    confidence={"capture_score": 95, "quality_level": "green"},
                ),
            ),
        ]
        # Adjuntamos plan limpio.
        attach_semantic_plan_to_mission(
            mission,
            plan=build_semantic_execution_plan(
                mission, intent_plan=_full_intent_plan(),
            ),
        )
        return mission

    def test_decision_uses_semantic_plan_source(self, monkeypatch):
        # Stub del executor para que no toque el SO.
        from app.services.missions import smart_runner_bridge as bridge

        captured = {}

        def fake_run(self_, steps, **_kw):
            from app.services.missions.smart_executor import (
                MissionResult, StepOutcome,
            )
            captured["steps"] = list(steps)
            res = MissionResult(run_id="r", status="success")
            for s in steps:
                res.outcomes.append(StepOutcome(
                    step_id=s.id, kind=s.kind, status="success",
                    strategy_used=s.params.get("preferred_strategy"),
                ))
            return res

        from app.services.missions.smart_executor import SmartMissionExecutor
        monkeypatch.setattr(SmartMissionExecutor, "run_mission", fake_run)

        mission = self._make_executable_mission()
        decision = bridge.run_mission_smart(
            mission,
            allow_legacy_fallback=False,
            use_auto_learning=False,
        )

        assert not decision.blocked, (
            f"bloqueada inesperadamente: {decision.reason} "
            f"errors={[e.code for e in (decision.report.errors or [])] if decision.report else []}"
        )
        assert decision.player_used == "smart"
        assert decision.source == "semantic_execution_plan"
        assert decision.legacy_graph_ignored is True
        # Tras PRD 2026-05-14 §SIPE el bridge promueve tipos legacy /
        # canónicos de búsqueda (search_youtube/search_content/open_url)
        # a tipos genéricos (search_site/open_site). El resto del orden
        # se conserva.
        assert decision.semantic_step_kinds == [
            "open_app", "select_profile", "open_new_tab",
            "open_site", "search_site", "scroll_results",
        ]
        # Los steps que llegaron al executor son los del plan, no los
        # del compiled_execution_graph (que tenía solo 5).
        assert len(captured["steps"]) == 6
        assert captured["steps"][0].kind == "open_app"
        # coords_used queda False cuando ningún outcome usó coords.
        assert decision.coords_used is False

    def test_decision_blocks_when_plan_step_needs_label(self, monkeypatch):
        from app.services.missions import smart_runner_bridge as bridge

        mission = self._make_executable_mission()
        # Reescribimos el plan con perfil genérico → bloquea.
        attach_semantic_plan_to_mission(
            mission,
            plan=build_semantic_execution_plan(
                mission,
                intent_plan=_full_intent_plan(profile="perfil del navegador"),
            ),
            force=True,
        )
        # Forzamos que classify_status diga EXECUTABLE para llegar al
        # plan check.
        from app.contracts.mission import MissionStatus
        monkeypatch.setattr(
            bridge, "classify_status",
            lambda m: MissionStatus.EXECUTABLE,
        )
        # Stub del executor por si se llegara a ejecutar (no debería).
        from app.services.missions.smart_executor import (
            MissionResult, SmartMissionExecutor, StepOutcome,
        )

        def fake_run(self_, steps, **_kw):
            res = MissionResult(run_id="r", status="success")
            for s in steps:
                res.outcomes.append(StepOutcome(
                    step_id=s.id, kind=s.kind, status="success",
                ))
            return res
        monkeypatch.setattr(SmartMissionExecutor, "run_mission", fake_run)

        decision = bridge.run_mission_smart(
            mission,
            allow_legacy_fallback=False,
            use_auto_learning=False,
        )
        assert decision.blocked is True
        assert decision.reason == "semantic_plan_blockers"
        assert any(b.code == "empty_profile" for b in decision.blockers)
        # Y el plan se conservó en la decisión para auditoría.
        assert decision.semantic_plan is not None
        assert decision.semantic_plan.needs_user_label is True


# ─── 6. Telemetría a dict ─────────────────────────────────────────────


class TestRunnerDecisionTelemetry:
    def test_to_dict_includes_new_fields(self, monkeypatch):
        from app.services.missions import smart_runner_bridge as bridge

        def fake_run(self_, steps, **_kw):
            from app.services.missions.smart_executor import (
                MissionResult, StepOutcome,
            )
            res = MissionResult(run_id="r", status="success")
            for s in steps:
                res.outcomes.append(StepOutcome(
                    step_id=s.id, kind=s.kind, status="success",
                    strategy_used=s.params.get("preferred_strategy"),
                ))
            return res

        from app.services.missions.smart_executor import SmartMissionExecutor
        monkeypatch.setattr(SmartMissionExecutor, "run_mission", fake_run)

        mission = TestSmartRunnerBridgeUsesSemanticPlan()._make_executable_mission()
        decision = bridge.run_mission_smart(
            mission,
            allow_legacy_fallback=False,
            use_auto_learning=False,
        )
        d = decision.to_dict()
        assert d["source"] == "semantic_execution_plan"
        assert d["legacy_graph_ignored"] is True
        assert d["coords_used"] is False
        assert "semantic_step_kinds" in d
        # PRD 2026-05-14 §SIPE: tipos genéricos tras la promoción.
        assert d["semantic_step_kinds"] == [
            "open_app", "select_profile", "open_new_tab",
            "open_site", "search_site", "scroll_results",
        ]


# ─── 7. SmartExecutor: estrategias prohibidas reordenadas ─────────────


class TestSmartExecutorStrategyOrdering:
    def test_forbidden_strategies_pushed_to_end(self):
        """Si un step llega SIN preferred_strategy del plan, el
        executor todavía debe degradar las estrategias visuales/coords
        al final del ranking.
        """
        from app.services.missions.execution_contracts import (
            StepContract, MissionStep,
        )
        from app.services.missions.smart_executor import _resolve_strategies

        contract = StepContract(
            step=MissionStep(id="x", kind="search_youtube", params={}),
            precondition=lambda s: True,
            postcondition=lambda s: True,
            preferred_strategy="search_youtube:dom_input",
            fallback_strategies=[
                "search_youtube:visual_asset",   # PROHIBIDA primaria
                "search_youtube:uia_searchbox",
                "search_youtube:relative_coords",  # PROHIBIDA primaria
            ],
        )
        order = _resolve_strategies(contract.step, contract)
        # Las primeras NO deben ser prohibidas.
        assert not any(
            h in order[0].lower() for h in _FORBIDDEN_PRIMARY_HINTS
        )
        # Las prohibidas, si existen, van al final.
        assert "search_youtube:visual_asset" in order[-2:]
        assert "search_youtube:relative_coords" in order[-2:]

    def test_plan_preferred_strategy_wins_over_contract(self):
        """Cuando ``step.params['preferred_strategy']`` viene del plan,
        esa estrategia va PRIMERO siempre — el LearningLogger no puede
        moverla.
        """
        from app.services.missions.execution_contracts import (
            StepContract, MissionStep,
        )
        from app.services.missions.smart_executor import _resolve_strategies

        step = MissionStep(
            id="x", kind="open_new_tab",
            params={
                "app": "chrome",
                "preferred_strategy": "open_new_tab:hotkey_ctrl_t",
                "fallback_strategies": ["open_new_tab:uia_button"],
            },
        )
        contract = StepContract(
            step=step,
            precondition=lambda s: True,
            postcondition=lambda s: True,
            preferred_strategy="open_new_tab:vision_coords",   # contract roto
            fallback_strategies=[
                "open_new_tab:hotkey_ctrl_t",
                "open_new_tab:uia_button",
            ],
        )
        order = _resolve_strategies(step, contract)
        assert order[0] == "open_new_tab:hotkey_ctrl_t"
        # vision_coords nunca puede aparecer antes que hotkey_ctrl_t.
        assert order.index("open_new_tab:hotkey_ctrl_t") < \
            order.index("open_new_tab:vision_coords")


# ─── 8. Approval gate bloquea profile genérico ────────────────────────


class TestApprovalGateGenericProfile:
    def test_profile_perfil_del_navegador_blocks(self):
        from app.services.missions.approval_gate import check_mission_approvable

        mission = Mission(
            id="m1", name="m",
            compiled_execution_graph=[
                CompiledStep(
                    goal="profile",
                    action_strategy=ActionStrategy.SELECT_PROFILE,
                    action_payload={
                        "profile": "perfil del navegador",
                        "app": "chrome",
                    },
                    target_context=TargetContext(
                        semantic={"intent": "select_profile"},
                        confidence={"capture_score": 90, "quality_level": "green"},
                    ),
                ),
            ],
        )
        report = check_mission_approvable(mission)
        codes = {v.code for v in report.errors or []}
        assert "empty_profile" in codes

    def test_profile_real_name_does_not_block(self):
        from app.services.missions.approval_gate import check_mission_approvable

        mission = Mission(
            id="m2", name="m",
            compiled_execution_graph=[
                CompiledStep(
                    goal="profile",
                    action_strategy=ActionStrategy.SELECT_PROFILE,
                    action_payload={
                        "profile": "Daniel Arturo Ramos",
                        "app": "chrome",
                    },
                    target_context=TargetContext(
                        semantic={"intent": "select_profile"},
                        confidence={"capture_score": 90, "quality_level": "green"},
                    ),
                ),
            ],
        )
        report = check_mission_approvable(mission)
        codes = {v.code for v in report.errors or []}
        assert "empty_profile" not in codes
