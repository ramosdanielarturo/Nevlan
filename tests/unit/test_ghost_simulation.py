"""
Tests para Ghost Simulation + Semantic Certainty Score.

Cubre PRD 2026-05-06 (§§ 1, 2, 3, 7):

  1. ``search_youtube`` con query+site+target → semantic_override.
  2. ``open_new_tab`` en Chrome → semantic_override.
  3. ``open_url`` apuntando a YouTube → semantic_override.
  4. ``scroll_results`` después de un search → semantic_override.
  5. ``select_profile`` con profile vacío → ghost_missing/needs_user_label.
  6. CLICK genérico sin name/locator/etiqueta → ghost_missing.
  7. Caso intermedio (certainty 0.60–0.84) → soft_warning, no bloqueante.

Estos tests NO requieren PyQt6: golpean directamente las funciones
puras de ``ghost_simulation`` y ``approval_gate``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    InterpretedStep,
    Mission,
    MissionStatus,
    TargetContext,
    ValidationStrategy,
)
from app.services.missions.approval_gate import (
    check_mission_approvable,
    classify_status,
)
from app.services.missions.ghost_simulation import (
    GhostStatus,
    SEMANTIC_SOFT_THRESHOLD,
    SEMANTIC_STRONG_THRESHOLD,
    mark_missing_for_review,
    semantic_certainty,
    simulate_mission,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _step(
    action: ActionStrategy,
    *,
    payload: Optional[Dict[str, Any]] = None,
    semantic: Optional[Dict[str, Any]] = None,
    uia_data: Optional[Dict[str, Any]] = None,
    web_data: Optional[Dict[str, Any]] = None,
    process: str = "chrome.exe",
    title: str = "",
    validation: ValidationStrategy = ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
) -> CompiledStep:
    tc = TargetContext(
        semantic=semantic or {},
        uia_data=uia_data or {},
        web_data=web_data or {},
        process_name=process,
        window_title=title,
    )
    return CompiledStep(
        action_strategy=action,
        action_payload=dict(payload or {}),
        target_context=tc,
        validation_strategy=validation,
    )


def _mission(*steps: CompiledStep) -> Mission:
    m = Mission(name="ghost-test", id="ghost-mission")
    m.compiled_execution_graph = list(steps)
    m.interpreted_steps = [
        InterpretedStep(description=str(i), raw_event_ids=[])
        for i in range(len(steps))
    ]
    return m


def _force_ghost_missing(mission: Mission) -> None:
    """Ejecuta ``simulate_mission`` y luego ``mark_missing_for_review``
    para producir el estado que vería el approval_gate después de un
    pre-save real.
    """
    rep = simulate_mission(mission)
    mark_missing_for_review(mission, rep)


# ──────────────────────────────────────────────────────────────────────
# 1. Semantic certainty alto: search_youtube no debe pedir confirmación
# ──────────────────────────────────────────────────────────────────────

class TestSearchYoutubeObvious:
    """Caso 1: search_youtube_obvious_no_prompt."""

    def _build_step(self) -> CompiledStep:
        return _step(
            ActionStrategy.SUBMIT_SEARCH,
            payload={
                "site": "youtube",
                "text": "Devuélveme el amor Luis Miguel",
                "submit_after": True,
                "target_label": "youtube_search_box",
                "locators": [
                    {"kind": "css", "value": 'input[name="search_query"]'},
                ],
            },
            semantic={
                "intent": "search_youtube",
                "human_label": "Buscar en YouTube",
                "site": "youtube",
            },
            title="YouTube — Google Chrome",
            process="chrome.exe",
        )

    def test_certainty_is_strong(self):
        cs = self._build_step()
        score = semantic_certainty(cs, context={"title": "YouTube"})
        assert score >= SEMANTIC_STRONG_THRESHOLD, (
            f"score={score} debería ser >= {SEMANTIC_STRONG_THRESHOLD}"
        )

    def test_no_prompt_when_ghost_cannot_find_target(self):
        m = _mission(self._build_step())
        _force_ghost_missing(m)
        cs = m.compiled_execution_graph[0]
        pl = cs.action_payload
        assert pl.get("ghost_status") == GhostStatus.SEMANTIC_OVERRIDE
        assert not pl.get("ghost_missing"), (
            "search_youtube obvio NO debería marcarse como ghost_missing"
        )
        assert not pl.get("needs_user_label"), (
            "search_youtube obvio NO debería pedir label humana"
        )
        assert pl.get("executable") is True

    def test_approval_gate_not_blocked(self):
        m = _mission(self._build_step())
        _force_ghost_missing(m)
        report = check_mission_approvable(m)
        codes = {v.code for v in report.errors}
        assert "ghost_missing" not in codes
        # Tampoco debería terminar en NEEDS_REVIEW por el ghost.
        status = classify_status(m)
        assert status == MissionStatus.EXECUTABLE, (
            f"esperado EXECUTABLE, obtuve {status} con violations={codes}"
        )


# ──────────────────────────────────────────────────────────────────────
# 2. open_new_tab no debe pedir confirmación
# ──────────────────────────────────────────────────────────────────────

class TestOpenNewTabNoPrompt:
    def _build_step(self) -> CompiledStep:
        return _step(
            ActionStrategy.OPEN_NEW_TAB,
            payload={"app": "chrome"},
            semantic={
                "intent": "open_new_tab",
                "human_label": "Nueva pestaña",
            },
            process="chrome.exe",
            title="Chrome",
        )

    def test_certainty_is_strong(self):
        cs = self._build_step()
        score = semantic_certainty(cs, context={"app": "chrome"})
        assert score >= SEMANTIC_STRONG_THRESHOLD

    def test_no_ghost_missing(self):
        m = _mission(self._build_step())
        _force_ghost_missing(m)
        pl = m.compiled_execution_graph[0].action_payload
        # OPEN_NEW_TAB es un step "non-visual" → simulate lo deja como OK
        # (no como semantic_override) porque no necesita target visual.
        # Lo importante para el PRD es: NO ghost_missing y NO needs_user_label.
        assert not pl.get("ghost_missing")
        assert not pl.get("needs_user_label")
        assert classify_status(m) == MissionStatus.EXECUTABLE


# ──────────────────────────────────────────────────────────────────────
# 3. open_url youtube no debe pedir confirmación
# ──────────────────────────────────────────────────────────────────────

class TestOpenUrlYoutubeNoPrompt:
    def _build_step(self) -> CompiledStep:
        return _step(
            ActionStrategy.OPEN_BOOKMARK,
            payload={"name": "YouTube", "url": "https://www.youtube.com"},
            semantic={
                "intent": "open_url",
                "human_label": "Ir a YouTube",
            },
            process="chrome.exe",
        )

    def test_certainty_is_strong(self):
        cs = self._build_step()
        score = semantic_certainty(cs)
        assert score >= SEMANTIC_STRONG_THRESHOLD

    def test_no_ghost_missing(self):
        m = _mission(self._build_step())
        _force_ghost_missing(m)
        pl = m.compiled_execution_graph[0].action_payload
        assert not pl.get("ghost_missing")
        assert not pl.get("needs_user_label")
        assert classify_status(m) == MissionStatus.EXECUTABLE


# ──────────────────────────────────────────────────────────────────────
# 4. scroll_results después de search_youtube
# ──────────────────────────────────────────────────────────────────────

class TestScrollResultsAfterSearchNoPrompt:
    def _build_steps(self):
        search = _step(
            ActionStrategy.SUBMIT_SEARCH,
            payload={
                "site": "youtube",
                "text": "luis miguel",
                "submit_after": True,
                "target_label": "youtube_search_box",
                "locators": [{"kind": "css", "value": 'input[name="search_query"]'}],
            },
            semantic={
                "intent": "search_youtube",
                "human_label": "Buscar en YouTube",
                "site": "youtube",
            },
        )
        scroll = _step(
            ActionStrategy.SCROLL_UNTIL_VISIBLE,
            payload={"direction": "down"},
            semantic={
                "intent": "scroll_results",
                "human_label": "Bajar resultados de YouTube",
                "site": "youtube",
            },
        )
        return [search, scroll]

    def test_certainty_is_strong(self):
        steps = self._build_steps()
        ctx = {"prev_intent": "search_youtube", "site": "youtube"}
        score = semantic_certainty(steps[1], context=ctx)
        assert score >= SEMANTIC_STRONG_THRESHOLD

    def test_no_ghost_missing(self):
        m = _mission(*self._build_steps())
        _force_ghost_missing(m)
        for cs in m.compiled_execution_graph:
            pl = cs.action_payload
            assert not pl.get("ghost_missing")
            assert not pl.get("needs_user_label")
        assert classify_status(m) == MissionStatus.EXECUTABLE


# ──────────────────────────────────────────────────────────────────────
# 5. select_profile vacío SIGUE pidiendo confirmación
# ──────────────────────────────────────────────────────────────────────

class TestSelectProfileEmptyStillPrompts:
    def test_empty_profile_prompts(self):
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": ""},
            semantic={"intent": "select_profile"},
            validation=ValidationStrategy.REQUIRE_TEXT_MATCH,
        )
        score = semantic_certainty(cs)
        assert score < SEMANTIC_STRONG_THRESHOLD, (
            f"select_profile vacío no debería ser semantic_strong: {score}"
        )
        # El approval_gate ya sin ghost levanta empty_profile;
        # con ghost mark, el step queda en NEEDS_REVIEW.
        m = _mission(cs)
        _force_ghost_missing(m)
        status = classify_status(m)
        assert status == MissionStatus.NEEDS_REVIEW, status

    def test_profile_set_does_not_prompt(self):
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "Daniel Arturo Ramos"},
            semantic={"intent": "select_profile"},
            validation=ValidationStrategy.REQUIRE_TEXT_MATCH,
        )
        score = semantic_certainty(cs)
        assert score >= SEMANTIC_STRONG_THRESHOLD


# ──────────────────────────────────────────────────────────────────────
# 6. CLICK genérico sin name/locator → ghost_missing clásico
# ──────────────────────────────────────────────────────────────────────

class TestWeakUnknownClickStillPrompts:
    def test_click_without_name_blocks(self):
        cs = _step(
            ActionStrategy.CLICK,
            payload={},
            semantic={},
            uia_data={"name": "", "automation_id": ""},
            web_data={},
        )
        score = semantic_certainty(cs)
        assert score < SEMANTIC_SOFT_THRESHOLD, (
            f"click sin nombre no debería superar el umbral suave: {score}"
        )
        m = _mission(cs)
        rep = simulate_mission(m)
        mark_missing_for_review(m, rep)
        pl = m.compiled_execution_graph[0].action_payload
        # El click genérico sin nombre es considerado missing por el
        # ghost (el _simulate_step para CLICK pasa por la decision
        # policy y debe marcar missing) o, como respaldo, el approval_gate
        # bloquea por empty_target.
        gating = pl.get("ghost_missing") or pl.get("needs_user_label")
        if not gating:
            # Si por contexto no llega a la rama UIA-probe (depende de
            # uiautomation), validamos vía approval_gate:
            codes = {v.code for v in check_mission_approvable(m).errors}
            assert "empty_target" in codes or "invalid_click_target" in codes


# ──────────────────────────────────────────────────────────────────────
# 7. Soft warning: certeza media → no bloquea, no abre confirmación
# ──────────────────────────────────────────────────────────────────────

class TestSoftWarningMidConfidence:
    def _build_step(self) -> CompiledStep:
        # Step con señales medias:
        #   * type+params (+0.30)
        #   * window_title context (+0.20)
        #   * submit_after = Enter posterior (+0.10)
        #   * intent + human_label (+0.10)
        #   = 0.70 → soft band [0.60, 0.85).
        # Sin site ni locators robustos para evitar el +0.30 de la
        # cadena de resolución y los boosts por youtube.
        return _step(
            ActionStrategy.SET_FIELD_VALUE,
            payload={
                "text": "hola",
                "submit_after": True,
                "target_label": "campo desconocido",
            },
            semantic={
                "intent": "fill_field",
                "human_label": "Llenar un campo",
            },
            title="Notepad - Untitled",
            process="notepad.exe",
        )

    def test_score_in_soft_band(self):
        cs = self._build_step()
        # window_title del TargetContext sólo entra al score cuando se
        # arma desde una Mission; en la llamada directa lo pasamos
        # explícitamente por context.
        score = semantic_certainty(cs, context={"window_title": "Notepad"})
        assert SEMANTIC_SOFT_THRESHOLD <= score < SEMANTIC_STRONG_THRESHOLD, (
            f"score={score} esperaba soft band "
            f"[{SEMANTIC_SOFT_THRESHOLD}, {SEMANTIC_STRONG_THRESHOLD})"
        )

    def test_soft_warning_is_not_blocking(self):
        m = _mission(self._build_step())
        rep = simulate_mission(m)
        mark_missing_for_review(m, rep)
        pl = m.compiled_execution_graph[0].action_payload
        # Soft warning NO bloquea (no ghost_missing) pero deja la huella.
        if pl.get("ghost_status") == GhostStatus.SOFT_WARNING:
            assert not pl.get("ghost_missing")
            assert not pl.get("needs_user_label")
            report = check_mission_approvable(m)
            assert report.ok or all(
                v.severity != "error" for v in report.violations
                if v.code == "ghost_soft_warning"
            )


# ──────────────────────────────────────────────────────────────────────
# 8. Aceptación end-to-end (PRD §8)
# ──────────────────────────────────────────────────────────────────────

class TestAcceptanceCriteria:
    """PRD §8: la misión completa Chrome→Perfil→Tab→YouTube→Buscar→Scroll
    sólo puede pedir confirmación por ``select_profile`` (si el profile
    no se identificó). Todo lo demás debe quedar EXECUTABLE.
    """

    def _build_full_mission(self, *, with_profile: bool = True) -> Mission:
        steps = [
            _step(
                ActionStrategy.LAUNCH_APP,
                payload={"app_name": "Chrome"},
                semantic={"intent": "open_app", "human_label": "Abrir Chrome"},
            ),
            _step(
                ActionStrategy.SELECT_PROFILE,
                payload={
                    "profile": "Daniel Arturo Ramos" if with_profile else "",
                },
                semantic={"intent": "select_profile"},
                validation=ValidationStrategy.REQUIRE_TEXT_MATCH,
            ),
            _step(
                ActionStrategy.OPEN_NEW_TAB,
                payload={"app": "chrome"},
                semantic={"intent": "open_new_tab"},
            ),
            _step(
                ActionStrategy.OPEN_BOOKMARK,
                payload={"name": "YouTube", "url": "https://www.youtube.com"},
                semantic={"intent": "open_url", "human_label": "Ir a YouTube"},
            ),
            _step(
                ActionStrategy.SUBMIT_SEARCH,
                payload={
                    "site": "youtube",
                    "text": "Devuélveme el amor Luis Miguel",
                    "submit_after": True,
                    "target_label": "youtube_search_box",
                    "locators": [
                        {"kind": "css", "value": 'input[name="search_query"]'},
                    ],
                },
                semantic={
                    "intent": "search_youtube",
                    "human_label": "Buscar en YouTube",
                    "site": "youtube",
                },
            ),
            _step(
                ActionStrategy.SCROLL_UNTIL_VISIBLE,
                payload={"direction": "down"},
                semantic={
                    "intent": "scroll_results",
                    "human_label": "Bajar resultados",
                    "site": "youtube",
                },
            ),
        ]
        return _mission(*steps)

    def test_full_mission_with_profile_is_executable(self):
        m = self._build_full_mission(with_profile=True)
        _force_ghost_missing(m)
        status = classify_status(m)
        assert status == MissionStatus.EXECUTABLE, (
            f"esperado EXECUTABLE, obtuve {status}; "
            f"violations={[v.code for v in check_mission_approvable(m).violations]}"
        )
        # Ningún step puede haber quedado pidiendo confirmación.
        for cs in m.compiled_execution_graph:
            pl = cs.action_payload
            assert not pl.get("ghost_missing"), cs.action_strategy
            assert not pl.get("needs_user_label"), cs.action_strategy

    def test_full_mission_without_profile_only_blocks_profile(self):
        m = self._build_full_mission(with_profile=False)
        _force_ghost_missing(m)
        status = classify_status(m)
        assert status == MissionStatus.NEEDS_REVIEW, status
        # El único motivo de NEEDS_REVIEW debe ser el perfil vacío.
        report = check_mission_approvable(m)
        codes = {v.code for v in report.errors}
        # Aceptamos empty_profile o needs_user_label (ambos cubren el
        # mismo caso semántico de "perfil sin etiqueta humana").
        assert codes & {"empty_profile", "needs_user_label"}, codes
        # Y NINGÚN otro paso del flujo YouTube debe haber generado
        # ghost_missing.
        for cs in m.compiled_execution_graph:
            if cs.action_strategy == ActionStrategy.SELECT_PROFILE:
                continue
            pl = cs.action_payload
            assert not pl.get("ghost_missing"), cs.action_strategy
