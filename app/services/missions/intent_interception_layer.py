"""
Nevlan — Intent Interception Layer (PRD 2026-05-10j)
======================================================

"El click humano no ejecuta. El click humano enseña. Nevlan observa,
entiende, decide y ejecuta."

Esta capa es el **orquestador central** del modo grabación premium.
Cuando el usuario hace click:

    1. Captura ``mouse_down`` ANTES del ``mouse_up`` (intercept).
    2. Bloquea temporalmente el click real (``handoff_to_system=False``).
    3. Registra evidencia pre-click:
         - pre_action_snapshot (ring buffer)
         - target_anchor (window/title/url/proceso) → ``before``
    4. Ejecuta hit-tests (UIA, DOM si aplica, OCR, visión).
    5. Pasa los candidatos al :class:`CandidateFusionEngine`.
    6. Decide:
         - ``accept``  → continúa
         - ``confirm`` → marca needs_confirmation
         - ``ask``     → emite question; **no ejecuta** el click real.
    7. Si ``accept`` o ``confirm``: ejecuta el click real mediante
       el ``clicker`` inyectado (handoff explícito).
    8. Captura ``target_anchor`` (after) y agenda el post_action_state.
    9. Aplica :func:`enforce_target_identity_isolation` para mover
       cualquier evidencia post-action a ``debug_after_state``.
   10. Devuelve un :class:`InterceptedClick` con:
         - ``target_identity`` (lo que el usuario quiso tocar)
         - ``outcome`` (lo que cambió después)
         - ``debug_after_state`` (evidencia posterior que no identifica)

Diseño deliberado
-----------------

* **Pure-Python orquestador**, sin Win32 hooks ni Qt. Todas las
  dependencias (clicker, UIA probe, DOM probe, OCR runner, clock,
  ring buffer) se inyectan vía constructor — así los tests corren
  100% headless sin tocar pantalla real.
* **Estado de máquina explícito**: la sesión de un click tiene
  estados ``capturing → fusing → awaiting_confirmation → executing
  → completed`` o ``asking_user``. Cada transición es observable.
* **No hardcode por app**: ningún literal "Chrome", "YouTube", etc.
* **No mezcla target con outcome**: la separación es estructural.
* **Reglas de seguridad**:
    - ``decision == "ask"`` ⇒ NUNCA se llama al ``clicker``.
    - Si ``clicker is None`` ⇒ es modo "teach only" (la capa solo
      observa, no actúa).
    - Si el ``clicker`` lanza, el outcome se marca ``failed`` y la
      identidad sigue siendo válida (el target era correcto, el
      sistema falló).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.services.missions.candidate_fusion_engine import (
    CandidateFusionEngine,
    FusionResult,
)
from app.services.missions.recorder_capture_contract import (
    PostActionScheduler,
    PostActionState,
    PreActionSnapshot,
    ScreenshotRingBuffer,
    TargetAnchor,
    capture_target_anchor,
    enforce_target_identity_isolation,
)
from app.services.missions.target_identity import (
    Ambiguity,
    CONFIDENCE_ACCEPT_THRESHOLD,
    CONFIDENCE_CONFIRM_THRESHOLD,
    TargetIdentity,
)


# ──────────────────────────────────────────────────────────────────────
# Tipos públicos
# ──────────────────────────────────────────────────────────────────────


@dataclass
class InterceptedClick:
    """Resultado de interceptar y procesar un click humano.

    Campos:

      * ``intercept_id`` — id único de la intercepción.
      * ``state`` — estado final: ``"executed"``, ``"awaiting_confirmation"``,
                    ``"asked_user"``, ``"teach_only"``, ``"failed_execution"``.
      * ``target_identity`` — identidad robusta (puede ser ``None`` si state="asked_user").
      * ``outcome`` — :class:`PostActionState` o ``None``.
      * ``debug_after_state`` — evidencia migrada post-action (no identifica).
      * ``fusion`` — :class:`FusionResult` completo con audit trail.
      * ``handoff_to_system`` — ``True`` si Nevlan ejecutó el click real.
      * ``ask_question`` — :class:`AskQuestion` si state="asked_user".
      * ``pre_snapshot`` — :class:`PreActionSnapshot` capturado.
      * ``before_anchor`` / ``after_anchor`` — anchors del estado.
      * ``error`` — string si state="failed_execution".
    """

    intercept_id: str = ""
    state: str = "teach_only"
    target_identity: Optional[TargetIdentity] = None
    outcome: Optional[PostActionState] = None
    debug_after_state: Dict[str, Any] = field(default_factory=dict)
    fusion: Optional[FusionResult] = None
    handoff_to_system: bool = False
    ask_question: Optional["AskQuestion"] = None
    pre_snapshot: Optional[PreActionSnapshot] = None
    before_anchor: Optional[TargetAnchor] = None
    after_anchor: Optional[TargetAnchor] = None
    captured_at_ms: int = 0
    executed_at_ms: int = 0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intercept_id": str(self.intercept_id),
            "state": str(self.state),
            "target_identity": (
                self.target_identity.to_dict()
                if self.target_identity is not None else None
            ),
            "outcome": (
                self.outcome.to_metadata()
                if self.outcome is not None else None
            ),
            "debug_after_state": dict(self.debug_after_state),
            "fusion": (
                self.fusion.to_dict() if self.fusion is not None else None
            ),
            "handoff_to_system": bool(self.handoff_to_system),
            "ask_question": (
                self.ask_question.to_dict()
                if self.ask_question is not None else None
            ),
            "pre_snapshot": (
                self.pre_snapshot.to_metadata()
                if self.pre_snapshot is not None else None
            ),
            "before_anchor": (
                self.before_anchor.to_dict()
                if self.before_anchor is not None else None
            ),
            "after_anchor": (
                self.after_anchor.to_dict()
                if self.after_anchor is not None else None
            ),
            "captured_at_ms": int(self.captured_at_ms),
            "executed_at_ms": int(self.executed_at_ms),
            "error": str(self.error or ""),
        }


@dataclass
class AskQuestion:
    """Pregunta clara al usuario cuando la confianza es baja.

    No es Qt — es solo el contrato. La UI traduce ``code`` a copy
    localizable.
    """

    code: str = ""
    description: str = ""
    options: List[Dict[str, Any]] = field(default_factory=list)
    ambiguity: Optional[Ambiguity] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": str(self.code or ""),
            "description": str(self.description or ""),
            "options": list(self.options or []),
            "ambiguity": (
                self.ambiguity.to_dict() if self.ambiguity is not None else None
            ),
        }


# ──────────────────────────────────────────────────────────────────────
# Inyecciones / callbacks
# ──────────────────────────────────────────────────────────────────────

# Hit-tests: cada uno recibe (x, y) y devuelve dict | None.
UiaHitTestFn = Callable[[int, int], Optional[Dict[str, Any]]]
DomHitTestFn = Callable[[int, int], Optional[Dict[str, Any]]]
OcrRunnerFn = Callable[
    [Optional[str], Tuple[int, int]], Optional[Dict[str, Any]]
]
VisualProbeFn = Callable[
    [Optional[str], Tuple[int, int]], Optional[Dict[str, Any]]
]
WindowCtxFn = Callable[[], Dict[str, Any]]
ParentChainFn = Callable[[int, int], Optional[Dict[str, Any]]]

# Ejecuta el click real. Recibe (x, y). Lanza si falla.
ClickerFn = Callable[[int, int], None]

# Captura estado para outcome (window/url/etc).
StateProbeFn = Callable[[], Dict[str, Any]]

# Captura screenshot tras el click (best-effort, puede devolver None).
ScreenshotFn = Callable[[], Optional[str]]

# Reloj inyectable.
ClockFn = Callable[[], int]


def _wall_ms() -> int:
    return int(time.time() * 1000)


# ──────────────────────────────────────────────────────────────────────
# Sesión de un click (estado entre mouse_down y mouse_up)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _PendingIntercept:
    """Estado entre ``mouse_down`` y ``mouse_up`` de un mismo click."""

    intercept_id: str
    x: int
    y: int
    down_ms: int
    window_ctx: Dict[str, Any]
    before_anchor: TargetAnchor
    pre_snapshot: Optional[PreActionSnapshot]
    handed_off: bool = False


# ──────────────────────────────────────────────────────────────────────
# La capa
# ──────────────────────────────────────────────────────────────────────


class IntentInterceptionLayer:
    """Capa transparente que intercepta clicks humanos durante grabación.

    Reglas de máquina de estados (tres llamadas canónicas):

      1. ``on_mouse_down(x, y)`` →
            * captura pre_snapshot, anchor before, window_ctx,
            * crea ``_PendingIntercept``,
            * NO ejecuta el click.

      2. ``on_mouse_up(x, y)`` →
            * corre hit-tests sobre (x, y),
            * pasa candidatos al fusion engine,
            * según decision:
                - ``accept``  → llama ``clicker(x, y)`` y observa outcome.
                - ``confirm`` → llama ``clicker(x, y)`` pero marca
                                 ``needs_confirmation`` para el reviewer.
                - ``ask``     → NO llama ``clicker``; devuelve
                                 ``state="asked_user"`` con ``ask_question``.
            * captura anchor after y outcome,
            * aplica :func:`enforce_target_identity_isolation`,
            * devuelve :class:`InterceptedClick`.

      3. ``cancel()`` → descarta el pending si existe (Esc del usuario).

    Si se llama ``on_mouse_up`` sin ``on_mouse_down`` previo (clicks
    rápidos sintéticos en tests / casos degenerados), la capa hace
    ambos pasos atómicamente.
    """

    def __init__(
        self,
        *,
        clicker: Optional[ClickerFn] = None,
        uia_probe: Optional[UiaHitTestFn] = None,
        dom_probe: Optional[DomHitTestFn] = None,
        ocr_runner: Optional[OcrRunnerFn] = None,
        visual_probe: Optional[VisualProbeFn] = None,
        parent_chain_probe: Optional[ParentChainFn] = None,
        window_ctx_probe: Optional[WindowCtxFn] = None,
        state_probe: Optional[StateProbeFn] = None,
        screenshot_probe: Optional[ScreenshotFn] = None,
        pre_buffer: Optional[ScreenshotRingBuffer] = None,
        post_scheduler: Optional[PostActionScheduler] = None,
        fusion: Optional[CandidateFusionEngine] = None,
        clock_ms: Optional[ClockFn] = None,
    ) -> None:
        self.clicker = clicker
        self.uia_probe = uia_probe
        self.dom_probe = dom_probe
        self.ocr_runner = ocr_runner
        self.visual_probe = visual_probe
        self.parent_chain_probe = parent_chain_probe
        self.window_ctx_probe = window_ctx_probe or (lambda: {})
        self.state_probe = state_probe or self.window_ctx_probe
        self.screenshot_probe = screenshot_probe
        self.pre_buffer = pre_buffer
        self.post_scheduler = post_scheduler
        self.fusion = fusion or CandidateFusionEngine()
        self.clock_ms = clock_ms or _wall_ms

        self._pending: Optional[_PendingIntercept] = None
        self._lock = threading.Lock()

    # ── API pública ──────────────────────────────────────────────
    def on_mouse_down(self, x: int, y: int) -> None:
        """Intercept temprano. NO ejecuta el click.

        Captura toda la evidencia que solo existe ANTES de que el
        click cambie la pantalla:
          - pre_snapshot del ring buffer,
          - window_ctx actual,
          - target_anchor (before).
        """
        now = int(self.clock_ms())
        win_ctx = self._safe_call(self.window_ctx_probe, default={}) or {}
        before = capture_target_anchor(win_ctx, clock_ms=lambda: now)
        pre_snap = None
        if self.pre_buffer is not None:
            try:
                pre_snap = self.pre_buffer.get_before(now)
            except Exception:
                pre_snap = None
        with self._lock:
            self._pending = _PendingIntercept(
                intercept_id=uuid.uuid4().hex,
                x=int(x), y=int(y), down_ms=now,
                window_ctx=dict(win_ctx),
                before_anchor=before,
                pre_snapshot=pre_snap,
            )

    def on_mouse_up(self, x: int, y: int) -> InterceptedClick:
        """Procesa el click completo.

        Si no hubo ``on_mouse_down`` previo, ejecuta el flujo
        atómico (down+up). Devuelve un :class:`InterceptedClick`
        con la decisión de Nevlan.
        """
        with self._lock:
            pending = self._pending
            self._pending = None
        if pending is None:
            self.on_mouse_down(int(x), int(y))
            with self._lock:
                pending = self._pending
                self._pending = None
            if pending is None:
                # No debería pasar; degeneramos con anchor mínimo.
                pending = _PendingIntercept(
                    intercept_id=uuid.uuid4().hex,
                    x=int(x), y=int(y),
                    down_ms=int(self.clock_ms()),
                    window_ctx={},
                    before_anchor=TargetAnchor(),
                    pre_snapshot=None,
                )
        return self._process(pending, up_xy=(int(x), int(y)))

    def cancel(self) -> None:
        """Descarta cualquier ``_PendingIntercept`` activo."""
        with self._lock:
            self._pending = None

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    # ── Orquestación interna ─────────────────────────────────────
    def _process(
        self,
        pending: _PendingIntercept,
        *,
        up_xy: Tuple[int, int],
    ) -> InterceptedClick:
        # Tomamos las coords del mouse_up como las canónicas (drag
        # ínfimo dentro de bbox tolerable).
        x, y = up_xy
        intercept_id = pending.intercept_id

        # 1. Hit-tests.
        uia = self._safe_call(self.uia_probe, x, y)
        dom = self._safe_call(self.dom_probe, x, y)
        parent_chain = self._safe_call(self.parent_chain_probe, x, y)
        pre_path = (
            pending.pre_snapshot.full_path if pending.pre_snapshot else None
        )
        ocr = self._safe_call(self.ocr_runner, pre_path, (x, y))
        visual = self._safe_call(self.visual_probe, pre_path, (x, y))

        # 2. Fusion.
        capture_ms = int(self.clock_ms())
        fusion_result = self.fusion.fuse(
            click_xy=(x, y),
            click_ms=int(pending.down_ms),
            window_ctx=pending.window_ctx,
            uia=uia,
            dom=dom,
            ocr=ocr,
            visual=visual,
            parent_chain=parent_chain,
            outcome_confirmed=False,
            capture_ms=capture_ms,
        )

        intercepted = InterceptedClick(
            intercept_id=intercept_id,
            target_identity=fusion_result.chosen,
            fusion=fusion_result,
            pre_snapshot=pending.pre_snapshot,
            before_anchor=pending.before_anchor,
            captured_at_ms=capture_ms,
        )

        # 3. Decisión: solo en accept/confirm hacemos handoff.
        if fusion_result.decision == "ask":
            intercepted.state = "asked_user"
            intercepted.ask_question = AskQuestion(
                code=(
                    fusion_result.ambiguity.code
                    if fusion_result.ambiguity else "low_confidence"
                ),
                description=(
                    fusion_result.ambiguity.description
                    if fusion_result.ambiguity
                    else "Necesito una pista de qué tocaste."
                ),
                ambiguity=fusion_result.ambiguity,
            )
            intercepted.handoff_to_system = False
            return intercepted

        if self.clicker is None:
            # Modo teach-only: identidad lista pero no ejecutamos.
            intercepted.state = "teach_only"
            intercepted.handoff_to_system = False
            return intercepted

        # 4. Ejecución del click real.
        try:
            self.clicker(x, y)
            intercepted.handoff_to_system = True
            intercepted.executed_at_ms = int(self.clock_ms())
        except Exception as e:
            intercepted.state = "failed_execution"
            intercepted.error = f"{type(e).__name__}: {e}"
            intercepted.handoff_to_system = False
            return intercepted

        # 5. Outcome: anchor after + post_action_state.
        after_state = self._safe_call(self.state_probe, default={}) or {}
        after_anchor = capture_target_anchor(
            after_state, clock_ms=lambda: int(self.clock_ms())
        )
        intercepted.after_anchor = after_anchor

        post: Optional[PostActionState] = None
        if self.post_scheduler is not None:
            # Programación asíncrona; en tests / runtime ligero
            # usamos un eject sincrónico simple:
            collected: List[PostActionState] = []
            self.post_scheduler.schedule(
                event_id=intercept_id,
                click_ts_ms=int(intercepted.executed_at_ms),
                before_state=dict(pending.window_ctx),
                capture_state=lambda: self._safe_call(
                    self.state_probe, default={}) or {},
                capture_screenshot=(
                    self.screenshot_probe
                    if self.screenshot_probe is not None else None
                ),
                on_complete=lambda p: collected.append(p),
            )
            self.post_scheduler.wait_all(timeout_ms=3000)
            if collected:
                post = collected[-1]
        else:
            # Sin scheduler: outcome sincronico minimal.
            post = PostActionState(
                captured_at_ms=int(self.clock_ms()),
                latency_ms=max(
                    0, int(self.clock_ms()) - int(intercepted.executed_at_ms)
                ),
                window_hwnd=after_state.get("hwnd"),
                window_title=str(after_state.get("title") or ""),
                window_process=str(after_state.get("process_name") or ""),
                url=str(after_state.get("url") or ""),
                snapshot_full=None,
                detected_change=(
                    pending.before_anchor.hwnd != after_anchor.hwnd
                    or pending.before_anchor.title != after_anchor.title
                    or pending.before_anchor.url != after_anchor.url
                ),
            )
        intercepted.outcome = post

        # 6. Aplicar enforce_target_identity_isolation sobre un dict
        # virtual de metadata para detectar si el UIA/web capturado
        # cae en estado post-action. Eso protege contra "outcome se
        # convirtió en target".
        meta: Dict[str, Any] = {}
        if uia:
            meta["uia"] = dict(uia)
        if dom:
            meta["web"] = dict(dom)
        meta = enforce_target_identity_isolation(
            meta,
            before=pending.before_anchor,
            after=after_anchor,
            click_xy=(int(x), int(y)),
        )
        intercepted.debug_after_state = dict(
            meta.get("debug_after_state") or {}
        )

        # Si la isolation migró el UIA al after, debemos invalidar el
        # target_identity construido sobre ese UIA — no podemos
        # afirmar identidad si la evidencia que la sostiene era
        # posterior al click.
        iso_audit = meta.get("target_identity_isolation") or {}
        preserved = iso_audit.get("identity_preserved_after_transition") is True
        moved = list(
            iso_audit.get(
                "moved_to_debug_after_state"
            ) or []
        )
        if (
            moved
            and not preserved
            and fusion_result.evidence_sources
            and any(s.startswith("uia.") for s in fusion_result.evidence_sources)
        ):
            # Reducimos confianza: el UIA en el que nos apoyábamos era
            # del estado posterior. Si NO había otra fuente fuerte
            # (DOM/OCR), bajamos a confirm/ask.
            other = [
                s for s in fusion_result.evidence_sources
                if not s.startswith("uia.")
                and s not in ("coords.absolute", "coords.relative")
            ]
            if not other:
                # Sin otra evidencia: la identidad queda débil.
                if intercepted.target_identity is not None:
                    new_conf = min(
                        intercepted.target_identity.confidence,
                        CONFIDENCE_CONFIRM_THRESHOLD - 0.01,
                    )
                    intercepted.target_identity.confidence = new_conf
                    intercepted.target_identity.ambiguity = Ambiguity(
                        code="uia_moved_to_after_state",
                        description=(
                            "El UIA capturado pertenece al estado "
                            "posterior; no puede usarse como identidad."
                        ),
                    )

        intercepted.state = (
            "executed"
            if fusion_result.decision == "accept"
            else "awaiting_confirmation"
        )
        return intercepted

    # ── Utilidades ───────────────────────────────────────────────
    def _safe_call(self, fn: Optional[Callable], *args, default: Any = None) -> Any:
        if fn is None:
            return default
        try:
            return fn(*args)
        except Exception:
            return default


__all__ = [
    "AskQuestion",
    "IntentInterceptionLayer",
    "InterceptedClick",
]
