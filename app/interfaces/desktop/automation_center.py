"""
Nevlan — Centro de Automatizaciones
------------------------------------
Vista principal full-screen para gestionar automatizaciones.
Barra superior + Sidebar + Panel central + Actividad reciente.
Búsqueda en vivo, Programar con opciones, Historial de ejecuciones.
"""
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple, Any
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QFrame, QLineEdit, QScrollArea,
                             QStackedWidget, QApplication, QSizePolicy, QDialog,
                             QComboBox, QSpinBox, QTimeEdit, QCheckBox, QMessageBox,
                             QGroupBox, QProgressBar, QMenu, QToolButton,
                             QPlainTextEdit)
from PyQt6.QtCore import Qt, QTimer, QTime, pyqtSignal, QEvent
from PyQt6.QtGui import QFont, QColor
from app.core.logger import log
from app.services.missions.repair_context import RepairBrief
# Mission Truth Gate: única fuente de pasos visibles en la UI normal.
# Los call-sites que mostraban mission.compiled_execution_graph /
# interpreted_steps directamente fueron migrados a estos helpers.
from app.services.missions.mission_truth_gate import (
    has_semantic_view as _truth_has_semantic_view,
    is_executable as _truth_is_executable,
    step_card_models as _truth_step_card_models,
    ui_blockers as _truth_ui_blockers,
    visible_step_count as _truth_visible_step_count,
)
from app.interfaces.desktop.mission_ui_status import (
    compute_mission_ui_execution_state,
    mission_ui_detail_status_line,
    mission_ui_list_icon,
    mission_ui_list_tooltip_suffix,
    mission_ui_should_offer_execute_and_test,
    mission_ui_shows_in_attention_filter,
)


