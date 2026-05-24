"""
Unit tests para ``app.services.missions.semantic_fusion``.

Cubren los tres passes (PRD 2026-05-03 §A/§B/§C):

  * Invalid Target Filter — rescata clicks sobre contenedores genéricos
    fundiéndolos con el siguiente texto en campo, o los conserva para
    que el ``approval_gate`` los bloquee.
  * Chrome Workflow — Win Search rescue, select_profile, open_new_tab,
    open_bookmark.
  * Field Session Merger v2 — fragmentos separados por Enter accidental.
"""
from __future__ import annotations

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    InterpretedStep,
    TargetContext,
    ValidationStrategy,
)
from app.services.missions.semantic_fusion import (
    apply_semantic_fusion,
    confirm_step_label,
    maybe_promote_click_fallback_identity,
    pass_chrome_workflow,
    pass_drop_redundant_focus_clicks,
    pass_field_session_merger,
    pass_invalid_target_filter,
    pass_red_step_guard,
    pass_search_finalizer,
    _bbox_is_pathological,
    _is_invalid_click_target,
    _is_red_or_fallback_click,
    _is_technical_name,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _step(action: ActionStrategy, *,
          uia_name: str = "", uia_aid: str = "",
          uia_ctype: str = "ButtonControl",
          process: str = "chrome.exe",
          window_title: str = "",
          payload: dict | None = None,
          web: dict | None = None,
          uia_bbox: dict | None = None,
          target_source: str = "uia",
          quality_level: str = "high",
          ) -> CompiledStep:
    uia = {
        "name": uia_name,
        "automation_id": uia_aid,
        "control_type": uia_ctype,
    }
    if uia_bbox is not None:
        uia["bbox"] = uia_bbox
    tc = TargetContext(
        uia_data=uia,
        web_data=web or {},
        process_name=process,
        window_title=window_title,
        anchor_bbox={"left": 0, "top": 0, "width": 100, "height": 30},
        confidence={"quality_level": quality_level},
        signature={"target_source": target_source},
    )
    return CompiledStep(
        action_strategy=action,
        action_payload=payload or {},
        target_context=tc,
        validation_strategy=ValidationStrategy.NONE,
    )


def _ispep(desc: str = "") -> InterpretedStep:
    return InterpretedStep(description=desc, raw_event_ids=[])


# ──────────────────────────────────────────────────────────────────────
# Pass A: Invalid Target Filter
# ──────────────────────────────────────────────────────────────────────

class TestInvalidTargetFilter:
    def test_keeps_click_with_uia_name(self):
        s = _step(ActionStrategy.CLICK, uia_name="Save", uia_aid="btnSave")
        out_c, out_i = pass_invalid_target_filter([s], [_ispep()])
        assert len(out_c) == 1
        assert out_c[0].action_strategy == ActionStrategy.CLICK

    def test_drops_invalid_click_when_followed_by_field_text(self):
        # Click en GroupControl vacío → siguiente paso es type_text en el
        # mismo proceso → rescate (descarta el click, pasa contexto).
        click = _step(
            ActionStrategy.CLICK,
            uia_name="", uia_aid="guide-service",
            uia_ctype="GroupControl",
        )
        text = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "hello"},
        )
        out_c, _ = pass_invalid_target_filter(
            [click, text], [_ispep(), _ispep()],
        )
        assert len(out_c) == 1
        assert out_c[0].action_strategy == ActionStrategy.TYPE_TEXT
        assert out_c[0].action_payload.get("fused_from_invalid_click") is True

    def test_keeps_invalid_click_when_no_rescue_available(self):
        # Sin siguiente texto → conservamos el click para que approval_gate
        # lo bloquee (no eliminamos silenciosamente la única acción).
        s = _step(
            ActionStrategy.CLICK,
            uia_name="", uia_aid="",
            uia_ctype="GroupControl",
        )
        out_c, _ = pass_invalid_target_filter([s], [_ispep()])
        assert len(out_c) == 1
        assert out_c[0].action_strategy == ActionStrategy.CLICK

    def test_drops_click_with_technical_aid(self):
        # automation_id 'guide-service' es técnico → rescatable o conservable
        # según contexto. Sin rescate → conservamos.
        s = _step(
            ActionStrategy.CLICK,
            uia_aid="guide-service",
            uia_ctype="GroupControl",
        )
        out_c, _ = pass_invalid_target_filter([s], [_ispep()])
        assert len(out_c) == 1  # no rescue, kept

    def test_does_not_drop_non_click(self):
        s = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "x"},
        )
        out_c, _ = pass_invalid_target_filter([s], [_ispep()])
        assert len(out_c) == 1


