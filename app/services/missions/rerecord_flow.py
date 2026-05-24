"""
Nevlan — Rerecord Flow (PRD 2026-05-09 §G — adapter central)
==============================================================

Punto único al que **todos** los entrypoints de regrabación /
edición de pasos delegan, para que un mismo paso de una misma
misión produzca exactamente el mismo resultado independientemente
del botón que lo dispare:

  * "Pasos de la automatización → Regrabar"
  * "Mission Review → Regrabar"
  * "Reparar pasos débiles → Regrabar (1 paso)"
  * "Reparar pasos débiles → Regrabar los N en cola → Regrabar"

Diseño deliberado
-----------------

* **Sin Qt**. Esto NO crea overlays ni dialogs — devuelve un
  ``RerecordFlowHandle`` con un ``RerecordOrchestrator`` ya
  configurado y listo. La UI se encarga de pegar el overlay encima.
* **Verbos canónicos** = los que pidió el usuario en el ticket:
    - :func:`start_rerecord_flow`
    - :func:`save_rerecord`
    - :func:`repeat_rerecord`
    - :func:`cancel_rerecord`
    - :func:`start_vibe_coding_flow`
    - :func:`choose_correct_element_flow`
* **Reutilizamos** lo que ya existe (RerecordOrchestrator,
  vibe_service.apply_vibe_edit, repair.OneClickCapture) — este
  módulo es un adapter, no un re-diseño.
* **Validación temprana**: si la misión no tiene
  ``semantic_execution_plan``, ``start_rerecord_flow`` levanta
  :class:`RerecordSessionError` con mensaje claro — exactamente el
  mismo mensaje que vería el usuario en cualquier entrypoint.

Uso típico desde la UI::

    handle = start_rerecord_flow(
        mission, step_index=4, source_entrypoint="mission_review"
    )
    overlay = build_rerecord_overlay(handle)
    overlay.save_requested.connect(lambda: save_rerecord(handle, evidence))
    overlay.cancel_requested.connect(lambda: cancel_rerecord(handle))

Las funciones ``save_rerecord`` / ``repeat_rerecord`` /
``cancel_rerecord`` son **trampolines explícitos** sobre los
verbos del orchestrator para que cualquier consumidor (botón
inline, atajo de teclado, test) tenga el mismo path de código.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.contracts.mission import Mission
from app.core.logger import log
from app.services.missions.rerecord_session import (
    RerecordOrchestrator,
    RerecordSession,
    RerecordSessionError,
)


# Entrypoints reconocidos. Útiles para audit forense en logs y para
# que la UI sepa si debe mostrar/ocultar el modal de preparación.
ENTRY_AUTOMATION_STEPS = "automation_steps"
ENTRY_MISSION_REVIEW = "mission_review"
ENTRY_REPAIR_WEAK = "repair_weak_steps"
ENTRY_REPAIR_QUEUE = "repair_weak_steps_queue"


#: Umbral de similitud por encima del cual consideramos que el
#: elemento NO cambió visualmente. Si el usuario regrabó pero la
#: nueva huella es casi idéntica a la vieja (≥ 0.92), avisamos en el
#: log que probablemente no haga falta — sin bloquear la operación.
FINGERPRINT_SIMILARITY_UNCHANGED: float = 0.92


@dataclass
class RerecordFingerprintDiff:
    """Comparación de visual_fingerprint entre el paso original y el
    capturado en la regrabación. Se calcula como parte de
    :func:`save_rerecord` y queda disponible en el handle para que la
    UI pueda mostrar un aviso si el elemento "no cambió".
    """

    similarity: float = 0.0
    old_available: bool = False
    new_available: bool = False
    likely_unchanged: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "similarity": round(float(self.similarity), 4),
            "old_available": bool(self.old_available),
            "new_available": bool(self.new_available),
            "likely_unchanged": bool(self.likely_unchanged),
            "reason": str(self.reason or ""),
        }


@dataclass
class RerecordFlowHandle:
    """Handle opaco que expone la sesión + orchestrator + metadata
    de origen. La UI lo trata como un blob — no debe mutar campos
    aquí, solo leerlos para pintar.
    """

    orchestrator: RerecordOrchestrator
    session: RerecordSession
    mission_id: str
    target_step_id: str
    target_step_index: int
    source_entrypoint: str
    queue_total: int = 0
    queue_index: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)
    last_fingerprint_diff: Optional[RerecordFingerprintDiff] = None

    @property
    def is_in_queue(self) -> bool:
        return self.queue_total > 0


# ─────────────────────────────────────────────────────────────────────
# REGRABAR
# ─────────────────────────────────────────────────────────────────────


def start_rerecord_flow(
    mission: Mission,
    *,
    step_index: int,
    source_entrypoint: str,
    queue_index: int = 0,
    queue_total: int = 0,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_state_change: Optional[Callable[[str], None]] = None,
    executor_factory: Optional[Callable[[], Any]] = None,
) -> RerecordFlowHandle:
    """Crea un :class:`RerecordOrchestrator`, arranca la sesión y
    devuelve un handle listo para que la UI conecte el overlay.

    Levanta :class:`RerecordSessionError` si la misión no es
    regrabable (ej. sin ``semantic_execution_plan``).
    """
    if not mission:
        raise RerecordSessionError("mission no puede ser None.")

    orch = RerecordOrchestrator(
        executor_factory=executor_factory,
        on_progress=on_progress,
        on_state_change=on_state_change,
    )
    session = orch.start(mission, target_step_index=step_index)
    handle = RerecordFlowHandle(
        orchestrator=orch,
        session=session,
        mission_id=session.mission_id,
        target_step_id=session.target_step_id,
        target_step_index=session.target_step_index,
        source_entrypoint=str(source_entrypoint or "unknown"),
        queue_total=max(0, int(queue_total)),
        queue_index=max(0, int(queue_index)),
    )
    log.info(
        f"[rerecord_flow] start entry={handle.source_entrypoint!r} "
        f"mission={handle.mission_id} step_idx={handle.target_step_index} "
        f"queue={handle.queue_index}/{handle.queue_total}"
    )
    return handle


def _extract_visual_fingerprint(blob: Any) -> Optional[Dict[str, Any]]:
    """Extrae el visual_fingerprint de un step/evento, sin asumir su
    forma exacta. Lo busca en lugares razonables:

      * ``blob["metadata"]["visual_fingerprint"]`` (raw event)
      * ``blob["target_context"]["vision_data"]["visual_fingerprint"]``
        (compiled step / SEP step)
      * ``blob["visual_fingerprint"]`` (forma directa)

    Devuelve ``None`` si no hay huella o si ``available != True``.
    """
    if not isinstance(blob, dict):
        return None
    candidates: List[Any] = []
    meta = blob.get("metadata")
    if isinstance(meta, dict):
        candidates.append(meta.get("visual_fingerprint"))
    tc = blob.get("target_context")
    if isinstance(tc, dict):
        vd = tc.get("vision_data")
        if isinstance(vd, dict):
            candidates.append(vd.get("visual_fingerprint"))
    candidates.append(blob.get("visual_fingerprint"))
    for c in candidates:
        if isinstance(c, dict) and c.get("available"):
            return dict(c)
    return None


def compute_rerecord_fingerprint_diff(
    *,
    original_step_snapshot: Dict[str, Any],
    captured_evidence: List[Dict[str, Any]],
) -> RerecordFingerprintDiff:
    """Compara el visual_fingerprint del step ORIGINAL contra el del
    primer click de la evidencia NUEVA. Pure-function: testeable sin
    Mission ni orchestrator.

    Returns:
        :class:`RerecordFingerprintDiff` con la similitud y el flag
        ``likely_unchanged`` cuando la huella nueva es prácticamente
        idéntica a la vieja (≥ 0.92). El caller decide qué mostrar.
    """
    from app.services.missions.visual_fingerprint import (
        VisualFingerprint, compare_fingerprints,
    )

    old_fp_dict = _extract_visual_fingerprint(original_step_snapshot)
    new_fp_dict: Optional[Dict[str, Any]] = None
    for ev in (captured_evidence or []):
        cand = _extract_visual_fingerprint(ev)
        if cand is not None:
            new_fp_dict = cand
            break

    diff = RerecordFingerprintDiff()
    diff.old_available = old_fp_dict is not None
    diff.new_available = new_fp_dict is not None

    if not (diff.old_available and diff.new_available):
        diff.reason = (
            "old_missing" if not diff.old_available
            else "new_missing"
        )
        return diff

    try:
        old_fp = VisualFingerprint.from_dict(old_fp_dict)
        new_fp = VisualFingerprint.from_dict(new_fp_dict)
        sim = float(compare_fingerprints(old_fp, new_fp))
    except Exception as e:
        diff.reason = f"compare_failed:{e}"
        return diff

    diff.similarity = sim
    diff.likely_unchanged = sim >= FINGERPRINT_SIMILARITY_UNCHANGED
    diff.reason = "compared"
    return diff


def save_rerecord(
    handle: RerecordFlowHandle,
    evidence: List[Dict[str, Any]],
) -> Any:
    """Aplica la regrabación con la evidencia capturada y persiste
    audit trail. Devuelve el ``SemanticExecutionPlan`` resultante.

    Errores propios del orchestrator (estado inválido, evidencia
    vacía) se propagan como :class:`RerecordSessionError`.

    PRD 2026-05-12: antes de guardar, comparamos visual_fingerprint
    viejo vs nuevo y dejamos el diff en ``handle.last_fingerprint_diff``
    para que la UI pueda mostrar un aviso si el elemento no cambió.
    No bloqueamos el save aunque la similitud sea alta — el usuario
    sabe lo que hace; solo lo informamos.
    """
    if not handle or not handle.orchestrator:
        raise RerecordSessionError("handle inválido (orchestrator=None)")
    orch = handle.orchestrator
    if not evidence:
        raise RerecordSessionError(
            "evidence vacía: nada que guardar."
        )
    ok = orch.capture_user_event(list(evidence))
    if not ok:
        raise RerecordSessionError(
            "La captura fue rechazada por la validación de eventos."
        )

    # Comparación de huellas — best-effort, no bloquea el save.
    try:
        sess = orch.session
        if sess is not None:
            diff = compute_rerecord_fingerprint_diff(
                original_step_snapshot=sess.original_step_snapshot or {},
                captured_evidence=list(evidence),
            )
            handle.last_fingerprint_diff = diff
            if diff.likely_unchanged:
                log.warning(
                    f"[rerecord_flow] fingerprint similarity={diff.similarity:.3f} "
                    f">= {FINGERPRINT_SIMILARITY_UNCHANGED}: el elemento parece "
                    f"NO haber cambiado. Step={handle.target_step_index}"
                )
            elif diff.old_available and diff.new_available:
                log.info(
                    f"[rerecord_flow] fingerprint similarity={diff.similarity:.3f} "
                    f"(< {FINGERPRINT_SIMILARITY_UNCHANGED}): elemento cambió, ok."
                )
    except Exception as e:
        log.debug(f"[rerecord_flow] fingerprint diff failed: {e}")

    plan = orch.save()
    log.info(
        f"[rerecord_flow] save entry={handle.source_entrypoint!r} "
        f"step_idx={handle.target_step_index} ok"
    )
    return plan


def repeat_rerecord(handle: RerecordFlowHandle) -> None:
    """Descarta la captura actual y deja la sesión esperando una
    nueva captura del mismo paso. NO muta la misión.
    """
    if not handle or not handle.orchestrator:
        return
    try:
        handle.orchestrator.discard_capture()
    except RerecordSessionError as e:
        # Discard llamado en estado inválido (ej. saved). No es fatal:
        # solo lo logueamos para audit.
        log.debug(f"[rerecord_flow] repeat noop: {e}")


def cancel_rerecord(handle: RerecordFlowHandle) -> None:
    """Cancela la sesión: pide cooperative-cancel al worker, restaura
    snapshot bit-perfecto y libera recursos. Idempotente.
    """
    if not handle or not handle.orchestrator:
        return
    orch = handle.orchestrator
    try:
        orch.request_cancel()
    except Exception:
        pass
    try:
        if orch.session is not None and orch.session.state != "saved":
            orch.cancel()
    except RerecordSessionError as e:
        log.debug(f"[rerecord_flow] cancel noop: {e}")
    try:
        orch.release()
    except Exception:
        pass
    log.info(
        f"[rerecord_flow] cancel entry={handle.source_entrypoint!r} "
        f"step_idx={handle.target_step_index}"
    )


# ─────────────────────────────────────────────────────────────────────
# VIBE CODING (adapter sobre vibe_service.apply_vibe_edit)
# ─────────────────────────────────────────────────────────────────────


def start_vibe_coding_flow(
    mission: Mission,
    *,
    step_index: int,
    instruction: str,
    on_confirm_agent: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Llama a :func:`app.services.missions.vibe_service.apply_vibe_edit`.

    Single source of truth para Vibe Coding. Devuelve el dict
    estándar ``{"ok", "path", "description", "error"}``.
    """
    from app.services.missions.vibe_service import apply_vibe_edit
    return apply_vibe_edit(
        mission, step_index, instruction,
        on_confirm_agent=on_confirm_agent,
    )


