"""
Nevlan — Add Step Dialog
------------------------
Mini-diálogo para crear un paso nuevo (click/doble-click/hotkey/type/wait/agent)
sin tener que regrabarlo. Produce un CompiledStep + InterpretedStep validados.
"""
from __future__ import annotations

from typing import Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit,
    QSpinBox, QPushButton, QStackedWidget, QWidget, QCheckBox, QTextEdit,
    QDialogButtonBox,
)

from app.contracts.mission import (
    CompiledStep, InterpretedStep, ActionStrategy, TargetResolutionStrategy,
    FallbackStrategy, TargetContext, ValidationStrategy,
)
from app.services.missions.vibe_parser import (
    parse_keyboard_intent, validate_hotkey_payload, describe_hotkey_payload,
)


_TYPES = [
    ("Click", "click"),
    ("Doble-click", "double_click"),
    ("Click derecho", "right_click"),
    ("Escribir texto", "type_text"),
    ("Hotkey / secuencia de teclas", "send_hotkey"),
    ("Esperar (ms)", "wait_for_state"),
    ("Agent (IA)", "agent_prompt"),
]


class AddStepDialog(QDialog):
    """Construye un paso nuevo sin grabar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Agregar paso")
        self.setMinimumWidth(520)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        title = QLabel("➕ Nuevo paso")
        title.setStyleSheet("font-size: 18px; font-weight: 800;")
        root.addWidget(title)

        sub = QLabel("Elige qué acción hará este paso.")
        sub.setStyleSheet("color: #a0a5b0; font-size: 11px;")
        root.addWidget(sub)

        # Selector de tipo
        row = QHBoxLayout()
        row.addWidget(QLabel("Tipo de paso:"))
        self.type_cb = QComboBox()
        for label, _v in _TYPES:
            self.type_cb.addItem(label)
        self.type_cb.currentIndexChanged.connect(self._on_type_changed)
        row.addWidget(self.type_cb, 1)
        root.addLayout(row)

        self.stack = QStackedWidget()
        self._build_panels()
        root.addWidget(self.stack)

        # Descripción opcional
        desc_row = QHBoxLayout()
        desc_row.addWidget(QLabel("Descripción:"))
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("Ej: 'Clic en botón Guardar' (opcional)")
        desc_row.addWidget(self.desc_edit, 1)
        root.addLayout(desc_row)

        # Botones
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Agregar paso")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        self._on_type_changed(0)

    # ─────────────────────────────────────────────────────────────────
    # UI por tipo
    # ─────────────────────────────────────────────────────────────────

    def _build_panels(self):
        # 0 click / 1 dblclick / 2 rightclick  → coords
        self.stack.addWidget(self._panel_coords("Clic"))
        self.stack.addWidget(self._panel_coords("Doble-clic"))
        self.stack.addWidget(self._panel_coords("Clic derecho"))
        # 3 type_text
        self.stack.addWidget(self._panel_type_text())
        # 4 hotkey
        self.stack.addWidget(self._panel_hotkey())
        # 5 wait
        self.stack.addWidget(self._panel_wait())
        # 6 agent_prompt
        self.stack.addWidget(self._panel_agent())

    def _panel_coords(self, label: str) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(
            f"{label} en coordenadas absolutas (x, y). "
            f"Para un click fiable, usa mejor '🎯 Regrabar' sobre un paso y apunta el elemento real."
        ))
        r = QHBoxLayout()
        r.addWidget(QLabel("x:"))
        sx = QSpinBox(); sx.setRange(0, 20000); sx.setValue(0)
        r.addWidget(sx)
        r.addWidget(QLabel("y:"))
        sy = QSpinBox(); sy.setRange(0, 20000); sy.setValue(0)
        r.addWidget(sy)
        r.addStretch()
        lay.addLayout(r)
        w._x = sx
        w._y = sy
        return w

    def _panel_type_text(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Texto a escribir (será tipeado en la ventana activa al ejecutar):"))
        ed = QLineEdit()
        ed.setPlaceholderText("Ej: usuario@correo.com")
        lay.addWidget(ed)
        w._text = ed
        return w

    def _panel_hotkey(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Instrucción de teclado (se interpretará localmente):"))
        ed = QLineEdit()
        ed.setPlaceholderText("Ej: ctrl+s · tab 3 veces · tab, tab, enter · alt+f4")
        lay.addWidget(ed)
        preview = QLabel("—")
        preview.setStyleSheet("color: #74b9ff; font-size: 11px; padding-top: 4px;")
        lay.addWidget(preview)

        def _update_preview():
            txt = ed.text().strip()
            p = parse_keyboard_intent(txt) if txt else None
            if p:
                preview.setText("✔ " + describe_hotkey_payload(validate_hotkey_payload(p)))
            elif txt:
                preview.setText("⚠ No pude interpretar. Prueba: 'ctrl+s', 'tab 3 veces'.")
            else:
                preview.setText("—")
        ed.textChanged.connect(lambda _=None: _update_preview())
        w._text = ed
        w._preview = preview
        return w

    def _panel_wait(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Tiempo a esperar antes del siguiente paso:"))
        r = QHBoxLayout()
        sp = QSpinBox()
        sp.setRange(50, 60000)
        sp.setSingleStep(100)
        sp.setValue(500)
        sp.setSuffix(" ms")
        r.addWidget(sp)
        r.addStretch()
        lay.addLayout(r)
        w._ms = sp
        return w

    def _panel_agent(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(
            "Prompt para el agente de IA. Úsalo solo cuando no sepas de antemano "
            "qué teclear (ej: contenido dinámico). Es más lento que tipeo fijo."
        ))
        ta = QTextEdit()
        ta.setFixedHeight(80)
        ta.setPlaceholderText("Ej: Escribe un saludo formal dirigido al destinatario que ves en pantalla.")
        lay.addWidget(ta)
        w._prompt = ta
        return w

    def _on_type_changed(self, idx: int):
        self.stack.setCurrentIndex(idx)

    # ─────────────────────────────────────────────────────────────────
    # Build the step
    # ─────────────────────────────────────────────────────────────────

    def build_step(self) -> Tuple[Optional[CompiledStep], Optional[InterpretedStep]]:
        idx = self.type_cb.currentIndex()
        key = _TYPES[idx][1]
        desc_user = self.desc_edit.text().strip()

        if key in ("click", "double_click", "right_click"):
            panel = self.stack.widget(idx)
            x = int(panel._x.value())
            y = int(panel._y.value())
            strat = {
                "click": ActionStrategy.CLICK,
                "double_click": ActionStrategy.DOUBLE_CLICK,
                "right_click": ActionStrategy.RIGHT_CLICK,
            }[key]
            step = CompiledStep(
                goal=f"{strat.value} at ({x},{y})",
                target_context=TargetContext(fallback_coords={"x": x, "y": y}),
                target_resolution_strategy=TargetResolutionStrategy.COORDS_ABSOLUTE,
                action_strategy=strat,
                fallback_strategy=FallbackStrategy.USE_COORDS,
            )
            default_desc = f"{strat.value.replace('_', ' ')} en ({x}, {y})"

        elif key == "type_text":
            panel = self.stack.widget(idx)
            text = panel._text.text()
            if not text:
                return None, None
            step = CompiledStep(
                goal=f"Type text",
                target_context=TargetContext(),
                target_resolution_strategy=TargetResolutionStrategy.COORDS_ABSOLUTE,
                action_strategy=ActionStrategy.TYPE_TEXT,
                action_payload={"text": text},
                fallback_strategy=FallbackStrategy.FAIL_FAST,
            )
            default_desc = f"Escribir '{text[:40]}{'…' if len(text) > 40 else ''}'"

        elif key == "send_hotkey":
            panel = self.stack.widget(idx)
            txt = panel._text.text().strip()
            if not txt:
                return None, None
            payload = parse_keyboard_intent(txt)
            if not payload:
                payload = {"hotkey": txt.lower()}
            payload = validate_hotkey_payload(payload)
            step = CompiledStep(
                goal="Send hotkey",
                target_context=TargetContext(),
                target_resolution_strategy=TargetResolutionStrategy.COORDS_ABSOLUTE,
                action_strategy=ActionStrategy.SEND_HOTKEY,
                action_payload=payload,
                fallback_strategy=FallbackStrategy.FAIL_FAST,
            )
            default_desc = describe_hotkey_payload(payload)

        elif key == "wait_for_state":
            panel = self.stack.widget(idx)
            ms = int(panel._ms.value())
            step = CompiledStep(
                goal=f"Wait {ms}ms",
                target_context=TargetContext(),
                target_resolution_strategy=TargetResolutionStrategy.COORDS_ABSOLUTE,
                action_strategy=ActionStrategy.WAIT_FOR_STATE,
                action_payload={"timeout_ms": ms},
                fallback_strategy=FallbackStrategy.FAIL_FAST,
            )
            default_desc = f"Esperar {ms} ms"

        elif key == "agent_prompt":
            panel = self.stack.widget(idx)
            prompt = panel._prompt.toPlainText().strip()
            if not prompt:
                return None, None
            step = CompiledStep(
                goal="Agent prompt",
                target_context=TargetContext(),
                target_resolution_strategy=TargetResolutionStrategy.COORDS_ABSOLUTE,
                action_strategy=ActionStrategy.AGENT_PROMPT,
                action_payload={"text": prompt},
                fallback_strategy=FallbackStrategy.FAIL_FAST,
            )
            default_desc = f"🤖 Agente: {prompt[:60]}{'…' if len(prompt) > 60 else ''}"

        else:
            return None, None

        final_desc = desc_user or default_desc
        i_step = InterpretedStep(description=final_desc, raw_event_ids=[])
        return step, i_step
