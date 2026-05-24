"""
Nevlan — Mission Review UX V2 (vista simple)
=============================================

Vista colapsada de Mission Review: el usuario ve **bloques humanos**
(no 10 pasos técnicos), con un check / warning por cada uno y un
botón "Reparar automáticamente".

Filosofía
---------

    El usuario NO arregla el recorder. El recorder entiende al usuario.

Mostrar 30 pasos UIA + locators web + coords es lo contrario de
entender. La vista simple muestra los ``action_groups`` de la misión
(generados por el compiler V2) — típicamente 4-6 bloques —, cada uno
con:

  * ✔ verde si todos sus steps son ``EXECUTABLE``.
  * ⚠ ámbar si tiene ``NEEDS_REVIEW`` (Quick Reinforcement abrirá los
    pendientes).
  * ✖ rojo si hay ``COMPILED_DRAFT`` no resoluble (ghost missing,
    targets vacíos…).

Botones:

  * **Reparar automáticamente** — corre ``intent_compiler_v2.apply``
    sobre el raw_trace original, lo que regenera bloques limpios.
  * **Vista detallada** — fallback al ``MissionReviewDialog`` clásico.

Si la misión NO tiene ``action_groups`` (legacy), la vista simple
los reconstruye a partir del compiled_execution_graph al vuelo.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from app.contracts.mission import (
    ActionGroup, ActionStrategy, CompiledStep, Mission, MissionStatus,
)
from app.core.logger import log

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QKeySequence, QShortcut
    from PyQt6.QtWidgets import (
        QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
        QScrollArea, QVBoxLayout, QWidget,
    )
    HAS_QT = True
except Exception as _e:
    HAS_QT = False
    log.warning(f"[mission_review_simple] PyQt6 no disponible: {_e}")


# ──────────────────────────────────────────────────────────────────────
# Reconstrucción de bloques cuando la misión no los trae
# ──────────────────────────────────────────────────────────────────────

def _reconstruct_groups(mission: Mission) -> List[ActionGroup]:
    """Construye action_groups simples a partir del grafo compilado.

    Estrategia: agrupamos por *tipo* de step (open_app, select_profile,
    open_url, search) — cada secuencia continua del mismo tipo se
    convierte en un grupo. Sirve para misiones legacy que no pasaron
    por el intent_compiler_v2.
    """
    groups: List[ActionGroup] = []
    if not mission or not mission.compiled_execution_graph:
        return groups
    cur_kind = ""
    cur_ids: List[str] = []
    for cs in mission.compiled_execution_graph:
        kind = _step_kind(cs)
        if kind != cur_kind and cur_ids:
            groups.append(ActionGroup(
                group_title=_human_block_title(cur_kind),
                step_ids=cur_ids,
                semantic_tag=cur_kind,
                quality_level="green",
            ))
            cur_ids = []
        cur_kind = kind
        cur_ids.append(cs.id)
    if cur_ids:
        groups.append(ActionGroup(
            group_title=_human_block_title(cur_kind),
            step_ids=cur_ids,
            semantic_tag=cur_kind,
            quality_level="green",
        ))
    return groups


def _step_kind(cs: CompiledStep) -> str:
    a = cs.action_strategy
    if a == ActionStrategy.LAUNCH_APP:
        return "open_app"
    if a == ActionStrategy.SELECT_PROFILE:
        return "select_profile"
    if a in (ActionStrategy.OPEN_NEW_TAB, ActionStrategy.OPEN_BOOKMARK):
        return "open_url"
    if a in (ActionStrategy.SUBMIT_SEARCH, ActionStrategy.SET_FIELD_VALUE):
        return "search"
    if a == ActionStrategy.NAVIGATE_OR_SEARCH:
        return "open_url"
    if a in (ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK):
        return "click"
    if a == ActionStrategy.WAIT_FOR_STATE:
        return "wait"
    return a.value


def _human_block_title(kind: str) -> str:
    titles = {
        "open_app": "Abrir aplicación",
        "select_profile": "Seleccionar perfil",
        "open_url": "Abrir destino",
        "search": "Buscar",
        "click": "Acción",
        "wait": "Esperar a que cargue",
    }
    return titles.get(kind, kind.replace("_", " ").title())


# ──────────────────────────────────────────────────────────────────────
# Estado del bloque (✔ ⚠ ✖)
# ──────────────────────────────────────────────────────────────────────

def _block_status(mission: Mission, group: ActionGroup) -> str:
    """Devuelve "ok" | "warn" | "fail" según los steps que contiene.

    PRD 2026-05-06 §6:
      * ``ghost_status == "semantic_override"`` → NO se considera fail
        ni warn (se trata como "ok"). El executor lo resuelve en
        runtime; en modo experto se mostraría un tooltip.
      * ``ghost_status == "soft_warning"`` → warn no bloqueante.

    PRD 2026-05-06b §2:
      * Si el step tiene ``execution_confidence`` GREEN no contamos
        capture rojo como ``fail`` (la estrategia runtime es robusta).
      * Sólo bloqueamos por necesidades humanas reales o por
        ``execution_confidence`` RED + ``is_blocking``.
    """
    try:
        from app.services.missions.execution_confidence import (
            ExecLevel,
            compute_execution_confidence,
        )
    except Exception:  # pragma: no cover
        ExecLevel = None  # type: ignore
        compute_execution_confidence = None  # type: ignore
    steps_by_id = {cs.id: cs for cs in mission.compiled_execution_graph or []}
    has_warn = False
    has_fail = False
    for sid in group.step_ids:
        cs = steps_by_id.get(sid)
        if cs is None:
            continue
        pl = cs.action_payload or {}
        # Calcular execution_confidence (idempotente — actualiza payload).
        exec_r = None
        if compute_execution_confidence is not None:
            try:
                exec_r = compute_execution_confidence(cs, mission=mission)
            except Exception:
                exec_r = None
        ghost_status = str(pl.get("ghost_status") or "").strip()
        if ghost_status == "semantic_override":
            continue
        if ghost_status == "soft_warning":
            has_warn = True
            continue
        if pl.get("ghost_missing"):
            has_fail = True
        if pl.get("needs_user_label") or pl.get("needs_reinforcement"):
            has_warn = True
        # capture rojo SOLO cuenta como fail si execution también es débil.
        try:
            ql = (cs.target_context.confidence or {}).get("quality_level")
        except Exception:
            ql = None
        if ql == "red":
            if exec_r is not None and ExecLevel is not None \
                    and exec_r.level == ExecLevel.GREEN \
                    and not exec_r.is_blocking:
                # Capture rojo pero ejecución robusta: no es fail.
                pass
            else:
                has_fail = True
        # Bloqueante "duro" del execution_confidence (unknown_kind, etc.)
        if exec_r is not None and exec_r.is_blocking:
            has_fail = True
    if has_fail:
        return "fail"
    if has_warn:
        return "warn"
    return "ok"


# ──────────────────────────────────────────────────────────────────────
# Diálogo Qt
# ──────────────────────────────────────────────────────────────────────

if HAS_QT:

    _ICONS = {"ok": "✔", "warn": "⚠", "fail": "✖"}
    _COLORS = {
        "ok": "#26de81",
        "warn": "#ffa500",
        "fail": "#ff7675",
    }

    class MissionReviewSimpleDialog(QDialog):
        """Vista simple de revisión: bloques humanos + botones de
        acción rápida.  Cierra con accept/reject según interacción.
        """

        def __init__(
            self,
            mission: Mission,
            parent=None,
            *,
            on_open_detailed: Optional[Callable[[], None]] = None,
        ) -> None:
            super().__init__(parent)
            self.mission = mission
            self._on_open_detailed = on_open_detailed
            self._dirty = False
            self.setWindowTitle("Nevlan — Revisar misión")
            self.setMinimumWidth(560)
            self._build()

        def _build(self) -> None:
            root = QVBoxLayout(self)
            root.setContentsMargins(18, 16, 18, 14)
            root.setSpacing(10)

            title = QLabel(f"<b>{self.mission.name or 'Misión sin nombre'}</b>")
            title.setStyleSheet("color: #fff; font-size: 14px;")
            root.addWidget(title)

            sub = QLabel(
                "<span style='color:#aaa;font-size:11px;'>"
                "Vista simple — bloques semánticos. "
                "Atajos: Enter = Guardar, Esc = Cancelar, F5 = Reparar."
                "</span>"
            )
            sub.setWordWrap(True)
            root.addWidget(sub)

            # ── Contenedor de bloques ──────────────────────────────
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("QScrollArea { border: none; }")
            host = QWidget()
            self._host_layout = QVBoxLayout(host)
            self._host_layout.setContentsMargins(0, 0, 0, 0)
            self._host_layout.setSpacing(6)
            scroll.setWidget(host)
            root.addWidget(scroll, 1)

            self._render_blocks()

            # ── Botones de acción ──────────────────────────────────
            row = QHBoxLayout()
            self.btn_repair = QPushButton("🩹 Reparar automáticamente")
            self.btn_repair.setStyleSheet(
                "QPushButton { background: rgba(255,200,80,0.18);"
                " color: #ffd166; border: 1px solid rgba(255,200,80,0.25);"
                " border-radius: 4px; padding: 6px 12px; }"
                "QPushButton:hover { background: rgba(255,200,80,0.32); }"
                "QPushButton:focus { outline: 2px solid #ffd166; }"
            )
            self.btn_repair.clicked.connect(self._do_auto_repair)
            row.addWidget(self.btn_repair)

            self.btn_detailed = QPushButton("Ver detalle técnico")
            self.btn_detailed.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.08);"
                " color: #ddd; border: 1px solid rgba(255,255,255,0.18);"
                " border-radius: 4px; padding: 6px 10px; }"
                "QPushButton:hover { background: rgba(255,255,255,0.14); }"
            )
            self.btn_detailed.clicked.connect(self._open_detailed)
            row.addWidget(self.btn_detailed)

            row.addStretch(1)
            self.btn_cancel = QPushButton("Cancelar")
            self.btn_cancel.setShortcut("Escape")
            self.btn_cancel.clicked.connect(self.reject)
            row.addWidget(self.btn_cancel)

            self.btn_save = QPushButton("Guardar")
            self.btn_save.setStyleSheet(
                "QPushButton { background: #0984e3; color: white;"
                " border: none; border-radius: 4px; padding: 6px 14px;"
                " font-weight: 600; }"
                "QPushButton:hover { background: #74b9ff; }"
                "QPushButton:focus { outline: 2px solid #fff; }"
            )
            self.btn_save.setDefault(True)
            self.btn_save.setAutoDefault(True)
            self.btn_save.clicked.connect(self.accept)
            row.addWidget(self.btn_save)
            root.addLayout(row)

            # F5 dispara reparación.
            self._sc_repair = QShortcut(QKeySequence("F5"), self)
            self._sc_repair.activated.connect(self._do_auto_repair)

            # Foco al botón guardar.
            self.btn_save.setFocus(Qt.FocusReason.OtherFocusReason)

        def _render_blocks(self) -> None:
            # Limpia hijos.
            while self._host_layout.count() > 0:
                it = self._host_layout.takeAt(0)
                w = it.widget()
                if w:
                    w.deleteLater()

            groups = self.mission.action_groups or _reconstruct_groups(self.mission)
            if not groups:
                lbl = QLabel(
                    "<i>Esta misión no tiene pasos compilados.</i>"
                )
                lbl.setStyleSheet("color: #aaa; padding: 12px;")
                self._host_layout.addWidget(lbl)
                return

            for grp in groups:
                status = _block_status(self.mission, grp)
                self._host_layout.addWidget(self._block_card(grp, status))
            self._host_layout.addStretch(1)

        def _block_card(self, grp: ActionGroup, status: str) -> QFrame:
            card = QFrame()
            color = _COLORS.get(status, "#aaa")
            card.setStyleSheet(
                f"QFrame {{ background: rgba(255,255,255,0.06);"
                f" border: 1px solid {color}33;"
                f" border-radius: 6px; padding: 6px; }}"
            )
            v = QVBoxLayout(card)
            v.setContentsMargins(10, 8, 10, 8)
            v.setSpacing(2)

            title = QLabel(
                f"<span style='color:{color};font-size:14px;'>"
                f"{_ICONS.get(status, '·')}</span>  "
                f"<b style='color:white;'>{grp.group_title or '(sin título)'}</b>"
                f"  <span style='color:#888;font-size:11px;'>"
                f"({len(grp.step_ids)} pasos)</span>"
            )
            title.setTextFormat(Qt.TextFormat.RichText)
            v.addWidget(title)

            if grp.group_summary and grp.group_summary != grp.group_title:
                sub = QLabel(f"<span style='color:#aaa;font-size:11px;'>{grp.group_summary}</span>")
                v.addWidget(sub)

            if status != "ok":
                # PRD 2026-05-06 §6: si TODOS los warns del bloque
                # provienen de soft_warning del ghost, mostramos un
                # mensaje no bloqueante en vez del genérico de
                # "necesita confirmación humana".
                only_soft = self._block_only_soft_warning(grp) if status == "warn" else False
                if status == "warn" and only_soft:
                    hint = "Se validará durante ejecución."
                elif status == "warn":
                    hint = "⚠ Necesita confirmación humana."
                else:
                    hint = "✖ El target no está disponible."
                hl = QLabel(f"<i style='color:{color};font-size:11px;'>{hint}</i>")
                v.addWidget(hl)
            return card

        def _block_only_soft_warning(self, grp: ActionGroup) -> bool:
            """¿El bloque sólo está en warn por ``ghost_status=soft_warning``?

            Si hay otra fuente de warning (needs_user_label, etc.),
            mantenemos el copy original. Si ningún step es soft_warning,
            tampoco aplica.
            """
            steps_by_id = {
                cs.id: cs for cs in self.mission.compiled_execution_graph or []
            }
            saw_soft = False
            for sid in grp.step_ids or []:
                cs = steps_by_id.get(sid)
                if cs is None:
                    continue
                pl = cs.action_payload or {}
                ghost_status = str(pl.get("ghost_status") or "").strip()
                if ghost_status == "soft_warning":
                    saw_soft = True
                    continue
                if ghost_status == "semantic_override":
                    continue
                if pl.get("needs_user_label") or pl.get("needs_reinforcement"):
                    return False
                try:
                    ql = (cs.target_context.confidence or {}).get("quality_level")
                except Exception:
                    ql = None
                if ql == "red":
                    return False
            return saw_soft

        # ── Botón "Reparar automáticamente" ────────────────────────
        def _do_auto_repair(self) -> None:
            try:
                from app.services.missions.intent_compiler_v2 import (
                    apply_intent_to_mission,
                )
                rep = apply_intent_to_mission(self.mission)
                self._dirty = True
                QMessageBox.information(
                    self, "Reparación automática",
                    f"Se regeneraron {rep.steps_emitted} pasos en "
                    f"{rep.blocks_emitted} bloques. "
                    f"({rep.rescued_invalid_targets} targets rescatados, "
                    f"{rep.waits_injected} esperas dinámicas inyectadas)."
                )
                self._render_blocks()
            except Exception as e:
                log.error(f"[mission_review_simple] auto_repair: {e}")
                QMessageBox.warning(
                    self, "Reparación automática",
                    f"No se pudo reparar automáticamente: {e}"
                )

        def _open_detailed(self) -> None:
            if self._on_open_detailed:
                try:
                    self._on_open_detailed()
                except Exception as e:
                    log.error(f"open detailed: {e}")
            self.accept()

        # ── Hotkey override ────────────────────────────────────────
        def keyPressEvent(self, event):  # type: ignore[override]
            try:
                k = event.key()
                if k == Qt.Key.Key_Escape:
                    self.reject()
                    event.accept()
                    return
                if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self.accept()
                    event.accept()
                    return
            except Exception:
                pass
            return super().keyPressEvent(event)

else:  # pragma: no cover
    class MissionReviewSimpleDialog:  # type: ignore[no-redef]
        def __init__(self, mission: Mission, parent=None, **_kw) -> None:
            self.mission = mission

        def exec(self) -> int:
            log.info("MissionReviewSimpleDialog stub (Qt no disponible)")
            return 0


__all__ = ["MissionReviewSimpleDialog"]
