"""
Tests del Semantic Handoff (PRD 2026-05-07).
=============================================

Verifica las 10 reglas obligatorias del handoff semántico:

1.  Toda misión nueva debe guardar ``semantic_execution_plan`` no nulo.
2.  Si ``semantic_execution_plan`` existe, Smart/AutoLearningExecutor
    debe ignorar ``compiled_execution_graph``.
3.  No permitir status executable/compiled limpio si hay rutas /temp/.
4.  ``select_profile`` no puede usar ``profile=""``.
5.  Quick reinforcement debe pedir el perfil una sola vez y guardarlo
    como ``profile="<nombre real>"``.
6.  ``open_new_tab`` debe ejecutarse con Ctrl+T.
7.  ``open_url`` YouTube debe ejecutarse con Ctrl+L.
8.  ``search_youtube`` debe ejecutarse con DOM/UIA/OCR.
9.  Scroll debe convertirse a ``scroll_results``.
10. El grafo legacy queda como ``legacy_debug_only`` para debug.

Criterio de aceptación: una misión "Chrome → perfil → nueva pestaña →
YouTube → buscar → scroll" debe quedar:

  * status=EXECUTABLE (tras refuerzo del perfil),
  * semantic_execution_plan con 5–6 pasos,
  * compiled_execution_graph marcado como legacy_debug_only,
  * 0 rutas /temp/,
  * 0 profile vacío,
  * 0 guide-service ejecutable,
  * 0 use_coords como estrategia principal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from app.contracts.mission import (
    ActionStrategy,
    EventType,
    KeyboardAction,
    Mission,
    MissionStatus,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions.approval_gate import (
    check_mission_approvable,
    classify_status,
)
from app.services.missions.compiler import MissionCompiler


# ──────────────────────────────────────────────────────────────────────
# Helpers — mismo patrón usado en test_intent_collapse_engine.
# ──────────────────────────────────────────────────────────────────────


_BASE_TS = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


def _trace_chrome_youtube_full() -> List[RawEvent]:
    """Trace completo del caso canónico: Win Search → Chrome →
    profile picker → nueva pestaña → bookmark YouTube → query →
    Enter → 2 scrolls.
    """
    base = _BASE_TS

    def _at(off: int) -> datetime:
        return base + timedelta(seconds=off)

    def _click(idx: int, proc: str, title: str) -> RawEvent:
        return RawEvent(
            id=f"e{idx}",
            event_type=EventType.MOUSE_CLICK,
            timestamp=_at(idx),
            window_context=WindowContext(process_name=proc, title=title),
            mouse_action=MouseAction(button="left", x=100 + idx, y=200),
        )

    def _type(idx: int, proc: str, title: str, txt: str,
              sess_id: str = "") -> RawEvent:
        meta = {"final_text": txt}
        if sess_id:
            meta["field_session"] = {"id": sess_id}
        return RawEvent(
            id=f"e{idx}",
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            timestamp=_at(idx),
            window_context=WindowContext(process_name=proc, title=title),
            metadata=meta,
        )

    def _enter(idx: int, proc: str, title: str) -> RawEvent:
        return RawEvent(
            id=f"e{idx}",
            event_type=EventType.KEYBOARD_KEY_PRESS,
            timestamp=_at(idx),
            window_context=WindowContext(process_name=proc, title=title),
            keyboard_action=KeyboardAction(keys=["enter"], modifiers=[]),
        )

    def _scroll(idx: int, proc: str, title: str) -> RawEvent:
        return RawEvent(
            id=f"e{idx}",
            event_type=EventType.MOUSE_SCROLL,
            timestamp=_at(idx),
            window_context=WindowContext(process_name=proc, title=title),
            mouse_action=MouseAction(button="left", x=500, y=400),
            metadata={"count": 1},
        )

    return [
        _click(0, "explorer.exe", ""),
        _click(1, "SearchUI.exe", "Búsqueda"),
        _type(2, "SearchUI.exe", "Búsqueda", "chrome"),
        _click(3, "chrome.exe", "Google Chrome"),
        _click(4, "chrome.exe", "Avances de Nevlan - Google Chrome"),
        _click(5, "chrome.exe", "Nueva pestaña - Google Chrome"),
        _click(6, "chrome.exe", "YouTube - Google Chrome"),
        _type(7, "chrome.exe", "YouTube - Google Chrome", "devu", sess_id="s7"),
        _enter(8, "chrome.exe", "YouTube - Google Chrome"),
        _type(9, "chrome.exe", "YouTube - Google Chrome",
              "devuelveme el amor luis miguel", sess_id="s7"),
        _scroll(10, "chrome.exe",
                "devuelveme el amor luis miguel - YouTube - Google Chrome"),
        _scroll(11, "chrome.exe",
                "devuelveme el amor luis miguel - YouTube - Google Chrome"),
    ]


def _make_mission_compiled() -> Mission:
    """Construye una misión y la pasa por el compiler completo.

    El ``MissionCompiler`` corre el ICE en modo no-destructivo, así
    que la misión sale con:
      * ``compiled_execution_graph`` = grafo legacy (CLICKs/TYPEs).
      * ``semantic_execution_plan`` = plan canónico (open_app /
        select_profile / open_new_tab / open_url / search_content /
        scroll_results) — es la fuente para ejecutar y validar.
    """
    m = Mission(
        id="handoff-test",
        name="Chrome → YouTube → buscar → scroll",
        raw_trace=_trace_chrome_youtube_full(),
    )
    return MissionCompiler.compile_mission(m)


def _confirm_profile_in_plan(mission: Mission, name: str) -> None:
    """Confirma el perfil directo en el ``semantic_execution_plan``
    (puente para tests; en producción lo hace
    ``sync_semantic_plan_after_reinforcement``).
    """
    sep = mission.semantic_execution_plan
    assert sep is not None
    for ps in sep.get("steps", []):
        if ps.get("type") == "select_profile":
            params = ps.setdefault("params", {})
            params["profile"] = name
            params["profile_name"] = name
            ps["needs_user_label"] = False


# ──────────────────────────────────────────────────────────────────────
# §1 — Toda misión nueva debe guardar semantic_execution_plan no nulo.
# ──────────────────────────────────────────────────────────────────────


class TestRule1_PlanIsAlwaysPersisted:
    def test_compile_mission_persists_semantic_plan(self):
        m = _make_mission_compiled()
        assert m.semantic_execution_plan is not None
        steps = m.semantic_execution_plan.get("steps") or []
        assert len(steps) >= 5  # 5–6 pasos esperados

    def test_plan_kinds_canonical(self):
        m = _make_mission_compiled()
        kinds = [s.get("type") for s in m.semantic_execution_plan["steps"]]
        # Debe incluir las 6 intenciones canónicas en orden.
        assert "open_app" in kinds
        assert "select_profile" in kinds
        assert "open_new_tab" in kinds
        assert "open_url" in kinds
        assert "search_content" in kinds
        assert "scroll_results" in kinds


# ──────────────────────────────────────────────────────────────────────
# §10 — compiled_execution_graph queda como legacy_debug_only.
# ──────────────────────────────────────────────────────────────────────


class TestRule10_LegacyGraphMarkedDebugOnly:
    def test_legacy_purpose_after_compile(self):
        m = _make_mission_compiled()
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"

    def test_legacy_purpose_default_is_execution(self):
        m = Mission(id="x", name="raw")
        # Una misión recién creada (sin pasar por el compiler) tiene el
        # default "execution" — el grafo es la fuente para ejecución.
        assert m.legacy_compiled_graph_purpose == "execution"


# ──────────────────────────────────────────────────────────────────────
# §6, §7, §8, §9 — preferred_strategy canónico para cada intent.
# ──────────────────────────────────────────────────────────────────────


class TestRules_6_7_8_9_PreferredStrategies:
    def test_open_new_tab_uses_ctrl_t(self):
        m = _make_mission_compiled()
        ont = next(
            s for s in m.semantic_execution_plan["steps"]
            if s.get("type") == "open_new_tab"
        )
        # PRD §6: open_new_tab debe usar Ctrl+T, no click/coords.
        assert "ctrl_t" in str(ont.get("preferred_strategy", "")).lower()
        for fb in ont.get("fallback_strategies", []):
            assert "use_coords" not in str(fb).lower()
            assert "click_button" not in str(fb).lower()

    def test_open_url_youtube_uses_ctrl_l(self):
        m = _make_mission_compiled()
        ou = next(
            s for s in m.semantic_execution_plan["steps"]
            if s.get("type") == "open_url"
        )
        # PRD §7: open_url YouTube debe usar Ctrl+L + youtube.com.
        assert "ctrl_l" in str(ou.get("preferred_strategy", "")).lower()
        for fb in ou.get("fallback_strategies", []):
            assert "use_coords" not in str(fb).lower()

    def test_search_content_youtube_routes_without_guide_service(self):
        m = _make_mission_compiled()
        sy = next(
            s for s in m.semantic_execution_plan["steps"]
            if s.get("type") == "search_content"
            and str((s.get("params") or {}).get("provider", "")).lower()
            == "youtube"
        )
        # UIC canónico: primaria ``smart_route``; fallbacks incluyen DOM/UIA/OCR.
        ps = str(sy.get("preferred_strategy", "")).lower()
        assert (
            "smart_route" in ps or "dom" in ps or "uia" in ps or "ocr" in ps
        )
        # Y NO guide-service / coords.
        all_strats = [ps] + [str(f).lower() for f in sy.get("fallback_strategies", [])]
        for s in all_strats:
            assert "guide_service" not in s
            assert "guide-service" not in s
            assert "vision_coords" not in s

    def test_scroll_results_uses_wheel_or_keyboard(self):
        m = _make_mission_compiled()
        sr = next(
            s for s in m.semantic_execution_plan["steps"]
            if s.get("type") == "scroll_results"
        )
        # PRD §9: scroll → scroll_results, no mouse_scroll con coords.
        ps = str(sr.get("preferred_strategy", "")).lower()
        assert "wheel" in ps or "keyboard" in ps or "scroll_results" in ps
        for fb in sr.get("fallback_strategies", []):
            assert "use_coords" not in str(fb).lower()


# ──────────────────────────────────────────────────────────────────────
# §4 + §5 — select_profile vacío bloquea, refuerzo lo cura.
# ──────────────────────────────────────────────────────────────────────


class TestRules_4_5_ProfileEnforcement:
    def test_empty_profile_blocks_executable(self):
        m = _make_mission_compiled()
        # Profile picker detectado pero sin OCR → needs_user_label=True.
        # ApprovalGate debe bloquear con empty_profile / needs_user_label.
        report = check_mission_approvable(m)
        codes = {v.code for v in report.errors}
        assert "empty_profile" in codes or "needs_user_label" in codes
        # Status NO puede ser EXECUTABLE.
        assert classify_status(m) != MissionStatus.EXECUTABLE

    def test_real_name_after_reinforcement_makes_executable(self):
        m = _make_mission_compiled()
        _confirm_profile_in_plan(m, "Daniel Arturo Ramos")
        # Ahora el ApprovalGate ya no debe bloquear por perfil.
        report = check_mission_approvable(m)
        codes = {v.code for v in report.errors}
        assert "empty_profile" not in codes
        # Y el plan semántico trae el nombre real.
        ps = next(
            s for s in m.semantic_execution_plan["steps"]
            if s.get("type") == "select_profile"
        )
        assert ps["params"]["profile"] == "Daniel Arturo Ramos"
        assert ps["needs_user_label"] is False

    def test_generic_label_rejected(self):
        from app.interfaces.desktop.quick_reinforcement import (
            _GenericProfileLabelError,
            apply_reinforcement,
        )
        # Sintetizamos un CompiledStep tipo SELECT_PROFILE para probar
        # la validación de etiqueta genérica (no necesita misión real).
        from app.contracts.mission import CompiledStep, TargetContext
        cs = CompiledStep(
            action_strategy=ActionStrategy.SELECT_PROFILE,
            action_payload={"profile": "", "needs_user_label": True},
            target_context=TargetContext(process_name="chrome.exe"),
        )
        with pytest.raises(_GenericProfileLabelError):
            apply_reinforcement(cs, label="perfil del navegador")


# ──────────────────────────────────────────────────────────────────────
# §3 — rutas /temp/ bloquean executable.
# ──────────────────────────────────────────────────────────────────────


class TestRule3_TempPathsBlock:
    def test_temp_path_in_raw_metadata_blocks(self):
        m = _make_mission_compiled()
        # Inyectamos una ruta /assets/temp/ en raw_trace[0].snapshot_ref.
        m.raw_trace[0].snapshot_ref = (
            r"var/missions/assets/temp/step_xxx_small.png"
        )
        report = check_mission_approvable(m)
        codes = {v.code for v in report.errors}
        assert "temp_paths" in codes

    def test_temp_path_in_semantic_plan_params_blocks(self):
        m = _make_mission_compiled()
        # Inyectamos una ruta /assets/temp/ en el plan semántico.
        m.semantic_execution_plan["steps"][0]["params"]["image_ref"] = (
            "var/missions/assets/temp/asset.png"
        )
        report = check_mission_approvable(m)
        codes = {v.code for v in report.errors}
        assert "temp_paths" in codes


# ──────────────────────────────────────────────────────────────────────
# §2 — bridge ignora compiled_graph cuando hay plan semántico.
# ──────────────────────────────────────────────────────────────────────


class TestRule2_BridgeIgnoresLegacyGraph:
    def test_smart_bridge_uses_semantic_plan(self, monkeypatch):
        """Verifica que smart_runner_bridge marca legacy_graph_ignored=True
        cuando el plan semántico está presente y todos sus steps están
        confirmados. Mockea el executor para no tocar SO real.
        """
        from app.services.missions.smart_runner_bridge import (
            run_mission_smart,
        )
        from app.services.missions.smart_executor import (
            SmartMissionExecutor,
            StepOutcome,
        )
        m = _make_mission_compiled()
        # Confirmamos el perfil para que el plan no esté bloqueado.
        _confirm_profile_in_plan(m, "Daniel Arturo Ramos")

        # Mock del executor para no ejecutar realmente.
        def fake_run(self, steps, **_kw):
            return [
                StepOutcome(
                    step_id=s.id, status="ok",
                    strategy_used=str(getattr(s, "preferred_strategy", "")) or "open_app:windows_search",
                    attempts=1, error=None,
                    artifacts={}, evidence=[],
                )
                for s in steps
            ]
        monkeypatch.setattr(SmartMissionExecutor, "run_mission", fake_run)
        # AutoLearningExecutor también puede usarse — mockéalo si existe.
        try:
            from app.services.missions.auto_learning_executor import (
                AutoLearningExecutor,
            )
            monkeypatch.setattr(AutoLearningExecutor, "run_mission", fake_run)
        except Exception:
            pass

        decision = run_mission_smart(m, allow_legacy_fallback=False)
        assert decision.source == "semantic_execution_plan", (
            f"reason={decision.reason} blocked={decision.blocked} "
            f"blockers={[b.code for b in (decision.blockers or [])]}"
        )
        assert decision.legacy_graph_ignored is True
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"


# ──────────────────────────────────────────────────────────────────────
# Acceptance integral
# ──────────────────────────────────────────────────────────────────────


class TestAcceptanceFullHandoff:
    def test_after_reinforcement_status_executable_and_clean(self):
        m = _make_mission_compiled()
        _confirm_profile_in_plan(m, "Daniel Arturo Ramos")

        status = classify_status(m)
        report = check_mission_approvable(m)

        # Acceptance criteria:
        assert status == MissionStatus.EXECUTABLE, (
            f"Esperaba EXECUTABLE; obtuve {status} con violaciones: "
            f"{[v.code for v in report.errors]}"
        )
        # 5–6 pasos.
        steps = m.semantic_execution_plan["steps"]
        assert 5 <= len(steps) <= 6
        # Legacy debug only.
        assert m.legacy_compiled_graph_purpose == "legacy_debug_only"
        # 0 perfil vacío.
        for s in steps:
            if s.get("type") == "select_profile":
                assert s["params"]["profile"]
        # 0 use_coords como estrategia principal.
        for s in steps:
            ps = str(s.get("preferred_strategy", "")).lower()
            assert "use_coords" not in ps
            assert "vision_coords" not in ps
        # 0 guide-service ejecutable.
        for s in steps:
            ps = str(s.get("preferred_strategy", "")).lower()
            assert "guide_service" not in ps
            assert "guide-service" not in ps


# ──────────────────────────────────────────────────────────────────────
# Whitelist semántica (PRD 2026-05-07 follow-up).
# ──────────────────────────────────────────────────────────────────────


class TestSemanticWhitelist:
    """``has_valid_semantic_execution_plan`` debe rechazar planes
    contaminados con steps técnicos legacy y aceptar solo planes con
    intenciones reconocibles.
    """

    def _mission_with_plan(self, steps_blob):
        m = Mission(id="wl", name="t")
        m.semantic_execution_plan = {"version": 1, "steps": steps_blob}
        return m

    def test_rejects_pure_legacy_click_steps(self):
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = self._mission_with_plan([
            {"type": "click"},
            {"type": "mouse_scroll"},
            {"type": "key_press"},
        ])
        assert has_valid_semantic_execution_plan(m) is False

    def test_rejects_empty_steps(self):
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = self._mission_with_plan([])
        assert has_valid_semantic_execution_plan(m) is False

    def test_rejects_steps_without_type_or_intent(self):
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = self._mission_with_plan([{"params": {"foo": "bar"}}])
        assert has_valid_semantic_execution_plan(m) is False

    def test_accepts_canonical_step(self):
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = self._mission_with_plan([
            {"type": "open_app", "params": {"name": "chrome"}},
        ])
        assert has_valid_semantic_execution_plan(m) is True

    def test_accepts_intent_field_even_without_canonical_type(self):
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = self._mission_with_plan([
            {"intent": "search_youtube", "query": "Devuélveme el amor"},
        ])
        assert has_valid_semantic_execution_plan(m) is True

    def test_accepts_action_field(self):
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = self._mission_with_plan([
            {"action": "open_url", "url": "youtube.com"},
        ])
        assert has_valid_semantic_execution_plan(m) is True

    def test_mixed_legacy_and_canonical_accepts_when_at_least_one_valid(self):
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = self._mission_with_plan([
            {"type": "click"},
            {"type": "open_app", "params": {"name": "chrome"}},
        ])
        assert has_valid_semantic_execution_plan(m) is True

    def test_no_plan_at_all_returns_false(self):
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = Mission(id="empty", name="t")
        assert has_valid_semantic_execution_plan(m) is False

    def test_semantic_types_includes_canonical_set(self):
        from app.services.missions.semantic_execution_plan import (
            SEMANTIC_TYPES,
        )
        for t in (
            "open_app", "select_profile", "open_new_tab",
            "open_url", "search_youtube", "search_content", "scroll_results",
            "wait_for", "confirm",
        ):
            assert t in SEMANTIC_TYPES, f"falta tipo canónico: {t}"
        # Y NO acepta tipos técnicos sueltos.
        for t in ("click", "mouse_scroll", "key_press", "type_text"):
            assert t not in SEMANTIC_TYPES


class TestAdapterAndGateAgreeOnSemanticDecision:
    """Garantía clave: nunca debe pasar que el adapter diga
    "es semántica" y el gate diga "no es semántica" (o viceversa).
    """

    def test_pure_legacy_plan_not_semantic_anywhere(self):
        from app.services.missions.semantic_adapter import (
            is_mission_semantic,
        )
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = Mission(id="lg", name="t")
        m.semantic_execution_plan = {
            "version": 1,
            "steps": [{"type": "click"}, {"type": "mouse_scroll"}],
        }
        # Ambos lo rechazan.
        assert has_valid_semantic_execution_plan(m) is False
        # is_mission_semantic puede aún devolver True si el grafo
        # legacy tiene LAUNCH_APP, pero NO porque vea el plan basura.
        # Si no hay grafo legacy, debería ser False.
        assert is_mission_semantic(m) is False

    def test_canonical_plan_recognized_by_adapter(self):
        from app.services.missions.semantic_adapter import (
            is_mission_semantic,
        )
        from app.services.missions.semantic_execution_plan import (
            has_valid_semantic_execution_plan,
        )
        m = Mission(id="cn", name="t")
        m.semantic_execution_plan = {
            "version": 1,
            "steps": [
                {"type": "open_app", "params": {"name": "chrome"}},
                {"type": "search_youtube", "params": {"query": "x"}},
            ],
        }
        assert has_valid_semantic_execution_plan(m) is True
        assert is_mission_semantic(m) is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
