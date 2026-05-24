"""
Nevlan — Time-Travel UI
========================

Slider que permite *recorrer* la misión paso a paso viendo:

  * el screenshot capturado al ejecutar el evento,
  * el highlight (rectángulo) sobre el target del click,
  * el texto detectado / OCR cercano,
  * la descripción humana del paso (action_groups).

Objetivo: confianza visual inmediata. El usuario ve EXACTAMENTE qué
hizo Nevlan y por qué — antes de aprobar, antes de ejecutar, después
de ejecutar.

Diseño
------

* Componente standalone: ``TimeTravelDialog``. Se abre desde Mission
  Review como tab adicional o desde un botón "Visualizar grabación".
* Lee screenshots desde ``CompiledStep.target_context.image_ref_mid``
  (o image_ref si no hay mid). Si el archivo no existe, muestra un
  placeholder con el text label del paso.
* El highlight se pinta sobre el screenshot usando coordenadas
  relativas (relative_to_window_fx_fy / click_offset_within_bbox).

Casos límite
------------

* Misiones legacy sin image_ref → placeholder informativo, slider
  sigue funcional.
* Resolución del screenshot ≠ pantalla actual → la imagen se escala
  al QLabel manteniendo aspect ratio.
* Slider con un solo paso → degrada a "vista de paso único" (sin
  navegación).
"""
from __future__ import annotations

import os
from typing import Optional

from app.contracts.mission import CompiledStep, Mission
from app.core.logger import log

try:
    from PyQt6.QtCore import Qt, QSize
    from PyQt6.QtGui import QPainter, QPen, QColor, QPixmap
    from PyQt6.QtWidgets import (
        QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QSlider,
        QVBoxLayout, QWidget,
    )
    HAS_QT = True
except Exception as _e:
    HAS_QT = False
    log.warning(f"[time_travel_ui] PyQt6 no disponible: {_e}")


def _step_screenshot_path(cs: CompiledStep) -> Optional[str]:
    tc = cs.target_context
    if not tc:
        return None
    for k in ("image_ref_mid", "image_ref"):
        v = getattr(tc, k, None)
        if v and isinstance(v, str) and os.path.exists(v):
            return v
    # Buscar en vision_data por si quedó allí.
    vis = tc.vision_data or {}
    if isinstance(vis, dict):
        for k in ("image_ref_mid", "image_ref"):
            v = vis.get(k)
            if v and isinstance(v, str) and os.path.exists(v):
                return v
    return None


def _step_human_label(cs: CompiledStep) -> str:
    sem = (cs.target_context.semantic if cs.target_context else None) or {}
    label = str(sem.get("human_label") or "").strip()
    if label:
        return label
    pl = cs.action_payload or {}
    txt = pl.get("text") or pl.get("query") or pl.get("val") or ""
    return cs.goal or f"{cs.action_strategy.value}: {txt}"


def _step_ocr_text(cs: CompiledStep) -> str:
    tc = cs.target_context
    vis = tc.vision_data if tc else None
    if isinstance(vis, dict):
        return str(vis.get("ocr_text_around") or vis.get("nearest_text_anchor") or "")
    return ""


