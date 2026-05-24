"""
Calidad de paso Nevlan — única fuente de verdad para semáforo y contadores.

Usar ``compute_step_quality`` en compilador, revisión de misión,
automation center y agrupadores semánticos para evitar inconsistencias.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Literal, Sequence

from app.contracts.mission import (
    CompiledStep,
    InterpretedStep,
    ActionStrategy,
    ValidationStrategy,
)

Level = Literal["green", "yellow", "red"]


def level_from_capture_score(score: float) -> Level:
    if score >= 80:
        return "green"
    if score >= 50:
        return "yellow"
    return "red"


def _worst(a: Level, b: Level) -> Level:
    order = {"green": 0, "yellow": 1, "red": 2}
    return a if order[a] >= order[b] else b


def _has_web_locators(tc) -> bool:
    w = tc.web_data or {}
    locs = w.get("locators") or []
    return bool(locs)


def _has_uia_signal(tc) -> bool:
    uia = tc.uia_data or {}
    if not isinstance(uia, dict):
        return False
    aid = (uia.get("automation_id") or "").strip()
    nm = (uia.get("name") or "").strip()
    ct = (uia.get("control_type") or "").strip()
    GENERIC = {
        "groupcontrol", "panecontrol", "customcontrol", "documentcontrol",
        "windowcontrol", "toolbarcontrol", "statusbarcontrol",
    }
    if aid:
        return True
    return bool(nm and ct and ct.lower() not in GENERIC)


def _has_anchor_image(tc) -> bool:
    return bool(tc.image_ref_mid or tc.image_ref)


def _has_rel_window_coords(tc) -> bool:
    return tc.fallback_coords_relative_to_window is not None


def _has_validation(cs: CompiledStep) -> bool:
    vs = getattr(cs.validation_strategy, "value", str(cs.validation_strategy))
    return bool(vs) and vs != "none" and cs.validation_strategy != ValidationStrategy.NONE


def _is_coordinates_only_fallback(tc) -> bool:
    """Sólo coords absolutas sin señales fuertes (frágil)."""
    strong = (
        _has_web_locators(tc)
        or _has_uia_signal(tc)
        or _has_anchor_image(tc)
        or (tc.desktop_uia and isinstance(tc.desktop_uia, dict) and tc.desktop_uia.get("runtime_id"))
    )
    fc = tc.fallback_coords or {}
    if strong:
        return False
    # Tiene coords absolutas como único ancla
    if fc and isinstance(fc.get("x"), int) and isinstance(fc.get("y"), int):
        return True
    return False


@dataclass
class StepQuality:
    level: Level
    """Semáforo final (peor nivel gana entre heurísticas)."""
    capture_score_pct: float
    capture_level: Level = "green"
    """Puro modelo de captura/compilador (antes de runtime)."""
    human_label_es: str = ""
    """Etiqueta corta UX: Confiable · Revisar · Débil · …"""
    reasons: List[str] = field(default_factory=list)
    has_uia: bool = False
    has_web_locators: bool = False
    has_image_anchor: bool = False
    has_relative_window_coords: bool = False
    has_validation_strategy: bool = False
    abs_coords_only_weak: bool = False
    signals_score: float = 0.0

    def is_weak(self) -> bool:
        return self.level == "red"

    def summary_line(self) -> str:
        return f"{self.level} ({self.capture_score_pct:.0f}%): " + "; ".join(
            self.reasons[:6]
        )


def compute_step_quality(
    c_step: CompiledStep,
    interpreted_step: Optional[InterpretedStep] = None,
) -> StepQuality:
    """Calcula calidad estable para UI, compilador y estadísticas."""
    tc = c_step.target_context
    cf: Dict[str, Any] = dict(tc.confidence or {})
    capture = float(cf.get("capture_score") if cf.get("capture_score") is not None else float("nan"))
    if capture != capture:  # nan
        capture = 0.0
    rs = list(cf.get("capture_reasons") or [])

    capture_level: Level = level_from_capture_score(capture)
    level = capture_level

    interp_level: Optional[str] = None
    if interpreted_step is not None:
        interp_level = interpreted_step.quality_level

    has_uia = _has_uia_signal(tc)
    has_web = _has_web_locators(tc)
    has_img = _has_anchor_image(tc)
    has_rel = _has_rel_window_coords(tc)
    has_val = _has_validation(c_step)
    abs_weak = _is_coordinates_only_fallback(tc)

    # Firma Nevlan opcional (recorder / migración).
    sig = tc.signature if isinstance(tc.signature, dict) else {}
    sig_conf = sig.get("confidence") if isinstance(sig.get("confidence"), dict) else {}
    sq_sig = sig_conf.get("target_quality") or sig_conf.get("quality_level")
    if isinstance(sq_sig, str) and sq_sig.strip().lower() in ("green", "yellow", "red"):
        level = _worst(level, sq_sig.strip().lower())  # type: ignore[arg-type]
        rs.append(f"signature:{sq_sig}")

    if interp_level in ("green", "yellow", "red"):
        level = _worst(level, interp_level)  # type: ignore[arg-type]

    # Penalización explícita: sólo pantalla/coords sin bundle.
    if abs_weak and c_step.action_strategy in (
        ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK,
        ActionStrategy.RIGHT_CLICK, ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        level = _worst(level, "red")
        if "-only_abs_coords" not in "".join(rs):
            rs.append("abs_coords_only_fallback")

    # Boost parcial cuando hay combinación muy buena (no fuerza green si capture es <50).
    sig_pts = (
        int(has_web) * 35
        + int(has_uia) * 30
        + int(has_img) * 18
        + int(has_rel) * 12
        + int(has_val) * 8
    )
    signals_score = float(min(100, sig_pts))

    if has_web and has_uia and has_img and has_rel and capture >= 65:
        # Bundle completo: sube al menos a amarillo salvo caso extremo (<35).
        if capture >= 45:
            level = _worst("yellow", level)  # type: ignore[arg-type]
        if capture >= 75:
            level = _worst("green", level)  # type: ignore[arg-type]
        rs.append("strong_bundle_multi_signal")
    elif sig_pts >= 70 and capture >= 60:
        rs.append("partial_signals_high")

    # Replay — solo lectura de ``confidence.runtime_stats``
    rt: Dict[str, Any] = dict(cf.get("runtime_stats") or {})
    fails = int(rt.get("failures") or 0)
    runs_rt = int(rt.get("runs") or 0)
    streak_fail = int(rt.get("fail_streak") or 0)
    last_ok = rt.get("last_success")
    if runs_rt >= 2 and fails >= 1:
        if streak_fail >= 2 or fails >= max(2, runs_rt // 2):
            level = _worst(level, "red")  # type: ignore[arg-type]
            rs.append("runtime_recent_fail")
        elif last_ok is False:
            level = _worst(level, "yellow")  # type: ignore[arg-type]
            rs.append("runtime_last_fail")

    human_lab = ""
    # Etiquetas en español
    if cf.get("user_accepted_weak"):
        human_lab = "Riesgo aceptado"
    elif streak_fail >= 2 or (runs_rt >= 2 and last_ok is False):
        human_lab = "Falló recientemente"
    elif level == "green":
        human_lab = "Confiable"
    elif level == "yellow":
        human_lab = "Revisar"
    else:
        human_lab = "Débil"

    return StepQuality(
        level=level,  # type: ignore[arg-type]
        capture_level=capture_level,  # type: ignore[arg-type]
        capture_score_pct=capture,
        human_label_es=human_lab,
        reasons=list(dict.fromkeys([str(x) for x in rs])),
        has_uia=has_uia,
        has_web_locators=has_web,
        has_image_anchor=has_img,
        has_relative_window_coords=has_rel,
        has_validation_strategy=has_val,
        abs_coords_only_weak=abs_weak,
        signals_score=signals_score,
    )


def count_weak_steps(
    compiled: Sequence[CompiledStep],
    interpreted: Optional[Sequence[InterpretedStep]] = None,
) -> int:
    """Cuenta pasos frágiles: rojos o marcados runtime como fallidos recientes."""
    n = 0
    for i, c in enumerate(compiled):
        i_step = interpreted[i] if interpreted and i < len(interpreted) else None
        sq = compute_step_quality(c, i_step)
        if sq.level == "red":
            n += 1
        elif sq.human_label_es == "Falló recientemente":
            n += 1
    return n