# ── Design Tokens ──
class NevlanTheme:
    BG_MAIN = "#111118"
    BG_SIDEBAR = "#16161e"
    BG_CARD = "#1e1e2a"
    BG_CARD_HOVER = "#262636"
    BORDER = "rgba(255,255,255,0.08)"
    ACCENT = "#0984e3"
    ACCENT_HOVER = "#74b9ff"
    TEXT_PRIMARY = "#e8e8f0"
    TEXT_SECONDARY = "#8b8ba0"
    TEXT_MUTED = "#5a5a70"
    SUCCESS = "#00cec9"
    WARNING = "#ffa500"
    ERROR = "#ff6b6b"
    FONT = "'Segoe UI', 'Inter', system-ui, sans-serif"

    # Shared styles
    SCROLL_STYLE = """
        QScrollArea { border: none; background: transparent; }
        QScrollBar:vertical { background: transparent; width: 4px; }
        QScrollBar::handle:vertical { background: rgba(255,255,255,0.15); border-radius: 2px; min-height: 20px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    """


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers UI compartidos
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _SearchClearEventFilter(QWidget):
    """EventFilter para mantener el botón "✕" del buscador alineado +
    soporta atajo Esc para limpiarlo.

    PRD 2026-05-08c §C.1: la "X" debe funcionar con mouse Y teclado,
    sin afectar otros widgets. Reposiciona el botón en cada Resize y
    captura ``Escape`` cuando el lineedit tiene foco para vaciarlo.
    """

    def __init__(self, owner, *, on_resize):
        super().__init__(parent=owner)
        self._owner = owner
        self._on_resize = on_resize

    def eventFilter(self, obj, event):  # type: ignore[override]
        et = event.type()
        if et == QEvent.Type.Resize or et == QEvent.Type.Show:
            try:
                self._on_resize()
            except Exception:
                pass
        elif et == QEvent.Type.KeyPress:
            try:
                key = event.key()
                if key == Qt.Key.Key_Escape:
                    line = getattr(self._owner, "search_input", None)
                    if line is not None and line.text():
                        self._owner._on_clear_search()
                        return True
            except Exception:
                pass
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SCHEDULE DIALOG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _TriggerRow(QFrame):
    """Widget de un disparador (mode + parámetros + botón eliminar).

    El ScheduleDialog permite tener varios de estos apilados y guarda una
    lista de triggers. Cada row se configura independientemente.
    """

    DAY_NAMES = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
    DAY_LABELS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

    def __init__(self, initial: Optional[dict] = None, on_remove=None, parent=None):
        super().__init__(parent)
        self._on_remove = on_remove
        self.setStyleSheet(f"""
            QFrame {{
                background: rgba(255,255,255,0.03);
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 8px;
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)

        # Header: modo + botón eliminar
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            "Bajo demanda (manual)", "Diario", "Semanal",
            "Cada X minutos", "Por evento",
        ])
        self.mode_combo.setStyleSheet(f"""
            QComboBox {{
                background: {NevlanTheme.BG_CARD};
                color: {NevlanTheme.TEXT_PRIMARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 6px; padding: 6px 10px; font-size: 12px;
            }}
            QComboBox::drop-down {{ border: none; width: 22px; }}
            QComboBox QAbstractItemView {{
                background: {NevlanTheme.BG_CARD};
                color: {NevlanTheme.TEXT_PRIMARY};
                border: 1px solid {NevlanTheme.BORDER};
                selection-background-color: {NevlanTheme.ACCENT};
            }}
        """)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_change)
        hdr.addWidget(self.mode_combo, 1)

        self.btn_remove = QPushButton("✕")
        self.btn_remove.setFixedSize(26, 26)
        self.btn_remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_remove.setToolTip("Quitar este disparador")
        self.btn_remove.setStyleSheet(f"""
            QPushButton {{
                background: rgba(214,48,49,0.12);
                color: #ff7675;
                border: 1px solid rgba(214,48,49,0.3);
                border-radius: 6px; font-size: 12px; font-weight: 700;
            }}
            QPushButton:hover {{ background: rgba(214,48,49,0.22); }}
        """)
        self.btn_remove.clicked.connect(self._emit_remove)
        hdr.addWidget(self.btn_remove)
        root.addLayout(hdr)

        # Stack con los parámetros por modo
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent;")

        # 0 — Manual
        manual_page = QWidget()
        ml = QVBoxLayout(manual_page)
        ml.setContentsMargins(0, 2, 0, 0)
        lbl = QLabel("Se ejecutará solo cuando la lances manualmente.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px;")
        ml.addWidget(lbl)
        self.stack.addWidget(manual_page)

        # 1 — Diario
        daily_page = QWidget()
        dl = QVBoxLayout(daily_page)
        dl.setContentsMargins(0, 2, 0, 0)
        dl.setSpacing(6)
        dl.addWidget(self._sub_label("Hora de ejecución:"))
        self.daily_time = QTimeEdit()
        self.daily_time.setTime(QTime(9, 0))
        self.daily_time.setDisplayFormat("HH:mm")
        self.daily_time.setStyleSheet(self._input_style())
        dl.addWidget(self.daily_time)
        self.stack.addWidget(daily_page)

        # 2 — Semanal
        weekly_page = QWidget()
        wl = QVBoxLayout(weekly_page)
        wl.setContentsMargins(0, 2, 0, 0)
        wl.setSpacing(6)
        wl.addWidget(self._sub_label("Días:"))
        days_row = QHBoxLayout()
        days_row.setSpacing(2)
        self.day_checks: list[QCheckBox] = []
        for lbl_day in self.DAY_LABELS:
            cb = QCheckBox(lbl_day)
            cb.setStyleSheet(f"""
                QCheckBox {{ color: {NevlanTheme.TEXT_PRIMARY}; font-size: 10px; spacing: 2px; background: transparent; border: none; }}
                QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid {NevlanTheme.BORDER}; border-radius: 3px; background: {NevlanTheme.BG_CARD}; }}
                QCheckBox::indicator:checked {{ background: {NevlanTheme.ACCENT}; border-color: {NevlanTheme.ACCENT}; }}
            """)
            days_row.addWidget(cb)
            self.day_checks.append(cb)
        for i in range(5):
            self.day_checks[i].setChecked(True)
        wl.addLayout(days_row)
        wl.addWidget(self._sub_label("Hora:"))
        self.weekly_time = QTimeEdit()
        self.weekly_time.setTime(QTime(9, 0))
        self.weekly_time.setDisplayFormat("HH:mm")
        self.weekly_time.setStyleSheet(self._input_style())
        wl.addWidget(self.weekly_time)
        self.stack.addWidget(weekly_page)

        # 3 — Cada X minutos
        interval_page = QWidget()
        il = QVBoxLayout(interval_page)
        il.setContentsMargins(0, 2, 0, 0)
        il.setSpacing(6)
        il.addWidget(self._sub_label("Ejecutar cada:"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setValue(30)
        self.interval_spin.setSuffix(" min")
        self.interval_spin.setStyleSheet(self._input_style())
        il.addWidget(self.interval_spin)

        # "A partir de…" — define la hora-base desde la que se cuenta el
        # intervalo. Sin marcar: se toma el momento en que guardas. Marcado:
        # los disparos se alinean al reloj (p. ej. anchor 09:00 + cada
        # 10 min → 09:00, 09:10, 09:20 …), y la alineación se conserva al
        # reiniciar la app.
        anchor_row = QHBoxLayout()
        anchor_row.setContentsMargins(0, 4, 0, 0)
        anchor_row.setSpacing(6)
        self.interval_anchor_cb = QCheckBox("A partir de las")
        self.interval_anchor_cb.setStyleSheet(f"""
            QCheckBox {{
                color: {NevlanTheme.TEXT_SECONDARY};
                font-size: 10px;
                spacing: 5px;
                background: transparent; border: none;
            }}
            QCheckBox::indicator {{
                width: 13px; height: 13px;
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 3px;
                background: {NevlanTheme.BG_CARD};
            }}
            QCheckBox::indicator:checked {{
                background: {NevlanTheme.ACCENT};
                border-color: {NevlanTheme.ACCENT};
            }}
        """)
        anchor_row.addWidget(self.interval_anchor_cb)
        self.interval_anchor_time = QTimeEdit()
        self.interval_anchor_time.setTime(QTime(9, 0))
        self.interval_anchor_time.setDisplayFormat("HH:mm")
        self.interval_anchor_time.setEnabled(False)
        self.interval_anchor_time.setStyleSheet(self._input_style())
        anchor_row.addWidget(self.interval_anchor_time)
        anchor_row.addStretch()
        il.addLayout(anchor_row)

        self.interval_anchor_cb.toggled.connect(
            self.interval_anchor_time.setEnabled
        )

        anchor_hint = QLabel(
            "Si activas esta opción, los disparos se alinean al reloj\n"
            "(ej. 09:00, 09:10, 09:20…). Si no, se cuenta desde el\n"
            "momento en que guardas."
        )
        anchor_hint.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 9px;"
            " background: transparent; border: none;"
        )
        anchor_hint.setWordWrap(True)
        il.addWidget(anchor_hint)

        self.stack.addWidget(interval_page)

        # 4 — Por evento
        event_page = QWidget()
        el = QVBoxLayout(event_page)
        el.setContentsMargins(0, 2, 0, 0)
        el.setSpacing(6)
        el.addWidget(self._sub_label("Tipo de disparador:"))
        self.event_combo = QComboBox()
        self.event_combo.addItems([
            "Cuando se abre una aplicación",
            "Cuando aparece un archivo en una carpeta",
            "Cuando se conecta un dispositivo USB",
            "Comando de voz personalizado",
        ])
        self.event_combo.setStyleSheet(self.mode_combo.styleSheet())
        el.addWidget(self.event_combo)
        self.event_param = QLineEdit()
        self.event_param.setPlaceholderText("app / ruta / frase de voz…")
        self.event_param.setStyleSheet(self._input_style())
        el.addWidget(self.event_param)
        note = QLabel("💡 Los eventos se activarán próximamente.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {NevlanTheme.WARNING}; font-size: 9px;")
        el.addWidget(note)
        self.stack.addWidget(event_page)

        root.addWidget(self.stack)

        if initial:
            self._load_from(initial)

    def set_removable(self, removable: bool):
        self.btn_remove.setEnabled(removable)
        self.btn_remove.setVisible(removable)

    def _emit_remove(self):
        if self._on_remove:
            self._on_remove(self)

    def _sub_label(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setStyleSheet(
            f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 10px; "
            "background: transparent; border: none;"
        )
        return lb

    def _input_style(self) -> str:
        return (
            f"background: {NevlanTheme.BG_CARD};"
            f" color: {NevlanTheme.TEXT_PRIMARY};"
            f" border: 1px solid {NevlanTheme.BORDER};"
            " border-radius: 6px; padding: 4px 8px; font-size: 12px;"
        )

    def _on_mode_change(self, idx: int):
        self.stack.setCurrentIndex(idx)

    def _load_from(self, cfg: dict):
        modes = ["Bajo demanda (manual)", "Diario", "Semanal",
                 "Cada X minutos", "Por evento"]
        mode = cfg.get("mode") or modes[0]
        if mode in modes:
            self.mode_combo.setCurrentIndex(modes.index(mode))
        t = cfg.get("time") or "09:00"
        try:
            h, m = map(int, t.split(":"))
            self.daily_time.setTime(QTime(h, m))
            self.weekly_time.setTime(QTime(h, m))
        except Exception:
            pass
        days = set(cfg.get("days") or [])
        for i, name in enumerate(self.DAY_NAMES):
            self.day_checks[i].setChecked(name in days)
        try:
            self.interval_spin.setValue(int(cfg.get("interval_minutes") or 30))
        except Exception:
            pass
        anchor = cfg.get("anchor_time") or ""
        if anchor:
            try:
                ah, am = map(int, anchor.split(":"))
                self.interval_anchor_time.setTime(QTime(ah, am))
                self.interval_anchor_cb.setChecked(True)
                self.interval_anchor_time.setEnabled(True)
            except Exception:
                self.interval_anchor_cb.setChecked(False)
                self.interval_anchor_time.setEnabled(False)
        else:
            self.interval_anchor_cb.setChecked(False)
            self.interval_anchor_time.setEnabled(False)
        ev = cfg.get("event_type") or ""
        if ev:
            idx = self.event_combo.findText(ev)
            if idx >= 0:
                self.event_combo.setCurrentIndex(idx)
        self.event_param.setText(cfg.get("event_param") or "")

    def to_dict(self) -> dict:
        idx = self.mode_combo.currentIndex()
        d: dict = {"mode": self.mode_combo.currentText()}
        if idx == 1:  # Diario
            d["time"] = self.daily_time.time().toString("HH:mm")
        elif idx == 2:  # Semanal
            d["time"] = self.weekly_time.time().toString("HH:mm")
            d["days"] = [
                self.DAY_NAMES[i] for i, cb in enumerate(self.day_checks)
                if cb.isChecked()
            ]
        elif idx == 3:  # Interval
            d["interval_minutes"] = int(self.interval_spin.value())
            if self.interval_anchor_cb.isChecked():
                d["anchor_time"] = self.interval_anchor_time.time().toString("HH:mm")
            else:
                d["anchor_time"] = None
        elif idx == 4:  # Evento
            d["event_type"] = self.event_combo.currentText()
            d["event_param"] = self.event_param.text()
        return d


class ScheduleDialog(QDialog):
    """Diálogo de programación — admite MÚLTIPLES disparadores por misión.

    La misión se ejecutará cuando CUALQUIER disparador se dispare. Esto
    permite, p.ej., programar "todos los lunes a las 9" + "todos los jueves
    a las 17" en la misma automatización.
    """

    def __init__(self, mission_name: str, mission_id: str = "", parent=None):
        super().__init__(parent)
        self.mission_id = mission_id
        self.mission_name = mission_name
        self.setWindowTitle(f"Programar — {mission_name}")
        self.setMinimumSize(480, 520)
        self.resize(520, 600)
        self.setStyleSheet(f"""
            QDialog {{ background: {NevlanTheme.BG_MAIN}; }}
            * {{ font-family: {NevlanTheme.FONT}; }}
            QLabel {{ color: {NevlanTheme.TEXT_PRIMARY}; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        title = QLabel("📅 Programar ejecución")
        title.setStyleSheet(
            f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 16px; font-weight: 700;"
        )
        root.addWidget(title)
        sub = QLabel(f"Automatización: {mission_name}")
        sub.setStyleSheet(f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 12px;")
        root.addWidget(sub)
        root.addWidget(self._divider())

        # Hint
        hint = QLabel(
            "Puedes añadir varios disparadores. La automatización se ejecutará "
            "cuando CUALQUIERA se cumpla."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {NevlanTheme.TEXT_MUTED}; font-size: 11px;")
        root.addWidget(hint)

        # Scroll con la lista de triggers
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(NevlanTheme.SCROLL_STYLE)
        self._rows_host = QWidget()
        self._rows_host.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(8)
        self._rows_layout.addStretch()
        scroll.setWidget(self._rows_host)
        root.addWidget(scroll, 1)

        self._rows: list[_TriggerRow] = []

        # Cargar triggers existentes (si hay)
        self._load_existing()
        if not self._rows:
            self._add_row()

        # Botón añadir
        add_btn = QPushButton("＋  Añadir disparador")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setFixedHeight(32)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(9,132,227,0.1);
                color: {NevlanTheme.ACCENT};
                border: 1px dashed rgba(9,132,227,0.4);
                border-radius: 6px; font-size: 12px; font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(9,132,227,0.18); }}
        """)
        add_btn.clicked.connect(lambda: self._add_row())
        root.addWidget(add_btn)

        root.addWidget(self._divider())

        # Botones inferiores
        btn_row = QHBoxLayout()
        btn_clear = QPushButton("🗑  Quitar programación")
        btn_clear.setFixedHeight(34)
        btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear.setStyleSheet(f"""
            QPushButton {{
                background: rgba(214,48,49,0.14);
                color: #ff7675;
                border: 1px solid rgba(214,48,49,0.35);
                border-radius: 6px; padding: 0 14px; font-size: 11px; font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(214,48,49,0.28); }}
        """)
        btn_clear.clicked.connect(self._clear_all)
        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancelar")
        btn_cancel.setFixedHeight(34)
        btn_cancel.setStyleSheet(f"""
            QPushButton {{
                background: {NevlanTheme.BG_CARD};
                color: {NevlanTheme.TEXT_PRIMARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 6px; padding: 0 18px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {NevlanTheme.BG_CARD_HOVER}; }}
        """)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_save = QPushButton("💾 Guardar programación")
        btn_save.setFixedHeight(34)
        btn_save.setStyleSheet(f"""
            QPushButton {{
                background: {NevlanTheme.ACCENT}; color: white;
                border: none; border-radius: 6px; padding: 0 18px;
                font-size: 12px; font-weight: 600;
            }}
            QPushButton:hover {{ background: {NevlanTheme.ACCENT_HOVER}; color: #111; }}
        """)
        btn_save.clicked.connect(self._save)
        btn_row.addWidget(btn_save)
        root.addLayout(btn_row)

    def _divider(self):
        d = QFrame()
        d.setFixedHeight(1)
        d.setStyleSheet(f"background: {NevlanTheme.BORDER};")
        return d

    def _load_existing(self):
        """Si la misión ya tiene schedule, pre-cargamos sus triggers."""
        try:
            from app.services.missions.scheduler import scheduler
            entry = scheduler.get_schedule(self.mission_id)
            if not entry:
                return
            for t in entry.triggers:
                self._add_row(t.to_dict())
        except Exception as e:
            log.debug(f"load_existing schedule: {e}")

    def _add_row(self, initial: Optional[dict] = None):
        row = _TriggerRow(initial=initial, on_remove=self._remove_row, parent=self._rows_host)
        # Insert before stretch
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._rows.append(row)
        self._refresh_removable()

    def _remove_row(self, row: _TriggerRow):
        if row in self._rows:
            self._rows.remove(row)
            row.setParent(None)
            row.deleteLater()
        self._refresh_removable()

    def _refresh_removable(self):
        # Un único disparador no debe poder eliminarse (usa "Quitar programación").
        for r in self._rows:
            r.set_removable(len(self._rows) > 1)

    def _clear_all(self):
        """Elimina por completo la programación de la misión."""
        try:
            from app.services.missions.scheduler import scheduler
            existed = scheduler.remove_schedule(self.mission_id)
            if existed:
                QMessageBox.information(
                    self, "Programación eliminada",
                    f"'{self.mission_name}' ya no tiene programación activa."
                )
            self.accept()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"No se pudo eliminar: {e}")

    def _save(self):
        triggers = [r.to_dict() for r in self._rows]
        # Si el usuario dejó todo en "Bajo demanda", equivale a "sin programar".
        non_manual = [t for t in triggers if t.get("mode") != "Bajo demanda (manual)"]
        try:
            from app.services.missions.scheduler import scheduler
            if not non_manual:
                # Todo manual → quitamos del scheduler para no mostrar en "Próximas"
                scheduler.remove_schedule(self.mission_id)
                QMessageBox.information(
                    self, "Sin disparadores activos",
                    "Solo quedó 'Bajo demanda'. La misión se ejecutará al lanzarla manualmente."
                )
                self.accept()
                return

            scheduler.set_schedule(
                self.mission_id, self.mission_name,
                {"enabled": True, "triggers": triggers},
            )
            # Resumen legible
            summary = "\n".join(
                f"• {t.get('mode')}" + (
                    f" — {t.get('time','')}"
                    if t.get('mode') in ('Diario', 'Semanal') else ""
                ) + (
                    f" ({', '.join(t.get('days', []))})"
                    if t.get('mode') == 'Semanal' else ""
                ) + (
                    f" — cada {t.get('interval_minutes')} min"
                    if t.get('mode') == 'Cada X minutos' else ""
                )
                for t in non_manual
            )
            QMessageBox.information(
                self, "✅ Programación activada",
                f"{len(non_manual)} disparador(es) activos:\n\n{summary}"
            )
        except Exception as e:
            log.error(f"Error guardando programación: {e}")
            QMessageBox.warning(self, "Error", f"No se pudo guardar: {e}")
        self.accept()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HISTORY DIALOG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HistoryDialog(QDialog):
    """Historial de ejecuciones de una automatización."""

    def __init__(self, mission, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Historial — {mission.name}")
        self.setFixedSize(500, 450)
        self.setStyleSheet(f"""
            QDialog {{ background: {NevlanTheme.BG_MAIN}; }}
            * {{ font-family: {NevlanTheme.FONT}; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # Title
        title = QLabel(f"📊 Historial de ejecuciones")
        title.setStyleSheet(f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 16px; font-weight: 700;")
        layout.addWidget(title)

        sub = QLabel(f"Automatización: {mission.name}")
        sub.setStyleSheet(f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 12px;")
        layout.addWidget(sub)

        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background: {NevlanTheme.BORDER};")
        layout.addWidget(div)

        # Scroll area for execution entries
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(NevlanTheme.SCROLL_STYLE)

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)

        # Load execution history
        executions = self._load_executions(mission)

        if not executions:
            empty = QLabel("Sin ejecuciones registradas para esta automatización.\n\nEjecuta la automatización para ver el historial aquí.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {NevlanTheme.TEXT_MUTED}; font-size: 12px; padding: 24px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(empty)
        else:
            for ex in executions:
                cl.addWidget(self._build_exec_entry(ex))

        cl.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        # Close button
        btn_close = QPushButton("Cerrar")
        btn_close.setFixedHeight(34)
        btn_close.setStyleSheet(f"""
            QPushButton {{ background: {NevlanTheme.BG_CARD}; color: {NevlanTheme.TEXT_PRIMARY}; border: 1px solid {NevlanTheme.BORDER}; border-radius: 6px; padding: 0 24px; font-size: 12px; }}
            QPushButton:hover {{ background: {NevlanTheme.BG_CARD_HOVER}; }}
        """)
        btn_close.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _load_executions(self, mission):
        """Carga ejecuciones desde el ExecutionTracker."""
        try:
            from app.services.missions.execution_tracker import tracker
            return tracker.get_for_mission(mission.id)
        except:
            return []

    def _build_exec_entry(self, ex):
        # PRD §C: el historial muestra la etiqueta lifecycle ("Ejecutado",
        # "Falló", "No iniciado", "Cancelado", "Sin respuesta",
        # "En ejecución") en lugar del status legacy libre. Eso garantiza
        # que jamás veamos "Ejecutado" sin trace real.
        entry = QFrame()
        is_success = bool(getattr(ex, "is_executed", False))
        label = getattr(ex, "history_label", "") or ex.status

        border_color = "rgba(0,206,201,0.3)" if is_success else "rgba(255,107,107,0.3)"
        entry.setStyleSheet(f"""
            QFrame {{ background: rgba(255,255,255,0.02); border: 1px solid {border_color}; border-radius: 8px; }}
        """)
        el = QVBoxLayout(entry)
        el.setContentsMargins(14, 10, 14, 10)
        el.setSpacing(4)

        icon = ex.icon
        time_str = ex.started_at[:16].replace('T', ' ') if ex.started_at else '—'

        row1 = QLabel(f"{icon}  {label}  ·  {time_str}")
        row1.setStyleSheet(f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 12px; font-weight: 500; border: none; background: transparent;")
        el.addWidget(row1)

        steps_total = max(0, int(getattr(ex, "total_steps", 0) or 0))
        steps_done = max(0, int(getattr(ex, "steps_executed", 0) or 0))
        row2 = QLabel(f"Pasos: {steps_done}/{steps_total} · {ex.trigger}")
        row2.setStyleSheet(f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px; border: none; background: transparent;")
        el.addWidget(row2)

        # Mostrar error_code cuando exista (PRD §D).
        err_code = str(getattr(ex, "error_code", "") or "")
        err_msg = str(getattr(ex, "error", "") or "")
        if err_code or err_msg:
            parts = []
            if err_code:
                parts.append(err_code)
            if err_msg:
                parts.append(err_msg)
            err_text = " · ".join(parts)
            err_lbl = QLabel(f"⚠️ {err_text[:300]}")
            err_lbl.setWordWrap(True)
            err_lbl.setStyleSheet(f"color: {NevlanTheme.ERROR}; font-size: 10px; border: none; background: transparent;")
            el.addWidget(err_lbl)

        return entry


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AUTOMATION CENTER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FLOATING RECORDING CONTROL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RecordingControlWidget(QWidget):
    """Botón flotante pequeño visible durante la grabación."""

    def __init__(self, parent_center=None, expected_mission_id: str = ""):
        super().__init__(None)
        self.parent_center = parent_center
        self.expected_mission_id = expected_mission_id or ""
        self._cleaned = False
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        container = QFrame()
        container.setStyleSheet(f"""
            QFrame {{
                background: rgba(17, 17, 24, 240);
                border: 1px solid rgba(255, 80, 80, 0.4);
                border-radius: 10px;
            }}
        """)
        cl = QVBoxLayout(container)
        cl.setContentsMargins(14, 10, 14, 10)
        cl.setSpacing(6)

        # Header
        header = QHBoxLayout()
        dot = QLabel("🔴")
        dot.setStyleSheet("font-size: 12px; border: none; background: transparent;")
        title = QLabel("Grabando proceso")
        title.setStyleSheet(f"color: {NevlanTheme.ERROR}; font-size: 11px; font-weight: 600; border: none; background: transparent;")
        self.timer_lbl = QLabel("00:00")
        self.timer_lbl.setStyleSheet(f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px; font-family: 'Consolas'; border: none; background: transparent;")
        header.addWidget(dot)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.timer_lbl)
        cl.addLayout(header)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        btn_style = f"""
            QPushButton {{
                background: rgba(255,255,255,0.08); color: {{fg}};
                border: 1px solid {{border}}; border-radius: 5px;
                padding: 5px 10px; font-size: 10px; font-weight: 600;
            }}
            QPushButton:hover {{ background: {{hover}}; }}
        """

        self.btn_stop = QPushButton("⏹ Finalizar")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.setStyleSheet(f"""
            QPushButton {{ background: rgba(39,174,96,0.2); color: #2ecc71; border: 1px solid rgba(39,174,96,0.3); border-radius: 5px; padding: 5px 10px; font-size: 10px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(39,174,96,0.4); }}
        """)
        self.btn_stop.clicked.connect(self._stop_recording)
        btn_row.addWidget(self.btn_stop)

        self.btn_restart = QPushButton("🔄 Reiniciar")
        self.btn_restart.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_restart.setStyleSheet(f"""
            QPushButton {{ background: rgba(255,165,0,0.15); color: #ffa500; border: 1px solid rgba(255,165,0,0.2); border-radius: 5px; padding: 5px 10px; font-size: 10px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,165,0,0.3); }}
        """)
        self.btn_restart.clicked.connect(self._restart_recording)
        btn_row.addWidget(self.btn_restart)

        self.btn_cancel = QPushButton("✕ Cancelar")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setStyleSheet(f"""
            QPushButton {{ background: rgba(255,0,0,0.1); color: #ff7675; border: 1px solid rgba(255,0,0,0.15); border-radius: 5px; padding: 5px 10px; font-size: 10px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,0,0,0.25); }}
        """)
        self.btn_cancel.clicked.connect(self._cancel_recording)
        btn_row.addWidget(self.btn_cancel)

        cl.addLayout(btn_row)
        layout.addWidget(container)

        self.resize(280, 78)
        self._position_bottom_right()

        # Timer
        self._elapsed = 0
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_timer)
        self._tick.start(1000)

        # Mantener visible sobre otras ventanas (Centro puede ser la única ventana de la app)
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._raise_keep_on_top)
        self._raise_timer.start(2000)

        # Subscribe to bus for recording done (solo si coincide el id de esta sesión)
        try:
            from app.runtime.bus import bus
            bus.subscribe("mission.review_requested", self._on_recording_done)
        except Exception:
            pass

    def _position_bottom_right(self):
        try:
            from PyQt6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen().availableGeometry()
            x = screen.width() - self.width() - 16
            y = screen.height() - self.height() - 16
            self.move(x, y)
        except Exception:
            pass

    def _raise_keep_on_top(self):
        if self.isVisible():
            self.raise_()

    def _update_timer(self):
        self._elapsed += 1
        m, s = divmod(self._elapsed, 60)
        self.timer_lbl.setText(f"{m:02d}:{s:02d}")

    def _stop_recording(self):
        """Finaliza compilando y guardando (no depende de la pill / bus ui.action)."""
        try:
            from app.services.missions.recorder import stop_and_compile
            stop_and_compile()
        except Exception as e:
            log.error(f"Error deteniendo grabación: {e}")
            try:
                QMessageBox.warning(
                    self, "Grabación",
                    f"No se pudo finalizar la grabación:\n{e}",
                )
            except Exception:
                pass
            return
        # mission.review_requested disparará _on_recording_done → _cleanup; si el bus falla, cerrar igual
        QTimer.singleShot(400, self._cleanup_if_still_open)

    def _cleanup_if_still_open(self):
        if not self._cleaned and self.isVisible():
            self._cleanup()

    def _restart_recording(self):
        try:
            from app.services.missions.recorder import stop_recording, start_recording
            stop_recording()
            import time
            time.sleep(0.3)
            m = start_recording("")
            self.expected_mission_id = m.id
            if self.parent_center:
                self.parent_center._pending_recording_mid = m.id
            self._elapsed = 0
        except Exception as e:
            log.error(f"Error reiniciando: {e}")

    def _cancel_recording(self):
        try:
            from app.services.missions.recorder import stop_recording
            stop_recording()
        except Exception:
            pass
        if self.parent_center:
            self.parent_center._pending_recording_mid = None
        self._cleanup()

    def _on_recording_done(self, event):
        """Cierre del widget si el evento corresponde a esta grabación."""
        mid = (event.payload or {}).get("mission_id") if event.payload else None
        if self.expected_mission_id and mid and mid != self.expected_mission_id:
            return
        QTimer.singleShot(300, self._cleanup)

    def shutdown_synchronously(self):
        """Cierra el panel flotante sin esperar timers: desuscribe del bus y
        detiene timers para evitar handlers duplicados al iniciar una segunda
        grabación (provocaba cierres intermitentes).
        """
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self._tick.stop()
        except Exception:
            pass
        if hasattr(self, "_raise_timer"):
            try:
                self._raise_timer.stop()
            except Exception:
                pass
        try:
            from app.runtime.bus import bus
            bus.unsubscribe("mission.review_requested", self._on_recording_done)
        except Exception:
            pass
        try:
            self.hide()
        except Exception:
            pass
        try:
            self.deleteLater()
        except Exception:
            pass

    def _cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        self._tick.stop()
        if hasattr(self, "_raise_timer"):
            self._raise_timer.stop()
        try:
            from app.runtime.bus import bus
            bus.unsubscribe("mission.review_requested", self._on_recording_done)
        except Exception:
            pass
        if self.parent_center:
            self.parent_center.status_lbl.setText("● Listo")
            self.parent_center.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 500;")
            # Cancelar o cerrar el panel: volver a la ventana principal (misma
            # lógica que al terminar con compilación; mission.review no se publica
            # en cancel puro, así que se restaura aquí).
            try:
                QTimer.singleShot(0, self.parent_center._ensure_automation_center_front_and_focus)
            except Exception:
                self.parent_center._ensure_automation_center_front_and_focus()
        self.close()
        self.deleteLater()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ExecutionControlWidget — panel flotante durante una ejecución
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ExecutionControlWidget(QWidget):
    """Panel flotante visible durante la ejecución de una automatización.

    Ofrece:
    - Pausa / Reanudar / Cancelar (cooperativos vía MissionPlayer).
    - Barra de progreso por pasos.
    - Indicador del paso actual y ETA aproximado.
    - Pequeño contador de tiempo transcurrido.
    - **PRD §B**: bindable a un :class:`ExecutionRun` para garantizar
      que la ventana cierra en todas las rutas terminales y que el
      heartbeat detecta cuelgues (sin terminal_signal del worker).

    Es deliberadamente compacto y se mantiene siempre visible
    (Tool + WindowStaysOnTopHint, sin robar foco).
    """

    # PRD §B: señal emitida cuando el panel pasa a un estado
    # terminal (success/failure/cancel/timeout/stuck). El payload
    # es ``ExecutionStatus.value``. La UI principal puede
    # conectarse para refrescar el historial **una vez** sin
    # depender de timers ad-hoc.
    lifecycle_terminal = pyqtSignal(str)

    def __init__(self, mission_name: str, total_steps: int,
                 player, parent_center=None):
        super().__init__(None)
        self.mission_name = mission_name or "automatización"
        self.total_steps = max(1, int(total_steps or 1))
        self.player = player  # MissionPlayer (acepta None para tests)
        self.parent_center = parent_center
        self._cleaned = False
        self._elapsed = 0
        self._is_paused = False
        self._step_idx = 0  # último step_index reportado
        self._step_started_at: Optional[float] = None
        self._avg_step_ms = 0.0  # media móvil simple
        # PRD §B — lifecycle binding
        self._run = None  # type: Optional["ExecutionRun"]
        self._terminal_emitted = False
        self._watchdog: Optional[QTimer] = None
        self._stuck_timeout_seconds: float = 90.0
        self._last_progress_seen_at: Optional[float] = None
        self._hold_open_for_smart_repair = False
        self._smart_repair_brief: Optional[RepairBrief] = None
        self._smart_repair_mission_ref: Optional[Any] = None

        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # No robar foco para que las teclas/clicks lleguen a la app objetivo.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        container = QFrame()
        container.setStyleSheet(f"""
            QFrame {{
                background: rgba(17, 17, 24, 240);
                border: 1px solid rgba(9, 132, 227, 0.45);
                border-radius: 12px;
            }}
        """)
        cl = QVBoxLayout(container)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)

        # ── Header: estado + tiempo
        header = QHBoxLayout()
        self.dot = QLabel("▶")
        self.dot.setStyleSheet("font-size: 13px; color: #74b9ff; border: none; background: transparent;")
        header.addWidget(self.dot)
        self.status_text = QLabel("Ejecutando")
        self.status_text.setStyleSheet(
            f"color: {NevlanTheme.ACCENT_HOVER}; font-size: 11px;"
            " font-weight: 700; border: none; background: transparent;"
            " letter-spacing: 0.3px;"
        )
        header.addWidget(self.status_text)
        header.addStretch()
        self.timer_lbl = QLabel("00:00")
        self.timer_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px;"
            " font-family: 'Consolas','Cascadia Mono',monospace;"
            " border: none; background: transparent;"
        )
        header.addWidget(self.timer_lbl)
        cl.addLayout(header)

        # ── Nombre de la misión (truncado)
        name = QLabel(self.mission_name)
        name.setStyleSheet(
            f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 12px; font-weight: 600;"
            " border: none; background: transparent;"
        )
        name.setMaximumWidth(280)
        # Trunca con elipsis si es muy largo.
        try:
            name.setTextFormat(Qt.TextFormat.PlainText)
        except Exception:
            pass
        cl.addWidget(name)

        # ── Barra de progreso por pasos
        self.progress = QProgressBar()
        self.progress.setRange(0, self.total_steps)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setStyleSheet(f"""
            QProgressBar {{
                background: rgba(255,255,255,0.08);
                border: none; border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {NevlanTheme.ACCENT}, stop:1 #74b9ff);
                border-radius: 3px;
            }}
        """)
        cl.addWidget(self.progress)

        # ── Texto de paso actual + ETA
        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.step_lbl = QLabel(f"Paso 0 / {self.total_steps}")
        self.step_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 10px;"
            " border: none; background: transparent;"
        )
        meta.addWidget(self.step_lbl)
        meta.addStretch()
        self.eta_lbl = QLabel("ETA —")
        self.eta_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px;"
            " border: none; background: transparent;"
        )
        meta.addWidget(self.eta_lbl)
        cl.addLayout(meta)

        self.detail_lbl = QLabel("")
        self.detail_lbl.setWordWrap(True)
        self.detail_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 9px;"
            " border: none; background: transparent;"
        )
        self.detail_lbl.hide()
        cl.addWidget(self.detail_lbl)

        self.group_lbl = QLabel("")
        self.group_lbl.setWordWrap(True)
        self.group_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 9px;"
            " border: none; background: transparent;"
        )
        self.group_lbl.hide()
        cl.addWidget(self.group_lbl)

        self.phase_lbl = QLabel("Listo para comenzar…")
        self.phase_lbl.setWordWrap(True)
        self.phase_lbl.setStyleSheet(
            f"color: {NevlanTheme.ACCENT_HOVER}; font-size: 10px; font-weight: 600;"
            " border: none; background: transparent;"
        )
        cl.addWidget(self.phase_lbl)

        self.fail_frame = QFrame()
        self.fail_frame.setStyleSheet(
            "background: rgba(214,48,49,0.12); border-radius:8px; border:none;"
        )
        ff = QVBoxLayout(self.fail_frame)
        ff.setContentsMargins(8, 6, 8, 6)
        self.fail_title = QLabel("")
        self.fail_title.setStyleSheet(
            "color:#ff7675;font-size:10px;font-weight:700;border:none;"
        )
        ff.addWidget(self.fail_title)
        self.fail_btns = QHBoxLayout()
        self.btn_retry_fail = QPushButton("Reintentar")
        self.btn_skip_step = QPushButton("Omitir paso")
        self.btn_manual_help = QPushButton("Ayúdame manualmente")
        self.btn_repair_step = QPushButton("Abrir reparación")
        self.btn_fail_dismiss = QPushButton("Cerrar panel")
        for b in (
            self.btn_retry_fail,
            self.btn_skip_step,
            self.btn_manual_help,
            self.btn_repair_step,
            self.btn_fail_dismiss,
        ):
            b.setStyleSheet(self._btn_style("#dfe6e9", 0.1, 0.24))
            self.fail_btns.addWidget(b)
        ff.addLayout(self.fail_btns)
        self.fail_frame.hide()
        cl.addWidget(self.fail_frame)

        # ── Botones
        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.btn_pause = QPushButton("⏸ Pausar")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.setStyleSheet(self._btn_style("#ffd166", 0.18, 0.32))
        self.btn_pause.clicked.connect(self._toggle_pause)
        btns.addWidget(self.btn_pause)

        self.btn_cancel = QPushButton("✕ Cancelar")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setStyleSheet(self._btn_style("#ff7675", 0.16, 0.30))
        self.btn_cancel.clicked.connect(self._cancel)
        btns.addWidget(self.btn_cancel)
        cl.addLayout(btns)

        outer.addWidget(container)

        self._failed_step_idx = -1
        self._failed_step_id = ""
        self._recovery_retry_blocked = False
        self._tracked_execution_id = (
            getattr(getattr(player, "execution", None), "id", "") if player else ""
        )
        self._expert_exec_lines = ""
        self.resize(360, 280)
        self._position_bottom_right()

        # Timer 1Hz: actualiza tiempo y ETA. Solo 1 hilo, costo despreciable.
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(1000)

        # Mantener visible
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._raise_keep_on_top)
        self._raise_timer.start(2000)

        # Suscripción al bus para recibir step_start/step_ok/replay_complete
        try:
            from app.runtime.bus import bus
            bus.subscribe("mission.step_start", self._on_step_start)
            bus.subscribe("mission.step_ok", self._on_step_ok)
            bus.subscribe("mission.step_failed", self._on_step_failed)
            bus.subscribe("mission.replay_complete", self._on_replay_complete)
            bus.subscribe("mission.replay_paused", self._on_replay_paused)
            bus.subscribe("mission.replay_resumed", self._on_replay_resumed)
            bus.subscribe("mission.replay_cancelled", self._on_replay_cancelled)
        except Exception as e:
            log.debug(f"ExecutionControlWidget bus subscribe: {e}")
        try:
            self._connect_fail_buttons()
        except Exception:
            pass

    @staticmethod
    def _btn_style(color: str, alpha_idle: float, alpha_hover: float) -> str:
        return f"""
            QPushButton {{
                background: rgba(255,255,255,0.08); color: {color};
                border: 1px solid rgba(255,255,255,0.12); border-radius: 6px;
                padding: 6px 12px; font-size: 11px; font-weight: 700;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,{alpha_hover}); }}
            QPushButton:disabled {{ color: rgba(255,255,255,0.30); }}
        """

    def _position_bottom_right(self):
        try:
            from PyQt6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen().availableGeometry()
            x = screen.width() - self.width() - 16
            y = screen.height() - self.height() - 16
            self.move(x, y)
        except Exception:
            pass

    def _raise_keep_on_top(self):
        if self.isVisible():
            try:
                self.raise_()
            except Exception:
                pass

    # ─── Bus handlers ────────────────────────────────────────────────
    def _on_step_start(self, ev):
        try:
            p = ev.payload or {}
            idx = int(p.get("step_index", self._step_idx))
            total = int(p.get("total_steps", self.total_steps))
            self.fail_frame.hide()
            try:
                self.btn_retry_fail.setEnabled(True)
                self._recovery_retry_blocked = False
            except Exception:
                pass
            self._step_idx = idx
            self.total_steps = max(1, total)
            self.progress.setRange(0, self.total_steps)
            self.progress.setValue(idx)
            self.step_lbl.setText(f"Paso {idx + 1} / {self.total_steps}")
            import time as _t
            self._step_started_at = _t.perf_counter()
            sd = str(p.get("step_description") or "").strip()
            if sd:
                self.detail_lbl.setText(sd[:400])
                self.detail_lbl.show()
            else:
                self.detail_lbl.hide()
            gt = str(p.get("group_title") or "").strip()
            if gt:
                self.group_lbl.setText(f"📂 {gt[:120]}")
                self.group_lbl.show()
            else:
                self.group_lbl.hide()
            ph = str(p.get("phase_hint") or "").strip()
            if ph:
                self.phase_lbl.setText(ph)
        except Exception:
            pass

    def _on_step_ok(self, ev):
        try:
            p = ev.payload or {}
            idx = int(p.get("step_index", self._step_idx)) + 1
            self.progress.setValue(idx)
            ms = float(p.get("ms") or 0.0)
            if ms > 0:
                # media móvil simple (suaviza outliers)
                if self._avg_step_ms <= 0:
                    self._avg_step_ms = ms
                else:
                    self._avg_step_ms = self._avg_step_ms * 0.7 + ms * 0.3
            nl = str(p.get("status_line_normal") or "").strip()
            xl = str(p.get("status_line_expert") or "").strip()
            self._expert_exec_lines = xl
            if nl:
                self.phase_lbl.setText(f"✓ {nl}")
            elif xl:
                self.phase_lbl.setText("✓ Completado")
        except Exception:
            pass

    def _on_step_failed(self, ev):
        try:
            p = ev.payload or {}
            self._failed_step_idx = int(p.get("step_index", -1))
            self._failed_step_id = str(p.get("step_id") or "")
            self._recovery_retry_blocked = bool(p.get("retry_blocked", False))
            try:
                self.btn_retry_fail.setEnabled(not self._recovery_retry_blocked)
                if self._recovery_retry_blocked:
                    br = str(p.get("blocked_reason") or "").strip()
                    if br:
                        self.fail_title.setText(
                            (self.fail_title.text() or "")
                            + "\n\n(No se permiten más reintentos automáticos aquí: "
                            + br + ".)"
                        )
            except Exception:
                pass
            err = str(p.get("error") or "").strip()
            sd = str(p.get("step_description") or "").strip()
            gt = str(p.get("group_title") or "").strip()
            pm = p.get("premium_click_meta") or {}
            txt = (
                "No encontré bien este elemento para hacer clic/actuar.\n\n"
                f"{err}"
            )
            if sd:
                txt += f"\n\nPasó en: «{sd[:120]}»"
            if gt:
                txt += f"\nGrupo: {gt}"
            self.fail_title.setText(txt[:900])
            ex = ""
            if isinstance(pm, dict) and pm:
                pairs = []
                for k in (
                    "strategy_used",
                    "region_used",
                    "image_used",
                    "fallback_used",
                    "visual_match_score",
                    "offset_applied",
                ):
                    if k in pm:
                        pairs.append(f"{k}={pm.get(k)}")
                ex = " · ".join(pairs[:10])
            if ex:
                self.phase_lbl.setText("Se requiere atención · " + ex[:220])
            self.fail_frame.show()
        except Exception:
            pass

    def _connect_fail_buttons(self):
        self.btn_retry_fail.clicked.connect(self._fail_retry_player)
        self.btn_skip_step.clicked.connect(self._fail_skip_confirmed)
        self.btn_manual_help.clicked.connect(self._fail_manual_flow)
        self.btn_repair_step.clicked.connect(self._fail_open_review)
        self.btn_fail_dismiss.clicked.connect(lambda: self.fail_frame.hide())

    def _fail_retry_player(self):
        if not self.player:
            return
        try:
            self.player.submit_recovery_command("retry")
            self.phase_lbl.setText("🔄 Reintentando el mismo paso…")
        except Exception as e:
            log.debug(f"_fail_retry_player: {e}")

    def _fail_skip_confirmed(self):
        if not self.player:
            return
        m = getattr(self.parent_center, "_selected_mission", None)
        step = None
        if m and getattr(self, "_failed_step_id", ""):
            for s in getattr(m, "compiled_execution_graph", []) or []:
                if s.id == self._failed_step_id:
                    step = s
                    break
        crit = "medium"
        try:
            if step is not None:
                from app.services.missions.step_criticality import infer_step_criticality

                crit = infer_step_criticality(step)
        except Exception:
            crit = "medium"

        extra = ""
        if crit == "high":
            extra = (
                "\n\nEste paso parece importante (validación o escritura sensible). "
                "Omitirlo puede hacer fallos difíciles de detectar después."
            )
        txt = (
            "Omitir este paso puede afectar a los siguientes (elementos ausentes,"
            " estados incompletos, etc.)." + extra + "\n\n¿Omitir y continuar?"
        )
        r = QMessageBox.warning(
            self,
            "Omitir paso",
            txt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            self.player.submit_recovery_command("skip")
            self.fail_frame.hide()
            self.phase_lbl.setText("⏭ Paso omitido — continuamos con el siguiente.")
        except Exception as e:
            log.debug(f"_fail_skip_confirmed: {e}")

    def _fail_manual_flow(self):
        if not self.player:
            return
        pc = self.parent_center
        desc = ""
        if pc and getattr(pc, "_selected_mission", None):
            interp = getattr(pc._selected_mission, "interpreted_steps", []) or []
            if 0 <= int(self._failed_step_idx) < len(interp):
                desc = (interp[int(self._failed_step_idx)].description or "").strip()
        QMessageBox.information(
            self,
            "Ayuda manual",
            "Haz este paso tú mismo en la aplicación destino "
            "(clic, formulario, lo que corresponda).\n\n"
            + (f"Paso: «{desc[:320]}»\n\n" if desc else "")
            + "Cuando hayas terminado, confirma en el siguiente diálogo.",
        )
        r = QMessageBox.question(
            self,
            "Continuar automatización",
            "¿Terminaste el paso manualmente y quieres que Nevlan continúe "
            "con el siguiente paso?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            self.player.submit_recovery_command("manual_continue")
            self.fail_frame.hide()
            self.phase_lbl.setText("🤝 Intervención manual registrada — continuamos.")
        except Exception as e:
            log.debug(f"_fail_manual_flow: {e}")

    def _fail_retry_info(self):
        try:
            QMessageBox.information(
                self,
                "Reintentar",
                "Vuelve a lanzar la automatización desde Nevlan. "
                "Si el fallo es persistente, usa «Abrir reparación».",
            )
        except Exception:
            pass

    def _fail_open_review(self):
        self.fail_frame.hide()
        pc = self.parent_center
        if pc is None:
            return
        try:
            from app.interfaces.desktop.mission_review import MissionReviewDialog
            pc._restore_main_interface()
            pc.activateWindow()
            m = getattr(pc, "_selected_mission", None)
            if not m:
                return
            d = MissionReviewDialog(m, pc)
            d.exec()
        except Exception as e:
            log.debug(f"_fail_open_review: {e}")

    def _on_replay_complete(self, ev):
        QTimer.singleShot(600, self._cleanup)

    def _on_replay_paused(self, ev):
        self._is_paused = True
        self.status_text.setText("Pausado")
        self.dot.setText("⏸")
        self.btn_pause.setText("▶ Reanudar")

    def _on_replay_resumed(self, ev):
        self._is_paused = False
        self.status_text.setText("Ejecutando")
        self.dot.setText("▶")
        self.btn_pause.setText("⏸ Pausar")

    def _on_replay_cancelled(self, ev):
        self.status_text.setText("Cancelado")
        QTimer.singleShot(600, self._cleanup)

    # ─── Lifecycle binding (PRD §B) ──────────────────────────────────
    def bind_lifecycle(
        self,
        run,
        *,
        stuck_timeout_seconds: float = 90.0,
    ) -> None:
        """Asocia este panel a un :class:`ExecutionRun` y enciende el
        watchdog. El watchdog comprueba cada 2s que ``run`` haya
        avanzado ``last_progress_at``; si pasa
        ``stuck_timeout_seconds`` sin progreso (y el run no es
        terminal), se llama a :meth:`run.mark_stuck` y se cierra el
        panel emitiendo ``lifecycle_terminal``.
        """
        self._run = run
        self._stuck_timeout_seconds = float(stuck_timeout_seconds or 90.0)
        try:
            import time as _t
            self._last_progress_seen_at = _t.monotonic()
        except Exception:
            self._last_progress_seen_at = None
        try:
            self._watchdog = QTimer(self)
            self._watchdog.timeout.connect(self._on_watchdog_tick)
            self._watchdog.start(2000)
        except Exception as e:
            log.debug(f"bind_lifecycle: no se pudo iniciar watchdog: {e}")

    def _on_watchdog_tick(self) -> None:
        run = self._run
        if run is None:
            return
        try:
            from app.services.missions.execution_lifecycle import (
                ExecutionStatus,
            )
            if run.is_terminal:
                if self.player is None and getattr(run, "repair_ui_hint", None):
                    return
                self._handle_run_terminal(run.status)
                return
            import time as _t
            now = _t.monotonic()
            seen = self._last_progress_seen_at or now
            # Si hay progreso reciente registrado en el run,
            # actualizar nuestra ancla local.
            if run.last_progress_at is not None:
                self._last_progress_seen_at = now
                return
            if (now - seen) >= self._stuck_timeout_seconds:
                run.mark_stuck()
                self._handle_run_terminal(ExecutionStatus.STUCK)
        except Exception as e:
            log.debug(f"_on_watchdog_tick: {e}")

    def _handle_run_terminal(self, status) -> None:
        """Refleja en UI un estado terminal y programa cierre.

        Garantiza que ``lifecycle_terminal`` se emite **una sola
        vez** por panel, incluso si llegan duplicados (worker +
        watchdog + cancel manual). PRD §B.
        """
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        try:
            from app.services.missions.execution_lifecycle import (
                ExecutionStatus,
            )
            value = (
                status.value if hasattr(status, "value")
                else str(status or "")
            )
            if value == ExecutionStatus.SUCCEEDED.value:
                self.status_text.setText("Completado")
                self.dot.setText("✓")
            elif value == ExecutionStatus.CANCELLED.value:
                self.status_text.setText("Cancelado")
                self.dot.setText("⏹")
            elif value in (
                ExecutionStatus.TIMED_OUT.value,
                ExecutionStatus.STUCK.value,
            ):
                self.status_text.setText("Sin respuesta")
                self.dot.setText("⏱")
            else:
                self.status_text.setText("Con errores")
                self.dot.setText("✕")
            try:
                self.lifecycle_terminal.emit(value)
            except Exception:
                pass
        except Exception:
            pass
        try:
            if getattr(self, "_hold_open_for_smart_repair", False):
                return
            QTimer.singleShot(900, self._cleanup)
        except Exception:
            self._cleanup()

    # ─── Timer ───────────────────────────────────────────────────────
    def _on_tick(self):
        if not self._is_paused:
            self._elapsed += 1
        m, s = divmod(self._elapsed, 60)
        self.timer_lbl.setText(f"{m:02d}:{s:02d}")
        try:
            import time as _time
            ss = self._step_started_at
            if (
                ss is not None
                and not self._is_paused
                and (_time.perf_counter() - ss) > 5.0
            ):
                cur = self.phase_lbl.text() or ""
                if "Buscando" not in cur and "Pausado" not in cur and "✓" not in cur:
                    self.phase_lbl.setText(
                        "Buscando elemento o ventana… puedes seguir usando el PC."
                    )
        except Exception:
            pass
        # ETA basado en pasos restantes * media de duración por paso
        try:
            remaining = max(0, self.total_steps - max(0, self._step_idx))
            if remaining > 0 and self._avg_step_ms > 0:
                eta_s = int((remaining * self._avg_step_ms) / 1000.0)
                em, es = divmod(eta_s, 60)
                self.eta_lbl.setText(f"ETA {em:02d}:{es:02d}")
            else:
                self.eta_lbl.setText("ETA —")
        except Exception:
            pass

    # ─── Acciones de usuario ─────────────────────────────────────────
    def _toggle_pause(self):
        if not self.player:
            return
        try:
            if self._is_paused:
                self.player.request_resume()
            else:
                self.player.request_pause()
        except Exception as e:
            log.debug(f"toggle_pause: {e}")

    def _cancel(self):
        self._hold_open_for_smart_repair = False
        # PRD §B: el botón cancelar es siempre funcional, incluso
        # cuando ``self.player is None`` (caso SmartExecutor sin
        # cooperative cancel). Mover el ``run`` al estado
        # ``cancelled`` ya cierra la ventana automáticamente vía
        # ``lifecycle_terminal``.
        run = self._run
        if run is not None and not run.is_terminal:
            try:
                run.mark_cancelled()
            except Exception as e:
                log.debug(f"_cancel: mark_cancelled falló: {e}")
            try:
                from app.services.missions.execution_lifecycle import (
                    ExecutionStatus,
                )
                self._handle_run_terminal(ExecutionStatus.CANCELLED)
            except Exception:
                self._cleanup()
        if self.player is None:
            if run is None:
                self._cleanup()
            return
        try:
            self.player.request_stop()
        except Exception as e:
            log.debug(f"cancel: {e}")

    # ─── Limpieza ────────────────────────────────────────────────────
    def shutdown_synchronously(self):
        if getattr(self, "_hold_open_for_smart_repair", False):
            return
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self._tick.stop()
        except Exception:
            pass
        try:
            self._raise_timer.stop()
        except Exception:
            pass
        try:
            if self._watchdog is not None:
                self._watchdog.stop()
                self._watchdog = None
        except Exception:
            pass
        try:
            from app.runtime.bus import bus
            bus.unsubscribe("mission.step_start", self._on_step_start)
            bus.unsubscribe("mission.step_ok", self._on_step_ok)
            bus.unsubscribe("mission.step_failed", self._on_step_failed)
            bus.unsubscribe("mission.replay_complete", self._on_replay_complete)
            bus.unsubscribe("mission.replay_paused", self._on_replay_paused)
            bus.unsubscribe("mission.replay_resumed", self._on_replay_resumed)
            bus.unsubscribe("mission.replay_cancelled", self._on_replay_cancelled)
        except Exception:
            pass
        try:
            self.hide()
        except Exception:
            pass
        try:
            self.deleteLater()
        except Exception:
            pass

    def _cleanup(self):
        if getattr(self, "_hold_open_for_smart_repair", False):
            return
        self.shutdown_synchronously()

    # ─── API pública para SmartMissionExecutor ───────────────────────
    # SmartMissionExecutor no publica eventos en el bus (usa callbacks
    # directos), así que necesita una vía explícita para actualizar el
    # panel y para marcar el final de la ejecución sin depender de
    # ``mission.replay_complete``.
    def mark_smart_step_started(
        self,
        *,
        step_index: int,
        total: int,
        phase_text: str = "",
    ) -> None:
        """PRD 2026-05-09 §exec-progress: refleja en UI que un step
        está a punto de ejecutarse (ANTES de que termine).

        Antes la ventana flotante quedaba en "Paso 0 / N" hasta que
        ``update_smart_progress`` se llamaba con el primer outcome
        finalizado. Para misiones con un step inicial lento (Chrome
        cargando, profile picker), eso parecía "ejecución colgada".
        Ahora el SmartExecutor llama a este método ANTES de cada
        step y la UI mueve el contador al instante.

        ``step_index`` es 1-based: el paso que va a ejecutarse a
        continuación.
        """
        try:
            total = max(1, int(total or self.total_steps))
            self.total_steps = total
            idx = max(1, min(int(step_index or 1), total))
            self.progress.setRange(0, total)
            # progress.setValue(idx-1) refleja "estoy EN el step idx,
            # pero todavía no terminé". Cuando termine, _ui_step_done
            # llamará a update_smart_progress con done=idx que pondrá
            # el value en idx (paso completado).
            self.progress.setValue(max(0, idx - 1))
            self._step_idx = max(0, idx - 1)
            self.step_lbl.setText(f"Paso {idx} / {total}")
            ph = (phase_text or "").strip()
            if ph:
                self.phase_lbl.setText(f"Ejecutando: {ph[:200]}")
            import time as _t
            self._step_started_at = _t.perf_counter()
        except Exception as e:
            log.debug(f"mark_smart_step_started: {e}")

    def update_smart_progress(
        self,
        *,
        step_index: int,
        total: int,
        phase_text: str = "",
        detail_text: str = "",
        group_text: str = "",
        elapsed_ms: float = 0.0,
    ) -> None:
        """Refleja el avance de un paso ejecutado por el SmartExecutor.

        ``step_index`` es 1-based (el paso que acaba de terminar).
        """
        try:
            total = max(1, int(total or self.total_steps))
            self.total_steps = total
            self.progress.setRange(0, self.total_steps)
            done = max(0, min(int(step_index or 0), self.total_steps))
            self.progress.setValue(done)
            self._step_idx = done
            shown = min(done + 1, self.total_steps)
            self.step_lbl.setText(f"Paso {shown} / {self.total_steps}")
            if elapsed_ms > 0:
                if self._avg_step_ms <= 0:
                    self._avg_step_ms = float(elapsed_ms)
                else:
                    self._avg_step_ms = (
                        self._avg_step_ms * 0.7 + float(elapsed_ms) * 0.3
                    )
            ph = (phase_text or "").strip()
            if ph:
                self.phase_lbl.setText(ph[:240])
            dt = (detail_text or "").strip()
            if dt:
                self.detail_lbl.setText(dt[:400])
                self.detail_lbl.show()
            gt = (group_text or "").strip()
            if gt:
                self.group_lbl.setText(f"📂 {gt[:120]}")
                self.group_lbl.show()
            import time as _t
            self._step_started_at = _t.perf_counter()
        except Exception as e:
            log.debug(f"update_smart_progress: {e}")

    def preempt_terminal_for_smart_repair(self, headline: str = "") -> None:
        """Cierra watchdog y marca terminal SIN autocleanup — el panel muestra la oferta."""
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        try:
            if self._watchdog is not None:
                try:
                    self._watchdog.stop()
                finally:
                    self._watchdog = None
        except Exception:
            pass
        try:
            from app.services.missions.execution_lifecycle import ExecutionStatus

            self.status_text.setText("Te guío")
            self.dot.setText("✦")
            if headline:
                self.phase_lbl.setText(headline[:420])
            self.lifecycle_terminal.emit(ExecutionStatus.FAILED.value)
        except Exception:
            pass

    def offer_smart_repair_offer(self, brief: RepairBrief, mission_ref: Any) -> None:
        """Conectado con Mission Review desde el Centro (misma política Nevlan)."""
        self._hold_open_for_smart_repair = True
        self._smart_repair_brief = brief
        self._smart_repair_mission_ref = mission_ref
        title = (brief.headline or "").strip()
        body = (brief.subtext or "").strip()
        msg = f"{title}\n\n{body}".strip()
        self.preempt_terminal_for_smart_repair(title)
        try:
            self.fail_title.setText(msg.strip()[:5000])
        except Exception:
            pass
        try:
            self.btn_skip_step.hide()
            self.btn_manual_help.hide()
            self.btn_retry_fail.clicked.disconnect()
        except Exception:
            pass
        try:
            self.btn_repair_step.clicked.disconnect()
        except Exception:
            pass
        try:
            self.btn_fail_dismiss.clicked.disconnect()
        except Exception:
            pass
        self.btn_retry_fail.setText("Cerrar panel")
        self.btn_retry_fail.show()
        self.btn_retry_fail.clicked.connect(self._smart_repair_dismiss)
        self.btn_repair_step.setText("Adaptar este paso…")
        self.btn_repair_step.show()
        self.btn_repair_step.clicked.connect(self._smart_repair_open_review)
        try:
            self.btn_fail_dismiss.hide()
        except Exception:
            pass
        try:
            self.fail_frame.show()
        except Exception:
            pass

    def _smart_repair_dismiss(self) -> None:
        self._hold_open_for_smart_repair = False
        try:
            self.fail_frame.hide()
        except Exception:
            pass
        try:
            self._cleanup()
        except Exception:
            pass

    def _smart_repair_open_review(self) -> None:
        pc = self.parent_center
        brief = self._smart_repair_brief
        snap = getattr(self, "_smart_repair_mission_ref", None)
        self._hold_open_for_smart_repair = False
        try:
            self.fail_frame.hide()
        except Exception:
            pass
        try:
            self.shutdown_synchronously()
        except Exception:
            pass
        if pc is None or brief is None or snap is None:
            return
        try:
            pc._open_runtime_repair_review(mission_snap=snap, brief=brief)
        except Exception:
            log.exception("runtime repair handoff desde panel flotante")

    def mark_smart_finished(
        self,
        *,
        status: str,
        message: str = "",
        auto_close_ms: int = 1500,
    ) -> None:
        """Marca la misión como terminada y programa el cierre del panel.

        ``status`` puede ser ``success``, ``stopped_for_human``,
        ``failed``, ``blocked``, ``error``, ``cancelled``, ``timed_out``
        o cualquier valor de :class:`ExecutionStatus`. PRD §B: el panel
        SIEMPRE cierra en una ruta terminal — ``auto_close_ms <= 0`` ya
        no significa "dejar abierto para siempre", sino "cerrar con
        delay default mínimo". Lo único que mantiene el panel abierto
        es un fallo legacy con UI accionable (handled by ``fail_frame``).
        """
        try:
            self.progress.setValue(self.total_steps)
            label = (message or "").strip()
            if status == "success" or status == "succeeded":
                self.status_text.setText("Completado")
                self.dot.setText("✓")
                self.phase_lbl.setText(
                    label or "✓ Misión completada correctamente."
                )
            elif status == "stopped_for_human":
                self.status_text.setText("Necesita ayuda")
                self.dot.setText("⚠")
                self.phase_lbl.setText(
                    label or "⚠ Necesito tu ayuda para continuar."
                )
            elif status == "blocked":
                self.status_text.setText("Bloqueada")
                self.dot.setText("■")
                self.phase_lbl.setText(
                    label or "■ Misión bloqueada antes de empezar."
                )
            elif status == "cancelled":
                self.status_text.setText("Cancelado")
                self.dot.setText("⏹")
                self.phase_lbl.setText(
                    label or "⏹ Cancelado por el usuario."
                )
            elif status in ("timed_out", "stuck"):
                self.status_text.setText("Sin respuesta")
                self.dot.setText("⏱")
                self.phase_lbl.setText(
                    label or "⏱ Sin respuesta del worker."
                )
            else:
                self.status_text.setText("Con errores")
                self.dot.setText("✕")
                self.phase_lbl.setText(
                    label or "✕ La misión terminó con errores."
                )
        except Exception as e:
            log.debug(f"mark_smart_finished: {e}")
        # PRD §B: emitir lifecycle_terminal siempre que status sea
        # un terminal canónico. Esto es defensivo — si bind_lifecycle
        # ya lo emitió antes, _handle_run_terminal lo idempotenta.
        try:
            from app.services.missions.execution_lifecycle import (
                ExecutionStatus,
            )
            terminal_value = None
            if status in ("success", "succeeded"):
                terminal_value = ExecutionStatus.SUCCEEDED.value
            elif status == "cancelled":
                terminal_value = ExecutionStatus.CANCELLED.value
            elif status == "timed_out":
                terminal_value = ExecutionStatus.TIMED_OUT.value
            elif status == "stuck":
                terminal_value = ExecutionStatus.STUCK.value
            elif status in ("failed", "error", "blocked", "stopped_for_human"):
                terminal_value = ExecutionStatus.FAILED.value
            if terminal_value is not None:
                self._handle_run_terminal(terminal_value)
        except Exception:
            pass
        if getattr(self, "_hold_open_for_smart_repair", False):
            return
        # Fallback de cierre (back-compat con tests legacy):
        if auto_close_ms and auto_close_ms > 0:
            try:
                QTimer.singleShot(int(auto_close_ms), self._cleanup)
            except Exception:
                self._cleanup()
        else:
            try:
                QTimer.singleShot(2500, self._cleanup)
            except Exception:
                self._cleanup()


class AutomationCenter(QMainWindow):
    """Centro de Automatizaciones — ventana principal de Nevlan."""

    _execution_done = pyqtSignal()
    # Marshal cross-thread del scheduler: (mission_id, mission_name,
    # total_steps, player). Emitido desde el hilo del scheduler y procesado
    # en el hilo de UI para abrir el panel flotante de ejecución.
    _scheduled_execution_started = pyqtSignal(str, str, int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nevlan — Centro de Automatizaciones")
        # Mínimo más laxo: el sistema operativo permite resize desde
        # los 4 bordes (decoración nativa). Mantener 800x520 deja
        # margen para pantallas pequeñas y portátiles 13".
        self.setMinimumSize(800, 520)
        # Tamaño por defecto grande, pero main.py llama showMaximized()
        # para que arranque ocupando la pantalla completa.
        self.resize(1440, 900)
        self.setStyleSheet(f"""
            QMainWindow {{ background: {NevlanTheme.BG_MAIN}; }}
            * {{ font-family: {NevlanTheme.FONT}; }}
        """)

        self._all_missions = []  # Cached for search
        self._sidebar_buttons = []  # For search filtering
        self._current_filter = "Todas"
        self._search_query = ""  # Query activo en la barra de búsqueda
        self._recording_widget = None  # Floating recording control
        self._execution_widget = None  # Floating execution control
        self._pending_recording_mid = None  # Misión recién grabada a enfocar al guardar
        self._last_run_mission_id = ""

        # Scheduler: guardamos la referencia ya, pero DIFERIMOS el start()
        # hasta que la ventana esté visible y el usuario vea el Centro de
        # Automatizaciones. Si una misión "Cada X minutos" estaba vencida,
        # arrancarla aquí bloqueaba la apertura de la app.
        try:
            from app.services.missions.scheduler import scheduler
            self._scheduler = scheduler
            QTimer.singleShot(1500, self._start_scheduler_deferred)
        except Exception as e:
            log.error(f"Error preparando scheduler: {e}")
            self._scheduler = None

        # Execution tracker
        try:
            from app.services.missions.execution_tracker import tracker
            self._tracker = tracker
        except:
            self._tracker = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ━━━ TOP BAR ━━━
        root.addWidget(self._build_topbar())

        # ━━━ BODY ━━━
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        body.addWidget(self._build_sidebar(), 0)
        body.addWidget(self._build_center_panel(), 1)
        body.addWidget(self._build_activity_panel(), 0)

        body_w = QWidget()
        body_w.setLayout(body)
        root.addWidget(body_w, 1)

        self._selected_mission = None
        self._center_on_screen()

        # Refresh adaptativo: 1Hz cuando hay una ejecución en menos de 60s,
        # 30s en otro caso. Evita saturar la UI cuando faltan horas, pero
        # permite ver el countdown segundo-a-segundo cerca del disparo.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_next_exec_adaptive)
        self._refresh_timer.start(30000)
        self._refresh_next_exec_adaptive()

        self._execution_done.connect(self._refresh_activity_panel)
        self._execution_done.connect(self._restore_after_execution)
        try:
            from app.runtime.bus import bus
            bus.subscribe("mission.review_requested", self._on_recording_mission_ready)
            bus.subscribe("mission.execution_finished", self._on_mission_execution_finished_bus)
        except Exception as e:
            log.debug(f"No se pudo suscribir a mission.review_requested: {e}")

    def _start_scheduler_deferred(self):
        """Arranca el scheduler una vez que el Centro de Automatizaciones
        es visible. Evita que una misión programada dispare antes de que
        el usuario vea la UI."""
        try:
            if self._scheduler is not None:
                # Conectamos la señal cross-thread antes de arrancar para
                # no perder el primer 'execution_started'.
                try:
                    self._scheduled_execution_started.connect(
                        self._on_scheduled_execution_started
                    )
                except Exception:
                    pass
                try:
                    self._scheduler.set_execution_start_callback(
                        self._emit_scheduled_execution_started
                    )
                except Exception as e:
                    log.debug(f"set_execution_start_callback: {e}")
                self._scheduler.start()
        except Exception as e:
            log.error(f"Error iniciando scheduler: {e}")

    def _emit_scheduled_execution_started(
        self, mission_id: str, mission_name: str, total_steps: int, player
    ):
        """Callback invocado desde el hilo del scheduler. Reemitimos vía
        pyqtSignal para pasar al hilo principal y mostrar el widget."""
        try:
            self._scheduled_execution_started.emit(
                str(mission_id or ""),
                str(mission_name or ""),
                int(total_steps or 0),
                player,
            )
        except Exception as e:
            log.debug(f"_emit_scheduled_execution_started: {e}")

    def _on_scheduled_execution_started(
        self, mission_id: str, mission_name: str, total_steps: int, player
    ):
        """Se ejecuta en el hilo de UI. Muestra el panel flotante de
        ejecución para una misión lanzada por el scheduler, igual que con
        el botón manual."""
        try:
            # Si ya hay un widget (p. ej. una ejecución manual solapada),
            # cerramos el anterior antes de abrir el nuevo.
            existing = getattr(self, "_execution_widget", None)
            if existing is not None:
                try:
                    existing.shutdown_synchronously()
                except Exception:
                    pass
                self._execution_widget = None
            self._show_execution_control(mission_name, total_steps, player)
            self._last_run_mission_id = str(mission_id or "")
            self.status_lbl.setText(f"▶️ Ejecutando '{mission_name}' (programada)")
            self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
            )
        except Exception as e:
            log.error(f"_on_scheduled_execution_started: {e}")

    # ─── Helpers de estado de ventana (maximizado persistente) ───
    def _minimize_preserving_state(self):
        """Minimiza recordando si estábamos maximizados para restaurar igual."""
        try:
            self._was_maximized = bool(self.isMaximized())
        except Exception:
            self._was_maximized = True  # valor por defecto seguro
        try:
            self.showMinimized()
        except Exception:
            pass

    def _restore_previous_state(self):
        """Restaura el estado previo a un minimize (maximizado o normal)."""
        try:
            if getattr(self, "_was_maximized", True):
                self.showMaximized()
            else:
                self.showNormal()
            self.raise_()
        except Exception:
            try:
                self.showNormal()
            except Exception:
                pass

    def _restore_main_interface(self):
        """Vuelve a mostrar el Centro de Automatizaciones al terminar (o
        cancelar) una grabación: durante la captura la ventana se minimizaba
        para ceder foco a la app objetivo."""
        try:
            if not self.isMinimized():
                return
        except Exception:
            pass
        try:
            self._restore_previous_state()
            self.activateWindow()
        except Exception:
            pass

    def _ensure_automation_center_front_and_focus(self) -> None:
        """Fuerza que la ventana principal de Nevlan sea visible y activa.
        Tras grabar, el usuario debe volver aquí aunque la ventana no estuviera minimizada."""
        try:
            if self.isMinimized():
                self._restore_previous_state()
        except Exception:
            pass
        try:
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def _on_mission_execution_finished_bus(self, event) -> None:
        """Resumen al cerrar replay (misión ejecutada desde el Centro)."""
        try:
            pl = getattr(event, "payload", None) or {}
            mid = pl.get("mission_id")
            if mid and getattr(self, "_last_run_mission_id", ""):
                if str(mid) != str(getattr(self, "_last_run_mission_id", "")):
                    return
            QTimer.singleShot(0, lambda p=dict(pl): self._show_run_summary_dialog(p))
        except Exception as e:
            log.debug(f"_on_mission_execution_finished_bus: {e}")

    def _show_run_summary_dialog(self, pl: dict) -> None:
        """Ventana corta de fin de ejecución (éxito, recuperaciones, cancel o fallo)."""
        try:
            self._restore_main_interface()
            self.raise_()
        except Exception:
            pass

        raw_st = str(pl.get("status") or "")
        st = raw_st.replace("_", " ")
        skips = int(pl.get("steps_skipped") or 0)
        manual = int(pl.get("steps_manual") or 0)
        recov = int(pl.get("steps_recovered_retry") or 0)
        steps_exec = int(pl.get("steps_executed") or 0)

        summary = ""
        try:
            es = str(pl.get("execution_summary") or "").strip()
            if es:
                summary = "\n\n" + es[:600]
        except Exception:
            pass

        reco = ""
        if raw_st in ("failed",):
            reco = (
                "\n\nRecomendado: revisar pasos marcados como débiles, reforzar con imagen/UI "
                "o lanzar desde el editor."
            )

        hints = ""
        if skips > 0 or manual > 0 or recov > 0:
            hints += (
                f"\n\nDetalle recuperación:"
                f"\n· Pasos ejecutados bien: {steps_exec}"
                f"\n· Recuperados por reintentar el mismo paso: {recov}"
                f"\n· Omitidos: {skips}"
                f"\n· Intervención manual: {manual}"
            )

        if raw_st == "cancelled":
            title = "Ejecución cancelada"
            txt = (
                "La automatización se detuvo porque se solicitó cancelar."
                + summary + hints
            )
        elif raw_st == "failed":
            title = "Ejecución con fallos"
            err = str(pl.get("error_details") or "").strip()
            txt = (
                ("No pudimos terminar todas las etapas."
                 + ("\nMotivo registrado:\n" + err[:850] + "\n" if err else ""))
                + reco + hints + summary
            )
        elif raw_st == "success_with_recovery":
            title = "Ejecución completada con recuperaciones"
            txt = (
                "Automatización terminada. Se encontraron recuperaciones durante el camino;"
                " revisión recomendada."
                + hints + summary
            )
        elif raw_st == "success":
            title = "Automatización completada"
            txt = "La corrida terminó bien." + hints + summary
            txt += (
                "\n\nPróximo paso: puedes programar esta automatización o duplicarla "
                "como plantilla desde el Centro."
            )
        else:
            title = "Fin de ejecución"
            txt = f"Estado: {st}.{summary}"

        QMessageBox.information(self, title, txt[:4900])

    def _center_on_screen(self):
        try:
            from PyQt6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen().geometry()
            x = (screen.width() - self.width()) // 2
            y = (screen.height() - self.height()) // 2
            self.move(x, y)
        except:
            pass

    def _resize_to_fraction_and_center(self, fraction: float = 0.7):
        """Redimensiona la ventana al % indicado del área disponible y la
        centra. Se usa cuando el usuario restaura desde maximizado para que
        Nevlan luzca bien en vez de quedar en una esquina o con un tamaño
        heredado que no encaja."""
        try:
            from PyQt6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen().availableGeometry()
            w = max(self.minimumWidth(), int(screen.width() * fraction))
            h = max(self.minimumHeight(), int(screen.height() * fraction))
            self.resize(w, h)
            x = screen.x() + (screen.width() - w) // 2
            y = screen.y() + (screen.height() - h) // 2
            self.move(x, y)
        except Exception:
            pass

    def changeEvent(self, event):
        """Detecta la transición maximizado → normal para aplicar un tamaño
        elegante (70% centrado) en vez del tamaño anterior, que a veces
        deja la ventana descentrada o demasiado pequeña.
        """
        try:
            if event.type() == QEvent.Type.WindowStateChange:
                old = event.oldState()
                # Transición: estábamos maximizados y ya no lo estamos.
                was_max = bool(old & Qt.WindowState.WindowMaximized)
                is_max = bool(self.windowState() & Qt.WindowState.WindowMaximized)
                is_min = bool(self.windowState() & Qt.WindowState.WindowMinimized)
                if was_max and not is_max and not is_min:
                    # Programamos el resize post-evento para evitar
                    # interferir con el cálculo interno de Qt.
                    QTimer.singleShot(0, lambda: self._resize_to_fraction_and_center(0.7))
                # Desde la barra de tareas / minimizar → restaurar: recalcular
                # layouts (topbar, cuerpo) una vez el tamaño real está listo.
                was_min = bool(old & Qt.WindowState.WindowMinimized)
                if was_min and not is_min:
                    QTimer.singleShot(0, self._relayout_main_window)
        except Exception:
            pass
        super().changeEvent(event)

    def showEvent(self, event):
        """Tras mostrar o des-ocultar, forzar un pase de layout: evita iconos
        o paneles con geometría obsoleta al volver de minimizado."""
        super().showEvent(event)
        try:
            QTimer.singleShot(0, self._relayout_main_window)
        except Exception:
            pass

    def _relayout_main_window(self):
        """Ajuste fino de layouts tras resize / restore desde minimizar."""
        try:
            self._apply_topbar_compact_mode()
        except Exception:
            pass
        try:
            cw = self.centralWidget()
            if cw:
                cw.updateGeometry()
        except Exception:
            pass
        try:
            self.updateGeometry()
            QApplication.processEvents()
        except Exception:
            pass

    def resizeEvent(self, event):
        """Adapta la barra superior al ancho actual de la ventana.

        En anchos pequeños colapsamos elementos secundarios para que el
        CTA principal 'Grabar proceso' siga siendo accesible y nada
        quede recortado o solapado.
        """
        try:
            self._apply_topbar_compact_mode()
        except Exception:
            pass
        super().resizeEvent(event)

    def _apply_topbar_compact_mode(self):
        """Reorganiza la topbar según el ancho disponible.

        - ≥ 1100 px: todo a tamaño normal.
        - 900–1099 px: el CTA mantiene texto, el botón de voz pasa a icon-only.
        - < 900 px:   búsqueda al mínimo, voz icon-only y CTA con texto corto.
        """
        if not hasattr(self, "voice_btn") or not hasattr(self, "top_record_btn"):
            return
        w = self.width() or 0

        if w < 900:
            self.voice_btn.setText("🎙")
            self.voice_btn.setFixedWidth(40)
            self.top_record_btn.setText("⏺  Grabar")
            self.top_record_btn.setMinimumWidth(150)
            if hasattr(self, "search_input"):
                self.search_input.setMaximumWidth(320)
        elif w < 1100:
            self.voice_btn.setText("🎙")
            self.voice_btn.setFixedWidth(40)
            self.top_record_btn.setText("⏺   Grabar proceso")
            self.top_record_btn.setMinimumWidth(200)
            if hasattr(self, "search_input"):
                self.search_input.setMaximumWidth(460)
        else:
            self.voice_btn.setText("🎙  Asistente de voz")
            # Restaurar tamaño dinámico del botón de voz.
            self.voice_btn.setMinimumWidth(0)
            self.voice_btn.setMaximumWidth(16777215)
            self.top_record_btn.setText("⏺   Grabar proceso")
            self.top_record_btn.setMinimumWidth(220)
            if hasattr(self, "search_input"):
                self.search_input.setMaximumWidth(620)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TOP BAR
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_topbar(self):
        bar = QFrame()
        bar.setFixedHeight(64)
        bar.setStyleSheet(f"""
            QFrame {{
                background: {NevlanTheme.BG_SIDEBAR};
                border-bottom: 1px solid {NevlanTheme.BORDER};
            }}
        """)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(16)

        # Logo
        logo = QLabel("◆ Nevlan")
        logo.setStyleSheet(f"""
            color: {NevlanTheme.ACCENT};
            font-size: 18px; font-weight: 700; letter-spacing: 1px;
        """)
        layout.addWidget(logo)
        layout.addSpacing(20)

        # Search (FUNCTIONAL) — flexible: encoge en pantallas chicas pero
        # nunca por debajo de 230px ni más allá de 461px (otro +20% horiz.). Ocupa
        # el ancho disponible entre el logo y el grupo de CTAs.
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍  Buscar automatización...")
        self.search_input.setMinimumWidth(320)
        self.search_input.setMaximumWidth(620)
        self.search_input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.search_input.setFixedHeight(40)
        # Padding-right reservado para el botón "✕" interno (28px).
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(255,255,255,0.05);
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 8px;
                color: {NevlanTheme.TEXT_PRIMARY};
                padding: 0 36px 0 14px;
                font-size: 14px;
            }}
            QLineEdit:focus {{ border-color: {NevlanTheme.ACCENT}; }}
        """)
        self.search_input.textChanged.connect(self._on_search)
        layout.addWidget(self.search_input)

        # PRD 2026-05-08c §C.1: botón "✕" para limpiar la búsqueda.
        # - Aparece SOLO cuando hay texto.
        # - Click → limpia el input + foco al buscador + refiltro.
        # - Diseño Nevlan: redondo, hover sutil, accesible por teclado.
        # - Atajo de teclado: Esc dentro del campo (instalamos un
        #   eventFilter en el lineedit).
        self.search_clear_btn = QToolButton(self.search_input)
        self.search_clear_btn.setText("✕")
        self.search_clear_btn.setObjectName("searchClearBtn")
        self.search_clear_btn.setToolTip("Limpiar búsqueda")
        self.search_clear_btn.setAccessibleName("Limpiar búsqueda")
        self.search_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.search_clear_btn.setStyleSheet(f"""
            QToolButton#searchClearBtn {{
                background: transparent;
                color: {NevlanTheme.TEXT_MUTED};
                border: none;
                font-size: 14px;
                font-weight: 700;
                padding: 0;
            }}
            QToolButton#searchClearBtn:hover {{
                color: {NevlanTheme.TEXT_PRIMARY};
                background: rgba(255,255,255,0.07);
                border-radius: 10px;
            }}
            QToolButton#searchClearBtn:pressed {{
                color: {NevlanTheme.ACCENT};
            }}
        """)
        self.search_clear_btn.setFixedSize(20, 20)
        self.search_clear_btn.hide()
        self.search_clear_btn.clicked.connect(self._on_clear_search)
        # Reposicionar a la derecha cuando cambie el tamaño del input.
        def _reposition_clear_btn():
            try:
                w = self.search_input.width()
                h = self.search_input.height()
                self.search_clear_btn.move(w - 28, (h - 20) // 2)
            except Exception:
                pass
        self.search_input.installEventFilter(
            _SearchClearEventFilter(
                self,
                on_resize=_reposition_clear_btn,
            )
        )
        # Posición inicial.
        QTimer.singleShot(0, _reposition_clear_btn)
        # Mostrar/ocultar según haya texto.
        self.search_input.textChanged.connect(self._toggle_search_clear_btn)

        # Stretch pequeño a la izquierda para que el CTA principal quede
        # desplazado hacia la izquierda (no pegado al buscador, pero tampoco
        # empujado al borde derecho). El stretch grande irá después del CTA.
        layout.addStretch(1)

        # ── CTA "Grabar proceso" (solo visible cuando el hero NO lo muestra)
        # Exactamente el mismo botón protagónico que aparece en el hero: misma
        # función (`_on_record`), misma intención visual, mismo tooltip. Cuando
        # hay una automatización seleccionada, el hero se oculta y este CTA
        # ocupa su lugar en la barra superior para que grabar siga a un click.
        self.top_record_btn = QPushButton("⏺   Grabar proceso")
        self.top_record_btn.setFixedHeight(40)
        self.top_record_btn.setMinimumWidth(220)
        self.top_record_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.top_record_btn.setToolTip(
            "Le enseñas el proceso. Le dices cuándo. Nevlan lo hace."
        )
        self.top_record_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {NevlanTheme.ACCENT}, stop:1 #6c5ce7);
                color: white; border: none; border-radius: 10px;
                padding: 0 22px; font-size: 13px; font-weight: 700;
                letter-spacing: 0.3px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {NevlanTheme.ACCENT_HOVER}, stop:1 #a29bfe);
                color: #111;
            }}
            QPushButton:pressed {{ background: {NevlanTheme.ACCENT}; }}
        """)
        self.top_record_btn.clicked.connect(self._on_record)
        self.top_record_btn.hide()
        layout.addWidget(self.top_record_btn)

        layout.addSpacing(12)

        # Asistente de voz — acción secundaria, discreto, a la derecha del CTA.
        # Guardamos la referencia para poder colapsar a icon-only en ventanas
        # estrechas (ver `_apply_topbar_compact_mode`).
        self.voice_btn = QPushButton("🎙  Asistente de voz")
        self.voice_btn.setFixedHeight(36)
        self.voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.voice_btn.setStyleSheet(f"""
            QPushButton {{
                background: {NevlanTheme.BG_CARD};
                color: {NevlanTheme.TEXT_SECONDARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 8px;
                padding: 0 14px;
                font-size: 11px;
            }}
            QPushButton:hover {{ background: {NevlanTheme.BG_CARD_HOVER}; color: {NevlanTheme.TEXT_PRIMARY}; }}
        """)
        self.voice_btn.setToolTip("Asistente de voz")
        self.voice_btn.clicked.connect(self._open_voice_pill)
        layout.addWidget(self.voice_btn)

        # Stretch grande a la derecha: empuja el CTA y el asistente hacia
        # la izquierda, dejando aire contra el borde derecho.
        layout.addStretch(4)

        # ── Botón de Usuario (menú: perfil, plan, ajustes, ayuda, salir)
        self.user_btn = QToolButton()
        self.user_btn.setText("👤")
        self.user_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.user_btn.setToolTip("Cuenta y preferencias")
        self.user_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.user_btn.setFixedSize(40, 40)
        self.user_btn.setStyleSheet(f"""
            QToolButton {{
                background: rgba(255,255,255,0.06);
                color: {NevlanTheme.TEXT_PRIMARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 20px;
                font-size: 16px;
            }}
            QToolButton:hover {{
                background: rgba(9,132,227,0.18);
                border-color: rgba(9,132,227,0.45);
            }}
            QToolButton::menu-indicator {{ image: none; width: 0; }}
        """)
        self.user_btn.setMenu(self._build_user_menu())
        layout.addWidget(self.user_btn)

        # `status_lbl` se mantiene como label invisible (sin padre) para
        # preservar todas las llamadas a setText/setStyleSheet que hay en el
        # resto del código sin necesidad de un chip visible en la topbar.
        # El estado de grabación/ejecución ya se comunica con el widget
        # flotante de grabación y con el propio CTA deshabilitado.
        self.status_lbl = QLabel("● Listo")
        self.status_lbl.setVisible(False)

        return bar

    def _build_user_menu(self) -> QMenu:
        """Construye el menú desplegable de cuenta.

        Estructura mínima pero suficiente:
          1. Cabecera: nombre + email + plan actual.
          2. Cuenta: Perfil, Cambiar contraseña.
          3. Suscripción: Plan, Métodos de pago, Facturación.
          4. Aplicación: Preferencias, Atajos de teclado, Idioma.
          5. Soporte: Ayuda, Reportar problema, Acerca de.
          6. Salir.
        """
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {NevlanTheme.BG_CARD};
                color: {NevlanTheme.TEXT_PRIMARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 8px; padding: 6px;
            }}
            QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
            QMenu::item:selected {{
                background: rgba(9,132,227,0.20); color: white;
            }}
            QMenu::separator {{
                height: 1px; background: {NevlanTheme.BORDER};
                margin: 6px 4px;
            }}
        """)

        # ── Cabecera (sólo visual, no clicable)
        header = menu.addAction("Tu cuenta — Plan: Free")
        header.setEnabled(False)
        menu.addSeparator()

        # ── Cuenta
        menu.addAction("👤  Perfil", self._on_user_profile)
        menu.addAction("🔒  Cambiar contraseña", self._on_user_password)
        menu.addSeparator()

        # ── Suscripción
        menu.addAction("✨  Plan y suscripción", self._on_user_plan)
        menu.addAction("💳  Métodos de pago", self._on_user_billing_methods)
        menu.addAction("🧾  Facturación e historial", self._on_user_invoices)
        menu.addSeparator()

        # ── Aplicación
        menu.addAction("⚙   Preferencias", self._on_user_preferences)
        menu.addAction("⌨   Atajos de teclado", self._on_user_shortcuts)
        menu.addSeparator()

        # ── Soporte
        menu.addAction("❓  Ayuda y documentación", self._on_user_help)
        menu.addAction("🐞  Reportar un problema", self._on_user_report)
        menu.addAction("ℹ   Acerca de Nevlan", self._on_user_about)
        menu.addSeparator()

        # ── Salir
        menu.addAction("⏻   Cerrar sesión", self._on_user_signout)
        return menu

    # ─── Handlers del menú de usuario ────────────────────────────────
    def _user_stub(self, title: str, body: str):
        """Stub de placeholder mientras la integración con backend
        de cuentas/billing no exista. Mantiene UX consistente y deja
        un único lugar para conectar cuando llegue."""
        try:
            QMessageBox.information(self, title, body)
        except Exception:
            log.info(f"[user] {title}: {body}")

    def _on_user_profile(self):
        self._user_stub(
            "Perfil",
            "Próximamente: gestión de tu nombre, foto, idioma y zona horaria.\n"
            "Por ahora, los cambios se guardan localmente en la app."
        )

    def _on_user_password(self):
        self._user_stub(
            "Cambiar contraseña",
            "Próximamente: actualización segura de credenciales.\n"
            "Mientras tanto, Nevlan funciona en modo local sin login."
        )

    def _on_user_plan(self):
        self._user_stub(
            "Plan y suscripción",
            "Plan actual: Free.\n\n"
            "• Free   — automatizaciones ilimitadas en local.\n"
            "• Pro    — sincronización en la nube y compartir misiones.\n"
            "• Team   — equipos, roles y auditoría.\n\n"
            "Próximamente: comparativa y upgrade desde aquí."
        )

    def _on_user_billing_methods(self):
        self._user_stub(
            "Métodos de pago",
            "Próximamente: tarjetas y wallets para tu suscripción."
        )

    def _on_user_invoices(self):
        self._user_stub(
            "Facturación e historial",
            "Próximamente: descarga de facturas en PDF e historial de pagos."
        )

    def _on_user_preferences(self):
        self._user_stub(
            "Preferencias",
            "Próximamente: tema, densidad, atajos, ubicación de datos."
        )

    def _on_user_shortcuts(self):
        QMessageBox.information(
            self, "Atajos de teclado",
            "Atajos de Nevlan:\n"
            "  Ctrl+S       Guardar cambios\n"
            "  Ctrl+Z       Deshacer último cambio\n"
            "  Ctrl+Y       Rehacer cambio deshecho\n"
            "  Ctrl+R       Ejecutar automatización seleccionada\n"
            "  Ctrl+G       Empezar a grabar un proceso\n"
            "  F2           Editar nombre de la automatización\n"
            "  Esc          Cancelar grabación / cerrar diálogo\n"
        )

    def _on_user_help(self):
        self._user_stub(
            "Ayuda y documentación",
            "Próximamente: guía interactiva.\n"
            "Mientras tanto: el botón flotante de voz puede responder dudas."
        )

    def _on_user_report(self):
        self._user_stub(
            "Reportar un problema",
            "Próximamente: envío directo desde la app.\n"
            "Mientras tanto, los logs viven en la carpeta logs/ junto al ejecutable."
        )

    def _on_user_about(self):
        QMessageBox.information(
            self, "Acerca de Nevlan",
            "Nevlan — Le enseñas el proceso. Le dices cuándo. Nevlan lo hace.\n\n"
            "Versión: 0.9 (beta)\n"
            "© Nevlan"
        )

    def _on_user_signout(self):
        self._user_stub(
            "Cerrar sesión",
            "Próximamente: gestión de sesiones.\n"
            "Por ahora Nevlan ejecuta sin login."
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # SIDEBAR
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setFixedWidth(260)
        sidebar.setStyleSheet(f"""
            QFrame {{
                background: {NevlanTheme.BG_SIDEBAR};
                border-right: 1px solid {NevlanTheme.BORDER};
            }}
        """)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(6)

        title = QLabel("AUTOMATIZACIONES")
        title.setStyleSheet(f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px; font-weight: 700; letter-spacing: 2px;")
        layout.addWidget(title)
        layout.addSpacing(8)

        # Filters
        filters = ["Todas", "Recientes", "Programadas", "Favoritas", "Requieren atención"]
        filter_icons = ["📁", "🕐", "📅", "⭐", "⚠️"]
        self.filter_btns = []
        for i, (f, icon) in enumerate(zip(filters, filter_icons)):
            btn = QPushButton(f"{icon}  {f}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            active = i == 0
            btn.setStyleSheet(self._filter_style(active))
            btn.clicked.connect(lambda checked, name=f, idx=i: self._apply_filter(name, idx))
            layout.addWidget(btn)
            self.filter_btns.append(btn)

        layout.addSpacing(12)
        div = QFrame(); div.setFixedHeight(1)
        div.setStyleSheet(f"background: {NevlanTheme.BORDER};")
        layout.addWidget(div)
        layout.addSpacing(8)

        # Automation list scroll
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(NevlanTheme.SCROLL_STYLE)

        self.list_widget = QWidget()
        self.list_widget.setStyleSheet("background: transparent;")
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(4)

        self._load_sidebar_list()
        self.list_layout.addStretch()

        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, 1)

        return sidebar

    def _filter_style(self, active):
        return f"""
            QPushButton {{
                background: {"rgba(9,132,227,0.12)" if active else "transparent"};
                color: {NevlanTheme.ACCENT if active else NevlanTheme.TEXT_SECONDARY};
                border: none; border-radius: 6px; padding: 8px 12px;
                text-align: left; font-size: 12px; font-weight: {"600" if active else "400"};
            }}
            QPushButton:hover {{ background: rgba(255,255,255,0.05); color: {NevlanTheme.TEXT_PRIMARY}; }}
        """

    def _load_sidebar_list(self):
        try:
            from app.services.missions import mission_store
            self._all_missions = mission_store.list_all()
            self._all_missions.sort(key=lambda x: x.updated_at, reverse=True)
        except Exception as e:
            log.error(f"Error cargando automatizaciones: {e}")
            self._all_missions = []

        if getattr(self, "filter_btns", None):
            btn_idx = next(
                (i for i, b in enumerate(self.filter_btns) if self._current_filter in b.text()),
                0,
            )
            self._apply_filter(self._current_filter, btn_idx)
        else:
            self._rebuild_sidebar_buttons(self._all_missions)

    def _rebuild_sidebar_buttons(self, missions):
        """Rebuild sidebar buttons from a list of missions."""
        # Clear existing
        while self.list_layout.count() > 0:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._sidebar_buttons = []

        if not missions:
            empty = QLabel("Sin resultados")
            empty.setStyleSheet(f"color: {NevlanTheme.TEXT_MUTED}; font-size: 11px; padding: 12px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.list_layout.addWidget(empty)
            self.list_layout.addStretch()
            return

        for m in missions:
            # Mission Truth Gate: el conteo visible viene del SEP.
            # Antes leíamos len(m.compiled_execution_graph) directamente
            # y eso podía mostrar 8 pasos cuando el SEP solo tenía 6
            # (ej. mission_1778451248).
            steps = _truth_visible_step_count(m)
            status_icon = mission_ui_list_icon(m)

            btn = QPushButton(f"{status_icon}  {m.name}")
            btn.setToolTip(
                f"{steps} pasos · {mission_ui_list_tooltip_suffix(m)}",
            )
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {NevlanTheme.TEXT_PRIMARY};
                    border: none; border-radius: 6px; padding: 10px 12px;
                    text-align: left; font-size: 12px;
                }}
                QPushButton:hover {{ background: rgba(255,255,255,0.04); }}
            """)
            btn.clicked.connect(lambda checked, mission=m: self._select_automation(mission))
            self.list_layout.addWidget(btn)
            self._sidebar_buttons.append((btn, m))

        self.list_layout.addStretch()

    def _toggle_search_clear_btn(self, text: str) -> None:
        """Muestra el botón "✕" solo cuando hay texto en el buscador.

        PRD 2026-05-08c §C.1: la cruz aparece SOLO cuando hay texto.
        """
        try:
            visible = bool((text or "").strip())
            if visible:
                self.search_clear_btn.show()
                self.search_clear_btn.raise_()
            else:
                self.search_clear_btn.hide()
        except Exception:
            pass

    def _on_clear_search(self) -> None:
        """Handler del botón "✕": limpia el buscador y devuelve foco.

        PRD 2026-05-08c §C.1:
          * limpia el input
          * devuelve el foco al buscador
          * actualiza la lista inmediatamente (vía textChanged)
        """
        try:
            # ``setText("")`` dispara textChanged, que llama a _on_search
            # y restablece el sidebar + paneles de actividad/upcoming.
            self.search_input.setText("")
            self.search_input.setFocus(Qt.FocusReason.OtherFocusReason)
        except Exception as e:
            log.debug(f"[automation_center] clear_search error: {e}")

    def _on_search(self, text):
        """Filtra el sidebar Y los paneles de actividad/próximas ejecuciones.

        Una sola caja de búsqueda actúa sobre los tres feeds simultáneamente:
        - Sidebar de automatizaciones
        - Actividad reciente
        - Próximas ejecuciones
        Así el usuario tiene una vista coherente de todo lo relacionado con
        el término que está buscando.
        """
        query = text.strip().lower()
        self._search_query = query
        if not query:
            self._apply_filter(self._current_filter,
                next((i for i, b in enumerate(self.filter_btns)
                      if self._current_filter in b.text()), 0))
        else:
            filtered = [m for m in self._all_missions if query in m.name.lower()]
            self._rebuild_sidebar_buttons(filtered)
        # Refrescar paneles laterales para que respeten el filtro.
        try:
            self._refresh_activity_panel()
            self._refresh_upcoming_panel()
        except Exception:
            pass

    def _apply_filter(self, filter_name, btn_idx):
        """Apply a sidebar filter."""
        self._current_filter = filter_name
        # Update button styles
        for i, btn in enumerate(self.filter_btns):
            btn.setStyleSheet(self._filter_style(i == btn_idx))

        missions = self._all_missions

        if filter_name == "Todas":
            filtered = missions
        elif filter_name == "Recientes":
            filtered = sorted(missions, key=lambda x: x.updated_at, reverse=True)[:10]
        elif filter_name == "Programadas":
            if self._scheduler:
                scheduled_ids = {s.mission_id for s in self._scheduler.get_all_schedules()
                                 if s.mode != "Bajo demanda (manual)"}
                filtered = [m for m in missions if m.id in scheduled_ids]
            else:
                filtered = []
        elif filter_name == "Favoritas":
            if self._tracker:
                fav_ids = self._tracker.get_favorites()
                filtered = [m for m in missions if m.id in fav_ids]
            else:
                filtered = []
        elif filter_name == "Requieren atenci\u00f3n":
            attention_ids = set()
            for m in missions:
                if mission_ui_shows_in_attention_filter(m):
                    attention_ids.add(m.id)
            # Failed last execution
            if self._tracker:
                attention_ids.update(self._tracker.get_failed_mission_ids())
            filtered = [m for m in missions if m.id in attention_ids]
        else:
            filtered = missions

        self._rebuild_sidebar_buttons(filtered)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # CENTER PANEL
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_center_panel(self):
        self.center_stack = QStackedWidget()
        self.hero_page = self._build_hero()
        self.center_stack.addWidget(self.hero_page)
        self.detail_page = self._build_detail()
        self.center_stack.addWidget(self.detail_page)
        self.center_stack.setCurrentIndex(0)
        # Sincroniza el CTA "Grabar proceso" superior con el stack:
        # solo aparece cuando NO estamos en el hero (que ya muestra el
        # botón grande centrado). Evita duplicar CTAs en pantalla.
        self.center_stack.currentChanged.connect(self._sync_top_record_visibility)
        return self.center_stack

    def _sync_top_record_visibility(self, index: Optional[int] = None):
        """Muestra el CTA superior solo en la vista de detalle."""
        if not hasattr(self, "top_record_btn"):
            return
        try:
            idx = self.center_stack.currentIndex() if index is None else int(index)
        except Exception:
            idx = 0
        # index 0 = hero (ya trae CTA grande), 1 = detalle (subimos el CTA)
        self.top_record_btn.setVisible(idx != 0)

    def _build_hero(self):
        page = QWidget()
        page.setStyleSheet(f"background: {NevlanTheme.BG_MAIN};")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(60, 60, 60, 60)
        layout.setSpacing(16)

        layout.addStretch()

        title = QLabel("Automatiza tareas en minutos")
        title.setStyleSheet(f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 32px; font-weight: 700;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        tagline = QLabel("Le enseñas el proceso. Le dices cuándo. Nevlan lo hace.")
        tagline.setStyleSheet(f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 15px;")
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(tagline)

        layout.addSpacing(24)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_record = QPushButton("⏺   Grabar proceso")
        btn_record.setFixedSize(280, 64)
        btn_record.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_record.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {NevlanTheme.ACCENT}, stop:1 #6c5ce7);
                color: white; border: none; border-radius: 14px;
                font-size: 18px; font-weight: 700; letter-spacing: 0.5px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {NevlanTheme.ACCENT_HOVER}, stop:1 #a29bfe);
                color: #111;
            }}
        """)
        btn_record.clicked.connect(self._on_record)
        btn_row.addWidget(btn_record)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Hint below CTA
        hint = QLabel("Presiona y sigue tu proceso paso a paso. Nevlan aprenderá.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color: {NevlanTheme.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(hint)

        layout.addSpacing(32)

        # Quick action cards
        cards_row = QHBoxLayout()
        cards_row.setSpacing(16)
        cards_row.addStretch()

        quick_actions = [
            ("▶️", "Ejecutar ahora", "Selecciona una automatización y ejecútala"),
            ("📅", "Programar", "Configura horarios de ejecución automática"),
            ("⚡", "Crear disparador", "Ejecuta por evento o condición"),
        ]

        for icon, label, desc in quick_actions:
            card = QFrame()
            card.setFixedSize(200, 110)
            card.setStyleSheet(f"""
                QFrame {{ background: {NevlanTheme.BG_CARD}; border: 1px solid {NevlanTheme.BORDER}; border-radius: 10px; }}
                QFrame:hover {{ background: {NevlanTheme.BG_CARD_HOVER}; border-color: rgba(9,132,227,0.3); }}
            """)
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(16, 14, 16, 14)
            cl.setSpacing(6)

            ic = QLabel(icon)
            ic.setStyleSheet("font-size: 22px; background: transparent; border: none;")
            cl.addWidget(ic)
            lb = QLabel(label)
            lb.setStyleSheet(f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 13px; font-weight: 600; background: transparent; border: none;")
            cl.addWidget(lb)
            dc = QLabel(desc)
            dc.setWordWrap(True)
            dc.setStyleSheet(f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px; background: transparent; border: none;")
            cl.addWidget(dc)
            cards_row.addWidget(card)

        cards_row.addStretch()
        layout.addLayout(cards_row)
        layout.addStretch()
        return page

    def _build_detail(self):
        page = QWidget()
        page.setStyleSheet(f"background: {NevlanTheme.BG_MAIN};")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)

        # ─────────────────────────────────────────────────────────────
        # Orden jerárquico solicitado:
        #   [Row 1] Título   "Pasos de la automatización"  + badge
        #   [Row 2] Subtítulo  "Edita, prueba o regraba…"
        #   [Row 3] Nombre de la automatización (editable)
        #   [Row 4] Controles: agregar / guardar / deshacer / cancelar
        #   [separador]
        #   [scroll] tarjetas de pasos
        # ─────────────────────────────────────────────────────────────

        # ── Row 1: Título + badge de pasos ─────────────────────────
        title_row = QHBoxLayout()
        title_row.setSpacing(10)

        self.detail_steps_lbl = QLabel("Pasos de la automatización")
        self.detail_steps_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 24px; font-weight: 800;"
            " letter-spacing: 0.3px; background: transparent; border: none;"
        )
        title_row.addWidget(self.detail_steps_lbl)

        self.step_count_badge = QLabel("0 pasos")
        self.step_count_badge.setStyleSheet(
            f"color: {NevlanTheme.ACCENT}; font-size: 11px; font-weight: 600;"
            f" background: rgba(9,132,227,0.12); border: none;"
            f" border-radius: 10px; padding: 3px 12px;"
        )
        title_row.addWidget(self.step_count_badge)
        title_row.addStretch()

        self.fav_btn = QPushButton("☆ Favorita")
        self.fav_btn.setFixedHeight(32)
        self.fav_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fav_btn.setToolTip("Marcar o quitar esta automatización de favoritos")
        self.fav_btn.setStyleSheet(f"""
            QPushButton {{
                background: {NevlanTheme.BG_CARD};
                color: {NevlanTheme.TEXT_SECONDARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 6px;
                padding: 0 12px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {NevlanTheme.BG_CARD_HOVER}; color: {NevlanTheme.TEXT_PRIMARY}; }}
        """)
        self.fav_btn.clicked.connect(self._on_toggle_favorite)
        self.fav_btn.setEnabled(self._tracker is not None)
        if self._tracker is None:
            self.fav_btn.setToolTip("Favoritos no disponibles")
        title_row.addWidget(self.fav_btn)

        self.detail_status = QLabel("Estado")
        self.detail_status.setStyleSheet(f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 12px;")
        title_row.addWidget(self.detail_status)

        layout.addLayout(title_row)

        # ── Row 2: Subtítulo ───────────────────────────────────────
        steps_subtitle = QLabel(
            "Edita, prueba o regraba cada paso sin salir de esta vista."
        )
        steps_subtitle.setStyleSheet(
            f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 12px;"
            " background: transparent; border: none;"
        )
        layout.addWidget(steps_subtitle)

        # ── Row 3: Nombre de la automatización (ahora debajo del título) ─
        name_row = QHBoxLayout()
        name_row.setSpacing(8)

        name_lbl = QLabel("Nombre")
        name_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px; font-weight: 700;"
            " letter-spacing: 1px; text-transform: uppercase;"
            " background: transparent; border: none;"
        )
        name_row.addWidget(name_lbl)

        self.detail_name_edit = QLineEdit("Nombre")
        self.detail_name_edit.setFixedHeight(36)
        self.detail_name_edit.setStyleSheet(f"""
            QLineEdit {{
                color: {NevlanTheme.TEXT_PRIMARY}; font-size: 16px; font-weight: 600;
                background: rgba(0,0,0,0.2); border: 1px solid {NevlanTheme.BORDER};
                border-radius: 6px; padding: 6px 12px;
            }}
            QLineEdit:focus {{ border-color: {NevlanTheme.ACCENT}; }}
        """)
        self.detail_name_edit.textChanged.connect(self._on_name_changed)
        name_row.addWidget(self.detail_name_edit, 1)
        layout.addLayout(name_row)

        # ── Row 4: Controles de edición (sin encimado) ─────────────
        # Envolvemos en QScrollArea horizontal para que cuando la ventana
        # se haga estrecha los botones (Agregar paso / Deshacer / Cancelar /
        # Guardar) NO se descoloquen ni se encimen, sólo se vuelve scrollable.
        ctrl_scroll = QScrollArea()
        ctrl_scroll.setWidgetResizable(True)
        ctrl_scroll.setFrameShape(QFrame.Shape.NoFrame)
        ctrl_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        ctrl_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ctrl_scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:horizontal {{ background: transparent; height: 6px; margin: 0; }}
            QScrollBar::handle:horizontal {{
                background: rgba(255,255,255,0.18); border-radius: 3px; min-width: 24px;
            }}
            QScrollBar::handle:horizontal:hover {{ background: rgba(255,255,255,0.30); }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        """)
        ctrl_scroll.setFixedHeight(46)
        ctrl_inner = QWidget()
        ctrl_inner.setStyleSheet("background: transparent;")
        ctrl_row = QHBoxLayout(ctrl_inner)
        ctrl_row.setContentsMargins(0, 0, 0, 0)
        ctrl_row.setSpacing(0)

        # La leyenda de iconos por paso (Reordenar/Insertar/Vibe/...) ya no
        # va junto a los botones de la cabecera: se reubica DEBAJO, en su
        # propia fila. Los botones (Agregar paso / Deshacer / Cancelar
        # cambios / Guardar cambios) se distribuyen JUSTIFICADOS a lo
        # ancho disponible: el primero pegado al borde izquierdo, el
        # último al borde derecho, con espacio igual entre cada par.

        self.add_step_btn = QPushButton("+ Agregar paso")
        self.add_step_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_step_btn.setFixedHeight(32)
        self.add_step_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(9,132,227,0.18);
                color: #74b9ff;
                border: 1px solid rgba(9,132,227,0.35);
                border-radius: 6px;
                padding: 0 14px;
                font-size: 11px; font-weight: 700;
            }}
            QPushButton:hover {{ background: rgba(9,132,227,0.32); }}
        """)
        self.add_step_btn.setToolTip("Agregar un paso nuevo al final de la automatización")
        self.add_step_btn.clicked.connect(lambda: self._add_step_at(None))
        ctrl_row.addWidget(self.add_step_btn)
        ctrl_row.addStretch(1)

        # Deshacer último cambio (se habilita cuando hay algo en el stack)
        self.undo_changes_btn = QPushButton("↶ Deshacer")
        self.undo_changes_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_changes_btn.setFixedHeight(32)
        self.undo_changes_btn.setStyleSheet(f"""
            QPushButton {{
                background: {NevlanTheme.BG_CARD};
                color: {NevlanTheme.TEXT_SECONDARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 6px; padding: 0 12px;
                font-size: 11px; font-weight: 600;
            }}
            QPushButton:hover {{
                background: {NevlanTheme.BG_CARD_HOVER};
                color: {NevlanTheme.TEXT_PRIMARY};
            }}
            QPushButton:disabled {{ color: rgba(255,255,255,0.25); }}
        """)
        self.undo_changes_btn.setToolTip("Revertir el último cambio sin guardar (Ctrl+Z)")
        self.undo_changes_btn.setEnabled(False)
        self.undo_changes_btn.clicked.connect(self._undo_last_change)
        ctrl_row.addWidget(self.undo_changes_btn)
        ctrl_row.addStretch(1)

        # Rehacer último cambio deshecho (mismo estilo que Deshacer pero
        # con flecha en sentido contrario). Se habilita en cuanto el
        # usuario hace al menos un Deshacer y se invalida en cuanto
        # introduce un cambio nuevo (comportamiento estándar de redo).
        self.redo_changes_btn = QPushButton("↷ Rehacer")
        self.redo_changes_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.redo_changes_btn.setFixedHeight(32)
        self.redo_changes_btn.setStyleSheet(f"""
            QPushButton {{
                background: {NevlanTheme.BG_CARD};
                color: {NevlanTheme.TEXT_SECONDARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 6px; padding: 0 12px;
                font-size: 11px; font-weight: 600;
            }}
            QPushButton:hover {{
                background: {NevlanTheme.BG_CARD_HOVER};
                color: {NevlanTheme.TEXT_PRIMARY};
            }}
            QPushButton:disabled {{ color: rgba(255,255,255,0.25); }}
        """)
        self.redo_changes_btn.setToolTip("Rehacer el último cambio deshecho (Ctrl+Y)")
        self.redo_changes_btn.setEnabled(False)
        self.redo_changes_btn.clicked.connect(self._redo_last_change)
        ctrl_row.addWidget(self.redo_changes_btn)
        ctrl_row.addStretch(1)

        # Cancelar TODOS los cambios (recarga desde disco)
        self.cancel_changes_btn = QPushButton("✕ Cancelar cambios")
        self.cancel_changes_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_changes_btn.setFixedHeight(32)
        self.cancel_changes_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(231,76,60,0.15);
                color: #ff7675;
                border: 1px solid rgba(231,76,60,0.35);
                border-radius: 6px; padding: 0 12px;
                font-size: 11px; font-weight: 700;
            }}
            QPushButton:hover {{ background: rgba(231,76,60,0.28); }}
            QPushButton:disabled {{
                background: transparent; color: rgba(255,255,255,0.25);
                border-color: {NevlanTheme.BORDER};
            }}
        """)
        self.cancel_changes_btn.setToolTip(
            "Descartar TODOS los cambios sin guardar y recargar la automatización"
        )
        self.cancel_changes_btn.setEnabled(False)
        self.cancel_changes_btn.clicked.connect(self._cancel_all_changes)
        ctrl_row.addWidget(self.cancel_changes_btn)
        ctrl_row.addStretch(1)

        # Guardar cambios — tono sobrio cuando hay algo que guardar, casi
        # invisible cuando está deshabilitado. Nada de colores chillones.
        self.save_changes_btn = QPushButton("💾 Guardar cambios")
        self.save_changes_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_changes_btn.setFixedHeight(32)
        self.save_changes_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(0,206,201,0.16);
                color: #7ed9d5;
                border: 1px solid rgba(0,206,201,0.35);
                border-radius: 6px;
                padding: 0 14px; font-size: 11px; font-weight: 700;
            }}
            QPushButton:hover {{
                background: rgba(0,206,201,0.28);
                color: #a6e8e5;
            }}
            QPushButton:disabled {{
                background: transparent;
                color: rgba(255,255,255,0.22);
                border: 1px solid {NevlanTheme.BORDER};
            }}
        """)
        self.save_changes_btn.setToolTip("Guardar los cambios pendientes (Ctrl+S)")
        self.save_changes_btn.setEnabled(False)
        self.save_changes_btn.clicked.connect(self._save_pending_changes)
        ctrl_row.addWidget(self.save_changes_btn)

        ctrl_scroll.setWidget(ctrl_inner)
        layout.addWidget(ctrl_scroll)

        # ── Leyenda de iconos de paso (debajo de la fila de botones) ──
        # Reproduce la disposición real de los iconos dentro de cada
        # tarjeta de paso:
        #   IZQUIERDA → ↑↓ Reordenar  ⤵ Insertar
        #   DERECHA   → ✨ Vibe  🎯 Regrabar  🧪 Probar  ✕ Eliminar
        # Así la leyenda queda alineada visualmente con los iconos que
        # acompañan a cada paso en la lista.
        legend_widget = QWidget()
        legend_widget.setStyleSheet("background: transparent; border: none;")
        legend_row = QHBoxLayout(legend_widget)
        legend_row.setContentsMargins(2, 2, 2, 4)
        legend_row.setSpacing(10)

        _legend_left = ["↑↓ Reordenar", "⤵ Insertar"]
        _legend_right = ["✨ Vibe", "🎯 Regrabar", "🧪 Probar", "✕ Eliminar"]
        _legend_style = (
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px;"
            " background: transparent; border: none;"
        )

        for _txt in _legend_left:
            _lbl = QLabel(_txt)
            _lbl.setStyleSheet(_legend_style)
            legend_row.addWidget(_lbl)

        legend_row.addStretch(1)

        for _txt in _legend_right:
            _lbl = QLabel(_txt)
            _lbl.setStyleSheet(_legend_style)
            legend_row.addWidget(_lbl)

        layout.addWidget(legend_widget)

        div = QFrame(); div.setFixedHeight(1)
        div.setStyleSheet(f"background: {NevlanTheme.BORDER};")
        layout.addWidget(div)

        # ── Steps scroll ──
        self.detail_steps_area = QScrollArea()
        self.detail_steps_area.setWidgetResizable(True)
        self.detail_steps_area.setStyleSheet(NevlanTheme.SCROLL_STYLE)
        self.detail_steps_widget = QWidget()
        self.detail_steps_widget.setStyleSheet("background: transparent;")
        self.detail_steps_layout = QVBoxLayout(self.detail_steps_widget)
        self.detail_steps_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_steps_layout.setSpacing(4)
        self.detail_steps_area.setWidget(self.detail_steps_widget)
        layout.addWidget(self.detail_steps_area, 1)

        # ── Action buttons ────────────────────────────────────────
        #   Compactos, sin scroll horizontal. Dos filas:
        #   Fila 1: Ejecutar (primario) + Programar, Historial, Recompilar, Recompilar todas.
        #   Fila 2: Eliminar (derecha, peligro).
        action_container = QWidget()
        action_container.setStyleSheet("background: transparent;")
        action_vbox = QVBoxLayout(action_container)
        action_vbox.setContentsMargins(0, 4, 0, 0)
        action_vbox.setSpacing(4)

        def _mk_action_btn(label: str, kind: str, handler, tooltip: str = "") -> QPushButton:
            """kind: 'primary' | 'secondary' | 'danger'."""
            b = QPushButton(label)
            b.setFixedHeight(32)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if tooltip:
                b.setToolTip(tooltip)
            if kind == "primary":
                b.setStyleSheet(f"""
                    QPushButton {{
                        background: {NevlanTheme.ACCENT};
                        color: white; border: none; border-radius: 7px;
                        padding: 0 12px; font-size: 11px; font-weight: 700;
                    }}
                    QPushButton:hover {{
                        background: {NevlanTheme.ACCENT_HOVER};
                        color: #111;
                    }}
                """)
            elif kind == "danger":
                b.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent;
                        color: #ff7675;
                        border: 1px solid rgba(231,76,60,0.35);
                        border-radius: 7px;
                        padding: 0 12px; font-size: 11px; font-weight: 600;
                    }}
                    QPushButton:hover {{
                        background: rgba(231,76,60,0.18);
                        color: #ff9b97;
                    }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{
                        background: {NevlanTheme.BG_CARD};
                        color: {NevlanTheme.TEXT_PRIMARY};
                        border: 1px solid {NevlanTheme.BORDER};
                        border-radius: 7px;
                        padding: 0 12px; font-size: 11px; font-weight: 500;
                    }}
                    QPushButton:hover {{
                        background: {NevlanTheme.BG_CARD_HOVER};
                        border-color: rgba(9,132,227,0.35);
                    }}
                """)
            b.clicked.connect(handler)
            return b

        # Fila 1: acciones principales
        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(5)

        self.detail_execute_btn = _mk_action_btn(
            "▶ Ejecutar", "primary", self._on_execute,
            "Ejecutar la automatización ahora",
        )
        row1.addWidget(self.detail_execute_btn)

        # Separador visual compacto
        sep1 = QFrame()
        sep1.setFixedWidth(1)
        sep1.setFixedHeight(22)
        sep1.setStyleSheet(f"background: {NevlanTheme.BORDER}; border: none;")
        row1.addWidget(sep1)

        row1.addWidget(_mk_action_btn(
            "📅 Programar", "secondary", self._on_schedule,
            "Agendar ejecución recurrente"))
        row1.addWidget(_mk_action_btn(
            "📊 Historial", "secondary", self._on_history,
            "Ver historial de ejecuciones"))
        row1.addWidget(_mk_action_btn(
            "♻ Recompilar", "secondary", self._on_recompile,
            "Regenerar pasos desde el raw_trace"))
        row1.addWidget(_mk_action_btn(
            "♻ Recomp. todas", "secondary", self._on_recompile_all,
            "Recompilar todas las automatizaciones con raw_trace"))

        row1.addStretch()

        # Eliminar al final de fila 1
        row1.addWidget(_mk_action_btn(
            "🗑 Eliminar", "danger", self._on_delete_mission,
            "Eliminar esta automatización definitivamente"))

        action_vbox.addLayout(row1)
        layout.addWidget(action_container)
        return page

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ACTIVITY PANEL (RIGHT)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_activity_panel(self):
        panel = QFrame()
        panel.setFixedWidth(320)
        panel.setStyleSheet(f"""
            QFrame {{ background: {NevlanTheme.BG_SIDEBAR}; border-left: 1px solid {NevlanTheme.BORDER}; }}
        """)
        root = QVBoxLayout(panel)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # ── SECTION A: Actividad reciente (scroll, hasta 1 mes) ──
        section_a = QFrame()
        section_a.setStyleSheet(
            f"QFrame {{ background: rgba(255,255,255,0.02); border: 1px solid {NevlanTheme.BORDER}; border-radius: 10px; }}"
        )
        a_layout = QVBoxLayout(section_a)
        a_layout.setContentsMargins(12, 10, 12, 10)
        a_layout.setSpacing(8)

        a_header = QHBoxLayout()
        a_title = QLabel("ACTIVIDAD RECIENTE")
        a_title.setStyleSheet(
            f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 11px; font-weight: 800;"
            " letter-spacing: 1.5px; background: transparent; border: none;"
        )
        a_header.addWidget(a_title)
        a_header.addStretch()
        self.activity_range_lbl = QLabel("últimos 30 días")
        self.activity_range_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 9px; font-weight: 600;"
            " background: transparent; border: none;"
        )
        a_header.addWidget(self.activity_range_lbl)
        a_layout.addLayout(a_header)

        self.activity_scroll = QScrollArea()
        self.activity_scroll.setWidgetResizable(True)
        self.activity_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # El panel tiene ancho fijo: nunca queremos scroll horizontal,
        # solo vertical. Así las tarjetas jamás provocan desbordes.
        self.activity_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.activity_scroll.setStyleSheet(f"""
            QScrollArea {{ border: none; background: transparent; }}
            QScrollBar:vertical {{
                background: rgba(255,255,255,0.04); width: 8px; border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(255,255,255,0.25); border-radius: 4px; min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,0.4); }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

        self.activity_feed = QWidget()
        self.activity_feed.setStyleSheet("background: transparent;")
        self.activity_feed_layout = QVBoxLayout(self.activity_feed)
        self.activity_feed_layout.setContentsMargins(0, 0, 0, 0)
        self.activity_feed_layout.setSpacing(6)
        self.activity_scroll.setWidget(self.activity_feed)
        self.activity_scroll.setMinimumHeight(240)
        a_layout.addWidget(self.activity_scroll, 1)

        root.addWidget(section_a, 3)

        # ── SECTION B: Próximas ejecuciones (scroll propio) ──
        section_b = QFrame()
        section_b.setStyleSheet(
            f"QFrame {{ background: rgba(9,132,227,0.06); border: 1px solid rgba(9,132,227,0.18);"
            f" border-radius: 10px; }}"
        )
        b_layout = QVBoxLayout(section_b)
        b_layout.setContentsMargins(12, 10, 12, 10)
        b_layout.setSpacing(8)

        b_header = QHBoxLayout()
        b_title = QLabel("PRÓXIMAS EJECUCIONES")
        b_title.setStyleSheet(
            f"color: {NevlanTheme.ACCENT}; font-size: 11px; font-weight: 800;"
            " letter-spacing: 1.5px; background: transparent; border: none;"
        )
        b_header.addWidget(b_title)
        b_header.addStretch()
        self.upcoming_count_lbl = QLabel("0")
        self.upcoming_count_lbl.setStyleSheet(
            f"color: {NevlanTheme.ACCENT}; font-size: 10px; font-weight: 700;"
            " background: rgba(9,132,227,0.15); border: none;"
            " border-radius: 8px; padding: 1px 8px;"
        )
        b_header.addWidget(self.upcoming_count_lbl)
        b_layout.addLayout(b_header)

        self.upcoming_scroll = QScrollArea()
        self.upcoming_scroll.setWidgetResizable(True)
        self.upcoming_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.upcoming_scroll.setStyleSheet(self.activity_scroll.styleSheet())

        self.upcoming_feed = QWidget()
        self.upcoming_feed.setStyleSheet("background: transparent;")
        self.upcoming_feed_layout = QVBoxLayout(self.upcoming_feed)
        self.upcoming_feed_layout.setContentsMargins(0, 0, 0, 0)
        self.upcoming_feed_layout.setSpacing(6)
        self.upcoming_scroll.setWidget(self.upcoming_feed)
        self.upcoming_scroll.setMinimumHeight(140)
        b_layout.addWidget(self.upcoming_scroll, 1)

        # Label compacto con la próxima ejecución global (legacy)
        self.next_exec_lbl = QLabel("Sin ejecuciones programadas")
        self.next_exec_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px; font-style: italic;"
            " background: transparent; border: none;"
        )
        b_layout.addWidget(self.next_exec_lbl)

        root.addWidget(section_b, 2)

        self._refresh_activity_panel()
        self._refresh_upcoming_panel()
        return panel

    def _refresh_activity_panel(self):
        """Actualiza el feed de actividad con historial de 1 mes.

        Si hay query activo en la búsqueda, solo muestra ejecuciones cuyo
        nombre de misión contenga el término — coherente con el sidebar.
        """
        if not hasattr(self, "activity_feed_layout"):
            return
        while self.activity_feed_layout.count():
            item = self.activity_feed_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._tracker:
            self.activity_feed_layout.addWidget(
                self._build_activity_entry_empty("Actividad no disponible"))
            self.activity_feed_layout.addStretch()
            return

        entries = self._tracker.get_since(days=30, limit=200)
        query = (getattr(self, "_search_query", "") or "").strip().lower()
        if query:
            entries = [
                ex for ex in entries
                if query in (ex.mission_name or "").lower()
            ]

        if not entries:
            msg = (f"Sin actividad para «{query}»" if query
                   else "Sin ejecuciones en los últimos 30 días")
            self.activity_feed_layout.addWidget(self._build_activity_entry_empty(msg))
            self.activity_feed_layout.addStretch()
            return

        for ex in entries:
            self.activity_feed_layout.addWidget(self._build_activity_entry_rich(ex))
        self.activity_feed_layout.addStretch()

    def _refresh_upcoming_panel(self):
        """Actualiza el feed de próximas ejecuciones.

        Respeta el query de búsqueda igual que el sidebar y la actividad.
        """
        if not hasattr(self, "upcoming_feed_layout"):
            return
        while self.upcoming_feed_layout.count():
            item = self.upcoming_feed_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        items = []
        if self._scheduler:
            try:
                items = self._scheduler.get_upcoming(limit=30)
            except Exception as e:
                log.debug(f"get_upcoming falló: {e}")

        query = (getattr(self, "_search_query", "") or "").strip().lower()
        if query:
            items = [it for it in items
                     if query in (it.get("name", "") or "").lower()]

        if hasattr(self, "upcoming_count_lbl"):
            self.upcoming_count_lbl.setText(str(len(items)))

        if not items:
            msg = (f"Sin programaciones para «{query}»" if query
                   else "Sin ejecuciones programadas")
            self.upcoming_feed_layout.addWidget(self._build_activity_entry_empty(msg))
            self.upcoming_feed_layout.addStretch()
            return

        for it in items:
            self.upcoming_feed_layout.addWidget(self._build_upcoming_entry(it))
        self.upcoming_feed_layout.addStretch()

    def _build_activity_entry_empty(self, msg: str):
        lbl = QLabel(msg)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 11px;"
            " padding: 16px 8px; background: transparent; border: none;"
        )
        return lbl

    def _build_activity_entry_rich(self, ex):
        """Tarjeta compacta de actividad con nombre, meta, estado y botón borrar.

        PRD §C: estado y color se derivan del lifecycle real
        (``ex.is_executed``/``ex.required_intervention``), nunca del
        string libre legacy.
        """
        entry = QFrame()
        is_success = bool(getattr(ex, "is_executed", False))
        is_fail = bool(getattr(ex, "required_intervention", False))
        bar_color = (NevlanTheme.SUCCESS if is_success
                     else (NevlanTheme.ERROR if is_fail else NevlanTheme.WARNING))

        entry.setStyleSheet(f"""
            QFrame {{
                background: rgba(255,255,255,0.03);
                border: 1px solid {NevlanTheme.BORDER};
                border-left: 3px solid {bar_color};
                border-radius: 7px;
            }}
        """)
        el = QVBoxLayout(entry)
        el.setContentsMargins(8, 4, 6, 4)
        el.setSpacing(1)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        ic = QLabel(ex.icon)
        ic.setStyleSheet("font-size: 11px; background: transparent; border: none;")
        row1.addWidget(ic)
        name = QLabel(ex.mission_name or "—")
        name.setStyleSheet(
            f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 10px; font-weight: 600;"
            " background: transparent; border: none;"
        )
        name.setMaximumWidth(180)
        name.setToolTip(ex.mission_name or "—")
        # Elipsis si el nombre es largo
        fm = name.fontMetrics()
        elided = fm.elidedText(ex.mission_name or "—", Qt.TextElideMode.ElideRight, 170)
        name.setText(elided)
        row1.addWidget(name)
        row1.addStretch()

        # Botón borrar entrada del historial
        del_btn = QPushButton("×")
        del_btn.setFixedSize(16, 16)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setToolTip("Borrar del historial (no elimina la automatización)")
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {NevlanTheme.TEXT_MUTED};
                border: none; font-size: 12px; font-weight: 700;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background: rgba(255,0,0,0.18); color: #ff7675;
            }}
        """)
        entry_id = getattr(ex, "id", None)
        del_btn.clicked.connect(lambda checked, eid=entry_id: self._delete_activity_entry(eid))
        row1.addWidget(del_btn)
        el.addLayout(row1)

        meta_parts = [ex.datetime_human]
        if ex.duration_human and ex.duration_human != "—":
            meta_parts.append(ex.duration_human)
        meta_parts.append(ex.trigger)
        meta = QLabel("  ·  ".join(meta_parts))
        meta.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 8px;"
            " background: transparent; border: none; padding-left: 17px;"
        )
        el.addWidget(meta)

        if ex.required_intervention:
            warn = QLabel("⚠️ Requirió intervención")
            warn.setStyleSheet(
                f"color: {NevlanTheme.ERROR}; font-size: 8px; font-weight: 600;"
                " background: transparent; border: none; padding-left: 17px;"
            )
            el.addWidget(warn)

        return entry

    def _delete_activity_entry(self, entry_id):
        """Borra una entrada del historial tras confirmación."""
        if not entry_id or not self._tracker:
            return
        reply = QMessageBox.question(
            self, "Borrar entrada",
            "¿Borrar esta entrada del historial?\nLa automatización no se eliminará.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._tracker.delete_entry(entry_id)
            self._refresh_activity_panel()

    def _build_upcoming_entry(self, item: dict):
        entry = QFrame()
        enabled = item.get("enabled", True)
        mid = item.get("mission_id", "")
        entry.setStyleSheet(f"""
            QFrame {{
                background: rgba(9,132,227,{'0.08' if enabled else '0.02'});
                border: 1px solid rgba(9,132,227,0.2);
                border-radius: 7px;
            }}
        """)
        el = QVBoxLayout(entry)
        el.setContentsMargins(10, 7, 10, 7)
        el.setSpacing(3)

        row1 = QHBoxLayout()
        ic = QLabel("📅")
        ic.setStyleSheet("font-size: 12px; background: transparent; border: none;")
        row1.addWidget(ic)
        name = QLabel(item.get("name", "—"))
        name.setStyleSheet(
            f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 11px; font-weight: 600;"
            " background: transparent; border: none;"
        )
        name.setWordWrap(True)
        row1.addWidget(name, 1)
        state = QLabel("Activa" if enabled else "Inactiva")
        state.setStyleSheet(
            f"color: {NevlanTheme.SUCCESS if enabled else NevlanTheme.TEXT_MUTED};"
            " font-size: 9px; font-weight: 700;"
            " background: transparent; border: none;"
        )
        row1.addWidget(state)
        el.addLayout(row1)

        meta = QLabel(f"{item.get('trigger','—')}  ·  {item.get('next_run','—')}")
        meta.setStyleSheet(
            f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 9px;"
            " background: transparent; border: none; padding-left: 17px;"
        )
        el.addWidget(meta)

        # Segunda línea: "desde las HH:MM · próx. HH:MM" para triggers
        # tipo "Cada X minutos" donde el usuario necesita saber desde cuándo
        # se está contando el intervalo.
        since_h = item.get("since_human")
        next_at = item.get("next_run_at")
        if since_h or next_at:
            parts = []
            if since_h:
                parts.append(since_h)
            if next_at:
                parts.append(f"próx. {next_at}")
            detail = QLabel("  ·  ".join(parts))
            detail.setStyleSheet(
                f"color: {NevlanTheme.TEXT_MUTED}; font-size: 9px;"
                " background: transparent; border: none; padding-left: 17px;"
            )
            el.addWidget(detail)

        # ── Botones de acción ────────────────────────────────────────
        # Con "▶ Ejecutar", "📝 Editar" y "Desactivar/Activar" en texto, y
        # la papelera 🗑 como icono compacto. Tamaños fijos para que la
        # fila quepa exacta en el ancho del panel.

        run_btn = QPushButton("▶ Ejecutar")
        run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        run_btn.setFixedSize(70, 22)
        run_btn.setToolTip(
            "Ejecutar ahora sin alterar el orden de las ejecuciones programadas."
        )
        run_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(9,132,227,0.18);
                color: {NevlanTheme.ACCENT};
                border: 1px solid rgba(9,132,227,0.4);
                border-radius: 4px; font-size: 10px; font-weight: 700;
                padding: 0 4px;
            }}
            QPushButton:hover {{
                background: rgba(9,132,227,0.32);
                color: #74b9ff;
            }}
        """)
        run_btn.clicked.connect(
            lambda _=False, m=mid, n=item.get("name", ""):
            self._run_scheduled_mission_now(m, n)
        )

        edit_btn = QPushButton("Editar")
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.setFixedSize(50, 22)
        edit_btn.setToolTip(
            "Editar horario (intervalo, a partir de cuándo, días, etc.)."
        )
        edit_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255,255,255,0.05);
                color: {NevlanTheme.TEXT_SECONDARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 4px; font-size: 10px; font-weight: 600;
                padding: 0 4px;
            }}
            QPushButton:hover {{
                background: rgba(9,132,227,0.15);
                color: {NevlanTheme.ACCENT};
                border-color: rgba(9,132,227,0.35);
            }}
        """)
        edit_btn.clicked.connect(
            lambda _=False, m=mid, n=item.get("name", ""):
            self._edit_schedule_from_upcoming(m, n)
        )

        toggle_btn = QPushButton("Desactivar" if enabled else "Activar")
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.setFixedSize(66, 22)
        toggle_btn.setToolTip(
            "Desactiva la programación y detiene la ejecución en curso"
            if enabled else "Reactivar la programación"
        )
        if enabled:
            toggle_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(214,48,49,0.15);
                    color: #ff7675;
                    border: 1px solid rgba(214,48,49,0.35);
                    border-radius: 4px; font-size: 10px; font-weight: 600;
                    padding: 0 4px;
                }}
                QPushButton:hover {{ background: rgba(214,48,49,0.3); }}
            """)
        else:
            toggle_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(0,184,148,0.15);
                    color: {NevlanTheme.SUCCESS};
                    border: 1px solid rgba(0,184,148,0.35);
                    border-radius: 4px; font-size: 10px; font-weight: 600;
                    padding: 0 4px;
                }}
                QPushButton:hover {{ background: rgba(0,184,148,0.3); }}
            """)
        toggle_btn.clicked.connect(
            lambda _=False, m=mid: self._toggle_schedule_enabled(m)
        )

        delete_btn = QPushButton("🗑")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.setFixedSize(24, 22)
        delete_btn.setToolTip(
            "Quitar esta programación (no elimina la automatización)."
        )
        delete_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255,255,255,0.04);
                color: {NevlanTheme.TEXT_MUTED};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 4px; font-size: 12px;
                padding: 0;
            }}
            QPushButton:hover {{
                background: rgba(214,48,49,0.2);
                color: #ff7675;
                border-color: rgba(214,48,49,0.4);
            }}
        """)
        delete_btn.clicked.connect(
            lambda _=False, m=mid, n=item.get("name", ""): self._remove_schedule(m, n)
        )

        row2 = QHBoxLayout()
        row2.setContentsMargins(17, 0, 0, 0)
        row2.setSpacing(4)
        row2.addWidget(run_btn)
        row2.addWidget(edit_btn)
        row2.addWidget(toggle_btn)
        row2.addWidget(delete_btn)
        row2.addStretch()
        el.addLayout(row2)

        return entry

    def _remove_schedule(self, mission_id: str, mission_name: str = ""):
        """Borra SOLO la programación de una misión (no la misión)."""
        if not mission_id or not self._scheduler:
            return
        resp = QMessageBox.question(
            self, "Quitar programación",
            f"¿Eliminar la programación de '{mission_name or mission_id}'?\n\n"
            "La automatización y su historial se conservan. Solo dejará de "
            "ejecutarse automáticamente.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            self._scheduler.remove_schedule(mission_id)
            self.status_lbl.setText(f"🗑 Programación de '{mission_name}' eliminada")
            self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 12px; font-weight: 600;"
            )
            QTimer.singleShot(2200, lambda: self.status_lbl.setText("● Listo"))
            QTimer.singleShot(2200, lambda: self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 500;"
            ))
        except Exception as e:
            log.error(f"remove_schedule fallo: {e}")
        self._refresh_upcoming_panel()
        self._refresh_next_exec()

    def _toggle_schedule_enabled(self, mission_id: str):
        """Alterna activa/inactiva para una programación y refresca el panel.
        Si estamos DESACTIVANDO y la misión está ejecutándose justo ahora
        como parte de la programación, `toggle_enabled` ya le pide stop al
        player internamente — aquí sólo reflejamos el cambio en la UI."""
        if not mission_id or not self._scheduler:
            return
        try:
            new_state = self._scheduler.toggle_enabled(mission_id)
            name = ""
            sched = self._scheduler.get_schedule(mission_id)
            if sched is not None:
                name = sched.mission_name
            if new_state:
                msg = f"Programación de '{name}' → ACTIVA"
            else:
                msg = f"⏹ Programación de '{name}' DESACTIVADA (se detuvo ejecución en curso)"
            self.status_lbl.setText(msg)
            self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.SUCCESS if new_state else NevlanTheme.TEXT_MUTED};"
                " font-size: 12px; font-weight: 600;"
            )
            QTimer.singleShot(2800, lambda: self.status_lbl.setText("● Listo"))
            QTimer.singleShot(2800, lambda: self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 500;"
            ))
        except Exception as e:
            log.error(f"Toggle schedule fallo: {e}")
        # Reconstruir el panel y el indicador superior con el nuevo estado.
        self._refresh_upcoming_panel()
        self._refresh_next_exec()

    def _edit_schedule_from_upcoming(self, mission_id: str, mission_name: str = ""):
        """Abre el diálogo 'Programar' directamente desde una entrada de
        Próximas ejecuciones, sin exigir al usuario seleccionar la misión
        antes en el sidebar. Al guardar, refrescamos el panel para reflejar
        el nuevo horario y los contadores regresivos."""
        if not mission_id or not self._scheduler:
            return
        try:
            # Preferimos el nombre actualizado del scheduler por si la misión
            # fue renombrada.
            sched = self._scheduler.get_schedule(mission_id)
            display_name = (sched.mission_name if sched else mission_name) or mission_id
            dialog = ScheduleDialog(display_name, mission_id, self)
            if dialog.exec():
                # El diálogo ya persistió los cambios en el scheduler.
                # Solo refrescamos la UI dependiente.
                self._refresh_upcoming_panel()
                self._refresh_next_exec()
                self.status_lbl.setText(
                    f"📅 Programación de '{display_name}' actualizada"
                )
                self.status_lbl.setStyleSheet(
                    f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 600;"
                )
                QTimer.singleShot(2500, lambda: self.status_lbl.setText("● Listo"))
                QTimer.singleShot(2500, lambda: self.status_lbl.setStyleSheet(
                    f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 500;"
                ))
        except Exception as e:
            log.error(f"_edit_schedule_from_upcoming falló: {e}")
            from app.interfaces.desktop.nevlan_dialog import show_error
            show_error(
                self,
                title="No se pudo abrir el editor de programación",
                message=(
                    "Hubo un problema al cargar la pantalla de "
                    "programación. Reintenta o reinicia el módulo."
                ),
                expert_details=f"{type(e).__name__}: {e}",
            )

    def _run_scheduled_mission_now(self, mission_id: str, mission_name: str = ""):
        """Ejecuta AHORA una misión programada, tal como lo haría el botón
        '▶ Ejecutar' principal, pero:

        - Funciona desde el panel 'Próximas ejecuciones' sin tener que
          seleccionar antes la automatización en el sidebar.
        - NO toca el `last_run` del trigger: el siguiente disparo programado
          se mantiene exactamente en la misma hora que iba a ocurrir, es
          decir, el orden de los minutos pendientes NO cambia.
        - Usa `MissionPlayer` + widget flotante de ejecución (pausa /
          reanudar / cancelar), igual que el botón principal.
        """
        if not mission_id:
            return
        import threading, time as _time

        from app.interfaces.desktop.nevlan_dialog import (
            show_error, show_warning,
        )
        try:
            from app.services.missions.store import mission_store
            mission = mission_store.load(mission_id)
        except Exception as e:
            log.error(f"_run_scheduled_mission_now: no se pudo cargar '{mission_id}': {e}")
            show_error(
                self,
                title="No se pudo ejecutar la automatización",
                message=(
                    f"No se pudo cargar la automatización "
                    f"'{mission_name or mission_id}'."
                ),
                expert_details=f"{type(e).__name__}: {e}",
            )
            return
        if not mission:
            show_error(
                self,
                title="No se pudo ejecutar la automatización",
                message=(
                    f"La automatización '{mission_name or mission_id}' "
                    "ya no existe en el almacén."
                ),
            )
            self._refresh_upcoming_panel()
            return

        # Si ya hay una ejecución en marcha, evitamos pisarla.
        existing = getattr(self, "_execution_widget", None)
        if existing is not None:
            show_warning(
                self,
                title="Ya hay una ejecución en curso",
                message=(
                    "Espera a que termine (o cancélala desde el panel "
                    "flotante) antes de lanzar otra."
                ),
                primary="Entendido",
                cancel="",
            )
            return

        # PRD 2026-05-03d §1: incluso las ejecuciones programadas se
        # rutean por el bridge inteligente. Scheduled missions heredan
        # el mismo gate y la misma lógica Smart/Legacy que el botón
        # manual.
        from app.services.missions.smart_runner_bridge import decide_player
        try:
            decision = decide_player(mission)
        except Exception as e:
            log.exception(f"scheduled decide_player: {e}")
            decision = "blocked"

        if decision == "blocked":
            self._handle_execution_blocked(mission)
            return

        if decision == "smart":
            self._run_mission_with_smart_executor(mission)
        else:
            # Scheduled runs con misiones legacy: NO preguntamos (no hay
            # usuario delante). Logueamos la advertencia y seguimos.
            log.warning(
                f"[Scheduled·Legacy] '{mission.name}' usa el ejecutor "
                "antiguo; puede ser menos precisa."
            )
            self._run_mission_with_legacy_player(mission, legacy_warning=False)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ACTIONS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _select_automation(self, mission):
        # Aviso si hay cambios sin guardar en la misión actual
        if (getattr(self, "_dirty", False)
                and self._selected_mission
                and self._selected_mission is not mission):
            resp = QMessageBox.question(
                self, "Cambios sin guardar",
                f"Tienes cambios sin guardar en '{self._selected_mission.name}'."
                "\n¿Descartarlos y abrir la otra automatización?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return

        self._selected_mission = mission
        # Reset estado de edición al cambiar de misión
        self._dirty = False
        self._undo_stack = []
        self._redo_stack = []
        self.detail_name_edit.blockSignals(True)
        self.detail_name_edit.setText(mission.name)
        self.detail_name_edit.blockSignals(False)
        self._refresh_edit_buttons()
        self._clear_dirty()
        self._sync_fav_button()

        # Mission Truth Gate: el header debe reflejar la verdad del SEP.
        steps = _truth_visible_step_count(mission)
        self.detail_status.setText(mission_ui_detail_status_line(mission))
        self.detail_steps_lbl.setText("Pasos de la automatización")
        if hasattr(self, "step_count_badge"):
            self.step_count_badge.setText(f"{steps} pasos")
        self._sync_detail_execution_action_state()

        self._rebuild_step_cards()
        self.center_stack.setCurrentIndex(1)

    def _sync_detail_execution_action_state(self) -> None:
        """▶ Ejecutar respeta ``is_executable`` + workflow, no ``mission.status``."""
        btn = getattr(self, "detail_execute_btn", None)
        if btn is None or not getattr(self, "_selected_mission", None):
            return
        m = self._selected_mission
        ok = mission_ui_should_offer_execute_and_test(m)
        btn.setEnabled(ok)
        if ok:
            btn.setToolTip("Ejecutar la automatización ahora")
            return
        st = compute_mission_ui_execution_state(m)
        parts = [self._humanize_blocker_code(c) for c in st.visible_blockers[:4]]
        tip = "No ejecutable todavía."
        if parts:
            tip += " " + " · ".join(parts)
        elif st.gate_blockers:
            tip += f" ({st.gate_blockers[0]})"
        btn.setToolTip(tip)

    # Lazy rendering: para automatizaciones grandes (200+ pasos),
    # renderizamos por bloques para mantener el scroll fluido.
    _LAZY_CHUNK = 40

    def _rebuild_step_cards(self):
        """Rebuild interactive step cards inline con lazy rendering."""
        while self.detail_steps_layout.count() > 0:
            item = self.detail_steps_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._selected_mission:
            return

        self._rendered_count = 0
        self._load_more_btn = None
        self._render_next_chunk()

        # Conectar scroll al lazy load (una sola vez)
        try:
            bar = self.detail_steps_area.verticalScrollBar()
            if not getattr(self, "_scroll_conn", False):
                bar.valueChanged.connect(self._on_steps_scroll)
                self._scroll_conn = True
        except Exception:
            pass

    def _on_steps_scroll(self, value):
        """Si llegamos al final del scroll, auto-carga más pasos."""
        bar = self.detail_steps_area.verticalScrollBar()
        if bar.maximum() == 0:
            return
        if value >= bar.maximum() - 60:
            # Mission Truth Gate: el total de pasos visibles viene del gate.
            if self._selected_mission and self._rendered_count < _truth_visible_step_count(self._selected_mission):
                self._render_next_chunk()

    def _render_next_chunk(self):
        """Agrega el siguiente bloque de tarjetas sin destruir las existentes.

        Mission Truth Gate (PRD 2026-05-10e):

          * Si el SEP existe → renderizamos las tarjetas desde
            ``step_card_models(mission)`` (vista canónica). Incluyen
            🧪 «Probar paso» ejecutando ese índice vía Smart Executor.
            La edición inline (mover, insertar, eliminar) sigue en vista
            experto / Mission Review.
          * Si NO hay SEP → fallback legacy con ``compiled_execution_graph``
            + ``interpreted_steps`` (misiones viejas pre-gate).
        """
        if not self._selected_mission:
            return

        # quitar el stretch / botón "cargar más" si está al final
        for _ in range(2):
            if self.detail_steps_layout.count() == 0:
                break
            last = self.detail_steps_layout.itemAt(self.detail_steps_layout.count() - 1)
            if last is None:
                break
            w = last.widget()
            if w is None or (self._load_more_btn is not None and w is self._load_more_btn):
                self.detail_steps_layout.takeAt(self.detail_steps_layout.count() - 1)
                if w is not None:
                    w.deleteLater()
                if w is self._load_more_btn:
                    self._load_more_btn = None
            else:
                break

        if _truth_has_semantic_view(self._selected_mission):
            # Vista normal canónica: SEP es la única fuente de pasos visibles.
            models = _truth_step_card_models(self._selected_mission, expert=False)
            blockers = _truth_ui_blockers(self._selected_mission)

            if self._rendered_count == 0 and blockers:
                self.detail_steps_layout.addWidget(
                    self._build_blocker_banner(blockers)
                )

            total = len(models)
            start = self._rendered_count
            end = min(start + self._LAZY_CHUNK, total)
            for i in range(start, end):
                self.detail_steps_layout.addWidget(
                    self._build_truth_step_card(models[i])
                )
            self._rendered_count = end
        else:
            # Misión legacy sin SEP: fallback al render anterior para que
            # JSONs viejos (pre-gate) sigan visibles.
            steps = self._selected_mission.compiled_execution_graph
            interp = self._selected_mission.interpreted_steps
            total = len(steps)
            start = self._rendered_count
            end = min(start + self._LAZY_CHUNK, total)

            for i in range(start, end):
                c_step = steps[i]
                desc = interp[i].description if i < len(interp) else "Paso"
                self.detail_steps_layout.addWidget(self._build_step_card(i, c_step, desc))

            self._rendered_count = end

        if end < total:
            # Botón de carga manual
            remaining = total - end
            self._load_more_btn = QPushButton(f"⬇ Cargar {min(self._LAZY_CHUNK, remaining)} más  ·  {remaining} restantes")
            self._load_more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._load_more_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255,255,255,0.04); color: {NevlanTheme.TEXT_SECONDARY};
                    border: 1px dashed {NevlanTheme.BORDER}; border-radius: 6px;
                    padding: 8px; font-size: 11px;
                }}
                QPushButton:hover {{ background: rgba(255,255,255,0.08); color: {NevlanTheme.TEXT_PRIMARY}; }}
            """)
            self._load_more_btn.clicked.connect(self._render_next_chunk)
            self.detail_steps_layout.addWidget(self._load_more_btn)

        self.detail_steps_layout.addStretch()

    def _build_step_card(self, i: int, c_step, desc: str) -> QFrame:
        """Construye una tarjeta de paso individual (lazy rendering friendly).

        IMPORTANTE (Mission Truth Gate):
        Esta tarjeta solo se renderiza para misiones legacy SIN
        ``semantic_execution_plan`` (JSONs viejos pre-gate). Para
        cualquier misión con SEP la lista normal usa
        ``_build_truth_step_card`` y los pasos legacy quedan
        confinados a regrabación / vista experto.
        """
        from app.interfaces.desktop.mission_review import STRAT_ICONS, MINI_BTN
        try:
            from app.services.missions.vibe_parser import describe_hotkey_payload
        except Exception:
            describe_hotkey_payload = None

        action_strat = getattr(c_step.action_strategy, 'value', str(c_step.action_strategy))

        card = QFrame()
        card.setObjectName(f"stepCard_{i}")
        card.setStyleSheet(f"""
            QFrame#stepCard_{i} {{
                background: rgba(255,255,255,0.03);
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 6px;
            }}
            QFrame#stepCard_{i}:hover {{
                background: rgba(255,255,255,0.05);
                border-color: rgba(9,132,227,0.2);
            }}
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 6, 10, 6)
        card_layout.setSpacing(3)

        row1 = QHBoxLayout()
        row1.setSpacing(4)

        num_lbl = QLabel(f"{i+1}")
        num_lbl.setFixedWidth(26)
        num_lbl.setStyleSheet(
            f"color: {NevlanTheme.ACCENT}; font-weight: bold; font-size: 11px;"
            " border: none; background: transparent;"
        )
        num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row1.addWidget(num_lbl)

        # Flechas de reordenar
        up_btn = QPushButton("↑")
        up_btn.setFixedSize(20, 24)
        up_btn.setToolTip("Subir paso")
        up_btn.setStyleSheet(MINI_BTN.format(
            bg="rgba(255,255,255,0.05)", fg=NevlanTheme.TEXT_SECONDARY,
            border="rgba(255,255,255,0.1)", hover="rgba(9,132,227,0.25)"))
        up_btn.clicked.connect(lambda _=False, idx=i: self._move_step(idx, -1))
        if i == 0:
            up_btn.setEnabled(False)
        row1.addWidget(up_btn)

        down_btn = QPushButton("↓")
        down_btn.setFixedSize(20, 24)
        down_btn.setToolTip("Bajar paso")
        down_btn.setStyleSheet(MINI_BTN.format(
            bg="rgba(255,255,255,0.05)", fg=NevlanTheme.TEXT_SECONDARY,
            border="rgba(255,255,255,0.1)", hover="rgba(9,132,227,0.25)"))
        down_btn.clicked.connect(lambda _=False, idx=i: self._move_step(idx, +1))
        # Nota: el estado disabled "es último" se reconstruye entero en cada cambio.
        if self._selected_mission and i >= len(self._selected_mission.compiled_execution_graph) - 1:
            down_btn.setEnabled(False)
        row1.addWidget(down_btn)

        # Insertar paso después de este
        ins_btn = QPushButton("⤵")
        ins_btn.setFixedSize(22, 24)
        ins_btn.setToolTip("Insertar un paso después de este")
        ins_btn.setStyleSheet(MINI_BTN.format(
            bg="rgba(9,132,227,0.12)", fg="#74b9ff",
            border="rgba(9,132,227,0.25)", hover="rgba(9,132,227,0.35)"))
        ins_btn.clicked.connect(lambda _=False, idx=i: self._add_step_at(idx + 1))
        row1.addWidget(ins_btn)

        desc_edit = QLineEdit(desc)
        desc_edit.setStyleSheet(f"""
            color: {NevlanTheme.TEXT_PRIMARY}; font-size: 11px;
            background: rgba(0,0,0,0.2);
            border: 1px solid {NevlanTheme.BORDER};
            border-radius: 3px; padding: 2px 6px;
        """)
        desc_edit.setFixedHeight(24)
        desc_edit.textChanged.connect(lambda text, idx=i: self._update_step_desc(idx, text))
        row1.addWidget(desc_edit)

        icon = STRAT_ICONS.get(action_strat, "⚙️")
        badge = QLabel(icon)
        badge.setFixedWidth(20)
        badge.setStyleSheet("font-size: 12px; border: none; background: transparent;")
        badge.setToolTip(action_strat)
        row1.addWidget(badge)

        vibe_btn = QPushButton("✨")
        vibe_btn.setFixedSize(24, 24)
        vibe_btn.setToolTip("Editar con IA (Vibe)")
        vibe_btn.setStyleSheet(MINI_BTN.format(
            bg="rgba(162,155,254,0.15)", fg="#a29bfe",
            border="rgba(162,155,254,0.2)", hover="rgba(162,155,254,0.35)"))
        vibe_btn.clicked.connect(lambda checked, idx=i: self._vibe_edit_step(idx))
        row1.addWidget(vibe_btn)

        rec_btn = QPushButton("🎯")
        rec_btn.setFixedSize(24, 24)
        rec_btn.setToolTip("Regrabar paso (inline)")
        rec_btn.setStyleSheet(MINI_BTN.format(
            bg="rgba(255,165,0,0.15)", fg="#ffa500",
            border="rgba(255,165,0,0.2)", hover="rgba(255,165,0,0.35)"))
        rec_btn.clicked.connect(lambda checked, idx=i: self._rerecord_step(idx))
        row1.addWidget(rec_btn)

        test_btn = QPushButton("🧪")
        test_btn.setFixedSize(24, 24)
        test_btn.setToolTip("Probar paso")
        test_btn.setStyleSheet(MINI_BTN.format(
            bg="rgba(0,206,201,0.15)", fg="#00cec9",
            border="rgba(0,206,201,0.2)", hover="rgba(0,206,201,0.35)"))
        test_btn.clicked.connect(lambda checked, idx=i: self._test_step(idx))
        row1.addWidget(test_btn)

        del_btn = QPushButton("✕")
        del_btn.setFixedSize(24, 24)
        del_btn.setToolTip("Eliminar paso")
        del_btn.setStyleSheet(MINI_BTN.format(
            bg="rgba(255,0,0,0.1)", fg="#ff7675",
            border="rgba(255,0,0,0.15)", hover="rgba(255,0,0,0.3)"))
        del_btn.clicked.connect(lambda checked, idx=i: self._delete_step(idx))
        row1.addWidget(del_btn)

        card_layout.addLayout(row1)

        # ── Row 2: preview legible ──
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
            if describe_hotkey_payload:
                dsl_parts.append(f"→ {describe_hotkey_payload(c_step.action_payload)}")
            else:
                dsl_parts.append(f"→ {c_step.action_payload.get('hotkey', '?')}")
        elif action_strat == "agent_prompt":
            p = c_step.action_payload.get("text", "")
            dsl_parts.append(f'→ "{p[:20]}{"…" if len(p) > 20 else ""}"')

        dsl_lbl = QLabel(" · ".join(dsl_parts))
        dsl_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 9px;"
            " font-family: 'Consolas', monospace;"
            " border: none; background: transparent; padding-left: 30px;"
        )
        card_layout.addWidget(dsl_lbl)

        return card

    # ──────────────────────────────────────────────────────────────────
    # Mission Truth Gate — vista normal canónica
    # ──────────────────────────────────────────────────────────────────

    _TRUTH_KIND_ICONS = {
        "open_app": "🚀",
        "open_url": "🌐",
        "open_new_tab": "➕",
        "select_profile": "👤",
        "search_youtube": "🔍",
        "search_google": "🔎",
        "scroll_results": "↕️",
        "type_text": "⌨️",
        "click_element": "🎯",
    }

    def _build_truth_step_card(self, model: dict) -> QFrame:
        """Tarjeta de paso derivada del Mission Truth Gate.

        Muestra ``human_label`` + ``kind`` + ``needs_user_label`` y 🧪
        para probar ese paso del SEP (Smart Executor). La reordenación
        inline y el grafo legacy siguen en flujos de edición separados.
        """
        i = int(model.get("index", 0))
        kind = str(model.get("kind") or "")
        label = str(model.get("human_label") or kind or "Paso")
        needs_review = bool(model.get("needs_user_label"))
        if (
            getattr(self, "_selected_mission", None) is not None
            and _truth_is_executable(self._selected_mission)
        ):
            needs_review = False
        prompt = str(model.get("label_prompt") or "")

        card = QFrame()
        card.setObjectName(f"truthStepCard_{i}")
        card.setProperty("nevlan_step_source", str(model.get("source") or ""))
        card.setStyleSheet(f"""
            QFrame#truthStepCard_{i} {{
                background: rgba(255,255,255,0.03);
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 6px;
            }}
            QFrame#truthStepCard_{i}:hover {{
                background: rgba(255,255,255,0.05);
                border-color: rgba(9,132,227,0.2);
            }}
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 6, 10, 6)
        card_layout.setSpacing(3)

        row = QHBoxLayout()
        row.setSpacing(6)

        num_lbl = QLabel(f"{i + 1}")
        num_lbl.setFixedWidth(26)
        num_lbl.setStyleSheet(
            f"color: {NevlanTheme.ACCENT}; font-weight: bold; font-size: 11px;"
            " border: none; background: transparent;"
        )
        num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(num_lbl)

        icon_lbl = QLabel(self._TRUTH_KIND_ICONS.get(kind, "•"))
        icon_lbl.setFixedWidth(22)
        icon_lbl.setStyleSheet(
            "font-size: 13px; border: none; background: transparent;"
        )
        icon_lbl.setToolTip(kind or "paso semántico")
        row.addWidget(icon_lbl)

        text_lbl = QLabel(label)
        text_lbl.setStyleSheet(
            f"color: {NevlanTheme.TEXT_PRIMARY}; font-size: 12px;"
            " border: none; background: transparent;"
        )
        text_lbl.setWordWrap(True)
        row.addWidget(text_lbl, 1)

        from app.interfaces.desktop.mission_review import MINI_BTN

        test_btn = QPushButton("🧪")
        test_btn.setFixedSize(24, 24)
        test_btn.setToolTip("Probar este paso (según plan semántico)")
        test_btn.setStyleSheet(MINI_BTN.format(
            bg="rgba(0,206,201,0.15)", fg="#00cec9",
            border="rgba(0,206,201,0.2)", hover="rgba(0,206,201,0.35)"))
        test_btn.clicked.connect(lambda checked, idx=i: self._test_semantic_step(idx))
        row.addWidget(test_btn)

        if needs_review:
            pill = QLabel("⚠ Necesita aclaración")
            pill.setStyleSheet(
                f"color: {NevlanTheme.WARNING};"
                " background: rgba(255,165,0,0.12);"
                f" border: 1px solid rgba(255,165,0,0.25);"
                " border-radius: 4px; padding: 1px 6px; font-size: 10px;"
            )
            pill.setToolTip(prompt or "Falta etiqueta humana confirmada.")
            row.addWidget(pill)

        card_layout.addLayout(row)

        sub_parts = [kind] if kind else []
        strat = str(model.get("preferred_strategy") or "")
        if strat:
            sub_parts.append(strat)
        if sub_parts:
            sub_lbl = QLabel(" · ".join(sub_parts))
            sub_lbl.setStyleSheet(
                f"color: {NevlanTheme.TEXT_MUTED}; font-size: 9px;"
                " font-family: 'Consolas', monospace;"
                " border: none; background: transparent; padding-left: 48px;"
            )
            card_layout.addWidget(sub_lbl)

        return card

    def _build_blocker_banner(self, blockers: list) -> QFrame:
        """Banner visible cuando el gate dice ``NEEDS_REVIEW``.

        Sustituye a la lista legacy (no enseñamos GroupControl/
        use_coords/capture_score 0 en vista normal).
        """
        banner = QFrame()
        banner.setObjectName("nevlanBlockerBanner")
        banner.setStyleSheet(f"""
            QFrame#nevlanBlockerBanner {{
                background: rgba(255,107,107,0.08);
                border: 1px solid rgba(255,107,107,0.30);
                border-radius: 6px;
            }}
        """)
        layout = QVBoxLayout(banner)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        title = QLabel("⚠ Necesita aclaración")
        title.setStyleSheet(
            f"color: {NevlanTheme.ERROR}; font-weight: bold; font-size: 12px;"
            " border: none; background: transparent;"
        )
        layout.addWidget(title)

        for b in blockers[:6]:
            item = QLabel(f"• {b}")
            item.setStyleSheet(
                f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 10px;"
                " font-family: 'Consolas', monospace;"
                " border: none; background: transparent;"
            )
            layout.addWidget(item)

        hint = QLabel(
            "Nevlan no muestra los pasos legacy aquí porque "
            "no son ejecutables. Usa la vista de revisión para "
            "completar la etiqueta o regrabar."
        )
        hint.setStyleSheet(
            f"color: {NevlanTheme.TEXT_MUTED}; font-size: 10px;"
            " border: none; background: transparent;"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        return banner

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # INLINE STEP EDITING
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _update_step_desc(self, index, new_text):
        if self._selected_mission and index < len(self._selected_mission.interpreted_steps):
            if self._selected_mission.interpreted_steps[index].description != new_text:
                # Sólo empujamos undo la primera vez (al empezar a escribir);
                # no contaminamos el stack con un snapshot por letra.
                if not getattr(self, "_dirty", False):
                    self._push_undo()
                self._selected_mission.interpreted_steps[index].description = new_text
                self._mark_dirty()

    def _auto_save(self):
        from app.services.missions import mission_store
        if self._selected_mission:
            mission_store.save(self._selected_mission)
            log.debug("Auto-guardado.")
        # Las operaciones que llaman a _auto_save ya dejan los cambios en
        # disco — limpiamos el flag.
        self._clear_dirty()

    def _relink_graph(self):
        if not self._selected_mission:
            return
        steps = self._selected_mission.compiled_execution_graph
        for i in range(len(steps) - 1):
            steps[i].next_step_id_on_success = steps[i+1].id
        if steps:
            steps[-1].next_step_id_on_success = None
        try:
            from app.services.missions.compiler import MissionCompiler
            MissionCompiler.rebuild_action_groups(self._selected_mission)
        except Exception as e:
            log.debug(f"rebuild_action_groups: {e}")

    # ─────────────────────────────────────────────────────────────
    #   Patrón DIRTY + UNDO/CANCEL
    # ─────────────────────────────────────────────────────────────
    #   Cada modificación (reorden, agregar, borrar, rename, vibe,
    #   re-record, toggle, recompilar) DEBE:
    #     1. llamar _push_undo() ANTES del cambio (serializa la misión)
    #     2. hacer el cambio en memoria
    #     3. llamar _mark_dirty()
    #   La persistencia sólo ocurre si el usuario da "Guardar".
    # ─────────────────────────────────────────────────────────────

    _UNDO_STACK_MAX = 25

    def _snapshot_mission(self):
        """Devuelve una copia profunda serializable de la misión actual."""
        if not self._selected_mission:
            return None
        try:
            return self._selected_mission.model_dump(mode="json")
        except Exception as e:
            log.debug(f"snapshot failed: {e}")
            return None

    def _push_undo(self):
        """Empuja el estado actual al stack antes de aplicar un cambio."""
        if not self._selected_mission:
            return
        if not hasattr(self, "_undo_stack"):
            self._undo_stack = []
        if not hasattr(self, "_redo_stack"):
            self._redo_stack = []
        snap = self._snapshot_mission()
        if snap is None:
            return
        self._undo_stack.append(snap)
        # Cap tamaño
        if len(self._undo_stack) > self._UNDO_STACK_MAX:
            self._undo_stack.pop(0)
        # Cualquier cambio NUEVO invalida la rama de redo (comportamiento
        # estándar de los editores). Si el usuario hizo Deshacer y después
        # decide editar otra cosa, ya no podrá rehacer la rama anterior.
        self._redo_stack = []
        self._refresh_edit_buttons()

    def _clear_undo_stack(self):
        self._undo_stack = []
        self._redo_stack = []
        self._refresh_edit_buttons()

    def _refresh_edit_buttons(self):
        """Habilita/deshabilita los botones según estado actual."""
        has_undo = bool(getattr(self, "_undo_stack", []))
        has_redo = bool(getattr(self, "_redo_stack", []))
        dirty = bool(getattr(self, "_dirty", False))
        if hasattr(self, "undo_changes_btn"):
            self.undo_changes_btn.setEnabled(has_undo)
        if hasattr(self, "redo_changes_btn"):
            self.redo_changes_btn.setEnabled(has_redo)
        if hasattr(self, "cancel_changes_btn"):
            self.cancel_changes_btn.setEnabled(dirty or has_undo or has_redo)
        if hasattr(self, "save_changes_btn"):
            self.save_changes_btn.setEnabled(dirty)

    def _mark_dirty(self):
        """Marca que hay cambios sin guardar."""
        self._dirty = True
        if hasattr(self, "save_changes_btn"):
            self.save_changes_btn.setText("💾 Guardar cambios •")
        if hasattr(self, "status_lbl"):
            self.status_lbl.setText("● Cambios sin guardar")
            self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
            )
        self._refresh_edit_buttons()

    def _clear_dirty(self):
        self._dirty = False
        if hasattr(self, "save_changes_btn"):
            self.save_changes_btn.setText("💾 Guardar cambios")
        if hasattr(self, "status_lbl"):
            self.status_lbl.setText("● Listo")
            self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 500;"
            )
        self._refresh_edit_buttons()

    def _save_pending_changes(self):
        """Persiste cambios manuales (reorden, desc, rename) y propaga el
        nombre a scheduler/tracker para que 'Próximas ejecuciones' y
        'Actividad reciente' queden coherentes."""
        if not self._selected_mission:
            return
        from app.services.missions import mission_store
        self._relink_graph()
        mission_store.save(self._selected_mission)
        self._propagate_mission_rename()
        self._clear_dirty()
        self._clear_undo_stack()
        self.status_lbl.setText("✅ Cambios guardados")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 600;"
        )
        QTimer.singleShot(2000, lambda: self.status_lbl.setText("● Listo"))
        log.info(f"Cambios guardados en '{self._selected_mission.name}'")

    def _propagate_mission_rename(self):
        """Refleja el nombre actual de la misión en scheduler, tracker, sidebar
        y paneles laterales. Idempotente: si el nombre no cambió no hace nada
        visible. Pensado para llamarse después de cualquier guardado.
        """
        if not self._selected_mission:
            return
        mid = getattr(self._selected_mission, "id", None)
        name = getattr(self._selected_mission, "name", None)
        if not mid or not name:
            return
        # Scheduler: nombre que aparece en "Próximas ejecuciones"
        try:
            if self._scheduler:
                self._scheduler.update_mission_name(mid, name)
        except Exception as e:
            log.debug(f"propagate name → scheduler: {e}")
        # Tracker: nombres que aparecen en "Actividad reciente"
        try:
            if self._tracker and hasattr(self._tracker, "update_mission_name"):
                self._tracker.update_mission_name(mid, name)
        except Exception as e:
            log.debug(f"propagate name → tracker: {e}")
        # Sincronizar la lista cacheada en memoria para que el sidebar refleje
        # el nuevo nombre sin tener que ir a disco. Esto elimina el delay
        # perceptible al renombrar.
        try:
            for i, m in enumerate(self._all_missions):
                if getattr(m, "id", None) == mid:
                    self._all_missions[i].name = name
                    break
        except Exception:
            pass
        # Refrescar TODOS los lugares donde aparece el nombre de la misión.
        try:
            self.refresh_sidebar()
            self._refresh_upcoming_panel()
            self._refresh_activity_panel()
            self._refresh_next_exec()
            # También el header del detalle y el título de la ventana, por si
            # vienen reflejando el nombre anterior.
            if hasattr(self, "detail_name_edit") and self.detail_name_edit.text() != name:
                self.detail_name_edit.setText(name)
        except Exception:
            pass

    def _undo_last_change(self):
        """Revierte el último cambio (pop del stack y restaura).

        Antes de restaurar, snapshotea el estado ACTUAL y lo empuja al
        ``_redo_stack`` para que el botón Rehacer pueda devolver al
        estado previo al Deshacer.
        """
        if not getattr(self, "_undo_stack", None):
            return
        if not self._selected_mission:
            return
        try:
            from app.contracts.mission import Mission
            # Snapshot del estado actual para poder rehacer.
            current_snap = self._snapshot_mission()
            if current_snap is not None:
                if not hasattr(self, "_redo_stack"):
                    self._redo_stack = []
                self._redo_stack.append(current_snap)
                if len(self._redo_stack) > self._UNDO_STACK_MAX:
                    self._redo_stack.pop(0)
            snap = self._undo_stack.pop()
            restored = Mission.model_validate(snap)
            # Mutamos en sitio para no perder la referencia que tienen
            # otras partes de la UI.
            self._selected_mission.__dict__.update(restored.__dict__)
        except Exception as e:
            log.error(f"undo failed: {e}")
            return

        # Si aún quedan cambios, seguimos dirty. Si vaciamos el stack,
        # el estado es idéntico al último guardado → limpiar dirty.
        if not self._undo_stack:
            self._clear_dirty()
        else:
            self._mark_dirty()

        # Refresh de UI
        if hasattr(self, "detail_name_edit"):
            self.detail_name_edit.blockSignals(True)
            self.detail_name_edit.setText(self._selected_mission.name or "")
            self.detail_name_edit.blockSignals(False)
        self._rebuild_step_cards()
        self._update_step_count()
        self._refresh_edit_buttons()
        self.status_lbl.setText("↶ Cambio revertido")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.ACCENT}; font-size: 12px; font-weight: 600;"
        )
        QTimer.singleShot(1500, lambda: self.status_lbl.setText(
            "● Cambios sin guardar" if self._dirty else "● Listo"
        ))

    def _redo_last_change(self):
        """Vuelve a aplicar el último cambio deshecho.

        Hace el inverso de :py:meth:`_undo_last_change`: pop del
        ``_redo_stack``, snapshot del estado actual al ``_undo_stack``
        para que el usuario pueda volver a deshacer, y restaura.
        """
        if not getattr(self, "_redo_stack", None):
            return
        if not self._selected_mission:
            return
        try:
            from app.contracts.mission import Mission
            current_snap = self._snapshot_mission()
            if current_snap is not None:
                if not hasattr(self, "_undo_stack"):
                    self._undo_stack = []
                self._undo_stack.append(current_snap)
                if len(self._undo_stack) > self._UNDO_STACK_MAX:
                    self._undo_stack.pop(0)
            snap = self._redo_stack.pop()
            restored = Mission.model_validate(snap)
            self._selected_mission.__dict__.update(restored.__dict__)
        except Exception as e:
            log.error(f"redo failed: {e}")
            return

        # Si llegamos a un estado distinto del guardado (lo normal tras
        # rehacer), marcamos dirty. _mark_dirty refresca botones.
        self._mark_dirty()

        if hasattr(self, "detail_name_edit"):
            self.detail_name_edit.blockSignals(True)
            self.detail_name_edit.setText(self._selected_mission.name or "")
            self.detail_name_edit.blockSignals(False)
        self._rebuild_step_cards()
        self._update_step_count()
        self._refresh_edit_buttons()
        self.status_lbl.setText("↷ Cambio rehecho")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.ACCENT}; font-size: 12px; font-weight: 600;"
        )
        QTimer.singleShot(1500, lambda: self.status_lbl.setText(
            "● Cambios sin guardar" if self._dirty else "● Listo"
        ))

    def _cancel_all_changes(self):
        """Descarta TODOS los cambios sin guardar, recargando desde disco."""
        if not self._selected_mission:
            return
        if not getattr(self, "_dirty", False) and not getattr(self, "_undo_stack", []):
            return
        resp = QMessageBox.question(
            self,
            "Cancelar cambios",
            "¿Seguro que quieres descartar TODOS los cambios sin guardar?\n"
            "Se recargará la automatización desde disco y no se puede deshacer.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            from app.services.missions import mission_store
            fresh = mission_store.get(self._selected_mission.id)
            if fresh is None:
                # La misión nunca se guardó. La dejamos como estaba pero
                # al menos vaciamos el stack de undo.
                self._clear_undo_stack()
                self._clear_dirty()
                return
            # Mutación in-place para preservar referencias
            self._selected_mission.__dict__.update(fresh.__dict__)
        except Exception as e:
            log.error(f"cancel changes failed: {e}")
            QMessageBox.critical(self, "Error",
                                 f"No se pudieron cancelar los cambios: {e}")
            return

        self._clear_undo_stack()
        self._clear_dirty()
        if hasattr(self, "detail_name_edit"):
            self.detail_name_edit.blockSignals(True)
            self.detail_name_edit.setText(self._selected_mission.name or "")
            self.detail_name_edit.blockSignals(False)
        self._rebuild_step_cards()
        self._update_step_count()
        self.status_lbl.setText("✕ Cambios descartados")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
        )
        QTimer.singleShot(1800, lambda: self.status_lbl.setText("● Listo"))

    # ── Reordenar pasos ────────────────────────────────────────
    def _move_step(self, index: int, delta: int):
        """Mueve un paso arriba (-1) o abajo (+1). No persiste hasta Guardar."""
        if not self._selected_mission:
            return
        steps = self._selected_mission.compiled_execution_graph
        interp = self._selected_mission.interpreted_steps
        new_idx = index + delta
        if index < 0 or index >= len(steps):
            return
        if new_idx < 0 or new_idx >= len(steps):
            return
        self._push_undo()
        steps[index], steps[new_idx] = steps[new_idx], steps[index]
        if index < len(interp) and new_idx < len(interp):
            interp[index], interp[new_idx] = interp[new_idx], interp[index]
        self._relink_graph()
        self._mark_dirty()
        self._rebuild_step_cards()
        self._update_step_count()

    # ── Agregar paso / Insertar paso ───────────────────────────
    def _add_step_at(self, insert_index: Optional[int]):
        """Abre un mini-diálogo para elegir tipo de paso y lo inserta.

        `insert_index = None` → agrega al final.
        `insert_index = k`    → inserta antes del paso k (después del k-1).
        """
        if not self._selected_mission:
            QMessageBox.information(self, "Sin automatización",
                                    "Primero selecciona una automatización.")
            return
        from app.interfaces.desktop.add_step_dialog import AddStepDialog
        dlg = AddStepDialog(self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        c_step, i_step = dlg.build_step()
        if c_step is None:
            return

        self._push_undo()

        steps = self._selected_mission.compiled_execution_graph
        interp = self._selected_mission.interpreted_steps

        if insert_index is None or insert_index >= len(steps):
            steps.append(c_step)
            interp.append(i_step)
        else:
            steps.insert(insert_index, c_step)
            if insert_index <= len(interp):
                interp.insert(insert_index, i_step)
            else:
                interp.append(i_step)

        self._relink_graph()
        # Cambio NO persistido: respetamos el patrón dirty. El usuario debe
        # dar "Guardar cambios" explícitamente — así puede deshacer o cancelar.
        self._mark_dirty()
        self._rebuild_step_cards()
        self._update_step_count()
        self.status_lbl.setText("✅ Paso agregado (sin guardar)")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
        )
        log.info(
            f"Paso agregado en {insert_index if insert_index is not None else 'final'}:"
            f" {c_step.action_strategy.value}"
        )

    def _vibe_edit_step(self, index):
        """Vibe Edit inline usando el servicio único `vibe_service`."""
        from PyQt6.QtWidgets import QInputDialog
        from app.services.missions.vibe_service import apply_vibe_edit

        if not self._selected_mission:
            return
        if index >= len(self._selected_mission.compiled_execution_graph):
            return

        i_step = (
            self._selected_mission.interpreted_steps[index]
            if index < len(self._selected_mission.interpreted_steps) else None
        )

        text, ok = QInputDialog.getText(
            self,
            f"✨ Vibe Edit — Paso {index + 1}",
            f"Paso actual: {i_step.description if i_step else '—'}\n\n"
            f"¿Qué cambio quieres hacer?\n"
            f"(Ej: 'ctrl+s', 'tab 3 veces', 'tab, enter', 'escribe hola')"
        )
        if not ok or not text:
            return

        def _confirm_agent() -> bool:
            res = QMessageBox.warning(
                self, "⚠ Convertir a Agent Prompt",
                "El paso actual es determinista. Convertirlo a agent_prompt "
                "lo hará menos confiable.\n\n¿Continuar?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return res == QMessageBox.StandardButton.Yes

        self._push_undo()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = apply_vibe_edit(
                self._selected_mission, index, text,
                on_confirm_agent=_confirm_agent,
            )
        finally:
            QApplication.restoreOverrideCursor()

        if result.get("ok"):
            self._mark_dirty()
            self._rebuild_step_cards()
            self._update_step_count()
            self.status_lbl.setText(
                f"✨ Vibe: {result.get('description', '')[:40]} (sin guardar)"
            )
            self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
            )
        else:
            # Si no se aplicó nada, revertimos el undo que empujamos
            if getattr(self, "_undo_stack", []):
                self._undo_stack.pop()
                self._refresh_edit_buttons()
            err = result.get("error") or "No se pudo aplicar."
            QMessageBox.information(self, "Vibe", err)

    def _rerecord_step(self, index):
        """Regrabación de un paso desde "Pasos de la automatización".

        PRD 2026-05-09 §G — **Centralización**: cuando la misión tiene
        ``semantic_execution_plan`` delegamos al MissionReviewDialog
        (mismo flujo central RerecordOrchestrator + RerecordOverlay
        que usan Mission Review y Reparar pasos débiles). Eso garantiza
        que regrabar el mismo paso desde cualquier botón produzca el
        mismo resultado, incluyendo el replay previo automático.

        Si la misión NO tiene ``semantic_execution_plan`` (misiones
        legacy) caemos al flujo inline original — sigue operativo y
        marca dirty para que el usuario decida cuándo persistir.
        """
        if not self._selected_mission:
            return
        if index >= len(self._selected_mission.compiled_execution_graph):
            return

        # ── Camino central: requiere semantic_execution_plan ─────
        plan_blob = (
            getattr(self._selected_mission, "semantic_execution_plan", None)
            or {}
        )
        plan_steps = list(plan_blob.get("steps") or [])
        if plan_steps and 0 <= index < len(plan_steps):
            try:
                self._delegate_rerecord_to_review(index)
                return
            except Exception as e:
                # Si el delegate explota, no dejamos al usuario sin
                # opción: caemos al inline con un breadcrumb visible
                # en logs. NO mostramos popup de error vacío.
                log.warning(
                    f"[automation_center] delegate rerecord falló, "
                    f"usando inline fallback: {e}"
                )

        # ── Fallback: flujo inline (misiones sin plan) ───────────
        from app.services.missions.recorder import StepFragmentRecorder
        from app.interfaces.desktop.rerecord_overlay import RerecordOverlay

        c_step = self._selected_mission.compiled_execution_graph[index]
        i_step = (self._selected_mission.interpreted_steps[index]
                  if index < len(self._selected_mission.interpreted_steps) else None)
        desc = i_step.description if i_step else "Paso"
        strat = getattr(c_step.action_strategy, 'value', str(c_step.action_strategy))

        self._rerec_index = index
        self._rerec_recorder = StepFragmentRecorder()
        self._rerec_overlay = RerecordOverlay(index, desc, strat)
        self._rerec_overlay.save_requested.connect(self._rerec_save)
        self._rerec_overlay.retry_requested.connect(self._rerec_retry)
        self._rerec_overlay.cancel_requested.connect(self._rerec_cancel)

        # Minimizamos Nevlan para no pelearnos por el foco. El overlay se
        # muestra sin robar foco (WA_ShowWithoutActivating) — así los clicks
        # del usuario van a la app destino y pynput captura coordenadas y
        # UIA correctas.
        self._minimize_preserving_state()
        self._rerec_overlay.show()
        self._rerec_overlay.raise_()
        # NO llamamos activateWindow(): eso haría que el overlay robe foco
        # y todos los clicks siguientes "vean" a Nevlan como ventana activa.
        self._rerec_recorder.start()

        self._rerec_poll = QTimer(self)
        self._rerec_poll.timeout.connect(self._rerec_poll_events)
        self._rerec_poll.start(250)

    def _rerec_poll_events(self):
        if not getattr(self, "_rerec_recorder", None) or not getattr(
            self, "_rerec_overlay", None
        ):
            return
        from app.services.missions.rerecord_display import build_rerecord_display

        events = self._rerec_recorder.get_events()
        expert = bool(getattr(self._rerec_overlay, "expert_mode", False))
        human, expert_lines = build_rerecord_display(events, expert=expert)
        self._rerec_overlay.set_human_display(human, expert_lines)

    def _rerec_stop_recorder(self):
        if getattr(self, "_rerec_poll", None):
            self._rerec_poll.stop()
            self._rerec_poll = None
        rec = getattr(self, "_rerec_recorder", None)
        if rec and rec.is_recording():
            try:
                return rec.stop()
            except Exception as e:
                log.debug(f"rerec stop: {e}")
        return []

    def _rerec_save(self):
        from app.services.missions.recorder import compile_fragment
        raw = self._rerec_stop_recorder()

        if not raw:
            QMessageBox.warning(self, "Sin eventos", "No se capturaron acciones.")
            self._rerec_cleanup()
            return

        try:
            new_steps, new_interp = compile_fragment(raw)
            if not new_steps:
                QMessageBox.warning(self, "Sin pasos", "Los eventos no compilaron a ningún paso.")
                self._rerec_cleanup()
                return
        except Exception as e:
            log.error(f"rerec compile: {e}")
            QMessageBox.critical(self, "Error", f"Error compilando fragmento: {e}")
            self._rerec_cleanup()
            return

        idx = self._rerec_index
        mission = self._selected_mission

        self._push_undo()

        steps = mission.compiled_execution_graph
        interp = mission.interpreted_steps

        if idx < len(steps):
            steps.pop(idx)
        if idx < len(interp):
            interp.pop(idx)

        for j, ns in enumerate(new_steps):
            steps.insert(idx + j, ns)
        for j, ni in enumerate(new_interp):
            if idx + j <= len(interp):
                interp.insert(idx + j, ni)

        try:
            from app.services.missions.compiler import MissionCompiler
            for off in range(len(new_steps)):
                bi = idx + off
                if bi < len(steps) and bi < len(interp):
                    MissionCompiler._enrich_semantic_and_score(steps[bi], interp[bi])
        except Exception as e:
            log.debug(f"enrich rerec inline: {e}")

        self._relink_graph()
        self._mark_dirty()
        log.info(f"Paso {idx} reemplazado por {len(new_steps)} paso(s) (sin guardar)")

        self._rerec_cleanup()
        self._rebuild_step_cards()
        self._update_step_count()
        self.status_lbl.setText(
            f"🎯 Regrabado: {len(new_steps)} paso(s). Guarda para aplicar."
        )
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
        )

    def _rerec_retry(self):
        self._rerec_stop_recorder()
        from app.services.missions.recorder import StepFragmentRecorder
        self._rerec_recorder = StepFragmentRecorder()
        self._rerec_recorder.start()
        if getattr(self, "_rerec_overlay", None):
            self._rerec_overlay.reset_for_retry()
        self._rerec_poll = QTimer(self)
        self._rerec_poll.timeout.connect(self._rerec_poll_events)
        self._rerec_poll.start(250)

    def _rerec_cancel(self):
        # PRD 2026-05-09 §E — re-entrancy-safe: si el cleanup ya está
        # corriendo (overlay.close → closeEvent → cancel_requested),
        # no volvemos a entrar.
        if getattr(self, "_rerec_cleanup_in_progress", False):
            return
        self._rerec_stop_recorder()
        self._rerec_cleanup()

    def _rerec_cleanup(self):
        # PRD 2026-05-09 §E — guard de re-entrancia. Antes:
        # ``overlay.close()`` disparaba ``closeEvent`` →
        # ``cancel_requested`` → ``_rerec_cancel`` → ``_rerec_cleanup``
        # recursivo, dejando referencias a un overlay ya destruido y
        # restaurando la ventana dos veces seguidas.
        if getattr(self, "_rerec_cleanup_in_progress", False):
            return
        self._rerec_cleanup_in_progress = True
        try:
            self._rerec_stop_recorder()
            ov = getattr(self, "_rerec_overlay", None)
            self._rerec_overlay = None
            if ov is not None:
                try:
                    if hasattr(ov, "mark_closing"):
                        ov.mark_closing()
                except Exception:
                    pass
                try:
                    ov.close()
                except Exception:
                    pass
            self._rerec_recorder = None
            self._rerec_index = -1
            try:
                self._restore_previous_state()
            except Exception:
                pass
            try:
                self.activateWindow()
            except Exception:
                pass
        finally:
            self._rerec_cleanup_in_progress = False

    # ─────────────────────────────────────────────────────────────────
    # Delegación al flujo central (Mission Review)
    # ─────────────────────────────────────────────────────────────────

    def _delegate_rerecord_to_review(self, index: int) -> None:
        """PRD 2026-05-09 §G — Lanza una sesión de regrabación usando
        exactamente el mismo flujo central que Mission Review (mismo
        :class:`RerecordOrchestrator` + :class:`RerecordOverlay` con
        replay previo).

        Implementación: abrimos un :class:`MissionReviewDialog` modal
        sobre la misión actual y disparamos su ``rerecord_step(index)``
        diferido para que el dialog termine de construirse antes.

        Esto es el pegamento mínimo posible: no duplicamos lógica de
        orchestrator + worker + heartbeat + overlay-state-machine.
        Cuando el usuario guarda/cancela en el overlay, la misión
        queda mutada en memoria; al cerrar Mission Review, la
        sincronizamos con la misión seleccionada.
        """
        from app.interfaces.desktop.mission_review import MissionReviewDialog

        log.info(
            f"[automation_center] delegando regrabación al flujo "
            f"central (Mission Review) para step={index}"
        )
        # Snapshot defensivo del estado actual: si el dialog se cancela
        # en mitad de algo raro y la misión queda en estado inconsistente,
        # tenemos forma de saberlo en logs.
        try:
            steps_before = len(self._selected_mission.compiled_execution_graph)
        except Exception:
            steps_before = -1

        dlg = MissionReviewDialog(self._selected_mission, parent=self)
        # Disparamos rerecord_step DESPUÉS de que el dialog construya
        # sus tarjetas (singleShot=0 para esperar al primer event-loop
        # tick post-show).
        QTimer.singleShot(0, lambda i=index, d=dlg: d.rerecord_step(i))
        dlg.exec()

        # Tras cerrarse Mission Review (sea por OK, sea por cancel del
        # overlay), refrescamos la vista inline para reflejar cualquier
        # cambio aplicado por el orchestrator.
        try:
            steps_after = len(self._selected_mission.compiled_execution_graph)
        except Exception:
            steps_after = -1
        log.info(
            f"[automation_center] delegate rerecord finished "
            f"steps_before={steps_before} steps_after={steps_after}"
        )
        try:
            self._rebuild_step_cards()
            self._update_step_count()
        except Exception as e:
            log.debug(f"refresh tras delegate rerecord: {e}")

    def _test_semantic_step(self, index: int) -> None:
        """Probar un paso cuya fuente visible es el SEP (Mission Truth Gate)."""
        from app.interfaces.desktop.nevlan_dialog import show_warning
        from app.services.missions.smart_executor import (
            run_single_semantic_plan_step_from_mission,
        )

        if not self._selected_mission:
            return

        self._minimize_preserving_state()
        QApplication.processEvents()

        import time
        time.sleep(0.5)

        success = False
        error: Optional[str] = None
        exception_repr = ""
        try:
            success, err = run_single_semantic_plan_step_from_mission(
                self._selected_mission,
                index,
                expert_mode=False,
            )
            error = err or None
        except Exception as e:
            log.exception(f"[test_semantic_step] excepción: {e}")
            success = False
            error = (
                "Ocurrió un error inesperado al probar el paso semántico. "
                "Intenta de nuevo o abre Mission Review."
            )
            exception_repr = f"{type(e).__name__}: {e}"

        time.sleep(0.3)
        self._restore_previous_state()

        if success:
            show_warning(
                self,
                title="Paso exitoso",
                message=(
                    f"El paso {index + 1} se ejecutó correctamente "
                    f"(plan semántico)."
                ),
                primary="Cerrar",
                cancel="",
            )
            return

        msg = (
            error
            or f"El paso {index + 1} falló sin un mensaje detallado."
        )
        show_warning(
            self,
            title=f"El paso {index + 1} no pudo completarse",
            message=msg,
            primary="Cerrar",
            secondary="",
            cancel="",
            expert_details=exception_repr or "",
        )

    def _test_step(self, index):
        """Ejecuta un solo paso y muestra resultado.

        PRD 2026-05-08c §J + §D: el resultado se reporta vía
        ``NevlanDialog`` con texto SIEMPRE visible (éxito o fallo) y
        con detalles técnicos colapsables si hubo error.
        """
        from app.services.missions.player import MissionPlayer
        from app.interfaces.desktop.nevlan_dialog import (
            RESULT_PRIMARY, show_error, show_warning,
        )
        if not self._selected_mission:
            return

        if _truth_has_semantic_view(self._selected_mission):
            self._test_semantic_step(index)
            return

        self._minimize_preserving_state()
        QApplication.processEvents()

        import time
        time.sleep(0.5)

        success = False
        error: Optional[str] = None
        exception_repr = ""
        try:
            player = MissionPlayer(self._selected_mission)
            success, error = player.test_single_step(index)
        except Exception as e:
            log.exception(f"[test_step] excepción: {e}")
            success = False
            error = (
                "Ocurrió un error inesperado al probar el paso. "
                "Intenta de nuevo o reabre el Centro de Automatizaciones."
            )
            exception_repr = f"{type(e).__name__}: {e}"

        time.sleep(0.3)
        self._restore_previous_state()

        if success:
            show_warning(
                self,
                title="Paso exitoso",
                message=(
                    f"El paso {index + 1} se ejecutó correctamente."
                ),
                primary="Cerrar",
                cancel="",
            )
            return

        # Error path — texto SIEMPRE visible; detalles técnicos
        # disponibles en colapsable.
        msg = (
            error
            or f"El paso {index + 1} falló sin un mensaje detallado."
        )
        result = show_warning(
            self,
            title=f"El paso {index + 1} no pudo completarse",
            message=msg,
            primary="Regrabar paso",
            secondary="Cerrar",
            cancel="",
            expert_details=exception_repr or "",
        )
        if result == RESULT_PRIMARY:
            try:
                self._rerecord_step(index)
            except Exception as e:
                log.exception(f"[test_step] rerecord falló: {e}")
                show_error(
                    self,
                    title="No pude iniciar la regrabación",
                    message=(
                        "Falló el inicio de la regrabación. Intenta "
                        "abrir Mission Review y regrabar desde ahí."
                    ),
                    expert_details=f"{type(e).__name__}: {e}",
                )

    def _delete_step(self, index):
        if not self._selected_mission:
            return
        if index >= len(self._selected_mission.compiled_execution_graph):
            return
        self._push_undo()
        self._selected_mission.compiled_execution_graph.pop(index)
        if index < len(self._selected_mission.interpreted_steps):
            self._selected_mission.interpreted_steps.pop(index)
        self._relink_graph()
        self._mark_dirty()
        self._rebuild_step_cards()
        self._update_step_count()
        log.info(f"Paso {index} eliminado (sin guardar)")

    def _on_name_changed(self, text):
        if not self._selected_mission:
            return
        if self._selected_mission.name != text:
            if not getattr(self, "_dirty", False):
                self._push_undo()
            self._selected_mission.name = text
            self._mark_dirty()

    def _on_save_inline(self):
        """Guardar cambios inline."""
        from app.services.missions import mission_store
        from app.contracts.mission import MissionStatus
        if not self._selected_mission:
            return
        self._selected_mission.name = self.detail_name_edit.text()
        self._selected_mission.status = MissionStatus.APPROVED
        mission_store.save(self._selected_mission)
        self._propagate_mission_rename()
        log.info(f"Automatización guardada: {self._selected_mission.name}")
        self.status_lbl.setText("💾 Guardado")
        self.status_lbl.setStyleSheet(f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 500;")
        QTimer.singleShot(2000, lambda: self.status_lbl.setText("● Listo"))
        QTimer.singleShot(2000, lambda: self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 500;"))
        self.refresh_sidebar()

    def _on_recompile(self):
        """Recompila la misión seleccionada usando su raw_trace con las nuevas reglas.

        Útil para migrar automatizaciones grabadas antes de este sprint: al hacerlo,
        aplican hotkeys con modifiers, patrones (llenar campo, guardar…), scroll,
        drag y normalización. Requiere `raw_trace` en la misión.
        """
        if not self._selected_mission:
            return
        if not self._selected_mission.raw_trace:
            QMessageBox.information(
                self, "Sin trazas crudas",
                "Esta automatización no tiene raw_trace guardado y no puede "
                "recompilarse. Vuelve a grabarla o edita paso a paso."
            )
            return

        n_before = len(self._selected_mission.compiled_execution_graph)
        res = QMessageBox.question(
            self, "Recompilar automatización",
            f"Vamos a recompilar '{self._selected_mission.name}' con las reglas "
            f"nuevas (hotkeys con modifiers, scroll, drag, patrones, descripciones).\n\n"
            f"Pasos actuales: {n_before}. El raw_trace original se conserva.\n"
            f"Se creará una copia .bak del JSON antes de cambiar.\n\n"
            f"Las descripciones y payloads se regenerarán. ¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if res != QMessageBox.StandardButton.Yes:
            return

        from app.services.missions import mission_store
        bak = mission_store.backup_mission_json(
            self._selected_mission.id, tag="pre_recompile"
        )

        self._push_undo()
        try:
            from app.services.missions.compiler import recompile_mission
            recompiled = recompile_mission(self._selected_mission)
            # Mutación in-place para preservar referencias
            self._selected_mission.__dict__.update(recompiled.__dict__)
            n_after = len(self._selected_mission.compiled_execution_graph)
            n_groups = len(getattr(self._selected_mission, "action_groups", []) or [])
            weak = sum(
                1
                for s in (self._selected_mission.interpreted_steps or [])
                if getattr(s, "quality_level", None) == "red"
            )
            self._mark_dirty()
            self._rebuild_step_cards()
            self._update_step_count()
            self.status_lbl.setText(
                f"♻ Recompilada: {n_before} → {n_after} pasos · {n_groups} grupos"
                f" · ~{weak} débiles (sin guardar)"
            )
            self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
            )
            log.info(
                f"Recompilada '{self._selected_mission.name}': {n_before}→{n_after} "
                f"grupos={n_groups} débiles≈{weak} backup={bak.name if bak else 'n/a'}"
            )
        except Exception as e:
            log.error(f"recompile fallo: {e}")
            # Si falló, quitamos el snapshot que empujamos
            if getattr(self, "_undo_stack", []):
                self._undo_stack.pop()
                self._refresh_edit_buttons()
            from app.interfaces.desktop.nevlan_dialog import show_error
            show_error(
                self,
                title="No se pudo recompilar la automatización",
                message=(
                    "Hubo un problema al regenerar los pasos. La "
                    "automatización quedó intacta — vuelve a intentar "
                    "o reabre el Centro de Automatizaciones."
                ),
                expert_details=f"{type(e).__name__}: {e}",
            )

    def _on_recompile_all(self):
        """Vista previa en memoria; persistencia sólo tras confirmar (copia .bak al guardar)."""
        from app.services.missions import mission_store
        from app.services.missions.bulk_recompile import (
            preview_recompile_candidates,
            BulkRecompilePreviewRow,
        )

        all_m = mission_store.list_all()
        candidates = [m for m in all_m if m.raw_trace]
        if not candidates:
            QMessageBox.information(
                self,
                "Nada que recompilar",
                "Ninguna automatización guardada tiene raw_trace.",
            )
            return

        res = QMessageBox.question(
            self,
            "Vista previa en memoria",
            f"Se generará una vista previa (sin escribir disco) para "
            f"{len(candidates)} automatización(es) con raw_trace.\n\n"
            f"¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if res != QMessageBox.StandardButton.Yes:
            return

        rows: List[Any] = preview_recompile_candidates(candidates)
        if not any(getattr(r, "ok", False) for r in rows):
            err_body = "\n".join(
                f"• {r.mission_name}: {r.error}" for r in rows if getattr(r, "error", None)
            )
            QMessageBox.warning(
                self,
                "Sin resultados",
                "No hubo ninguna recompilación válida en memoria.\n\n" + err_body,
            )
            return

        self._show_bulk_recompile_preview_dialog(rows)

    def _show_bulk_recompile_preview_dialog(self, rows: List[Any]) -> None:
        from app.services.missions import mission_store
        from app.services.missions.bulk_recompile import BulkRecompilePreviewRow

        dlg = QDialog(self)
        dlg.setWindowTitle("Recompilar todas — vista previa")
        dlg.resize(700, 480)
        root = QVBoxLayout(dlg)

        root.addWidget(QLabel(
            "Nada se ha escrito en disco. «Guardar cambios» creará un .bak "
            "antes de sobrescribir cada JSON.<br>"
            "<b>Opción recomendada</b>: revisa pasos débiles y errores antes de guardar.",
        ))

        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        lines = []
        for r in rows:
            if isinstance(r, BulkRecompilePreviewRow) and not r.ok:
                lines.append(f"✖ {r.mission_name} — ERROR: {r.error}")
            elif isinstance(r, BulkRecompilePreviewRow):
                lines.append(
                    f"✓ {r.mission_name} — pasos {r.steps_before}→{r.steps_after}, "
                    f"grupos {r.action_groups}, débiles≈{r.weak_steps}, "
                    f"firmas≈{r.signatures_hint}"
                )
        txt.setPlainText("\n".join(lines))
        root.addWidget(txt, 1)

        btn_row = QHBoxLayout()
        btn_save = QPushButton("Guardar cambios")
        btn_discard = QPushButton("Descartar")
        btn_details = QPushButton("Ver detalles")
        for b in (btn_save, btn_discard, btn_details):
            b.setMinimumHeight(32)
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_discard)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_details)
        root.addLayout(btn_row)

        def _show_details():
            sub = QDialog(dlg)
            sub.setWindowTitle("Detalle — vista previa")
            sv = QVBoxLayout(sub)
            te = QPlainTextEdit()
            te.setReadOnly(True)
            te.setPlainText(txt.toPlainText())
            sv.addWidget(te, 1)
            close_btn = QPushButton("Cerrar")
            close_btn.clicked.connect(sub.accept)
            sv.addWidget(close_btn)
            sub.resize(680, 520)
            sub.exec()

        btn_details.clicked.connect(_show_details)

        def _commit():
            ok_ct = sum(
                1 for r in rows
                if isinstance(r, BulkRecompilePreviewRow)
                and r.ok
                and r.compiled_mission is not None
            )
            if ok_ct <= 0:
                dlg.reject()
                return
            conf = QMessageBox.question(
                dlg,
                "Guardar en disco",
                f"Se escribirán {ok_ct} automatización(es).\n"
                f"Por cada una se creará un respaldo *.bak antes de sobrescribir.\n\n"
                f"¿Confirmar?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if conf != QMessageBox.StandardButton.Yes:
                return
            backups: List[str] = []
            for r in rows:
                if (
                    isinstance(r, BulkRecompilePreviewRow)
                    and r.ok
                    and r.compiled_mission is not None
                ):
                    bak = mission_store.backup_mission_json(
                        r.mission_id,
                        tag="pre_recompile_all_commit",
                    )
                    mission_store.save(r.compiled_mission)
                    if bak:
                        backups.append(bak.name)
            log.info(
                f"bulk recompile persisted: missions={ok_ct} "
                f"bak_examples={backups[:3]}"
            )
            self.refresh_sidebar()
            self.status_lbl.setText("♻ Recompilar todas guardado en disco.")
            sid = getattr(self._selected_mission, "id", None)
            if sid:
                fresh = mission_store.load(sid)
                if fresh:
                    self._selected_mission = fresh
                    self._rebuild_step_cards()
                    self._update_step_count()
            dlg.accept()

        btn_save.clicked.connect(_commit)
        btn_discard.clicked.connect(dlg.reject)

        dlg.exec()

    def _on_delete_mission(self):
        from app.services.missions import mission_store
        if not self._selected_mission:
            return
        res = QMessageBox.warning(self, "Eliminar automatización",
            f"¿Seguro que deseas eliminar '{self._selected_mission.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if res == QMessageBox.StandardButton.Yes:
            mission_store.delete(self._selected_mission.id)
            log.info(f"Automatización eliminada: {self._selected_mission.id}")
            self._selected_mission = None
            self.center_stack.setCurrentIndex(0)
            self.refresh_sidebar()

    def _on_toggle_favorite(self):
        if not self._selected_mission or not self._tracker:
            return
        is_fav = self._tracker.toggle_favorite(self._selected_mission.id)
        self._update_fav_button(is_fav)
        self.refresh_sidebar()

    def _update_fav_button(self, is_fav):
        if hasattr(self, "fav_btn"):
            self.fav_btn.setText("⭐ Favorita" if is_fav else "☆ Favorita")

    def _sync_fav_button(self):
        if not hasattr(self, "fav_btn") or not self._selected_mission or not self._tracker:
            return
        self._update_fav_button(self._tracker.is_favorite(self._selected_mission.id))

    def refresh_sidebar(self):
        """Reload missions and rebuild sidebar."""
        self._load_sidebar_list()

    def _on_recording_mission_ready(self, event):
        """Tras guardar/compilar una misión: lista y actividad; abre detalle si es esta grabación."""
        mid = (event.payload or {}).get("mission_id")
        if not mid:
            return
        try:
            self._ensure_automation_center_front_and_focus()
        except Exception:
            pass
        try:
            from app.services.missions import mission_store
            m = mission_store.load(mid)
            self.refresh_sidebar()
            self._refresh_activity_panel()
            if m and self._pending_recording_mid and mid == self._pending_recording_mid:
                self._pending_recording_mid = None
                self._select_automation(m)
                try:
                    # Siguiente ciclo de eventos: UI principal ya restaurada por _restore_main_interface
                    QTimer.singleShot(0, lambda mm=m: self._show_post_recording_review(mm))
                except Exception:
                    pass
            self.status_lbl.setText("● Listo")
            self.status_lbl.setStyleSheet(
                f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 500;")
        except Exception as e:
            log.error(f"_on_recording_mission_ready: {e}")

    def _show_post_recording_review(self, mission) -> None:
        """Resumen post-grabación premium: métricas, alertas, grupos, CTAs organizados."""
        try:
            from app.services.missions.step_quality import count_weak_steps
        except Exception:
            return
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QGridLayout

        try:
            self._ensure_automation_center_front_and_focus()
        except Exception:
            pass

        groups = len(getattr(mission, "action_groups", None) or [])
        # Mission Truth Gate: el conteo "N pasos" de la vista post-grabación
        # también debe venir del SEP. Las métricas weak / capture_score
        # se mantienen como diagnóstico interno de captura (debug).
        steps = _truth_visible_step_count(mission)
        weak = count_weak_steps(
            mission.compiled_execution_graph, mission.interpreted_steps
        )
        scores: List[float] = []
        for c in mission.compiled_execution_graph:
            cf = c.target_context.confidence or {}
            if cf.get("capture_score") is not None:
                try:
                    scores.append(float(cf["capture_score"]))
                except (TypeError, ValueError):
                    pass
        avg_c = (
            round(sum(scores) / len(scores)) if scores else None
        )
        strong = sum(1 for s in scores if s >= 70)
        medium = sum(1 for s in scores if 40 <= s < 70)
        fragile = sum(1 for s in scores if s < 40)

        # Determinar estado general
        if weak <= 0 and avg_c and avg_c >= 70:
            health_icon = "✅"
            health_text = "Captura robusta"
            health_color = NevlanTheme.SUCCESS
            hint = "Lista para ejecutar o programar."
        elif weak <= 2:
            health_icon = "⚠️"
            health_text = "Captura aceptable"
            health_color = NevlanTheme.WARNING
            hint = "Algunos pasos podrían fallar. Reparar antes de producción."
        else:
            health_icon = "🔴"
            health_text = "Necesita revisión"
            health_color = NevlanTheme.ERROR
            hint = "Varios pasos débiles. Reparar es muy recomendado."

        dlg = QDialog(self)
        dlg.setModal(True)
        dlg.setWindowTitle("Proceso grabado — Nevlan")
        dlg.resize(620, 520)
        dlg.setMinimumWidth(560)
        dlg.setStyleSheet(f"""
            QDialog {{
                background-color: {NevlanTheme.BG_CARD};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 14px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {NevlanTheme.TEXT_PRIMARY};
            }}
        """)

        root = QVBoxLayout(dlg)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(16)

        # ── Header con gradiente ──
        header = QLabel(
            f"<h2 style='margin:0; color:{NevlanTheme.TEXT_PRIMARY}; "
            f"font-weight: 800; letter-spacing: 0.3px;'>"
            f"{health_icon} Proceso grabado</h2>"
        )
        root.addWidget(header)

        name_lbl = QLabel(
            f"<span style='font-size:15px; font-weight:700; "
            f"color:{NevlanTheme.ACCENT};'>{mission.name}</span>"
        )
        root.addWidget(name_lbl)

        # ── Tarjetas de métricas (grid 2x2) ──
        metrics = QFrame()
        metrics.setStyleSheet(
            f"QFrame {{ background: rgba(255,255,255,0.02); "
            f"border: 1px solid {NevlanTheme.BORDER}; border-radius: 10px; }}"
        )
        mg = QGridLayout(metrics)
        mg.setContentsMargins(14, 10, 14, 10)
        mg.setSpacing(8)

        def _metric_card(icon: str, value: str, label: str, color: str = NevlanTheme.TEXT_PRIMARY):
            w = QFrame()
            w.setStyleSheet("background: transparent; border: none;")
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(1)
            top = QLabel(f"{icon} <b style='color:{color}; font-size:16px;'>{value}</b>")
            top.setStyleSheet("font-size: 14px;")
            bot = QLabel(label)
            bot.setStyleSheet(
                f"color: {NevlanTheme.TEXT_MUTED}; font-size: 9px; font-weight: 600;"
            )
            vl.addWidget(top)
            vl.addWidget(bot)
            return w

        mg.addWidget(_metric_card("📦", str(groups), "Grupos de acciones"), 0, 0)
        mg.addWidget(_metric_card("⚡", str(steps), "Pasos técnicos"), 0, 1)
        avg_display = f"{avg_c}%" if avg_c is not None else "—"
        avg_color = NevlanTheme.SUCCESS if (avg_c or 0) >= 70 else (
            NevlanTheme.WARNING if (avg_c or 0) >= 40 else NevlanTheme.ERROR
        )
        mg.addWidget(_metric_card("🎯", avg_display, "Confianza media", avg_color), 1, 0)
        weak_color = NevlanTheme.SUCCESS if weak == 0 else (
            NevlanTheme.WARNING if weak <= 2 else NevlanTheme.ERROR
        )
        mg.addWidget(_metric_card("🩹", str(weak), "Requieren atención", weak_color), 1, 1)
        root.addWidget(metrics)

        # ── Barra de calidad visual ──
        if scores:
            bar_frame = QFrame()
            bar_frame.setFixedHeight(28)
            bar_frame.setStyleSheet("background: transparent; border: none;")
            bar_hl = QHBoxLayout(bar_frame)
            bar_hl.setContentsMargins(0, 0, 0, 0)
            bar_hl.setSpacing(2)
            total = strong + medium + fragile or 1
            for (count, color, tip) in [
                (strong, NevlanTheme.SUCCESS, f"{strong} robustos"),
                (medium, NevlanTheme.WARNING, f"{medium} aceptables"),
                (fragile, NevlanTheme.ERROR, f"{fragile} frágiles"),
            ]:
                if count > 0:
                    seg = QFrame()
                    seg.setFixedHeight(6)
                    seg.setStyleSheet(
                        f"background: {color}; border-radius: 3px; border: none;"
                    )
                    seg.setToolTip(tip)
                    bar_hl.addWidget(seg, count)
            root.addWidget(bar_frame)

        # ── Hint ──
        hint_lbl = QLabel(
            f"<span style='color: {health_color}; font-weight: 600;'>"
            f"{health_text}</span>"
            f"<span style='color: {NevlanTheme.TEXT_SECONDARY};'> — {hint}</span>"
        )
        hint_lbl.setStyleSheet("font-size: 12px;")
        hint_lbl.setWordWrap(True)
        root.addWidget(hint_lbl)

        # ── Grupos principales (preview, máx 5) ──
        action_groups = getattr(mission, "action_groups", None) or []
        if action_groups:
            grp_frame = QFrame()
            grp_frame.setStyleSheet(
                f"QFrame {{ background: rgba(162,155,254,0.06); "
                f"border: 1px solid rgba(162,155,254,0.15); border-radius: 8px; }}"
            )
            grp_l = QVBoxLayout(grp_frame)
            grp_l.setContentsMargins(10, 6, 10, 6)
            grp_l.setSpacing(2)
            grp_title = QLabel("Grupos detectados:")
            grp_title.setStyleSheet(
                f"color: {NevlanTheme.TEXT_MUTED}; font-size: 9px; font-weight: 700;"
                " letter-spacing: 1px;"
            )
            grp_l.addWidget(grp_title)
            for g in action_groups[:5]:
                gl = QLabel(f"  📂 {g.group_title}")
                gl.setStyleSheet(
                    f"color: {NevlanTheme.TEXT_SECONDARY}; font-size: 10px;"
                )
                grp_l.addWidget(gl)
            if len(action_groups) > 5:
                more = QLabel(f"  … y {len(action_groups) - 5} más")
                more.setStyleSheet(
                    f"color: {NevlanTheme.TEXT_MUTED}; font-size: 9px; font-style: italic;"
                )
                grp_l.addWidget(more)
            root.addWidget(grp_frame)

        root.addStretch()

        # ── CTAs — Primary row ──
        btn_style_primary = f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {NevlanTheme.ACCENT}, stop:1 #6c5ce7);
                color: white; border: none; border-radius: 8px;
                padding: 10px 16px; font-size: 12px; font-weight: 700;
                min-height: 24px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {NevlanTheme.ACCENT_HOVER}, stop:1 #a29bfe);
            }}
        """
        btn_style_repair = f"""
            QPushButton {{
                background: rgba(255, 200, 80, 0.16);
                color: #ffd166; border: 1px solid rgba(255, 200, 80, 0.40);
                border-radius: 8px; padding: 10px 14px; font-size: 11px;
                font-weight: 600; min-height: 24px;
            }}
            QPushButton:hover {{ background: rgba(255, 200, 80, 0.28); }}
        """
        btn_style_secondary = f"""
            QPushButton {{
                background: {NevlanTheme.BG_SIDEBAR};
                color: {NevlanTheme.TEXT_SECONDARY};
                border: 1px solid {NevlanTheme.BORDER};
                border-radius: 8px; padding: 8px 12px; font-size: 11px;
                font-weight: 600; min-height: 22px;
            }}
            QPushButton:hover {{
                background: {NevlanTheme.BG_CARD_HOVER};
                color: {NevlanTheme.TEXT_PRIMARY};
            }}
        """

        primary_row = QHBoxLayout()
        primary_row.setSpacing(8)

        btn_review = QPushButton("📝 Guardar y revisar pasos")
        btn_review.setStyleSheet(btn_style_primary)
        btn_review.setToolTip("Abre el editor de pasos para revisar/editar cada uno")

        btn_test = QPushButton("🧪 Ejecutar prueba")
        btn_test.setStyleSheet(btn_style_primary)
        btn_test.setToolTip("Ejecuta la automatización de inicio a fin para validar")

        btn_fix = QPushButton(f"🛠 Reparar{f' ({weak})' if weak > 0 else ''}")
        btn_fix.setStyleSheet(btn_style_repair)
        btn_fix.setToolTip("Recorre los pasos débiles y te pide elegir el elemento correcto")
        if weak <= 0:
            btn_fix.setEnabled(False)
            btn_fix.setToolTip("No hay pasos débiles que reparar")

        primary_row.addWidget(btn_review)
        primary_row.addWidget(btn_test)
        primary_row.addWidget(btn_fix)
        root.addLayout(primary_row)

        # ── CTAs — Secondary row ──
        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(6)

        btn_plan = QPushButton("📅 Programar")
        btn_plan.setStyleSheet(btn_style_secondary)
        btn_trig = QPushButton("⚡ Activar por evento")
        btn_trig.setStyleSheet(btn_style_secondary)
        btn_close = QPushButton("Seguir en el Centro")
        btn_close.setStyleSheet(btn_style_secondary)

        secondary_row.addWidget(btn_plan)
        secondary_row.addWidget(btn_trig)
        secondary_row.addStretch()
        secondary_row.addWidget(btn_close)
        root.addLayout(secondary_row)

        # ── Handlers ──
        # Capturamos `mission` en la closure para no depender de
        # _selected_mission (que puede ser None si la selección cambió).
        def _safe_open_review(m=mission):
            try:
                self._ensure_automation_center_front_and_focus()
            except Exception:
                pass
            # Antes de abrir el editor: garantizamos que el grabador no
            # tenga listeners colgados ni candados runtime activos. Si
            # el grabador fue parado pero su thread de pyautogui sigue
            # disparando callbacks contra widgets destruidos, podía
            # provocar un segfault al construir el modal — observado el
            # 2026-05-01 como "Nevlan se cierra al abrir el editor".
            try:
                from app.services.missions.recorder import (
                    is_recording_active,
                    _current_recorder,
                    _dispose_stale_recorder_unsafe,
                )
                if not is_recording_active() and _current_recorder is not None:
                    _dispose_stale_recorder_unsafe()
            except Exception:
                pass
            try:
                from app.services.missions.runtime_locks import locks as _rt_locks
                # Si por alguna razón quedó marcado como "grabando"
                # pero el recorder ya no está activo, soltamos el slot
                # para que el review tenga acceso libre al runtime.
                _rt_locks.release_recorder()
            except Exception:
                pass
            try:
                from app.interfaces.desktop.mission_review import MissionReviewDialog
                # parent=None → top-level window. Evita segfault por
                # Qt destruyendo el QDialog padre (post-recording) mientras
                # el MissionReviewDialog intenta renderizarse como hijo.
                #
                # NO usamos ``WA_DeleteOnClose`` con ``exec()``: el modal
                # cierra → Qt schedules ``deleteLater`` → el wrapper
                # Python sigue vivo apuntando a un C++ borrado → al
                # siguiente acceso (incluso GC silencioso) crashea con
                # access violation. Patrón conocido de PyQt6 que disparó
                # el cierre del 2026-05-03. Python decrementa refcount
                # solo al salir del scope: Qt cleanup natural es seguro.
                log.info("safe_open_review: instanciando MissionReviewDialog")
                dialog = MissionReviewDialog(m, None, auto_open_weak_repair=True)
                # Mantenemos referencia para que Python NO destruya el
                # wrapper antes de que Qt termine de procesar eventos
                # tras ``exec()``. Sin esto, alguna señal pendiente del
                # event loop podía referenciar un wrapper Python
                # liberado.
                self._last_review_dialog = dialog
                log.info("safe_open_review: dialog construido, llamando exec()")
                try:
                    dialog.exec()
                    log.info("safe_open_review: exec() retornó sin excepción")
                finally:
                    # Liberamos la ref tras ``exec()``. Qt ya procesó
                    # todos los eventos pendientes (modal blocking).
                    try:
                        self._last_review_dialog = None
                    except Exception:
                        pass
            except Exception as e:
                log.exception(f"Error abriendo review post-grabación: {e}")
                try:
                    from app.core.crash_handler import write_manual_crash
                    write_manual_crash("safe_open_review", e)
                except Exception:
                    pass
                try:
                    QMessageBox.warning(
                        self, "Error",
                        f"No se pudo abrir el editor de pasos:\n{e}",
                    )
                except Exception:
                    pass
            try:
                if self._selected_mission:
                    self._select_automation(self._selected_mission)
                self.raise_()
                self.activateWindow()
            except Exception:
                pass

        def _finish_review():
            dlg.accept()
            # 200ms de delay para que Qt termine de destruir el dlg
            QTimer.singleShot(200, _safe_open_review)

        def _finish_test():
            dlg.accept()
            QTimer.singleShot(0, self._on_execute)

        def _finish_repair():
            dlg.accept()
            QTimer.singleShot(200, _safe_open_review)

        def _finish_plan():
            dlg.accept()
            QTimer.singleShot(0, self._on_schedule)

        def _finish_triggers():
            dlg.accept()
            QTimer.singleShot(0, self._on_schedule)

        btn_review.clicked.connect(_finish_review)
        btn_test.clicked.connect(_finish_test)
        btn_fix.clicked.connect(_finish_repair)
        btn_plan.clicked.connect(_finish_plan)
        btn_trig.clicked.connect(_finish_triggers)
        btn_close.clicked.connect(dlg.accept)

        for b in (btn_review, btn_test, btn_fix, btn_plan, btn_trig, btn_close):
            try:
                b.setCursor(Qt.CursorShape.PointingHandCursor)
            except Exception:
                pass

        try:
            dlg.raise_()
            dlg.activateWindow()
        except Exception:
            pass

        dlg.exec()

    def _update_step_count(self):
        if self._selected_mission:
            # Mission Truth Gate: el badge "N pasos" debe reflejar la
            # cuenta canónica del SEP, no el grafo compilado legacy.
            steps = _truth_visible_step_count(self._selected_mission)
            self.detail_steps_lbl.setText("Pasos de la automatización")
            if hasattr(self, "step_count_badge"):
                self.step_count_badge.setText(f"{steps} pasos")
            self.detail_status.setText(
                mission_ui_detail_status_line(self._selected_mission),
            )
            self._sync_detail_execution_action_state()

    def _on_record(self):
        """Único punto de entrada para 'Grabar proceso'.

        Ambos botones (el grande del hero y el de la barra superior) llaman
        aquí: garantiza que el flujo sea idéntico desde cualquier lado.

        Pasos:
        1. Si ya hay grabación activa, surfacear aviso (no fallar en silencio).
        2. Crear misión nueva y arrancar el recorder.
        3. Mostrar el widget flotante de control (finalizar / reiniciar / cancelar).
        4. Minimizar Nevlan a la barra de tareas para ceder foco a la app
           objetivo — sin esto, los clicks/teclas grabados golpean a la propia
           ventana de Nevlan y el usuario percibe que 'no inicia grabación'.
        """
        try:
            from app.services.missions.recorder import (
                start_recording, is_recording_active,
            )
        except Exception as e:
            log.error(f"No se pudo cargar el recorder: {e}")
            return

        if is_recording_active():
            try:
                QMessageBox.information(
                    self, "Grabación en curso",
                    "Ya hay una grabación activa. Termínala o cancélala "
                    "desde el panel flotante antes de iniciar otra.",
                )
            except Exception:
                pass
            # Re-mostrar el widget flotante por si quedó oculto.
            try:
                if self._recording_widget and self._recording_widget.isVisible():
                    self._recording_widget.raise_()
                    self._recording_widget.activateWindow()
            except Exception:
                pass
            return

        try:
            mission = start_recording("")
        except Exception as e:
            log.error(f"Error iniciando grabación: {e}")
            try:
                QMessageBox.warning(
                    self, "Grabación",
                    f"No se pudo iniciar la grabación:\n{e}",
                )
            except Exception:
                pass
            return

        self._pending_recording_mid = mission.id
        self.status_lbl.setText("🔴 Grabando")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.ERROR}; font-size: 12px; font-weight: 600;"
        )
        self._show_recording_control(mission.id)

        # Cedemos foco: Nevlan a la barra de tareas para que las teclas/click
        # del usuario lleguen a la app objetivo y NO a Nevlan misma. El widget
        # flotante de control sigue visible (Tool + WindowStaysOnTopHint).
        try:
            self._minimize_preserving_state()
        except Exception:
            try:
                self.showMinimized()
            except Exception:
                pass

    def _show_recording_control(self, recording_mission_id: str = ""):
        """Show floating recording control widget."""
        if self._recording_widget:
            try:
                # No basta con close(): deleteLater es async; un segundo inicio
                # de grabación dejaba suscriptores duplicados al bus.
                self._recording_widget.shutdown_synchronously()
            except Exception as e:
                log.debug(f"_show_recording_control cleanup prev: {e}")
            self._recording_widget = None

        self._recording_widget = RecordingControlWidget(
            parent_center=self, expected_mission_id=recording_mission_id)
        self._recording_widget.show()

    def _on_execute(self):
        if not self._selected_mission:
            return
        m = self._selected_mission

        # PRD 2026-05-03d §1 + §3: antes de tocar nada, pasar por el
        # gate + decidir ejecutor. Si el gate bloquea, NO se ejecuta
        # ningún paso — abrimos refuerzo rápido en su lugar.
        from app.services.missions.smart_runner_bridge import decide_player
        try:
            decision = decide_player(m)
        except Exception as e:
            log.exception(f"_on_execute decide_player: {e}")
            decision = "blocked"

        if decision == "blocked":
            self._handle_execution_blocked(m)
            return

        if decision == "smart":
            self._run_mission_with_smart_executor(m)
            return

        # Fallback: misión legacy ⇒ MissionPlayer antiguo con warning.
        self._run_mission_with_legacy_player(m, legacy_warning=True)

    # ── Smart vs Legacy routing ──────────────────────────────────
    def _handle_execution_blocked(self, mission):
        """Gate bloqueó la misión — muestra violaciones y abre Mission
        Review si aplica. Garantiza que nunca se ejecuta un paso rojo,
        un perfil vacío, un target vacío o una misión con ``/temp/``.

        PRD 2026-05-08c §G + §D + §I:
          * Usa ``get_visible_blockers_for_user`` para construir la
            lista que se muestra al usuario común — descarta códigos
            puramente técnicos.
          * **Nunca** muestra "Arregla los puntos marcados…" si la
            lista visible está vacía: en ese caso muestra un mensaje
            claro de "No hay puntos visibles bloqueando" + detalles
            técnicos colapsables.
          * Diálogo Nevlan, no QMessageBox nativo. Texto siempre
            visible.
        """
        from app.services.missions.approval_gate import (
            check_mission_approvable,
        )
        from app.services.missions.mission_review_summary import (
            build_mission_review_summary,
        )
        from app.interfaces.desktop.nevlan_dialog import (
            RESULT_CANCEL, RESULT_PRIMARY, RESULT_SECONDARY,
            show_warning,
        )

        report = check_mission_approvable(mission)
        try:
            summary = build_mission_review_summary(mission)
            visible = list(summary.visible_blockers)
        except Exception as e:
            log.debug(f"[execution_blocked] summary falló: {e}")
            summary = None
            visible = []

        # Top de violaciones (técnicas, para vista experto/details).
        top_msgs = [
            f"• [{v.code}] {v.message}"
            for v in report.errors[:8]
        ]
        extra = ""
        if len(report.errors) > 8:
            extra = f"\n…y {len(report.errors) - 8} bloqueantes más."
        expert_block = "\n".join(top_msgs) + extra

        # Mensaje principal:
        if visible:
            #   1. Hay puntos visibles → mensaje accionable (PRD §G).
            visible_lines = "\n".join(
                f"• {self._humanize_blocker_code(c)}"
                for c in visible[:6]
            )
            extra_visible = ""
            if len(visible) > 6:
                extra_visible = (
                    f"\n…y {len(visible) - 6} más en Mission Review."
                )
            primary_msg = (
                "Esta automatización tiene puntos por confirmar antes "
                "de ejecutarse:\n\n"
                + visible_lines
                + extra_visible
            )
            primary_btn = "Abrir Mission Review"
            secondary_btn = ""
        else:
            #   2. NO hay puntos visibles → no podemos decir
            #   "arregla los puntos marcados". Damos un mensaje
            #   honesto: hay un blocker técnico, no algo que el
            #   usuario común tenga que arreglar a mano.
            primary_msg = (
                "El sistema todavía no puede ejecutar esta "
                "automatización, pero no encontré puntos visibles "
                "que tú debas confirmar. Esto suele indicar un "
                "bloqueo técnico — abre Mission Review para verlo en "
                "modo experto, o reintenta más tarde."
            )
            primary_btn = "Ver Mission Review"
            secondary_btn = "Cerrar"

        self.status_lbl.setText("■ Bloqueada por revisión")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
        )

        result = show_warning(
            self,
            title="Esta automatización aún no puede ejecutarse",
            message=primary_msg,
            primary=primary_btn,
            secondary=secondary_btn,
            cancel="Cancelar",
            details="",
            expert_details=expert_block.strip() or "(sin detalles)",
        )
        if result not in (RESULT_PRIMARY, RESULT_SECONDARY):
            return

        if result == RESULT_PRIMARY:
            try:
                from app.interfaces.desktop.mission_review import (
                    MissionReviewDialog,
                )
                d = MissionReviewDialog(mission, self)
                d.exec()
                if self._selected_mission:
                    self._select_automation(self._selected_mission)
            except Exception as e:
                log.debug(f"abrir mission_review falló: {e}")
                show_warning(
                    self,
                    title="No pude abrir Mission Review",
                    message=(
                        "Ocurrió un problema al cargar la pantalla de "
                        "revisión. Reintenta o reinicia el módulo."
                    ),
                    expert_details=f"{type(e).__name__}: {e}",
                )

    def _humanize_blocker_code(self, code: str) -> str:
        """Traduce un código a un texto humano accionable.

        PRD 2026-05-08c §G: la vista normal NUNCA debe mostrar el
        código crudo (``EMPTY_PROFILE_NAME_IN_SELECT_PROFILE``). Lo
        traducimos a una frase que el usuario entiende.
        """
        mapping = {
            "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE":
                "Confirmar el perfil o cuenta de usuario a seleccionar.",
            "QUERY_FIDELITY_SUSPECT":
                "Confirmar el texto de búsqueda.",
            "QUERY_MISSING_SHORT_TOKEN":
                "Confirmar el texto de búsqueda (parece haber perdido "
                "una palabra corta).",
            "EMPTY_QUERY_IN_SEARCH":
                "Falta el texto a buscar.",
            "EMPTY_URL_IN_OPEN_URL":
                "Falta la URL a abrir.",
            "EMPTY_APP_IN_OPEN_APP":
                "Falta indicar qué aplicación abrir.",
            "NEEDS_USER_LABEL_PENDING":
                "Hay pasos esperando tu confirmación.",
            "TEMP_ASSET_IN_APPROVED_PLAN":
                "Hay archivos temporales pendientes (Asset Finalizer).",
            "PROFILE_RESOLUTION_REQUIRED":
                "Necesito resolver el perfil del navegador.",
            "SEMANTIC_INTENT_UNDER_SPECIFIED":
                "La intención humana parece incompleta o demasiado genérica.",
            "SEMANTIC_INTENT_MULTI_TARGET":
                "Hay varios posibles objetivos equivalentes para este paso.",
        }
        return mapping.get(code, code.replace("_", " ").lower().capitalize())

    def _run_mission_with_smart_executor(
        self, mission, *, semantic_resume_start_index: int = 0,
    ):
        """Ejecuta la misión con ``SmartMissionExecutor`` (nuevo default).

        PRD 2026-05-08c §exec-lifecycle:

        * Crea un :class:`ExecutionRun` honesto **antes** de invocar
          al worker. El historial registra ``queued`` — NO "Ejecutado".
        * El widget flotante queda bindeado al run para watchdog y
          señalización terminal.
        * Cualquier ruta del bridge (success / failed / blocked /
          excepción / sin steps) acaba en un estado terminal del run
          (PRD §A) y, por extensión, en un cierre garantizado de la
          ventana (PRD §B).
        * El historial se actualiza vía :meth:`tracker.log_run_finished`
          con la trace real (PRD §C, §D).
        """
        import threading, time as _time
        from app.services.missions.execution_lifecycle import (
            ExecutionRun,
            ExecutionStatus,
        )
        from app.services.missions.execution_orchestration import (
            execute_with_lifecycle,
        )
        from app.services.missions.smart_runner_bridge import run_mission_smart

        # Construimos primero el run agregado: el resto del orquestador
        # transita estados sobre el mismo objeto.
        plan = getattr(mission, "semantic_execution_plan", None)
        plan_steps = list(getattr(plan, "steps", []) or []) if plan else []
        total_steps = (
            len(plan_steps) or len(mission.compiled_execution_graph) or 1
        )
        plan_source = (
            "semantic_execution_plan" if plan_steps
            else "compiled_execution_graph"
        )
        run = ExecutionRun(
            mission_id=str(getattr(mission, "id", "") or ""),
            mission_name=str(getattr(mission, "name", "") or ""),
            total_steps=total_steps,
            trigger="manual",
            plan_source=plan_source,
            executor_used="smart",
        )

        srs = max(0, int(semantic_resume_start_index or 0))
        extra_runner: dict = {}
        if srs > 0:
            extra_runner["semantic_start_step_index"] = srs
        if self._tracker:
            try:
                exec_idx = self._tracker.log_start(
                    mission.id, mission.name,
                    total_steps, "manual",
                )
            except Exception as e:
                log.debug(f"tracker.log_start falló: {e}")
        try:
            self._last_run_mission_id = str(mission.id)
        except Exception:
            self._last_run_mission_id = ""

        self.status_lbl.setText("▶ Ejecutando (inteligente)")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
        )

        expert_mode = bool(getattr(self, "_expert_mode_enabled", False))

        # Panel flotante de ejecución — visible durante toda la corrida.
        # PRD §B: lo bindeamos al ``ExecutionRun`` para watchdog y señal
        # ``lifecycle_terminal``. El cancel button del widget pasará por
        # ``run.mark_cancelled()`` automáticamente.
        self._show_execution_control(
            mission.name, total_steps, None
        )
        widget = getattr(self, "_execution_widget", None)
        if widget is not None:
            try:
                widget.bind_lifecycle(run)
            except Exception as e:
                log.debug(f"bind_lifecycle: {e}")

        if srs > 0:
            def _pulse_resume():
                ww = getattr(self, "_execution_widget", None)
                if ww is not None:
                    try:
                        ww.phase_lbl.setText(
                            "Continuamos desde el paso adaptado sin repetir todo."
                        )
                    except Exception:
                        pass

            try:
                QTimer.singleShot(50, _pulse_resume)
            except Exception:
                pass

        # Contador de pasos completados (para mapear ``on_step_done`` a
        # progreso lineal — el SmartExecutor no expone ``step_index``).
        completed = {"i": 0}

        def _humanize_outcome(outcome) -> str:
            kind = (outcome.kind or "paso").replace("_", " ")
            st = outcome.status
            if st == "success":
                return f"✓ {kind} listo"
            if st == "skipped":
                return f"↳ {kind} ya estaba"
            if st == "needs_human":
                return f"⚠ {kind} necesita tu ayuda"
            if st == "failed":
                return f"✕ {kind} falló"
            return f"● {kind} ({st})"

        def _ui_status(msg: str) -> None:
            text = (msg or "")[:240]

            def _apply():
                try:
                    self.status_lbl.setText(f"● {text[:160]}")
                except Exception:
                    pass
                w = getattr(self, "_execution_widget", None)
                if w is not None:
                    try:
                        w.phase_lbl.setText(text)
                    except Exception:
                        pass
            try:
                QTimer.singleShot(0, _apply)
            except Exception:
                pass

        def _ui_step_done(outcome) -> None:
            completed["i"] += 1
            done = completed["i"]
            phase = _humanize_outcome(outcome)
            detail = (outcome.human_message or "").strip()
            elapsed = float(getattr(outcome, "duration_ms", 0) or 0)

            def _apply():
                w = getattr(self, "_execution_widget", None)
                if w is not None:
                    try:
                        w.update_smart_progress(
                            step_index=done,
                            total=total_steps,
                            phase_text=phase,
                            detail_text=detail,
                            elapsed_ms=elapsed,
                        )
                    except Exception as e:
                        log.debug(f"_ui_step_done widget update: {e}")
            try:
                QTimer.singleShot(0, _apply)
            except Exception:
                pass
            if expert_mode:
                log.info(
                    f"[Nevlan·smart] step={outcome.kind} "
                    f"status={outcome.status} strategy={outcome.strategy_used} "
                    f"retries={outcome.retries} duration_ms={outcome.duration_ms}"
                )

        def _ui_step_started(payload) -> None:
            """PRD 2026-05-09 §exec-progress: callback que el
            SmartExecutor dispara ANTES de empezar cada step.
            Actualiza el contador de la ventana flotante a
            "Paso N / total" al instante (en vez de quedarse en
            "Paso 0 / N" hasta que el primer paso TERMINA).
            """
            try:
                idx = int(getattr(payload, "index", 0) or 0)
                total = int(getattr(payload, "total", total_steps) or total_steps)
                label = str(getattr(payload, "human_label", "") or "")
            except Exception:
                idx, total, label = 0, total_steps, ""

            def _apply():
                w = getattr(self, "_execution_widget", None)
                if w is None:
                    return
                try:
                    w.mark_smart_step_started(
                        step_index=idx,
                        total=total,
                        phase_text=label,
                    )
                except Exception as e:
                    log.debug(f"_ui_step_started widget update: {e}")
            try:
                QTimer.singleShot(0, _apply)
            except Exception:
                pass

        def _ui_human_help(outcome) -> None:
            msg = outcome.human_message or "No pude continuar de forma segura."

            def _ask():
                try:
                    QMessageBox.warning(self, "Necesito tu ayuda", msg)
                except Exception:
                    pass
            try:
                QTimer.singleShot(0, _ask)
            except Exception:
                pass

        try:
            self._minimize_preserving_state()
            QApplication.processEvents()
        except Exception:
            pass
        _time.sleep(0.35)

        # Notificar al historial que el run pasó a "running" en cuanto
        # arranquemos el thread. Esto es defensivo: el callback de
        # status también lo hará, pero queremos que no haya ventanas
        # de "queued" colgadas si el primer status tarda.
        if self._tracker and exec_idx >= 0:
            try:
                run.status = ExecutionStatus.STARTING
                self._tracker.log_run_started(exec_idx, run)
            except Exception:
                pass

        def _run():
            try:
                # PRD §A + §B + §D: el helper translate cada outcome a
                # entries del trace + transiciona el lifecycle al
                # estado terminal correcto en TODAS las rutas (success
                # / failed / blocked / NO_STEPS_STARTED / excepción).
                decision = execute_with_lifecycle(
                    mission, run,
                    runner=run_mission_smart,
                    on_status=_ui_status,
                    on_human_help_needed=_ui_human_help,
                    on_step_done=_ui_step_done,
                    on_step_started=_ui_step_started,
                    expert_mode=expert_mode,
                    allow_legacy_fallback=False,
                    extra_runner_kwargs=(extra_runner if extra_runner else None),
                )
                log.info(
                    f"[SmartExecutor] lifecycle={run.status.value} "
                    f"error_code={run.error_code!r} "
                    f"trace_steps={len(run.trace.entries)} "
                    f"plan_source={run.plan_source!r}"
                )
            except BaseException as e:  # safety net (no debería pasar)
                if not run.is_terminal:
                    try:
                        run.mark_failed_with_exception(e)
                    except Exception:
                        pass
                log.exception(f"_run_mission_with_smart_executor: {e}")
            finally:
                # Persistir trace + lifecycle real en historial.
                if self._tracker and exec_idx >= 0:
                    try:
                        self._tracker.log_run_finished(exec_idx, run)
                    except Exception as e:
                        log.debug(f"log_run_finished: {e}")

                # PRD §B: garantizar terminal en widget.
                terminal_value = run.status.value
                terminal_msg = run.error_message or ""

                def _finalize():
                    from app.services.missions.execution_lifecycle import (
                        EXECUTOR_NOT_INVOKED,
                        ExecutionStatus,
                        GATE_BLOCKED,
                        NO_STEPS_STARTED,
                        USER_CANCELLED,
                    )

                    w = getattr(self, "_execution_widget", None)
                    hint = getattr(run, "repair_ui_hint", None) or {}
                    brief_o = RepairBrief(**hint) if hint else None
                    skip_ec = frozenset({
                        str(GATE_BLOCKED),
                        str(EXECUTOR_NOT_INVOKED),
                        str(NO_STEPS_STARTED),
                        str(USER_CANCELLED),
                        "",
                    })
                    eligible = (
                        brief_o is not None
                        and run.status == ExecutionStatus.FAILED
                        and str(run.error_code or "") not in skip_ec
                    )
                    if eligible and w is not None:
                        try:
                            w.offer_smart_repair_offer(brief_o, mission)
                            return
                        except Exception as fe:
                            log.debug(f"offer_smart_repair_offer: {fe}")
                    if w is not None:
                        try:
                            w.mark_smart_finished(
                                status=terminal_value,
                                message=terminal_msg,
                                auto_close_ms=1500,
                            )
                        except Exception as e:
                            log.debug(f"mark_smart_finished falló: {e}")
                try:
                    QTimer.singleShot(0, _finalize)
                except Exception:
                    pass
                try:
                    self._execution_done.emit()
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def _open_runtime_repair_review(
        self, *, mission_snap: Any, brief: RepairBrief,
    ) -> None:
        """Puente Centro ↔ Mission Review tras fallo smart: regrabación y resume."""
        from app.interfaces.desktop.mission_review import MissionReviewDialog
        from app.services.missions.store import mission_store

        mid = str(getattr(mission_snap, "id", "") or "")
        fresh = mission_store.load(mid) if mid else None
        m = fresh or mission_snap
        lc = int(getattr(brief, "compiled_step_index", -1) or -1)
        launch_idx = lc if lc >= 0 else None
        banner = (
            f"{getattr(brief, 'headline', '')}\n\n"
            f"{getattr(brief, 'subtext', '')}"
        ).strip()

        dlg = MissionReviewDialog(
            m,
            self,
            launch_rerecord_compiled_index=launch_idx,
            runtime_repair_banner=banner,
        )
        dlg.exec()
        if not getattr(dlg, "did_apply_rerecord_save", False):
            return
        m2 = mission_store.load(mid) if mid else fresh

        resume_i = max(
            0, int(getattr(brief, "resume_semantic_start_index", 0) or 0),
        )

        expert_mode_ctx = bool(getattr(self, "_expert_mode_enabled", False))

        try:
            from app.services.missions.repair_step_micro_validate import (
                micro_validate_repaired_semantic_step,
            )

            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            QApplication.processEvents()
            vr = micro_validate_repaired_semantic_step(
                m2,
                resume_i,
                expert_mode=expert_mode_ctx,
            )
        finally:
            try:
                QApplication.restoreOverrideCursor()
            except Exception:
                pass

        chk_lines = []
        try:
            labels = (
                ("Plan ejecutable listo", "contract_and_runtime"),
                ("Detector y estrategias probadas", "target_and_strategy_attempted"),
                ("Sin click ciego de emergencia", "no_blind_emergency_click"),
                ("El resultado pudimos comprobarlo", "outcome_measurable"),
            )
            for lab, key in labels:
                okk = vr.checks_passed.get(key, False)
                chk_lines.append(f"{'✓' if okk else '✕'} {lab}")
        except Exception:
            chk_lines = []
        checks_txt = "\n".join(chk_lines)

        diag_body = vr.user_summary.strip()
        if expert_mode_ctx and (vr.expert_detail or "").strip():
            diag_body += f"\n\n[Experto]\n{(vr.expert_detail or '').strip()[:2600]}"

        if not vr.ok:
            QMessageBox.warning(
                self,
                "Comprobación del paso",
                (
                    diag_body
                    + (f"\n\n{checks_txt}" if chk_lines else "")
                ).strip()[:4200],
            )
            return

        q_body = (
            diag_body.strip()
            + (f"\n\n{checks_txt}" if chk_lines else "")
            + "\n\n¿Continuar ahora con el resto de la automatización?"
        )
        rq = QMessageBox.question(
            self,
            "Paso verificado",
            q_body.strip()[:4200],
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if rq != QMessageBox.StandardButton.Yes:
            return

        resume_after = max(0, int(vr.continue_semantic_index or 0))
        tst = max(0, int(getattr(vr, "total_semantic_steps", 0) or 0))

        if tst > 0 and resume_after >= tst:
            QMessageBox.information(
                self,
                "Listo",
                "Con esa adaptación cerraste los pasos pendientes "
                "(no hay más automatización por reanudar desde aquí).",
            )
            return

        if m2:
            self._run_mission_with_smart_executor(
                m2,
                semantic_resume_start_index=resume_after,
            )

    def _run_mission_with_legacy_player(self, mission, *, legacy_warning: bool):
        """Fallback al MissionPlayer legacy — sólo para misiones que NO
        son semánticas. PRD §1 literal: mostrar advertencia.
        """
        import threading, time as _time
        if legacy_warning:
            try:
                QMessageBox.information(
                    self,
                    "Ejecutor legacy",
                    "Esta automatización usa el ejecutor antiguo porque no "
                    "tiene bloques semánticos.\n"
                    "Puede ser menos precisa; considera regrabarla."
                )
            except Exception:
                pass

        self.status_lbl.setText("▶ Ejecutando (legacy)")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.WARNING}; font-size: 12px; font-weight: 600;"
        )

        exec_idx = -1
        if self._tracker:
            try:
                exec_idx = self._tracker.log_start(
                    mission.id, mission.name,
                    len(mission.compiled_execution_graph), "manual",
                )
            except Exception as e:
                log.debug(f"tracker.log_start falló: {e}")
        try:
            self._last_run_mission_id = str(mission.id)
        except Exception:
            self._last_run_mission_id = ""

        from app.services.missions.player import MissionPlayer
        player = MissionPlayer(mission)
        player.start_execution()

        self._show_execution_control(
            mission.name, len(mission.compiled_execution_graph), player)

        try:
            self._minimize_preserving_state()
            QApplication.processEvents()
        except Exception:
            pass
        _time.sleep(0.35)

        def _run():
            try:
                execution = player.replay()
                status = execution.status.value
                log.info(f"[LegacyPlayer] Automatización finalizada: {status}")
                if self._tracker and exec_idx >= 0:
                    err = execution.error_details or ""
                    self._tracker.log_complete(
                        exec_idx, status,
                        steps_executed=execution.steps_executed,
                        error=str(err),
                    )
            except Exception as e:
                log.error(f"Error: {e}")
                if self._tracker and exec_idx >= 0:
                    try:
                        self._tracker.log_complete(
                            exec_idx, "error", error=str(e),
                        )
                    except Exception:
                        pass
            finally:
                try:
                    self._execution_done.emit()
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def _show_execution_control(self, mission_name: str, total_steps: int, player):
        """Crea/muestra el panel flotante de ejecución."""
        existing = getattr(self, "_execution_widget", None)
        if existing:
            try:
                existing.shutdown_synchronously()
            except Exception as e:
                log.debug(f"_show_execution_control cleanup: {e}")
            self._execution_widget = None
        try:
            self._execution_widget = ExecutionControlWidget(
                mission_name=mission_name,
                total_steps=total_steps,
                player=player,
                parent_center=self,
            )
            self._execution_widget.show()
        except Exception as e:
            log.error(f"No se pudo abrir ExecutionControlWidget: {e}")

    def _on_edit(self):
        if not self._selected_mission:
            return
        from app.interfaces.desktop.mission_review import MissionReviewDialog
        dialog = MissionReviewDialog(self._selected_mission, self)
        dialog.exec()
        self._select_automation(self._selected_mission)

    def _open_mission_review_for_repair(self):
        """Abre revisión de pasos y, tras cargar la ventana, muestra «Reparar débiles»."""
        mission = self._selected_mission
        if not mission:
            try:
                self._ensure_automation_center_front_and_focus()
            except Exception:
                pass
            log.warning("_open_mission_review_for_repair: sin misión seleccionada")
            return
        try:
            self._ensure_automation_center_front_and_focus()
        except Exception:
            pass
        try:
            from app.interfaces.desktop.mission_review import MissionReviewDialog

            dialog = MissionReviewDialog(
                mission,
                self,
                auto_open_weak_repair=True,
            )
            dialog.exec()
        except Exception as e:
            log.exception(f"_open_mission_review_for_repair falló: {e}")
            try:
                QMessageBox.warning(
                    self,
                    "Error al abrir reparación",
                    f"No se pudo abrir el editor de pasos:\n\n{e}\n\n"
                    "Puedes intentar desde el panel de pasos directamente.",
                )
            except Exception:
                pass
        try:
            if self._selected_mission:
                self._select_automation(self._selected_mission)
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def _on_schedule(self):
        if not self._selected_mission:
            return
        dialog = ScheduleDialog(self._selected_mission.name, self._selected_mission.id, self)
        if dialog.exec():
            self._refresh_next_exec()

    def _on_history(self):
        if not self._selected_mission:
            return
        dialog = HistoryDialog(self._selected_mission, self)
        dialog.exec()

    def _restore_after_execution(self):
        """Restaura Nevlan cuando termina una ejecución lanzada manualmente."""
        try:
            self._restore_previous_state()
            self.activateWindow()
        except Exception:
            pass
        self.status_lbl.setText("● Listo")
        self.status_lbl.setStyleSheet(
            f"color: {NevlanTheme.SUCCESS}; font-size: 12px; font-weight: 500;"
        )

    def _refresh_next_exec(self):
        """Actualiza el label de próxima ejecución y el panel de próximas."""
        if self._scheduler:
            next_str = self._scheduler.get_next_execution()
            if next_str:
                self.next_exec_lbl.setText(f"Más próxima: {next_str}")
            else:
                self.next_exec_lbl.setText("Sin ejecuciones programadas")
        self._refresh_upcoming_panel()

    def _refresh_next_exec_adaptive(self):
        """Hace el refresh y elige el próximo intervalo según cuán cerca esté
        la próxima ejecución. Cuando faltan ≤60s, sube a 1Hz para que el
        countdown muestre segundos. En otro caso vuelve a 30s para no
        saturar el sistema.
        """
        self._refresh_next_exec()
        try:
            soonest = float("inf")
            if self._scheduler:
                with self._scheduler._lock:  # type: ignore[attr-defined]
                    entries = list(self._scheduler._schedules.values())  # type: ignore[attr-defined]
                for entry in entries:
                    s = entry.seconds_until_next()
                    if s is not None and s >= 0:
                        soonest = min(soonest, s)
            # < 60s: 1Hz; < 1h: 5s; otherwise 30s
            if soonest < 60:
                interval_ms = 1000
            elif soonest < 3600:
                interval_ms = 5000
            else:
                interval_ms = 30000
            if self._refresh_timer.interval() != interval_ms:
                self._refresh_timer.setInterval(interval_ms)
        except Exception as e:
            log.debug(f"_refresh_next_exec_adaptive: {e}")

    def _open_voice_pill(self):
        """Toggle de la pill flotante de voz: abre si está cerrada, cierra si está abierta."""
        try:
            existing = getattr(self, "_pill", None)
            if existing is not None:
                try:
                    if existing.isVisible():
                        existing.close()
                        self._pill = None
                        return
                except RuntimeError:
                    # Widget ya destruido por Qt → tratamos como cerrado
                    self._pill = None

            from app.interfaces.desktop.main_window import NevlanPill
            self._pill = NevlanPill()
            try:
                self._pill.destroyed.connect(lambda *_: setattr(self, "_pill", None))
            except Exception:
                pass
            self._pill.show()
            self._pill.raise_()
            self._pill.activateWindow()
        except Exception as e:
            log.error(f"Error abriendo asistente de voz: {e}")

    def closeEvent(self, event):
        """Detiene el scheduler al cerrar."""
        try:
            from app.runtime.bus import bus
            bus.unsubscribe("mission.review_requested", self._on_recording_mission_ready)
        except Exception:
            pass
        if self._scheduler:
            self._scheduler.stop()
        super().closeEvent(event)