if HAS_QT:

    class _ScreenshotCanvas(QLabel):
        """Label que pinta una imagen + un rectángulo highlight."""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setMinimumSize(420, 260)
            self.setStyleSheet(
                "QLabel { background: #111; border: 1px solid #333;"
                " border-radius: 6px; }"
            )
            self._pix: Optional[QPixmap] = None
            self._hl_rect: Optional[tuple] = None  # (fx, fy, fw, fh) relativo

        def show_step(
            self,
            pixmap_path: Optional[str],
            highlight_rel: Optional[tuple] = None,
        ) -> None:
            self._hl_rect = highlight_rel
            if pixmap_path and os.path.exists(pixmap_path):
                self._pix = QPixmap(pixmap_path)
            else:
                self._pix = None
            self.update()

        def paintEvent(self, ev):  # type: ignore[override]
            super().paintEvent(ev)
            painter = QPainter(self)
            try:
                rect = self.rect()
                if self._pix is not None and not self._pix.isNull():
                    scaled = self._pix.scaled(
                        rect.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    x = (rect.width() - scaled.width()) // 2
                    y = (rect.height() - scaled.height()) // 2
                    painter.drawPixmap(x, y, scaled)
                    if self._hl_rect:
                        fx, fy, fw, fh = self._hl_rect
                        try:
                            hx = int(x + fx * scaled.width())
                            hy = int(y + fy * scaled.height())
                            hw = max(8, int(fw * scaled.width()))
                            hh = max(8, int(fh * scaled.height()))
                            pen = QPen(QColor(255, 217, 102, 220), 3)
                            painter.setPen(pen)
                            painter.drawRect(hx, hy, hw, hh)
                        except Exception:
                            pass
                else:
                    painter.setPen(QPen(QColor(120, 120, 130, 180)))
                    painter.drawText(
                        rect,
                        int(Qt.AlignmentFlag.AlignCenter),
                        "Sin captura para este paso",
                    )
            finally:
                painter.end()


    class TimeTravelDialog(QDialog):
        """Visualización slider + screenshot por paso."""

        def __init__(self, mission: Mission, parent=None) -> None:
            super().__init__(parent)
            self.mission = mission
            self.setWindowTitle("Nevlan — Visualización paso a paso")
            self.setMinimumSize(640, 520)
            self._build()
            self._show_index(0)

        def _build(self) -> None:
            root = QVBoxLayout(self)
            root.setContentsMargins(16, 14, 16, 14)
            root.setSpacing(10)

            title = QLabel(f"<b>{self.mission.name}</b>")
            title.setStyleSheet("color: white; font-size: 14px;")
            root.addWidget(title)

            self.canvas = _ScreenshotCanvas()
            root.addWidget(self.canvas, 1)

            self.lbl_step = QLabel("")
            self.lbl_step.setWordWrap(True)
            self.lbl_step.setStyleSheet("color: #ddd; font-size: 12px;")
            root.addWidget(self.lbl_step)

            self.lbl_ocr = QLabel("")
            self.lbl_ocr.setWordWrap(True)
            self.lbl_ocr.setStyleSheet(
                "color: #888; font-size: 11px; font-style: italic;"
            )
            root.addWidget(self.lbl_ocr)

            steps = self.mission.compiled_execution_graph or []
            n = max(0, len(steps) - 1)

            slider_row = QHBoxLayout()
            self.btn_prev = QPushButton("◀")
            self.btn_prev.setFixedWidth(30)
            self.btn_prev.clicked.connect(self._prev)
            self.slider = QSlider(Qt.Orientation.Horizontal)
            self.slider.setMinimum(0)
            self.slider.setMaximum(n)
            self.slider.setSingleStep(1)
            self.slider.setPageStep(1)
            self.slider.valueChanged.connect(self._show_index)
            self.btn_next = QPushButton("▶")
            self.btn_next.setFixedWidth(30)
            self.btn_next.clicked.connect(self._next)
            slider_row.addWidget(self.btn_prev)
            slider_row.addWidget(self.slider, 1)
            slider_row.addWidget(self.btn_next)
            root.addLayout(slider_row)

            close_row = QHBoxLayout()
            close_row.addStretch(1)
            self.btn_close = QPushButton("Cerrar")
            self.btn_close.setShortcut("Escape")
            self.btn_close.setDefault(True)
            self.btn_close.setAutoDefault(True)
            self.btn_close.clicked.connect(self.accept)
            close_row.addWidget(self.btn_close)
            root.addLayout(close_row)

        def _show_index(self, idx: int) -> None:
            steps = self.mission.compiled_execution_graph or []
            if not steps:
                self.canvas.show_step(None)
                self.lbl_step.setText("(misión sin pasos)")
                return
            idx = max(0, min(idx, len(steps) - 1))
            cs = steps[idx]
            path = _step_screenshot_path(cs)
            hl = self._highlight_rect(cs)
            self.canvas.show_step(path, hl)
            label = _step_human_label(cs)
            self.lbl_step.setText(
                f"<b>Paso {idx + 1}/{len(steps)}.</b> {label}"
            )
            ocr = _step_ocr_text(cs)
            if ocr:
                self.lbl_ocr.setText(f"OCR cercano: {ocr[:200]}")
            else:
                self.lbl_ocr.setText("")

        def _highlight_rect(self, cs: CompiledStep) -> Optional[tuple]:
            """Devuelve (fx, fy, fw, fh) relativos a la imagen [0..1]."""
            tc = cs.target_context
            if not tc:
                return None
            # Preferimos relative_to_window_fx_fy (relative al window screenshot).
            rel = tc.fallback_coords_relative_to_window
            if isinstance(rel, dict):
                fx = float(rel.get("fx", 0))
                fy = float(rel.get("fy", 0))
                if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
                    # bbox por defecto: 4% wide centered.
                    return (fx - 0.02, fy - 0.02, 0.04, 0.04)
            # bbox visual.
            uia = tc.uia_data or {}
            bbox = uia.get("bbox") if isinstance(uia, dict) else None
            if isinstance(bbox, dict):
                # No tenemos forma estable de saber el tamaño absoluto del
                # screenshot — devolvemos None para no pintar mal.
                pass
            return None

        def _prev(self) -> None:
            self.slider.setValue(max(0, self.slider.value() - 1))

        def _next(self) -> None:
            self.slider.setValue(min(self.slider.maximum(), self.slider.value() + 1))

        def keyPressEvent(self, ev):  # type: ignore[override]
            try:
                k = ev.key()
                if k == Qt.Key.Key_Left:
                    self._prev()
                    ev.accept()
                    return
                if k == Qt.Key.Key_Right:
                    self._next()
                    ev.accept()
                    return
                if k == Qt.Key.Key_Escape:
                    self.reject()
                    ev.accept()
                    return
            except Exception:
                pass
            return super().keyPressEvent(ev)


else:  # pragma: no cover
    class TimeTravelDialog:  # type: ignore[no-redef]
        def __init__(self, mission: Mission, parent=None) -> None:
            self.mission = mission

        def exec(self) -> int:
            log.info("TimeTravelDialog stub (Qt no disponible)")
            return 0


__all__ = ["TimeTravelDialog"]
