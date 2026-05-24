"""
Tests for PRD 2026-05-12 — `relocate_target()` integrado al TargetResolver.

Cubre la regla central:

  Cuando una estrategia no encuentra el target en su posición esperada,
  antes de fallar o usar coordenadas, el resolver llama al pipeline
  visual progresivo (que internamente usa `relocate_target()`).

  1. Si encuentra con confianza alta → ejecuta ahí (no usa coords).
  2. Si encuentra con confianza media → marca needs_confirmation=True.
  3. Si no encuentra Y existía fingerprint → degrada a method="none"
     con safety_reason="fingerprint_failed_no_blind_coords" en vez de
     caer a coords ciegas.

Inyectamos `locate_with_progressive_vision` en el módulo del resolver
para no tocar el SO / mss / pantallas reales.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import pytest

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    TargetContext,
)
from app.services.missions.target_resolver import TargetResolver


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _make_step_with_fingerprint_and_coords(
    *,
    fingerprint_available: bool = True,
    fallback_x: int = 100,
    fallback_y: int = 200,
) -> CompiledStep:
    """Construye un step que SOLO tiene fingerprint + fallback_coords.

    No tiene UIA / web / visual_anchor → eso fuerza al resolver a elegir
    entre `visual_fingerprint` y `coords_absolute`. La pregunta es: ¿cuál
    gana, y cuándo bloqueamos el coord-only?
    """
    tc = TargetContext()
    tc.fallback_coords = {"x": int(fallback_x), "y": int(fallback_y)}
    if fingerprint_available:
        tc.vision_data = {
            "visual_fingerprint": {
                "available": True,
                "size": (48, 48),
                "phash_hex": "deadbeef" * 8,
                "dhash_hex": "f00dface" * 8,
                "orb_desc_b64": "",
                "orb_kp": [],
                "template_bgr_b64": "",
            }
        }
    return CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=tc,
    )


def _fake_lookup_result(*, found: bool, decision: str = "accept",
                       x: int = 500, y: int = 600, score: float = 0.95):
    """Construye un `VisionLookupResult`-shaped objeto. Como
    `_try_visual_fingerprint` solo lee atributos, podemos usar un
    SimpleNamespace.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        found=bool(found),
        decision=str(decision),
        x=int(x), y=int(y),
        score=float(score),
        label="strong" if score >= 0.78 else "medium",
        bbox=None,
        level="local",
        components={},
        captures_taken=1,
        elapsed_ms=10,
        reason="" if found else "no_match",
    )


# ──────────────────────────────────────────────────────────────────────
# 1) Happy path: target movido → relocalizado → executor usa NEW coords
# ──────────────────────────────────────────────────────────────────────


def test_relocate_high_confidence_replaces_fallback_coords() -> None:
    """Target grabado en (100,200) pero ahora está en (700,800).

    El fingerprint relocaliza con score 0.95 → el resolver elige
    `visual_fingerprint` con coords (700,800), NO `coords_absolute`
    con (100,200).
    """
    step = _make_step_with_fingerprint_and_coords(
        fallback_x=100, fallback_y=200,
    )
    resolver = TargetResolver()

    relocated = _fake_lookup_result(
        found=True, decision="accept", x=700, y=800, score=0.95,
    )
    with patch(
        "app.services.missions.execution_vision_pipeline"
        ".locate_with_progressive_vision",
        return_value=relocated,
    ):
        result = resolver.resolve(step)

    assert result.method == "visual_fingerprint", (
        f"esperaba visual_fingerprint, vino {result.method!r}"
    )
    assert result.target_kind == "visual_point"
    assert result.coords == {"x": 700, "y": 800}
    # CRÍTICO: NO usamos las fallback_coords originales (100,200).
    assert result.coords != {"x": 100, "y": 200}
    # Alta confianza, sin pedir confirmación.
    assert result.is_high_confidence()
    assert result.needs_confirmation is False
    assert result.safety_reason == ""


# ──────────────────────────────────────────────────────────────────────
# 2) Match medio → needs_confirmation = True
# ──────────────────────────────────────────────────────────────────────


