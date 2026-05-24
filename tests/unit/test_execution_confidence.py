"""
Tests para Semantic Execution Confidence (PRD 2026-05-06b).

Cubre:

  §1  Tabla de confianzas por kind:
       open_app, select_profile, open_new_tab, open_url,
       search_youtube, scroll_results, hotkey, click.
  §3  ``MissionExecutionReport.can_test`` decide si se puede pulsar
       "Probar misión" — sólo bloquean los hard-blockers.
  §7  Misión completa Chrome→Perfil→Tab→YouTube→Buscar→Scroll:
        - 5 pasos
        - confianza promedio >= 85%
        - máximo 1 confirmación real (perfil)
        - "Probar misión" habilitado
        - approval_gate v2 NO bloquea por capture débil
"""
from __future__ import annotations

from typing import Any, Dict, Optional

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
from app.services.missions.execution_confidence import (
    EXEC_AMBER_THRESHOLD,
    EXEC_GREEN_THRESHOLD,
    ExecLevel,
    ExecStrategy,
    compute_execution_confidence,
    compute_mission_execution_confidence,
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
    capture_score: Optional[float] = None,
    signature: Optional[Dict[str, Any]] = None,
) -> CompiledStep:
    confidence: Dict[str, Any] = {}
    if capture_score is not None:
        confidence["capture_score"] = capture_score
        confidence["quality_level"] = (
            "green" if capture_score >= 80
            else "yellow" if capture_score >= 50
            else "red"
        )
    tc = TargetContext(
        semantic=semantic or {},
        uia_data=uia_data or {},
        web_data=web_data or {},
        process_name=process,
        window_title=title,
        confidence=confidence,
        signature=dict(signature or {}),
    )
    return CompiledStep(
        action_strategy=action,
        action_payload=dict(payload or {}),
        target_context=tc,
        validation_strategy=validation,
    )


def _mission(*steps: CompiledStep) -> Mission:
    m = Mission(name="exec-conf-test", id="exec-conf-mission")
    m.compiled_execution_graph = list(steps)
    m.interpreted_steps = [
        InterpretedStep(description=str(i), raw_event_ids=[])
        for i in range(len(steps))
    ]
    return m


# ──────────────────────────────────────────────────────────────────────
# §1 — Tabla por kind
# ──────────────────────────────────────────────────────────────────────


class TestOpenAppConfidence:
    def test_open_chrome_strong(self):
        cs = _step(
            ActionStrategy.LAUNCH_APP,
            payload={"app_name": "Chrome"},
            semantic={"intent": "open_app"},
        )
        r = compute_execution_confidence(cs)
        assert r.confidence >= 0.90
        assert r.level == ExecLevel.GREEN
        assert r.preferred_strategy == ExecStrategy.WINDOWS_SEARCH
        assert r.is_executable is True

    def test_open_app_without_name_blocks(self):
        cs = _step(
            ActionStrategy.LAUNCH_APP,
            payload={},
            semantic={},
        )
        r = compute_execution_confidence(cs)
        assert r.is_blocking
        assert r.block_code == "empty_app"


class TestSelectProfileConfidence:
    def test_profile_set_strong(self):
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "Daniel Arturo Ramos"},
        )
        r = compute_execution_confidence(cs)
        assert r.confidence >= 0.85
        assert r.level == ExecLevel.GREEN
        assert r.preferred_strategy == ExecStrategy.PROFILE_NAME_MATCH

    def test_profile_empty_with_intent_warns_and_blocks(self):
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "", "needs_user_label": True},
            semantic={"intent": "select_profile"},
        )
        r = compute_execution_confidence(cs)
        assert r.is_blocking
        assert r.block_code == "empty_profile"
        # Pero la UI debería mostrar como warning (amber) — no rojo técnico.
        assert r.level == ExecLevel.AMBER


class TestOpenNewTabConfidence:
    def test_chrome_strong_via_ctrl_t(self):
        cs = _step(
            ActionStrategy.OPEN_NEW_TAB,
            payload={"app": "chrome"},
        )
        r = compute_execution_confidence(cs)
        assert r.confidence >= 0.95
        assert r.level == ExecLevel.GREEN
        assert r.preferred_strategy == ExecStrategy.CTRL_T
        # PRD §1: NO depende de UIA/click/imagen.
        assert r.is_executable is True
        assert not r.is_blocking