# ──────────────────────────────────────────────────────────────────────
# Pass B: Chrome Workflow
# ──────────────────────────────────────────────────────────────────────

class TestChromeWorkflow:
    def test_drops_click_on_win_search_box_before_launch_app(self):
        click_search = _step(
            ActionStrategy.CLICK,
            uia_name="Escribe aquí para buscar.",
            uia_aid="4101",
            process="explorer.exe",
        )
        launch = _step(
            ActionStrategy.LAUNCH_APP,
            uia_name="Google Chrome, Aplicación de escritorio",
            payload={"app_name": "Chrome", "launch_method": "windows_search"},
            process="SearchUI.exe",
        )
        out_c, _ = pass_chrome_workflow(
            [click_search, launch], [_ispep(), _ispep()],
        )
        assert len(out_c) == 1
        assert out_c[0].action_strategy == ActionStrategy.LAUNCH_APP

    def test_converts_first_post_launch_chrome_click_to_select_profile(self):
        launch = _step(
            ActionStrategy.LAUNCH_APP,
            payload={"app_name": "Chrome"},
            process="SearchUI.exe",
        )
        profile_click = _step(
            ActionStrategy.CLICK,
            uia_name="", uia_aid="",
            uia_ctype="GroupControl",
            process="chrome.exe",
            window_title="Google Chrome",
        )
        new_tab = _step(
            ActionStrategy.CLICK,
            uia_name="Nueva pestaña",
            process="chrome.exe",
            window_title="Avances - Google Chrome",
        )
        out_c, _ = pass_chrome_workflow(
            [launch, profile_click, new_tab],
            [_ispep(), _ispep(), _ispep()],
        )
        assert out_c[0].action_strategy == ActionStrategy.LAUNCH_APP
        assert out_c[1].action_strategy == ActionStrategy.SELECT_PROFILE
        assert out_c[1].action_payload.get("app") == "chrome"
        assert out_c[2].action_strategy == ActionStrategy.OPEN_NEW_TAB

    def test_specific_uia_name_does_not_become_select_profile(self):
        # Click en chrome.exe pegado a LAUNCH_APP, pero con UIA específico
        # ("Nueva pestaña") → debe ir por la rama OPEN_NEW_TAB, NO
        # SELECT_PROFILE.
        launch = _step(
            ActionStrategy.LAUNCH_APP,
            payload={"app_name": "Chrome"},
            process="SearchUI.exe",
        )
        nueva = _step(
            ActionStrategy.CLICK,
            uia_name="Nueva pestaña",
            process="chrome.exe",
        )
        out_c, _ = pass_chrome_workflow(
            [launch, nueva], [_ispep(), _ispep()],
        )
        assert out_c[1].action_strategy == ActionStrategy.OPEN_NEW_TAB

    def test_known_bookmark_becomes_open_bookmark(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="Youtube",
            process="chrome.exe",
        )
        out_c, _ = pass_chrome_workflow([s], [_ispep()])
        assert out_c[0].action_strategy == ActionStrategy.OPEN_BOOKMARK
        assert out_c[0].action_payload.get("name") == "Youtube"
        assert "youtube.com" in out_c[0].action_payload.get("url", "")

    def test_unknown_bookmark_stays_click(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="Mi marcador raro",
            process="chrome.exe",
        )
        out_c, _ = pass_chrome_workflow([s], [_ispep()])
        assert out_c[0].action_strategy == ActionStrategy.CLICK

    def test_select_profile_only_fires_once_per_launch(self):
        launch = _step(
            ActionStrategy.LAUNCH_APP,
            payload={"app_name": "Chrome"},
            process="SearchUI.exe",
        )
        c1 = _step(
            ActionStrategy.CLICK,
            uia_ctype="GroupControl", process="chrome.exe",
            window_title="Google Chrome",
        )
        c2 = _step(  # un segundo click ambiguo no debe ser otro select_profile
            ActionStrategy.CLICK,
            uia_ctype="GroupControl", process="chrome.exe",
            window_title="Google Chrome",
        )
        out_c, _ = pass_chrome_workflow(
            [launch, c1, c2], [_ispep(), _ispep(), _ispep()],
        )
        kinds = [s.action_strategy for s in out_c]
        assert kinds.count(ActionStrategy.SELECT_PROFILE) == 1


