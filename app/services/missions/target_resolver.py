"""
ArthurOS Services - TargetResolver (semantic, scored, self-healing)
-------------------------------------------------------------------
Resolución unificada de targets con scoring y *context gate*.

Orden de estrategias (siempre): contexto correcto → web → UIA exacto →
UIA semántico → visual/OCR/icono → coords relativas a ventana → coords
absolutas (último recurso). NUNCA hace pantalla completa salvo modo
reparación. NUNCA toca ventanas/procesos de Nevlan.

Para cada estrategia tentada, devolvemos un ``TargetResolutionResult``
con el método usado, el score (0-100), el target/coords resueltos y una
explicación legible para la UI.

Este resolver es complementario al pipeline ya implementado en
``MissionPlayer``. Se ofrece como motor "moderno" para el modo
reparación y para futuros pasos del runtime que prefieran scoring
explícito.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
import os

from app.contracts.mission import CompiledStep
from app.core.logger import log


try:
    import uiautomation as auto  # type: ignore
    HAS_UIA = True
except ImportError:
    HAS_UIA = False


_OWN_PID = os.getpid()


# ──────────────────────────────────────────────────────────────────────
# Resultado público
# ──────────────────────────────────────────────────────────────────────

@dataclass
class TargetResolutionResult:
    """Resultado canónico del TargetResolverV2.

    ``target_kind`` clasifica el HOW para que el Player V3 ejecute con la
    API más rápida posible:

      - ``web_locator``    → ``target`` es un ``playwright.Locator`` listo
                             para ``click()/fill()/dblclick()``. NO mover
                             mouse, NO usar coords.
      - ``uia_control``    → ``target`` es un control UIA. Preferir
                             ``Invoke()`` o ``GetClickablePoint()`` antes
                             que ``coords``.
      - ``visual_point``   → ``coords`` provienen de visión/template-match;
                             el Player las usará con pyautogui.
      - ``relative_point`` → ``coords`` reconstruidas desde fx,fy de la
                             ventana actual.
      - ``absolute_point`` → ``coords`` absolutas grabadas (último recurso,
                             solo si el resto falló y el riesgo es bajo).
      - ``none``           → ninguna estrategia produjo resultado; el
                             player debe abrir modo reparación.
    """

    method: str               # "web_locator" | "uia_exact" | "uia_semantic"
                              # "visual_anchor" | "rel_window" | "coords_absolute"
                              # "context_gate_failed" | "none"
    score: int                # 0..100 — confianza
    target: Any = None        # UIA control / Playwright Locator / None
    coords: Optional[Dict[str, int]] = None
    explanation: str = ""
    candidates_tried: List[Dict[str, Any]] = field(default_factory=list)
    # ── Phase 5: target_kind, bundle_used, candidate_rank ─────────
    target_kind: str = "none"        # web_locator | uia_control | visual_point
                                     # | relative_point | absolute_point | none
    bundle_used: str = "primary"     # "primary" o "alternate:N"
    candidate_rank: int = 0          # ranking entre todos los candidatos probados
    # ── PRD 2026-05-12: safety / confirmation flags ───────────────
    needs_confirmation: bool = False  # match medio → pedir confirmación
    safety_reason: str = ""           # explica por qué se downgradeó

    def is_high_confidence(self) -> bool:
        return self.score >= 65 and self.method != "none"


# Mapping interno método → target_kind canónico.
_METHOD_KIND = {
    "web_locator": "web_locator",
    "uia_exact": "uia_control",
    "uia_semantic": "uia_control",
    "visual_fingerprint": "visual_point",  # PRD 2026-05-12
    "visual_anchor": "visual_point",
    "rel_window": "relative_point",
    "coords_absolute": "absolute_point",
    "none": "none",
}


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _is_own_pid_uia(ctrl) -> bool:
    try:
        pid = int(getattr(ctrl, "ProcessId", 0) or 0)
        return pid == _OWN_PID
    except Exception:
        return False


def _success_score_for(step: CompiledStep, method: str) -> int:
    """Devuelve el porcentaje histórico de éxito del método (0-100).
    Si no hay historia, devuelve 0."""
    try:
        stats = ((step.target_context.confidence or {}).get("success_by_method") or {})
        s = stats.get(method) or {}
        ok = int(s.get("ok", 0))
        fail = int(s.get("fail", 0))
        total = ok + fail
        if total == 0:
            return 0
        return int(round(100.0 * ok / total))
    except Exception:
        return 0


def _bonus_history(step: CompiledStep, method: str) -> int:
    """Pequeño boost para métodos que tienen historial favorable."""
    s = _success_score_for(step, method)
    if s >= 80:
        return 8
    if s >= 60:
        return 4
    return 0


def _has_fingerprint_and_attempted(step: CompiledStep) -> bool:
    """True si el step tenía visual_fingerprint disponible y la
    estrategia de relocate corrió (haya o no encontrado el target).

    Lo usamos en la safety gate para distinguir:
      * "no hay fingerprint" → coords son aceptables (modo emergencia)
      * "hay fingerprint pero falló" → coords NO son aceptables
    """
    try:
        tc = step.target_context
        if tc is None:
            return False
        vd = tc.vision_data or {}
        if not isinstance(vd, dict):
            return False
        fp = vd.get("visual_fingerprint")
        if isinstance(fp, dict) and fp.get("available"):
            return True
        return False
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────
# Resolver principal
# ──────────────────────────────────────────────────────────────────────

class TargetResolver:
    """Stateless: una instancia por ejecución, llamadas por paso."""

    def __init__(
        self,
        *,
        uia_probe_factory: Optional[Callable[[], Any]] = None,
        dom_probe_factory: Optional[Callable[[], Any]] = None,
    ):
        # Cache rápido (ventana resuelta, página web actual).
        self._win_ctrl = None
        self._win_bbox: Optional[Dict[str, int]] = None
        self._page = None
        # Inyectables para tests del coord_verifier zero-capture.
        # En prod usamos los probes reales del recorder por defecto.
        self._uia_probe_factory = uia_probe_factory
        self._dom_probe_factory = dom_probe_factory

    # ── Coord verifier (zero-capture nivel 1) ────────────────────
    def _build_coord_verifier(self, tc) -> Optional[Callable[[int, int], bool]]:
        """Construye un coord_verifier ``(x, y) -> bool`` que devuelve
        True solo si UIA y/o DOM confirman estructuralmente el target
        con confidence ≥ 0.85.

        Si no hay evidencia estructural (UIA ni web data), devolvemos
        None y la pipeline visual baja a nivel 2 sin más.
        """
        try:
            from app.services.missions.coord_verifier import (
                make_bool_coord_verifier,
            )
        except Exception as e:
            log.debug(f"_build_coord_verifier: import failed: {e}")
            return None
        expected_uia = None
        expected_web = None
        if tc is not None:
            ud = tc.uia_data
            if isinstance(ud, dict) and ud:
                expected_uia = dict(ud)
            wd = tc.web_data
            if isinstance(wd, dict) and wd:
                expected_web = dict(wd)
        if not expected_uia and not expected_web:
            return None
        # Probes reales por defecto; tests pueden inyectarlos.
        uia_probe = None
        dom_probe = None
        if self._uia_probe_factory is not None:
            try:
                uia_probe = self._uia_probe_factory()
            except Exception:
                uia_probe = None
        else:
            try:
                from app.services.missions.recorder_intent_integration import (
                    _make_uia_probe,
                )
                uia_probe = _make_uia_probe()
            except Exception:
                uia_probe = None
        if self._dom_probe_factory is not None:
            try:
                dom_probe = self._dom_probe_factory()
            except Exception:
                dom_probe = None
        else:
            try:
                from app.services.missions.dom_probe import DOMProbe
                dom_probe = DOMProbe().element_from_point
            except Exception:
                dom_probe = None
        if uia_probe is None and dom_probe is None:
            return None
        return make_bool_coord_verifier(
            expected_uia=expected_uia,
            expected_web=expected_web,
            uia_probe=uia_probe,
            dom_probe=dom_probe,
        )

    # ── Context gate ─────────────────────────────────────────────
    def _check_context_gate(self, step: CompiledStep) -> Tuple[bool, str]:
        """Verifica que estamos en la app/url correcta. False ⇒ pasamos
        al resolver, que hará lo que pueda; True ⇒ contexto OK."""
        tc = step.target_context
        proc = (tc.process_name or "").lower()
        title = (tc.window_title or "").strip()
        web = tc.web_data or {}
        url = (web.get("url") or "").strip()

        if proc and proc.startswith(("chrome", "msedge", "firefox", "brave")):
            # Web: comparamos dominio si lo tenemos.
            if url:
                try:
                    from app.skills.tools.web import _get_page  # type: ignore
                    page = _get_page()
                    self._page = page
                    cur = page.url or ""
                    if cur and url:
                        # Mismo dominio ya es señal suficiente.
                        from urllib.parse import urlparse
                        d_old = urlparse(url).netloc
                        d_cur = urlparse(cur).netloc
                        if d_old and d_cur and d_old != d_cur:
                            return False, f"dominio actual='{d_cur}' ≠ esperado='{d_old}'"
                except Exception:
                    pass
            return True, "web context"

        if title:
            try:
                import pygetwindow as gw  # type: ignore
                wins = gw.getWindowsWithTitle(title)
                if not wins:
                    head = title.split(" - ")[0].strip()
                    if head and head != title:
                        wins = gw.getWindowsWithTitle(head)
                if not wins:
                    return False, f"no encontré ventana '{title}'"
                # Activamos la primera no minimizada.
                for w in wins:
                    try:
                        if not w.isMinimized:
                            w.activate()
                            return True, f"ventana '{title}' activa"
                    except Exception:
                        continue
            except Exception:
                pass
        return True, "context gate skipped"

    # ── Web locator strategy ─────────────────────────────────────
    def _try_web_locator(self, step: CompiledStep) -> Optional[TargetResolutionResult]:
        web = step.target_context.web_data or {}
        if not web.get("locators"):
            return None
        try:
            if self._page is None:
                from app.skills.tools.web import _get_page  # type: ignore
                self._page = _get_page()
            page = self._page
        except Exception as e:
            log.debug(f"resolver web: no hay página: {e}")
            return None

        tried: List[Dict[str, Any]] = []
        for spec in web["locators"]:
            kind = spec.get("kind")
            tried.append(dict(spec))
            try:
                from app.services.missions.web_recorder import _build_locator
                loc = _build_locator(page, spec)
                if loc is None:
                    continue
                n = loc.count()
                if n == 0:
                    continue
                first = loc.first
                # Score base por tipo (estabilidad relativa).
                base = {
                    "test_id": 95,
                    "role": 90 if spec.get("name") else 75,
                    "label": 88,
                    "placeholder": 82,
                    "text": 70,
                    "css": 60,
                    "xpath": 55,
                }.get(kind, 50)
                if n > 1:
                    base -= min(20, (n - 1) * 4)  # ambigüedad penaliza
                base += _bonus_history(step, "web_locator")
                base = max(0, min(100, base))
                # Visibilidad
                try:
                    if not first.is_visible(timeout=400):
                        continue
                except Exception:
                    pass
                bbox = None
                try:
                    box = first.bounding_box()
                    if box:
                        bbox = {
                            "x": int(box["x"] + box["width"] / 2),
                            "y": int(box["y"] + box["height"] / 2),
                        }
                except Exception:
                    pass
                return TargetResolutionResult(
                    method="web_locator",
                    target_kind="web_locator",
                    score=base,
                    target=first,
                    coords=bbox,
                    explanation=f"locator {kind} → {n} match(es)",
                    candidates_tried=tried,
                )
            except Exception as e:
                log.debug(f"web locator {kind} falló: {e}")
                continue
        return None

    # ── UIA exact/semantic ───────────────────────────────────────
    def _try_uia(self, step: CompiledStep) -> Optional[TargetResolutionResult]:
        if not HAS_UIA:
            return None
        uia_meta = step.target_context.uia_data or {}
        if not uia_meta:
            return None
        aid = (uia_meta.get("automation_id") or "").strip()
        name = (uia_meta.get("name") or "").strip()
        ct = (uia_meta.get("control_type") or "").strip()
        cls = (uia_meta.get("class_name") or "").strip()

        # Filtrar nombres genéricos (Name == ControlType).
        generic = {"groupcontrol", "panecontrol", "customcontrol", "documentcontrol",
                   "windowcontrol", "toolbarcontrol", "statusbarcontrol", "windowunknown",
                   "group", "pane", "custom", "document", "window", "toolbar", "statusbar"}
        if name and name.strip().lower() in generic:
            name = ""
        if name and ct and name.strip().lower() == ct.strip().lower():
            name = ""

        if not aid and not name:
            return None

        kwargs = {}
        if aid:
            kwargs["AutomationId"] = aid
        if name:
            kwargs["Name"] = name
        if cls:
            kwargs["ClassName"] = cls

        # Buscar dentro de la ventana (priorizando título).
        win = self._get_or_find_window(step)
        ctrl = None
        try:
            if win is not None:
                ctrl = win.Control(searchDepth=16, **kwargs)
                if not ctrl.Exists(0, 0.2):
                    ctrl = None
                elif _is_own_pid_uia(ctrl):
                    ctrl = None
        except Exception as e:
            log.debug(f"uia scoped: {e}")

        if ctrl is None:
            try:
                ctrl = auto.Control(searchDepth=16, **kwargs)
                if not ctrl.Exists(0, 0.2):
                    ctrl = None
                elif _is_own_pid_uia(ctrl):
                    ctrl = None
            except Exception as e:
                log.debug(f"uia global: {e}")
                ctrl = None

        if ctrl is None:
            return None

        # Decidir método y score.
        method = "uia_exact" if aid else "uia_semantic"
        base = 92 if aid else 78
        if name and not aid:
            base = 80
        # Verificación geométrica: si tenemos anchor_bbox, comprobamos
        # que el control resuelto está cerca.
        anchor = step.target_context.anchor_bbox or {}
        if anchor:
            try:
                r = ctrl.BoundingRectangle
                cx = (int(r.left) + int(r.right)) // 2
                cy = (int(r.top) + int(r.bottom)) // 2
                ax = int(anchor.get("left", 0)) + int(anchor.get("width", 0)) // 2
                ay = int(anchor.get("top", 0)) + int(anchor.get("height", 0)) // 2
                max_dim = max(int(anchor.get("width", 0)),
                              int(anchor.get("height", 0)), 50)
                tol = int(max_dim * 1.5)
                if abs(cx - ax) > tol or abs(cy - ay) > tol:
                    base -= 25  # ambiguous match
            except Exception:
                pass
        base += _bonus_history(step, method)
        base = max(0, min(100, base))

        coords = None
        try:
            r = ctrl.BoundingRectangle
            coords = {"x": (int(r.left) + int(r.right)) // 2,
                      "y": (int(r.top) + int(r.bottom)) // 2}
        except Exception:
            pass

        return TargetResolutionResult(
            method=method, target_kind="uia_control",
            score=base, target=ctrl, coords=coords,
            explanation=f"UIA {ct} '{name or aid}'",
        )

    def _get_or_find_window(self, step: CompiledStep):
        if not HAS_UIA:
            return None
        if self._win_ctrl is not None:
            return self._win_ctrl
        title = (step.target_context.window_title
                 or ((step.target_context.uia_data or {}).get("window") or {}).get("title")
                 or "")
        if not title:
            try:
                self._win_ctrl = auto.GetForegroundControl()
                return self._win_ctrl
            except Exception:
                return None
        try:
            w = auto.WindowControl(searchDepth=1, SubName=title)
            if w.Exists(0, 0.2):
                self._win_ctrl = w
                try:
                    r = w.BoundingRectangle
                    self._win_bbox = {
                        "left": int(r.left), "top": int(r.top),
                        "width": int(r.right - r.left),
                        "height": int(r.bottom - r.top),
                    }
                except Exception:
                    pass
                return w
        except Exception:
            pass
        return None

    # ── Visual fingerprint (PRD 2026-05-12) ──────────────────────
    def _try_visual_fingerprint(
        self, step: CompiledStep,
    ) -> Optional[TargetResolutionResult]:
        """Re-localiza el target usando el visual fingerprint determinista
        grabado al momento del click (pHash + dHash + ORB + template).

        Pipeline progresivo (mínimo costo de capturas):
            1. coord grabada (sin captura)
            2. recorte LOCAL 200×200 alrededor de la coord
            3. recorte de VENTANA del proceso
            4. captura FULL-SCREEN
            5. si nada matchea ≥ score medio → None y dejamos que
               otras estrategias intenten.

        Devuelve ``None`` (no este método) si:
          * No hay fingerprint en ``target_context.vision_data``,
          * OpenCV no está disponible,
          * El score quedó debajo del umbral medio.
        """
        tc = step.target_context
        if tc is None:
            return None
        vision = tc.vision_data or {}
        fingerprint = vision.get("visual_fingerprint") \
            if isinstance(vision, dict) else None
        if not isinstance(fingerprint, dict) or not fingerprint.get("available"):
            return None
        # Coord grabada: priorizamos absolute si está, si no rel_window
        # proyectado contra la ventana actual.
        recorded_xy: Optional[Tuple[int, int]] = None
        ab = tc.fallback_coords or {}
        if isinstance(ab, dict) and "x" in ab and "y" in ab:
            try:
                recorded_xy = (int(ab["x"]), int(ab["y"]))
            except Exception:
                pass
        if recorded_xy is None:
            rel = tc.fallback_coords_relative_to_window or {}
            bbox = self._win_bbox
            if isinstance(rel, dict) and bbox:
                try:
                    fx = max(0.0, min(1.0, float(rel.get("fx", 0.0))))
                    fy = max(0.0, min(1.0, float(rel.get("fy", 0.0))))
                    recorded_xy = (
                        int(bbox["left"]) + int(bbox["width"] * fx),
                        int(bbox["top"]) + int(bbox["height"] * fy),
                    )
                except Exception:
                    pass
        try:
            from app.services.missions.execution_vision_pipeline import (
                locate_with_progressive_vision,
            )
        except Exception as e:
            log.debug(f"_try_visual_fingerprint: import failed: {e}")
            return None

        # PRD nivel-1 zero-capture: construimos un coord_verifier real
        # con UIA/DOM probes si el step tiene evidencia estructural.
        # Si UIA/DOM confirman la coord, la pipeline visual NO toma
        # ninguna foto (cero CPU de visión, cero captura).
        coord_verifier_fn = self._build_coord_verifier(tc)
        try:
            out = locate_with_progressive_vision(
                fingerprint=fingerprint,
                recorded_xy=recorded_xy,
                window_bbox=self._win_bbox,
                coord_verifier=coord_verifier_fn,
            )
        except Exception as e:
            log.debug(f"_try_visual_fingerprint: lookup failed: {e}")
            return None
        if not out.found:
            # No found: dejamos rastro para la safety gate de resolve().
            # Devolvemos None — sin candidato — pero anotamos en el step.
            try:
                step.target_context.vision_data = dict(
                    step.target_context.vision_data or {}
                )
                step.target_context.vision_data["_visual_fingerprint_attempt"] = {
                    "found": False,
                    "level": out.level,
                    "captures": int(out.captures_taken),
                    "reason": out.reason or "no_match",
                }
            except Exception:
                pass
            return None
        # Score 0-1 → 0-100. Estrategia "visual_fingerprint" se mapea a
        # target_kind="visual_point" — el ActionRunner ya sabe procesar.
        base_score = int(round(float(out.score) * 100))
        base_score += _bonus_history(step, "visual_fingerprint")
        base_score = max(0, min(100, base_score))
        # Match medio (decision="confirm") → pedimos confirmación al humano.
        needs_confirm = (out.decision == "confirm")
        return TargetResolutionResult(
            method="visual_fingerprint",
            target_kind="visual_point",
            score=base_score,
            coords={"x": int(out.x), "y": int(out.y)},
            explanation=(
                f"visual_fingerprint resolvió en nivel={out.level} "
                f"score={out.score:.2f} captures={out.captures_taken}"
            ),
            needs_confirmation=needs_confirm,
            safety_reason=(
                "visual_fingerprint_medium_confidence" if needs_confirm else ""
            ),
        )

    # ── Visual anchor / OCR / icon ──────────────────────────────
    def _try_visual(self, step: CompiledStep) -> Optional[TargetResolutionResult]:
        tc = step.target_context
        anchor = tc.anchor_bbox or {}
        # Sólo aceptamos visión SI tenemos anchor_bbox + snapshot.
        if not (anchor and (tc.image_ref_mid or tc.image_ref)):
            return None
        try:
            import pyautogui  # type: ignore
        except Exception:
            return None
        bbox = self._win_bbox or {}
        anchor_cx = int(anchor["left"]) + int(anchor["width"]) // 2
        anchor_cy = int(anchor["top"]) + int(anchor["height"]) // 2
        pad_x = max(50, int(anchor["width"]))
        pad_y = max(50, int(anchor["height"]))
        region = (
            int(anchor["left"]) - pad_x,
            int(anchor["top"]) - pad_y,
            int(anchor["width"]) + pad_x * 2,
            int(anchor["height"]) + pad_y * 2,
        )
        for img, conf in [(tc.image_ref_mid, 0.9), (tc.image_ref, 0.88)]:
            if not img:
                continue
            try:
                box = pyautogui.locateOnScreen(img, confidence=conf, region=region)
            except Exception:
                box = None
            if box is None:
                continue
            try:
                cx = int(box.left) + int(box.width) // 2
                cy = int(box.top) + int(box.height) // 2
            except Exception:
                continue
            drift = max(abs(cx - anchor_cx), abs(cy - anchor_cy))
            base = 85 if conf >= 0.9 else 78
            base -= min(20, drift // 20)
            base += _bonus_history(step, "visual_anchor")
            base = max(0, min(100, base))
            return TargetResolutionResult(
                method="visual_anchor",
                target_kind="visual_point",
                score=base,
                coords={"x": cx, "y": cy},
                explanation=f"visión cerca del anchor (drift={drift}px)",
            )
        return None

    # ── Coords relativas a ventana ───────────────────────────────
    def _try_rel_window(self, step: CompiledStep) -> Optional[TargetResolutionResult]:
        rel = step.target_context.fallback_coords_relative_to_window
        if not rel:
            return None
        bbox = self._win_bbox
        if not bbox:
            # Intentar obtener bbox aquí mismo.
            self._get_or_find_window(step)
            bbox = self._win_bbox
        if not bbox or bbox.get("width", 0) <= 0:
            return None
        try:
            fx = float(rel.get("fx", 0.0))
            fy = float(rel.get("fy", 0.0))
            x = int(bbox["left"]) + int(bbox["width"] * max(0.0, min(1.0, fx)))
            y = int(bbox["top"]) + int(bbox["height"] * max(0.0, min(1.0, fy)))
        except Exception:
            return None
        score = 60 + _bonus_history(step, "rel_window")
        # PRD §5: sin fingerprint, las coords son emergencia, no strong.
        emergency = not _has_fingerprint_and_attempted(step)
        return TargetResolutionResult(
            method="rel_window", target_kind="relative_point", score=score,
            coords={"x": x, "y": y},
            explanation="coords relativas a ventana actual",
            safety_reason=(
                "coords_emergency_no_fingerprint_available" if emergency else ""
            ),
        )

    # ── Coords absolutas (último recurso) ────────────────────────
    def _try_absolute(self, step: CompiledStep) -> Optional[TargetResolutionResult]:
        fb = step.target_context.fallback_coords
        if not fb:
            return None
        bbox = self._win_bbox
        score = 30 + _bonus_history(step, "coords_absolute")
        if bbox:
            try:
                x = int(fb.get("x", 0))
                y = int(fb.get("y", 0))
                left = int(bbox.get("left", 0))
                top = int(bbox.get("top", 0))
                width = int(bbox.get("width", 0))
                height = int(bbox.get("height", 0))
                tol_x = max(10, width // 5)
                tol_y = max(10, height // 5)
                inside = ((left - tol_x) <= x <= (left + width + tol_x)
                          and (top - tol_y) <= y <= (top + height + tol_y))
                if inside:
                    score += 20  # mismo layout, riesgo bajo
            except Exception:
                pass
        score = max(0, min(100, score))
        # PRD §5: si NO había fingerprint disponible, las coords son
        # emergencia explícita — la UI debe presentarlas como tal en
        # vez de "strong target". safety_reason lo marca claro.
        emergency = not _has_fingerprint_and_attempted(step)
        return TargetResolutionResult(
            method="coords_absolute", target_kind="absolute_point",
            score=score, coords=fb,
            explanation="coords grabadas (fallback)",
            safety_reason=(
                "coords_emergency_no_fingerprint_available" if emergency else ""
            ),
        )

    # ── Bundle projection (primary + alternates) ────────────────
    @staticmethod
    def _project_bundle_onto_step(step: CompiledStep,
                                   bundle: Dict[str, Any]) -> CompiledStep:
        """Construye un CompiledStep efímero con el ``target_context``
        sobreescrito por los campos de un alternate bundle. Lo usamos para
        re-ejecutar las estrategias contra cada candidato (primary + alts)
        sin duplicar lógica.
        """
        try:
            tc = step.target_context.model_copy(deep=True)
        except Exception:
            return step
        for key in (
            "uia_data", "anchor_bbox", "click_offset_within_bbox",
            "image_ref", "image_ref_mid", "fallback_coords",
            "fallback_coords_relative_to_window", "web_data",
            "process_name", "window_title", "hwnd",
            "desktop_uia", "vision_data", "semantic", "target_id",
        ):
            v = bundle.get(key)
            if v is not None:
                try:
                    setattr(tc, key, v)
                except Exception:
                    continue
        try:
            s2 = step.model_copy(deep=False)
            s2.target_context = tc
            return s2
        except Exception:
            return step

    def _resolution_try_functions(
        self,
        step: CompiledStep,
        mission: Optional[Any] = None,
    ) -> List[Any]:
        chain: List[Tuple[str, Any]] = [
            ("resolver:web_locator", self._try_web_locator),
            ("resolver:uia", self._try_uia),
            ("resolver:visual_fingerprint", self._try_visual_fingerprint),
            ("resolver:visual_anchor", self._try_visual),
            ("resolver:rel_window", self._try_rel_window),
            ("resolver:coords_absolute", self._try_absolute),
        ]
        try:
            from app.core.config import get_settings

            if not get_settings().SLL_ENABLED:
                return [fn for _, fn in chain]
        except Exception:
            return [fn for _, fn in chain]
        try:
            from app.services.runtime.strategy_learning_layer import (
                build_context_from_compiled_step,
                execution_signals_for_mission,
                rank_strategies_with_learning,
            )

            ctx = build_context_from_compiled_step(step, mission)
            truth, sep = execution_signals_for_mission(mission)
            names = [n for n, _ in chain]
            ranked, _ = rank_strategies_with_learning(
                names,
                ctx,
                learning=None,
                execution_truth_confirmed=truth,
                sep_alignment_score=sep,
            )
            name_to_fn = dict(chain)
            return [name_to_fn[n] for n in ranked if n in name_to_fn]
        except Exception as e:
            log.debug("SLL resolver order fallback: %s", e)
            return [fn for _, fn in chain]

    def _resolve_single_bundle(
        self, step: CompiledStep, label: str,
        *,
        mission: Optional[Any] = None,
    ) -> List[TargetResolutionResult]:
        """Ejecuta TODAS las estrategias contra un único bundle ya
        proyectado en ``step``. Devuelve la lista de resultados no nulos,
        anotados con ``bundle_used`` para trazabilidad.
        """
        results: List[TargetResolutionResult] = []
        for fn in self._resolution_try_functions(step, mission):
            try:
                r = fn(step)
            except Exception as e:
                log.debug(f"resolver {fn.__name__} excepcion: {e}")
                r = None
            if r is not None:
                r.bundle_used = label
                # target_kind ya viene puesto por cada _try_*; saneamos por si.
                if not r.target_kind or r.target_kind == "none":
                    r.target_kind = _METHOD_KIND.get(r.method, "none")
                results.append(r)
        return results

    # ── Public ──────────────────────────────────────────────────
    def resolve(self, step: CompiledStep, *, mission: Optional[Any] = None) -> TargetResolutionResult:
        """Devuelve la mejor estrategia disponible probando primero el
        bundle principal y luego cada alternate en orden histórico.

        Reglas:
          1. SIEMPRE pasamos por el context gate (app/url correcta).
          2. Construimos la lista de candidatos como
             ``[primary_bundle] + step.target_context.alternates``.
          3. Ejecutamos todas las estrategias contra cada uno y mezclamos.
          4. Ordenamos por (score, prioridad de método).
          5. Si gana un alternate, anotamos para que el repair lo promueva.
          6. Si nada funciona devolvemos ``method='none'`` con score 0
             para que el caller abra modo reparación.
          7. PRD §Fase 2D: medimos ``target_resolution_time_ms`` y
             registramos un :class:`ResolutionEvent` en la telemetría.
        """
        import time as _time
        _t0 = _time.time()
        ok, gate_msg = self._check_context_gate(step)
        if not ok:
            log.warning(f"context gate falló: {gate_msg}")
            # Devolvemos score bajo pero seguimos intentando.
        # Cacheamos la ventana antes de los visual/rel_window.
        self._get_or_find_window(step)

        candidates: List[TargetResolutionResult] = []

        # 1) Primary bundle (lo que ya está en target_context).
        candidates.extend(self._resolve_single_bundle(step, "primary", mission=mission))

        # 2) Alternates (aprendidos por reparación).
        alternates = list(step.target_context.alternates or [])
        for idx, alt in enumerate(alternates):
            try:
                synth = self._project_bundle_onto_step(step, alt)
                candidates.extend(
                    self._resolve_single_bundle(synth, f"alternate:{idx}", mission=mission),
                )
            except Exception as e:
                log.debug(f"resolver alternate#{idx} excepcion: {e}")
                continue

        if not candidates:
            # Sin candidatos pero con fingerprint disponible que falló →
            # explicitamos el motivo para que el Player abra reparación
            # en vez de un mensaje genérico.
            sr = ""
            if _has_fingerprint_and_attempted(step):
                sr = "fingerprint_failed_no_blind_coords"
            empty = TargetResolutionResult(
                method="none", target_kind="none", score=0,
                explanation="ninguna estrategia produjo resultado",
                safety_reason=sr,
            )
            self._emit_telemetry(empty, step, candidates, _t0)
            return empty
        # Ranking: score absoluto, con desempate por orden de preferencia.
        priority = {"web_locator": 7, "uia_exact": 6, "uia_semantic": 5,
                    "visual_fingerprint": 4, "visual_anchor": 3,
                    "rel_window": 2, "coords_absolute": 1}
        candidates.sort(key=lambda c: (c.score, priority.get(c.method, 0)),
                        reverse=True)
        # Anotar candidate_rank en orden descendente (1 = ganador).
        for rank, cand in enumerate(candidates, start=1):
            cand.candidate_rank = rank

        winner = candidates[0]

        # ── PRD 2026-05-12: SAFETY GATE ────────────────────────────
        # Si el ganador es coord-based (rel_window / coords_absolute) Y
        # había un fingerprint disponible que NO consiguió relocar el
        # target, prohibimos el click a ciegas: degradamos a "none"
        # con safety_reason. El Player abre reparación / pregunta.
        #
        # Regla del PRD §10: NO ejecutar coordenadas absolutas salvo
        # emergency mode explícito. Aquí el "modo emergencia" es: no
        # tenemos fingerprint en absoluto. Si hay fingerprint, la
        # respuesta correcta es preguntar, no clickear.
        if winner.method in ("coords_absolute", "rel_window"):
            visual_succeeded = any(
                c.method == "visual_fingerprint" for c in candidates
            )
            if (not visual_succeeded) and _has_fingerprint_and_attempted(step):
                log.warning(
                    "[resolver] safety gate: visual_fingerprint falló y "
                    f"el ganador era '{winner.method}'. Bloqueando click "
                    "a ciegas; el Player pedirá confirmación humana."
                )
                blocked = TargetResolutionResult(
                    method="none", target_kind="none", score=0,
                    explanation=(
                        f"safety: ganador era {winner.method} pero "
                        "el fingerprint no relocalizó; no clickeamos a ciegas"
                    ),
                    safety_reason="fingerprint_failed_no_blind_coords",
                    candidates_tried=[
                        {"method": c.method, "score": c.score}
                        for c in candidates
                    ],
                )
                self._emit_telemetry(blocked, step, candidates, _t0)
                return blocked

        self._emit_telemetry(winner, step, candidates, _t0)
        return winner

    # ── Telemetry helper (PRD §Fase 2E) ─────────────────────────
    def _emit_telemetry(
        self,
        winner: "TargetResolutionResult",
        step: "CompiledStep",
        candidates: List["TargetResolutionResult"],
        t0: float,
    ) -> None:
        """Registra un :class:`ResolutionEvent` en el colector default.

        Best-effort: cualquier fallo se traga sin afectar la resolución.
        """
        try:
            import time as _time
            from app.services.missions.execution_telemetry import (
                ResolutionEvent,
                get_default_collector,
            )
            from app.services.missions.execution_decision import (
                compute_execution_confidence as compute_execution_confidence_tristate,
            )
            compute_execution_confidence = compute_execution_confidence_tristate  # type: ignore[assignment]
            latency_ms = int(round((_time.time() - t0) * 1000))
            # zero_capture = el ganador fue visual_fingerprint cuya
            # explicación mencionó nivel="coord" (sin foto). Mantenemos
            # detección robusta por substring para no acoplarnos a la
            # forma exacta del mensaje.
            zero_cap = (
                winner.method == "visual_fingerprint"
                and "nivel=coord" in (winner.explanation or "")
            )
            fp_used = any(
                c.method == "visual_fingerprint" for c in candidates
            ) or _has_fingerprint_and_attempted(step)
            confidence = compute_execution_confidence(winner)
            evt = ResolutionEvent(
                method=winner.method,
                score=int(winner.score or 0),
                target_kind=winner.target_kind,
                latency_ms=latency_ms,
                zero_capture=bool(zero_cap),
                fingerprint_used=bool(fp_used),
                needs_confirmation=bool(winner.needs_confirmation),
                safety_reason=str(winner.safety_reason or ""),
                confidence=confidence.value,
            )
            get_default_collector().record_resolution(evt)
        except Exception as e:
            log.debug(f"resolver telemetry emit failed: {e}")


__all__ = ["TargetResolver", "TargetResolutionResult"]
