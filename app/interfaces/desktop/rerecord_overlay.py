"""
Nevlan — Re-record Step Overlay
---------------------------------
Overlay flotante durante la micro-grabación de un paso.
Atajos: Enter=Guardar, Esc=Cancelar, F5=Repetir, Ctrl+Z=Deshacer

Soporta dos fases (PRD 2026-05-08c §C):

  * **replay**: Nevlan ejecuta los pasos previos 1..N-1 antes de
    pedir captura. El overlay muestra "Preparando…" + barra de
    progreso + botón "Empezar" / "Cancelar". El botón "Guardar"
    está deshabilitado durante esta fase.
  * **capture**: tras llegar al paso N, Nevlan habilita la captura
    real (Guardar/Repetir/Cancelar). Esta es la fase clásica.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QPushButton, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from app.core.logger import log


PHASE_REPLAY = "replay"
PHASE_CAPTURE = "capture"


class RerecordOverlay(QWidget):
    """
    Overlay flotante para regrabación de un paso individual.
    Atajos de teclado: Enter=Guardar, Escape=Cancelar, F5=Repetir, Ctrl+Z=Deshacer
    """
    save_requested = pyqtSignal()
    retry_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    rollback_requested = pyqtSignal()
    start_replay_requested = pyqtSignal()

    def __init__(
        self,
        step_index: int,
        step_description: str,
        step_strategy: str,
        parent=None,
        *,
        expert_mode: bool = False,
        phase: str = PHASE_CAPTURE,
        total_previous_steps: int = 0,
    ):
        super().__init__(parent)
        self.step_index = step_index
        self.expert_mode = expert_mode
        self._is_pick = str(step_strategy).lower().strip() == "pick"
        self._save_enabled = False
        self._phase = phase if phase in (PHASE_REPLAY, PHASE_CAPTURE) \
            else PHASE_CAPTURE
        self._total_previous_steps = max(0, int(total_previous_steps))
        # PRD 2026-05-08d §C — flag de "ya emitimos señal terminal"
        # para evitar que ``closeEvent`` re-emita ``cancel_requested``
        # cuando el cancel vino de un click intencional.
        self._emitted_terminal: bool = False

        # Tool + WA_ShowWithoutActivating para que el overlay no robe foco
        # a la ventana destino durante la grabación (Windows eleva la app
        # que tiene foco al llamar raise/activateWindow; sin esto, cada
        # "force on top" puede desenfocar Chrome/Word y tus clicks van
        # dirigidos a Nevlan o al escritorio).
        #
        # PRD 2026-05-08d §B.1 — bug "botones muertos":
        # Antes usábamos ``WindowDoesNotAcceptFocus`` + ``NoFocus`` para
        # evitar el robo de foco a la app destino. Pero esa combinación
        # tiene dos efectos colaterales documentados en Windows 10/11:
        #   (a) los QPushButton del overlay no reciben hover/press
        #       confiable cuando otra ventana es activa, lo que el
        #       usuario percibe como "los botones no responden",
        #   (b) ``keyPressEvent`` jamás se dispara, así que los
        #       atajos Enter/Esc/F5 quedan muertos.
        # Ahora sólo usamos ``WA_ShowWithoutActivating`` (no robamos
        # foco AL MOSTRARNOS) pero permitimos focus-on-click en el
        # overlay. Si el usuario clica sobre un botón del overlay,
        # el foco vendrá al overlay (eso es lo que él quiere); la
        # app destino lo recupera al primer click sobre ella.
        self.setWindowFlags(
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # FocusPolicy.ClickFocus: el overlay solo recibe foco cuando
        # el usuario lo clica directamente. No roba foco al ganar
        # visibilidad, pero los botones SÍ reaccionan.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.container = QFrame()
        self.container.setStyleSheet("""
            QFrame {
                background-color: rgba(20, 20, 28, 245);
                border: 2px solid rgba(255, 120, 50, 0.7);
                border-radius: 10px;
            }
        """)
        c_layout = QVBoxLayout(self.container)
        c_layout.setContentsMargins(14, 10, 14, 10)
        c_layout.setSpacing(4)

        # Header
        header = QHBoxLayout()
        title_txt = (
            "🩹 Clic en el elemento correcto"
            if self._is_pick
            else f"🎯 Regrabando paso {step_index + 1}"
        )
        title = QLabel(title_txt)
        title.setStyleSheet("color: #ff7675; font-weight: bold; font-size: 13px; border: none; background: transparent;")
        # PRD 2026-05-09 §F — guardamos referencia al label del título
        # para poder anteponer "Paso N de M · …" cuando el overlay se
        # usa desde la cola "Reparar pasos débiles".
        self._title_lbl = title
        self._title_base = title_txt
        self._queue_progress: tuple[int, int] | None = None

        self.timer_lbl = QLabel("00:00")
        self.timer_lbl.setStyleSheet("color: #b2bec3; font-family: 'Consolas', monospace; font-size: 11px; border: none; background: transparent;")

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.timer_lbl)
        c_layout.addLayout(header)

        # Original info
        orig_lbl = QLabel(f"Original: {step_description}")
        orig_lbl.setStyleSheet("color: #636e72; font-size: 10px; border: none; background: transparent;")
        orig_lbl.setWordWrap(True)
        if self._is_pick:
            orig_lbl.hide()
        c_layout.addWidget(orig_lbl)

        strat_lbl = QLabel(f"Paso técnico: {step_strategy}")
        strat_lbl.setStyleSheet("color: #636e72; font-size: 10px; border: none; background: transparent;")
        strat_lbl.setVisible(False)
        c_layout.addWidget(strat_lbl)
        self._strat_lbl_ref = strat_lbl

        c_layout.addWidget(self._divider())

        # ── Indicador de progreso de replay (visible solo en phase=replay) ──
        self.replay_progress_lbl = QLabel("")
        self.replay_progress_lbl.setStyleSheet(
            "color: #74b9ff; font-size: 11px; font-weight: bold;"
            " border: none; background: transparent;"
        )
        self.replay_progress_lbl.setWordWrap(True)
        self.replay_progress_lbl.setMinimumHeight(0)
        self.replay_progress_lbl.hide()
        c_layout.addWidget(self.replay_progress_lbl)

        # Interpretado para humanos (sin microhistoria de teclas por defecto)
        self.events_lbl = QLabel("Ejecuta la acción en la aplicación objetivo.")
        self.events_lbl.setStyleSheet(
            "color: #dfe6e9; font-size: 10px;"
            " font-family: 'Segoe UI', system-ui;"
            " border: none; background: transparent;"
        )
        self.events_lbl.setWordWrap(True)
        self.events_lbl.setMinimumHeight(44)
        c_layout.addWidget(self.events_lbl)

        self.expert_lbl = QLabel("")
        self.expert_lbl.setStyleSheet(
            "color: #636e72; font-size: 9px;"
            " font-family: Consolas, monospace;"
            " border: none; background: transparent;"
        )
        self.expert_lbl.setWordWrap(True)
        self.expert_lbl.setMinimumHeight(0)
        self.expert_lbl.hide()
        c_layout.addWidget(self.expert_lbl)

        if not self._is_pick:
            self.expert_chk = QCheckBox("Modo experto (detalle técnico)")
            self.expert_chk.setChecked(self.expert_mode)
            self.expert_chk.setStyleSheet(
                "color: #b2bec3; font-size: 9px; border: none; background: transparent;"
            )
            self.expert_chk.stateChanged.connect(self._on_expert_changed)
            c_layout.addWidget(self.expert_chk)

        c_layout.addWidget(self._divider())

        # ── Instrucciones de atajos (VISIBLES) ──
        shortcuts_lbl = QLabel(
            "⌨️  Enter = Guardar  ·  Esc = Cancelar  ·  F5 = Repetir  ·  Ctrl+Z = Deshacer"
        )
        shortcuts_lbl.setStyleSheet("color: #ffa500; font-size: 10px; font-weight: bold; border: none; background: transparent;")
        shortcuts_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        c_layout.addWidget(shortcuts_lbl)

        c_layout.addWidget(self._divider())

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        self.btn_start = QPushButton("▶ Empezar")
        self.btn_start.setObjectName("rerecordBtnStart")
        self.btn_start.setVisible(self._phase == PHASE_REPLAY)
        # PRD 2026-05-08d §C — botones siempre clickeables. La validación
        # "no hay captura" se hace dentro del handler con NevlanDialog
        # ("No hay nada que guardar"), nunca dejando un botón muerto.
        # ``_save_enabled`` se mantiene como hint visual interno, pero
        # el botón ya NO se setEnabled(False) por sí mismo.
        self.btn_save = QPushButton("💾 Guardar (Enter)")
        self.btn_save.setObjectName("rerecordBtnSave")
        self.btn_save.setEnabled(True)
        self.btn_retry = QPushButton("🔄 Repetir (F5)")
        self.btn_retry.setObjectName("rerecordBtnRetry")
        self.btn_cancel = QPushButton("❌ Cancelar (Esc)")
        self.btn_cancel.setObjectName("rerecordBtnCancel")
        # Cursor pointer para indicar afordancia.
        for btn in (self.btn_start, self.btn_save, self.btn_retry,
                    self.btn_cancel):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

        btn_style = """
            QPushButton {
                background: rgba(255,255,255,12);
                border: 1px solid rgba(255,255,255,25);
                border-radius: 4px; color: white;
                padding: 0 8px; font-size: 10px;
            }
            QPushButton:hover { background: rgba(255,255,255,30); }
            QPushButton:disabled { color: rgba(255,255,255,40); }
        """
        for btn in [self.btn_start, self.btn_save, self.btn_retry, self.btn_cancel]:
            btn.setFixedHeight(28)
            btn.setStyleSheet(btn_style)
        self.btn_start.setStyleSheet("""
            QPushButton {
                background: rgba(0,206,201,0.3);
                border: 1px solid rgba(0,206,201,0.5);
                border-radius: 4px; color: white;
                padding: 0 8px; font-size: 10px; font-weight: bold;
            }
            QPushButton:hover { background: rgba(0,206,201,0.5); }
            QPushButton:disabled { color: rgba(255,255,255,40); background: rgba(0,206,201,0.1); }
        """)

        self.btn_save.setStyleSheet("""
            QPushButton {
                background: rgba(9,132,227,0.3);
                border: 1px solid rgba(9,132,227,0.5);
                border-radius: 4px; color: white;
                padding: 0 8px; font-size: 10px; font-weight: bold;
            }
            QPushButton:hover { background: rgba(9,132,227,0.5); }
            QPushButton:disabled { color: rgba(255,255,255,40); background: rgba(9,132,227,0.1); }
        """)

        self.btn_start.clicked.connect(self._on_start)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_retry.clicked.connect(self._on_retry)
        self.btn_cancel.clicked.connect(self._on_cancel)

        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_retry)
        btn_row.addWidget(self.btn_cancel)
        c_layout.addLayout(btn_row)

        # Aplica la fase inicial (oculta/muestra widgets según corresponda).
        self._apply_phase()

        layout.addWidget(self.container)

        # Timer
        self._elapsed = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

        # Nota: evitamos el raise_() periódico. En Windows, invocar raise()
        # sobre una ventana WindowStaysOnTopHint está bien, pero si además
        # mezclamos activateWindow() antes, seguimos peleándonos con Chrome.
        # Dejamos el StaysOnTop a Windows y listo.
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._force_on_top)
        self._raise_timer.start(500)

        # Position top-right (more visible than bottom-right)
        self._position_on_screen()
        self.resize(360, 280)

    def _divider(self):
        d = QFrame()
        d.setFixedHeight(1)
        d.setStyleSheet("background: rgba(255,255,255,15); border: none;")
        return d

    def _position_on_screen(self):
        try:
            from PyQt6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen().geometry()
            self.move(screen.width() - 380, 40)  # Top-right corner
        except:
            self.move(100, 40)

    def _force_on_top(self):
        """Windows: sólo fuerza raise() si el overlay se ocultó detrás.
        Nunca llama activateWindow() — eso robaría foco a la app destino."""
        try:
            if self.isVisible() and not self.isActiveWindow():
                self.raise_()
        except Exception:
            pass

    def _tick(self):
        self._elapsed += 1
        mins = self._elapsed // 60
        secs = self._elapsed % 60
        self.timer_lbl.setText(f"{mins:02d}:{secs:02d}")

    # ── Keyboard shortcuts ──

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            if self._save_enabled:
                self._on_save()
        elif key == Qt.Key.Key_Escape:
            self._on_cancel()
        elif key == Qt.Key.Key_F5:
            self._on_retry()
        elif (
            key == Qt.Key.Key_Z
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._emit_rollback()
        else:
            super().keyPressEvent(event)

    def _emit_rollback(self):
        log.info("RerecordOverlay: Undo (Ctrl+Z) solicitado")
        try:
            self.rollback_requested.emit()
        except Exception as e:
            log.exception(f"RerecordOverlay: emit rollback_requested error: {e}")

    def _on_save(self):
        log.info("RerecordOverlay: Guardar solicitado")
        # No marcamos _emitted_terminal aquí: el caller decide si
        # cierra el overlay (save exitoso) o lo deja vivo (validación
        # falló y el handler invoca retry).
        try:
            self.save_requested.emit()
        except Exception as e:
            log.exception(f"RerecordOverlay: emit save_requested error: {e}")

    def _on_retry(self):
        log.info("RerecordOverlay: Repetir solicitado")
        try:
            self.retry_requested.emit()
        except Exception as e:
            log.exception(f"RerecordOverlay: emit retry_requested error: {e}")

    def _on_cancel(self):
        log.info("RerecordOverlay: Cancelar solicitado")
        # Marcamos terminal antes de emitir para que ``closeEvent``
        # (que el caller dispara como parte del cleanup) no re-emita.
        self._emitted_terminal = True
        try:
            self.cancel_requested.emit()
        except Exception as e:
            log.exception(f"RerecordOverlay: emit cancel_requested error: {e}")

    def _on_start(self):
        log.info("RerecordOverlay: Empezar replay solicitado")
        self.btn_start.setEnabled(False)
        self.start_replay_requested.emit()

    # ── Fase replay vs captura (PRD 2026-05-08c §C) ─────────────

    @property
    def phase(self) -> str:
        return self._phase

    def _apply_phase(self) -> None:
        """Renderiza el overlay según la fase actual."""
        if self._phase == PHASE_REPLAY:
            self.replay_progress_lbl.show()
            base = (
                f"Voy a ejecutar {self._total_previous_steps} paso(s) "
                f"anterior(es) para llegar al paso a regrabar."
                if self._total_previous_steps
                else "Listo para regrabar el primer paso."
            )
            self.replay_progress_lbl.setText(base)
            self.events_lbl.setText(
                "Cuando estés listo, presiona Empezar."
            )
            self.btn_start.show()
            self.btn_start.setEnabled(True)
            self.btn_save.hide()
            self.btn_retry.hide()
            self.btn_cancel.show()
            self._save_enabled = False
            self.btn_save.setEnabled(False)
        else:  # PHASE_CAPTURE
            self.replay_progress_lbl.hide()
            self.btn_start.hide()
            self.btn_save.show()
            # PRD 2026-05-08d §C: el botón Guardar NUNCA se deshabilita
            # mientras el overlay está vivo. Si el usuario lo presiona
            # sin haber capturado nada, el handler muestra un
            # NevlanDialog informativo ("No hay nada que guardar").
            self.btn_save.setEnabled(True)
            self.btn_retry.show()
            self.btn_cancel.show()

    def update_replay_progress(
        self,
        idx: int,
        total: int,
        label: str = "",
    ) -> None:
        """Actualiza el indicador de progreso del replay (1/N…N-1/N).

        Llamable desde el thread del worker — la UI debe rutar a
        este método con ``QTimer.singleShot(0, ...)`` para garantizar
        que se ejecuta en el hilo de Qt.
        """
        if self._phase != PHASE_REPLAY:
            return
        if total > 0:
            text = f"Replay: {idx}/{total}"
            if label:
                text = f"{text} — {label}"
        else:
            text = label or "Preparando…"
        self.replay_progress_lbl.setText(text)

    def enter_capture_phase(self) -> None:
        """Cambia el overlay al modo captura: habilita Save/Retry/Cancel
        y oculta progress de replay."""
        self._phase = PHASE_CAPTURE
        self._apply_phase()
        if self._is_pick:
            self.events_lbl.setText(
                "Llegué al paso a regrabar. Haz clic en el elemento correcto."
            )
        else:
            self.events_lbl.setText(
                "Llegué al paso a regrabar. Realiza la acción y presiona Guardar."
            )

    def set_queue_progress(self, idx: int, total: int) -> None:
        """PRD 2026-05-09 §F — Renderiza "Paso N de M · …" en el título
        del overlay cuando se está regrabando dentro de la cola
        "Reparar pasos débiles". Si ``total <= 0`` limpia el indicador.

        Llamable múltiples veces sin riesgo: solo actualiza el QLabel
        del título. No emite señales ni muta orchestrator.
        """
        if total <= 0:
            self._queue_progress = None
            try:
                self._title_lbl.setText(self._title_base)
            except Exception:
                pass
            return
        try:
            idx_safe = max(1, int(idx))
            total_safe = max(idx_safe, int(total))
        except Exception:
            return
        self._queue_progress = (idx_safe, total_safe)
        try:
            self._title_lbl.setText(
                f"📋 Paso {idx_safe} de {total_safe} · {self._title_base}"
            )
        except Exception:
            pass

    def mark_closing(self) -> None:
        """PRD 2026-05-09 §E — Marca el overlay como "el caller lo va
        a cerrar voluntariamente, no emitas cancel_requested".

        Lo usan los flujos Save / cleanup post-save / cancel del
        propio caller, para evitar que ``closeEvent`` re-emita
        ``cancel_requested`` y cause re-entrancia en el cleanup
        del MissionReviewDialog (bug causante del freeze observado
        en la cola "Reparar pasos débiles").
        """
        self._emitted_terminal = True
        # Detenemos los timers desde ya: aunque ``closeEvent`` también
        # los detiene, marcarlos aquí asegura que el ``_force_on_top``
        # no vuelva a competir por foco mientras el caller cierra.
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            self._raise_timer.stop()
        except Exception:
            pass

    def show_replay_failed(self, error_message: str = "") -> None:
        """Marca el overlay como "replay fallido" pero NO lo cierra
        (el caller decide si reintenta, regraba manual o cancela).
        """
        if self._phase != PHASE_REPLAY:
            return
        self.btn_start.setEnabled(True)
        self.btn_start.setText("🔁 Reintentar replay")
        msg = error_message or "El replay no llegó al paso."
        self.replay_progress_lbl.setText(f"⚠ {msg}")

    def _on_expert_changed(self, _state: int = 0):
        chk = getattr(self, "expert_chk", None)
        if chk is None:
            return
        self.expert_mode = chk.isChecked()

    def reset_for_retry(self):
        """Limpia la vista tras «Repetir» (F5): vuelve al estado inicial de captura."""
        # PRD 2026-05-08d §C — Save permanece clickeable; reseteamos
        # solo el hint interno + texto. La validación "sin captura"
        # vive en el handler.
        self._save_enabled = False
        self.btn_save.setEnabled(True)
        self.expert_lbl.hide()
        if hasattr(self, "_strat_lbl_ref"):
            self._strat_lbl_ref.setVisible(False)
        if self._is_pick:
            self.events_lbl.setText("Esperando tu clic en la ventana destino…")
            return
        self.events_lbl.setText(
            "Listo, repite este paso nuevamente."
        )

    def set_human_display(self, human_text: str, expert_lines: str = ""):
        """Actualiza texto humano y (opcional) trazas técnicas modo experto."""
        if self._is_pick:
            self.events_lbl.setText("Esperando tu clic en la ventana destino…")
            self.expert_lbl.hide()
            return

        self.events_lbl.setText(human_text or "…")
        if self.expert_mode and expert_lines and expert_lines.strip():
            self.expert_lbl.setText(expert_lines.strip())
            self.expert_lbl.show()
            self._strat_lbl_ref.setVisible(True)
        else:
            self.expert_lbl.hide()
            self._strat_lbl_ref.setVisible(False)

        if not self._save_enabled:
            self._save_enabled = True
            self.btn_save.setEnabled(True)

    def add_event(self, event_type: str, detail: str = ""):  # compat tests antiguos
        self.set_human_display(f"[legacy] {event_type} {detail}".strip(), "")

    def closeEvent(self, event):
        # PRD 2026-05-08d §C — si el overlay se cierra por una vía
        # que NO pasó por nuestros handlers (Alt+F4, kill desde
        # window manager, ``close()`` programático del padre),
        # emitimos ``cancel_requested`` para garantizar que el caller
        # libere recursos (worker, listeners, timers, orchestrator).
        # ``_emitted_terminal`` evita doble-emit cuando el cancel
        # vino de un click intencional.
        try:
            if not getattr(self, "_emitted_terminal", False):
                self._emitted_terminal = True
                try:
                    self.cancel_requested.emit()
                except Exception:
                    pass
        finally:
            try:
                self._timer.stop()
            except Exception:
                pass
            try:
                self._raise_timer.stop()
            except Exception:
                pass
        super().closeEvent(event)