# ──────────────────────────────────────────────────────────────────────
# Pass C: Field Session Merger
# ──────────────────────────────────────────────────────────────────────

class TestFieldSessionMerger:
    def test_merges_prefix_fragments_with_enter_in_between(self):
        a = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "De"},
            process="chrome.exe",
        )
        enter = _step(
            ActionStrategy.SEND_HOTKEY,
            payload={"hotkey": "enter"},
            process="chrome.exe",
        )
        b = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "Devuelveme el amor Luis Miguel"},
            process="chrome.exe",
        )
        out_c, _ = pass_field_session_merger(
            [a, enter, b],
            [_ispep("a"), _ispep("enter"), _ispep("b")],
        )
        assert len(out_c) == 1
        merged = out_c[0]
        assert merged.action_strategy == ActionStrategy.TYPE_TEXT
        assert merged.action_payload["text"] == "Devuelveme el amor Luis Miguel"
        assert merged.action_payload["submit_after"] is True
        assert merged.action_payload["merged_from_fragments"] == [
            "De", "Devuelveme el amor Luis Miguel",
        ]
        assert merged.validation_strategy == ValidationStrategy.REQUIRE_FIELD_VALUE

    def test_keeps_unrelated_fragments(self):
        # Si el segundo texto NO es prefijo-extensión del primero, NO se
        # fusiona (podrían ser dos campos distintos en la misma página).
        a = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "Hola"},
            process="chrome.exe",
        )
        enter = _step(
            ActionStrategy.SEND_HOTKEY,
            payload={"hotkey": "enter"},
            process="chrome.exe",
        )
        b = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "Cd"},  # no relacionado
            process="chrome.exe",
        )
        out_c, _ = pass_field_session_merger(
            [a, enter, b],
            [_ispep(), _ispep(), _ispep()],
        )
        assert len(out_c) == 3

    def test_does_not_merge_across_processes(self):
        a = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "abc"},
            process="chrome.exe",
        )
        enter = _step(
            ActionStrategy.SEND_HOTKEY,
            payload={"hotkey": "enter"},
            process="chrome.exe",
        )
        b = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "abcdef"},
            process="notepad.exe",
        )
        out_c, _ = pass_field_session_merger(
            [a, enter, b],
            [_ispep(), _ispep(), _ispep()],
        )
        assert len(out_c) == 3


# ──────────────────────────────────────────────────────────────────────
# End-to-end pipeline
# ──────────────────────────────────────────────────────────────────────

