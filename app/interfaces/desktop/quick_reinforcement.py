"""
Nevlan — Quick Reinforcement UI
================================

Diálogo modal que se levanta cuando una misión queda en
``MissionStatus.NEEDS_REVIEW`` por uno de los siguientes motivos
(detectados por el ``approval_gate``):

  * ``needs_user_label`` — el step trae ``needs_user_label=True`` o
    ``needs_reinforcement=True`` con un ``label_prompt`` para el usuario.
  * ``empty_profile`` — ``SELECT_PROFILE`` sin nombre de perfil.
  * ``empty_target_label`` — ``search`` con sitio detectado pero sin
    descriptor canónico de la barra.

Para cada step problemático, mostramos una tarjeta con:

  * el título del step (``intent`` semántico → label humano),
  * la pregunta concreta a responder (``label_prompt``) y la sugerencia
    de Nevlan (``semantic.human_label`` cuando existe),
  * cuatro acciones: **Confirmar intención**, **Editar etiqueta**,
    **Re-grabar paso**, **Eliminar paso**.

Tras pulsar "Confirmar" o "Editar", el helper
``app.services.missions.semantic_fusion.confirm_step_label`` aplica la
etiqueta y limpia los flags de pendiente. El diálogo re-corre el
``approval_gate`` y, si la misión queda limpia, propone aprobarla
``EXECUTABLE`` directamente.

Diseño
------

Self-contained: sólo depende de Qt y de los servicios de misiones.
No mutamos el ``MissionReviewDialog`` para no romper ninguna ruta UI.
El diálogo se abre como modal y devuelve ``QDialog.Accepted`` si el
usuario completó al menos una corrección o ``QDialog.Rejected`` si
canceló sin tocar nada.

NOTA: Mantenemos el código robusto a ausencia de PyQt en CI: si Qt no
puede importarse (ejecución headless), exportamos una versión "noop"
que sólo imprime las intenciones detectadas. Los tests unitarios
deben golpear directamente las funciones puras de
``confirm_step_label`` + ``classify_status``.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from app.contracts.mission import ActionStrategy, CompiledStep, Mission
from app.core.logger import log
from app.services.missions.approval_gate import (
    check_mission_approvable,
    classify_status,
)
from app.services.missions.semantic_fusion import confirm_step_label


# ──────────────────────────────────────────────────────────────────────
# Detección de pasos que necesitan refuerzo (puro, sin Qt)
# ──────────────────────────────────────────────────────────────────────

def steps_needing_reinforcement(
    mission: Mission,
) -> List[Tuple[int, CompiledStep, Dict[str, str]]]:
    """Devuelve los steps que requieren etiqueta humana, junto a un dict
    de "hints" para la UI: ``prompt``, ``suggestion``, ``placeholder``.

    Cada entrada es ``(index, compiled_step, hints)``.
    """
    out: List[Tuple[int, CompiledStep, Dict[str, str]]] = []
    if not mission or not mission.compiled_execution_graph:
        return out
    for i, cs in enumerate(mission.compiled_execution_graph):
        pl = cs.action_payload or {}
        sem = (cs.target_context.semantic if cs.target_context else None) or {}

        # PRD 2026-05-06 §6: si Ghost marcó este step como
        # "semantic_override" (intención fuerte), NO abrimos tarjeta
        # de confirmación. El executor lo resolverá en runtime.
        ghost_status = str(pl.get("ghost_status") or "").strip()
        if ghost_status == "semantic_override":
            continue

        # Pendiente si el step:
        #   - lleva needs_user_label / needs_reinforcement explícito,
        #   - o es SELECT_PROFILE con profile vacío,
        #   - o es un search/submit con site sin target_label.
        is_placeholder = bool(
            pl.get("needs_user_label")
            or pl.get("needs_reinforcement")
            or sem.get("needs_reinforcement")
        )
        is_empty_profile = (
            cs.action_strategy == ActionStrategy.SELECT_PROFILE
            and not str(pl.get("profile", "")).strip()
        )
        is_empty_target = (
            cs.action_strategy
            in (
                ActionStrategy.SUBMIT_SEARCH,
                ActionStrategy.SET_FIELD_VALUE,
                ActionStrategy.TYPE_TEXT,
            )
            and bool(pl.get("submit_after"))
            and bool(pl.get("site") or pl.get("search_site"))
            and not str(pl.get("target_label") or pl.get("target") or "").strip()
        )
        if not (is_placeholder or is_empty_profile or is_empty_target):
            continue

        prompt = (
            str(pl.get("label_prompt") or sem.get("label_prompt") or "").strip()
            or _default_prompt(cs)
        )
        suggestion = str(sem.get("human_label") or "").strip()
        placeholder = _default_placeholder(cs)
        hints = {
            "prompt": prompt,
            "suggestion": suggestion,
            "placeholder": placeholder,
        }
        out.append((i, cs, hints))
    return out


def _default_prompt(cs: CompiledStep) -> str:
    if cs.action_strategy == ActionStrategy.SELECT_PROFILE:
        return "¿Qué perfil seleccionaste?"
    if cs.action_strategy in (
        ActionStrategy.SUBMIT_SEARCH,
        ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        return "¿Cómo se llama el campo de búsqueda?"
    return "¿Sobre qué elemento hiciste click?"


def _default_placeholder(cs: CompiledStep) -> str:
    if cs.action_strategy == ActionStrategy.SELECT_PROFILE:
        # Placeholder UI: pista neutra de qué tipo de dato queremos.
        # NO hardcodeamos un nombre concreto — el resolver es genérico
        # y cualquier "Nombre Apellido" debe valer.
        return "Nombre del perfil"
    if cs.action_strategy in (
        ActionStrategy.SUBMIT_SEARCH,
        ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        return "Campo de búsqueda"
    return "Botón Guardar"


# ──────────────────────────────────────────────────────────────────────
# Aplicación del refuerzo (puro, sin Qt)
# ──────────────────────────────────────────────────────────────────────

class _GenericProfileLabelError(ValueError):
    """Levantada cuando el usuario intenta confirmar un perfil con
    una etiqueta genérica (ej. "perfil del navegador") en vez del
    nombre real (ej. "Daniel Arturo Ramos").
    """


def apply_reinforcement(
    cs: CompiledStep,
    *,
    label: str,
    semantic_label: Optional[str] = None,
) -> None:
    """Aplica una etiqueta humana al step y limpia las marcas de
    pendiente. Atajo sobre ``confirm_step_label`` que decide si el label
    va a ``profile``, ``target_label`` o sólo ``user_confirmed_label``
    según la acción del step.

    PRD 2026-05-07 §5: para ``SELECT_PROFILE``, NUNCA se acepta una
    etiqueta genérica como "perfil del navegador" / "default" — debe
    ser el nombre real del perfil de Chrome (ej. "Daniel Arturo Ramos").
    Si el llamador pasa un label genérico, levantamos
    :class:`_GenericProfileLabelError` para que la UI vuelva a pedirlo.
    """
    label = label.strip()
    if not label:
        return
    if cs.action_strategy == ActionStrategy.SELECT_PROFILE:
        # Validación PRD §5.
        try:
            from app.services.missions.semantic_execution_plan import (
                is_generic_profile_label,
            )
        except Exception:
            is_generic_profile_label = lambda s: False  # type: ignore
        if is_generic_profile_label(label):
            raise _GenericProfileLabelError(
                f'"{label}" es una etiqueta genérica. '
                "Escribe el nombre real del perfil del navegador "
                "(p. ej. tu nombre y apellido)."
            )
        confirm_step_label(
            cs, profile=label, user_confirmed_label=label,
            semantic_label=semantic_label,
        )
        return
    if cs.action_strategy in (
        ActionStrategy.SUBMIT_SEARCH,
        ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        confirm_step_label(
            cs, target_label=label, user_confirmed_label=label,
            semantic_label=semantic_label,
        )
        return
    confirm_step_label(
        cs, user_confirmed_label=label, semantic_label=semantic_label,
    )


def sync_semantic_plan_after_reinforcement(mission: Mission) -> None:
    """Tras aplicar refuerzos al ``compiled_execution_graph``, reescribe
    ``mission.semantic_execution_plan`` para que refleje los nuevos
    valores (perfil real, target_label confirmado, etc.).

    Reusa el plan ya colapsado: para cada ``SELECT_PROFILE`` con
    ``profile`` no vacío, marca el step semántico equivalente como
    ``needs_user_label=False`` e inyecta el ``profile``. Para
    ``SUBMIT_SEARCH``/``SET_FIELD_VALUE`` con ``target_label``
    confirmado, hace lo mismo en el campo ``target``.

    Más simple y robusto que mantener back-references entre cada
    ``CompiledStep`` y su ``SemanticPlanStep``.
    """
    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict):
        return
    plan_steps = sep.get("steps") or []
    if not isinstance(plan_steps, list):
        return

    by_type: Dict[str, List[Dict[str, object]]] = {}
    for ps in plan_steps:
        if not isinstance(ps, dict):
            continue
        by_type.setdefault(str(ps.get("type") or ""), []).append(ps)

    for cs in mission.compiled_execution_graph or []:
        pl = cs.action_payload or {}
        if cs.action_strategy == ActionStrategy.SELECT_PROFILE:
            profile = str(pl.get("profile", "")).strip()
            if not profile:
                continue
            for ps in by_type.get("select_profile", []):
                params = ps.setdefault("params", {})
                # Sincronizamos AMBOS para que tanto el ApprovalGate
                # como el SmartExecutor encuentren el valor (algunos
                # consumers leen ``profile``, otros ``profile_name``).
                params["profile"] = profile
                params["profile_name"] = profile
                ps["needs_user_label"] = False
        elif cs.action_strategy in (
            ActionStrategy.SUBMIT_SEARCH,
            ActionStrategy.SET_FIELD_VALUE,
            ActionStrategy.TYPE_TEXT,
        ):
            target = str(pl.get("target_label") or pl.get("target") or "").strip()
            if not target:
                continue
            for ps in by_type.get("search_youtube", []):
                params = ps.setdefault("params", {})
                params["target"] = target
                ps["needs_user_label"] = False


def delete_step(mission: Mission, step_index: int) -> bool:
    """Elimina un paso del ``compiled_execution_graph``. Devuelve
    ``True`` si lo borró, ``False`` si el índice estaba fuera de rango.
    """
    if not mission or not mission.compiled_execution_graph:
        return False
    if not (0 <= step_index < len(mission.compiled_execution_graph)):
        return False
    mission.compiled_execution_graph.pop(step_index)
    if mission.interpreted_steps and step_index < len(mission.interpreted_steps):
        mission.interpreted_steps.pop(step_index)
    return True


# ──────────────────────────────────────────────────────────────────────
# Diálogo Qt
# ──────────────────────────────────────────────────────────────────────

try:  # pragma: no cover  (sólo se ejecuta con Qt disponible)
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import (
        QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
        QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
    )

    class QuickReinforcementDialog(QDialog):
        """Diálogo modal con una tarjeta por cada paso pendiente."""

        TITLE = "Nevlan — Confirmación de pasos"

        def __init__(
            self,
            mission: Mission,
            parent=None,
            *,
            on_rerecord: Optional[Callable[[int, CompiledStep], None]] = None,
        ) -> None:
            super().__init__(parent)
            self.mission = mission
            self._on_rerecord = on_rerecord
            self._dirty = False
            self.setWindowTitle(self.TITLE)
            self.setMinimumWidth(620)
            self._build_ui()
            self._render_cards()

        def _build_ui(self) -> None:
            root = QVBoxLayout(self)
            root.setContentsMargins(16, 14, 16, 14)
            root.setSpacing(10)

            header = QLabel(
                "<b>Nevlan aprendió estos pasos, pero necesita confirmación.</b>"
                "<br><span style='color:#aaa;font-size:11px;'>"
                "Confirma la intención sugerida, edita la etiqueta o "
                "vuelve a grabar el paso. Mientras queden pasos sin "
                "etiqueta la misión no podrá ejecutarse."
                "<br><i>Atajos: Enter = guardar, Esc = cancelar, "
                "F5 = re-grabar, Tab = navegar.</i>"
                "</span>"
            )
            header.setWordWrap(True)
            root.addWidget(header)

            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self._cards_host = QWidget()
            self._cards_layout = QVBoxLayout(self._cards_host)
            self._cards_layout.setContentsMargins(0, 0, 0, 0)
            self._cards_layout.setSpacing(8)
            self._cards_layout.addStretch(1)
            self.scroll.setWidget(self._cards_host)
            root.addWidget(self.scroll, stretch=1)

            footer = QHBoxLayout()
            footer.addStretch(1)
            self.btn_close = QPushButton("Terminar")
            self.btn_close.setDefault(True)
            self.btn_close.setAutoDefault(True)
            self.btn_close.clicked.connect(self._on_close)
            footer.addWidget(self.btn_close)
            root.addLayout(footer)

            # Esc cancela siempre.
            from PyQt6.QtGui import QShortcut, QKeySequence
            self._sc_esc = QShortcut(QKeySequence("Escape"), self)
            self._sc_esc.activated.connect(self.reject)
            self._sc_save = QShortcut(QKeySequence("Ctrl+S"), self)
            self._sc_save.activated.connect(self._on_close)

        # Hotkey global (Enter cuando foco no en QLineEdit lo dispara via
        # botón default; mantenemos override por si algún plug-in lo
        # consume antes — defensivo).
        def keyPressEvent(self, event):  # type: ignore[override]
            from PyQt6.QtWidgets import QLineEdit
            try:
                k = event.key()
                if k == Qt.Key.Key_Escape:
                    self.reject()
                    event.accept()
                    return
                if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    fw = self.focusWidget()
                    if isinstance(fw, QLineEdit):
                        # Si es un input de etiqueta, confirmamos esa
                        # tarjeta sin cerrar el diálogo; delegamos al
                        # editor que tiene focus.
                        return super().keyPressEvent(event)
                    self._on_close()
                    event.accept()
                    return
                if k == Qt.Key.Key_F5:
                    # F5 re-graba el primer paso pendiente — atajo de
                    # operador experto. Si no hay handler, no-op.
                    if self._on_rerecord:
                        try:
                            pending = steps_needing_reinforcement(self.mission)
                            if pending:
                                idx, cs, _ = pending[0]
                                self._on_rerecord(idx, cs)
                        except Exception as e:
                            log.debug(f"F5 rerecord: {e}")
                    event.accept()
                    return
            except Exception as e:
                log.debug(f"QuickReinforcement.keyPressEvent: {e}")
            return super().keyPressEvent(event)

        # ---------- rendering ------------------------------------------------

        def _render_cards(self) -> None:
            # Limpiamos
            while self._cards_layout.count() > 1:
                item = self._cards_layout.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()

            pending = steps_needing_reinforcement(self.mission)
            if not pending:
                done = QLabel(
                    "<i>Todos los pasos quedaron etiquetados. La misión "
                    "está lista para guardarse como ejecutable.</i>"
                )
                done.setStyleSheet("color:#9ad97a;padding:24px;")
                self._cards_layout.insertWidget(0, done)
                return

            for idx, cs, hints in pending:
                card = self._build_card(idx, cs, hints)
                self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)

        def _build_card(self, idx: int, cs: CompiledStep, hints: Dict[str, str]) -> QFrame:
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card.setStyleSheet(
                "QFrame{background:rgba(255,255,255,8);"
                "border:1px solid rgba(255,205,86,80);"
                "border-radius:6px;}"
            )
            v = QVBoxLayout(card)
            v.setContentsMargins(12, 10, 12, 10)
            v.setSpacing(6)

            title = QLabel(
                f"<b>Paso {idx + 1}.</b> "
                f"<span style='color:#ffc94d;'>{cs.action_strategy.value}</span>"
            )
            title.setTextFormat(Qt.TextFormat.RichText)
            v.addWidget(title)

            prompt = QLabel(hints.get("prompt", ""))
            prompt.setWordWrap(True)
            prompt.setStyleSheet("color:#ddd;")
            v.addWidget(prompt)

            suggestion = hints.get("suggestion", "")
            placeholder = hints.get("placeholder", "")
            self_input = QLineEdit()
            self_input.setPlaceholderText(suggestion or placeholder)
            v.addWidget(self_input)

            row = QHBoxLayout()
            row.setSpacing(6)
            btn_confirm = QPushButton(
                f"Confirmar: {suggestion}" if suggestion else "Confirmar"
            )
            btn_edit = QPushButton("Editar etiqueta")
            btn_rerecord = QPushButton("Re-grabar")
            btn_delete = QPushButton("Eliminar")
            for b in (btn_confirm, btn_edit, btn_rerecord, btn_delete):
                b.setCursor(Qt.CursorShape.PointingHandCursor)
            if not suggestion:
                btn_confirm.setEnabled(False)
            row.addWidget(btn_confirm)
            row.addWidget(btn_edit)
            row.addStretch(1)
            row.addWidget(btn_rerecord)
            row.addWidget(btn_delete)
            v.addLayout(row)

            def _do_confirm() -> None:
                label = (suggestion or self_input.text() or placeholder).strip()
                if not label:
                    QMessageBox.information(
                        self, "Falta etiqueta",
                        "Escribe una etiqueta antes de confirmar.",
                    )
                    return
                try:
                    apply_reinforcement(
                        cs, label=label, semantic_label=suggestion or None,
                    )
                except _GenericProfileLabelError as gpe:
                    QMessageBox.warning(
                        self, "Etiqueta genérica", str(gpe),
                    )
                    self_input.setFocus()
                    return
                # Sincroniza el plan semántico con el valor confirmado
                # (PRD 2026-05-07 §1 + §5).
                try:
                    sync_semantic_plan_after_reinforcement(self.mission)
                except Exception as e:
                    log.debug(f"sync_semantic_plan: {e}")
                self._dirty = True
                self._render_cards()

            def _do_edit() -> None:
                value = self_input.text().strip() or suggestion or placeholder
                value, ok = QInputDialog.getText(
                    self,
                    "Editar etiqueta",
                    hints.get("prompt", "") or "Etiqueta",
                    text=value,
                )
                if not ok:
                    return
                value = value.strip()
                if not value:
                    return
                try:
                    apply_reinforcement(
                        cs, label=value, semantic_label=suggestion or None,
                    )
                except _GenericProfileLabelError as gpe:
                    QMessageBox.warning(
                        self, "Etiqueta genérica", str(gpe),
                    )
                    return
                try:
                    sync_semantic_plan_after_reinforcement(self.mission)
                except Exception as e:
                    log.debug(f"sync_semantic_plan: {e}")
                self._dirty = True
                self._render_cards()

            def _do_rerecord() -> None:
                if self._on_rerecord is None:
                    QMessageBox.information(
                        self, "Re-grabar",
                        "Re-grabar pasos individuales sólo está "
                        "disponible desde el panel completo de Mission "
                        "Review (botón 🎯).",
                    )
                    return
                try:
                    self._on_rerecord(idx, cs)
                except Exception as e:
                    log.error(f"on_rerecord falló: {e}")
                    QMessageBox.warning(
                        self, "Re-grabar", f"No se pudo iniciar la re-grabación: {e}",
                    )

            def _do_delete() -> None:
                res = QMessageBox.question(
                    self, "Eliminar paso",
                    f"¿Eliminar el paso {idx + 1}? Esta acción no se "
                    "puede deshacer aquí (puedes re-compilar la misión).",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if res != QMessageBox.StandardButton.Yes:
                    return
                if delete_step(self.mission, idx):
                    self._dirty = True
                    self._render_cards()

            btn_confirm.clicked.connect(_do_confirm)
            btn_edit.clicked.connect(_do_edit)
            btn_rerecord.clicked.connect(_do_rerecord)
            btn_delete.clicked.connect(_do_delete)

            return card

        # ---------- close ----------------------------------------------------

        def _on_close(self) -> None:
            # Re-corremos el gate para ver el balance final.
            new_status = classify_status(self.mission)
            report = check_mission_approvable(self.mission)
            log.info(
                f"QuickReinforcement: status={new_status.value} "
                f"violations={len(report.errors)}"
            )
            self._dirty = self._dirty or False
            if self._dirty:
                self.accept()
            else:
                self.reject()

except Exception as _e:  # pragma: no cover  (Qt no disponible)
    log.warning(f"PyQt6 no disponible para QuickReinforcementDialog: {_e}")

    class QuickReinforcementDialog:  # type: ignore[no-redef]
        """Stub headless: no levanta UI, sólo informa por log."""

        def __init__(self, mission: Mission, parent=None, **_kw) -> None:
            self.mission = mission

        def exec(self) -> int:
            pending = steps_needing_reinforcement(self.mission)
            log.info(
                f"QuickReinforcementDialog (stub): {len(pending)} pasos "
                "pendientes de etiqueta humana."
            )
            return 0


__all__ = [
    "QuickReinforcementDialog",
    "steps_needing_reinforcement",
    "apply_reinforcement",
    "sync_semantic_plan_after_reinforcement",
    "delete_step",
]
