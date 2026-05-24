"""
Validación estricta del semantic_execution_plan (PRD 2026-05-07 follow-up).
==========================================================================

Regla conceptual:

    "La evidencia puede ser ruidosa.
     La intención ejecutable debe ser limpia."

El ``compiled_execution_graph`` puede arrastrar clicks/scrolls crudos
porque es evidencia de captura. Pero el ``semantic_execution_plan``
NO puede contener steps legacy adentro — sería contradictorio.

Estos tests garantizan los 7 escenarios canónicos del PRD:

1. Plan limpio aprueba.
2. Compiled graph sucio no afecta si plan está limpio.
3. Plan con ``click`` falla validación estricta.
4. Plan con ``mouse_scroll`` falla validación estricta.
5. Plan con step no-dict falla validación estricta.
6. Plan con dict vacío falla validación estricta.
7. Adapter, Gate y Executor están alineados.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    Mission,
    MissionStatus,
    TargetContext,
    ValidationStrategy,
)
from app.services.missions.approval_gate import (
    check_mission_approvable,
    classify_status,
)
from app.services.missions.semantic_adapter import is_mission_semantic
from app.services.missions.semantic_execution_plan import (
    StrictValidationCode,
    has_valid_semantic_execution_plan,
    validate_semantic_execution_plan_strict,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


_CLEAN_PLAN_STEPS = [
    {
        "id": "s1",
        "type": "open_app",
        "params": {"app": "chrome", "method": "windows_search"},
        "preferred_strategy": "open_app:windows_search",
        "fallback_strategies": ["open_app:os_startfile"],
        "confidence": 0.95,
        "human_label": "Abrir Chrome",
    },
    {
        "id": "s2",
        "type": "open_url",
        "params": {"url": "youtube.com", "alias": "youtube"},
        "preferred_strategy": "open_url:hotkey_ctrl_l",
        "fallback_strategies": [],
        "confidence": 0.95,
        "human_label": "Abrir YouTube",
    },
    {
        "id": "s3",
        "type": "search_youtube",
        "params": {
            "query": "Devuélveme el amor de Luis Miguel",
            "site": "youtube",
            "target": "youtube_search_box",
        },
        "preferred_strategy": "search_youtube:dom_input",
        "fallback_strategies": [
            "search_youtube:uia_searchbox",
            "search_youtube:ocr_buscar",
        ],
        "confidence": 0.95,
        "human_label": "Buscar en YouTube",
    },
]


def _mission_with_plan(steps_blob, *, mid: str = "m") -> Mission:
    m = Mission(id=mid, name="t")
    m.semantic_execution_plan = {
        "version": 1,
        "source": "intent_collapse_engine",
        "average_confidence": 0.92,
        "coords_used": False,
        "legacy_graph_ignored": True,
        "intent_status": "executable",
        "steps": steps_blob,
    }
    return m


def _legacy_compiled_step(action: ActionStrategy, **payload) -> CompiledStep:
    """CompiledStep tipo legacy con click crudo / coords."""
    return CompiledStep(
        action_strategy=action,
        action_payload=payload,
        target_context=TargetContext(process_name="chrome.exe"),
        validation_strategy=ValidationStrategy.NONE,
    )


# ──────────────────────────────────────────────────────────────────────
# 1. Semantic plan limpio aprueba
# ──────────────────────────────────────────────────────────────────────


class TestCleanPlanApproves:
    def test_has_valid_semantic_plan_true(self):
        m = _mission_with_plan(_CLEAN_PLAN_STEPS)
        assert has_valid_semantic_execution_plan(m) is True

    def test_strict_validation_is_valid(self):
        m = _mission_with_plan(_CLEAN_PLAN_STEPS)
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is True
        assert result.violations == []

    def test_approval_gate_approvable(self):
        m = _mission_with_plan(_CLEAN_PLAN_STEPS)
        report = check_mission_approvable(m)
        # No debería haber ninguna violación strict.
        for v in report.errors:
            assert "SEMANTIC_PLAN" not in v.code
            assert "NON_SEMANTIC" not in v.code
            assert "LEGACY_STEP_INSIDE" not in v.code

    def test_status_executable_with_clean_plan(self):
        m = _mission_with_plan(_CLEAN_PLAN_STEPS)
        assert classify_status(m) == MissionStatus.EXECUTABLE

    def test_adapter_recognizes_semantic(self):
        m = _mission_with_plan(_CLEAN_PLAN_STEPS)
        assert is_mission_semantic(m) is True


# ──────────────────────────────────────────────────────────────────────
# 2. Compiled graph sucio no afecta si plan está limpio
# ──────────────────────────────────────────────────────────────────────


class TestDirtyCompiledGraphIgnoredWhenPlanClean:
    def _mission(self) -> Mission:
        m = _mission_with_plan(_CLEAN_PLAN_STEPS)
        # Inyectamos basura en el compiled_execution_graph: click sin
        # target, coords absolutas, etc. — la "evidencia ruidosa" que
        # el PRD dice que ES permitida.
        m.compiled_execution_graph = [
            _legacy_compiled_step(ActionStrategy.CLICK, x=100, y=200),
            _legacy_compiled_step(ActionStrategy.TYPE_TEXT, text="hola"),
            _legacy_compiled_step(ActionStrategy.SCROLL_UNTIL_VISIBLE),
        ]
        return m

    def test_strict_validation_is_valid_despite_dirty_graph(self):
        m = self._mission()
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is True

    def test_approval_gate_does_not_emit_legacy_blockers(self):
        m = self._mission()
        report = check_mission_approvable(m)
        codes = {v.code for v in report.errors}
        assert StrictValidationCode.LEGACY_STEP not in codes
        assert StrictValidationCode.NON_SEMANTIC_STEP not in codes
        # Y no aparece red_step / invalid_click_target del grafo
        # legacy (porque la ruta semántica los ignora).
        assert "red_step" not in codes
        assert "invalid_click_target" not in codes

    def test_bridge_uses_semantic_plan_source(self, monkeypatch):
        from app.services.missions import smart_runner_bridge as bridge
        from app.services.missions.smart_executor import (
            MissionResult,
            SmartMissionExecutor,
            StepOutcome,
        )

        def fake_run(self_, steps, **_kw):
            res = MissionResult(run_id="r", status="success")
            for s in steps:
                res.outcomes.append(StepOutcome(
                    step_id=s.id, kind=s.kind, status="success",
                    strategy_used=s.params.get("preferred_strategy"),
                ))
            return res
        monkeypatch.setattr(SmartMissionExecutor, "run_mission", fake_run)

        m = self._mission()
        decision = bridge.run_mission_smart(
            m,
            allow_legacy_fallback=False,
            use_auto_learning=False,
        )
        assert decision.source == "semantic_execution_plan"
        assert decision.legacy_graph_ignored is True
        assert not decision.blocked
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"


# ──────────────────────────────────────────────────────────────────────
# 3. Semantic plan contaminado con click DEBE fallar validación estricta
# ──────────────────────────────────────────────────────────────────────


class TestPlanWithClickFails:
    def _contaminated_plan(self) -> Mission:
        return _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"type": "click", "x": 100, "y": 200},  # ← contaminación
            {
                "type": "search_youtube",
                "params": {
                    "query": "Devuélveme el amor de Luis Miguel",
                },
            },
        ])

    def test_routing_check_still_passes(self):
        # El routing check sigue True porque hay AL MENOS un step
        # semántico. Eso es esperado: el routing decide la ruta, la
        # validación estricta decide si pasa.
        m = self._contaminated_plan()
        assert has_valid_semantic_execution_plan(m) is True

    def test_strict_validation_fails_with_legacy_code(self):
        m = self._contaminated_plan()
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes
        # El step #1 (índice del click) es el contaminado.
        legacy_v = next(
            v for v in result.violations
            if v.code == StrictValidationCode.LEGACY_STEP
        )
        assert legacy_v.step_index == 1

    def test_approval_gate_does_not_approve(self):
        m = self._contaminated_plan()
        status = classify_status(m)
        assert status == MissionStatus.NEEDS_REVIEW
        report = check_mission_approvable(m)
        codes = {v.code for v in report.errors}
        assert StrictValidationCode.LEGACY_STEP in codes


# ──────────────────────────────────────────────────────────────────────
# 4. Semantic plan contaminado con mouse_scroll DEBE fallar
# ──────────────────────────────────────────────────────────────────────


class TestPlanWithMouseScrollFails:
    def _contaminated_plan(self) -> Mission:
        return _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"type": "mouse_scroll"},  # ← contaminación
            {
                "type": "search_youtube",
                "params": {
                    "query": "Devuélveme el amor de Luis Miguel",
                },
            },
        ])

    def test_strict_validation_fails(self):
        m = self._contaminated_plan()
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes

    def test_blocker_index_points_to_mouse_scroll(self):
        m = self._contaminated_plan()
        result = validate_semantic_execution_plan_strict(m)
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.LEGACY_STEP
        )
        assert v.step_index == 1
        assert "mouse_scroll" in v.message

    def test_bridge_does_not_fall_to_legacy(self, monkeypatch):
        from app.services.missions import smart_runner_bridge as bridge
        from app.services.missions.smart_executor import (
            SmartMissionExecutor,
        )

        called = {"n": 0}

        def fake_run(self_, steps, **_kw):
            called["n"] += 1
            from app.services.missions.smart_executor import (
                MissionResult,
            )
            return MissionResult(run_id="r", status="success")
        monkeypatch.setattr(SmartMissionExecutor, "run_mission", fake_run)

        legacy_player_called = {"n": 0}

        def fake_legacy_player(_m):
            legacy_player_called["n"] += 1
            return None

        m = self._contaminated_plan()
        decision = bridge.run_mission_smart(
            m,
            allow_legacy_fallback=True,
            run_legacy_player=fake_legacy_player,
            use_auto_learning=False,
        )
        # Debe estar bloqueada y NO haber caído al player legacy.
        assert decision.blocked is True
        assert legacy_player_called["n"] == 0
        # El SmartExecutor TAMPOCO debe haberse ejecutado.
        assert called["n"] == 0
        # Y el blocker debe ser semántico, no "legacy_only".
        codes = [b.code for b in decision.blockers]
        assert any(
            c == StrictValidationCode.LEGACY_STEP
            or "SEMANTIC_PLAN" in c
            or c.startswith("gate_status=")
            for c in codes
        ) or decision.reason in (
            "semantic_plan_contaminated",
            "gate_status=needs_review",
        )


# ──────────────────────────────────────────────────────────────────────
# 5. Semantic plan con step no dict DEBE fallar
# ──────────────────────────────────────────────────────────────────────


class TestPlanWithNonDictStepFails:
    def _bad_plan(self) -> Mission:
        return _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            "click basura",  # ← string en vez de dict
            {
                "type": "search_youtube",
                "params": {"query": "x"},
            },
        ])

    def test_strict_validation_fails(self):
        m = self._bad_plan()
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert StrictValidationCode.STEP_NOT_DICT in result.violation_codes

    def test_blocker_has_clear_message(self):
        m = self._bad_plan()
        result = validate_semantic_execution_plan_strict(m)
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.STEP_NOT_DICT
        )
        assert v.step_index == 1
        assert "click basura" in v.message or "no es dict" in v.message

    def test_approval_gate_blocks(self):
        m = self._bad_plan()
        status = classify_status(m)
        assert status == MissionStatus.NEEDS_REVIEW


# ──────────────────────────────────────────────────────────────────────
# 6. Semantic plan con dict vacío DEBE fallar
# ──────────────────────────────────────────────────────────────────────


class TestPlanWithEmptyDictFails:
    def _bad_plan(self) -> Mission:
        return _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {},  # ← dict vacío
            {
                "type": "search_youtube",
                "params": {"query": "x"},
            },
        ])

    def test_strict_validation_fails(self):
        m = self._bad_plan()
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert StrictValidationCode.EMPTY_STEP in result.violation_codes

    def test_blocker_index_correct(self):
        m = self._bad_plan()
        result = validate_semantic_execution_plan_strict(m)
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.EMPTY_STEP
        )
        assert v.step_index == 1

    def test_approval_gate_blocks(self):
        m = self._bad_plan()
        status = classify_status(m)
        assert status == MissionStatus.NEEDS_REVIEW


# ──────────────────────────────────────────────────────────────────────
# 7. Adapter, Gate y Executor alineados
# ──────────────────────────────────────────────────────────────────────


class TestAdapterGateExecutorAlignment:
    """Para una misión limpia y para una contaminada, los tres
    componentes deben dar la MISMA decisión sobre ejecutar o no.
    """

    def test_clean_mission_all_aligned(self, monkeypatch):
        from app.services.missions import smart_runner_bridge as bridge
        from app.services.missions.smart_executor import (
            MissionResult,
            SmartMissionExecutor,
            StepOutcome,
        )

        def fake_run(self_, steps, **_kw):
            res = MissionResult(run_id="r", status="success")
            for s in steps:
                res.outcomes.append(StepOutcome(
                    step_id=s.id, kind=s.kind, status="success",
                ))
            return res
        monkeypatch.setattr(SmartMissionExecutor, "run_mission", fake_run)

        m = _mission_with_plan(_CLEAN_PLAN_STEPS)
        # Adapter dice: semántica
        assert is_mission_semantic(m) is True
        # Gate aprueba
        assert classify_status(m) == MissionStatus.EXECUTABLE
        # Executor usa el plan
        decision = bridge.run_mission_smart(
            m,
            allow_legacy_fallback=False,
            use_auto_learning=False,
        )
        assert not decision.blocked
        assert decision.source == "semantic_execution_plan"

    def test_contaminated_mission_all_aligned(self, monkeypatch):
        from app.services.missions import smart_runner_bridge as bridge
        from app.services.missions.smart_executor import (
            SmartMissionExecutor,
        )

        executor_calls = {"n": 0}

        def fake_run(self_, steps, **_kw):
            executor_calls["n"] += 1
            from app.services.missions.smart_executor import (
                MissionResult,
            )
            return MissionResult(run_id="r", status="success")
        monkeypatch.setattr(SmartMissionExecutor, "run_mission", fake_run)

        legacy_calls = {"n": 0}

        def fake_legacy_player(_m):
            legacy_calls["n"] += 1

        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"type": "click", "x": 100, "y": 200},  # ← contaminación
            {
                "type": "search_youtube",
                "params": {"query": "x"},
            },
        ])
        # Adapter PUEDE reconocer que hay plan semántico (routing).
        assert is_mission_semantic(m) is True
        # Gate NO aprueba.
        assert classify_status(m) == MissionStatus.NEEDS_REVIEW
        # Executor NO cae a legacy automáticamente.
        decision = bridge.run_mission_smart(
            m,
            allow_legacy_fallback=True,
            run_legacy_player=fake_legacy_player,
            use_auto_learning=False,
        )
        assert decision.blocked is True
        assert legacy_calls["n"] == 0, (
            "El bridge cayó al player legacy con plan contaminado — "
            "es exactamente lo que el PRD prohíbe."
        )
        assert executor_calls["n"] == 0, (
            "El SmartExecutor recibió un plan contaminado."
        )


# ──────────────────────────────────────────────────────────────────────
# 8. Casos edge de la blacklist
# ──────────────────────────────────────────────────────────────────────


class TestBlacklistCoverage:
    @pytest.mark.parametrize("legacy_type", [
        "click", "click_button", "double_click", "right_click",
        "mouse_click", "mouse_scroll", "scroll", "key_press",
        "key_press_enter", "type", "type_text", "send_hotkey",
        "coordinate_click", "use_coords", "GroupControl", "guide-service",
        "video-stream", "vision_coords", "relative_coords", "visual_asset",
    ])
    def test_each_legacy_type_blocked(self, legacy_type):
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"type": legacy_type},
            {"type": "search_youtube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False, (
            f'type="{legacy_type}" debería ser rechazado por blacklist'
        )
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes

    def test_blacklist_check_is_case_insensitive(self):
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"type": "  CLICK  "},  # mayúsculas + espacios
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes


# ──────────────────────────────────────────────────────────────────────
# 9. Blacklist debe cubrir type, intent y action por igual
#
# La contaminación legacy puede entrar por cualquiera de las tres
# llaves ejecutables. Si la blacklist solo mira ``type``, basta con
# escribir ``{"action": "click"}`` para colarla.
# ──────────────────────────────────────────────────────────────────────


class TestBlacklistCoversAllExecutableKeys:
    def test_action_click_inside_semantic_plan_fails(self):
        """Caso del PRD: ``{"action": "click", "x": 100, "y": 200}``
        debe fallar aunque no tenga ``type``."""
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"action": "click", "x": 100, "y": 200},
            {"type": "search_youtube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.LEGACY_STEP
        )
        assert v.step_index == 1
        assert 'action="click"' in v.message

    def test_intent_mouse_scroll_inside_semantic_plan_fails(self):
        """Caso del PRD: ``{"intent": "mouse_scroll"}`` debe fallar
        aunque no tenga ``type``."""
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"intent": "mouse_scroll"},
            {"type": "search_youtube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.LEGACY_STEP
        )
        assert v.step_index == 1
        assert 'intent="mouse_scroll"' in v.message

    def test_step_with_semantic_intent_but_legacy_type_fails(self):
        """Caso del PRD más sutil: un step que mezcla intent semántico
        con type legacy. Aunque el ``intent`` sea válido, el ``type``
        legacy lo contamina y debe ir a reparación.
        """
        m = _mission_with_plan([
            {
                "intent": "search_youtube",
                "type": "click",  # ← contradicción legacy
                "query": "Devuélveme el amor de Luis Miguel",
            },
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.LEGACY_STEP
        )
        assert v.step_index == 0
        # El mensaje debe nombrar la llave contaminada (type) para
        # que la UX de reparación pueda apuntar al campo exacto.
        assert 'type="click"' in v.message

    @pytest.mark.parametrize("key", ["type", "intent", "action"])
    @pytest.mark.parametrize("legacy_value", [
        "click", "mouse_scroll", "key_press", "type_text",
        "coordinate_click", "use_coords", "GroupControl", "guide-service",
    ])
    def test_legacy_value_blocked_in_any_key(self, key, legacy_value):
        """Matriz exhaustiva: cualquier valor de la blacklist en
        cualquiera de las llaves ejecutables (type / intent / action)
        debe disparar LEGACY_STEP_INSIDE_SEMANTIC_PLAN."""
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {key: legacy_value},
            {"type": "search_youtube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False, (
            f'{key}="{legacy_value}" debería bloquear pero pasó strict.'
        )
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes

    def test_multiple_legacy_keys_in_same_step_reported_together(self):
        """Si un step trae legacy en varias llaves a la vez,
        el blocker debe nombrarlas todas (para que la reparación
        pueda mostrarlas juntas)."""
        m = _mission_with_plan([
            {"intent": "click", "action": "mouse_scroll"},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.LEGACY_STEP
        )
        assert 'intent="click"' in v.message
        assert 'action="mouse_scroll"' in v.message

    def test_clean_step_with_only_intent_still_passes(self):
        """Sanidad: la nueva regla NO debe romper steps semánticos
        legítimos que solo declaran intent (sin type)."""
        m = _mission_with_plan([
            {"intent": "open_app", "params": {"app": "chrome"}},
            {"intent": "search_youtube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is True
        assert result.violations == []

    def test_clean_step_with_only_action_still_passes(self):
        """Sanidad: lo mismo para steps que solo declaran action."""
        m = _mission_with_plan([
            {"action": "open_app", "params": {"app": "chrome"}},
            {"action": "search_youtube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is True
        assert result.violations == []


# ──────────────────────────────────────────────────────────────────────
# 10. Normalización: variantes de casing/whitespace deben detectarse
#
# La blacklist está en lowercase y la comparación normaliza con
# strip+lower. Nadie debe poder colar legacy con ``"Click"``,
# ``"  click  "`` o ``"MOUSE_SCROLL"``.
# ──────────────────────────────────────────────────────────────────────


class TestNormalizationDefeatsCasingTricks:
    @pytest.mark.parametrize("variant", [
        "Click", "CLICK", "click", " click ", "  Click  ",
        "ClIcK", "\tclick\n",
    ])
    def test_click_casing_variants_blocked_in_type(self, variant):
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"type": variant},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False, (
            f'type="{variant}" debería bloquear (variante de "click")'
        )
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes

    @pytest.mark.parametrize("variant", [
        "MOUSE_SCROLL", "Mouse_Scroll", "mouse_scroll",
        "  MOUSE_SCROLL  ", "Mouse_SCROLL\n",
    ])
    def test_mouse_scroll_casing_variants_blocked_in_action(self, variant):
        m = _mission_with_plan([
            {"action": "open_app", "params": {"app": "chrome"}},
            {"action": variant},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False, (
            f'action="{variant}" debería bloquear '
            '(variante de "mouse_scroll")'
        )
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes

    @pytest.mark.parametrize("variant", [
        "GroupControl", "GROUPCONTROL", "groupcontrol",
        "  GroupControl  ",
    ])
    def test_groupcontrol_casing_variants_blocked_in_intent(self, variant):
        m = _mission_with_plan([
            {"intent": "open_app"},
            {"intent": variant},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert StrictValidationCode.LEGACY_STEP in result.violation_codes

    def test_message_preserves_original_value_for_repair_ux(self):
        """El mensaje debe mostrar el valor original (con casing y
        espacios) para que la UI de reparación pueda señalar
        exactamente qué escribió el usuario/captura."""
        m = _mission_with_plan([
            {"type": "  Click  "},
        ])
        result = validate_semantic_execution_plan_strict(m)
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.LEGACY_STEP
        )
        assert "  Click  " in v.message


# ──────────────────────────────────────────────────────────────────────
# 11. Campos ejecutables con tipo inválido (no-string)
#
# {"type": ["click"]}, {"intent": {"name": "x"}}, {"action": 123} →
# INVALID_EXECUTABLE_FIELD_IN_SEMANTIC_PLAN.
# ──────────────────────────────────────────────────────────────────────


class TestInvalidExecutableFieldType:
    def test_type_as_list_fails(self):
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"type": ["click"]},
            {"type": "search_youtube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert (
            StrictValidationCode.INVALID_EXECUTABLE_FIELD
            in result.violation_codes
        )
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.INVALID_EXECUTABLE_FIELD
        )
        assert v.step_index == 1
        assert "type=<list>" in v.message

    def test_intent_as_dict_fails(self):
        m = _mission_with_plan([
            {"intent": {"name": "search_youtube"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.INVALID_EXECUTABLE_FIELD
        )
        assert v.step_index == 0
        assert "intent=<dict>" in v.message

    def test_action_as_int_fails(self):
        m = _mission_with_plan([
            {"action": 123},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.INVALID_EXECUTABLE_FIELD
        )
        assert "action=<int>" in v.message

    def test_action_as_bool_fails(self):
        # bool es subclase de int en Python; aún así, no es string ⇒ inválido.
        m = _mission_with_plan([{"action": True}])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert (
            StrictValidationCode.INVALID_EXECUTABLE_FIELD
            in result.violation_codes
        )

    def test_multiple_invalid_fields_in_same_step_reported_together(self):
        m = _mission_with_plan([
            {"type": ["click"], "intent": {"name": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.INVALID_EXECUTABLE_FIELD
        )
        assert "type=<list>" in v.message
        assert "intent=<dict>" in v.message

    def test_invalid_field_blocker_goes_to_needs_review(self):
        from app.services.missions.approval_gate import classify_status
        from app.contracts.mission import MissionStatus
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"intent": {"name": "x"}},
        ])
        assert classify_status(m) == MissionStatus.NEEDS_REVIEW

    def test_field_absent_is_fine(self):
        """Sanidad: si la llave no está, no hay violación de tipo."""
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is True

    def test_field_explicit_none_is_fine(self):
        """Sanidad: ``{"type": None}`` no dispara INVALID — la llave
        está vacía, eso lo cubre la regla NON_SEMANTIC_STEP si hace
        falta. La intención del usuario aquí es "no aplica"."""
        m = _mission_with_plan([
            {"type": None, "intent": "open_app"},
        ])
        result = validate_semantic_execution_plan_strict(m)
        # No debe haber INVALID_EXECUTABLE_FIELD porque None se acepta.
        assert (
            StrictValidationCode.INVALID_EXECUTABLE_FIELD
            not in result.violation_codes
        )


# ──────────────────────────────────────────────────────────────────────
# 12. Whitespace / strings vacíos en intent/action no deben pasar
#
# ``{"intent": "   "}`` y ``{"action": ""}`` aparentaban ser
# semánticos pero quedaban vacíos. Ahora ``_normalize_field_value``
# los reduce a ``None`` y ``_has_semantic_step_shape`` los rechaza,
# así que caen como NON_SEMANTIC_STEP.
# ──────────────────────────────────────────────────────────────────────


class TestEmptyOrWhitespaceExecutableFields:
    @pytest.mark.parametrize("intent_value", ["", " ", "   ", "\t", "\n"])
    def test_intent_only_whitespace_falls_as_non_semantic(self, intent_value):
        m = _mission_with_plan([
            {"intent": intent_value, "query": "algo"},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert (
            StrictValidationCode.NON_SEMANTIC_STEP
            in result.violation_codes
        )

    @pytest.mark.parametrize("action_value", ["", " ", "   "])
    def test_action_only_whitespace_falls_as_non_semantic(self, action_value):
        m = _mission_with_plan([
            {"action": action_value, "x": 1, "y": 2},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert (
            StrictValidationCode.NON_SEMANTIC_STEP
            in result.violation_codes
        )

    def test_routing_check_also_rejects_whitespace_only(self):
        """``has_valid_semantic_execution_plan`` (routing) también
        debe rechazar el plan que solo tiene steps con intent/action
        vacíos — antes los aceptaba por la verdad-en-Python de
        ``"   "`` (truthy)."""
        m = _mission_with_plan([
            {"intent": "   "},
            {"action": ""},
        ])
        assert has_valid_semantic_execution_plan(m) is False

    def test_whitespace_with_no_other_semantic_marker_blocks(self):
        """Combinación clásica del PRD:
        ``{"intent": "   ", "query": "algo"}`` — sin type/intent/action
        semántico real, debe bloquear."""
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"intent": "   ", "query": "algo"},
            {"type": "search_youtube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.NON_SEMANTIC_STEP
        )
        assert v.step_index == 1


# ──────────────────────────────────────────────────────────────────────
# 13. Canonicidad de ``type``
#
# Regla de producto: el plan aprobado debe estar limpio Y canónico.
# ``{"type": "OPEN_APP"}`` no se acepta silenciosamente como
# ``open_app`` — debe normalizarse explícitamente antes.
# ──────────────────────────────────────────────────────────────────────


class TestTypeMustBeCanonical:
    @pytest.mark.parametrize("non_canonical", [
        "OPEN_APP", "Open_App", "open_App", " open_app ",
        "  OPEN_APP  ", "Open_App\n",
    ])
    def test_non_canonical_type_blocks(self, non_canonical):
        m = _mission_with_plan([
            {"type": non_canonical, "params": {"app": "chrome"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert (
            StrictValidationCode.NON_CANONICAL_TYPE
            in result.violation_codes
        ), f'type="{non_canonical}" debería disparar NON_CANONICAL_TYPE'

    def test_non_canonical_search_youtube_blocks(self):
        m = _mission_with_plan([
            {"type": "Search_YouTube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        v = next(
            x for x in result.violations
            if x.code == StrictValidationCode.NON_CANONICAL_TYPE
        )
        assert "Search_YouTube" in v.message
        assert "search_youtube" in v.message  # forma canónica sugerida

    def test_canonical_type_passes(self):
        """Sanidad: el lowercase exacto sigue pasando sin warnings."""
        m = _mission_with_plan([
            {"type": "open_app", "params": {"app": "chrome"}},
            {"type": "search_youtube", "params": {"query": "x"}},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is True

    def test_unknown_type_falls_to_non_semantic_not_canonical(self):
        """Si el ``type`` no está en SEMANTIC_TYPES ni siquiera
        normalizado (es decir, es un valor inventado), va a
        NON_SEMANTIC_STEP, no a NON_CANONICAL_TYPE — el problema no
        es de casing, es de vocabulario."""
        m = _mission_with_plan([
            {"type": "do_magic_thing"},
        ])
        result = validate_semantic_execution_plan_strict(m)
        assert result.is_valid is False
        assert (
            StrictValidationCode.NON_SEMANTIC_STEP
            in result.violation_codes
        )
        assert (
            StrictValidationCode.NON_CANONICAL_TYPE
            not in result.violation_codes
        )

    def test_legacy_type_takes_priority_over_canonicity(self):
        """Si ``type`` cae en blacklist (ej ``"CLICK"``), el reporte
        es LEGACY_STEP — no NON_CANONICAL_TYPE. Es información más
        accionable."""
        m = _mission_with_plan([{"type": "CLICK"}])
        result = validate_semantic_execution_plan_strict(m)
        codes = result.violation_codes
        assert StrictValidationCode.LEGACY_STEP in codes
        assert StrictValidationCode.NON_CANONICAL_TYPE not in codes

    def test_non_canonical_blocks_at_approval_gate(self):
        from app.services.missions.approval_gate import classify_status
        from app.contracts.mission import MissionStatus
        m = _mission_with_plan([
            {"type": "OPEN_APP", "params": {"app": "chrome"}},
        ])
        assert classify_status(m) == MissionStatus.NEEDS_REVIEW


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