class TestLandmarksAndPathologicalBboxes:
    """PRD 2026-05-03b §1: rechazar landmarks HTML y bboxes patológicos."""

    @pytest.mark.parametrize("name", [
        "main", "body", "html", "root", "app",
        "MAIN", "Document", "navigation", "main-content",
    ])
    def test_landmark_names_are_technical(self, name):
        assert _is_technical_name(name) is True

    @pytest.mark.parametrize("bbox", [
        {"left": 0, "top": -113758, "width": 3000, "height": 1500},
        {"left": -10000, "top": 0, "width": 100, "height": 100},
        {"left": 0, "top": 0, "width": 0, "height": 100},
        {"left": 0, "top": 0, "width": 50000, "height": 50000},
    ])
    def test_pathological_bbox_detected(self, bbox):
        assert _bbox_is_pathological(bbox) is True

    @pytest.mark.parametrize("bbox", [
        {"left": 0, "top": 0, "width": 100, "height": 30},
        {"left": -100, "top": 200, "width": 50, "height": 50},  # negative pero legítimo
    ])
    def test_normal_bbox_passes(self, bbox):
        assert _bbox_is_pathological(bbox) is False

    def test_click_on_main_with_pathological_bbox_is_invalid(self):
        cs = _step(
            ActionStrategy.CLICK,
            uia_aid="main", uia_ctype="GroupControl",
            uia_bbox={"left": 0, "top": -113758, "width": 3000, "height": 1500},
        )
        assert _is_invalid_click_target(cs) is True

    def test_click_fallback_source_is_invalid_even_with_uia_name(self):
        # Aunque UIA reporte un name, si target_source=click_fallback el
        # bbox real no contenía la coordenada — el player no puede
        # reproducir el click sin OCR/vision adicional.
        cs = _step(
            ActionStrategy.CLICK,
            uia_name="Some Button",
            target_source="click_fallback",
            quality_level="low",
        )
        assert _is_invalid_click_target(cs) is True

    def test_main_landmark_in_chrome_becomes_select_profile(self):
        launch = _step(
            ActionStrategy.LAUNCH_APP,
            payload={"app_name": "Chrome"},
            process="SearchUI.exe",
        )
        # El click problema de la misión f1634274.
        click_main = _step(
            ActionStrategy.CLICK,
            uia_aid="main", uia_ctype="GroupControl",
            uia_bbox={"left": 0, "top": -113758, "width": 3000, "height": 1500},
            process="chrome.exe",
            window_title="Google Chrome",
            target_source="click_fallback",
            quality_level="low",
        )
        new_tab = _step(
            ActionStrategy.CLICK,
            uia_name="Nueva pestaña",
            process="chrome.exe",
            window_title="Workspace - Google Chrome",
        )
        out_c, _ = pass_chrome_workflow(
            [launch, click_main, new_tab], [_ispep(), _ispep(), _ispep()],
        )
        assert out_c[1].action_strategy == ActionStrategy.SELECT_PROFILE
        # Sin OCR/Playwright no podemos extraer el nombre del perfil.
        # El step debe pedir refuerzo humano.
        assert out_c[1].action_payload.get("needs_user_label") is True
        assert out_c[1].action_payload.get("label_prompt") == "¿Qué perfil seleccionaste?"
        # El intent semántico también queda actualizado (no más unknown).
        assert (out_c[1].target_context.semantic or {}).get("intent") == "select_profile"


class TestRedStepGuard:
    """PRD §5: clicks rojos / click_fallback no deben ser ejecutables."""

    def test_red_click_becomes_needs_user_label(self):
        click = _step(
            ActionStrategy.CLICK,
            uia_aid="something",
            target_source="click_fallback",
            quality_level="red",
        )
        out_c, _ = pass_red_step_guard([click], [_ispep()])
        assert len(out_c) == 1
        pl = out_c[0].action_payload
        assert pl.get("needs_reinforcement") is True
        assert "label_prompt" in pl
        sem = out_c[0].target_context.semantic or {}
        assert sem.get("intent") == "needs_user_label"
        assert sem.get("needs_reinforcement") is True

    def test_high_quality_click_untouched(self):
        click = _step(
            ActionStrategy.CLICK,
            uia_name="Save", uia_aid="btnSave",
            target_source="uia",
            quality_level="high",
        )
        out_c, _ = pass_red_step_guard([click], [_ispep()])
        assert out_c[0].action_payload.get("needs_reinforcement") is None

    def test_red_step_guard_only_touches_clicks(self):
        ne = _step(
            ActionStrategy.LAUNCH_APP,
            payload={"app_name": "Chrome"},
            quality_level="red",
        )
        out_c, _ = pass_red_step_guard([ne], [_ispep()])
        assert out_c[0].action_payload.get("needs_reinforcement") is None