class TestOpenUrlConfidence:
    def test_youtube_strong_via_ctrl_l(self):
        cs = _step(
            ActionStrategy.OPEN_BOOKMARK,
            payload={"name": "YouTube", "url": "https://www.youtube.com"},
        )
        r = compute_execution_confidence(cs)
        assert r.confidence >= 0.90
        assert r.level == ExecLevel.GREEN
        assert r.preferred_strategy == ExecStrategy.CTRL_L_URL

    def test_open_url_without_url_or_alias_blocks(self):
        cs = _step(ActionStrategy.OPEN_BOOKMARK, payload={})
        r = compute_execution_confidence(cs)
        assert r.is_blocking
        assert r.block_code == "empty_url"


class TestSearchYoutubeConfidence:
    def test_search_youtube_strong_with_site_query_submit(self):
        cs = _step(
            ActionStrategy.SUBMIT_SEARCH,
            payload={
                "site": "youtube",
                "text": "Devuélveme el amor Luis Miguel",
                "submit_after": True,
                "target_label": "youtube_search_box",
            },
            semantic={"intent": "search_youtube", "site": "youtube"},
        )
        r = compute_execution_confidence(cs)
        assert r.confidence >= 0.95
        assert r.level == ExecLevel.GREEN
        assert r.preferred_strategy == ExecStrategy.DOM_UIA_OCR

    def test_search_without_query_blocks(self):
        cs = _step(
            ActionStrategy.SUBMIT_SEARCH,
            payload={"site": "youtube", "submit_after": True},
            semantic={"intent": "search_youtube"},
        )
        r = compute_execution_confidence(cs)
        assert r.is_blocking
        assert r.block_code == "empty_query"


class TestScrollResultsConfidence:
    def test_scroll_strong(self):
        cs = _step(
            ActionStrategy.SCROLL_UNTIL_VISIBLE,
            payload={"direction": "down"},
            semantic={"intent": "scroll_results", "site": "youtube"},
        )
        r = compute_execution_confidence(cs)
        assert r.confidence >= 0.85
        assert r.level == ExecLevel.GREEN
        assert r.preferred_strategy == ExecStrategy.SCROLL_DYNAMIC


class TestClickConfidence:
    def test_click_with_uia_aid_strong(self):
        cs = _step(
            ActionStrategy.CLICK,
            uia_data={"automation_id": "btnOk", "name": "OK"},
        )
        r = compute_execution_confidence(cs)
        assert r.confidence >= 0.85
        assert r.preferred_strategy == ExecStrategy.UIA_AUTOMATION_ID

    def test_click_without_anything_blocks(self):
        cs = _step(
            ActionStrategy.CLICK,
            uia_data={},
            web_data={},
            semantic={},
        )
        r = compute_execution_confidence(cs)
        assert r.is_blocking
        assert r.block_code == "empty_target"

    def test_promoted_shell_identity_boosts_green(self):
        cs = _step(
            ActionStrategy.CLICK,
            process="explorer.exe",
            uia_data={
                "control_type": "ButtonControl",
                "name": "Start",
                "automation_id": "btnStart",
            },
            signature={"target_source": "semantic_target"},
        )
        r = compute_execution_confidence(cs)
        assert not r.is_blocking
        assert r.level == ExecLevel.GREEN
        assert r.confidence >= EXEC_GREEN_THRESHOLD

    def test_trusted_pre_action_success_transition_bonus(self):
        cs = _step(
            ActionStrategy.CLICK,
            uia_data={
                "name": "Chrome",
                "control_type": "ListItemControl",
            },
            signature={
                "target_source": "uia_control",
                "trusted_pre_action_identity": True,
                "successful_state_transition": True,
            },
        )
        r = compute_execution_confidence(cs)
        notes = " ".join(str(n) for n in (r.notes or []))
        assert "trust:pre_action_successful_transition" in notes
        assert r.confidence >= 0.85


# ──────────────────────────────────────────────────────────────────────
# §3 — can_test = no hard blockers
# ──────────────────────────────────────────────────────────────────────


