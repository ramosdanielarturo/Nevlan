"""
Nevlan — Dialog component (PRD 2026-05-08c §I)
================================================

``NevlanDialog`` es el reemplazo único para los popups del módulo de
Misiones. Garantiza el contrato:

  * **Nunca aparece vacío.** Si no se pasó ``title`` ni ``message``,
    rendea un placeholder explícito en lugar de quedar en blanco.
  * **Siempre con texto contrastado.** Estilos Nevlan: fondo oscuro,
    texto claro. Imposible "texto blanco sobre fondo blanco".
  * **Siempre con al menos un botón visible.** Default: "Aceptar".
  * **Detalles técnicos opcionales colapsables.** Para la vista
    experta y para errores con stacktrace.
  * **Bordes redondeados, ícono semántico, severidad coloreada.**

Uso típico::

    NevlanDialog.show_warning(
        parent,
        title="Esta misión tiene avisos",
        message="Detecté recomendaciones para mejorar antes de ejecutar.",
        primary="Continuar ejecución",
        secondary="Revisar puntos",
        cancel="Cancelar",
        details="execution_confidence=0.62 ; visual_capture=red",
    )

Tipos de severidad (PRD §I):

  * ``info``         — informativo, neutro.
  * ``success``      — verde, confirmación positiva.
  * ``warning``      — amarillo, requiere atención (no bloquea).
  * ``danger``       — rojo, requiere decisión cuidadosa.
  * ``confirmation`` — pregunta neutra con dos opciones.
  * ``expert_details`` — debugging técnico (sin colores fuertes).

Si el caller no especifica severidad, asumimos ``info``. La constante
:data:`MIN_BUTTON` (texto fallback) garantiza que SIEMPRE haya un
botón clickable, incluso si el caller olvidó pasarlos.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget, QFrame,
)


# ─────────────────────────────────────────────────────────────────────
# Theme — local copy de los tokens más usados para no acoplar a
# `automation_center.NevlanTheme` (este módulo es importable solo).
# ─────────────────────────────────────────────────────────────────────


_BG_DIALOG = "#1e1e2a"
_BG_HEADER = "#26263a"
_BORDER = "rgba(255,255,255,0.10)"
_TEXT_PRIMARY = "#e8e8f0"
_TEXT_SECONDARY = "#b9b9cf"
_ACCENT = "#0984e3"
_ACCENT_HOVER = "#74b9ff"
_SUCCESS = "#00cec9"
_WARNING = "#ffa500"
_DANGER = "#ff6b6b"
_FONT = "'Segoe UI', 'Inter', system-ui, sans-serif"


# Tipos públicos.
SEVERITY_INFO = "info"
SEVERITY_SUCCESS = "success"
SEVERITY_WARNING = "warning"
SEVERITY_DANGER = "danger"
SEVERITY_CONFIRMATION = "confirmation"
SEVERITY_EXPERT = "expert_details"

_KNOWN_SEVERITIES = (
    SEVERITY_INFO, SEVERITY_SUCCESS, SEVERITY_WARNING,
    SEVERITY_DANGER, SEVERITY_CONFIRMATION, SEVERITY_EXPERT,
)


_SEVERITY_COLORS = {
    SEVERITY_INFO: (_ACCENT, "ⓘ"),
    SEVERITY_SUCCESS: (_SUCCESS, "✓"),
    SEVERITY_WARNING: (_WARNING, "⚠"),
    SEVERITY_DANGER: (_DANGER, "⛔"),
    SEVERITY_CONFIRMATION: (_ACCENT, "?"),
    SEVERITY_EXPERT: (_TEXT_SECONDARY, "⚙"),
}


# Resultado del diálogo. La UI lee ``result`` para decidir qué hacer.
RESULT_PRIMARY = "primary"
RESULT_SECONDARY = "secondary"
RESULT_CANCEL = "cancel"
RESULT_CLOSE = "close"


# Texto fallback cuando el caller olvidó pasar message/title.
MIN_BUTTON = "Aceptar"
EMPTY_TITLE_FALLBACK = "Aviso"
EMPTY_MESSAGE_FALLBACK = (
    "Sin descripción disponible. (Si esto se repite, reporta un bug "
    "indicando qué acción intentaste.)"
)


@dataclass
class NevlanDialogSpec:
    """Configuración declarativa de un diálogo.

    Cualquier campo string vacío se reemplaza por un fallback explícito
    para garantizar que NUNCA salga un popup en blanco.
    """

    title: str = ""
    message: str = ""
    severity: str = SEVERITY_INFO
    primary_label: str = ""
    secondary_label: str = ""
    cancel_label: str = ""
    details: str = ""
    expert_details: str = ""

    def normalized(self) -> "NevlanDialogSpec":
        title = (self.title or "").strip() or EMPTY_TITLE_FALLBACK
        message = (self.message or "").strip() or EMPTY_MESSAGE_FALLBACK
        sev = self.severity if self.severity in _KNOWN_SEVERITIES \
            else SEVERITY_INFO
        # Si no se pasó ningún botón, generamos uno por defecto para
        # que SIEMPRE haya manera de cerrar.
        primary = (self.primary_label or "").strip()
        secondary = (self.secondary_label or "").strip()
        cancel = (self.cancel_label or "").strip()
        if not (primary or secondary or cancel):
            primary = MIN_BUTTON
        return NevlanDialogSpec(
            title=title,
            message=message,
            severity=sev,
            primary_label=primary,
            secondary_label=secondary,
            cancel_label=cancel,
            details=(self.details or "").strip(),
            expert_details=(self.expert_details or "").strip(),
        )


class NevlanDialog(QDialog):
    """Diálogo Nevlan único para warnings, errores, confirmaciones y
    detalles técnicos.

    Por contrato (PRD 2026-05-08c §I + §D):
      * Siempre tiene title visible.
      * Siempre tiene message visible.
      * Siempre tiene al menos un botón.
      * No usa stylesheets nativos del SO — todo NevlanTheme.
      * Soporta "Ver detalles técnicos" colapsables.
      * Tecla Escape cancela.
    """

    def __init__(
        self,
        spec: NevlanDialogSpec,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._spec = spec.normalized()
        self._result_key: str = RESULT_CLOSE
        self._build()

    # ── API resultado ────────────────────────────────────────────
    @property
    def result_key(self) -> str:
        return self._result_key

    # ── Construcción ─────────────────────────────────────────────
    def _build(self) -> None:
        self.setWindowTitle(self._spec.title)
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setStyleSheet(self._stylesheet())
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header con icono + título.
        header = QFrame()
        header.setObjectName("nvlHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 18, 20, 18)
        header_layout.setSpacing(12)

        color, icon = _SEVERITY_COLORS.get(
            self._spec.severity, _SEVERITY_COLORS[SEVERITY_INFO],
        )
        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet(
            f"color: {color}; font-size: 22px; font-weight: 700; "
            f"font-family: {_FONT};"
        )
        icon_lbl.setFixedWidth(28)
        title_lbl = QLabel(self._spec.title)
        title_lbl.setObjectName("nvlTitle")
        title_lbl.setWordWrap(True)
        header_layout.addWidget(icon_lbl)
        header_layout.addWidget(title_lbl, 1)

        # Body con mensaje principal.
        body = QFrame()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 18, 20, 8)
        body_layout.setSpacing(12)

        msg_lbl = QLabel(self._spec.message)
        msg_lbl.setObjectName("nvlMessage")
        msg_lbl.setWordWrap(True)
        msg_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body_layout.addWidget(msg_lbl)

        if self._spec.details:
            details_lbl = QLabel(self._spec.details)
            details_lbl.setObjectName("nvlDetails")
            details_lbl.setWordWrap(True)
            details_lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            body_layout.addWidget(details_lbl)

        # Expert details collapsible.
        if self._spec.expert_details:
            self._expert_panel = QFrame()
            ep_layout = QVBoxLayout(self._expert_panel)
            ep_layout.setContentsMargins(0, 6, 0, 0)
            ep_layout.setSpacing(6)
            self._expert_text = QLabel(self._spec.expert_details)
            self._expert_text.setObjectName("nvlExpert")
            self._expert_text.setWordWrap(True)
            self._expert_text.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            ep_layout.addWidget(self._expert_text)
            self._expert_panel.hide()

            self._toggle_btn = QToolButton()
            self._toggle_btn.setObjectName("nvlExpertToggle")
            self._toggle_btn.setText("Ver detalles técnicos")
            self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._toggle_btn.setCheckable(True)
            self._toggle_btn.toggled.connect(self._on_toggle_expert)
            self._toggle_btn.setSizePolicy(
                QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed,
            )
            body_layout.addWidget(self._toggle_btn)
            body_layout.addWidget(self._expert_panel)

        # Footer con botones.
        footer = QFrame()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(20, 10, 20, 18)
        footer_layout.setSpacing(10)
        footer_layout.addStretch(1)

        if self._spec.cancel_label:
            btn = QPushButton(self._spec.cancel_label)
            btn.setObjectName("nvlBtnCancel")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda: self._finish(RESULT_CANCEL))
            footer_layout.addWidget(btn)
        if self._spec.secondary_label:
            btn = QPushButton(self._spec.secondary_label)
            btn.setObjectName("nvlBtnSecondary")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda: self._finish(RESULT_SECONDARY))
            footer_layout.addWidget(btn)
        if self._spec.primary_label:
            btn = QPushButton(self._spec.primary_label)
            btn.setObjectName("nvlBtnPrimary")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setDefault(True)
            btn.setAutoDefault(True)
            btn.clicked.connect(lambda: self._finish(RESULT_PRIMARY))
            footer_layout.addWidget(btn)

        root.addWidget(header)
        root.addWidget(body, 1)
        root.addWidget(footer)

    def _on_toggle_expert(self, on: bool) -> None:
        self._expert_panel.setVisible(on)
        self._toggle_btn.setText(
            "Ocultar detalles técnicos" if on else "Ver detalles técnicos"
        )

    def _finish(self, key: str) -> None:
        self._result_key = key
        self.accept()

    # ── Stylesheet ───────────────────────────────────────────────
    def _stylesheet(self) -> str:
        sev_color = _SEVERITY_COLORS.get(
            self._spec.severity, _SEVERITY_COLORS[SEVERITY_INFO],
        )[0]
        return f"""
            QDialog {{
                background: {_BG_DIALOG};
                color: {_TEXT_PRIMARY};
                font-family: {_FONT};
                border: 1px solid {_BORDER};
                border-radius: 10px;
            }}
            #nvlHeader {{
                background: {_BG_HEADER};
                border-bottom: 1px solid {_BORDER};
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }}
            #nvlTitle {{
                color: {_TEXT_PRIMARY};
                font-size: 16px;
                font-weight: 700;
            }}
            #nvlMessage {{
                color: {_TEXT_PRIMARY};
                font-size: 14px;
                line-height: 1.45;
            }}
            #nvlDetails {{
                color: {_TEXT_SECONDARY};
                font-size: 12.5px;
            }}
            #nvlExpertToggle {{
                color: {_TEXT_SECONDARY};
                background: transparent;
                border: 1px dashed {_BORDER};
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
            }}
            #nvlExpertToggle:hover {{
                color: {_TEXT_PRIMARY};
                border-color: {_ACCENT};
            }}
            #nvlExpert {{
                color: {_TEXT_SECONDARY};
                background: rgba(255,255,255,0.04);
                border: 1px solid {_BORDER};
                border-radius: 6px;
                padding: 10px;
                font-family: 'Cascadia Mono', 'Consolas', monospace;
                font-size: 11.5px;
            }}
            QPushButton#nvlBtnPrimary {{
                background: {sev_color};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 600;
                font-size: 13px;
            }}
            QPushButton#nvlBtnPrimary:hover {{
                background: {_ACCENT_HOVER};
            }}
            QPushButton#nvlBtnSecondary {{
                background: rgba(255,255,255,0.06);
                color: {_TEXT_PRIMARY};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 500;
                font-size: 13px;
            }}
            QPushButton#nvlBtnSecondary:hover {{
                background: rgba(255,255,255,0.12);
            }}
            QPushButton#nvlBtnCancel {{
                background: transparent;
                color: {_TEXT_SECONDARY};
                border: 1px solid {_BORDER};
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 500;
                font-size: 13px;
            }}
            QPushButton#nvlBtnCancel:hover {{
                color: {_TEXT_PRIMARY};
                border-color: {_DANGER};
            }}
        """


# ─────────────────────────────────────────────────────────────────
# Atajos de uso
# ─────────────────────────────────────────────────────────────────


def show_warning(
    parent: Optional[QWidget],
    *,
    title: str,
    message: str,
    primary: str = "Continuar",
    secondary: str = "",
    cancel: str = "Cancelar",
    details: str = "",
    expert_details: str = "",
) -> str:
    """Muestra un diálogo de tipo ``warning`` y devuelve la clave del
    botón clickeado (``primary`` / ``secondary`` / ``cancel`` / ``close``).
    """
    dlg = NevlanDialog(
        NevlanDialogSpec(
            title=title, message=message,
            severity=SEVERITY_WARNING,
            primary_label=primary,
            secondary_label=secondary,
            cancel_label=cancel,
            details=details,
            expert_details=expert_details,
        ),
        parent=parent,
    )
    dlg.exec()
    return dlg.result_key


def show_danger(
    parent: Optional[QWidget],
    *,
    title: str,
    message: str,
    primary: str = "Aceptar riesgo",
    secondary: str = "",
    cancel: str = "Cancelar",
    details: str = "",
    expert_details: str = "",
) -> str:
    dlg = NevlanDialog(
        NevlanDialogSpec(
            title=title, message=message,
            severity=SEVERITY_DANGER,
            primary_label=primary,
            secondary_label=secondary,
            cancel_label=cancel,
            details=details,
            expert_details=expert_details,
        ),
        parent=parent,
    )
    dlg.exec()
    return dlg.result_key


def show_error(
    parent: Optional[QWidget],
    *,
    title: str,
    message: str,
    primary: str = "Aceptar",
    details: str = "",
    expert_details: str = "",
) -> str:
    dlg = NevlanDialog(
        NevlanDialogSpec(
            title=title, message=message,
            severity=SEVERITY_DANGER,
            primary_label=primary,
            details=details,
            expert_details=expert_details,
        ),
        parent=parent,
    )
    dlg.exec()
    return dlg.result_key


def show_confirmation(
    parent: Optional[QWidget],
    *,
    title: str,
    message: str,
    primary: str = "Continuar",
    cancel: str = "Cancelar",
    details: str = "",
) -> str:
    dlg = NevlanDialog(
        NevlanDialogSpec(
            title=title, message=message,
            severity=SEVERITY_CONFIRMATION,
            primary_label=primary,
            cancel_label=cancel,
            details=details,
        ),
        parent=parent,
    )
    dlg.exec()
    return dlg.result_key


def show_expert_details(
    parent: Optional[QWidget],
    *,
    title: str,
    message: str,
    expert_details: str,
) -> str:
    dlg = NevlanDialog(
        NevlanDialogSpec(
            title=title, message=message,
            severity=SEVERITY_EXPERT,
            expert_details=expert_details,
            primary_label="Cerrar",
        ),
        parent=parent,
    )
    dlg.exec()
    return dlg.result_key


# ─────────────────────────────────────────────────────────────────────
# show_text_input (PRD 2026-05-08d §A.4)
# ─────────────────────────────────────────────────────────────────────


def show_text_input(
    parent: Optional[QWidget],
    *,
    title: str,
    message: str,
    placeholder: str = "",
    initial_value: str = "",
    primary: str = "Confirmar",
    cancel: str = "Cancelar",
    severity: str = SEVERITY_CONFIRMATION,
    field_label: str = "",
) -> Tuple[str, str]:
    """Diálogo Nevlan con un input de texto. PRD §A.4 — usado por
    Mission Review para confirmar el ``profile_name`` cuando el
    raw_trace no trae evidencia robusta.

    Returns:
        Tupla ``(result_key, value)``. ``value`` es el texto escrito
        por el usuario (ya stripped). Si ``result_key != RESULT_PRIMARY``,
        ``value`` se devuelve aunque el usuario haya cancelado, por
        si el caller quiere persistir borradores.
    """
    spec = NevlanDialogSpec(
        title=title,
        message=message,
        severity=severity,
        primary_label=primary,
        cancel_label=cancel,
    ).normalized()

    dlg = QDialog(parent)
    dlg.setWindowTitle(spec.title)
    dlg.setModal(True)
    dlg.setMinimumWidth(480)
    dlg.setStyleSheet(f"""
        QDialog {{ background: {_BG_DIALOG}; color: {_TEXT_PRIMARY};
                   font-family: {_FONT}; border: 1px solid {_BORDER};
                   border-radius: 10px; }}
        #nvlHeader {{ background: {_BG_HEADER};
                      border-bottom: 1px solid {_BORDER};
                      border-top-left-radius: 10px;
                      border-top-right-radius: 10px; }}
        #nvlTitle {{ color: {_TEXT_PRIMARY}; font-size: 16px;
                     font-weight: 700; }}
        #nvlMessage {{ color: {_TEXT_PRIMARY}; font-size: 14px;
                       line-height: 1.45; }}
        #nvlFieldLabel {{ color: {_TEXT_SECONDARY}; font-size: 12px;
                          font-weight: 600; }}
        QLineEdit#nvlInput {{
            background: rgba(0,0,0,0.35);
            border: 1px solid {_BORDER};
            border-radius: 6px;
            color: {_TEXT_PRIMARY};
            padding: 8px 10px;
            font-size: 13px;
        }}
        QLineEdit#nvlInput:focus {{
            border: 1px solid {_ACCENT};
        }}
        QPushButton#nvlBtnPrimary {{
            background: {_ACCENT}; color: #ffffff; border: none;
            border-radius: 6px; padding: 8px 16px;
            font-weight: 600; font-size: 13px;
        }}
        QPushButton#nvlBtnPrimary:hover {{ background: {_ACCENT_HOVER}; }}
        QPushButton#nvlBtnPrimary:disabled {{
            background: rgba(255,255,255,0.10);
            color: rgba(255,255,255,0.40);
        }}
        QPushButton#nvlBtnCancel {{
            background: rgba(255,255,255,0.06);
            color: {_TEXT_PRIMARY};
            border: 1px solid {_BORDER};
            border-radius: 6px; padding: 8px 16px;
            font-weight: 600; font-size: 13px;
        }}
    """)

    color, icon = _SEVERITY_COLORS.get(spec.severity, _SEVERITY_COLORS[SEVERITY_INFO])

    root = QVBoxLayout(dlg)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    header = QFrame()
    header.setObjectName("nvlHeader")
    h_layout = QHBoxLayout(header)
    h_layout.setContentsMargins(20, 18, 20, 18)
    h_layout.setSpacing(12)
    icon_lbl = QLabel(icon)
    icon_lbl.setStyleSheet(
        f"color: {color}; font-size: 22px; font-weight: 700; "
        f"font-family: {_FONT};"
    )
    icon_lbl.setFixedWidth(28)
    title_lbl = QLabel(spec.title)
    title_lbl.setObjectName("nvlTitle")
    title_lbl.setWordWrap(True)
    h_layout.addWidget(icon_lbl)
    h_layout.addWidget(title_lbl, 1)

    body = QFrame()
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(20, 18, 20, 8)
    body_layout.setSpacing(10)
    msg_lbl = QLabel(spec.message)
    msg_lbl.setObjectName("nvlMessage")
    msg_lbl.setWordWrap(True)
    body_layout.addWidget(msg_lbl)

    if field_label:
        flbl = QLabel(field_label)
        flbl.setObjectName("nvlFieldLabel")
        body_layout.addWidget(flbl)

    line = QLineEdit(initial_value)
    line.setObjectName("nvlInput")
    if placeholder:
        line.setPlaceholderText(placeholder)
    body_layout.addWidget(line)

    footer = QFrame()
    f_layout = QHBoxLayout(footer)
    f_layout.setContentsMargins(20, 10, 20, 18)
    f_layout.setSpacing(10)
    f_layout.addStretch(1)

    state = {"key": RESULT_CLOSE}

    def _do_cancel() -> None:
        state["key"] = RESULT_CANCEL
        dlg.reject()

    def _do_primary() -> None:
        if not line.text().strip():
            return  # primary deshabilitado: nunca llega aquí
        state["key"] = RESULT_PRIMARY
        dlg.accept()

    if spec.cancel_label:
        btn_cancel = QPushButton(spec.cancel_label)
        btn_cancel.setObjectName("nvlBtnCancel")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.clicked.connect(_do_cancel)
        f_layout.addWidget(btn_cancel)

    btn_primary = QPushButton(spec.primary_label)
    btn_primary.setObjectName("nvlBtnPrimary")
    btn_primary.setCursor(Qt.CursorShape.PointingHandCursor)
    btn_primary.setDefault(True)
    btn_primary.setAutoDefault(True)
    btn_primary.clicked.connect(_do_primary)
    f_layout.addWidget(btn_primary)

    def _on_text_changed(txt: str) -> None:
        btn_primary.setEnabled(bool(txt.strip()))

    line.textChanged.connect(_on_text_changed)
    btn_primary.setEnabled(bool(initial_value.strip()))
    line.returnPressed.connect(_do_primary)

    root.addWidget(header)
    root.addWidget(body, 1)
    root.addWidget(footer)

    line.setFocus()
    line.selectAll()
    dlg.exec()
    return state["key"], (line.text() or "").strip()


__all__ = [
    "NevlanDialog",
    "NevlanDialogSpec",
    "SEVERITY_INFO",
    "SEVERITY_SUCCESS",
    "SEVERITY_WARNING",
    "SEVERITY_DANGER",
    "SEVERITY_CONFIRMATION",
    "SEVERITY_EXPERT",
    "RESULT_PRIMARY",
    "RESULT_SECONDARY",
    "RESULT_CANCEL",
    "RESULT_CLOSE",
    "MIN_BUTTON",
    "EMPTY_TITLE_FALLBACK",
    "EMPTY_MESSAGE_FALLBACK",
    "show_confirmation",
    "show_danger",
    "show_error",
    "show_expert_details",
    "show_text_input",
    "show_warning",
]