class TestSemanticIntent:
    """PRD §3: cada acción fusionada actualiza target_context.semantic.intent."""

    def test_open_new_tab_sets_intent(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="Nueva pestaña",
            process="chrome.exe",
        )
        out_c, _ = pass_chrome_workflow([s], [_ispep()])
        sem = out_c[0].target_context.semantic or {}
        assert sem.get("intent") == "open_new_tab"
        assert sem.get("action_kind") == "open_new_tab"

    def test_open_bookmark_sets_intent_open_url(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="Youtube",
            process="chrome.exe",
        )
        out_c, _ = pass_chrome_workflow([s], [_ispep()])
        sem = out_c[0].target_context.semantic or {}
        assert sem.get("intent") == "open_url"
        assert sem.get("action_kind") == "open_bookmark"

    def test_search_sets_intent_when_window_is_youtube(self):
        # type_text en YouTube + Enter + type_text → search con site
        a = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "De"},
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
        )
        enter = _step(
            ActionStrategy.SEND_HOTKEY,
            payload={"hotkey": "enter"},
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
        )
        b = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "Devuelveme el amor Luis Miguel"},
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
        )
        out_c, _ = pass_field_session_merger(
            [a, enter, b],
            [_ispep(), _ispep(), _ispep()],
        )
        merged = out_c[0]
        assert merged.action_payload.get("site") == "youtube"
        assert merged.action_payload.get("target_label") == "youtube_search_box"
        sem = merged.target_context.semantic or {}
        assert sem.get("intent") == "search"


class TestFullPipeline:
    def test_full_youtube_search_scenario(self):
        """Replica el escenario PRD: chrome → profile → newtab → bookmark → search."""
        launch = _step(
            ActionStrategy.LAUNCH_APP,
            uia_name="Chrome", payload={"app_name": "Chrome"},
            process="SearchUI.exe",
        )
        profile = _step(
            ActionStrategy.CLICK,
            uia_ctype="GroupControl",
            process="chrome.exe",
            window_title="Google Chrome",
        )
        newtab = _step(
            ActionStrategy.CLICK,
            uia_name="Nueva pestaña",
            process="chrome.exe",
            window_title="Workspace - Google Chrome",
        )
        bookmark = _step(
            ActionStrategy.CLICK,
            uia_name="Youtube",
            process="chrome.exe",
            window_title="Nueva pestaña - Google Chrome",
        )
        search_click = _step(
            ActionStrategy.CLICK,
            uia_aid="guide-service",
            uia_ctype="GroupControl",
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
        )
        type1 = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "De"},
            process="chrome.exe",
        )
        enter = _step(
            ActionStrategy.SEND_HOTKEY,
            payload={"hotkey": "enter"},
            process="chrome.exe",
        )
        type2 = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "Devuelveme el amor Luis Miguel"},
            process="chrome.exe",
        )

        compiled = [launch, profile, newtab, bookmark, search_click, type1, enter, type2]
        interp = [_ispep() for _ in compiled]
        out_c, _ = apply_semantic_fusion(compiled, interp)

        kinds = [s.action_strategy for s in out_c]
        assert kinds == [
            ActionStrategy.LAUNCH_APP,
            ActionStrategy.SELECT_PROFILE,
            ActionStrategy.OPEN_NEW_TAB,
            ActionStrategy.OPEN_BOOKMARK,
            ActionStrategy.TYPE_TEXT,  # merged from fragments
        ]
        assert out_c[-1].action_payload["text"] == "Devuelveme el amor Luis Miguel"
        assert out_c[-1].action_payload["submit_after"] is True


# ──────────────────────────────────────────────────────────────────────
# PRD 2026-05-03c — passes y helpers nuevos
# ──────────────────────────────────────────────────────────────────────