class TestCanTest:
    def test_clean_mission_can_test(self):
        m = _mission(
            _step(ActionStrategy.LAUNCH_APP, payload={"app_name": "Chrome"}),
            _step(ActionStrategy.OPEN_NEW_TAB, payload={"app": "chrome"}),
        )
        report = compute_mission_execution_confidence(m)
        assert report.can_test is True
        assert report.executable_count == 2
        assert report.blocking_count == 0

    def test_mission_with_unknown_app_cant_test(self):
        m = _mission(
            _step(ActionStrategy.LAUNCH_APP, payload={}),  # empty_app
            _step(ActionStrategy.OPEN_NEW_TAB, payload={"app": "chrome"}),
        )
        report = compute_mission_execution_confidence(m)
        assert report.can_test is False
        assert any(s.block_code == "empty_app" for s in report.steps)


# ──────────────────────────────────────────────────────────────────────
# §7 — Aceptación end-to-end
# ──────────────────────────────────────────────────────────────────────


class TestAcceptance:
    """Misión: Chrome → Perfil → Tab → YouTube → Buscar canción → Scroll."""

    def _build_full_mission(self, *, with_profile: bool = True) -> Mission:
        return _mission(
            _step(
                ActionStrategy.LAUNCH_APP,
                payload={"app_name": "Chrome"},
                semantic={"intent": "open_app"},
                # Capture débil: simulamos misión "real" mal grabada.
                capture_score=45.0,
            ),
            _step(
                ActionStrategy.SELECT_PROFILE,
                payload={
                    "profile": "Daniel Arturo Ramos" if with_profile else "",
                    "needs_user_label": (not with_profile),
                },
                semantic={"intent": "select_profile"},
                validation=ValidationStrategy.REQUIRE_TEXT_MATCH,
                capture_score=40.0,
            ),
            _step(
                ActionStrategy.OPEN_NEW_TAB,
                payload={"app": "chrome"},
                semantic={"intent": "open_new_tab"},
                # Capture muy débil — antes daba "Débil 45%".
                capture_score=30.0,
            ),
            _step(
                ActionStrategy.OPEN_BOOKMARK,
                payload={"name": "YouTube", "url": "https://www.youtube.com"},
                semantic={"intent": "open_url"},
                capture_score=55.0,
            ),
            _step(
                ActionStrategy.SUBMIT_SEARCH,
                payload={
                    "site": "youtube",
                    "text": "Devuélveme el amor Luis Miguel",
                    "submit_after": True,
                    "target_label": "youtube_search_box",
                },
                semantic={"intent": "search_youtube", "site": "youtube"},
                # Antes el target estaba vacío → "Débil 0% sin target".
                capture_score=0.0,
            ),
        )

    def test_5_steps(self):
        m = self._build_full_mission()
        assert len(m.compiled_execution_graph) == 5

    def test_average_confidence_at_least_85(self):
        m = self._build_full_mission()
        report = compute_mission_execution_confidence(m)
        assert report.average >= 0.85, (
            f"average={report.average:.2f}; "
            f"per-step={[s.confidence for s in report.steps]}"
        )

    def test_can_test_enabled(self):
        m = self._build_full_mission()
        report = compute_mission_execution_confidence(m)
        assert report.can_test is True

    def test_executable_count_majority(self):
        m = self._build_full_mission()
        report = compute_mission_execution_confidence(m)
        # Al menos 4 de 5 steps deben ser GREEN. El que falla puede
        # ser select_profile en AMBER si no hay profile, pero con el
        # default with_profile=True debe ser GREEN.
        assert report.executable_count >= 4, (
            f"executable={report.executable_count}; per-step="
            f"{[(s.preferred_strategy, s.level) for s in report.steps]}"
        )

    def test_approval_gate_passes_with_profile(self):
        """PRD §7: con perfil, status = EXECUTABLE."""
        m = self._build_full_mission(with_profile=True)
        # Aunque el capture sea bajo en TODOS los pasos, no debe
        # bloquear gracias al execution_confidence v2.
        status = classify_status(m)
        assert status == MissionStatus.EXECUTABLE, (
            f"violations={[v.code for v in check_mission_approvable(m).errors]}"
        )

    def test_warnings_dont_block(self):
        m = self._build_full_mission(with_profile=True)
        report = check_mission_approvable(m)
        # No debe haber errores. Puede haber warnings informativos
        # (weak_capture_strong_execution).
        assert report.ok, [v.code for v in report.errors]

    def test_only_profile_blocks_when_missing(self):
        m = self._build_full_mission(with_profile=False)
        status = classify_status(m)
        assert status == MissionStatus.NEEDS_REVIEW
        codes = {v.code for v in check_mission_approvable(m).errors}
        # El único motivo de NEEDS_REVIEW debe ser el perfil.
        assert codes & {"empty_profile", "needs_user_label"}, codes

    def test_open_new_tab_not_weak_anymore(self):
        """PRD §7 NEGATIVO: open_new_tab NO debe ser débil bloqueante."""
        m = self._build_full_mission()
        report = compute_mission_execution_confidence(m)
        new_tab = report.steps[2]  # tercera posición
        assert new_tab.preferred_strategy == ExecStrategy.CTRL_T
        assert new_tab.level == ExecLevel.GREEN
        assert new_tab.confidence >= EXEC_GREEN_THRESHOLD
        assert new_tab.is_blocking is False

    def test_search_youtube_not_zero_weak(self):
        """PRD §7 NEGATIVO: search_youtube NO debe aparecer "0% sin target"."""
        m = self._build_full_mission()
        report = compute_mission_execution_confidence(m)
        search = report.steps[4]  # quinta posición
        assert search.preferred_strategy == ExecStrategy.DOM_UIA_OCR
        assert search.level == ExecLevel.GREEN
        assert search.percent >= 95

    def test_open_url_youtube_not_revisar(self):
        """PRD §7 NEGATIVO: open_url YouTube NO debe pedir revisión."""
        m = self._build_full_mission()
        report = compute_mission_execution_confidence(m)
        open_url = report.steps[3]  # cuarta posición
        assert open_url.preferred_strategy == ExecStrategy.CTRL_L_URL
        assert open_url.level == ExecLevel.GREEN