def test_relocate_medium_confidence_sets_needs_confirmation() -> None:
    """Score 0.68 (entre 0.60 y 0.78) → decision='confirm'.

    El resolver lo acepta como ganador (mejor que coords_absolute)
    pero marca ``needs_confirmation=True`` para que el Player pida
    confirmación al humano antes de clickear.
    """
    step = _make_step_with_fingerprint_and_coords()
    resolver = TargetResolver()

    relocated = _fake_lookup_result(
        found=True, decision="confirm", x=410, y=320, score=0.68,
    )
    with patch(
        "app.services.missions.execution_vision_pipeline"
        ".locate_with_progressive_vision",
        return_value=relocated,
    ):
        result = resolver.resolve(step)

    assert result.method == "visual_fingerprint"
    assert result.needs_confirmation is True
    assert "medium_confidence" in result.safety_reason
    assert result.coords == {"x": 410, "y": 320}


# ──────────────────────────────────────────────────────────────────────
# 3) Fail-safe: target NO encontrado y NO se usan coordenadas
# ──────────────────────────────────────────────────────────────────────


def test_relocate_not_found_blocks_blind_coord_click() -> None:
    """No hay UIA / web / visual_anchor; el fingerprint NO matchea.

    La única estrategia que podría haber ganado era `coords_absolute`,
    pero el resolver lo BLOQUEA porque había un fingerprint disponible.
    Resultado: method='none' con safety_reason explícito.

    Esto es lo que evita clicks a ciegas (PRD §10).
    """
    step = _make_step_with_fingerprint_and_coords(
        fallback_x=999, fallback_y=999,
    )
    resolver = TargetResolver()

    not_found = _fake_lookup_result(found=False, score=0.0)
    with patch(
        "app.services.missions.execution_vision_pipeline"
        ".locate_with_progressive_vision",
        return_value=not_found,
    ):
        result = resolver.resolve(step)

    assert result.method == "none", (
        f"esperaba method='none', vino {result.method!r} con coords "
        f"{result.coords}. ESTO HUBIESE SIDO UN CLICK A CIEGAS."
    )
    assert result.target_kind == "none"
    assert result.score == 0
    assert result.safety_reason == "fingerprint_failed_no_blind_coords"
    # Las fallback_coords NUNCA deben aparecer en el resultado.
    assert result.coords is None
    # Y la explicación debe mencionar el motivo del block.
    assert "fingerprint" in result.explanation.lower()
    # En el audit deben quedar los candidatos que se intentaron, para
    # depurar por qué fallaron — pero ninguno se promueve a ganador.
    methods_tried = {c.get("method") for c in (result.candidates_tried or [])}
    assert "visual_fingerprint" not in methods_tried


# ──────────────────────────────────────────────────────────────────────
# 4) Sin fingerprint disponible: las coords SÍ son aceptables (emergency)
# ──────────────────────────────────────────────────────────────────────


def test_no_fingerprint_coords_remain_acceptable() -> None:
    """Si el step NO tiene fingerprint, no hay nada que validar y las
    coords son la única opción (modo emergencia). El resolver las
    acepta sin disparar la safety gate.

    Esto previene un regresión donde la gate fuese demasiado agresiva.
    """
    step = _make_step_with_fingerprint_and_coords(
        fingerprint_available=False,
        fallback_x=100, fallback_y=200,
    )
    resolver = TargetResolver()

    # No mockeamos: aunque el módulo se importe, no encontrará
    # fingerprint y devolverá None temprano.
    result = resolver.resolve(step)

    # coords_absolute o rel_window (si hay bbox) — cualquier coord-based
    # es aceptable porque no había fingerprint con qué cross-validar,
    # pero queda marcado como EMERGENCIA (PRD §5) — no como strong.
    assert result.method in {"coords_absolute", "rel_window"}
    assert result.safety_reason == "coords_emergency_no_fingerprint_available"
    assert result.coords == {"x": 100, "y": 200}


# ──────────────────────────────────────────────────────────────────────
# 5) Cuando el resolver bloquea, la safety gate deja trazas
# ──────────────────────────────────────────────────────────────────────


def test_blocked_resolution_records_safety_reason() -> None:
    """La safety_reason expone EXACTAMENTE qué pasó para que la UI
    pueda mostrar un mensaje preciso ('no encontramos el target,
    confirma manualmente') en vez de uno genérico.
    """
    step = _make_step_with_fingerprint_and_coords()
    resolver = TargetResolver()

    not_found = _fake_lookup_result(found=False, score=0.0)
    with patch(
        "app.services.missions.execution_vision_pipeline"
        ".locate_with_progressive_vision",
        return_value=not_found,
    ):
        result = resolver.resolve(step)

    assert result.safety_reason == "fingerprint_failed_no_blind_coords"
    # `needs_confirmation` es para el caso de match medio, no para el
    # bloqueado. El Player distingue por método=none vs needs_confirm.
    assert result.needs_confirmation is False
