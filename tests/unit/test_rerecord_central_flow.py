"""
Nevlan — Tests del flujo central de regrabación (PRD 2026-05-09 §E + §F + §G)
==============================================================================

Cubre:

  * El bug del **freeze** en "Reparar pasos débiles → Regrabar los unos
    en cola → Regrabar":
      - ``_cleanup_rerecord`` debe ser re-entrancy-safe: una llamada
        recursiva (vía ``overlay.close()`` → ``cancel_requested`` →
        ``_on_rerecord_cancel`` → ``_cleanup_rerecord``) no debe
        encolar el siguiente paso de la cola dos veces.
      - ``RerecordOverlay.mark_closing()`` evita que ``closeEvent``
        emita ``cancel_requested`` cuando el caller cierra
        voluntariamente el overlay.

  * El **modo cola** salta el modal "Preparando regrabación" entre
    iteraciones y muestra "Paso N de M" en el título del overlay.

  * **Todos los entrypoints** delegan en el mismo flujo central
    (``RerecordOrchestrator`` + ``RerecordOverlay``):
      - Mission Review → Regrabar
      - Pasos de la automatización (Centro) → Regrabar (delega a
        Mission Review cuando hay ``semantic_execution_plan``)
      - Reparar débiles → Regrabar (1 + cola)

  * **Vibe Coding** está conectado en ambos entrypoints, y la
    cancelación del input no muta la misión.

  * **Elegir elemento correcto** está conectado en Mission Review.

  * **Adapter ``rerecord_flow``** (``start_rerecord_flow`` /
    ``save_rerecord`` / ``repeat_rerecord`` / ``cancel_rerecord`` /
    ``start_vibe_coding_flow`` / ``choose_correct_element_flow``)
    expone los verbos canónicos pedidos en el ticket y delega a las
    implementaciones existentes (no duplica lógica).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MISSION_REVIEW_PATH = REPO_ROOT / "app" / "interfaces" / "desktop" / "mission_review.py"
AUTOMATION_CENTER_PATH = REPO_ROOT / "app" / "interfaces" / "desktop" / "automation_center.py"
RERECORD_OVERLAY_PATH = REPO_ROOT / "app" / "interfaces" / "desktop" / "rerecord_overlay.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slice_method(src: str, signature: str) -> str:
    start = src.index(signature)
    end = src.index("\n    def ", start + 1)
    return src[start:end]


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def _qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as e:
        pytest.skip(f"PyQt6 no disponible: {e}")
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ─────────────────────────────────────────────────────────────────────
# §E — Overlay: mark_closing y closeEvent re-entrancy-safe
# ─────────────────────────────────────────────────────────────────────


class TestOverlayMarkClosingPreventsCancelEmit:
    """``mark_closing()`` debe garantizar que ``closeEvent`` NO vuelva
    a emitir ``cancel_requested``. Sin esta garantía, el cleanup
    post-save dispara ``_on_rerecord_cancel`` recursivo y la cola
    "Reparar pasos débiles" se procesa por duplicado.
    """

    def test_overlay_has_mark_closing_method(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(0, "x", "click")
        try:
            assert hasattr(ov, "mark_closing"), (
                "RerecordOverlay debe exponer mark_closing() "
                "para que el caller pueda cerrar sin re-emitir cancel."
            )
        finally:
            ov.close()

    def test_mark_closing_then_close_does_not_emit_cancel(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(0, "x", "click")
        emitted = {"v": 0}
        ov.cancel_requested.connect(lambda: emitted.__setitem__("v", emitted["v"] + 1))
        ov.mark_closing()
        ov.close()
        assert emitted["v"] == 0, (
            "closeEvent NO debe emitir cancel_requested después de "
            "mark_closing() — eso era la causa del cleanup recursivo."
        )

    def test_close_without_mark_closing_does_emit_cancel(self, _qapp):
        """Por contraste: si NO marcamos closing, closeEvent SÍ emite
        cancel_requested. Eso es lo que queremos para Alt+F4 / kill
        externo — pero NO para cierre voluntario del caller.
        """
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(0, "x", "click")
        emitted = {"v": 0}
        ov.cancel_requested.connect(lambda: emitted.__setitem__("v", emitted["v"] + 1))
        ov.close()
        assert emitted["v"] == 1, (
            "Sin mark_closing(), closeEvent debe emitir cancel_requested "
            "para que callers externos (Alt+F4) liberen recursos."
        )

    def test_mark_closing_stops_internal_timers(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(0, "x", "click")
        try:
            assert ov._timer.isActive()
            assert ov._raise_timer.isActive()
            ov.mark_closing()
            assert not ov._timer.isActive()
            assert not ov._raise_timer.isActive()
        finally:
            ov.close()


# ─────────────────────────────────────────────────────────────────────
# §F — Overlay: set_queue_progress muestra "Paso N de M"
# ─────────────────────────────────────────────────────────────────────


class TestOverlayQueueProgress:

    def test_set_queue_progress_renders_paso_n_de_m(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(2, "Click en Buscar", "click")
        try:
            ov.set_queue_progress(2, 5)
            txt = ov._title_lbl.text()
            assert "Paso 2 de 5" in txt, (
                f"Esperaba 'Paso 2 de 5' en el título, got: {txt!r}"
            )
        finally:
            ov.close()

    def test_set_queue_progress_zero_clears(self, _qapp):
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
        ov = RerecordOverlay(2, "Click en Buscar", "click")
        try:
            ov.set_queue_progress(2, 5)
            ov.set_queue_progress(0, 0)
            assert "Paso" not in ov._title_lbl.text() or \
                   ov._title_lbl.text() == ov._title_base
        finally:
            ov.close()


# ─────────────────────────────────────────────────────────────────────
# §E — _cleanup_rerecord es re-entrancy-safe
# ─────────────────────────────────────────────────────────────────────


class TestCleanupRecursionGuard:
    """El bug crítico: _cleanup_rerecord se llamaba a sí mismo cuando
    overlay.close() emitía cancel_requested → _on_rerecord_cancel →
    _cleanup_rerecord, lo que duplicaba el QTimer.singleShot al
    siguiente paso de la cola.
    """

    def test_mission_review_has_cleanup_in_progress_guard(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def _cleanup_rerecord(self):")
        assert "_cleanup_in_progress" in body, (
            "_cleanup_rerecord debe llevar guard _cleanup_in_progress "
            "para evitar la recursión causa del freeze de la cola."
        )

    def test_mission_review_cleanup_calls_mark_closing_before_close(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def _cleanup_rerecord(self):")
        # mark_closing debe aparecer ANTES de la llamada real a ov.close().
        # Buscamos exactamente ``ov.close()`` (la del overlay local), no
        # cualquier ``.close()`` que aparezca en el docstring.
        idx_mark = body.find("mark_closing")
        idx_close = body.find("ov.close()")
        assert idx_mark != -1, (
            "_cleanup_rerecord debe llamar mark_closing() en el "
            "overlay antes de cerrarlo."
        )
        assert idx_close != -1, (
            "_cleanup_rerecord debe cerrar el overlay vía 'ov.close()' "
            "(referencia local detacheada)."
        )
        assert idx_mark < idx_close, (
            "mark_closing() debe ir ANTES de ov.close() para que "
            "closeEvent no re-emita cancel_requested."
        )

    def test_mission_review_cleanup_detaches_overlay_before_close(self):
        """Antes: ``self._rerecord_overlay.close(); self._rerecord_overlay = None``.
        Una llamada re-entrante encontraba ``self._rerecord_overlay``
        todavía seteado y llamaba a ``close()`` por segunda vez.

        Después: ``ov = self._rerecord_overlay; self._rerecord_overlay = None;
        ov.close()``. Cualquier re-entrancia ve ``None`` y sale limpio.
        """
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def _cleanup_rerecord(self):")
        # Heurística: encontrar la asignación a None ANTES de close().
        none_assign = body.find("self._rerecord_overlay = None")
        close_call = body.find("ov.close()")
        assert none_assign != -1, "Falta detach (= None) del overlay."
        assert close_call != -1, "Falta close() del overlay (vía ov local)."
        assert none_assign < close_call, (
            "self._rerecord_overlay debe asignarse a None ANTES "
            "del close() para que cualquier re-entrancia salga rápido."
        )

    def test_on_rerecord_cancel_returns_early_when_cleanup_in_progress(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def _on_rerecord_cancel(self):")
        assert "_cleanup_in_progress" in body, (
            "_on_rerecord_cancel debe respetar el guard "
            "_cleanup_in_progress para evitar la recursión."
        )


# ─────────────────────────────────────────────────────────────────────
# §F — Modo cola: skip prep modal + auto-start replay
# ─────────────────────────────────────────────────────────────────────


class TestQueueModeBehavior:

    def test_repair_weak_steps_sets_queue_flag(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def repair_weak_steps(self):")
        assert "self._in_repair_queue = True" in body, (
            "repair_weak_steps debe activar el flag de cola al elegir "
            "'⚡ Regrabar los N en cola'."
        )
        assert "self._repair_queue_total" in body, (
            "Debe registrar el total de pasos para mostrar 'Paso N de M'."
        )

    def test_rerecord_step_skips_prep_modal_in_queue(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def rerecord_step(self, index):")
        # show_confirmation con title="Preparando regrabación" debe
        # estar dentro de la rama ``not in_queue``.
        assert "in_queue = bool(self._in_repair_queue)" in body, (
            "rerecord_step debe leer self._in_repair_queue para decidir "
            "si saltar el modal."
        )
        assert 'if not in_queue:' in body, (
            "El modal Preparando regrabación debe omitirse en modo cola."
        )

    def test_rerecord_step_auto_starts_replay_in_queue(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def rerecord_step(self, index):")
        assert "_on_rerecord_start_replay" in body, (
            "rerecord_step debe poder lanzar _on_rerecord_start_replay."
        )
        # En modo cola debe agendar auto-start (sin esperar click "Empezar").
        assert "QTimer.singleShot(120, self._on_rerecord_start_replay)" in body, (
            "En modo cola, rerecord_step debe arrancar el replay "
            "automáticamente sin que el usuario clique 'Empezar'."
        )

    def test_rerecord_step_sets_queue_progress_on_overlay(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def rerecord_step(self, index):")
        assert "set_queue_progress" in body, (
            "rerecord_step debe propagar el progreso de la cola al overlay "
            "(set_queue_progress)."
        )

    def test_save_in_queue_skips_success_modal(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def _on_rerecord_save(self):")
        # El show_warning "Regrabación guardada" debe estar dentro de la
        # rama ``if not self._in_repair_queue``.
        assert "if not self._in_repair_queue:" in body, (
            "El popup de éxito 'Regrabación guardada' debe omitirse "
            "entre pasos de la cola para no bloquear el flujo."
        )
        assert '"Regrabación guardada"' in body, (
            "El título 'Regrabación guardada' debe seguir existiendo "
            "para el modo no-cola."
        )

    def test_cancel_in_queue_stops_whole_queue(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def _on_rerecord_cancel(self):")
        assert "self._repair_queue = []" in body, (
            "_on_rerecord_cancel debe vaciar la cola si el usuario "
            "cancela en modo cola: no procesar más pasos."
        )
        assert "self._in_repair_queue = False" in body


# ─────────────────────────────────────────────────────────────────────
# §G — Adapter rerecord_flow expone los verbos pedidos
# ─────────────────────────────────────────────────────────────────────


class TestCentralFlowAdapter:

    def test_module_exposes_canonical_verbs(self):
        from app.services.missions import rerecord_flow as rf
        for verb in (
            "start_rerecord_flow",
            "save_rerecord",
            "repeat_rerecord",
            "cancel_rerecord",
            "start_vibe_coding_flow",
            "choose_correct_element_flow",
        ):
            assert hasattr(rf, verb), (
                f"rerecord_flow debe exponer {verb} (verbo canónico "
                "pedido en el ticket)."
            )

    def test_start_rerecord_flow_returns_handle_with_orchestrator(self):
        """Smoke test: con una misión mínima válida, el adapter
        crea orchestrator + sesión y devuelve un handle."""
        from app.services.missions.rerecord_flow import (
            start_rerecord_flow, RerecordFlowHandle, ENTRY_MISSION_REVIEW,
        )

        # Construimos una misión mínima con un semantic_execution_plan
        # de 2 pasos. No tocamos el resto de la lógica (sin
        # compiled_execution_graph real para que el contract no
        # exija más cosas).
        mission_dict = {
            "id": "test-flow",
            "name": "Test flow",
            "raw_trace": [],
            "interpreted_steps": [],
            "compiled_execution_graph": [],
            "semantic_execution_plan": {
                "steps": [
                    {"id": "s1", "type": "click", "params": {}, "confidence": 0.9},
                    {"id": "s2", "type": "type_text", "params": {}, "confidence": 0.9},
                ],
                "average_confidence": 0.9,
                "intent_status": "READY",
                "not_ready_reasons": [],
            },
            "rerecord_history": [],
            "review_confirmations": [],
        }
        from app.contracts.mission import Mission
        mission = Mission.model_validate(mission_dict)

        handle = start_rerecord_flow(
            mission, step_index=1,
            source_entrypoint=ENTRY_MISSION_REVIEW,
            queue_index=0, queue_total=0,
        )
        try:
            assert isinstance(handle, RerecordFlowHandle)
            assert handle.target_step_index == 1
            assert handle.source_entrypoint == ENTRY_MISSION_REVIEW
            assert handle.orchestrator is not None
            assert handle.session is not None
            assert handle.session.mission_id == "test-flow"
        finally:
            from app.services.missions.rerecord_flow import cancel_rerecord
            cancel_rerecord(handle)

    def test_cancel_rerecord_idempotent_and_releases(self):
        from app.services.missions.rerecord_flow import (
            start_rerecord_flow, cancel_rerecord, ENTRY_MISSION_REVIEW,
        )
        from app.contracts.mission import Mission
        mission = Mission.model_validate({
            "id": "test-idem",
            "name": "Test",
            "raw_trace": [],
            "interpreted_steps": [],
            "compiled_execution_graph": [],
            "semantic_execution_plan": {
                "steps": [{"id": "s1", "type": "click", "params": {}, "confidence": 0.9}],
                "average_confidence": 0.9,
                "intent_status": "READY",
                "not_ready_reasons": [],
            },
            "rerecord_history": [],
            "review_confirmations": [],
        })
        h = start_rerecord_flow(
            mission, step_index=0,
            source_entrypoint=ENTRY_MISSION_REVIEW,
        )
        cancel_rerecord(h)
        # Segunda llamada no debe levantar.
        cancel_rerecord(h)

    def test_repeat_rerecord_does_not_mutate_mission(self):
        from app.services.missions.rerecord_flow import (
            start_rerecord_flow, repeat_rerecord, ENTRY_MISSION_REVIEW,
        )
        from app.contracts.mission import Mission
        mission = Mission.model_validate({
            "id": "test-rpt",
            "name": "Test",
            "raw_trace": [],
            "interpreted_steps": [],
            "compiled_execution_graph": [],
            "semantic_execution_plan": {
                "steps": [{"id": "s1", "type": "click", "params": {}, "confidence": 0.9}],
                "average_confidence": 0.9,
                "intent_status": "READY",
                "not_ready_reasons": [],
            },
            "rerecord_history": [],
            "review_confirmations": [],
        })
        before_history_len = len(mission.rerecord_history or [])
        h = start_rerecord_flow(
            mission, step_index=0,
            source_entrypoint=ENTRY_MISSION_REVIEW,
        )
        repeat_rerecord(h)  # noop si no hay captura todavía
        assert len(mission.rerecord_history or []) == before_history_len, (
            "repeat_rerecord NO debe mutar rerecord_history."
        )


# ─────────────────────────────────────────────────────────────────────
# §G — Todos los entrypoints delegan al mismo flujo central
# ─────────────────────────────────────────────────────────────────────


class TestAllEntrypointsDelegateToCentralFlow:

    def test_mission_review_rerecord_uses_orchestrator_and_overlay(self):
        body = _slice_method(_read(MISSION_REVIEW_PATH), "def rerecord_step(self, index):")
        assert "RerecordOrchestrator" in body, (
            "mission_review.rerecord_step debe usar RerecordOrchestrator."
        )
        assert "RerecordOverlay" in body
        assert "PHASE_REPLAY" in body, (
            "mission_review.rerecord_step debe arrancar el overlay en "
            "fase replay (replay previo automático)."
        )

    def test_repair_weak_step_dispatch_uses_rerecord_step(self):
        body = _slice_method(_read(MISSION_REVIEW_PATH), "def repair_weak_steps(self):")
        assert "self.rerecord_step(i)" in body, (
            "El botón '🎯 Regrabar paso' del dialog Reparar débiles "
            "debe llamar self.rerecord_step (mismo flujo central)."
        )

    def test_repair_queue_dispatch_uses_rerecord_step(self):
        body = _slice_method(
            _read(MISSION_REVIEW_PATH),
            "def _process_next_repair_in_queue(self):",
        )
        assert "self.rerecord_step(idx)" in body, (
            "_process_next_repair_in_queue debe llamar self.rerecord_step "
            "(mismo flujo central)."
        )

    def test_automation_center_rerecord_delegates_when_plan_exists(self):
        src = _read(AUTOMATION_CENTER_PATH)
        body = _slice_method(src, "def _rerecord_step(self, index):")
        assert "_delegate_rerecord_to_review" in body, (
            "automation_center._rerecord_step debe delegar al flujo "
            "central (Mission Review) cuando hay semantic_execution_plan."
        )
        # El delegate helper debe existir.
        assert "def _delegate_rerecord_to_review" in src, (
            "Falta el helper _delegate_rerecord_to_review en "
            "automation_center."
        )

    def test_automation_center_delegate_uses_mission_review_dialog(self):
        src = _read(AUTOMATION_CENTER_PATH)
        helper = _slice_method(src, "def _delegate_rerecord_to_review(self, index: int) -> None:")
        assert "MissionReviewDialog" in helper, (
            "_delegate_rerecord_to_review debe abrir un MissionReviewDialog "
            "y disparar su rerecord_step (reusa la maquinaria central)."
        )
        assert "rerecord_step(i)" in helper or "rerecord_step(index)" in helper


# ─────────────────────────────────────────────────────────────────────
# §C — Vibe Coding está conectado y no muta al cancelar
# ─────────────────────────────────────────────────────────────────────


class TestVibeCodingConnections:

    def test_mission_review_vibe_button_connected(self):
        src = _read(MISSION_REVIEW_PATH)
        # El botón Vibe se construye en _build_cards.
        assert "vibe_btn = QPushButton(\"✨\")" in src
        assert "vibe_btn.clicked.connect" in src
        assert "self.vibe_edit_step" in src, (
            "El botón Vibe en Mission Review debe estar conectado a "
            "vibe_edit_step (no muerto)."
        )

    def test_automation_center_vibe_button_connected(self):
        src = _read(AUTOMATION_CENTER_PATH)
        assert "vibe_btn.clicked.connect" in src
        assert "self._vibe_edit_step" in src, (
            "El botón Vibe en automation_center debe estar conectado a "
            "_vibe_edit_step (no muerto)."
        )

    def test_vibe_service_is_central_function(self):
        """Single source of truth: ambos handlers llaman a
        ``apply_vibe_edit`` del servicio único.
        """
        for path in (MISSION_REVIEW_PATH, AUTOMATION_CENTER_PATH):
            src = _read(path)
            assert "from app.services.missions.vibe_service import apply_vibe_edit" in src, (
                f"{path.name} debe importar apply_vibe_edit (servicio único)."
            )

    def test_vibe_cancel_does_not_mutate_mission_review(self):
        """Si el usuario cierra el QInputDialog, el handler retorna
        sin llamar apply_vibe_edit (no muta misión)."""
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def vibe_edit_step(self, index):")
        # Patrón canónico: ``if not ok or not text: return`` antes de
        # llamar apply_vibe_edit. Buscamos la INVOCACIÓN
        # (`apply_vibe_edit(`), no la línea de import.
        assert "if not ok or not text" in body, (
            "vibe_edit_step debe verificar ok+text antes de aplicar."
        )
        idx_guard = body.index("if not ok or not text")
        idx_apply = body.index("apply_vibe_edit(")
        assert idx_guard < idx_apply, (
            "El guard de cancelación debe ir ANTES de la invocación de "
            "apply_vibe_edit()."
        )


# ─────────────────────────────────────────────────────────────────────
# §D — Elegir elemento correcto está conectado
# ─────────────────────────────────────────────────────────────────────


class TestChooseCorrectElement:

    def test_mission_review_pick_button_connected(self):
        src = _read(MISSION_REVIEW_PATH)
        assert "pick_btn = QPushButton(\"🩹\")" in src
        assert "self.pick_correct_element" in src, (
            "El botón 'Elegir elemento correcto' (🩹) debe estar "
            "conectado a pick_correct_element."
        )

    def test_pick_correct_element_uses_one_click_capture(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def pick_correct_element(self, index: int):")
        assert "OneClickCapture" in body, (
            "pick_correct_element debe usar OneClickCapture (servicio único)."
        )
        assert "apply_repair_to_step" in body, (
            "pick_correct_element debe llamar apply_repair_to_step "
            "para acumular el descriptor alternativo."
        )

    def test_pick_correct_element_cancel_does_not_mutate(self):
        """Si el usuario cancela el OneClickCapture (bundle is None),
        el handler muestra 'Reparación cancelada' y NO llama
        apply_repair_to_step."""
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def pick_correct_element(self, index: int):")
        assert "if not bundle:" in body, (
            "pick_correct_element debe checar if not bundle antes "
            "de aplicar el repair."
        )
        # Apply debe ir DESPUÉS del guard. Buscamos la INVOCACIÓN
        # ``apply_repair_to_step(`` (no la línea de import).
        idx_guard = body.index("if not bundle:")
        idx_apply = body.index("apply_repair_to_step(")
        assert idx_guard < idx_apply


# ─────────────────────────────────────────────────────────────────────
# §B — Save sin captura muestra dialog visible (no botón muerto)
# ─────────────────────────────────────────────────────────────────────


class TestSaveWithoutCapture:

    def test_save_without_fragment_recorder_shows_visible_warning(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def _on_rerecord_save(self):")
        # El handler debe mostrar show_warning con título "No hay nada
        # que guardar" cuando _fragment_recorder es None.
        assert "if not self._fragment_recorder:" in body, (
            "_on_rerecord_save debe verificar fragment_recorder."
        )
        assert "No hay nada que guardar" in body, (
            "El handler debe mostrar un texto explícito (no botón muerto)."
        )

    def test_save_in_flight_guard_prevents_double_save(self):
        src = _read(MISSION_REVIEW_PATH)
        body = _slice_method(src, "def _on_rerecord_save(self):")
        assert "_rerecord_save_in_flight" in body, (
            "_on_rerecord_save debe llevar guard contra doble guardado."
        )