class TestDropRedundantFocusClicks:
    """``pass_drop_redundant_focus_clicks`` (PRD 2026-05-03c §4)."""

    def test_drops_guide_service_click_before_typing(self):
        click = _step(
            ActionStrategy.CLICK,
            uia_aid="guide-service", uia_ctype="GroupControl",
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
            target_source="click_fallback", quality_level="low",
        )
        text = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "Devuelveme el amor"},
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
        )
        out_c, _ = pass_drop_redundant_focus_clicks(
            [click, text], [_ispep(), _ispep()],
        )
        assert len(out_c) == 1
        assert out_c[0].action_strategy == ActionStrategy.TYPE_TEXT

    def test_keeps_legitimate_click_with_uia_name(self):
        click = _step(
            ActionStrategy.CLICK,
            uia_name="Send button", uia_aid="btn_send",
            process="chrome.exe",
        )
        text = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "x"},
            process="chrome.exe",
        )
        out_c, _ = pass_drop_redundant_focus_clicks(
            [click, text], [_ispep(), _ispep()],
        )
        assert len(out_c) == 2

    def test_keeps_invalid_click_when_no_field_text_follows(self):
        click = _step(
            ActionStrategy.CLICK,
            uia_aid="guide-service", uia_ctype="GroupControl",
            process="chrome.exe",
            target_source="click_fallback", quality_level="low",
        )
        out_c, _ = pass_drop_redundant_focus_clicks([click], [_ispep()])
        # Sin TYPE_TEXT detrás → no descartamos; deja que red_step_guard
        # o invalid_target_filter decidan.
        assert len(out_c) == 1


class TestSearchFinalizer:
    """``pass_search_finalizer`` (PRD 2026-05-03c §3)."""

    def test_fuses_text_then_enter(self):
        text = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "hola"},
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
        )
        enter = _step(
            ActionStrategy.SEND_HOTKEY,
            payload={"hotkey": "enter"},
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
        )
        out_c, _ = pass_search_finalizer(
            [text, enter], [_ispep(), _ispep()],
        )
        assert len(out_c) == 1
        pl = out_c[0].action_payload
        assert pl["submit_after"] is True
        assert pl.get("site") == "youtube"
        assert pl.get("target_label") == "youtube_search_box"

    def test_fuses_enter_then_text_inverted_order(self):
        enter = _step(
            ActionStrategy.SEND_HOTKEY,
            payload={"hotkey": "enter"},
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
        )
        text = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "hola"},
            process="chrome.exe",
            window_title="YouTube - Google Chrome",
        )
        out_c, _ = pass_search_finalizer(
            [enter, text], [_ispep(), _ispep()],
        )
        assert len(out_c) == 1
        pl = out_c[0].action_payload
        assert pl["submit_after"] is True
        assert pl.get("text") == "hola"
        assert pl.get("site") == "youtube"

    def test_no_fusion_across_processes(self):
        text = _step(
            ActionStrategy.TYPE_TEXT,
            payload={"text": "hola"},
            process="explorer.exe",
        )
        enter = _step(
            ActionStrategy.SEND_HOTKEY,
            payload={"hotkey": "enter"},
            process="chrome.exe",
        )
        out_c, _ = pass_search_finalizer(
            [text, enter], [_ispep(), _ispep()],
        )
        assert len(out_c) == 2