# ──────────────────────────────────────────────────────────────────────
# Persistencia en payload (interop con UI / Approval Gate)
# ──────────────────────────────────────────────────────────────────────


class TestPayloadPersistence:
    def test_step_payload_gets_execution_metadata(self):
        cs = _step(
            ActionStrategy.OPEN_NEW_TAB,
            payload={"app": "chrome"},
        )
        r = compute_execution_confidence(cs)
        pl = cs.action_payload
        assert pl["execution_confidence"] == r.percent
        assert pl["preferred_strategy"] == ExecStrategy.CTRL_T
        assert pl["execution_level"] == ExecLevel.GREEN
        assert "execution_blocker" not in pl

    def test_blocker_is_persisted(self):
        cs = _step(
            ActionStrategy.SUBMIT_SEARCH,
            payload={"site": "youtube", "submit_after": True},  # sin query
        )
        r = compute_execution_confidence(cs)
        assert r.is_blocking
        assert cs.action_payload.get("execution_blocker") == "empty_query"


# ──────────────────────────────────────────────────────────────────────
# Interacción con Ghost (PRD anterior)
# ──────────────────────────────────────────────────────────────────────


class TestGhostInteraction:
    def test_semantic_override_keeps_execution_strong(self):
        cs = _step(
            ActionStrategy.SUBMIT_SEARCH,
            payload={
                "site": "youtube",
                "text": "x",
                "submit_after": True,
                "target_label": "youtube_search_box",
                "ghost_status": "semantic_override",
            },
            semantic={"intent": "search_youtube"},
        )
        r = compute_execution_confidence(cs)
        assert r.level == ExecLevel.GREEN
        assert r.confidence >= EXEC_GREEN_THRESHOLD

    def test_soft_warning_degrades_amber(self):
        cs = _step(
            ActionStrategy.SUBMIT_SEARCH,
            payload={
                "site": "youtube",
                "text": "x",
                "submit_after": True,
                "target_label": "youtube_search_box",
                "ghost_status": "soft_warning",
            },
            semantic={"intent": "search_youtube"},
        )
        r = compute_execution_confidence(cs)
        # Es ámbar, pero sigue ejecutable.
        assert r.level == ExecLevel.AMBER
        assert r.is_executable is True
        assert r.confidence >= EXEC_AMBER_THRESHOLD
