"""
Nevlan — Review Dialog (v3)
-------------------------------------
Pasos de la automatización. Features:
- Vibe Edit inteligente con clasificador de intención
- Regrabar paso (🎯) con overlay en vivo
- Probar paso (🧪) individual
- Scroll vertical en tarjetas
- Layout compacto
"""
import os
import json
import threading
from typing import Optional
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QWidget, QFrame, QLineEdit,
                             QScrollArea, QFileDialog, QMessageBox, QApplication,
                             QSizeGrip, QSizePolicy, QInputDialog,
                             QListWidget, QListWidgetItem)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPoint, QRect, QThread, QObject
from PyQt6.QtGui import QPixmap, QIcon, QMouseEvent
from app.core.logger import log
from app.contracts.mission import (
    Mission, MissionStatus, TargetResolutionStrategy, ActionStrategy, CompiledStep,
    ValidationStrategy,
)
from app.services.missions.step_quality import compute_step_quality
from app.services.missions.execution_confidence import (
    EXEC_AMBER_THRESHOLD,
    EXEC_GREEN_THRESHOLD,
    ExecLevel,
    compute_execution_confidence,
    compute_mission_execution_confidence,
)
from app.services.missions.weak_step_repair_session import WeakStepRepairSession
from app.services.missions.rerecord_display import build_rerecord_display
from app.services.missions.rerecord_session import (
    REPLAY_FAILED_MESSAGE,
    RerecordOrchestrator,
    RerecordSessionError,
    STATE_CAPTURED,
    STATE_FAILED,
    STATE_REPLAYING,
    STATE_SAVED,
    STATE_WAITING_FOR_USER,
)
from app.services.missions.rerecord_controller import RerecordController
from app.services.missions.repair_controller import RepairController
from app.interfaces.desktop.rerecord_overlay import (
    PHASE_CAPTURE,
    PHASE_REPLAY,
)
from app.interfaces.desktop.nevlan_dialog import (
    RESULT_PRIMARY,
    RESULT_SECONDARY,
    RESULT_CANCEL,
    show_confirmation,
    show_error,
    show_warning,
)
from app.services.missions.asset_validation import (
    is_safe_png_path as _is_safe_png_path,
    sanitize_mission_image_refs as _sanitize_mission_image_refs,
)
from app.services.missions.vibe_parser import (
    parse_keyboard_intent, parse_type_text_intent,
    describe_hotkey_payload, validate_hotkey_payload,
)
from app.interfaces.desktop.mission_ui_status import (
    compute_mission_ui_execution_state,
    mission_ui_should_offer_execute_and_test,
)
from app.services.missions.execution_truth_engine import (
    should_suppress_legacy_capture_noise,
)
from app.services.missions.semantic_review_display import (
    find_semantic_step_for_compiled,
    normal_mode_review_caption_for_compiled,
    plan_declares_coords_used,
    use_semantic_primary_normal_mission_review,
)
from app.services.missions.semantic_ambiguity_engine import (
    profile_carrier_needs_confirmation,
)


class NevlanDesign:
    BG = "rgba(25, 25, 32, 250)"
    BORDER = "rgba(255, 255, 255, 35)"
    BTN_BG = "rgba(255, 255, 255, 20)"
    BTN_HOVER = "rgba(255, 255, 255, 40)"


# Strategies considered deterministic (should NOT become agent_prompt lightly)
DETERMINISTIC_STRATEGIES = {
    ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK, ActionStrategy.RIGHT_CLICK,
    ActionStrategy.TYPE_TEXT, ActionStrategy.SEND_HOTKEY, ActionStrategy.DRAG_DROP,
}

STRAT_ICONS = {
    "click": "🖱️", "double_click": "🖱️🖱️", "right_click": "🖱️▸",
    "type_text": "⌨️", "send_hotkey": "⌨️", "agent_prompt": "🤖",
    "drag_drop": "↔️", "wait_for_state": "⏳", "extract_data": "📊",
    "set_field_value": "📝", "scroll": "🖲",
    "scroll_until_visible": "⬇", "select_option": "▾",
    "open_menu_item": "📂", "confirm_dialog": "❓",
    "navigate_or_search": "🌐", "submit_search": "🔎",
    "launch_app": "🚀",
}


def _estimate_step_confidence(c_step: CompiledStep) -> int:
    """0-100 que tan robusto es el paso.

    PRD 2026-05-06b §2: la UI normal **debe** mostrar la
    ``execution_confidence`` (qué tan robusta es la estrategia runtime),
    no la ``capture_confidence`` (qué tan limpio fue el clic). Antes
    leíamos ``capture_score`` directo, lo que penalizaba pasos como
    ``open_new_tab`` (Ctrl+T) sólo porque el bbox del clic original
    era pequeño.

    Aquí delegamos a :func:`compute_execution_confidence` y devolvemos
    un porcentaje 0-100. Si por alguna razón el evaluador peta, hacemos
    fallback a ``capture_score``.
    """
    try:
        result = compute_execution_confidence(c_step)
        return result.percent
    except Exception:
        pass
    try:
        conf = c_step.target_context.confidence or {}
        cs = conf.get("capture_score")
        if cs is not None:
            return max(0, min(100, int(round(float(cs)))))
    except Exception:
        pass
    return 50


def _capture_score_legacy(c_step: CompiledStep) -> int:
    """Devuelve el ``capture_score`` heredado (visión del recorder).

    Sólo se usa en modo experto, para mostrarlo junto a la
    ``execution_confidence``. Mantiene compatibilidad con misiones
    grabadas antes de FASE 4 (sin ``capture_score`` canónico).
    """
    try:
        conf = c_step.target_context.confidence or {}
        cs = conf.get("capture_score")
        if cs is not None:
            return max(0, min(100, int(round(float(cs)))))
    except Exception:
        pass
    strat = getattr(c_step.action_strategy, "value", str(c_step.action_strategy))
    if strat in ("send_hotkey", "wait_for_state", "scroll", "agent_prompt"):
        return 95
    score = 0
    tc = c_step.target_context
    web = tc.web_data or {}
    locators = web.get("locators") or []
    if locators:
        kinds = {l.get("kind") for l in locators}
        unique_any = any(l.get("unique") for l in locators)
        if "test_id" in kinds or ("role" in kinds and any(l.get("name") for l in locators if l.get("kind") == "role")):
            score += 35
        else:
            score += 20
        if unique_any:
            score += 5
    uia = tc.uia_data or {}
    aid = (uia.get("automation_id") or "").strip() if isinstance(uia, dict) else ""
    name = (uia.get("name") or "").strip() if isinstance(uia, dict) else ""
    ct = (uia.get("control_type") or "").strip().lower() if isinstance(uia, dict) else ""
    GENERIC = {"groupcontrol", "panecontrol", "customcontrol", "documentcontrol",
               "windowcontrol", "toolbarcontrol", "statusbarcontrol"}
    if aid:
        score += 30
    elif name and name.lower() != ct and ct not in GENERIC:
        score += 20
    if tc.anchor_bbox and tc.image_ref_mid:
        score += 15
    elif tc.image_ref:
        score += 7
    if tc.fallback_coords_relative_to_window:
        score += 10
    if tc.fallback_coords:
        score += 5
    return max(0, min(100, score))


def _confidence_color(score: int) -> str:
    """Color por % de confianza con los umbrales del execution_confidence
    (PRD 2026-05-06b): verde ≥ 85, ámbar ≥ 60, rojo si no."""
    if score >= int(EXEC_GREEN_THRESHOLD * 100):
        return "#26de81"   # verde
    if score >= int(EXEC_AMBER_THRESHOLD * 100):
        return "#ffa500"   # amarillo
    return "#ff7675"       # rojo

MINI_BTN = """
    QPushButton {{
        background: {bg}; color: {fg};
        border: 1px solid {border};
        border-radius: 3px; font-size: 9px;
        padding: 0px;
    }}
    QPushButton:hover {{ background: {hover}; }}
"""


# ── Constantes de redimensión ────────────────────────────────────────
_RESIZE_MARGIN = 6  # px de borde sensible para resize


def _heal_stale_select_profile(mission) -> None:
    """Cura defensiva del plan al cargar la misión en Mission Review.

    PRD 2026-05-08d §A.3 — el JSON live persistido por versiones
    antiguas del ``intent_collapse_engine`` puede tener un step
    ``select_profile`` con:

      * ``profile_name`` genérico ("Seleccionar perfil",
        "Profile" …), o
      * ``profile_name`` contaminado por contenido web/Polymer
        (``ytd-*``, ``rendering-content``, ``style-scope``, …), o
      * ``needs_user_label=False`` cuando el profile es claramente
        no resoluble.

    Si detectamos cualquiera de esos síntomas, recompilamos el plan
    desde ``raw_trace`` con ``apply_collapse_to_mission``. Eso
    reinstala el blocker ``EMPTY_PROFILE_NAME_IN_SELECT_PROFILE`` y
    activa la UI de "Confirmar perfil" en Mission Review.

    No-op si:
      * la misión no tiene ``raw_trace`` (no podemos recompilar),
      * no hay ``select_profile`` en el plan,
      * el ``profile_name`` actual ya es válido.
    """
    plan = getattr(mission, "semantic_execution_plan", None) or {}
    steps = plan.get("steps") or []
    if not steps:
        return
    if not getattr(mission, "raw_trace", None):
        return

    try:
        from app.services.missions.profile_resolver import (
            is_generic_profile_label,
            is_invalid_profile_evidence,
        )
    except Exception:
        return

    needs_heal = False
    for step in steps:
        if (step or {}).get("type") != "select_profile":
            continue
        params = step.get("params") or {}
        name = str(params.get("profile_name") or "").strip()
        # Sintoma A: nombre genérico ("Seleccionar perfil") pero
        # marcado como ya resuelto.
        if name and is_generic_profile_label(name):
            needs_heal = True
            break
        # Sintoma B: nombre contaminado por evidencia web (ytd-*,
        # rendering-content, style-scope…).
        if name and is_invalid_profile_evidence(name):
            needs_heal = True
            break
        # Sintoma C: nombre vacío + needs_user_label en False (la UI
        # no mostraría el botón de confirmación).
        if not name and not step.get("needs_user_label"):
            needs_heal = True
            break

    if not needs_heal:
        return

    log.info(
        "[mission_review] plan stale detectado en select_profile; "
        "recompilando desde raw_trace para exponer blocker correcto."
    )
    try:
        from app.services.missions.intent_collapse_engine import (
            apply_collapse_to_mission,
        )
        apply_collapse_to_mission(mission)
    except Exception as e:
        log.warning(
            f"[mission_review] no pude recompilar plan stale: {e}"
        )


class _ReplayWorker(QObject):
    """Worker de replay previo de regrabación (PRD 2026-05-08c §C+G).

    Vive en un ``QThread`` propio para que ``proceed_to_capture()``
    no bloquee la UI. Emite ``progress`` por cada paso ejecutado y
    ``finished`` (con ``ok: bool``) al terminar.
    """
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)  # (ok, error_message)

    def __init__(self, orchestrator) -> None:
        super().__init__()
        self._orchestrator = orchestrator

    def run(self) -> None:  # slot conectado a QThread.started
        orch = self._orchestrator
        # Conectamos el callback de progress del orchestrator a
        # nuestro signal: como el callback corre en este thread,
        # Qt rutea el signal al hilo principal automáticamente
        # (queued connection por defecto).
        try:
            orch._on_progress = (
                lambda i, t, l: self.progress.emit(int(i), int(t), str(l))
            )
        except Exception:
            pass
        try:
            ok = bool(orch.proceed_to_capture())
        except Exception as e:
            log.exception(f"[rerecord] worker error: {e}")
            self.finished.emit(False, str(e))
            return
        msg = ""
        if not ok and orch.session is not None:
            msg = str(orch.session.error_message or "")
        self.finished.emit(ok, msg)