class TestConfirmStepLabel:
    """``confirm_step_label`` — Quick Reinforcement (PRD 2026-05-03c §B)."""

    def test_select_profile_with_label(self):
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={
                "profile": "",
                "needs_user_label": True,
                "label_prompt": "¿Qué perfil seleccionaste?",
            },
        )
        confirm_step_label(cs, profile="Daniel Arturo Ramos")
        pl = cs.action_payload
        assert pl["profile"] == "Daniel Arturo Ramos"
        assert "needs_user_label" not in pl
        assert "needs_reinforcement" not in pl
        assert "label_prompt" not in pl
        assert pl["user_confirmed_label"] == "Daniel Arturo Ramos"
        sem = cs.target_context.semantic
        assert sem["intent"] == "select_profile"
        assert sem["human_label"] == "Daniel Arturo Ramos"
        assert "needs_reinforcement" not in sem

    def test_click_with_user_label_promotes_intent(self):
        cs = _step(
            ActionStrategy.CLICK,
            payload={
                "needs_reinforcement": True,
                "label_prompt": "¿Sobre qué elemento hiciste click?",
            },
        )
        confirm_step_label(cs, user_confirmed_label="Botón Guardar")
        pl = cs.action_payload
        assert pl["user_confirmed_label"] == "Botón Guardar"
        assert "needs_reinforcement" not in pl
        sem = cs.target_context.semantic
        assert sem["intent"] == "click_button"
        assert sem["human_label"] == "Botón Guardar"

    def test_target_label_for_search(self):
        cs = _step(
            ActionStrategy.SUBMIT_SEARCH,
            payload={"text": "hola", "site": "youtube"},
        )
        confirm_step_label(cs, target_label="youtube_search_box")
        assert cs.action_payload["target_label"] == "youtube_search_box"

    def test_idempotent(self):
        cs = _step(
            ActionStrategy.SELECT_PROFILE,
            payload={"profile": "", "needs_user_label": True},
        )
        confirm_step_label(cs, profile="Pepe")
        confirm_step_label(cs, profile="Pepe")
        assert cs.action_payload["profile"] == "Pepe"
        assert "needs_user_label" not in cs.action_payload


class TestIdentityPromotionFallback:
    def test_promotes_click_fallback_with_stable_automation_id(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="OK",
            uia_aid="dlgBtn_OK",
            uia_ctype="ButtonControl",
            target_source="click_fallback",
            quality_level="low",
        )
        changed = maybe_promote_click_fallback_identity(s)
        assert changed
        assert s.target_context.signature["target_source"] == "uia_control"
        assert s.target_context.confidence["quality_level"] == "medium"

    def test_promotes_click_fallback_windows_search_box(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="Type here to search",
            uia_aid="",
            uia_ctype="EditControl",
            process="searchhost.exe",
            window_title="Search",
            target_source="click_fallback",
            quality_level="low",
        )
        changed = maybe_promote_click_fallback_identity(s)
        assert changed
        assert s.target_context.signature["target_source"] == "semantic_target"

    def test_leaves_fallback_when_identity_too_weak(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="",
            uia_aid="",
            uia_ctype="GroupControl",
            target_source="click_fallback",
        )
        assert not maybe_promote_click_fallback_identity(s)
        assert s.target_context.signature["target_source"] == "click_fallback"

    def test_promotes_click_fallback_when_trusted_pre_action_signature(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="Chrome",
            uia_ctype="ListItemControl",
            target_source="click_fallback",
            quality_level="low",
        )
        sig = dict(s.target_context.signature or {})
        sig["trusted_pre_action_identity"] = True
        sig["identity_preserved_after_transition"] = True
        s.target_context.signature = sig
        assert maybe_promote_click_fallback_identity(s)
        assert s.target_context.signature["target_source"] == "uia_control"


class TestTrustedPreActionInvalidFilter:
    def test_click_fallback_not_invalid_when_trusted(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="Save",
            target_source="click_fallback",
        )
        sig = dict(s.target_context.signature or {})
        sig["trusted_pre_action_identity"] = True
        sig["identity_preserved_after_transition"] = True
        s.target_context.signature = sig
        assert not _is_invalid_click_target(s)

    def test_not_red_when_trusted_fallback(self):
        s = _step(
            ActionStrategy.CLICK,
            uia_name="X",
            target_source="click_fallback",
            quality_level="low",
        )
        sig = dict(s.target_context.signature or {})
        sig["trusted_pre_action_identity"] = True
        sig["identity_preserved_after_transition"] = True
        s.target_context.signature = sig
        assert not _is_red_or_fallback_click(s)