def is_vibe_coding_available(mission: Mission, step_index: int) -> bool:
    """Vibe Coding está disponible si la misión tiene
    ``compiled_execution_graph`` con un step en ``step_index``.
    NO requiere LLM (los fast paths ``fast_kb`` / ``fast_type``
    funcionan sin red).
    """
    if not mission:
        return False
    graph = getattr(mission, "compiled_execution_graph", None) or []
    return 0 <= int(step_index) < len(graph)


# ─────────────────────────────────────────────────────────────────────
# ELEGIR ELEMENTO CORRECTO (adapter sobre repair.OneClickCapture)
# ─────────────────────────────────────────────────────────────────────


def choose_correct_element_flow(
    on_capture: Callable[[Optional[Dict[str, Any]]], None],
):
    """Crea y devuelve un :class:`OneClickCapture` listo para
    arrancar. La UI debe llamar ``cap.start()`` y manejar el
    on_capture en el hilo de Qt (vía ``QTimer.singleShot(0, ...)``).
    """
    from app.services.missions.repair import OneClickCapture
    return OneClickCapture(on_done=on_capture)


__all__ = [
    "ENTRY_AUTOMATION_STEPS",
    "ENTRY_MISSION_REVIEW",
    "ENTRY_REPAIR_WEAK",
    "ENTRY_REPAIR_QUEUE",
    "FINGERPRINT_SIMILARITY_UNCHANGED",
    "RerecordFingerprintDiff",
    "RerecordFlowHandle",
    "start_rerecord_flow",
    "save_rerecord",
    "repeat_rerecord",
    "cancel_rerecord",
    "start_vibe_coding_flow",
    "is_vibe_coding_available",
    "choose_correct_element_flow",
    "compute_rerecord_fingerprint_diff",
]