class MissionReviewDialog(QDialog):
    def __init__(
        self,
        mission: Mission,
        parent=None,
        *,
        auto_open_weak_repair: bool = False,
        launch_rerecord_compiled_index: Optional[int] = None,
        runtime_repair_banner: Optional[str] = None,
    ):
        super().__init__(parent)
        # Breadcrumbs INFO para que cualquier access violation deje
        # rastro de la fase exacta en que ocurrió. Sin esto el
        # faulthandler solo muestra ``dialog.exec()`` y no sabemos si
        # el crash fue durante construcción, layout, primer paint, etc.
        log.info(f"Review[1/8] start: {mission.name}")
        try:
            _sanitize_mission_image_refs(mission)
        except Exception as _e:
            log.debug(f"sanitize_mission_image_refs: {_e}")
        log.info("Review[2/8] image_refs sanitized")
        # PRD 2026-05-08d §A.3: cura defensiva del plan al cargar.
        # Si el JSON live trae un ``select_profile`` con
        # ``profile_name`` genérico ("Seleccionar perfil") o
        # contaminado por contenido web (``ytd-*``,
        # ``rendering-content``…), recompilamos el plan para que el
        # bloqueante ``EMPTY_PROFILE_NAME_IN_SELECT_PROFILE`` se
        # exponga correctamente y el botón "Confirmar perfil"
        # aparezca en la UI. Las versiones antiguas del ICE
        # persistían planes con ``profile_name="Seleccionar perfil"``
        # y ``needs_user_label=False`` — esto los reinicia.
        try:
            _heal_stale_select_profile(mission)
        except Exception as _e:
            log.debug(f"heal_stale_select_profile: {_e}")
        try:
            from app.services.missions.uic_migration import (
                migrate_mission_semantic_plan_uic,
            )
            migrate_mission_semantic_plan_uic(mission)
        except Exception as _e:
            log.debug(f"migrate_mission_semantic_plan_uic: {_e}")
        self.mission = mission
        self.did_apply_rerecord_save = False
        self._pending_launch_rerecord_compiled_idx: Optional[int] = (
            launch_rerecord_compiled_index
        )
        self._rerecord_controller = RerecordController()
        self._repair_controller = RepairController(rerecord=self._rerecord_controller)
        self._fragment_recorder = None  # StepFragmentRecorder activo
        self._rerecord_overlay = None   # overlay widget
        self._rerecord_step_idx = -1    # paso siendo regrabado
        # Orquestador de regrabación (PRD 2026-05-08c §C). Se crea
        # cada vez que el usuario presiona "Regrabar" sobre un paso
        # y se libera en ``_cleanup_rerecord``.
        self._rerecord_orchestrator: "RerecordOrchestrator | None" = None
        self._rerecord_thread: "QThread | None" = None
        self._rerecord_worker: "_ReplayWorker | None" = None
        self._rerecord_heartbeat_timer: "QTimer | None" = None
        self._rerecord_last_progress_ms: int = 0
        self._rerecord_save_in_flight: bool = False
        # PRD 2026-05-09 §E + §F — guards centrales del flujo de
        # regrabación (causaban el "freeze" reportado en la cola
        # "Reparar pasos débiles → Regrabar los unos en cola").
        #
        # ``_cleanup_in_progress`` evita la re-entrancia de
        # ``_cleanup_rerecord``. Sin este flag, ``overlay.close()``
        # disparaba ``closeEvent`` → ``cancel_requested`` →
        # ``_on_rerecord_cancel`` → ``_cleanup_rerecord`` recursivo,
        # que terminaba ENCOLANDO ``_process_next_repair_in_queue``
        # dos veces y abriendo dos overlays simultáneos.
        #
        # ``_in_repair_queue`` indica que estamos procesando una
        # cola: en ese modo ``rerecord_step`` salta el modal
        # "Preparando regrabación" (ya consintió al elegir la cola)
        # y muestra "Paso N de M" en el título del overlay.
        self._cleanup_in_progress: bool = False
        self._in_repair_queue: bool = False
        self._repair_queue_total: int = 0
        self._is_maximized = False
        self._restore_geom: QRect = QRect()
        self._drag_pos: QPoint = QPoint()

        # Sin StaysOnTop: durante un replay no interceptamos clicks del usuario
        # ni el player se cruza con el diálogo. NO usamos WA_TranslucentBackground:
        # con drivers de GPU específicos en Windows 10 LTSC 1809 dispara access
        # violation en el paint event del compositor cuando hay ≥1 widget
        # complejo (bug 2026-05-03). Compensamos con background opaco en el
        # ``reviewContainer`` y border-radius — visualmente equivalente.
        # IMPORTANTE: NO usamos ``FramelessWindowHint`` con ``exec()`` modal.
        # Bug 2026-05-03: el chrome custom (sin barra nativa) hacía que
        # Qt corriera el paint compositor C++ por una ruta que crashea
        # con ``access violation`` en el primer paint event en Win10
        # LTSC 1809. El log llegaba a ``Review[7/8]`` (init completo)
        # pero NUNCA a ``Review[8/8]`` (primer paint del singleShot).
        # Eso es sello inequívoco de un crash en el compositor durante
        # el primer ``showEvent``/``paintEvent`` del modal frameless.
        #
        # Solución: usar la barra nativa de Windows. Perdemos el chrome
        # personalizado (drag area custom, mini-buttons), pero ganamos
        # estabilidad 100%. Los usuarios pueden mover/maximizar/cerrar
        # con la barra nativa estándar de Windows.
        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowTitle("Pasos de la automatización — Nevlan")
        # ``setMouseTracking`` ya no es necesario (no hay resize manual).
        self.setMouseTracking(False)

        # Forzamos QFont explícito en el dialog: si Qt elige una fuente
        # bitmap legacy ("MS Serif") como fallback al renderizar
        # cualquier ``QLabel``, DirectWrite crashea con access violation.
        # Segoe UI es nativa de Windows desde Vista — siempre disponible.
        try:
            from PyQt6.QtGui import QFont
            self.setFont(QFont("Segoe UI", 9))
        except Exception:
            pass
        log.info("Review[3/8] window flags (NATIVE TITLE BAR) + font set")

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)

        # Sin WA_TranslucentBackground el dialog necesita un fondo
        # explícito para que no quede gris-default mientras Qt resuelve
        # el painter del frame interno.
        self.setStyleSheet(
            f"QDialog {{ background-color: {NevlanDesign.BG}; }}"
        )

        self.container = QFrame()
        self.container.setStyleSheet(f"""
            QFrame#reviewContainer {{
                background-color: {NevlanDesign.BG};
                border: 1px solid {NevlanDesign.BORDER};
                border-radius: 12px;
            }}
            QToolTip {{
                background-color: #2d3436;
                color: #dfe6e9;
                border: 1px solid #636e72;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
                /* Forzar Segoe UI: sin esto Qt podía caer a la fuente
                   bitmap legacy "MS Serif" y disparar access violation
                   en DirectWrite (bug 2026-05-02). */
                font-family: 'Segoe UI', 'Inter', sans-serif;
            }}
        """)
        self.container.setObjectName("reviewContainer")
        self.container.setMouseTracking(True)
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(12, 8, 12, 10)
        self.container_layout.setSpacing(6)
        self.main_layout.addWidget(self.container)
        log.info("Review[4/8] container ready")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Barra de título: usamos la NATIVA de Windows (más estable).
        # El title_bar custom queda construido pero SIN agregarlo al
        # layout — es lógica que otros métodos pueden referenciar
        # vía ``self.title_bar`` para no romper compatibilidad.
        # Bug 2026-05-03: el chrome frameless + custom title bar
        # disparaba access violation en el primer paint event.
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        self.title_bar = self._build_title_bar()
        self.title_bar.setVisible(False)
        log.info("Review[5/8] title bar built (HIDDEN — using native chrome)")

        # ── Header ──
        header_box = QVBoxLayout()
        header_box.setSpacing(2)
        header_top = QHBoxLayout()
        title_lbl = QLabel("Pasos de la automatización")
        title_lbl.setStyleSheet(
            "color: white; font-size: 20px; font-weight: 800; letter-spacing: 0.2px;"
            " border: none; background: transparent;"
        )
        step_count = len(self.mission.compiled_execution_graph)
        self.count_lbl = QLabel(f"{step_count} pasos")
        self.count_lbl.setStyleSheet(
            "color: #a29bfe; font-size: 11px; font-weight: 600; border: none;"
            " background: rgba(162,155,254,0.12); border-radius: 10px; padding: 2px 10px;"
        )
        header_top.addWidget(title_lbl)
        header_top.addStretch()
        header_top.addWidget(self.count_lbl)
        header_box.addLayout(header_top)
        subtitle = QLabel(
            (runtime_repair_banner or "").strip()
            if (runtime_repair_banner or "").strip()
            else "Edita, prueba o regraba cada paso sin salir de esta vista."
        )
        subtitle.setStyleSheet(
            "color: #8892a6; font-size: 11px; border: none; background: transparent;"
        )
        header_box.addWidget(subtitle)

        self.expert_mode = False
        expert_toggle_row = QHBoxLayout()
        self.chk_expert = QPushButton("Ver detalles técnicos")
        self.chk_expert.setCheckable(True)
        self.chk_expert.setChecked(False)
        self.chk_expert.setStyleSheet(
            "QPushButton { color: #b2bec3; font-size: 10px; border: none; background: transparent; }"
            "QPushButton:checked { color: #a29bfe; font-weight: 600; }"
        )
        self.chk_expert.setToolTip(
            "Modo usuario normal: solo pasos y confianza. "
            "Activa esto para locators, coordenadas, payload y estrategias."
        )
        self.chk_expert.clicked.connect(self._on_toggle_expert_mode)
        expert_toggle_row.addWidget(self.chk_expert)
        expert_toggle_row.addStretch()
        header_box.addLayout(expert_toggle_row)

        # Resumen de robustez (Phase 8): N robustos, M necesitan confirmación
        self.summary_lbl = QLabel("")
        self.summary_lbl.setStyleSheet(
            "color: #b2bec3; font-size: 11px; border: none; background: transparent;"
        )
        header_box.addWidget(self.summary_lbl)

        # PRD 2026-05-08 (revisión §4): panel "Confirmado por usuario".
        # Solo visible en modo experto. La construcción real ocurre en
        # ``_refresh_review_confirmations_panel`` para que se mantenga
        # sincronizado con cada confirmación nueva.
        self.confirmations_panel = QFrame()
        self.confirmations_panel.setObjectName("confirmationsPanel")
        self.confirmations_panel.setStyleSheet(
            "QFrame#confirmationsPanel { "
            "  background: rgba(162,155,254,0.08); "
            "  border: 1px solid rgba(162,155,254,0.30); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #c0b8ff; font-size: 11px; "
            "  border: none; background: transparent; }"
        )
        self.confirmations_panel.setVisible(False)
        self._confirmations_layout = QVBoxLayout(self.confirmations_panel)
        self._confirmations_layout.setContentsMargins(8, 6, 8, 6)
        self._confirmations_layout.setSpacing(2)
        header_box.addWidget(self.confirmations_panel)

        # Semantic-first shadow (Fase 1): diagnóstico solo en modo experto.
        self.semantic_shadow_panel = QFrame()
        self.semantic_shadow_panel.setObjectName("semanticShadowPanel")
        self.semantic_shadow_panel.setStyleSheet(
            "QFrame#semanticShadowPanel { "
            "  background: rgba(0,206,201,0.06); "
            "  border: 1px solid rgba(0,206,201,0.22); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #81ecec; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.semantic_shadow_panel.setVisible(False)
        self._semantic_shadow_layout = QVBoxLayout(self.semantic_shadow_panel)
        self._semantic_shadow_layout.setContentsMargins(8, 6, 8, 6)
        self._semantic_shadow_layout.setSpacing(2)
        header_box.addWidget(self.semantic_shadow_panel)

        self.semantic_sessions_panel = QFrame()
        self.semantic_sessions_panel.setObjectName("semanticSessionsPanel")
        self.semantic_sessions_panel.setStyleSheet(
            "QFrame#semanticSessionsPanel { "
            "  background: rgba(162,155,254,0.05); "
            "  border: 1px solid rgba(162,155,254,0.20); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #dfe6e9; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.semantic_sessions_panel.setVisible(False)
        self._semantic_sessions_layout = QVBoxLayout(self.semantic_sessions_panel)
        self._semantic_sessions_layout.setContentsMargins(8, 6, 8, 6)
        self._semantic_sessions_layout.setSpacing(2)
        header_box.addWidget(self.semantic_sessions_panel)

        # Canonical Operational Blocks (solo experto / sombra).
        self.cob_panel = QFrame()
        self.cob_panel.setObjectName("cobPanel")
        self.cob_panel.setStyleSheet(
            "QFrame#cobPanel { "
            "  background: rgba(253, 203, 110, 0.06); "
            "  border: 1px solid rgba(253, 203, 110, 0.22); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #ffeaa7; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.cob_panel.setVisible(False)
        self._cob_layout = QVBoxLayout(self.cob_panel)
        self._cob_layout.setContentsMargins(8, 6, 8, 6)
        self._cob_layout.setSpacing(2)
        header_box.addWidget(self.cob_panel)

        # Truth Extraction Layer (solo experto / sombra).
        self.tel_panel = QFrame()
        self.tel_panel.setObjectName("telPanel")
        self.tel_panel.setStyleSheet(
            "QFrame#telPanel { "
            "  background: rgba(214, 48, 49, 0.07); "
            "  border: 1px solid rgba(214, 48, 49, 0.28); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #ffb8b8; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.tel_panel.setVisible(False)
        self._tel_layout = QVBoxLayout(self.tel_panel)
        self._tel_layout.setContentsMargins(8, 6, 8, 6)
        self._tel_layout.setSpacing(2)
        header_box.addWidget(self.tel_panel)

        self.tel_sep_promotion_panel = QFrame()
        self.tel_sep_promotion_panel.setObjectName("telSepPromotionPanel")
        self.tel_sep_promotion_panel.setStyleSheet(
            "QFrame#telSepPromotionPanel { "
            "  background: rgba(99, 110, 114, 0.12); "
            "  border: 1px solid rgba(99, 110, 114, 0.35); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #dfe6e9; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.tel_sep_promotion_panel.setVisible(False)
        self._tel_sep_promotion_layout = QVBoxLayout(self.tel_sep_promotion_panel)
        self._tel_sep_promotion_layout.setContentsMargins(8, 6, 8, 6)
        self._tel_sep_promotion_layout.setSpacing(2)
        header_box.addWidget(self.tel_sep_promotion_panel)

        # Adaptive Runtime Layer (Fase 1) — sólo modo experto; observabilidad SAFE.
        self.arl_panel = QFrame()
        self.arl_panel.setObjectName("arlPanel")
        self.arl_panel.setStyleSheet(
            "QFrame#arlPanel { "
            "  background: rgba(162,155,254, 0.05); "
            "  border: 1px solid rgba(162,155,254, 0.25); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #dcd6ff; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.arl_panel.setVisible(False)
        self._arl_layout = QVBoxLayout(self.arl_panel)
        self._arl_layout.setContentsMargins(8, 6, 8, 6)
        self._arl_layout.setSpacing(2)
        header_box.addWidget(self.arl_panel)

        self.sll_panel = QFrame()
        self.sll_panel.setObjectName("sllPanel")
        self.sll_panel.setStyleSheet(
            "QFrame#sllPanel { "
            "  background: rgba(0, 184, 148, 0.06); "
            "  border: 1px solid rgba(0, 184, 148, 0.24); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #55efc4; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.sll_panel.setVisible(False)
        self._sll_layout = QVBoxLayout(self.sll_panel)
        self._sll_layout.setContentsMargins(8, 6, 8, 6)
        self._sll_layout.setSpacing(2)
        header_box.addWidget(self.sll_panel)

        self.ompc_panel = QFrame()
        self.ompc_panel.setObjectName("ompcPanel")
        self.ompc_panel.setStyleSheet(
            "QFrame#ompcPanel { "
            "  background: rgba(108, 92, 231, 0.07); "
            "  border: 1px solid rgba(108, 92, 231, 0.28); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #dcd6ff; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.ompc_panel.setVisible(False)
        self._ompc_layout = QVBoxLayout(self.ompc_panel)
        self._ompc_layout.setContentsMargins(8, 6, 8, 6)
        self._ompc_layout.setSpacing(2)
        header_box.addWidget(self.ompc_panel)

        # Operational Runtime Graph (ORG) — modo sombra; no cambia ejecutor.
        self.org_panel = QFrame()
        self.org_panel.setObjectName("orgPanel")
        self.org_panel.setStyleSheet(
            "QFrame#orgPanel { "
            "  background: rgba(116, 185, 255, 0.06); "
            "  border: 1px solid rgba(116, 185, 255, 0.28); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #74b9ff; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.org_panel.setVisible(False)
        self._org_layout = QVBoxLayout(self.org_panel)
        self._org_layout.setContentsMargins(8, 6, 8, 6)
        self._org_layout.setSpacing(2)
        header_box.addWidget(self.org_panel)

        # Operational Continuity Runtime (OCR) — sombra; no muta SEP ni ejecutores.
        self.ocr_panel = QFrame()
        self.ocr_panel.setObjectName("ocrPanel")
        self.ocr_panel.setStyleSheet(
            "QFrame#ocrPanel { "
            "  background: rgba(253, 203, 110, 0.07); "
            "  border: 1px solid rgba(253, 203, 110, 0.32); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #fdcb6e; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.ocr_panel.setVisible(False)
        self._ocr_layout = QVBoxLayout(self.ocr_panel)
        self._ocr_layout.setContentsMargins(8, 6, 8, 6)
        self._ocr_layout.setSpacing(2)
        header_box.addWidget(self.ocr_panel)

        # Goal-Oriented Operational Runtime (GOOR) — sombra declarativa objetivo-centric.
        self.goor_panel = QFrame()
        self.goor_panel.setObjectName("goorPanel")
        self.goor_panel.setStyleSheet(
            "QFrame#goorPanel { "
            "  background: rgba(253, 121, 168, 0.07); "
            "  border: 1px solid rgba(253, 121, 168, 0.32); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #ff9ecd; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.goor_panel.setVisible(False)
        self._goor_layout = QVBoxLayout(self.goor_panel)
        self._goor_layout.setContentsMargins(8, 6, 8, 6)
        self._goor_layout.setSpacing(2)
        header_box.addWidget(self.goor_panel)

        # Live Operational Perception (LOP) — sombra; percepción viva sin ejecutar.
        self.lop_panel = QFrame()
        self.lop_panel.setObjectName("lopPanel")
        self.lop_panel.setStyleSheet(
            "QFrame#lopPanel { "
            "  background: rgba(99, 205, 218, 0.08); "
            "  border: 1px solid rgba(99, 205, 218, 0.35); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #b2ebf2; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.lop_panel.setVisible(False)
        self._lop_layout = QVBoxLayout(self.lop_panel)
        self._lop_layout.setContentsMargins(8, 6, 8, 6)
        self._lop_layout.setSpacing(2)
        header_box.addWidget(self.lop_panel)

        # Pre-Click Operational Freeze (PCOF) — experto; snapshot pre-transición.
        self.pcof_panel = QFrame()
        self.pcof_panel.setObjectName("pcofPanel")
        self.pcof_panel.setStyleSheet(
            "QFrame#pcofPanel { "
            "  background: rgba(156, 204, 101, 0.08); "
            "  border: 1px solid rgba(156, 204, 101, 0.35); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #dcedc8; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.pcof_panel.setVisible(False)
        self._pcof_layout = QVBoxLayout(self.pcof_panel)
        self._pcof_layout.setContentsMargins(8, 6, 8, 6)
        self._pcof_layout.setSpacing(2)
        header_box.addWidget(self.pcof_panel)

        # UOL Runtime Acceptance — solo modo experto (telemetría de ejecución).
        self.uol_runtime_panel = QFrame()
        self.uol_runtime_panel.setObjectName("uolRuntimePanel")
        self.uol_runtime_panel.setStyleSheet(
            "QFrame#uolRuntimePanel { "
            "  background: rgba(0, 206, 201, 0.08); "
            "  border: 1px solid rgba(0, 206, 201, 0.35); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #81ecec; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.uol_runtime_panel.setVisible(False)
        self._uol_runtime_layout = QVBoxLayout(self.uol_runtime_panel)
        self._uol_runtime_layout.setContentsMargins(8, 6, 8, 6)
        self._uol_runtime_layout.setSpacing(2)
        header_box.addWidget(self.uol_runtime_panel)

        # ORCE — consistencia runtime vs Machine Layer (modo experto).
        self.orce_panel = QFrame()
        self.orce_panel.setObjectName("orcePanel")
        self.orce_panel.setStyleSheet(
            "QFrame#orcePanel { "
            "  background: rgba(253, 203, 110, 0.08); "
            "  border: 1px solid rgba(253, 203, 110, 0.35); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #ffeaa7; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.orce_panel.setVisible(False)
        self._orce_layout = QVBoxLayout(self.orce_panel)
        self._orce_layout.setContentsMargins(8, 6, 8, 6)
        self._orce_layout.setSpacing(2)
        header_box.addWidget(self.orce_panel)

        # Beta Runtime Acceptance — UOL/ORCE/OSUE unificado.
        self.beta_acceptance_panel = QFrame()
        self.beta_acceptance_panel.setObjectName("betaAcceptancePanel")
        self.beta_acceptance_panel.setStyleSheet(
            "QFrame#betaAcceptancePanel { "
            "  background: rgba(162, 155, 254, 0.08); "
            "  border: 1px solid rgba(162, 155, 254, 0.35); "
            "  border-radius: 6px; "
            "} "
            "QLabel { color: #dfe6e9; font-size: 10px; "
            "  border: none; background: transparent; }"
        )
        self.beta_acceptance_panel.setVisible(False)
        self._beta_acceptance_layout = QVBoxLayout(self.beta_acceptance_panel)
        self._beta_acceptance_layout.setContentsMargins(8, 6, 8, 6)
        self._beta_acceptance_layout.setSpacing(2)
        header_box.addWidget(self.beta_acceptance_panel)

        self.beta_acceptance_normal_lbl = QLabel("")
        self.beta_acceptance_normal_lbl.setStyleSheet(
            "color: #00b894; font-size: 11px; font-weight: bold; "
            "border: none; background: transparent;"
        )
        self.beta_acceptance_normal_lbl.setVisible(False)
        header_box.addWidget(self.beta_acceptance_normal_lbl)

        self.container_layout.addLayout(header_box)
        
        # ── Name ──
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_lbl = QLabel("Nombre:")
        name_lbl.setStyleSheet("color: #aaa; font-size: 11px; border: none; background: transparent;")
        name_lbl.setFixedWidth(55)
        self.name_edit = QLineEdit(self.mission.name)
        self.name_edit.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(0,0,0,0.3);
                border: 1px solid {NevlanDesign.BORDER};
                border-radius: 4px; color: white;
                padding: 3px 6px; font-size: 12px;
            }}
        """)
        self.name_edit.setFixedHeight(26)
        name_row.addWidget(name_lbl)
        name_row.addWidget(self.name_edit)
        self.container_layout.addLayout(name_row)
        
        # ── Divider ──
        self.container_layout.addWidget(self._divider())

        # ── Quick actions bar (FASE 10) ─────────────────────────────
        # Acciones de toda la misión: probar, reparar débiles, optimizar.
        quick_actions = QHBoxLayout()
        quick_actions.setSpacing(4)
        quick_actions.setContentsMargins(0, 0, 0, 2)

        self.btn_test_mission = QPushButton("🧪 Probar misión")
        self.btn_test_mission.setFixedHeight(22)
        self.btn_test_mission.setToolTip(
            "Ejecuta toda la misión de inicio a fin para validar que funciona."
        )
        self.btn_test_mission.setStyleSheet(f"""
            QPushButton {{
                background: rgba(0,206,201,0.15); color: #00cec9;
                border: 1px solid rgba(0,206,201,0.25);
                border-radius: 4px; padding: 0 10px; font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(0,206,201,0.30); }}
        """)
        self.btn_test_mission.clicked.connect(self.test_full_mission)
        quick_actions.addWidget(self.btn_test_mission)

        self.btn_repair_weak = QPushButton("🩹 Reparar débiles")
        self.btn_repair_weak.setFixedHeight(22)
        self.btn_repair_weak.setToolTip(
            "Recorre los pasos con score < 70 y te pide elegir el elemento correcto."
        )
        self.btn_repair_weak.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255,200,80,0.15); color: #ffd166;
                border: 1px solid rgba(255,200,80,0.25);
                border-radius: 4px; padding: 0 10px; font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(255,200,80,0.30); }}
        """)
        self.btn_repair_weak.clicked.connect(self.repair_weak_steps)
        quick_actions.addWidget(self.btn_repair_weak)

        self.btn_optimize = QPushButton("⚡ Optimizar")
        self.btn_optimize.setFixedHeight(22)
        self.btn_optimize.setToolTip(
            "Aplica MissionOptimizer: fusiona pasos, reemplaza typewrite por fill,"
            " elimina coords absolutas si hay alternativa, etc."
        )
        self.btn_optimize.setStyleSheet(f"""
            QPushButton {{
                background: rgba(162,155,254,0.15); color: #a29bfe;
                border: 1px solid rgba(162,155,254,0.25);
                border-radius: 4px; padding: 0 10px; font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(162,155,254,0.30); }}
        """)
        self.btn_optimize.clicked.connect(self.optimize_mission)
        quick_actions.addWidget(self.btn_optimize)

        # ── V2: Vista simple + Time-Travel ─────────────────────────
        self.btn_simple = QPushButton("👁 Vista simple")
        self.btn_simple.setFixedHeight(22)
        self.btn_simple.setToolTip(
            "Bloques semánticos humanos en lugar de pasos técnicos."
        )
        self.btn_simple.setStyleSheet(f"""
            QPushButton {{
                background: rgba(120,200,150,0.15); color: #9ad97a;
                border: 1px solid rgba(120,200,150,0.25);
                border-radius: 4px; padding: 0 10px; font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(120,200,150,0.30); }}
        """)
        self.btn_simple.clicked.connect(self._open_simple_view)
        quick_actions.addWidget(self.btn_simple)

        self.btn_timetravel = QPushButton("🎞 Time-Travel")
        self.btn_timetravel.setFixedHeight(22)
        self.btn_timetravel.setToolTip(
            "Recorre la misión paso a paso con screenshot + highlight."
        )
        self.btn_timetravel.setStyleSheet(f"""
            QPushButton {{
                background: rgba(140,140,255,0.15); color: #b6b3ff;
                border: 1px solid rgba(140,140,255,0.25);
                border-radius: 4px; padding: 0 10px; font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(140,140,255,0.30); }}
        """)
        self.btn_timetravel.clicked.connect(self._open_time_travel)
        quick_actions.addWidget(self.btn_timetravel)

        quick_actions.addStretch()
        self.container_layout.addLayout(quick_actions)

        # ── Scroll area for cards ──
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        # Solo scroll vertical: las tarjetas se reflejan al ancho disponible
        # y los iconos siempre quedan visibles (la descripción cede espacio).
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical {
                background: rgba(255,255,255,5); width: 6px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,25); border-radius: 3px; min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        
        self.cards_widget = QWidget()
        self.cards_widget.setStyleSheet("background: transparent; border: none;")
        self.cards_layout = QVBoxLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 2, 0)
        self.cards_layout.setSpacing(3)

        # Placeholder mientras se construyen las tarjetas. Diferimos el
        # build a un singleShot(0) para que la ventana se MUESTRE primero
        # y Qt termine su setup antes de que iteremos sobre N pasos
        # cargando QPixmap, conectando lambdas, etc. Sin este defer, una
        # excepción durante ``_build_cards`` puede dejar el QDialog en
        # estado inconsistente y disparar un segfault al cerrar — lo que
        # se observó como "Nevlan se cierra al abrir el editor" en el
        # bug del 2026-05-01.
        loading_lbl = QLabel("Cargando pasos…")
        loading_lbl.setStyleSheet(
            "color: #8892a6; font-size: 11px; padding: 12px;"
            " border: none; background: transparent;"
        )
        loading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_lbl.setObjectName("missionCardsLoading")
        self.cards_layout.addWidget(loading_lbl)
        self.cards_layout.addStretch()
        self.scroll.setWidget(self.cards_widget)
        self.container_layout.addWidget(self.scroll, 1)  # stretch factor

        log.info("Review[6/8] scroll area ready, deferring _build_cards")
        QTimer.singleShot(0, self._build_cards_safely)

        # ── Divider ──
        self.container_layout.addWidget(self._divider())
        
        # ── Action buttons ──
        actions = QHBoxLayout()
        actions.setSpacing(4)
        actions.setContentsMargins(0, 0, 0, 0)
        
        self.btn_delete = QPushButton("🗑️")
        self.btn_delete.setFixedSize(26, 24)
        self.btn_delete.setToolTip("Eliminar automatización")
        self.btn_delete.setStyleSheet("""
            QPushButton { background: rgba(255,0,0,0.15); color: #ff7675; border: 1px solid rgba(255,0,0,0.2); border-radius: 4px; font-size: 11px; }
            QPushButton:hover { background: rgba(255,0,0,0.35); }
        """)

        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setFixedHeight(24)
        self.btn_save = QPushButton("Guardar")
        self.btn_save.setFixedHeight(24)

        self.btn_cancel.setStyleSheet(f"""
            QPushButton {{ background: {NevlanDesign.BTN_BG}; color: white; border: 1px solid {NevlanDesign.BORDER}; border-radius: 4px; padding: 0 10px; font-size: 10px; }}
            QPushButton:hover {{ background: {NevlanDesign.BTN_HOVER}; }}
            QPushButton:focus {{ outline: 2px solid #74b9ff; outline-offset: 1px; }}
        """)
        self.btn_save.setStyleSheet("""
            QPushButton { background: #0984e3; color: white; border: none; border-radius: 4px; padding: 0 12px; font-size: 10px; font-weight: bold; }
            QPushButton:hover { background: #74b9ff; }
            QPushButton:focus { outline: 2px solid #ffffff; outline-offset: 1px; }
        """)

        # ── Hotkeys (BLOQUEADOR fix) ───────────────────────────────────
        # Enter ⇒ Guardar (default action), Esc ⇒ Cancelar, F5 ⇒ Repetir
        # acción primaria. setDefault hace que Enter dispare incluso si
        # el foco está en otro widget que no consume la tecla. Tab/Shift-Tab
        # ya navegan por el orden natural (FocusPolicy default Strong).
        self.btn_save.setDefault(True)
        self.btn_save.setAutoDefault(True)
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.setShortcut("Escape")

        self.btn_delete.clicked.connect(self.delete_mission)
        self.btn_cancel.clicked.connect(self.cancel_edition)
        self.btn_save.clicked.connect(self.save_mission)
        
        actions.addWidget(self.btn_delete)
        actions.addStretch()
        actions.addWidget(self.btn_cancel)
        actions.addWidget(self.btn_save)

        # Sin QSizeGrip: con la barra nativa, el redimensionamiento se
        # hace con los bordes nativos de Windows. Un QSizeGrip + paint
        # event del compositor era otra fuente potencial de access
        # violation (bug 2026-05-03).
        self.container_layout.addLayout(actions)

        # ── Tamaños proporcionales ──────────────────────────────────
        # Min: cabe en pantallas pequeñas. Default: cómodo (~70% de 1080p).
        self.setMinimumSize(560, 460)
        # Tomamos un default proporcional a la pantalla, máximo 900x780.
        try:
            from PyQt6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen().availableGeometry()
            w0 = min(900, max(560, int(screen.width() * 0.45)))
            h0 = min(820, max(460, int(screen.height() * 0.72)))
            self.resize(w0, h0)
        except Exception:
            self.resize(720, 640)
        self._center_on_screen()

        self._auto_open_weak_repair = bool(auto_open_weak_repair)
        if self._auto_open_weak_repair:
            QTimer.singleShot(320, self._maybe_launch_weak_repair_from_review)

        # Cuando el modal se muestra, enfocamos el botón Guardar para
        # que Enter actúe inmediatamente sin necesidad de un click previo.
        # Lo diferimos a un singleShot porque Qt resetea el foco al primer
        # widget en showEvent y debemos correr DESPUÉS.
        QTimer.singleShot(0, self._set_initial_focus)
        log.info("Review[7/8] init complete, awaiting first paint")

    # ── Hotkeys / focus management ─────────────────────────────────────

    def _set_initial_focus(self) -> None:
        """Enfoca el botón Guardar al abrir el diálogo. Garantiza que
        Enter funcione inmediatamente, sin click previo. Si por alguna
        razón el botón no existe (init parcial), no rompe nada.
        """
        try:
            if hasattr(self, "btn_save") and self.btn_save is not None:
                self.btn_save.setFocus(Qt.FocusReason.OtherFocusReason)
        except Exception as _e:
            log.debug(f"_set_initial_focus: {_e}")

    def keyPressEvent(self, event):  # type: ignore[override]
        """Hotkeys globales del Mission Review (BLOQUEADOR fix):
            Enter / Return  → Guardar (si el foco no está en un editor
                              de texto multi-línea que necesite la tecla).
            Escape          → Cancelar (sin importar foco).
            F5              → Repetir / Regrabar último paso enfocado o
                              abrir Repair Mode si hay pasos rojos.
            Ctrl+S          → Guardar (atajo profesional opcional).
        """
        try:
            from PyQt6.QtWidgets import QLineEdit, QTextEdit, QPlainTextEdit
            key = event.key()
            mods = event.modifiers()

            if key == Qt.Key.Key_Escape:
                self.cancel_edition()
                event.accept()
                return

            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                # No interceptar Enter cuando el foco está en QLineEdit
                # del nombre (queremos que confirme la edición), pero SÍ
                # cuando está en cualquier otro lugar (lista de tarjetas,
                # botones, scroll). Para QLineEdit/Combobox simples, dejar
                # que Qt lo maneje vía editingFinished y luego disparar
                # save al perder foco no es trivial — preferimos que Enter
                # SIEMPRE guarde, y los QLineEdit con focus pierdan foco
                # al confirmar (comportamiento estándar Qt).
                fw = self.focusWidget()
                if isinstance(fw, (QTextEdit, QPlainTextEdit)):
                    return super().keyPressEvent(event)
                self.save_mission()
                event.accept()
                return

            if key == Qt.Key.Key_F5:
                # Trigger repair mode / weak step session si existe.
                handler = getattr(
                    self, "_maybe_launch_weak_repair_from_review", None
                )
                if callable(handler):
                    try:
                        handler()
                    except Exception as e:
                        log.debug(f"F5 weak repair: {e}")
                event.accept()
                return

            if key == Qt.Key.Key_S and mods & Qt.KeyboardModifier.ControlModifier:
                self.save_mission()
                event.accept()
                return
        except Exception as _e:
            log.debug(f"keyPressEvent: {_e}")
        return super().keyPressEvent(event)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TITLE BAR + WINDOW CHROME (custom)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_title_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("missionTitleBar")
        bar.setFixedHeight(28)
        bar.setStyleSheet(
            "QFrame#missionTitleBar { background: transparent; border: none; }"
        )
        bar.setMouseTracking(True)
        # Drag para mover la ventana — solo cuando no está maximizada.
        bar.mousePressEvent = self._title_mouse_press   # type: ignore[assignment]
        bar.mouseMoveEvent = self._title_mouse_move     # type: ignore[assignment]
        bar.mouseDoubleClickEvent = self._title_double_click  # type: ignore[assignment]

        h = QHBoxLayout(bar)
        h.setContentsMargins(2, 0, 0, 0)
        h.setSpacing(4)

        brand = QLabel("●  Nevlan")
        brand.setStyleSheet(
            "color: #a29bfe; font-size: 11px; font-weight: 700;"
            " letter-spacing: 0.4px; background: transparent; border: none;"
            " padding: 0 6px;"
        )
        h.addWidget(brand)
        h.addStretch()

        def chrome_btn(symbol: str, fg: str, hover: str, tip: str) -> QPushButton:
            b = QPushButton(symbol)
            b.setFixedSize(28, 22)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {fg};"
                f" border: none; font-size: 12px; }}"
                f"QPushButton:hover {{ background: {hover}; border-radius: 4px; }}"
            )
            return b

        self.btn_min = chrome_btn("—", "#b2bec3", "rgba(255,255,255,0.10)", "Minimizar")
        self.btn_max = chrome_btn("□", "#b2bec3", "rgba(255,255,255,0.10)", "Maximizar")
        self.btn_close = chrome_btn("✕", "#dfe6e9", "rgba(232,67,67,0.55)", "Cerrar")

        self.btn_min.clicked.connect(self.showMinimized)
        self.btn_max.clicked.connect(self._toggle_max_restore)
        self.btn_close.clicked.connect(self.cancel_edition)

        h.addWidget(self.btn_min)
        h.addWidget(self.btn_max)
        h.addWidget(self.btn_close)
        return bar

    def _toggle_max_restore(self):
        if self.isMaximized():
            self.showNormal()
            self.btn_max.setText("□")
            self.btn_max.setToolTip("Maximizar")
        else:
            self.showMaximized()
            self.btn_max.setText("🗗")
            self.btn_max.setToolTip("Restaurar")

    def _title_mouse_press(self, ev: QMouseEvent):
        if ev.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
            self._drag_pos = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()

    def _title_mouse_move(self, ev: QMouseEvent):
        if (ev.buttons() & Qt.MouseButton.LeftButton) and not self._drag_pos.isNull():
            if self.isMaximized():
                self._toggle_max_restore()
                self._drag_pos = QPoint(self.width() // 2, 12)
            self.move(ev.globalPosition().toPoint() - self._drag_pos)
            ev.accept()

    def _title_double_click(self, ev: QMouseEvent):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._toggle_max_restore()

    # ``nativeEvent`` ELIMINADO: con la barra nativa, Windows ya
    # maneja WM_NCHITTEST por su cuenta. Mantener el override
    # interfería con el HitTest nativo y se sospechaba de causar el
    # access violation del primer paint event (bug 2026-05-03). Si en
    # el futuro vuelve el chrome custom, hay que reintroducirlo CON
    # un guard explícito ``if FramelessWindowHint in flags``.

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # HELPERS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    def _divider(self):
        d = QFrame()
        d.setFixedHeight(1)
        d.setStyleSheet("background: rgba(255,255,255,15); border: none;")
        return d
    
    def _center_on_screen(self):
        try:
            from PyQt6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen().geometry()
            w, h = self.width(), self.height()
            self.move((screen.width() - w) // 2, (screen.height() - h) // 2)
        except:
            pass

    # ── Lifecycle hooks con breadcrumbs ─────────────────────────────
    # Estos hooks no hacen nada funcional — solo loguean para que un
    # crash en el primer paint event deje rastro de hasta dónde llegó
    # el lifecycle de Qt. Bug 2026-05-03: el log mostraba ``Review[7/8]``
    # (init complete) pero crasheaba en el primer paint sin más rastro.
    _logged_first_show = False
    _logged_first_paint = False

    def showEvent(self, event):
        if not self._logged_first_show:
            log.info("Review[paint] showEvent dispatched")
            self._logged_first_show = True
        try:
            super().showEvent(event)
        except Exception as e:
            log.exception(f"showEvent crash: {e}")

    def paintEvent(self, event):
        if not self._logged_first_paint:
            log.info("Review[paint] first paintEvent OK")
            self._logged_first_paint = True
        try:
            super().paintEvent(event)
        except Exception as e:
            log.exception(f"paintEvent crash: {e}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # CARD BUILDER
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    def _build_cards_safely(self):
        """Wrapper deferred sobre ``_build_cards``.

        Cualquier excepción durante la creación de tarjetas se persiste
        a ``var/crashes/`` y se muestra un placeholder de error en lugar
        de dejar el QDialog medio construido — eso era lo que disparaba
        el segfault al cerrar el centro de automatizaciones.
        """
        log.info("Review[8/8] _build_cards starting")
        try:
            # Limpia el placeholder "Cargando pasos…" si aún está.
            for i in reversed(range(self.cards_layout.count())):
                w = self.cards_layout.itemAt(i).widget()
                if w is not None and w.objectName() == "missionCardsLoading":
                    w.deleteLater()
            self._build_cards()
            self.cards_layout.addStretch()
            try:
                self._refresh_review_confirmations_panel()
            except Exception as e:
                log.debug(f"[mission_review] confirm panel init: {e}")
            try:
                self._refresh_semantic_shadow_panel()
            except Exception as e:
                log.debug(f"[mission_review] semantic shadow init: {e}")
            try:
                self._refresh_semantic_sessions_panel()
            except Exception as e:
                log.debug(f"[mission_review] semantic sessions init: {e}")
            log.info("Review[8/8] _build_cards done")
            try:
                self._sync_truth_gate_quick_actions()
            except Exception as e:
                log.debug(f"[mission_review] sync truth gate actions: {e}")
            if self._pending_launch_rerecord_compiled_idx is not None:
                try:
                    QTimer.singleShot(140, self._maybe_deferred_runtime_rerecord)
                except Exception as e:
                    log.debug(f"defer runtime rerecord: {e}")
        except Exception as e:
            try:
                from app.core.crash_handler import write_manual_crash
                write_manual_crash("mission_review_build_cards", e)
            except Exception:
                pass
            log.exception(f"_build_cards falló: {e}")
            # Mostramos un fallback legible en vez de cerrar la app.
            err_lbl = QLabel(
                "No se pudieron cargar todos los pasos.\n"
                "Revisa var/crashes/ para el detalle.\n\n"
                f"Detalle: {type(e).__name__}: {e}"
            )
            err_lbl.setWordWrap(True)
            err_lbl.setStyleSheet(
                "color: #ff7675; font-size: 11px; padding: 14px;"
                " border: 1px solid rgba(255,118,117,40); border-radius: 6px;"
                " background: rgba(255,118,117,15);"
            )
            self.cards_layout.addWidget(err_lbl)
            self.cards_layout.addStretch()

    def _sync_truth_gate_quick_actions(self) -> None:
        """🧪 Probar misión sigue ``is_executable`` / SEP, no ``mission.status``."""
        if not hasattr(self, "btn_test_mission"):
            return
        ok = mission_ui_should_offer_execute_and_test(self.mission)
        self.btn_test_mission.setEnabled(ok)
        base = (
            "Ejecuta toda la misión de inicio a fin para validar que funciona."
        )
        if ok:
            self.btn_test_mission.setToolTip(base)
            return
        st = compute_mission_ui_execution_state(self.mission)
        parts = list(st.visible_blockers[:5])
        hint = ", ".join(parts) if parts else ""
        if not hint and st.gate_blockers:
            hint = str(st.gate_blockers[0])
        self.btn_test_mission.setToolTip(
            base + " " + (f"Bloqueado: {hint}" if hint else "(no ejecutable)"),
        )

    def _maybe_deferred_runtime_rerecord(self) -> None:
        idx = getattr(self, "_pending_launch_rerecord_compiled_idx", None)
        self._pending_launch_rerecord_compiled_idx = None
        if idx is None or int(idx) < 0:
            return
        graph = list(getattr(self.mission, "compiled_execution_graph", None) or [])
        if not graph:
            log.warning("[rerecord] auto-launch omitido — sin grafos compilados")
            return
        bounded = max(0, min(int(idx), len(graph) - 1))
        try:
            self.rerecord_step(bounded)
        except Exception as e:
            log.warning(f"[rerecord] auto-launch Centro→Review: {e}")

    def _count_label_text_for_current_view(self) -> str:
        """Texto del contador superior — alinea con la fuente de verdad."""
        n_graph = len(self.mission.compiled_execution_graph or [])
        sem_n = len(
            ((self.mission.semantic_execution_plan or {}) or {}).get("steps") or []
        )
        if getattr(self, "_used_semantic_primary_cards", False) and sem_n:
            return f"{sem_n} pasos · plan semántico"
        return f"{n_graph} pasos"

    def _build_cards_semantic_primary(self) -> None:
        """Vista normal cuando el SEP es SIPE: intenciones, no eventos legacy.

        No muestra ``compute_execution_confidence`` sobre
        ``compiled_execution_graph`` ni badges de última ejecución con
        click_fallback — eso queda en modo experto.
        """
        from app.services.missions.mission_review_summary import (
            build_mission_review_summary,
        )

        summary = build_mission_review_summary(self.mission)
        sep = (self.mission.semantic_execution_plan or {}) or {}
        steps_blob = list(sep.get("steps") or [])

        hint = QLabel(
            "Intenciones según el plan semántico (SIPE). Para métricas "
            "por clic, captura y coords del recorder, activa "
            "«Ver detalles técnicos»."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "color: #95a5a6; font-size: 10px; padding: 4px 2px 10px 2px; "
            "border: none; background: transparent;"
        )
        self.cards_layout.addWidget(hint)

        self._four_layer_step_ids: List[str] = []

        for i, sl in enumerate(summary.steps):
            sp = steps_blob[i] if i < len(steps_blob) else {}
            sid = str(sl.machine_step_id or sp.get("id") or "")
            if sid:
                self._four_layer_step_ids.append(sid)
            conf_f = float(sp.get("confidence") or 0.92)
            if conf_f > 1.0:
                pct = int(conf_f)
            else:
                pct = int(round(max(0.0, min(1.0, conf_f)) * 100))

            exec_truth = should_suppress_legacy_capture_noise(self.mission)
            if sl.disabled:
                sub_line = "  Desactivado — el runtime omitirá este paso"
            elif exec_truth:
                sub_line = (
                    "  Lista para ejecutar · identidad validada antes "
                    "de transición"
                )
            else:
                sub_line = f"  Ejecutable · plan semántico · {pct}%"

            card = QFrame()
            card.setObjectName(f"semCard_{i}")
            border_color = "#636e72" if sl.disabled else "#00b894"
            card.setStyleSheet(
                f"""
                QFrame#semCard_{i} {{
                    background: rgba(255,255,255,4);
                    border: 1px solid rgba(255,255,255,18);
                    border-left: 3px solid {border_color};
                    border-radius: 6px;
                }}
                """
            )
            lay = QVBoxLayout(card)
            lay.setContentsMargins(7, 6, 7, 6)
            lay.setSpacing(3)

            row1 = QHBoxLayout()
            num = QLabel(f"{sl.index}")
            num.setFixedWidth(22)
            num.setStyleSheet(
                "color:#a29bfe;font-weight:bold;font-size:11px;"
                "border:none;background:transparent;"
            )
            title = QLabel(sl.human_label)
            title.setWordWrap(True)
            title_style = (
                "color:#636e72;font-size:12px;font-weight:600;text-decoration:line-through;"
                if sl.disabled
                else "color:#dfe6e9;font-size:12px;font-weight:600;"
            )
            title.setStyleSheet(
                f"{title_style}border:none;background:transparent;"
            )
            row1.addWidget(num)
            row1.addWidget(title, 1)

            if sid:
                edit_btn = QPushButton("✎")
                edit_btn.setFixedSize(22, 22)
                edit_btn.setToolTip("Editar paso (etiqueta, parámetros, orden)")
                edit_btn.setStyleSheet(MINI_BTN.format(
                    bg="rgba(116,185,255,0.15)", fg="#74b9ff",
                    border="rgba(116,185,255,0.25)", hover="rgba(116,185,255,0.35)"))
                edit_btn.clicked.connect(
                    lambda checked=False, step_id=sid: self._open_four_layer_step_menu(step_id),
                )
                row1.addWidget(edit_btn)

            test_btn = QPushButton("🧪")
            test_btn.setFixedSize(22, 22)
            test_btn.setToolTip("Probar este paso (plan semántico)")
            test_btn.setStyleSheet(MINI_BTN.format(
                bg="rgba(0,206,201,0.15)", fg="#00cec9",
                border="rgba(0,206,201,0.2)", hover="rgba(0,206,201,0.35)"))
            test_btn.clicked.connect(lambda checked, idx=i: self.test_semantic_step(idx))
            row1.addWidget(test_btn)

            lay.addLayout(row1)

            sub = QLabel(sub_line)
            sub.setStyleSheet(
                "color:#00cec9;font-size:9px;font-weight:700;padding-left:24px;"
                "border:none;background:transparent;"
            )
            lay.addWidget(sub)

            if sl.badges:
                bb = QLabel("  " + "  ".join(sl.badges))
                bb.setWordWrap(True)
                bb.setStyleSheet(
                    "color:#b2bec3;font-size:8px;padding-left:24px;"
                    "border:none;background:transparent;"
                )
                lay.addWidget(bb)

            if sl.needs_review:
                wrn = QLabel("  ⚠ Requiere confirmación humana")
                wrn.setStyleSheet(
                    "color:#ffa502;font-size:9px;padding-left:24px;"
                    "border:none;background:transparent;"
                )
                lay.addWidget(wrn)

            self.cards_layout.addWidget(card)

        try:
            self._append_four_layer_expert_panel()
        except Exception as e:
            log.debug(f"[mission_review] four layer expert panel: {e}")

        if hasattr(self, "summary_lbl"):
            n = len(steps_blob)
            coords_decl = plan_declares_coords_used(self.mission)
            parts = [f"📊 {n} pasos (intención)", summary.status_label]
            if coords_decl is False:
                parts.append("📍 Sin coords como estrategia primaria (SEP)")
            elif coords_decl is True:
                parts.append("⚠ SEP marca coords_used")
            if summary.visible_blockers:
                parts.append(
                    f"⚠ {len(summary.visible_blockers)} revisión(es)"
                )
            self.summary_lbl.setText(" · ".join(parts))

    def _build_cards(self):
        self._used_semantic_primary_cards = False
        if use_semantic_primary_normal_mission_review(
            self.mission,
            expert_mode=bool(getattr(self, "expert_mode", False)),
        ):
            self._used_semantic_primary_cards = True
            self._build_cards_semantic_primary()
            return

        steps = self.mission.compiled_execution_graph
        interp = self.mission.interpreted_steps

        step_to_grp: dict = {}
        for gx in getattr(self.mission, "action_groups", []) or []:
            for sid in gx.step_ids:
                step_to_grp.setdefault(sid, gx)
        last_group_id: list = [None]

        robust = 0
        review = 0
        coord_only = 0
        log.info(f"_build_cards: iterando {len(steps)} pasos")
        for i, c_step in enumerate(steps):
            # Breadcrumb por paso. Si crashea Qt durante construcción de
            # un paso específico, este log nos dice cuál (sin spamear si
            # son pocos pasos).
            if len(steps) <= 30 or (i % 5 == 0):
                log.debug(f"_build_cards: paso {i+1}/{len(steps)}")
            desc_raw = interp[i].description if i < len(interp) else "Paso"
            desc = normal_mode_review_caption_for_compiled(
                self.mission,
                getattr(c_step, "id", ""),
                desc_raw,
                expert_mode=bool(getattr(self, "expert_mode", False)),
            )
            action_strat = getattr(c_step.action_strategy, 'value', str(c_step.action_strategy))
            _i_interp = interp[i] if i < len(interp) else None
            _sq = compute_step_quality(c_step, _i_interp)
            # PRD 2026-05-06b §2: la barra y el badge se pintan con
            # ``execution_confidence`` (qué tan robusta es la estrategia
            # runtime), no con ``capture_score`` (qué tan limpio fue el
            # clic). Eso evita pintar como "Débil 0%" un open_new_tab
            # que el executor resolverá con Ctrl+T.
            try:
                _exec = compute_execution_confidence(c_step, mission=self.mission)
            except Exception:
                _exec = None
            if _exec is not None:
                score = _exec.percent
                if _exec.level == ExecLevel.GREEN:
                    robust += 1
                elif _exec.level == ExecLevel.RED and _exec.is_blocking:
                    review += 1
            else:
                score = int(max(0, min(100, round(_sq.capture_score_pct))))
                if _sq.level == "green":
                    robust += 1
                elif _sq.level == "red":
                    review += 1
            # Detectar pasos que solo tienen coords absolutas (frágiles).
            tc_has = c_step.target_context
            has_strong_signal = bool(
                (tc_has.web_data or {}).get("locators")
                or (tc_has.uia_data or {}).get("automation_id")
                or (tc_has.uia_data or {}).get("name")
                or tc_has.anchor_bbox
            )
            if (not has_strong_signal) and tc_has.fallback_coords:
                coord_only += 1

            gh = step_to_grp.get(c_step.id)
            if gh is not None and getattr(gh, "id", None) != last_group_id[0]:
                tip = " · ".join(gh.warnings) if getattr(gh, "warnings", None) else ""
                hdr_txt = f"📂 {gh.group_title}"
                if getattr(gh, "group_summary", ""):
                    hdr_txt += f" — {gh.group_summary}"
                hdr = QLabel(hdr_txt)
                if tip:
                    hdr.setToolTip(tip)
                hdr.setStyleSheet(
                    "color: #81ecec; font-size: 10px; font-weight: 700;"
                    " padding: 6px 0 2px 4px; border: none; background: transparent;"
                )
                self.cards_layout.addWidget(hdr)
                last_group_id[0] = gh.id

            card = QFrame()
            card.setObjectName(f"stepCard_{i}")
            border_color = _confidence_color(score)
            # Borde lateral acentúa la confianza visual del paso.
            card.setStyleSheet(f"""
                QFrame#stepCard_{i} {{
                    background: rgba(255,255,255,4);
                    border: 1px solid rgba(255,255,255,18);
                    border-left: 3px solid {border_color};
                    border-radius: 6px;
                }}
            """)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(7, 3, 7, 3)
            card_layout.setSpacing(2)

            # ── Row 1: [#] [Description] [icon] [✨] [🎯] [🩹] [🧪] [✕] ──
            # Botones compactos (16x16) para que SIEMPRE se vean todos sin
            # importar el ancho del diálogo: la descripción cede espacio a
            # los iconos, no al revés.
            row1 = QHBoxLayout()
            row1.setSpacing(2)

            num_lbl = QLabel(f"{i+1}")
            num_lbl.setFixedWidth(16)
            num_lbl.setStyleSheet("color: #a29bfe; font-weight: bold; font-size: 10px; border: none; background: transparent;")
            num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row1.addWidget(num_lbl)

            desc_edit = QLineEdit(desc)
            desc_edit.setStyleSheet("color: white; font-size: 11px; background: rgba(0,0,0,0.2); border: 1px solid rgba(255,255,255,15); border-radius: 3px; padding: 2px 6px;")
            desc_edit.setMinimumHeight(22)
            # 0 minimum: cede TODO el espacio que sea necesario para que los
            # iconos siempre se vean. La descripción se trunca elegantemente.
            desc_edit.setMinimumWidth(0)
            desc_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            desc_edit.textChanged.connect(lambda text, idx=i: self._update_description(idx, text))
            row1.addWidget(desc_edit, 1)

            icon = STRAT_ICONS.get(action_strat, "⚙️")
            badge = QLabel(icon)
            badge.setFixedWidth(16)
            badge.setStyleSheet("font-size: 11px; border: none; background: transparent;")
            badge.setToolTip(action_strat)
            row1.addWidget(badge)

            BTN = 16  # tamaño uniforme de mini-acciones

            vibe_btn = QPushButton("✨")
            vibe_btn.setFixedSize(BTN, BTN)
            vibe_btn.setToolTip("Editar con IA (Vibe)")
            vibe_btn.setStyleSheet(MINI_BTN.format(
                bg="rgba(162,155,254,0.15)", fg="#a29bfe",
                border="rgba(162,155,254,0.2)", hover="rgba(162,155,254,0.35)"))
            vibe_btn.clicked.connect(lambda checked, idx=i: self.vibe_edit_step(idx))
            row1.addWidget(vibe_btn)

            rec_btn = QPushButton("🎯")
            rec_btn.setFixedSize(BTN, BTN)
            rec_btn.setToolTip("Regrabar paso")
            rec_btn.setStyleSheet(MINI_BTN.format(
                bg="rgba(255,165,0,0.15)", fg="#ffa500",
                border="rgba(255,165,0,0.2)", hover="rgba(255,165,0,0.35)"))
            rec_btn.clicked.connect(lambda checked, idx=i: self.rerecord_step(idx))
            row1.addWidget(rec_btn)

            pick_btn = QPushButton("🩹")
            pick_btn.setFixedSize(BTN, BTN)
            pick_btn.setToolTip("Elegir elemento correcto (1 click)")
            pick_btn.setStyleSheet(MINI_BTN.format(
                bg="rgba(255,200,80,0.15)", fg="#ffd166",
                border="rgba(255,200,80,0.2)", hover="rgba(255,200,80,0.35)"))
            pick_btn.clicked.connect(lambda checked, idx=i: self.pick_correct_element(idx))
            row1.addWidget(pick_btn)
            
            test_btn = QPushButton("🧪")
            test_btn.setFixedSize(BTN, BTN)
            test_btn.setToolTip("Probar paso")
            test_btn.setStyleSheet(MINI_BTN.format(
                bg="rgba(0,206,201,0.15)", fg="#00cec9",
                border="rgba(0,206,201,0.2)", hover="rgba(0,206,201,0.35)"))
            test_btn.clicked.connect(lambda checked, idx=i: self.test_step(idx))
            row1.addWidget(test_btn)

            del_btn = QPushButton("✕")
            del_btn.setFixedSize(BTN, BTN)
            del_btn.setStyleSheet(MINI_BTN.format(
                bg="rgba(255,0,0,0.1)", fg="#ff7675",
                border="rgba(255,0,0,0.15)", hover="rgba(255,0,0,0.3)"))
            del_btn.clicked.connect(lambda checked, idx=i, c=card: self.delete_step(idx, c))
            row1.addWidget(del_btn)
            
            card_layout.addLayout(row1)
            
            # ── Row 2: DSL técnico compacto ──
            dsl_parts = [action_strat]
            
            uia_name = (c_step.target_context.uia_data or {}).get("name", "")
            if uia_name:
                dsl_parts.append(f'"{uia_name[:20]}"')
            
            coords = c_step.target_context.fallback_coords
            if coords:
                dsl_parts.append(f"({coords.get('x','?')},{coords.get('y','?')})")
            
            if action_strat == "type_text":
                txt = c_step.action_payload.get("text", "")
                if txt:
                    dsl_parts.append(f'→ "{txt[:25]}{"…" if len(txt) > 25 else ""}"')
            elif action_strat == "send_hotkey":
                dsl_parts.append(f"→ {describe_hotkey_payload(c_step.action_payload)}")
            elif action_strat == "agent_prompt":
                p = c_step.action_payload.get("text", "")
                dsl_parts.append(f'→ "{p[:20]}{"…" if len(p) > 20 else ""}"')
            
            dsl_lbl = QLabel(" · ".join(dsl_parts))
            dsl_lbl.setStyleSheet("color: #636e72; font-size: 9px; font-family: 'Consolas', monospace; border: none; background: transparent; padding-left: 22px;")
            dsl_lbl.setVisible(bool(getattr(self, "expert_mode", False)))
            card_layout.addWidget(dsl_lbl)

            # Confianza + método (Phase 8 + PRD 2026-05-06b §2).
            # En vista normal mostramos:
            #   • execution_confidence + estrategia preferida.
            # En modo experto añadimos:
            #   • capture_confidence (legacy)
            #   • UIA / web / visual signals
            method_hint = self._infer_method_hint(c_step)
            ql = _sq.level
            if _exec is not None:
                exec_label = {
                    ExecLevel.GREEN: "Ejecutable",
                    ExecLevel.AMBER: "Advertencia",
                    ExecLevel.RED: "Bloqueado",
                }.get(_exec.level, "")
                strat_hint = (_exec.preferred_strategy or "").replace("_", " ")
                pref = f"{exec_label} · " if exec_label else ""
                conf_text = f"  {pref}{score}% · {strat_hint or method_hint}"
            else:
                lab_es = getattr(_sq, "human_label_es", "") or ""
                pref = f"{lab_es} · " if lab_es else ""
                conf_text = f"  {pref}{score}% · {method_hint}"
            conf_lbl = QLabel(conf_text)
            conf_lbl.setStyleSheet(
                f"color: {border_color}; font-size: 9px; font-weight: 700;"
                " border: none; background: transparent; padding-left: 22px;"
            )
            card_layout.addWidget(conf_lbl)

            if _exec is not None and getattr(self, "expert_mode", False):
                _capture = _capture_score_legacy(c_step)
                expert_lbl = QLabel(
                    f"  capture {_capture}% · "
                    f"signals {int(_sq.signals_score)}% · "
                    f"ghost {(c_step.action_payload or {}).get('ghost_status', 'n/a')}"
                )
                expert_lbl.setStyleSheet(
                    "color: #95a5a6; font-size: 9px;"
                    " border: none; background: transparent; padding-left: 22px;"
                )
                card_layout.addWidget(expert_lbl)

            try:
                from app.services.missions.execution_ui import last_run_badge_from_runtime_stats

                _rtb = (c_step.target_context.confidence or {}).get("runtime_stats") or {}
                _badge = last_run_badge_from_runtime_stats(
                    _rtb if isinstance(_rtb, dict) else {},
                )
                if _badge and not getattr(self, "expert_mode", False):
                    run_hint = QLabel(f"  {_badge}")
                    run_hint.setStyleSheet(
                        "color:#95a5a6;font-size:8px;padding-left:22px;"
                        "border:none;background:transparent;"
                    )
                    card_layout.addWidget(run_hint)
            except Exception:
                pass

            accepted_risk = bool(
                (c_step.target_context.confidence or {}).get("user_accepted_weak")
            )
            # PRD 2026-05-06b §2: NO mostramos "Este paso puede fallar"
            # cuando la execution_confidence es GREEN — aunque el
            # capture_score esté en rojo, el executor tiene una
            # estrategia robusta y mostrar la alarma confunde al usuario.
            exec_is_strong = (_exec is not None and _exec.level == ExecLevel.GREEN
                              and not _exec.is_blocking)
            if ql == "red" and accepted_risk and not exec_is_strong:
                rr_lbl = QLabel(
                    "⚠ Marcado «riesgo aceptado»: no implica alta confianza; "
                    "revisa si automatizas en producción."
                )
                rr_lbl.setStyleSheet(
                    "color: #fdcb6e; font-size: 9px; font-weight: 600;"
                    " padding-left: 20px; border: none; background: transparent;"
                )
                card_layout.addWidget(rr_lbl)
            elif ql == "red" and not exec_is_strong:
                warn_lbl = QLabel(
                    "Este paso puede fallar. ¿Quieres reforzarlo?"
                )
                warn_lbl.setStyleSheet(
                    "color: #ff7675; font-size: 10px; font-weight: 600;"
                    " padding-left: 20px; border: none; background: transparent;"
                )
                card_layout.addWidget(warn_lbl)
                act_row = QHBoxLayout()
                act_row.setSpacing(4)
                for label, fn in (
                    ("Regrabar", lambda idx=i: self.rerecord_step(idx)),
                    ("Ref. imagen (1 clic)", lambda idx=i: self.pick_correct_element(idx)),
                    ("Validación", lambda idx=i: self.add_step_validation_quick(idx)),
                    ("Aceptar riesgo", lambda idx=i: self.accept_weak_step_risk(idx)),
                ):
                    b = QPushButton(label)
                    b.setStyleSheet(
                        "color: #dfe6e9; font-size: 9px; padding: 3px 8px;"
                        " background: rgba(255,118,117,0.15);"
                        " border: 1px solid rgba(255,118,117,0.35); border-radius: 4px;"
                    )
                    b.clicked.connect(fn)
                    act_row.addWidget(b)
                card_layout.addLayout(act_row)

            # CTA cuando el PASO SEMÁNTICO porta ``profile_*`` pero la intención
            # textual sigue sub-especificada (sin ramas tipo ``select_profile``.
            try:
                _semantic_step = find_semantic_step_for_compiled(
                    self.mission, c_step.id,
                )
                if _semantic_step is not None \
                        and profile_carrier_needs_confirmation(
                            _semantic_step, self.mission,
                        ):
                    prof_row = QHBoxLayout()
                    prof_row.setSpacing(6)
                    warn_prof = QLabel("⚠ Confirmar perfil")
                    warn_prof.setStyleSheet(
                        "color: #ffa500; font-size: 11px; font-weight: 700;"
                        " border: none; background: transparent;"
                        " padding-left: 20px;"
                    )
                    prof_row.addWidget(warn_prof, 1)

                    prof_btn = QPushButton("👤 Confirmar perfil")
                    prof_btn.setObjectName(f"confirmProfileBtn_{i}")
                    prof_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    prof_btn.setToolTip(
                        "Confirmar qué perfil debe usar Nevlan. "
                        "Se guardará como alias para próximas misiones."
                    )
                    prof_btn.setStyleSheet(
                        "color: white; font-size: 10px; font-weight: 700;"
                        " padding: 6px 14px;"
                        " background: rgba(9,132,227,0.35);"
                        " border: 1px solid rgba(9,132,227,0.6);"
                        " border-radius: 6px;"
                    )
                    prof_btn.clicked.connect(
                        lambda _checked=False, idx=i:
                        self._open_profile_confirm_dialog(idx)
                    )
                    prof_row.addWidget(prof_btn)
                    card_layout.addLayout(prof_row)
            except Exception as _e:
                log.debug(f"profile confirm CTA skipped: {_e}")

            if self.expert_mode:
                cr = ""
                cf = (c_step.target_context.confidence or {}) or {}
                rt = cf.get("runtime_stats") or {}
                rex = ""
                if isinstance(rt, dict) and rt:
                    lx = rt.get("last_resolver") or ""
                    fk = rt.get("last_fallback") or rt.get("last_premium_fallback")
                    elapsed = rt.get("last_elapsed_ms")
                    rex_bits = []
                    if lx:
                        rex_bits.append(f"resolver={lx}")
                    if fk:
                        rex_bits.append(f"fallback={fk}")
                    if elapsed is not None:
                        try:
                            rex_bits.append(f"{float(elapsed):.0f}ms")
                        except (TypeError, ValueError):
                            pass
                    lp = rt.get("last_premium_meta")
                    if isinstance(lp, dict) and lp:
                        rex_bits.append("premium=" + str(lp)[:200])
                    if rex_bits:
                        rex = " · ".join(rex_bits)
                reasons = cf.get("quality_reasons") or cf.get("capture_reasons") or []
                if reasons:
                    cr = " · ".join(str(x) for x in reasons[:12])
                if rex:
                    cr = (cr + " | " if cr else "") + rex
                try:
                    from app.services.missions.execution_ui import friendly_resolver_summary

                    if isinstance(rt, dict) and rt.get("last_resolver"):
                        fn_n, _fn_x = friendly_resolver_summary(
                            str(rt.get("last_resolver")),
                            None,
                        )
                        if fn_n:
                            cr = (cr + " | " if cr else "") + f"ejec:{fn_n}"
                except Exception:
                    pass
                if cr:
                    exp_l = QLabel(f"  expert: {cr}")
                    exp_l.setWordWrap(True)
                    exp_l.setStyleSheet(
                        "color:#636e72;font-size:8px;padding-left:20px;background:transparent;border:none;"
                    )
                    card_layout.addWidget(exp_l)

            # ── Row 3: Thumbnail + label/contexto detectado (FASE 10) ──
            # Solo si tenemos snapshot o algún descriptor humano útil.
            preview_row = self._build_preview_row(c_step)
            if preview_row is not None:
                card_layout.addLayout(preview_row)

            self.cards_layout.addWidget(card)

        if hasattr(self, "summary_lbl"):
            total = len(steps)
            parts = [
                f"📊 {total} pasos",
                f"✅ {robust} robustos",
                f"🟡 {review} necesitan confirmación",
            ]
            if coord_only > 0:
                parts.append(f"⚠ {coord_only} dependen de coords")
            # Reliability score persistido por MissionOptimizer.
            rel = (self.mission.recovery_rules or {}).get(
                "mission_reliability_score"
            )
            if rel is not None:
                parts.append(f"🛡 confiabilidad {int(rel)}/100")
            ag = getattr(self.mission, "action_groups", None) or []
            if ag:
                parts.append(f"📂 {len(ag)} grupos")
            if review > 0:
                parts.append(
                    f"{review} paso(s) podrían fallar — revisa o reforzá antes de programar"
                )
            self.summary_lbl.setText(" · ".join(parts))

    def _on_toggle_expert_mode(self):
        self.expert_mode = bool(self.chk_expert.isChecked())
        if getattr(self, "_used_semantic_primary_cards", False):
            try:
                self._rebuild_cards_ui()
            except Exception as e:
                log.debug(f"[mission_review] expert rebuild: {e}")
        self.chk_expert.setText(
            "Ocultar detalles técnicos" if self.expert_mode else "Ver detalles técnicos"
        )
        try:
            self._refresh_semantic_shadow_panel()
        except Exception as e:
            log.debug(f"[mission_review] semantic shadow toggle: {e}")
        self.refresh_cards()

    def _infer_method_hint(self, c_step: CompiledStep) -> str:
        """Texto corto sobre cómo se identificará el target en replay."""
        tc = c_step.target_context
        if tc.web_data and (tc.web_data.get("locators")):
            return "web · Playwright locator"
        uia = tc.uia_data or {}
        if isinstance(uia, dict) and uia.get("automation_id"):
            return "desktop · UIA AutomationId"
        if isinstance(uia, dict) and uia.get("name"):
            return "desktop · UIA Name"
        if tc.image_ref_mid or tc.anchor_bbox:
            return "visual · ancla/imagen"
        if tc.fallback_coords_relative_to_window:
            return "coords relativas a ventana"
        if tc.fallback_coords:
            return "coords absolutas (último recurso)"
        return "sin target"

    def _build_preview_row(self, c_step: CompiledStep):
        """Devuelve un QHBoxLayout con thumbnail (si existe) + texto
        humano (label, near_text, locator favorito) o None si no hay
        nada interesante que mostrar.
        """
        tc = c_step.target_context
        thumb_path = tc.image_ref_mid or tc.image_ref

        # Texto a mostrar: label humano + contexto cercano
        sem = tc.semantic or {}
        web = tc.web_data or {}
        uia = tc.uia_data or {}
        label = (
            sem.get("human_label")
            or web.get("accessible_name") or web.get("label")
            or web.get("placeholder") or web.get("test_id")
            or uia.get("name") or uia.get("automation_id")
            or ""
        )
        near = (sem.get("near_text") or web.get("nearby_text") or "")[:80]
        intent = sem.get("intent") or ""

        # Locator favorito (cuál usaría primero el resolver)
        loc_hint = ""
        for spec in (web.get("locators") or [])[:1]:
            kind = spec.get("kind", "")
            val = spec.get("value", "") or spec.get("name", "")
            if kind and val:
                loc_hint = f"{kind}={val[:40]}"

        # Si no hay ni thumbnail ni label útil, no añadir la fila.
        if not thumb_path and not (label or near or loc_hint):
            return None

        row = QHBoxLayout()
        row.setContentsMargins(22, 0, 0, 0)
        row.setSpacing(6)

        # Validador estricto: existe + tamaño > 8 bytes + firma PNG.
        # Sin esto, un PNG corrupto puede crashear Qt en paint event.
        if _is_safe_png_path(thumb_path):
            try:
                pix = QPixmap(thumb_path)
                if not pix.isNull():
                    thumb = QLabel()
                    thumb.setFixedSize(40, 40)
                    thumb.setPixmap(pix.scaled(
                        40, 40,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    ))
                    thumb.setStyleSheet(
                        "border: 1px solid rgba(255,255,255,40);"
                        " border-radius: 3px; background: rgba(0,0,0,80);"
                    )
                    thumb.setToolTip(f"Vista previa del target capturado\n{thumb_path}")
                    row.addWidget(thumb)
            except Exception as _e:
                log.debug(f"_build_preview_row pixmap fail: {_e}")

        info_box = QVBoxLayout()
        info_box.setSpacing(0)
        info_box.setContentsMargins(0, 0, 0, 0)
        if label:
            l1 = QLabel(f"🎯 {label[:60]}")
            l1.setStyleSheet(
                "color: #dfe6e9; font-size: 10px; font-weight: 600;"
                " border: none; background: transparent;"
            )
            l1.setToolTip(label)
            info_box.addWidget(l1)
        if loc_hint:
            l2 = QLabel(f"⚡ {loc_hint}")
            l2.setStyleSheet(
                "color: #74b9ff; font-size: 9px;"
                " border: none; background: transparent;"
                " font-family: 'Consolas', monospace;"
            )
            l2.setToolTip("Locator preferido del resolver")
            info_box.addWidget(l2)
        if near and near.strip() and near.strip() != (label or "").strip():
            l3 = QLabel(f"💬 {near[:60]}")
            l3.setStyleSheet(
                "color: #95a5a6; font-size: 9px; font-style: italic;"
                " border: none; background: transparent;"
            )
            l3.setToolTip(near)
            info_box.addWidget(l3)
        if intent and not (label or loc_hint):
            l4 = QLabel(f"📌 {intent}")
            l4.setStyleSheet(
                "color: #95a5a6; font-size: 9px;"
                " border: none; background: transparent;"
            )
            info_box.addWidget(l4)
        row.addLayout(info_box, 1)
        return row
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # VIBE EDIT (Inteligente con clasificador)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    def vibe_edit_step(self, index):
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        from app.services.missions.vibe_service import apply_vibe_edit

        c_step = self.mission.compiled_execution_graph[index]
        i_step = self.mission.interpreted_steps[index]
        current_action = getattr(c_step.action_strategy, "value", str(c_step.action_strategy))

        text, ok = QInputDialog.getText(
            None,
            f"✨ Vibe Edit — Paso {index + 1}",
            f"Paso actual: {i_step.description}\n"
            f"Estrategia: {current_action}\n\n"
            f"¿Qué cambio quieres hacer?"
        )
        if not ok or not text:
            return

        def _confirm_agent() -> bool:
            res = QMessageBox.warning(
                None, "⚠ Conversión a Agent Prompt",
                f"El paso actual es determinista ({current_action}). Convertirlo a "
                f"agent_prompt lo hará menos confiable. ¿Continuar?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return res == QMessageBox.StandardButton.Yes

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = apply_vibe_edit(
                self.mission, index, text,
                on_confirm_agent=_confirm_agent,
            )
        finally:
            QApplication.restoreOverrideCursor()

        if result.get("ok"):
            self._auto_save()
            self.refresh_cards()
        elif result.get("error"):
            log.info(f"vibe_service no aplicó: {result['error']}")

    def accept_weak_step_risk(self, index: int):
        """Usuario acepta ejecutar un paso débil; queda auditado, sin borrar señales.

        PRD 2026-05-08c §D: el popup resultante usa ``NevlanDialog`` (no
        QMessageBox nativo) y tiene contrato de "nunca vacío":
          * Si la operación funcionó: dialog success con texto explícito.
          * Si falla durante el guardado: dialog danger con título +
            mensaje + detalles técnicos colapsables (no error invisible).
        """
        from app.interfaces.desktop.nevlan_dialog import (
            show_error, show_warning,
        )
        if not (0 <= index < len(self.mission.compiled_execution_graph)):
            show_error(
                self,
                title="No pude marcar el paso",
                message=(
                    "El índice del paso quedó fuera del rango actual. "
                    "Cierra y reabre Mission Review para refrescar."
                ),
                details=f"index={index} compiled_graph_len="
                        f"{len(self.mission.compiled_execution_graph)}",
            )
            return
        try:
            cs = self.mission.compiled_execution_graph[index]
            rr = dict(self.mission.recovery_rules or {})
            acc = dict(rr.get("accepted_weak_steps") or {})
            acc[cs.id] = True
            rr["accepted_weak_steps"] = acc
            self.mission.recovery_rules = rr
            cf = dict(cs.target_context.confidence or {})
            cf["user_accepted_weak"] = True
            cs.target_context.confidence = cf
            self._auto_save()
            self.refresh_cards()
        except Exception as e:
            log.exception(f"[review] accept_weak_step_risk falló: {e}")
            show_error(
                self,
                title="No pude registrar la aceptación de riesgo",
                message=(
                    "Ocurrió un problema al guardar tu confirmación. "
                    "Intenta de nuevo o reabre Mission Review."
                ),
                details="La misión NO quedó marcada como aceptada.",
                expert_details=f"{type(e).__name__}: {e}",
            )
            return
        show_warning(
            self,
            title="Riesgo aceptado",
            message=(
                "Queda registrado. El paso sigue visible; en modo "
                "experto verás la marca."
            ),
            primary="Entendido",
            cancel="",
        )

    def add_step_validation_quick(self, index: int):
        """Diálogo mínimo para añadir validación post-acción."""
        if not (0 <= index < len(self.mission.compiled_execution_graph)):
            return
        cs = self.mission.compiled_execution_graph[index]
        opts = [
            ("Verificar que aparece texto esperado", ValidationStrategy.REQUIRE_TEXT_MATCH),
            ("Verificar que aparece / coincide una imagen", ValidationStrategy.REQUIRE_IMAGE_MATCH),
            ("Verificar que el elemento sigue ahí después de la acción", ValidationStrategy.REQUIRE_ELEMENT_EXISTS),
            ("Verificar que el campo tiene un valor esperado", ValidationStrategy.REQUIRE_FIELD_VALUE),
            ("Verificar que un elemento/texto ya no aparece", ValidationStrategy.REQUIRE_DISAPPEAR),
        ]
        labels = [o[0] for o in opts]
        choice, ok = QInputDialog.getItem(
            self, "Validación", "Qué comprobar:", labels, 0, False,
        )
        if not ok:
            return
        strat = ValidationStrategy.NONE
        for lab, st in opts:
            if lab == choice:
                strat = st
                break
        if strat == ValidationStrategy.NONE:
            return
        cs.validation_strategy = strat
        self._auto_save()
        self.refresh_cards()
        QMessageBox.information(self, "Validación", "Estrategia de validación añadida al paso.")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ELEGIR ELEMENTO CORRECTO (1 click) — autocuración Phase 7/8
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def pick_correct_element(self, index: int):
        """Activa OneClickCapture: el siguiente clic del usuario sobre la
        app destino capturará un nuevo TargetBundle y lo añadirá como
        descriptor alternativo del paso (no reemplaza, acumula).

        El usuario ve un toast/overlay minimal pidiendo el clic; la
        ventana de Nevlan se minimiza para no estorbar. Coherente con
        automation_center: nada de robo de foco.
        """
        try:
            from app.services.missions.repair import (
                OneClickCapture, apply_repair_to_step,
            )
        except Exception as e:
            QMessageBox.warning(self, "Reparación", f"No disponible: {e}")
            return

        if not (0 <= index < len(self.mission.compiled_execution_graph)):
            return
        c_step = self.mission.compiled_execution_graph[index]

        # Toast informativo no intrusivo
        try:
            from app.interfaces.desktop.rerecord_overlay import RerecordOverlay
            i_step = self.mission.interpreted_steps[index] if index < len(self.mission.interpreted_steps) else None
            desc = i_step.description if i_step else "Paso"
            self._pick_overlay = RerecordOverlay(index, f"🩹 Click en el elemento correcto: {desc}", "pick")
            self._pick_overlay.show()
            self._pick_overlay.raise_()
        except Exception:
            self._pick_overlay = None

        self.showMinimized()

        def _on_done(bundle):
            try:
                if self._pick_overlay is not None:
                    self._pick_overlay.close()
            except Exception:
                pass
            self.showNormal()
            self.raise_()
            if not bundle:
                QMessageBox.information(
                    self, "Reparación cancelada",
                    "No se capturó ningún elemento."
                )
                return
            apply_repair_to_step(c_step, bundle, replace_primary=False)
            rf_msg = ""
            try:
                from app.services.missions.image_asset_repair import (
                    reinforce_step_from_capture_bundle,
                )

                rfr = reinforce_step_from_capture_bundle(
                    c_step, self.mission.id, bundle,
                )
                rf_msg = getattr(rfr, "message", "") or ""
            except Exception as e:
                log.debug(f"image reinforce: {e}")

            try:
                from app.services.missions.compiler import MissionCompiler
                if index < len(self.mission.interpreted_steps):
                    MissionCompiler._enrich_semantic_and_score(
                        c_step,
                        self.mission.interpreted_steps[index],
                    )
                MissionCompiler.rebuild_action_groups(self.mission)
                try:
                    from app.services.missions import mission_refresh

                    mission_refresh.refresh_mission_after_step_change(
                        self.mission,
                        reason="pick_correct_element",
                        affected_step_ids=[getattr(c_step, "id", "")],
                    )
                except Exception:
                    pass
            except Exception as e:
                log.debug(f"enrich after pick: {e}")
            self._auto_save()
            self.refresh_cards()
            body = (
                "Se añadió un descriptor alternativo. Si vuelve a fallar, "
                "se intentará automáticamente con la nueva pista."
            )
            if rf_msg:
                body = rf_msg + "\n\n" + body
            QMessageBox.information(self, "Elemento aprendido", body)

        # OneClickCapture llama on_done en el hilo del listener; reenviar a Qt
        def _safe_done(bundle):
            QTimer.singleShot(0, lambda b=bundle: _on_done(b))

        self._one_click = OneClickCapture(on_done=_safe_done)
        self._one_click.start()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # REGRABAR PASO
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    def rerecord_step(self, index):
        """Inicia regrabación orquestada de un paso (PRD 2026-05-08c §B-§D).

        Flujo:

            1. Crea ``RerecordOrchestrator`` con la misión actual.
            2. Muestra ``NevlanDialog`` "Preparando regrabación".
            3. Si el usuario confirma:
                a. Abre el overlay en fase ``replay``.
                b. Lanza ``proceed_to_capture()`` en un ``QThread``.
                c. Cuando el replay termina con éxito, transiciona
                   el overlay a fase ``capture`` y arranca el
                   ``StepFragmentRecorder``.
                d. Si el replay falla, muestra ``NevlanDialog`` con
                   3 opciones (reintentar / manual / cancelar).
            4. Save/Retry/Cancel pasan por el orchestrator.
        """
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay

        plan_blob = self.mission.semantic_execution_plan or {}
        plan_steps = list(plan_blob.get("steps") or [])

        if not plan_steps:
            show_error(
                self,
                title="Esta misión aún no se puede regrabar paso por paso",
                message=(
                    "Para regrabar un paso individual con replay previo, "
                    "Nevlan necesita el plan semántico de la misión "
                    "(``semantic_execution_plan``). Esta misión todavía "
                    "no lo tiene. Ejecuta el flujo de revisión completo "
                    "antes de regrabar pasos sueltos."
                ),
            )
            return

        if index < 0 or index >= len(plan_steps):
            show_error(
                self,
                title="No pude identificar el paso",
                message=(
                    f"El índice {index} está fuera del plan "
                    f"(0..{len(plan_steps) - 1})."
                ),
            )
            return

        target_step = plan_steps[index] or {}
        target_id = str(target_step.get("id") or f"idx_{index}")
        human_label = (
            target_step.get("human_label")
            or target_step.get("type")
            or f"Paso {index + 1}"
        )
        target_type = str(target_step.get("type") or "")
        previous_count = index

        # 1. Crear orchestrator + sesión.
        try:
            orch = RerecordOrchestrator()
            orch.start(self.mission, target_step_index=index)
        except RerecordSessionError as e:
            show_error(
                self,
                title="No pude iniciar la regrabación",
                message=str(e),
            )
            return
        self._rerecord_orchestrator = orch
        self._rerecord_step_idx = index

        # 2. Diálogo de preparación con NevlanDialog.
        # PRD 2026-05-09 §F — en modo cola "Reparar pasos débiles → Cola"
        # ya consintió el usuario al elegir "⚡ Regrabar los N en cola";
        # repetir un modal por cada paso de la cola era una de las
        # razones por las que parecía pasmarse. Saltamos el modal y
        # mostramos el progreso "Paso N de M" directamente en el overlay.
        in_queue = bool(self._in_repair_queue)
        if not in_queue:
            prep_msg = (
                f"Voy a ejecutar los {previous_count} paso(s) anterior(es) "
                f"automáticamente y me detendré justo antes de "
                f"\"{human_label}\". Después podrás repetir solo ese paso."
                if previous_count
                else (
                    "No hay pasos anteriores: te llevaré directo a la "
                    f"captura de \"{human_label}\"."
                )
            )
            expert_details = (
                f"target_step_id={target_id}\n"
                f"target_step_index={index}\n"
                f"target_type={target_type}\n"
                f"previous_steps_count={previous_count}\n"
                f"rerecord_session_id={orch.session.rerecord_session_id}"
            )
            result = show_confirmation(
                self,
                title="Preparando regrabación",
                message=prep_msg,
                primary="Continuar",
                cancel="Cancelar",
                details=expert_details if previous_count else "",
            )
            if result != RESULT_PRIMARY:
                try:
                    orch.cancel()
                except Exception:
                    pass
                orch.release()
                self._rerecord_orchestrator = None
                self._rerecord_step_idx = -1
                return

        # 3. Crear overlay en fase replay.
        self._rerecord_overlay = RerecordOverlay(
            index,
            human_label,
            target_type or "rerecord",
            phase=PHASE_REPLAY,
            total_previous_steps=previous_count,
        )
        # PRD 2026-05-09 §F — si venimos de la cola, mostramos "Paso N de M"
        # en el título del overlay para que el usuario sepa por qué se le
        # presenta una regrabación detrás de otra.
        if in_queue and self._repair_queue_total > 0:
            try:
                # ``_repair_queue`` ya tuvo el .pop(0); el actual es el
                # ``total - len(queue restante)``.
                remaining = len(getattr(self, "_repair_queue", []) or [])
                done_index = max(1, self._repair_queue_total - remaining)
                self._rerecord_overlay.set_queue_progress(
                    done_index, self._repair_queue_total,
                )
            except Exception as e:
                log.debug(f"[rerecord] set_queue_progress: {e}")
        self._rerecord_overlay.start_replay_requested.connect(
            self._on_rerecord_start_replay
        )
        self._rerecord_overlay.save_requested.connect(
            self._on_rerecord_save
        )
        self._rerecord_overlay.retry_requested.connect(
            self._on_rerecord_retry
        )
        self._rerecord_overlay.cancel_requested.connect(
            self._on_rerecord_cancel
        )
        self._rerecord_overlay.rollback_requested.connect(
            self._on_rerecord_undo_hotkey
        )

        # Move review dialog out of the way (don't hide — causes Qt issues)
        self.showMinimized()
        self._rerecord_overlay.show()
        self._rerecord_overlay.raise_()
        # NO activateWindow(): robaría foco al destino.

        # Caso paso 1: no hay nada que reproducir; saltamos directo a captura.
        if previous_count == 0:
            try:
                orch.proceed_to_capture()
            except Exception as e:
                log.exception(f"[rerecord] proceed (idx=0) error: {e}")
            self._enter_capture_phase()
            return

        # PRD 2026-05-09 §F — en modo cola, arrancamos el replay
        # automáticamente: el usuario ya consintió toda la cola y
        # tener que pulsar "Empezar" por cada paso es ruido.
        if in_queue:
            log.info(
                f"[rerecord] cola: auto-iniciando replay; total_prev={previous_count}"
            )
            QTimer.singleShot(120, self._on_rerecord_start_replay)
            return

        # Esperamos a que el usuario presione "Empezar" en el overlay.
        log.info(
            f"[rerecord] overlay listo en fase replay; total_prev={previous_count}"
        )

    def _on_rerecord_start_replay(self) -> None:
        """Lanza el replay en un QThread para no congelar la UI."""
        orch = self._rerecord_orchestrator
        if orch is None:
            return

        # Tear down de cualquier worker previo.
        self._stop_replay_worker()

        thread = QThread(self)
        worker = _ReplayWorker(orch)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_rerecord_replay_progress)
        worker.finished.connect(self._on_rerecord_replay_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._rerecord_thread = thread
        self._rerecord_worker = worker

        # Heartbeat: si pasan 30s sin progreso, alertamos al usuario
        # con NevlanDialog y dejamos que decida (cancelar o esperar).
        self._rerecord_last_progress_ms = 0
        if self._rerecord_heartbeat_timer is None:
            timer = QTimer(self)
            timer.setInterval(5000)  # chequeamos cada 5s
            timer.timeout.connect(self._check_replay_heartbeat)
            self._rerecord_heartbeat_timer = timer
        self._rerecord_heartbeat_timer.start()

        thread.start()

    def _on_rerecord_replay_progress(
        self, idx: int, total: int, label: str
    ) -> None:
        self._rerecord_last_progress_ms = 0
        if self._rerecord_overlay is not None:
            self._rerecord_overlay.update_replay_progress(idx, total, label)

    def _check_replay_heartbeat(self) -> None:
        # Solo aplicamos heartbeat mientras estamos en fase replay.
        orch = self._rerecord_orchestrator
        if orch is None or orch.session is None:
            return
        if orch.session.state != STATE_REPLAYING:
            return
        self._rerecord_last_progress_ms += 5000
        if self._rerecord_last_progress_ms < 30_000:
            return
        # 30 s sin progreso → preguntamos al usuario.
        self._rerecord_last_progress_ms = 0
        choice = show_warning(
            self,
            title="El replay sigue trabajando",
            message=(
                "Llevo 30 segundos esperando que termine un paso del "
                "replay previo. ¿Quieres seguir esperando o cancelar?"
            ),
            primary="Sigue esperando",
            cancel="Cancelar regrabación",
        )
        if choice == RESULT_CANCEL:
            try:
                orch.request_cancel()
            except Exception:
                pass
            self._on_rerecord_cancel()

    def _on_rerecord_replay_finished(self, ok: bool, error_message: str) -> None:
        if self._rerecord_heartbeat_timer is not None:
            self._rerecord_heartbeat_timer.stop()
        orch = self._rerecord_orchestrator
        if orch is None or orch.session is None:
            return

        state = orch.session.state
        if ok and state == STATE_WAITING_FOR_USER:
            self._enter_capture_phase()
            return

        if state == STATE_FAILED:
            self._handle_replay_failure(error_message or REPLAY_FAILED_MESSAGE)
            return

        # Cancelado durante replay → cleanup directo.
        self._cleanup_rerecord()
        self.refresh_cards()

    def _handle_replay_failure(self, error_message: str) -> None:
        if self._rerecord_overlay is not None:
            self._rerecord_overlay.show_replay_failed(error_message)

        details = ""
        orch = self._rerecord_orchestrator
        if orch is not None and orch.session is not None:
            details = (
                f"failed_step_id={orch.session.error_failed_step_id}\n"
                f"previous_steps_replayed="
                f"{orch.session.previous_steps_replayed}"
            )

        choice = show_warning(
            self,
            title="No pude llegar automáticamente al paso",
            message=(
                error_message
                or "Nevlan no pudo ejecutar los pasos anteriores para "
                   "llegar al punto exacto de regrabación."
            ),
            primary="Reintentar replay",
            secondary="Regrabar desde aquí manualmente",
            cancel="Cancelar",
            expert_details=details,
        )
        if choice == RESULT_PRIMARY:
            self._on_rerecord_start_replay()
            return
        if choice == RESULT_SECONDARY:
            try:
                if self._rerecord_orchestrator is not None:
                    self._rerecord_orchestrator.skip_replay_and_capture_manually()
            except Exception as e:
                log.warning(f"[rerecord] skip_replay falló: {e}")
            self._enter_capture_phase()
            return
        # Cancelar
        self._on_rerecord_cancel()

    def _enter_capture_phase(self) -> None:
        """Inicia la fase de captura: arranca el StepFragmentRecorder
        y habilita Save/Retry/Cancel."""
        from app.services.missions.recorder import StepFragmentRecorder

        if self._rerecord_overlay is not None:
            self._rerecord_overlay.enter_capture_phase()

        self._fragment_recorder = StepFragmentRecorder()
        try:
            self._fragment_recorder.start()
        except RuntimeError as e:
            log.warning(f"[rerecord] fragment_recorder.start: {e}")

        if not hasattr(self, "_poll_timer") or self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._poll_fragment_events)
        self._poll_timer.start(300)

    def _poll_fragment_events(self):
        """Actualiza el overlay con interpretación humana
        (no microhistoria de teclas)."""
        if not self._fragment_recorder or not self._rerecord_overlay:
            return
        events = self._fragment_recorder.get_events()
        expert = bool(getattr(self._rerecord_overlay, "expert_mode", False))
        human, expert_lines = build_rerecord_display(events, expert=expert)
        self._rerecord_overlay.set_human_display(human, expert_lines)

    def _on_rerecord_save(self):
        """Guardar el fragmento regrabado vía el orchestrator.

        PRD 2026-05-08d §C — el botón Guardar siempre está
        clickeable. Aquí enforced las pre-condiciones:

          1. Si todavía no hay captura activa (``_fragment_recorder``
             es ``None`` porque seguimos en fase replay o el overlay
             se abrió sin entrar a captura), mostramos un
             NevlanDialog con "No hay nada que guardar. Repite el
             paso antes de guardar." y NO mutamos la misión.
          2. Si la captura existe pero no hay eventos (el usuario
             no ejecutó ninguna acción), mostramos el mismo dialog
             y limpiamos el buffer para que pueda volver a intentar.
        """
        if self._rerecord_save_in_flight:
            return  # PRD §E: evitar doble guardado
        # Pre-condición #1: sin captura activa.
        if not self._fragment_recorder:
            show_warning(
                self,
                title="No hay nada que guardar",
                message=(
                    "Repite el paso antes de guardar. Espera a que "
                    "termine la fase de preparación y ejecuta la "
                    "acción que querías regrabar."
                ),
                primary="Entendido",
                cancel="",
            )
            return
        if self._poll_timer:
            self._poll_timer.stop()
        orch = self._rerecord_orchestrator
        if orch is None:
            self._cleanup_rerecord()
            return

        try:
            raw_events = self._fragment_recorder.stop()
        except RuntimeError as e:
            log.warning(f"[rerecord] stop fragment: {e}")
            raw_events = []

        if not raw_events:
            # Pre-condición #2: captura activa pero vacía.
            show_warning(
                self,
                title="No hay nada que guardar",
                message=(
                    "No detecté ninguna acción durante la regrabación. "
                    "Realiza la acción (clic, escribir, etc.) y vuelve "
                    "a presionar Guardar."
                ),
                primary="Volver a intentar",
                cancel="",
            )
            self._on_rerecord_retry()
            return

        evidence = [
            ev.model_dump(mode="json") if hasattr(ev, "model_dump") else dict(ev)
            for ev in raw_events
        ]
        ok = orch.capture_user_event(evidence)
        if not ok:
            show_warning(
                self,
                title="La captura no es válida",
                message=(
                    "Los eventos que capturé no compilaron a un paso "
                    "claro. Revisa que la acción sea visible y vuelve "
                    "a intentar."
                ),
                primary="Volver a intentar",
                cancel="",
            )
            self._on_rerecord_retry()
            return

        self._rerecord_save_in_flight = True
        pre_blob = self.mission.model_dump(mode="python")
        try:
            plan = orch.save()
        except Exception as e:
            self._rerecord_save_in_flight = False
            log.error(f"[rerecord] save fallido: {e}")
            show_error(
                self,
                title="No pude guardar la regrabación",
                message=(
                    "Encontré un error al aplicar la regrabación al "
                    "plan de la misión. La misión no fue modificada."
                ),
                expert_details=f"{type(e).__name__}: {e}",
            )
            self._cleanup_rerecord()
            self.refresh_cards()
            return
        finally:
            self._rerecord_save_in_flight = False

        self._rerecord_controller.offer_undo_anchor(pre_blob)

        # Persistimos en disco.
        try:
            self._auto_save()
        except Exception as e:  # pragma: no cover
            log.warning(f"[rerecord] auto_save tras save: {e}")

        log.info(
            f"[rerecord] save OK target_idx={self._rerecord_step_idx} "
            f"new_status={plan.intent_status}"
        )

        self.did_apply_rerecord_save = True

        # Mensaje de éxito.
        # PRD 2026-05-09 §F — en modo cola omitimos el dialog "Regrabación
        # guardada" entre pasos: el usuario verá el resumen final
        # "Reparación completa" cuando la cola se vacíe, y un toast en la
        # barra de estado por cada paso. Sin esto, la cola pedía clic
        # extra cada paso (lo que parecía "se pasma").
        if not self._in_repair_queue:
            try:
                visible = list(plan.not_ready_reasons or [])
            except Exception:
                visible = []
            success_summary = (
                f"Reemplacé el paso {self._rerecord_step_idx + 1} y recompilé "
                f"el plan. Estado: {plan.intent_status}."
            )
            if visible:
                success_summary += (
                    f"\n\nQuedan {len(visible)} aviso(s) por revisar."
                )
            show_warning(
                self,
                title="Regrabación guardada",
                message=success_summary,
                primary="Continuar",
                cancel="",
            )
        else:
            # Modo cola: marcamos progreso en el session UI y seguimos.
            sess_ui = getattr(self, "_weak_repair_ui_session", None)
            if isinstance(sess_ui, WeakStepRepairSession):
                try:
                    sess_ui.mark_success()
                except Exception:
                    pass

        self._cleanup_rerecord()
        self.refresh_cards()

    def _on_rerecord_undo_hotkey(self) -> None:
        """Ctrl+Z desde overlay: revierte última regrabación guardada."""
        if not self._rerecord_controller.rollback_latest_into(self.mission):
            show_warning(
                self,
                title="Nada que deshacer",
                message="No hay una regrabación previa para revertir.",
                primary="Entendido",
                cancel="",
            )
            return
        log.info("[rerecord] undo Ctrl+Z aplicado — estado anterior restaurado")
        try:
            self.refresh_cards()
        except Exception as e:
            log.warning(f"[rerecord] refresh tras undo: {e}")

    def _on_rerecord_retry(self):
        """Reiniciar la captura sin mutar la misión.

        PRD 2026-05-08d §C — descarta captura, limpia buffer,
        reinicia listener del mismo paso, mantiene sesión activa,
        NO toca la misión. El overlay muestra
        "Listo, repite este paso nuevamente." (texto seteado por
        :meth:`RerecordOverlay.reset_for_retry`).
        """
        if self._poll_timer:
            self._poll_timer.stop()

        if self._fragment_recorder and self._fragment_recorder.is_recording():
            try:
                self._fragment_recorder.stop()
            except Exception:
                pass

        orch = self._rerecord_orchestrator
        if orch is not None and orch.session is not None:
            try:
                orch.discard_capture()
            except RerecordSessionError:
                pass

        from app.services.missions.recorder import StepFragmentRecorder
        self._fragment_recorder = StepFragmentRecorder()
        try:
            self._fragment_recorder.start()
        except RuntimeError:
            pass

        if self._rerecord_overlay:
            self._rerecord_overlay.reset_for_retry()

        if self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._poll_fragment_events)
        self._poll_timer.start(300)

    def _on_rerecord_cancel(self):
        """Cancelar regrabación (con confirmación si hay captura sin guardar).

        PRD 2026-05-09 §E — Re-entrancy-safe: si el cleanup ya está
        en curso (típicamente porque venimos de ``overlay.close()``
        post-save), salimos sin hacer nada. El cleanup principal se
        encargará del resto.
        """
        if self._cleanup_in_progress:
            return

        orch = self._rerecord_orchestrator
        has_unsaved = (
            orch is not None
            and orch.session is not None
            and orch.session.state == STATE_CAPTURED
        )
        if has_unsaved:
            choice = show_confirmation(
                self,
                title="Cancelar regrabación",
                message=(
                    "Tienes una captura sin guardar. Si cancelas ahora, "
                    "perderás esa acción y la misión quedará exactamente "
                    "como estaba antes."
                ),
                primary="Sí, cancelar",
                cancel="No, seguir regrabando",
            )
            if choice != RESULT_PRIMARY:
                return

        # PRD 2026-05-09 §F — si el usuario cancela durante una cola
        # "Reparar pasos débiles", detenemos TODA la cola: no tiene
        # sentido que el siguiente paso aparezca solo porque cerramos
        # la ventana de regrabación del actual.
        if self._in_repair_queue:
            self._repair_queue = []
            self._in_repair_queue = False
            self._repair_queue_total = 0
            sess_ui = getattr(self, "_weak_repair_ui_session", None)
            if isinstance(sess_ui, WeakStepRepairSession):
                try:
                    sess_ui.cancel()
                except Exception:
                    pass

        # Detenemos cualquier captura activa.
        if self._poll_timer:
            self._poll_timer.stop()
        if self._fragment_recorder and self._fragment_recorder.is_recording():
            try:
                self._fragment_recorder.stop()
            except Exception:
                pass

        # Pedimos cancel cooperativo si hay replay corriendo.
        if orch is not None:
            try:
                orch.request_cancel()
            except Exception:
                pass
            try:
                if orch.session is not None and orch.session.state != STATE_SAVED:
                    orch.cancel()
            except RerecordSessionError as e:
                log.debug(f"[rerecord] cancel: {e}")

        self._stop_replay_worker()
        self._cleanup_rerecord()

    def _stop_replay_worker(self) -> None:
        """Detiene el QThread y libera el worker. Idempotente."""
        if self._rerecord_heartbeat_timer is not None:
            try:
                self._rerecord_heartbeat_timer.stop()
            except Exception:
                pass
            self._rerecord_heartbeat_timer = None
        thread = self._rerecord_thread
        if thread is not None:
            try:
                thread.quit()
                thread.wait(2000)
            except Exception:
                pass
        self._rerecord_thread = None
        self._rerecord_worker = None

    def _cleanup_rerecord(self):
        """Limpieza post-regrabación. SIEMPRE detiene los listeners y
        libera el orchestrator.

        PRD 2026-05-09 §E — **re-entrancy-safe**.

        Antes este método se llamaba recursivamente: ``overlay.close()``
        disparaba ``closeEvent`` que emitía ``cancel_requested`` que
        invocaba ``_on_rerecord_cancel`` que volvía a llamar a
        ``_cleanup_rerecord`` ANTES de que la primera invocación
        terminase. El efecto colateral en modo cola era encolar el
        siguiente paso DOS veces y abrir dos overlays simultáneos —
        eso es lo que el usuario percibía como "ventana pasmada".

        El guard ``_cleanup_in_progress`` corta esa recursión y
        ``overlay.mark_closing()`` se asegura de que ``closeEvent``
        no vuelva a emitir ``cancel_requested`` en el cierre voluntario.

        PRD 2026-05-09 §G — al terminar el cleanup, si hay una cola
        de "Reparar pasos" (``_repair_queue``) en curso, disparamos
        el siguiente paso a regrabar. Eso permite que el botón
        "⚡ Recorrer en cola" del dialog Reparar encadene las
        regrabaciones sin que el usuario tenga que repetir el flujo
        manual N veces.
        """
        if self._cleanup_in_progress:
            log.debug(
                "[rerecord] _cleanup_rerecord re-entrancy bloqueada"
            )
            return
        self._cleanup_in_progress = True
        try:
            if self._poll_timer:
                self._poll_timer.stop()
                self._poll_timer = None
            if self._fragment_recorder:
                if self._fragment_recorder.is_recording():
                    try:
                        self._fragment_recorder.stop()
                    except Exception as e:
                        log.debug(f"cleanup stop: {e}")
                self._fragment_recorder = None
            self._rerecord_step_idx = -1
            self._stop_replay_worker()
            if self._rerecord_orchestrator is not None:
                try:
                    self._rerecord_orchestrator.release()
                except Exception:
                    pass
                self._rerecord_orchestrator = None
            # Importante: detacheamos la referencia ANTES de cerrar para
            # que cualquier ``closeEvent`` re-entrante encuentre
            # ``self._rerecord_overlay is None`` y salga limpio. También
            # marcamos el overlay como "cerrándose voluntariamente" para
            # que ``closeEvent`` no emita ``cancel_requested``.
            ov = self._rerecord_overlay
            self._rerecord_overlay = None
            if ov is not None:
                try:
                    if hasattr(ov, "mark_closing"):
                        ov.mark_closing()
                except Exception:
                    pass
                try:
                    ov.close()
                except Exception as e:
                    log.debug(f"cleanup overlay close: {e}")
            try:
                self.showNormal()
                self.raise_()
                self._center_on_screen()
            except Exception as e:
                log.debug(f"cleanup restore main window: {e}")
        finally:
            self._cleanup_in_progress = False
        # Encadenar la cola de Reparar (si la hay). Lo hacemos FUERA del
        # bloque finally para que el siguiente iter. ya vea el flag
        # ``_cleanup_in_progress`` en False y un overlay limpio.
        if getattr(self, "_repair_queue", None):
            QTimer.singleShot(400, self._process_next_repair_in_queue)
        else:
            # Cola vacía: salimos del modo cola (resetea progreso).
            self._in_repair_queue = False
            self._repair_queue_total = 0
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PROBAR MISIÓN COMPLETA / REPARAR DÉBILES / OPTIMIZAR (FASE 10)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def test_full_mission(self):
        """Ejecuta toda la misión de inicio a fin para validar end-to-end.

        PRD 2026-05-06b §3 — "Probar misión":
          * No bloqueamos por capture débil si la execution_confidence
            es alta. El approval_gate ya degrada esos casos a
            ``severity=warning``.
          * Cuando sólo hay warnings (no errores), avisamos al usuario
            antes de ejecutar, pero NO bloqueamos:
              "Esta misión tiene advertencias, pero puede probarse
               porque el executor usará estrategias robustas."
        """
        if not self.mission.compiled_execution_graph:
            QMessageBox.information(
                self, "Sin pasos", "Esta misión no tiene pasos compilados."
            )
            return

        # Asegurar que el último estado del usuario está guardado.
        try:
            self._auto_save()
        except Exception as e:
            log.debug(f"_auto_save antes de test_full_mission: {e}")

        # PRD §3: pre-flight visible. Si hay warnings no críticos
        # (e.g. weak_capture_strong_execution), avisamos pero dejamos
        # ejecutar.
        try:
            from app.services.missions.approval_gate import (
                check_mission_approvable,
            )
            pre_report = check_mission_approvable(self.mission)
            if pre_report.ok and pre_report.warnings:
                proceed = QMessageBox.question(
                    self, "Probar misión",
                    "Esta misión tiene advertencias, pero puede probarse "
                    "porque el executor usará estrategias robustas.\n\n"
                    f"({len(pre_report.warnings)} advertencias no bloqueantes)",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Ok,
                )
                if proceed != QMessageBox.StandardButton.Ok:
                    return
        except Exception as e:
            log.debug(f"pre-flight gate check skipped: {e}")

        self.showMinimized()
        QApplication.processEvents()
        import time as _t
        _t.sleep(0.4)

        try:
            from app.services.missions.smart_runner_bridge import (
                run_mission_smart,
            )
            from app.services.missions.player import MissionPlayer
            from app.contracts.mission import ReplayStatus

            def _legacy_runner(mission):
                player = MissionPlayer(mission)
                return player.replay()

            decision = run_mission_smart(
                self.mission,
                on_status=lambda m: log.info(f"[Nevlan] {m}"),
                expert_mode=False,
                allow_legacy_fallback=True,
                run_legacy_player=_legacy_runner,
            )
            if decision.blocked:
                ok = False
                top = "; ".join(b.message for b in decision.blockers[:3])
                error = top or decision.reason
            elif decision.result is not None:
                ok = decision.result.status == "success"
                error = "" if ok else decision.result.last_message
            else:
                ok = True
                error = ""
        except Exception as e:
            ok, error = False, str(e)

        _t.sleep(0.2)
        self.showNormal()
        self.raise_()
        self._center_on_screen()

        if ok:
            QMessageBox.information(
                self, "✅ Misión exitosa",
                "La misión se ejecutó completa sin errores."
            )
        else:
            QMessageBox.warning(
                self, "❌ Misión falló",
                f"La ejecución se interrumpió:\n{error}\n\n"
                "Revisa los pasos amarillos/rojos."
            )

    def _maybe_launch_weak_repair_from_review(self) -> None:
        """Abre «Reparar débiles» cuando el review se abrió desde el post-grabación."""
        if not getattr(self, "_auto_open_weak_repair", False):
            return
        self._auto_open_weak_repair = False
        try:
            self.raise_()
            self.activateWindow()
            self.repair_weak_steps()
        except Exception as e:
            log.debug(f"_maybe_launch_weak_repair_from_review: {e}")

    def repair_weak_steps(self):
        """Asistente "Reparar débiles" — versión PRD 2026-05-06b §5.

        Antes mostraba todos los pasos rojos/amarillos según
        ``compute_step_quality`` (capture_score), lo que pedía
        regrabar 4 de 5 pasos en una misión sencilla. Ahora corremos
        el pipeline semántico completo:

          1. ``intent_collapse_engine`` (rescate de intención).
          2. ``ghost_simulation`` con semantic_override.
          3. ``compute_mission_execution_confidence``.
          4. ``check_mission_approvable`` v2.

        Y sólo mostramos los pasos que después de ese pipeline siguen
        siendo bloqueantes reales (``ExecLevel.RED`` con
        ``is_blocking``) o claramente ámbar accionable.
        """
        # 1. Intent collapse: regenera bloques semánticos limpios.
        try:
            from app.services.missions.intent_collapse_engine import (
                apply_collapse_to_mission,
            )
            # En "Reparar débiles" sí queremos sobrescribir el grafo
            # legacy con la versión semántica limpia (es el output que
            # la UI muestra).
            apply_collapse_to_mission(self.mission, rewrite_compiled_graph=True)
        except Exception as e:
            log.debug(f"repair_weak_steps: collapse skipped: {e}")
        # 2. Ghost simulation con semantic override.
        try:
            from app.services.missions.ghost_simulation import (
                simulate_mission, mark_missing_for_review,
            )
            ghost_rep = simulate_mission(self.mission)
            mark_missing_for_review(self.mission, ghost_rep)
        except Exception as e:
            log.debug(f"repair_weak_steps: ghost skipped: {e}")
        # 3. Execution confidence (efecto lateral: persiste por step).
        exec_report = compute_mission_execution_confidence(self.mission)
        # 4. Approval gate v2.
        from app.services.missions.approval_gate import check_mission_approvable
        gate_report = check_mission_approvable(self.mission)
        gate_blockers_by_step = {}
        for v in gate_report.errors:
            if v.step_id:
                gate_blockers_by_step.setdefault(v.step_id, []).append(v)

        steps = self.mission.compiled_execution_graph
        interp = self.mission.interpreted_steps
        rows: list[tuple[int, str, str, str]] = []
        for i, s in enumerate(steps):
            i_step = interp[i] if i < len(interp) else None
            exec_r = exec_report.steps[i] if i < len(exec_report.steps) else None
            gate_violations = gate_blockers_by_step.get(s.id, [])
            # Sólo mostramos:
            #   • bloqueantes del approval_gate v2, o
            #   • pasos cuya execution_confidence es RED/AMBER y además
            #     no tienen ya una estrategia robusta resolviendo.
            is_blocking = bool(gate_violations) or (
                exec_r is not None and exec_r.is_blocking
            )
            is_amber_actionable = (
                exec_r is not None
                and exec_r.level == ExecLevel.AMBER
                and (s.action_payload or {}).get("needs_user_label")
            )
            if not (is_blocking or is_amber_actionable):
                continue
            desc = (i_step.description if i_step else "") or f"Paso {i+1}"
            if gate_violations:
                reason = "; ".join(v.code for v in gate_violations[:2])
            elif exec_r is not None:
                reason = exec_r.message or "; ".join(exec_r.reasons[:2])
            else:
                reason = "paso pendiente"
            if exec_r and exec_r.block_code in {
                "empty_profile", "empty_query", "empty_url",
                "empty_app", "empty_target", "empty_target_label",
            }:
                rec = "Confirmar dato faltante"
            elif exec_r and exec_r.block_code == "unknown_kind":
                rec = "Recompilar o eliminar paso"
            else:
                rec = "Reforzar con 1 clic (imagen/UI)"
            rows.append((i, desc, reason, rec))

        if not rows:
            show_warning(
                self,
                title="Misión estable",
                message=(
                    "No hay pasos marcados como débiles o en revisión "
                    "con acción recomendada."
                ),
                primary="Aceptar",
                cancel="",
            )
            return

        dlg = QDialog(self)
        # PRD 2026-05-09 §G — el dialog de Reparar es no-modal-bloqueante
        # respecto al MissionReviewDialog, pero el QDialog en sí sí es
        # modal de aplicación para que sus botones reciban foco/clicks
        # sin competir con otros widgets. Window flags explícitas evitan
        # los issues vistos donde el dialog quedaba sin foco y sus
        # botones no respondían.
        dlg.setModal(True)
        dlg.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        dlg.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        dlg.setWindowTitle(f"Reparar pasos — Nevlan ({len(rows)})")
        dlg.resize(620, 460)
        dlg.setMinimumWidth(560)
        dlg.setStyleSheet(f"""
            QDialog {{
                background-color: rgba(18, 18, 24, 253);
                border: 1px solid {NevlanDesign.BORDER};
                border-radius: 12px;
            }}
            QLabel {{
                color: #e8e8f0;
                font-size: 11px;
                border: none;
                background: transparent;
            }}
            QListWidget {{
                background: rgba(0,0,0,0.42);
                border: 1px solid {NevlanDesign.BORDER};
                border-radius: 8px;
                padding: 4px;
                color: #e8e8f0;
                outline: none;
                font-size: 11px;
            }}
            QListWidget::item {{
                padding: 12px 10px;
                border-radius: 6px;
                margin-bottom: 4px;
                border: 1px solid transparent;
            }}
            QListWidget::item:selected {{
                background: rgba(9,132,227,0.38);
                border: 1px solid rgba(74, 185, 255, 0.35);
            }}
            QListWidget::item:hover {{
                background: rgba(255,255,255,0.07);
            }}
            QPushButton {{
                background: rgba(255,255,255,0.09);
                color: #dfe6e9;
                border: 1px solid {NevlanDesign.BORDER};
                border-radius: 8px;
                padding: 10px 18px;
                font-size: 11px;
                font-weight: 600;
                min-height: 18px;
            }}
            QPushButton:hover {{
                background: rgba(255,255,255,0.15);
            }}
            QPushButton#weakRepairQueueBtn {{
                background: rgba(255, 200, 80, 0.18);
                color: #ffd166;
                border: 1px solid rgba(255, 200, 80, 0.35);
            }}
            QPushButton#weakRepairQueueBtn:hover {{
                background: rgba(255, 200, 80, 0.30);
            }}
            QPushButton#weakRepairCloseBtn {{
                color: #8892a6;
                font-weight: 500;
            }}
        """)
        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        title_lbl = QLabel(f"🩹  Pasos que requieren atención  ·  {len(rows)}")
        title_lbl.setStyleSheet(
            "font-size: 14px; font-weight: 800; color: #dfe6e9;"
            " letter-spacing: 0.2px;"
        )
        outer.addWidget(title_lbl)
        help_lbl = QLabel(
            "Selecciona un paso y pulsa <b>🎯 Regrabar paso</b> "
            "(o haz doble-clic). Se ejecutarán <b>automáticamente los "
            "pasos anteriores</b> y al llegar al paso seleccionado se "
            "abrirá la ventana de regrabación."
        )
        help_lbl.setWordWrap(True)
        help_lbl.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(help_lbl)

        lw = QListWidget()
        lw.setAlternatingRowColors(False)
        for idx, desc, reason, rec in rows:
            it = QListWidgetItem(
                f"Paso {idx + 1}  ·  {desc[:80]}\n"
                f"    Motivo: {reason}\n"
                f"    → {rec}"
            )
            it.setData(Qt.ItemDataRole.UserRole, idx)
            lw.addItem(it)
        if lw.count() > 0:
            lw.setCurrentRow(0)
        outer.addWidget(lw, 1)

        chosen: list = [None]

        def _open_one(item: QListWidgetItem):
            chosen[0] = int(item.data(Qt.ItemDataRole.UserRole))
            dlg.accept()

        lw.itemDoubleClicked.connect(_open_one)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        rerecord_btn = QPushButton("🎯 Regrabar paso")
        rerecord_btn.setObjectName("weakRepairRerecordBtn")
        rerecord_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rerecord_btn.setDefault(True)
        rerecord_btn.setAutoDefault(True)

        def _rerecord_one():
            it = lw.currentItem()
            if it is None:
                return
            chosen[0] = int(it.data(Qt.ItemDataRole.UserRole))
            dlg.accept()

        rerecord_btn.clicked.connect(_rerecord_one)
        btn_row.addWidget(rerecord_btn, 1)

        queue_btn = QPushButton(f"⚡ Regrabar los {len(rows)} en cola")
        queue_btn.setObjectName("weakRepairQueueBtn")
        queue_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def _queue_all():
            chosen[0] = "__queue__"
            dlg.accept()

        queue_btn.clicked.connect(_queue_all)
        btn_row.addWidget(queue_btn, 1)

        close_btn = QPushButton("Cerrar")
        close_btn.setObjectName("weakRepairCloseBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(close_btn)

        outer.addLayout(btn_row)

        try:
            dlg.raise_()
            dlg.activateWindow()
            rerecord_btn.setFocus()
        except Exception:
            pass

        if dlg.exec() != int(QDialog.DialogCode.Accepted):
            return

        if chosen[0] is None:
            return
        if chosen[0] == "__queue__":
            self._repair_queue = [r[0] for r in rows]
            # PRD 2026-05-09 §F — entramos en modo cola: rerecord_step
            # leerá ``_in_repair_queue`` para saltar el modal "Preparando
            # regrabación" y mostrará "Paso N de M" en el overlay.
            self._in_repair_queue = True
            self._repair_queue_total = len(rows)
            self._weak_repair_ui_session = WeakStepRepairSession(
                total_steps=len(rows)
            )
            try:
                self._weak_repair_ui_session.begin_queue_after_dialog()
            except Exception:
                pass
            QTimer.singleShot(120, self._process_next_repair_in_queue)
            return
        if isinstance(chosen[0], int) and chosen[0] >= 0:
            # PRD 2026-05-09 §G — al "Reparar" un paso se debe regrabar
            # con replay previo automático: rerecord_step ejecuta los
            # pasos anteriores 0..idx-1 y luego abre RerecordOverlay
            # para capturar el paso idx.
            QTimer.singleShot(80, lambda i=chosen[0]: self.rerecord_step(i))

    def _process_next_repair_in_queue(self):
        """PRD 2026-05-09 §G — Procesa el siguiente paso de la cola
        ``_repair_queue`` lanzando ``rerecord_step``.

        El encadenado se hace en :meth:`_cleanup_rerecord`, que es la
        única salida garantizada del flujo de regrabación
        (Save / Cancel / cerrar overlay siempre lo invocan). Cuando la
        cola se vacía, mostramos un :class:`NevlanDialog` con el
        resumen y descartamos la sesión.
        """
        queue = getattr(self, "_repair_queue", None) or []
        sess = getattr(self, "_weak_repair_ui_session", None)
        if not queue:
            if isinstance(sess, WeakStepRepairSession):
                try:
                    sess.mark_completed_queue()
                except Exception:
                    pass
                progress = ""
                try:
                    progress = sess.progress_label_es()
                except Exception:
                    pass
                show_warning(
                    self,
                    title="Reparación completa",
                    message=(
                        f"{progress}. Cola de pasos a regrabar revisada."
                        if progress
                        else "Cola de pasos a regrabar revisada."
                    ),
                    primary="Aceptar",
                    cancel="",
                )
            self._weak_repair_ui_session = None
            self._repair_queue = []
            # PRD 2026-05-09 §F — salimos del modo cola para que las
            # regrabaciones individuales que el usuario lance después
            # vuelvan a mostrar el modal de preparación con detalles.
            self._in_repair_queue = False
            self._repair_queue_total = 0
            return

        idx = queue.pop(0)
        try:
            if isinstance(sess, WeakStepRepairSession):
                try:
                    sess.entering_pick(idx)
                except Exception:
                    pass
            self.rerecord_step(idx)
        except Exception as e:
            log.error(f"_process_next_repair_in_queue: {e}")
            if isinstance(sess, WeakStepRepairSession):
                try:
                    sess.mark_failed(str(e))
                except Exception:
                    pass
            QTimer.singleShot(300, self._process_next_repair_in_queue)

    def _process_next_weak(self):
        queue = getattr(self, "_weak_queue", None) or []
        sess = getattr(self, "_weak_repair_ui_session", None)
        if not queue:
            if isinstance(sess, WeakStepRepairSession):
                try:
                    sess.mark_completed_queue()
                except Exception:
                    pass
                QMessageBox.information(
                    self, "Reparación completa",
                    f"{sess.progress_label_es()}. "
                    "Cola de pasos débiles revisada.",
                )
            else:
                QMessageBox.information(
                    self, "Reparación completa",
                    "Terminamos de revisar la cola de pasos débiles.",
                )
            try:
                self._weak_repair_ui_session = None
            except Exception:
                pass
            return

        idx = queue.pop(0)
        try:
            if isinstance(sess, WeakStepRepairSession):
                sess.entering_pick(idx)

            self.pick_correct_element(idx)
            self._weak_watch_iters = 0

            marked_done_once = [False]

            def _watch():
                self._weak_watch_iters += 1
                cap = getattr(self, "_one_click", None)
                done_natural = (
                    cap is None
                    or getattr(cap, "_done", False)
                    or self._weak_watch_iters > 60
                )
                sx = getattr(self, "_weak_repair_ui_session", None)
                if (
                    isinstance(sx, WeakStepRepairSession)
                    and cap is not None
                    and getattr(cap, "_done", False)
                    and not marked_done_once[0]
                ):
                    try:
                        sx.mark_success()
                        marked_done_once[0] = True
                    except Exception:
                        pass

                if done_natural:
                    QTimer.singleShot(400, self._process_next_weak)
                else:
                    QTimer.singleShot(300, _watch)

            QTimer.singleShot(500, _watch)
        except Exception as e:
            log.error(f"repair_weak_steps: {e}")
            if isinstance(sess, WeakStepRepairSession):
                try:
                    sess.mark_failed(str(e))
                except Exception:
                    pass
            QTimer.singleShot(300, self._process_next_weak)

    def optimize_mission(self):
        """Aplica MissionOptimizer (FASE 11)."""
        try:
            from app.services.missions.optimizer import MissionOptimizer
        except Exception as e:
            log.error(f"MissionOptimizer no disponible: {e}")
            QMessageBox.warning(
                self, "Optimización", f"No disponible: {e}"
            )
            return

        try:
            summary = MissionOptimizer.optimize(self.mission)
        except Exception as e:
            log.error(f"MissionOptimizer falló: {e}")
            QMessageBox.warning(
                self, "Optimización", f"La optimización falló: {e}"
            )
            return

        self._auto_save()
        self.refresh_cards()

        # Construir un mensaje legible para el usuario.
        actions = summary.get("actions") or []
        details_block = ""
        if actions:
            shown = "\n".join(f"  • {a}" for a in actions[:10])
            extra = (
                f"\n  … y {len(actions) - 10} cambios más"
                if len(actions) > 10 else ""
            )
            details_block = f"\n\nCambios aplicados:\n{shown}{extra}"

        QMessageBox.information(
            self, "✨ Optimización aplicada",
            f"📊 Resumen:\n"
            f"  • Pasos eliminados: {summary.get('removed', 0)}\n"
            f"  • Pasos fusionados: {summary.get('merged', 0)}\n"
            f"  • Pasos reescritos: {summary.get('rewritten', 0)}\n"
            f"  • Pasos marcados como peligrosos: {summary.get('marked_dangerous', 0)}\n"
            f"\n🛡️ Confiabilidad de la misión: "
            f"{summary.get('reliability_score', 0)}/100"
            f"{details_block}"
        )

    def _analyze_optimizable(self) -> str:
        steps = self.mission.compiled_execution_graph
        n_typewrite = 0
        n_coord_only = 0
        n_low_score = 0
        n_redundant_clicks = 0
        prev_action = None
        prev_target_label = None
        for s in steps:
            strat = getattr(s.action_strategy, "value", str(s.action_strategy))
            if strat == "type_text" and (s.action_payload or {}).get("text"):
                n_typewrite += 1
            tc = s.target_context
            has_strong = bool(
                (tc.web_data or {}).get("locators")
                or (tc.uia_data or {}).get("automation_id")
                or (tc.uia_data or {}).get("name")
                or tc.anchor_bbox
            )
            if not has_strong and tc.fallback_coords:
                n_coord_only += 1
            if _estimate_step_confidence(s) < 70:
                n_low_score += 1
            cur_label = (tc.semantic or {}).get("human_label") or ""
            if prev_action == strat and cur_label and cur_label == prev_target_label:
                n_redundant_clicks += 1
            prev_action = strat
            prev_target_label = cur_label

        if not (n_typewrite or n_coord_only or n_low_score or n_redundant_clicks):
            return ""
        lines = ["Oportunidades detectadas:"]
        if n_typewrite:
            lines.append(
                f"  • {n_typewrite} TYPE_TEXT podrían ser SET_FIELD_VALUE "
                f"(más rápido y robusto)."
            )
        if n_coord_only:
            lines.append(
                f"  • {n_coord_only} pasos dependen solo de coords absolutas. "
                f"Re-grábalos para capturar el target."
            )
        if n_low_score:
            lines.append(
                f"  • {n_low_score} pasos tienen score < 70. "
                f"Usa “🩹 Reparar débiles”."
            )
        if n_redundant_clicks:
            lines.append(
                f"  • {n_redundant_clicks} clicks consecutivos sobre el mismo "
                f"target podrían fusionarse."
            )
        return "\n".join(lines)

    def test_semantic_step(self, index: int) -> None:
        """Ejecuta el paso ``index`` del plan semántico (Smart Executor)."""
        from app.services.missions.smart_executor import (
            run_single_semantic_plan_step_from_mission,
        )

        self.showMinimized()
        QApplication.processEvents()

        import time
        time.sleep(0.5)

        try:
            success, error = run_single_semantic_plan_step_from_mission(
                self.mission,
                index,
                expert_mode=bool(getattr(self, "expert_mode", False)),
            )
        except Exception as e:
            log.exception(f"[mission_review] test_semantic_step: {e}")
            success = False
            error = str(e)

        time.sleep(0.3)
        self.showNormal()
        self.raise_()
        self._center_on_screen()

        if success:
            QMessageBox.information(
                None,
                "✅ Paso exitoso",
                f"El paso {index + 1} se ejecutó correctamente (plan semántico).",
            )
            return

        res = QMessageBox.warning(
            None,
            "❌ Paso falló",
            f"El paso {index + 1} falló:\n{error}\n\n"
            "¿Quieres regrabarlo desde Mission Review?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if res == QMessageBox.StandardButton.Yes:
            self.rerecord_step(index)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PROBAR PASO
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def test_step(self, index):
        """Ejecuta un solo paso y muestra resultado."""
        if use_semantic_primary_normal_mission_review(
            self.mission,
            expert_mode=bool(getattr(self, "expert_mode", False)),
        ):
            self.test_semantic_step(index)
            return

        from app.services.missions.player import MissionPlayer
        
        self.showMinimized()
        QApplication.processEvents()
        
        import time
        time.sleep(0.5)  # Brief delay for UI to settle
        
        player = MissionPlayer(self.mission)
        success, error = player.test_single_step(index)
        
        time.sleep(0.3)
        self.showNormal()
        self.raise_()
        self._center_on_screen()
        
        if success:
            QMessageBox.information(None, "✅ Paso exitoso",
                f"El paso {index + 1} se ejecutó correctamente.")
        else:
            res = QMessageBox.warning(None, "❌ Paso falló",
                f"El paso {index + 1} falló:\n{error}\n\n¿Quieres regrabarlo?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if res == QMessageBox.StandardButton.Yes:
                self.rerecord_step(index)
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # CRUD
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    def _update_description(self, index, new_text):
        if index < len(self.mission.interpreted_steps):
            self.mission.interpreted_steps[index].description = new_text
    
    def _relink_graph(self):
        steps = self.mission.compiled_execution_graph
        for i in range(len(steps) - 1):
            steps[i].next_step_id_on_success = steps[i+1].id
        if steps:
            steps[-1].next_step_id_on_success = None
        try:
            from app.services.missions.compiler import MissionCompiler
            MissionCompiler.rebuild_action_groups(self.mission)
        except Exception as e:
            log.debug(f"rebuild_action_groups: {e}")
    
    def _auto_save(self):
        from app.services.missions import mission_store
        mission_store.save(self.mission)
        log.debug("Auto-guardado.")

    # ── Four-Layer Operational Model — edición en Mission Review ─────

    def _open_four_layer_step_menu(self, machine_step_id: str) -> None:
        from PyQt6.QtWidgets import QInputDialog, QMenu

        from app.interfaces.desktop.nevlan_dialog import show_info, show_warning
        from app.services.missions.mission_review_four_layer_editing import (
            FourLayerEditError,
            edit_step_params,
            edit_step_strategy,
            get_expert_mapping_lines,
            get_step_display_context,
            move_step_down,
            move_step_up,
            rename_step_label,
            save_mission_edits,
            set_step_disabled,
        )

        try:
            ctx = get_step_display_context(self.mission, machine_step_id)
        except FourLayerEditError as exc:
            show_warning(self, "Edición", str(exc))
            return

        menu = QMenu(self)
        act_rename = menu.addAction("Renombrar etiqueta")
        act_params = menu.addAction("Editar parámetros")
        act_strategy = menu.addAction("Cambiar estrategia")
        act_up = menu.addAction("Subir paso")
        act_down = menu.addAction("Bajar paso")
        if ctx.get("disabled"):
            act_disable = menu.addAction("Activar paso")
        else:
            act_disable = menu.addAction("Desactivar paso")
        act_details = menu.addAction("Ver detalles técnicos")
        chosen = menu.exec(
            self.mapToGlobal(self.rect().center()),
        )
        if not chosen:
            return

        try:
            if chosen == act_rename:
                text, ok = QInputDialog.getText(
                    self,
                    "Renombrar paso",
                    "Etiqueta visible (solo Human View):",
                    text=str(ctx.get("human_label") or ""),
                )
                if ok and text.strip():
                    rename_step_label(self.mission, machine_step_id, text.strip())
            elif chosen == act_params:
                keys = list(ctx.get("editable_keys") or [])
                if not keys:
                    show_warning(self, "Parámetros", "No hay parámetros editables.")
                else:
                    key, ok = QInputDialog.getItem(
                        self,
                        "Parámetro",
                        "Campo:",
                        keys,
                        0,
                        False,
                    )
                    if ok and key:
                        cur = str((ctx.get("params") or {}).get(key) or "")
                        val, ok2 = QInputDialog.getText(
                            self,
                            "Valor",
                            f"{key}:",
                            text=cur,
                        )
                        if ok2:
                            edit_step_params(
                                self.mission,
                                machine_step_id,
                                {key: val},
                            )
            elif chosen == act_strategy:
                strat, ok = QInputDialog.getText(
                    self,
                    "Estrategia",
                    "preferred_strategy:",
                    text=str(ctx.get("preferred_strategy") or ""),
                )
                if ok and strat.strip():
                    edit_step_strategy(
                        self.mission,
                        machine_step_id,
                        strat.strip(),
                    )
            elif chosen == act_up:
                move_step_up(self.mission, machine_step_id)
            elif chosen == act_down:
                move_step_down(self.mission, machine_step_id)
            elif chosen == act_disable:
                set_step_disabled(
                    self.mission,
                    machine_step_id,
                    disabled=not bool(ctx.get("disabled")),
                )
            elif chosen == act_details:
                lines = get_expert_mapping_lines(self.mission)
                show_info(
                    self,
                    "Detalles técnicos (4LOM)",
                    "\n".join(lines[:16]),
                )
                return
            issues = save_mission_edits(self.mission)
            self._auto_save()
            self._rebuild_cards_ui()
            if issues:
                show_warning(
                    self,
                    "Guardado con avisos",
                    "Invariantes: " + ", ".join(issues[:4]),
                )
        except FourLayerEditError as exc:
            show_warning(self, "No se pudo aplicar", str(exc))
        except Exception as exc:
            log.exception("[mission_review] four layer edit: %s", exc)
            show_warning(self, "Error", str(exc))

    def _rebuild_cards_ui(self) -> None:
        """Reconstruye tarjetas tras edición 4LOM."""
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if getattr(self, "_used_semantic_primary_cards", False):
            self._build_cards_semantic_primary()
        else:
            self._build_cards()
        self.cards_layout.addStretch()

    def _append_four_layer_expert_panel(self) -> None:
        if not getattr(self, "expert_mode", False):
            return
        from app.services.missions.mission_review_four_layer_editing import (
            get_expert_mapping_lines,
        )

        box = QFrame()
        box.setStyleSheet(
            "QFrame { background: rgba(0,0,0,30); border-radius: 6px; "
            "border: 1px solid rgba(255,255,255,20); }"
        )
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 6, 8, 6)
        hdr = QLabel("4LOM — Human View → Machine → SEP → COI")
        hdr.setStyleSheet(
            "color:#a29bfe;font-size:10px;font-weight:bold;border:none;"
        )
        lay.addWidget(hdr)
        body = QLabel("\n".join(get_expert_mapping_lines(self.mission)[:14]))
        body.setWordWrap(True)
        body.setStyleSheet(
            "color:#b2bec3;font-size:9px;font-family:Consolas,monospace;border:none;"
        )
        lay.addWidget(body)
        self.cards_layout.addWidget(box)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # CONFIRMAR PERFIL (PRD 2026-05-08d §A.4)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _open_profile_confirm_dialog(self, idx: int) -> None:
        """Abre el diálogo "Confirmar perfil" para el step ``idx``.

        Flujo:

          1. Localiza el step ``select_profile`` en el plan semántico.
          2. Calcula el mejor candidato precargado (alias_store +
             ``candidate_aliases`` del step).
          3. Muestra ``show_text_input`` (NevlanDialog con QLineEdit).
          4. Si el usuario confirma:
               * llama a ``confirm_profile`` (escribe el alias_store +
                 actualiza el step + recompila ``intent_status``),
               * persiste con ``_auto_save``,
               * refresca cards y muestra confirmación de éxito.
          5. Cancelar → no muta nada.

        Idempotente: si el plan ya tiene ``profile_name`` válido,
        el dialog se reutiliza para "cambiar" el perfil; al guardar
        el alias_store se actualiza para próximas misiones.
        """
        from app.interfaces.desktop.nevlan_dialog import (
            RESULT_PRIMARY,
            SEVERITY_CONFIRMATION,
            show_text_input,
            show_warning,
        )
        from app.services.missions.mission_review_confirm import (
            confirm_profile,
        )
        from app.services.missions.profile_resolver import (
            extract_canonical_profile_name,
            is_invalid_profile_evidence,
            lookup_profile_by_alias,
        )

        # 1) localizar el step semántico.
        graph = list(getattr(self.mission, "compiled_execution_graph", []) or [])
        if idx < 0 or idx >= len(graph):
            log.warning(f"[profile_confirm] index fuera de rango: {idx}")
            return
        compiled_step = graph[idx]
        sem_step = find_semantic_step_for_compiled(
            self.mission, getattr(compiled_step, "id", ""),
        )
        if sem_step is None or sem_step.get("type") != "select_profile":
            show_warning(
                self,
                title="Este paso no es un paso de perfil",
                message=(
                    "Solo los pasos «Seleccionar perfil» pueden "
                    "confirmarse desde aquí."
                ),
                primary="Cerrar",
                cancel="",
            )
            return

        # 2) mejor candidato — orden de preferencia:
        #    a) lookup en alias_store (perfil aprendido en sesiones
        #       anteriores: el más útil para "no volver a preguntar"),
        #    b) candidate_aliases del step (lo que el ICE recolectó),
        #    c) profile_name actual si no es genérico,
        #    d) vacío (el usuario tendrá que escribirlo).
        params = sem_step.get("params") or {}
        candidate_aliases = list(params.get("candidate_aliases") or [])
        current = str(params.get("profile_name") or "").strip()
        best = ""

        # a) alias_store: probamos cada candidato (limpiando primero
        #    los inválidos para no consultar con basura).
        for cand in candidate_aliases:
            if not cand or is_invalid_profile_evidence(cand):
                continue
            learned = lookup_profile_by_alias(cand)
            if learned:
                best = learned
                break
        if not best:
            for cand in candidate_aliases:
                if not cand or is_invalid_profile_evidence(cand):
                    continue
                extracted = extract_canonical_profile_name(cand)
                if extracted:
                    best = extracted
                    break
        if not best and current and not is_invalid_profile_evidence(current):
            best = extract_canonical_profile_name(current) or current

        # 3) NevlanDialog con input.
        msg = (
            "Nevlan no pudo identificar de manera confiable qué "
            "perfil de Chrome usaste. Confirma el nombre exactamente "
            "como aparece en el selector de perfiles. Lo guardaré "
            "para que la próxima vez no tenga que preguntar."
        )
        result_key, value = show_text_input(
            self,
            title="Confirmar perfil",
            message=msg,
            field_label="¿Qué perfil debo usar?",
            placeholder="Ej. Daniel Arturo Ramos",
            initial_value=best,
            primary="Confirmar",
            cancel="Cancelar",
            severity=SEVERITY_CONFIRMATION,
        )
        if result_key != RESULT_PRIMARY:
            return

        # 4) aplicar.
        try:
            plan = confirm_profile(self.mission, profile_name=value)
        except ValueError as e:
            show_warning(
                self,
                title="Nombre de perfil inválido",
                message=str(e),
                primary="Volver a intentar",
                cancel="",
            )
            self._open_profile_confirm_dialog(idx)
            return
        except Exception as e:
            log.exception(f"[profile_confirm] error inesperado: {e}")
            show_warning(
                self,
                title="No pude confirmar el perfil",
                message=(
                    "Ocurrió un error guardando el perfil confirmado. "
                    "Intenta de nuevo."
                ),
                primary="Cerrar",
                cancel="",
            )
            return

        try:
            self._auto_save()
        except Exception as e:
            log.warning(f"[profile_confirm] auto_save fallo: {e}")

        # 5) UX feedback + refresh.
        try:
            visible = list(plan.not_ready_reasons or [])
        except Exception:
            visible = []
        msg_ok = (
            f"Perfil confirmado: {value}.\n\n"
            f"Estado del plan: {plan.intent_status}."
        )
        if visible:
            msg_ok += (
                f"\n\nQuedan {len(visible)} aviso(s) por revisar."
            )
        else:
            msg_ok += "\n\n¡La misión ya está lista para ejecutarse!"
        show_warning(
            self,
            title="Perfil guardado",
            message=msg_ok,
            primary="Continuar",
            cancel="",
        )
        self.refresh_cards()

    # ── V2: Vista simple / Time-Travel ─────────────────────────────────
    def _open_simple_view(self) -> None:
        """Abre el ``MissionReviewSimpleDialog`` (vista de bloques)."""
        try:
            from app.interfaces.desktop.mission_review_simple import (
                MissionReviewSimpleDialog,
            )
            dlg = MissionReviewSimpleDialog(self.mission, parent=self)
            res = dlg.exec()
            if res == QDialog.DialogCode.Accepted:
                # Si el usuario aplicó "Reparar automáticamente", la
                # misión cambió; re-renderizamos las tarjetas.
                self.refresh_cards()
        except Exception as e:
            log.error(f"_open_simple_view: {e}")
            QMessageBox.warning(
                self, "Vista simple",
                f"No se pudo abrir la vista simple: {e}",
            )

    def _open_time_travel(self) -> None:
        """Abre el ``TimeTravelDialog`` (slider con screenshots)."""
        try:
            from app.interfaces.desktop.time_travel_ui import (
                TimeTravelDialog,
            )
            dlg = TimeTravelDialog(self.mission, parent=self)
            dlg.exec()
        except Exception as e:
            log.error(f"_open_time_travel: {e}")
            QMessageBox.warning(
                self, "Time-Travel",
                f"No se pudo abrir el visor: {e}",
            )
    
    def delete_step(self, step_idx, card_widget):
        if step_idx < len(self.mission.compiled_execution_graph):
            self.mission.compiled_execution_graph.pop(step_idx)
        if step_idx < len(self.mission.interpreted_steps):
            self.mission.interpreted_steps.pop(step_idx)
        
        card_widget.hide()
        self._relink_graph()
        self._auto_save()
        self.refresh_cards()
        log.info(f"Paso {step_idx} eliminado")
    
    def refresh_cards(self):
        while self.cards_layout.count() > 0:
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._build_cards()
        self.cards_layout.addStretch()
        self.count_lbl.setText(
            self._count_label_text_for_current_view()
        )
        # PRD 2026-05-08 (revisión §4): mantener sincronizado el
        # panel "Confirmado por usuario" con cada refresh.
        try:
            self._refresh_review_confirmations_panel()
        except Exception as e:
            log.debug(f"[mission_review] panel confirmaciones: {e}")
        try:
            self._refresh_semantic_shadow_panel()
        except Exception as e:
            log.debug(f"[mission_review] semantic shadow panel: {e}")
        try:
            self._refresh_semantic_sessions_panel()
        except Exception as e:
            log.debug(f"[mission_review] semantic sessions panel: {e}")
        try:
            self._refresh_cob_panel()
        except Exception as e:
            log.debug(f"[mission_review] cob panel: {e}")
        try:
            self._refresh_tel_panel()
        except Exception as e:
            log.debug(f"[mission_review] tel panel: {e}")
        try:
            self._refresh_tel_sep_promotion_panel()
        except Exception as e:
            log.debug(f"[mission_review] tel→sep promotion panel: {e}")
        try:
            self._refresh_arl_panel()
        except Exception as e:
            log.debug(f"[mission_review] arl panel: {e}")
        try:
            self._refresh_sll_panel()
        except Exception as e:
            log.debug(f"[mission_review] sll panel: {e}")
        try:
            self._refresh_ompc_panel()
        except Exception as e:
            log.debug(f"[mission_review] ompc panel: {e}")
        try:
            self._refresh_org_panel()
        except Exception as e:
            log.debug(f"[mission_review] org panel: {e}")
        try:
            self._refresh_ocr_panel()
        except Exception as e:
            log.debug(f"[mission_review] ocr panel: {e}")
        try:
            self._refresh_goor_panel()
        except Exception as e:
            log.debug(f"[mission_review] goor panel: {e}")
        try:
            self._refresh_lop_panel()
        except Exception as e:
            log.debug(f"[mission_review] lop panel: {e}")
        try:
            self._refresh_pcof_panel()
        except Exception as e:
            log.debug(f"[mission_review] pcof panel: {e}")
        try:
            self._refresh_uol_runtime_panel()
        except Exception as e:
            log.debug(f"[mission_review] uol runtime panel: {e}")
        try:
            self._refresh_orce_panel()
        except Exception as e:
            log.debug(f"[mission_review] orce panel: {e}")
        try:
            self._refresh_beta_acceptance_panel()
        except Exception as e:
            log.debug(f"[mission_review] beta acceptance panel: {e}")

    def _refresh_beta_acceptance_panel(self) -> None:
        while self._beta_acceptance_layout.count() > 0:
            it = self._beta_acceptance_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        from app.services.runtime.beta_runtime_acceptance import (
            beta_runtime_normal_label,
            expert_panel_lines,
        )

        normal_lbl = beta_runtime_normal_label(self.mission)
        if not getattr(self, "expert_mode", False):
            self.beta_acceptance_panel.setVisible(False)
            if normal_lbl:
                self.beta_acceptance_normal_lbl.setText(normal_lbl)
                color = "#00b894" if normal_lbl == "Lista para ejecutar" else "#fdcb6e"
                self.beta_acceptance_normal_lbl.setStyleSheet(
                    f"color: {color}; font-size: 11px; font-weight: bold; "
                    "border: none; background: transparent;"
                )
                self.beta_acceptance_normal_lbl.setVisible(True)
            else:
                self.beta_acceptance_normal_lbl.setVisible(False)
            return

        self.beta_acceptance_normal_lbl.setVisible(False)
        lines = expert_panel_lines(self.mission, expert_mode=True)
        if not lines:
            self.beta_acceptance_panel.setVisible(False)
            return
        for ln in lines:
            self._beta_acceptance_layout.addWidget(QLabel(ln))
        self.beta_acceptance_panel.setVisible(True)

    def _refresh_orce_panel(self) -> None:
        while self._orce_layout.count() > 0:
            it = self._orce_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.orce_panel.setVisible(False)
            return

        try:
            from app.services.runtime.operational_runtime_consistency_engine import (
                expert_panel_lines,
            )

            lines = expert_panel_lines(self.mission)
        except Exception as e:
            log.debug("ORCE panel: %s", e)
            self.orce_panel.setVisible(False)
            return

        if not lines:
            self.orce_panel.setVisible(False)
            return

        for ln in lines:
            self._orce_layout.addWidget(QLabel(ln))
        self.orce_panel.setVisible(True)

    def _refresh_uol_runtime_panel(self) -> None:
        while self._uol_runtime_layout.count() > 0:
            it = self._uol_runtime_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.uol_runtime_panel.setVisible(False)
            return

        try:
            from app.services.missions.uol_runtime_telemetry import expert_panel_lines

            lines = expert_panel_lines(self.mission)
        except Exception as e:
            log.debug("UOL runtime panel: %s", e)
            self.uol_runtime_panel.setVisible(False)
            return

        if not lines:
            self.uol_runtime_panel.setVisible(False)
            return

        for ln in lines:
            self._uol_runtime_layout.addWidget(QLabel(ln))
        self.uol_runtime_panel.setVisible(True)

    def _refresh_semantic_sessions_panel(self) -> None:
        while self._semantic_sessions_layout.count() > 0:
            it = self._semantic_sessions_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.semantic_sessions_panel.setVisible(False)
            return

        m = self.mission
        if not getattr(m, "semantic_sessions", None) and not getattr(
            m, "session_shadow_mode", False,
        ):
            self.semantic_sessions_panel.setVisible(False)
            return

        try:
            from app.services.missions.semantic_session_engine import (
                build_session_audit_dict,
            )

            audit = build_session_audit_dict(m)
            m.semantic_session_audit = audit
        except Exception as e:
            log.debug(f"build_session_audit_dict: {e}")
            self.semantic_sessions_panel.setVisible(False)
            return

        title = QLabel("Semantic Sessions")
        title.setStyleSheet(
            "color: #a29bfe; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._semantic_sessions_layout.addWidget(title)

        for line in (
            f"  session_count: {audit.get('session_count')}",
            f"  active_sessions: {audit.get('active_sessions')}",
            f"  operational_intent_count: {audit.get('operational_intent_count')}",
            f"  session_agreement_with_sep: {audit.get('session_agreement_with_sep')}",
            f"  average_session_confidence: {audit.get('average_session_confidence')}",
        ):
            self._semantic_sessions_layout.addWidget(QLabel(line))

        # Breve lista de sesiones cerradas
        closed = [s for s in (m.semantic_sessions or []) if s.status.value != "active"]
        for s in closed[:8]:
            self._semantic_sessions_layout.addWidget(
                QLabel(f"  • {s.session_type} ({s.status.value}) — {s.absorbed_events_count} ev"),
            )

        self.semantic_sessions_panel.setVisible(True)

    def _refresh_goor_panel(self) -> None:
        while self._goor_layout.count() > 0:
            it = self._goor_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.goor_panel.setVisible(False)
            return

        try:
            from app.core.config import get_settings

            if not getattr(get_settings(), "GOOR_SHADOW_ENABLED", True):
                self.goor_panel.setVisible(False)
                return
        except Exception:
            self.goor_panel.setVisible(False)
            return

        m = self.mission
        if not getattr(m, "goor_shadow_mode", False):
            self.goor_panel.setVisible(False)
            return

        try:
            from app.services.runtime.goal_oriented_runtime import refresh_goor_shadow

            refresh_goor_shadow(self.mission, get_settings())
        except Exception as e:
            log.debug("GOOR panel: %s", e)
            self.goor_panel.setVisible(False)
            return

        aud = getattr(m, "goal_oriented_runtime_audit", None)
        snaps = getattr(m, "operational_goal_snapshots", None) or []
        last_snap = snaps[-1] if isinstance(snaps, list) and snaps else {}

        if not isinstance(aud, dict) or not aud.get("ok"):
            self.goor_panel.setVisible(False)
            return
        if not isinstance(last_snap, dict):
            self.goor_panel.setVisible(False)
            return

        title = QLabel("Goal-Oriented Runtime (shadow)")
        title.setStyleSheet(
            "color: #ff9ecd; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._goor_layout.addWidget(title)

        met = last_snap.get("metrics") or {}
        agp = last_snap.get("active_goal_progress") or []
        txs = last_snap.get("ranked_candidate_transitions") or []
        ch = last_snap.get("chosen_transition")

        replay_dep = float(last_snap.get("replay_dependency_estimate") or 0.0)

        rp_mean = round(
            sum(float(x.get("recovery_pressure") or 0.0) for x in agp if isinstance(x, dict))
            / max(1, len(agp)),
            4,
        )
        stall_n = len(last_snap.get("stall_signals") or [])

        for line in (
            f"  goor_shadow_mode: {getattr(m, 'goor_shadow_mode', False)}",
            f"  autonomy_estimate: {float(last_snap.get('operational_autonomy_estimate') or 0):.3f}",
            f"  replay_dependency: {replay_dep:.3f}",
            f"  recovery_pressure_mean: {rp_mean:.3f}",
            f"  continuity_preservation_rate: {float(met.get('continuity_preservation_rate') or 0):.3f}",
            f"  dynamic_transition_quality: {float(met.get('dynamic_transition_quality') or 0):.3f}",
            f"  transition_diversity: {float(met.get('transition_diversity') or 0):.3f}",
            f"  recovery_diversity: {float(met.get('recovery_diversity') or 0):.3f}",
            f"  operational_decision_confidence: {float(met.get('operational_decision_confidence') or 0):.3f}",
            f"  stall_signals: {stall_n}",
        ):
            self._goor_layout.addWidget(QLabel(line))

        if agp:
            self._goor_layout.addWidget(QLabel("  Active operational goals:"))
            for gp in agp[:6]:
                if isinstance(gp, dict):
                    self._goor_layout.addWidget(
                        QLabel(
                            "    • "
                            f"{str(gp.get('goal_headline') or gp.get('goal_id') or '')} "
                            f"prog={float(gp.get('progress_ratio') or 0):.2f}",
                        ),
                    )

        self._goor_layout.addWidget(QLabel("  Top candidate transitions (goal-ranked):"))
        for tr in txs[:6]:
            if isinstance(tr, dict):
                self._goor_layout.addWidget(
                    QLabel(
                        "    → "
                        f"{tr.get('from_operational_state')} "
                        f"→ {tr.get('to_operational_state')} score={float(tr.get('promising_score') or 0):.3f}",
                    ),
                )

        if isinstance(ch, dict):
            self._goor_layout.addWidget(
                QLabel(
                    "  Chosen shadow transition: "
                    f"{ch.get('from_operational_state')} → {ch.get('to_operational_state')}",
                ),
            )

        gor = aud.get("goor_vs_sep") if isinstance(aud, dict) else {}
        gor = gor or {}
        gor = gor if isinstance(gor, dict) else {}
        gor = gor or last_snap.get("goor_vs_sep_comparison") or {}
        proj = gor.get("goor_projection") if isinstance(gor, dict) else {}
        if isinstance(proj, dict):
            self._goor_layout.addWidget(
                QLabel(
                    "  avoidable_procedure_steps_hint: "
                    f"{proj.get('estimated_procedural_step_delta')}",
                ),
            )

        self.goor_panel.setVisible(True)

    def _refresh_lop_panel(self) -> None:
        while self._lop_layout.count() > 0:
            it = self._lop_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.lop_panel.setVisible(False)
            return

        try:
            from app.core.config import get_settings

            if not getattr(get_settings(), "LOP_SHADOW_ENABLED", True):
                self.lop_panel.setVisible(False)
                return
        except Exception:
            self.lop_panel.setVisible(False)
            return

        m = self.mission
        if not getattr(m, "lop_shadow_mode", False):
            self.lop_panel.setVisible(False)
            return

        try:
            from app.core.config import get_settings

            gs = get_settings()
            if not getattr(gs, "LOP_SHADOW_ENABLED", True):
                self.lop_panel.setVisible(False)
                return
            from app.services.runtime.live_operational_perception import lop_expert_panel_lines

            if getattr(m, "lop_live_feed_enabled", False):
                from app.services.runtime.lop_sensor_bridge import refresh_lop_from_live_sensors

                refresh_lop_from_live_sensors(m, gs)
            else:
                from app.services.runtime.live_operational_perception import refresh_lop_shadow

                refresh_lop_shadow(m, gs)
            lines = lop_expert_panel_lines(m)
        except Exception as e:
            log.debug("LOP panel: %s", e)
            self.lop_panel.setVisible(False)
            return

        if not lines:
            self.lop_panel.setVisible(False)
            return

        title = QLabel("Live Operational Perception (shadow)")
        title.setStyleSheet(
            "color: #80deea; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._lop_layout.addWidget(title)
        for ln in lines:
            self._lop_layout.addWidget(QLabel(ln))

        aud = getattr(m, "live_operational_perception_audit", None) or {}
        if isinstance(aud, dict) and aud.get("elapsed_ms") is not None:
            self._lop_layout.addWidget(
                QLabel(f"  audit_elapsed_ms: {aud.get('elapsed_ms')} sources={aud.get('sensor_sources')}"),
            )

        self.lop_panel.setVisible(True)

    def _refresh_pcof_panel(self) -> None:
        while self._pcof_layout.count() > 0:
            it = self._pcof_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.pcof_panel.setVisible(False)
            return

        try:
            from app.core.config import get_settings
            from app.services.runtime.pre_click_operational_freeze import (
                pcof_enabled,
                pcof_expert_panel_lines,
            )

            if not pcof_enabled(get_settings()):
                self.pcof_panel.setVisible(False)
                return
            lines = pcof_expert_panel_lines(self.mission)
        except Exception as e:
            log.debug("PCOF panel: %s", e)
            self.pcof_panel.setVisible(False)
            return

        if not lines:
            self.pcof_panel.setVisible(False)
            return

        for ln in lines:
            self._pcof_layout.addWidget(QLabel(ln))

        try:
            from app.services.runtime.universal_operational_element_identity import (
                uoei_expert_panel_lines,
            )

            uoei_lines = uoei_expert_panel_lines(self.mission)
            if uoei_lines:
                self._pcof_layout.addWidget(QLabel(""))
                for ln in uoei_lines:
                    self._pcof_layout.addWidget(QLabel(ln))
        except Exception as exc:
            log.debug("UOEI panel: %s", exc)

        self.pcof_panel.setVisible(True)

    def _refresh_cob_panel(self) -> None:
        while self._cob_layout.count() > 0:
            it = self._cob_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.cob_panel.setVisible(False)
            return

        m = self.mission
        if not getattr(m, "canonical_operational_blocks", None) and not getattr(
            m, "cob_shadow_mode", False,
        ):
            self.cob_panel.setVisible(False)
            return

        try:
            from app.services.missions.canonical_operational_blocks import (
                build_cob_audit,
            )

            audit = build_cob_audit(m)
            m.cob_audit = audit
        except Exception as e:
            log.debug(f"build_cob_audit: {e}")
            self.cob_panel.setVisible(False)
            return

        try:
            from app.services.missions.cob_replay_shadow import (
                simulate_mission_cob_replay_shadow,
            )

            rs_snapshot = simulate_mission_cob_replay_shadow(m, persist=True)
        except Exception as e:
            log.debug(f"cob_replay_shadow simulate: {e}")
            rs_snapshot = None

        try:
            from app.services.missions.cob_runtime_adapter import (
                build_cob_runtime_adapter_report,
            )

            build_cob_runtime_adapter_report(m, replay_snapshot=rs_snapshot, persist=True)
        except Exception as e:
            log.debug(f"cob_runtime_adapter_report: {e}")

        try:
            from app.services.missions.cob_adapter_dry_run_bridge import (
                run_cob_adapter_dry_run_bridge,
            )

            run_cob_adapter_dry_run_bridge(m, persist=True, rebuild_adapter=False)
        except Exception as e:
            log.debug(f"cob_dry_run_bridge: {e}")

        title = QLabel("Canonical Operational Blocks")
        title.setStyleSheet(
            "color: #fdcb6e; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._cob_layout.addWidget(title)

        cmp_sep = audit.get("compare_vs_sep") or {}
        for line in (
            f"  cob_count: {audit.get('cob_count')}",
            f"  agreement_vs_sep: {cmp_sep.get('agreement_rate')}",
            f"  compression_events_per_cob: {cmp_sep.get('compression_ratio_events_per_cob')}",
            f"  noise_reduction_cob_vs_raw: {cmp_sep.get('structural_noise_reduction_cob_vs_raw')}",
        ):
            self._cob_layout.addWidget(QLabel(line))

        for c in (m.canonical_operational_blocks or [])[:12]:
            rs = c.replay_strategy or {}
            rr = c.repair_strategy or {}
            phases = rr.get("phases") or []
            absorbed = int(c.absorbed_steps or 0)
            txt = (
                f"  • {c.canonical_action} | type={c.block_type} | "
                f"conf={c.confidence:.2f} | replay={rs.get('mode', '')} | "
                f"repair_steps={len(phases)} | absorbed={absorbed} | "
                f"stab={c.stability_score:.2f} | port={c.portability_score:.2f}"
            )
            self._cob_layout.addWidget(QLabel(txt))

        if rs_snapshot and isinstance(rs_snapshot, dict):
            sub = QLabel("COB Replay Shadow (simulated)")
            sub.setStyleSheet(
                "color: #fab1a0; font-size: 10px; font-weight: 700; "
                "border: none; background: transparent; margin-top: 4px;"
            )
            self._cob_layout.addWidget(sub)
            comp = rs_snapshot.get("comparison") or {}
            for line in (
                f"  micro_steps_total: {comp.get('cob_replay_micro_steps_total')}",
                f"  avg_readiness: {comp.get('average_operational_readiness')}",
                f"  vs SEP steps: {comp.get('sep_step_count')} | compiled: {comp.get('compiled_graph_step_count')}",
                f"  intent_vs_legacy_hint: {comp.get('intent_driven_vs_event_legacy_hint')}",
            ):
                self._cob_layout.addWidget(QLabel(line))
            for plan in (rs_snapshot.get("plans") or [])[:4]:
                ca = plan.get("canonical_action", "")
                rd = plan.get("operational_readiness")
                self._cob_layout.addWidget(
                    QLabel(f"  ▸ ReplayPlan «{ca}» readiness={rd}"),
                )
                for s in (plan.get("steps") or [])[:8]:
                    oid = s.get("order", "")
                    pid = s.get("phase_id", "")
                    lbl = s.get("label", "")
                    self._cob_layout.addWidget(
                        QLabel(f"     {oid}. {pid} — {lbl}"),
                    )
                gaps = plan.get("gaps") or []
                if gaps:
                    self._cob_layout.addWidget(
                        QLabel(f"     gaps: {', '.join(str(g) for g in gaps[:5])}"),
                    )

        ad = getattr(m, "cob_runtime_adapter_shadow", None)
        if isinstance(ad, dict):
            cov = ad.get("coverage_vs_sep") or {}
            cov_title = QLabel("COB executable coverage (runtime adapter shadow)")
            cov_title.setStyleSheet(
                "color: #74b9ff; font-size: 10px; font-weight: 700; "
                "border: none; background: transparent; margin-top: 4px;"
            )
            self._cob_layout.addWidget(cov_title)
            for line in (
                f"  executable_coverage_ratio: {cov.get('cob_executable_coverage_ratio')}",
                f"  sep_fuzzy_overlap_ratio: {cov.get('sep_adapter_fuzzy_overlap_ratio')}",
                f"  sep_strict_overlap_ratio: {cov.get('sep_adapter_capability_overlap_ratio')}",
                f"  supported_steps: {cov.get('supported_translated_steps')}/{cov.get('translated_step_count')}",
            ):
                self._cob_layout.addWidget(QLabel(line))

        dry = getattr(m, "cob_dry_run_shadow", None)
        if isinstance(dry, dict):
            dr_title = QLabel("COB adapter dry-run (UIC + SEP)")
            dr_title.setStyleSheet(
                "color: #55efc4; font-size: 10px; font-weight: 700; "
                "border: none; background: transparent; margin-top: 4px;"
            )
            self._cob_layout.addWidget(dr_title)
            summ = dry.get("summary") or {}
            self._cob_layout.addWidget(
                QLabel(
                    f"  supported: {summ.get('supported_count')}/{summ.get('total_rows')} "
                    f"({summ.get('supported_ratio')})",
                ),
            )
            for diff_line in (dry.get("compact_diff_preview") or [])[:6]:
                self._cob_layout.addWidget(QLabel(f"  {diff_line[:140]}"))

        pilot_audit = getattr(m, "cob_execution_pilot_audit", None)
        if isinstance(pilot_audit, dict):
            px = QLabel("COB Execution Pilot (Fase 1)")
            px.setStyleSheet(
                "color: #ffeaa7; font-size: 10px; font-weight: 700; "
                "border: none; background: transparent; margin-top: 6px;"
            )
            self._cob_layout.addWidget(px)

            eb = pilot_audit.get("eligible_block_ids_seen") or []
            pe = pilot_audit.get("pilot_executed_ids") or []
            fr = pilot_audit.get("fallback_records") or []
            cmpd = pilot_audit.get("comparison") or {}
            mtr = pilot_audit.get("metrics_snapshot_global") or {}
            evs = pilot_audit.get("events") or []

            for line in (
                f"  eligible_blocks: {', '.join(str(x) for x in eb[:8])}{' …' if len(eb) > 8 else ''}",
                f"  pilot_executed: {', '.join(str(x) for x in pe[:8])}",
                f"  fallbacks_used: {len(fr)}",
                f"  runtime_comparison: «{cmpd.get('per_cob_deltas_summary', '')[:200]}»",
                f"  delta_vs_SEP_avg_ms: {cmpd.get('avg_delta_estimate_ms')}",
                f"  globals: pilot_attempts={mtr.get('pilot_attempts')} success={mtr.get('pilot_success')} "
                f"fallbacks={mtr.get('pilot_fallbacks')} val_fail={mtr.get('validator_failures')} "
                f"coverage_ratio={mtr.get('pilot_coverage_ratio')}",
            ):
                self._cob_layout.addWidget(QLabel(line))

            fal_lines = []
            for rec in fr[:8]:
                fb = rec.get("fallback") or {}
                fal_lines.append(
                    f"[{fb.get('cob_id', '')}] {fb.get('reason', rec.get('reason', ''))}"
                )
            if fal_lines:
                self._cob_layout.addWidget(QLabel(f"  failure/fallback_reasons: {', '.join(fal_lines)}"))

            _fail_shown = 0
            for ev in evs:
                if ev.get("type") != "eligibility_checked":
                    continue
                if ev.get("eligible"):
                    continue
                self._cob_layout.addWidget(
                    QLabel(f"  elig_fail {ev.get('cob_id')} → {ev.get('failures')}"),
                )
                _fail_shown += 1
                if _fail_shown >= 4:
                    break

        self.cob_panel.setVisible(True)

    def _refresh_tel_panel(self) -> None:
        while self._tel_layout.count() > 0:
            it = self._tel_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.tel_panel.setVisible(False)
            return

        m = self.mission
        if not getattr(m, "tel_shadow_mode", False) and not getattr(m, "truth_blocks", None):
            self.tel_panel.setVisible(False)
            return

        try:
            from app.services.missions.truth_extraction_layer import (
                apply_tel_to_mission_record,
                build_tel_audit,
                extract_truth_blocks,
            )

            if getattr(m, "tel_shadow_mode", False):
                apply_tel_to_mission_record(m)
                audit = m.tel_audit or {}
            else:
                audit = build_tel_audit(m, extract_truth_blocks(m))
        except Exception as e:
            log.debug(f"tel panel rebuild: {e}")
            self.tel_panel.setVisible(False)
            return

        title = QLabel("Truth Extraction Layer")
        title.setStyleSheet(
            "color: #ff7675; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._tel_layout.addWidget(title)

        for line in (
            f"  truth_block_count: {audit.get('truth_block_count')}",
            f"  sep_step_count: {audit.get('sep_step_count')}",
            f"  compression_SEP_per_truth_block: {audit.get('compression_sep_steps_per_truth_block')}",
            f"  operational_clarity_avg: {audit.get('operational_clarity_avg')}",
            f"  ambiguity_reduction_score: {audit.get('ambiguity_reduction_score')}",
            f"  noise_hypotheses_absorbed_aggregate: {audit.get('noise_hypotheses_absorbed_aggregate')}",
            f"  sep_runtime_mechanics_leak_penalty: {audit.get('sep_runtime_mechanics_leak_penalty')}",
            f"  replay_simplification_ratio: {audit.get('replay_simplification_ratio')}",
        ):
            self._tel_layout.addWidget(QLabel(line))

        truths = getattr(m, "truth_blocks", None) or []
        sub = QLabel("Truth blocks emitted")
        sub.setStyleSheet(
            "color: #fab1a0; font-size: 10px; font-weight: 700; "
            "border: none; background: transparent; margin-top: 4px;"
        )
        self._tel_layout.addWidget(sub)
        for tb in truths[:16]:
            dbg = getattr(tb, "debug_trace", None) or {}
            noise = dbg.get("absorbed_noise_hypothesis_approx", "")
            why = dbg.get("commit_rationale", "")
            self._tel_layout.addWidget(
                QLabel(
                    f"  • {tb.truth_type} → {tb.human_intent[:100]}"
                    + (f" …" if len(tb.human_intent) > 100 else ""),
                ),
            )
            self._tel_layout.addWidget(
                QLabel(
                    f"     stability={tb.stability_score:.2f} ambiguity={tb.ambiguity_score:.2f} "
                    f"readiness={tb.execution_readiness:.2f} noise≈{noise} "
                    f"sessions={len(tb.absorbed_sessions)} hypotheses={len(tb.absorbed_hypotheses)} "
                    f"events={len(tb.absorbed_events)}",
                ),
            )
            self._tel_layout.addWidget(
                QLabel(
                    "     replay_anchors: "
                    + ", ".join((tb.replay_anchor_candidates or [])[:5]),
                ),
            )
            self._tel_layout.addWidget(QLabel(f"     why_committed: {why}"))

        self.tel_panel.setVisible(True)

    def _refresh_tel_sep_promotion_panel(self) -> None:
        while self._tel_sep_promotion_layout.count() > 0:
            it = self._tel_sep_promotion_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.tel_sep_promotion_panel.setVisible(False)
            return

        m = self.mission
        try:
            from app.core.config import get_settings
            from app.services.missions.tel_sep_promoter import (
                run_tel_sep_hybrid_promotion,
            )

            audit = run_tel_sep_hybrid_promotion(m, get_settings())
        except Exception as e:
            log.debug(f"tel→sep promotion panel: {e}")
            self.tel_sep_promotion_panel.setVisible(False)
            return

        title = QLabel("TEL → SEP Promotion")
        title.setStyleSheet(
            "color: #b2bec3; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._tel_sep_promotion_layout.addWidget(title)

        dec = getattr(m, "tel_sep_promotion_decision", None) or {}
        cand_blocks = getattr(m, "tel_semantic_execution_plan_candidate", None) or {}

        cand_steps = cand_blocks.get("steps") if isinstance(cand_blocks, dict) else []
        cur = (m.semantic_execution_plan or {}) if isinstance(m.semantic_execution_plan, dict) else {}
        cur_steps = cur.get("steps") if isinstance(cur, dict) else []

        lines = (
            f"  decision: {dec.get('decision')}",
            f"  promote: {dec.get('promote')} | mode_arg: {dec.get('mode')}",
            f"  tel_blocks_used: {audit.get('truth_blocks_used')}",
            f"  compression_ratio: {audit.get('compression_ratio')}",
            f"  mechanics_leakage_reduction: {audit.get('mechanics_leakage_reduction')}",
            f"  readiness_delta: {audit.get('readiness_delta')} | ambiguity_truth_avg: "
            f"{audit.get('truth_avg_ambiguity')}",
        )
        for ln in lines:
            self._tel_sep_promotion_layout.addWidget(QLabel(ln))

        self._tel_sep_promotion_layout.addWidget(
            QLabel(f"  decision_reasons: {(dec.get('reasons') or [])[:180]}"),
        )

        cand_t = QLabel("TEL SEP candidato (tipos)")
        cand_t.setStyleSheet(
            "color: #74b9ff; font-weight: 600; margin-top: 6px;"
            "border: none; background: transparent;",
        )
        self._tel_sep_promotion_layout.addWidget(cand_t)
        cand_kinds = [str(x.get("type") or "?") for x in (cand_steps or []) if isinstance(x, dict)]
        self._tel_sep_promotion_layout.addWidget(QLabel(f"  {' → '.join(cand_kinds[:16]) or 'ø'}"))

        cur_t = QLabel("SEP persistido (tipos)")
        cur_t.setStyleSheet(
            "color: #fab1a0; font-weight: 600; margin-top: 4px;"
            "border: none; background: transparent;",
        )
        self._tel_sep_promotion_layout.addWidget(cur_t)
        cur_kinds = [str(x.get("type") or "?") for x in (cur_steps or []) if isinstance(x, dict)]
        self._tel_sep_promotion_layout.addWidget(QLabel(f"  {' → '.join(cur_kinds[:16]) or 'ø'}"))

        val = audit.get("validation") or []
        self._tel_sep_promotion_layout.addWidget(
            QLabel(f"  validation_issues: {'; '.join(str(v) for v in val[:5])}" if val else "  validation: ok"),
        )

        self.tel_sep_promotion_panel.setVisible(True)

    def _refresh_arl_panel(self) -> None:
        while self._arl_layout.count() > 0:
            it = self._arl_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.arl_panel.setVisible(False)
            return

        aud = getattr(self.mission, "adaptive_runtime_audit", None)
        if not isinstance(aud, dict) or not aud.get("events"):
            self.arl_panel.setVisible(False)
            return

        title = QLabel("Adaptive Runtime Layer (Fase 1)")
        title.setStyleSheet(
            "color: #b7a8ff; font-size: 10px; font-weight: 700; "
            "border: none; background: transparent; margin-top: 2px;"
        )
        self._arl_layout.addWidget(title)

        rollup = aud.get("rollup") or {}
        snap = rollup.get("arl_metric_snapshot") or {}
        lines = (
            f"  events_logged: {rollup.get('event_count', len(aud.get('events') or []))}",
            f"  arl_adapt_attempts: {snap.get('adaptations_attempted')}",
            f"  arl_adapt_success: {snap.get('adaptations_successful')}",
            f"  arl_fallbacks_triggered: {snap.get('fallbacks_triggered')}",
            f"  arl_relocation_ok: {snap.get('relocation_successful')}/{snap.get('relocation_attempted')}",
        )
        for line in lines:
            self._arl_layout.addWidget(QLabel(line))

        for ev in (aud.get("events") or [])[-5:]:
            if not isinstance(ev, dict):
                continue
            bundle = ev.get("bundle") or {}
            cls = bundle.get("classification") or {}
            dev = bundle.get("deviation") or {}
            dec = bundle.get("decision") or {}
            self._arl_layout.addWidget(
                QLabel(
                    f"  ▸ {ev.get('type')} | action={ev.get('canonical_action')} "
                    f"| risk={cls.get('risk')} kind={cls.get('primary_type')} "
                    f"| deviation={bool(dev.get('deviation_detected'))} "
                    f"| decide={dec.get('decision')} ({dec.get('rationale')})",
                ),
            )

        self.arl_panel.setVisible(True)

    def _refresh_sll_panel(self) -> None:
        while self._sll_layout.count() > 0:
            it = self._sll_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.sll_panel.setVisible(False)
            return

        try:
            from app.core.config import get_settings

            if not getattr(get_settings(), "SLL_ENABLED", False):
                self.sll_panel.setVisible(False)
                return
        except Exception:
            self.sll_panel.setVisible(False)
            return

        try:
            from app.services.runtime.strategy_learning_layer import (
                get_sll_global_metrics,
                get_strategy_learning_store,
            )

            store = get_strategy_learning_store()
            profiles = store.all_profiles()
            g = get_sll_global_metrics().to_dict()
        except Exception as e:
            log.debug("SLL panel load: %s", e)
            self.sll_panel.setVisible(False)
            return

        title = QLabel("Strategy Learning Layer (Fase 1)")
        title.setStyleSheet(
            "color: #00b894; font-size: 10px; font-weight: 700; "
            "border: none; background: transparent; margin-top: 2px;"
        )
        self._sll_layout.addWidget(title)

        obs_sess = g.get("observations_recorded_session", 0)
        for line in (
            f"  learned_strategy_coverage: {float(g.get('learned_strategy_coverage', 0)):.3f}",
            f"  profiles_loaded: {len(profiles)} (session_obs={obs_sess})",
            f"  avg_runtime_improvement: {float(g.get('avg_runtime_improvement', 0)):.3f}",
            f"  avg_validator_improvement: {float(g.get('avg_validator_improvement', 0)):.3f}",
            f"  fallback_reduction: {float(g.get('fallback_reduction', 0)):.3f}",
            f"  arl_recovery_improvement: {float(g.get('arl_recovery_improvement', 0)):.3f}",
        ):
            self._sll_layout.addWidget(QLabel(line))

        dist = g.get("strategy_confidence_distribution") or {}
        if dist:
            tail = ", ".join(f"{k}:{v}" for k, v in sorted(dist.items())[:8])
            self._sll_layout.addWidget(QLabel(f"  confidence_buckets: {tail}"))

        top = sorted(
            profiles,
            key=lambda p: (p.weighted_success_rate, -p.avg_latency_ms),
            reverse=True,
        )[:8]
        for p in top:
            self._sll_layout.addWidget(
                QLabel(
                    f"  ▸ {p.strategy_name[:56]} | ok≈{p.weighted_success_rate:.2f} "
                    f"n={p.attempts} conf={p.confidence:.2f} lat≈{p.avg_latency_ms:.0f}ms "
                    f"fb={p.fallback_to_sep} arl_r={p.arl_recoveries}",
                ),
            )

        self.sll_panel.setVisible(True)

    def _refresh_ompc_panel(self) -> None:
        while self._ompc_layout.count() > 0:
            it = self._ompc_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.ompc_panel.setVisible(False)
            return

        try:
            from app.core.config import get_settings

            if not getattr(get_settings(), "OMPC_ENABLED", False):
                self.ompc_panel.setVisible(False)
                return
        except Exception:
            self.ompc_panel.setVisible(False)
            return

        try:
            from app.services.runtime.operational_memory import (
                build_operational_memory_audit,
            )

            audit = build_operational_memory_audit(
                mission=self.mission,
                top_n=12,
            )
        except Exception as e:
            log.debug("OMPC panel load: %s", e)
            self.ompc_panel.setVisible(False)
            return

        title = QLabel("Operational Memory & Pattern Consolidation (Fase 1)")
        title.setStyleSheet(
            "color: #a29bfe; font-size: 10px; font-weight: 700; "
            "border: none; background: transparent; margin-top: 2px;"
        )
        self._ompc_layout.addWidget(title)

        g = audit.get("global_metrics") or {}
        for line in (
            f"  operational_patterns: {int(g.get('operational_patterns_count', 0))}",
            f"  stable_workflows: {int(g.get('stable_workflows_count', 0))}",
            f"  fragile_workflows: {int(g.get('fragile_workflows_count', 0))}",
            f"  recurring_recoveries (rows): {int(g.get('recurring_recoveries_count', 0))}",
            f"  optimization_hints (total): {int(g.get('optimization_hint_count', 0))}",
            f"  predicted_transition_accuracy: {float(g.get('predicted_transition_accuracy', 0)):.3f}",
            f"  runtime_optimization_gain_estimate: {float(g.get('runtime_optimization_gain_estimate', 0)):.3f}",
            f"  session_observations: {int(g.get('session_observations', 0))}",
        ):
            self._ompc_layout.addWidget(QLabel(line))

        for row in audit.get("top_patterns") or []:
            prev = str(row.get("sequence_preview") or "")[:72]
            self._ompc_layout.addWidget(
                QLabel(
                    f"  ▸ {row.get('pattern_id')} | wf={row.get('workflow_type', '')[:32]} "
                    f"stab={row.get('stability')} frag={row.get('fragility')} "
                    f"conf={row.get('confidence')} n={row.get('recurrence')} "
                    f"| {prev}",
                ),
            )
            hints = row.get("hints") or []
            if hints:
                self._ompc_layout.addWidget(
                    QLabel(f"     hints: {', '.join(str(h) for h in hints[:6])}"),
                )

        mh = audit.get("mission_hint")
        if isinstance(mh, dict):
            self._ompc_layout.addWidget(QLabel("  — misión actual —"))
            self._ompc_layout.addWidget(
                QLabel(f"    matched_memory: {mh.get('matched')}"),
            )
            for pred in (mh.get("predicted_next") or [])[:5]:
                self._ompc_layout.addWidget(
                    QLabel(
                        f"    next≈ {pred.get('next_token')} "
                        f"p={pred.get('probability')} ({pred.get('explain', '')[:80]})",
                    ),
                )
            for hp in (mh.get("recovery_paths") or [])[:3]:
                self._ompc_layout.addWidget(
                    QLabel(
                        f"    recovery: {hp.get('key')} x{hp.get('count')} — {hp.get('explain', '')[:72]}",
                    ),
                )

        self.ompc_panel.setVisible(True)

    def _refresh_org_panel(self) -> None:
        while self._org_layout.count() > 0:
            it = self._org_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.org_panel.setVisible(False)
            return

        try:
            from app.core.config import get_settings

            if not getattr(get_settings(), "ORG_SHADOW_ENABLED", True):
                self.org_panel.setVisible(False)
                return
        except Exception:
            self.org_panel.setVisible(False)
            return

        try:
            from app.services.runtime.operational_runtime_graph import refresh_operational_shadow

            refresh_operational_shadow(self.mission, get_settings())
        except Exception as e:
            log.debug("ORG panel: %s", e)
            self.org_panel.setVisible(False)
            return

        m = self.mission
        aud = getattr(m, "org_audit", None)
        blob = getattr(m, "operational_runtime_graph", None)
        if not isinstance(aud, dict) or not aud.get("ok"):
            self.org_panel.setVisible(False)
            return

        title = QLabel("Operational Runtime Graph (shadow)")
        title.setStyleSheet(
            "color: #74b9ff; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._org_layout.addWidget(title)

        rs = aud.get("resilience_scores") or {}
        cmp = aud.get("comparison") or {}
        sh = aud.get("shadow_run") or {}

        for line in (
            f"  org_shadow_mode: {getattr(m, 'org_shadow_mode', False)}",
            f"  sources: {', '.join(aud.get('sources') or []) or 'ø'}",
            f"  goals: {(blob or {}).get('goals', []) and len((blob or {}).get('goals', [])) or 0} | "
            f"nodes: {len((blob or {}).get('nodes') or [])} | "
            f"transitions: {len((blob or {}).get('transitions') or [])} | "
            f"recovery_edges: {aud.get('recovery_edge_total')}",
            f"  operational_resilience: {float(rs.get('operational_resilience_score', 0)):.3f} | "
            f"recovery_coverage: {float(rs.get('recovery_coverage', 0)):.3f} | "
            f"replay_independence: {float(rs.get('replay_independence_estimate', 0)):.3f}",
            f"  transition_predictability: {float(rs.get('transition_predictability', 0)):.3f} | "
            f"runtime_compactness: {float(rs.get('runtime_compactness', 0)):.3f} | "
            f"execution_continuity: {float(rs.get('execution_continuity', 0)):.3f}",
            f"  layout_independence: {float(rs.get('layout_independence', 0)):.3f} | "
            f"selector_independence: {float(rs.get('selector_independence', 0)):.3f}",
            f"  ORG↔SEP agreement (paired coarse): {cmp.get('paired_agreement_ratio')}",
            f"  shadow_walk_ok: {sh.get('ok')} | predicted_branch_tail: "
            f"{(sh.get('predicted_optional_branches_from_terminal') or [])[:6]}",
        ):
            self._org_layout.addWidget(QLabel(line))

        goals = ((blob or {}).get("goals") or [])[:4]
        for g in goals:
            if isinstance(g, dict):
                hl = str(g.get("headline") or "")
                rc = str(g.get("rationale") or "")[:220]
                self._org_layout.addWidget(QLabel(f"  Goal: {hl}"))
                if rc:
                    self._org_layout.addWidget(QLabel(f"    — {rc}"))

        nodes = ((blob or {}).get("nodes") or [])[:20]
        if nodes:
            self._org_layout.addWidget(QLabel("  Nodes (operational_state):"))
            for nd in nodes:
                if isinstance(nd, dict):
                    self._org_layout.addWidget(
                        QLabel(
                            f"    • {nd.get('operational_state')} "
                            f"(truth={nd.get('truth_type') or 'sep_fallback'})",
                        ),
                    )

        recv = ((blob or {}).get("recovery_edges") or [])[:8]
        if recv:
            self._org_layout.addWidget(QLabel("  Recoveries:"))
            for r in recv:
                if isinstance(r, dict):
                    self._org_layout.addWidget(
                        QLabel(
                            f"    Δ {r.get('symptom_state')} → {r.get('target_state')} "
                            f"({r.get('recovery_class')})",
                        ),
                    )

        self.org_panel.setVisible(True)

    def _refresh_ocr_panel(self) -> None:
        while self._ocr_layout.count() > 0:
            it = self._ocr_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.ocr_panel.setVisible(False)
            return

        try:
            from app.core.config import get_settings

            if not getattr(get_settings(), "OCR_SHADOW_ENABLED", True):
                self.ocr_panel.setVisible(False)
                return
        except Exception:
            self.ocr_panel.setVisible(False)
            return

        try:
            from app.services.runtime.operational_continuity_runtime import (
                refresh_operational_continuity_shadow,
            )

            refresh_operational_continuity_shadow(self.mission, get_settings())
        except Exception as e:
            log.debug("OCR panel: %s", e)
            self.ocr_panel.setVisible(False)
            return

        m = self.mission
        aud = getattr(m, "operational_continuity_audit", None)
        snap = getattr(m, "continuity_runtime_shadow", None)
        if not isinstance(aud, dict) or not aud.get("ok"):
            self.ocr_panel.setVisible(False)
            return
        if not isinstance(snap, dict):
            self.ocr_panel.setVisible(False)
            return

        title = QLabel("Operational Continuity Runtime (shadow)")
        title.setStyleSheet(
            "color: #fdcb6e; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._ocr_layout.addWidget(title)

        met = snap.get("metrics") or {}
        gx = snap.get("goal_execution_shadow") or {}
        ocr_cmp = snap.get("ocr_vs_sep") or {}

        for line in (
            f"  continuity_score: {float(snap.get('continuity_score') or 0):.3f}",
            f"  continuity_stability: {float(met.get('continuity_stability_score') or 0):.3f}",
            f"  active operational goal: {snap.get('active_goal_headline') or gx.get('headline') or 'ø'}",
            f"  current operational state: {snap.get('inferred_current_operational_state') or 'ø'}",
            f"  predicted next states: {(snap.get('predicted_next_states') or [])[:8]}",
            f"  drift detections: {len(snap.get('drift_detections') or [])} — "
            f"{', '.join(str(d) for d in (snap.get('drift_detections') or [])[:4])}",
            f"  continuity recoveries (prioritized): {len(snap.get('continuity_recoveries') or [])}",
            f"  continuity breaks: {snap.get('continuity_breaks') or []}",
            f"  replay dependence estimate: {float(snap.get('replay_dependence_estimate') or 0):.3f}",
            f"  replay fallback pressure: {float(met.get('replay_fallback_pressure') or 0):.3f}",
            f"  operational autonomy score: {float(snap.get('operational_autonomy_score') or 0):.3f}",
            f"  semantic recovery strength: {float(met.get('semantic_recovery_strength') or 0):.3f}",
            f"  continuity repair coverage: {float(met.get('continuity_repair_coverage') or 0):.3f}",
            f"  transition flexibility: {float(met.get('transition_flexibility') or 0):.3f}",
            f"  provider independence: {float(met.get('provider_independence') or 0):.3f}",
            f"  UI surface independence: {float(met.get('ui_surface_independence') or 0):.3f}",
            f"  operational drift resistance: {float(met.get('operational_drift_resistance') or 0):.3f}",
            f"  goal shadow satisfied: {gx.get('satisfied_shadow')} | path: "
            f"{(gx.get('simulation_path_states') or [])[:8]}",
            f"  OCR↔SEP paired agreement: {ocr_cmp.get('paired_agreement_ratio')}",
        ):
            self._ocr_layout.addWidget(QLabel(line))

        recs = (snap.get("continuity_recoveries") or [])[:8]
        if recs:
            self._ocr_layout.addWidget(QLabel("  Recoveries (semantic-first order):"))
            for r in recs:
                if isinstance(r, dict):
                    self._ocr_layout.addWidget(
                        QLabel(
                            f"    [{r.get('recovery_tier')}] {r.get('recovery_class')} "
                            f"({r.get('symptom_state')}) → {r.get('target_state')}",
                        ),
                    )

        self.ocr_panel.setVisible(True)

    def _refresh_semantic_shadow_panel(self) -> None:
        while self._semantic_shadow_layout.count() > 0:
            it = self._semantic_shadow_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.semantic_shadow_panel.setVisible(False)
            return

        m = self.mission
        has_shadow = (
            (getattr(m, "semantic_first_mode", None) or "").lower() == "shadow"
            or bool(getattr(m, "intent_timeline", None))
        )
        if not has_shadow:
            self.semantic_shadow_panel.setVisible(False)
            return

        try:
            from app.services.missions.recorder_semantic_shadow import (
                build_shadow_audit_dict,
            )

            audit = build_shadow_audit_dict(m)
            m.semantic_shadow_audit = audit
        except Exception as e:
            log.debug(f"build_shadow_audit_dict: {e}")
            self.semantic_shadow_panel.setVisible(False)
            return

        title = QLabel("Semantic-first shadow")
        title.setStyleSheet(
            "color: #00cec9; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._semantic_shadow_layout.addWidget(title)

        lines = [
            f"  semantic_first_shadow_enabled: {audit.get('semantic_first_shadow_enabled')}",
            f"  hypotheses_count: {audit.get('hypotheses_count')}",
            f"  accepted_hypotheses_count: {audit.get('accepted_hypotheses_count')}",
            f"  shadow_vs_sipe_agreement: {audit.get('shadow_vs_sipe_agreement')}",
        ]
        for line in lines:
            self._semantic_shadow_layout.addWidget(QLabel(line))

        mism = audit.get("top_mismatches") or []
        if mism:
            self._semantic_shadow_layout.addWidget(QLabel("  top_mismatches:"))
            for mm in mism[:6]:
                self._semantic_shadow_layout.addWidget(QLabel(f"    • {mm}"))

        self.semantic_shadow_panel.setVisible(True)

    def _refresh_review_confirmations_panel(self):
        """Reconstruye el panel "Confirmado por usuario".

        Política visual (PRD revisión §4):

          * Vista normal — panel oculto. El usuario solo ve los
            pasos limpios.
          * Vista experto — panel visible si hay confirmaciones, con
            una línea por dato confirmado y la traceability de
            ``ready_provenance``. Si no hay confirmaciones,
            permanece oculto (no añade ruido).
        """
        # Limpiamos el layout previo.
        while self._confirmations_layout.count() > 0:
            it = self._confirmations_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if not getattr(self, "expert_mode", False):
            self.confirmations_panel.setVisible(False)
            return

        from app.services.missions.mission_review_summary import (
            build_mission_review_summary,
        )
        summary = build_mission_review_summary(self.mission)

        if not summary.confirmations:
            # En vista experto, sin confirmaciones, también
            # ocultamos — no hay nada que trazar.
            self.confirmations_panel.setVisible(False)
            return

        title = QLabel("Confirmado por el usuario:")
        title.setStyleSheet(
            "color: #a29bfe; font-size: 11px; font-weight: 700; "
            "border: none; background: transparent;"
        )
        self._confirmations_layout.addWidget(title)

        for c in summary.confirmations:
            line = QLabel(f"  {c.field_label}: {c.value}")
            line.setWordWrap(True)
            self._confirmations_layout.addWidget(line)
            if c.original_value and c.original_value != c.value:
                orig = QLabel(f"      (antes: {c.original_value})")
                orig.setStyleSheet(
                    "color: #8892a6; font-size: 10px; "
                    "border: none; background: transparent;"
                )
                self._confirmations_layout.addWidget(orig)

        if summary.expert_note:
            note = QLabel(f"Trazabilidad: {summary.expert_note}")
            note.setWordWrap(True)
            note.setStyleSheet(
                "color: #8892a6; font-size: 10px; font-style: italic; "
                "padding-top: 4px; "
                "border: none; background: transparent;"
            )
            self._confirmations_layout.addWidget(note)

        self.confirmations_panel.setVisible(True)
    
    def cancel_edition(self):
        log.info(f"Edición cancelada: {self.mission.id}")
        self.reject()
    
    def delete_mission(self):
        from app.services.missions import mission_store
        res = QMessageBox.warning(None, "Eliminar automatización", "¿Seguro que deseas eliminar esta automatización?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if res == QMessageBox.StandardButton.Yes:
            mission_store.delete(self.mission.id)
            log.info(f"Automatización eliminada: {self.mission.id}")
            self.reject()
    
    def save_mission(self):
        from app.services.missions import mission_store
        from app.services.missions.asset_finalizer import (
            finalize_mission_assets,
        )
        from app.services.missions.approval_gate import (
            check_mission_approvable,
            classify_status,
        )

        self.mission.name = self.name_edit.text()

        # 1. Asset Finalizer — copiar temp → permanente y reescribir refs.
        try:
            fin_report = finalize_mission_assets(self.mission)
            if not fin_report.ok:
                log.warning(
                    f"Asset finalize devolvió warnings: "
                    f"missing={len(fin_report.missing)}, "
                    f"errors={len(fin_report.errors)}, "
                    f"temp_remaining={len(fin_report.temp_remaining)}"
                )
        except Exception as e:
            log.error(f"Asset finalize falló: {e}")
            fin_report = None

        # 1b. Ghost Simulation — pre-save self-healing.
        # Antes del approval_gate, intentamos localizar cada target en
        # la pantalla actual. Si hay missings, marcamos los steps con
        # ``ghost_missing=True + needs_user_label=True`` para que el
        # gate los redirija a NEEDS_REVIEW (Quick Reinforcement).
        # Esto bloquea misiones rotas antes de que el usuario las
        # descubra en producción. El probe es read-only.
        try:
            from app.services.missions.ghost_simulation import (
                simulate_mission, mark_missing_for_review,
            )
            ghost_report = simulate_mission(self.mission)
            if ghost_report.needs_user_signal:
                marked = mark_missing_for_review(self.mission, ghost_report)
                log.info(
                    f"[ghost] {marked} steps marcados como missing "
                    f"(summary={ghost_report.summary()})"
                )
                if marked:
                    # Aviso ligero al usuario antes del Quick Reinforcement.
                    summary = ghost_report.summary()
                    QMessageBox.information(
                        self,
                        "Verificación pre-guardado",
                        f"Nevlan no pudo encontrar {summary['missing']} "
                        "elemento(s) en pantalla. Te pediré confirmación "
                        "para evitar guardar una misión rota.",
                    )
        except Exception as e:
            log.debug(f"[ghost] simulator falló (no bloqueante): {e}")

        # 2. Approval Gate — clasifica el estado terminal según los
        #    invariantes (PRD 2026-05-03c §1):
        #       - EXECUTABLE      → todo OK, lista para ejecutarse
        #       - NEEDS_REVIEW    → hay needs_user_label / profile vacío
        #                            → ABRIR Quick Reinforcement UI
        #       - COMPILED_DRAFT  → hay rojo / temp / orphan / etc.
        #                            → arreglable por el sistema, NO ejec.
        new_status = classify_status(self.mission)

        if new_status == MissionStatus.NEEDS_REVIEW:
            # Levantar Quick Reinforcement UI: el usuario confirma o
            # edita las etiquetas pendientes y, si tras hacerlo la
            # misión queda limpia, la guardamos como EXECUTABLE.
            try:
                from app.interfaces.desktop.quick_reinforcement import (
                    QuickReinforcementDialog,
                )
                dlg = QuickReinforcementDialog(self.mission, parent=None)
                dlg.exec()
            except Exception as e:
                log.error(f"QuickReinforcementDialog falló: {e}")
            # Re-clasificamos tras el diálogo: el usuario pudo haber
            # arreglado todos los pasos.
            new_status = classify_status(self.mission)

        if new_status != MissionStatus.EXECUTABLE:
            gate = check_mission_approvable(self.mission)
            blockers = "\n  • ".join(v.message for v in gate.errors[:8])
            human_label = (
                "tu confirmación humana" if new_status == MissionStatus.NEEDS_REVIEW
                else "una recompilación"
            )
            QMessageBox.warning(
                None,
                "Misión no ejecutable",
                f"La misión queda como {new_status.value}. Antes de "
                f"poder ejecutarse necesita {human_label}:\n\n  • "
                + blockers
                + "\n\nUsa la sección de Mission Review para arreglar "
                "los pasos marcados.",
            )
            self.mission.status = new_status
            mission_store.save(self.mission)
            log.info(
                f"Automatización guardada como {new_status.value}: "
                f"{self.mission.name} ({len(gate.errors)} bloqueantes)"
            )
            self.accept()
            return

        self.mission.status = MissionStatus.EXECUTABLE
        mission_store.save(self.mission)
        log.info(f"Automatización ejecutable: {self.mission.name}")
        self.accept()
