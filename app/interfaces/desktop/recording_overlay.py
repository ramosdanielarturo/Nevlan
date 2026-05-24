from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton, QInputDialog, QApplication
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QShortcut, QKeySequence
from app.core.logger import log

try:
    from app.runtime.bus import bus
except ImportError:
    bus = None

class ActionRecorderOverlay(QWidget):
    """
    Panel translúcido interactivo que muestra pasos y permite controlar la grabación (Stop, Pause, Anotar).
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Propiedades Frameless y Translúcidas (Overlay)
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # UI Layout
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(10, 10, 10, 10)
        self.layout.setSpacing(5)
        
        # Estilo del Contenedor
        self.container = QFrame()
        self.container.setStyleSheet("""
            QFrame {
                background-color: rgba(20, 20, 25, 230);
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 8px;
            }
        """)
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(15, 10, 15, 10)
        self.layout.addWidget(self.container)
        
        # Header Layout (Title + Timer)
        self.header_layout = QHBoxLayout()
        self.title = QLabel("🔴 Nevlan — Grabando proceso")
        self.title.setStyleSheet("color: #ff7675; font-weight: bold; font-family: 'Segoe UI', Arial; font-size: 12px;")
        
        self.timer_lbl = QLabel("00:00")
        self.timer_lbl.setStyleSheet("color: #b2bec3; font-family: 'Consolas', monospace; font-size: 12px;")
        
        self.header_layout.addWidget(self.title)
        self.header_layout.addStretch()
        self.header_layout.addWidget(self.timer_lbl)
        self.container_layout.addLayout(self.header_layout)
        
        # Toolbar de Control
        self.toolbar = QHBoxLayout()
        self.btn_pause = QPushButton("⏸️")
        self.btn_stop = QPushButton("⏹️")
        self.btn_annotate = QPushButton("📝 Anotar")
        
        for btn in [self.btn_pause, self.btn_stop, self.btn_annotate]:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: rgba(255, 255, 255, 15);
                    border: 1px solid rgba(255, 255, 255, 30);
                    border-radius: 4px;
                    color: white;
                    padding: 4px 8px;
                    font-size: 11px;
                }
                QPushButton:hover { background-color: rgba(255, 255, 255, 40); }
            """)
        
        # Conexiones
        self.btn_stop.clicked.connect(self._request_stop)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_annotate.clicked.connect(self._request_annotation)
        
        self.toolbar.addWidget(self.btn_pause)
        self.toolbar.addWidget(self.btn_stop)
        self.toolbar.addWidget(self.btn_annotate)
        self.container_layout.addLayout(self.toolbar)
        
        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: rgba(255, 255, 255, 30);")
        self.container_layout.addWidget(line)
        
        # Contenedor de Pasos (historial corto)
        self.step_labels = []
        self.MAX_STEPS = 5
        
        # Configurar posición inicial (Esquina inferior derecha por defecto)
        self.resize(320, 175)
        self.hide()
        
        # Timer Interno
        self._elapsed_sec = 0
        self._is_paused = False
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._update_timer)
        
        # Suscribirse al bus si está disponible para recibir 'mission.live_step'
        if bus:
            bus.subscribe("mission.live_step", self._on_live_step)

        # ── Hotkeys de grabación (BLOQUEADOR fix) ──────────────────────
        # El overlay es Tool + FramelessWindowHint, lo cual hace que NO
        # reciba foco — los QShortcut a nivel widget no se disparan. La
        # solución: shortcuts con ApplicationShortcut, así Qt los captura
        # cuando esta app está activa, sin importar qué widget tiene foco.
        # F5 / Esc / Espacio funcionan EN CUALQUIER VENTANA de Nevlan.
        def _make_app_shortcut(seq: str, fn):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(fn)
            return sc

        self._sc_stop = _make_app_shortcut("Esc", self._request_stop)
        self._sc_pause = _make_app_shortcut("Space", self._toggle_pause)
        self._sc_annotate = _make_app_shortcut("F2", self._request_annotation)
        # Enter durante grabación NO se intercepta — es input legítimo
        # del usuario (el recorder lo captura como evento). El fix
        # aplica a Mission Review / Quick Reinforcement, donde Enter
        # = Guardar.

    def _update_timer(self):
        if not self._is_paused:
            self._elapsed_sec += 1
            mins, secs = divmod(self._elapsed_sec, 60)
            self.timer_lbl.setText(f"{mins:02d}:{secs:02d}")
            
    def _request_stop(self):
        if bus:
            from app.contracts.events import SystemEvent
            bus.publish(SystemEvent(name="ui.action", payload={"command": "stop_recording"}))

    def _toggle_pause(self):
        self._is_paused = not self._is_paused
        self.btn_pause.setText("▶️" if self._is_paused else "⏸️")
        self.title.setText("⏸️ Pausado" if self._is_paused else "🔴 Grabando")
        if bus:
            from app.contracts.events import SystemEvent
            cmd = "pause_recording" if self._is_paused else "resume_recording"
            bus.publish(SystemEvent(name="ui.action", payload={"command": cmd}))

    def _request_annotation(self):
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Agregar Anotación", "Regla, condición o variable a aplicar en este paso:")
        if ok and text and bus:
            from app.contracts.events import SystemEvent
            bus.publish(SystemEvent(name="mission.annotate", payload={"text": text}))

    def show_overlay(self):
        """Muestra el overlay y lo posiciona en la pantalla principal."""
        self._clear_steps()
        self._elapsed_sec = 0
        self._is_paused = False
        self.btn_pause.setText("⏸️")
        self.title.setText("🔴 Grabando")
        self.timer_lbl.setText("00:00")
        self.tick_timer.start(1000)
        
        # Posicionar en la esquina inferior derecha por encima de la barra de tareas
        from PyQt6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen().availableGeometry()
        x = screen.width() - self.width() - 20
        y = screen.height() - self.height() - 20
        self.move(x, y)
        
        self.show()

    def hide_overlay(self):
        """Oculta el overlay al detener la grabación."""
        self.tick_timer.stop()
        self.hide()

    _QUALITY_COLORS = {
        "good": "#26de81",   # verde
        "medium": "#ffa500", # ámbar
        "weak": "#ff7675",   # rojo
    }

    def _on_live_step(self, event):
        """Callback del EventBus (Desde hilo de grabación). Necesita enrutarse al hilo principal UI."""
        payload = event.payload
        text = payload.get("text", "Paso desconocido")
        icon = payload.get("icon", "🔹")
        quality = payload.get("quality")  # "good" / "medium" / "weak" o None
        
        # Usamos QTimer para invocar la UI de forma segura desde el Hilo Princial
        QTimer.singleShot(0, lambda: self._add_step_ui(f"{icon} {text}", quality))

    def _add_step_ui(self, step_text: str, quality: str | None = None):
        """Añade una nueva etiqueta de paso al layout, rotando las viejas."""
        # Prefijar un punto coloreado para indicar calidad de captura.
        if quality in self._QUALITY_COLORS:
            dot_color = self._QUALITY_COLORS[quality]
            full_text = f'<span style="color:{dot_color}; font-size:14px;">●</span> {step_text}'
        else:
            full_text = step_text

        lbl = QLabel(full_text)
        lbl.setStyleSheet("color: white; font-family: 'Segoe UI', Arial; font-size: 13px;")
        lbl.setWordWrap(True)
        # Guardamos la calidad para el efecto de fade.
        lbl.setProperty("step_quality", quality)
        
        self.container_layout.addWidget(lbl)
        self.step_labels.append(lbl)
        
        if len(self.step_labels) > self.MAX_STEPS:
            oldest = self.step_labels.pop(0)
            self.container_layout.removeWidget(oldest)
            oldest.deleteLater()
            
        # Efecto de desvanecimiento para pasos anteriores
        for i, label in enumerate(self.step_labels):
            opacity = 0.4 + (0.6 * (i / max(1, len(self.step_labels)-1)))
            label.setStyleSheet(f"color: rgba(255, 255, 255, {opacity}); font-family: 'Segoe UI', Arial; font-size: 13px;")

    def _clear_steps(self):
        for lbl in self.step_labels:
            self.container_layout.removeWidget(lbl)
            lbl.deleteLater()
        self.step_labels.clear()
