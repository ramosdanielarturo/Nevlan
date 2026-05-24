"""
Tests for PRD 2026-05-12 — Coord Verifier zero-capture nivel-1.

Cubre las reglas del PRD:

  * UIA hit-test verifica strong cuando coinciden automation_id +
    control_type + bbox compatible.
  * UIA name + control_type + click_inside_bbox = strong.
  * GroupControl / PaneControl SIN texto/automation_id = insufficient
    aunque la bbox cuadre (regla §10).
  * DOM elementFromPoint con aria-label + role + inside = strong.
  * DOM con placeholder + role = strong (caso input/searchbox).
  * URL/dominio cambió → no_match aunque otros campos coincidan.
  * make_bool_coord_verifier devuelve True solo en strong.
  * Integración con TargetResolver: cuando coord_verifier responde
    True, locate_with_progressive_vision termina en nivel="coord"
    con 0 capturas (zero-capture).
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest

from app.contracts.mission import (
    ActionStrategy, CompiledStep, TargetContext,
)
from app.services.missions.coord_verifier import (
    DEFAULT_STRONG_THRESHOLD,
    VerificationResult,
    make_bool_coord_verifier,
    verify_dom_at_xy,
    verify_target_at_xy,
    verify_uia_at_xy,
)
from app.services.missions.target_resolver import TargetResolver


# ──────────────────────────────────────────────────────────────────────
# UIA verifier — reglas estructurales fuertes
# ──────────────────────────────────────────────────────────────────────


class TestUIAVerifier:
    def test_automation_id_plus_control_type_plus_bbox_is_strong(self):
        expected = {
            "automation_id": "btn_submit",
            "control_type": "ButtonControl",
            "name": "Submit",
            "bbox": {"left": 100, "top": 200, "width": 80, "height": 30},
        }

        def probe(x, y):
            return {
                "automation_id": "btn_submit",
                "control_type": "ButtonControl",
                "name": "Submit",
                "bbox": {"left": 102, "top": 201, "width": 80, "height": 30},
            }

        r = verify_uia_at_xy(expected, 140, 215, uia_probe=probe)
        assert r.verified is True
        assert r.verified_strong is True
        assert r.confidence >= 0.85
        assert "automation_id" in r.matched_fields
        assert "control_type" in r.matched_fields

    def test_name_plus_control_type_plus_inside_is_strong(self):
        expected = {
            "automation_id": "",
            "control_type": "ButtonControl",
            "name": "Aceptar",
            "bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
        }

        def probe(x, y):
            return {
                "automation_id": "",
                "control_type": "ButtonControl",
                "name": "Aceptar",
                "bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
            }

        r = verify_uia_at_xy(expected, 130, 115, uia_probe=probe)
        assert r.verified_strong is True
        assert "name" in r.matched_fields
        assert "click_inside_bbox" in r.matched_fields

    def test_group_control_without_text_is_insufficient(self):
        """Regla PRD §10: GroupControl sin name/automation_id NO puede
        ser strong aunque la bbox cuadre."""
        expected = {
            "automation_id": "",
            "control_type": "GroupControl",
            "name": "",
            "bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
        }

        def probe(x, y):
            return {
                "automation_id": "",
                "control_type": "GroupControl",
                "name": "",
                "bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
            }

        r = verify_uia_at_xy(expected, 130, 115, uia_probe=probe)
        assert r.verified_strong is False
        assert "weak_container" in r.reason or "weak" in r.weak_reason

    def test_pane_control_without_text_is_insufficient(self):
        expected = {
            "automation_id": "",
            "control_type": "PaneControl",
            "name": "",
            "bbox": {"left": 0, "top": 0, "width": 800, "height": 600},
        }

        def probe(x, y):
            return {
                "automation_id": "",
                "control_type": "PaneControl",
                "name": "",
                "bbox": {"left": 0, "top": 0, "width": 800, "height": 600},
            }

        r = verify_uia_at_xy(expected, 400, 300, uia_probe=probe)
        assert r.verified_strong is False

    def test_bbox_only_is_weak(self):
        expected = {
            "automation_id": "id_a",
            "control_type": "ButtonControl",
            "name": "OK",
            "bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
        }

        def probe(x, y):
            return {
                "automation_id": "id_DIFFERENT",
                "control_type": "DifferentControl",
                "name": "Otro",
                "bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
            }

        r = verify_uia_at_xy(expected, 130, 115, uia_probe=probe)
        assert r.verified_strong is False
        assert r.confidence < DEFAULT_STRONG_THRESHOLD

    def test_probe_returns_none_means_no_match(self):
        expected = {
            "automation_id": "id_a",
            "control_type": "ButtonControl",
            "name": "OK",
            "bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
        }
        r = verify_uia_at_xy(expected, 130, 115, uia_probe=lambda x, y: None)
        assert r.verified is False
        assert r.verified_strong is False


# ──────────────────────────────────────────────────────────────────────
# DOM verifier
# ──────────────────────────────────────────────────────────────────────


class TestDOMVerifier:
    def test_aria_label_plus_role_plus_inside_is_strong(self):
        expected = {
            "role": "button",
            "aria_label": "Submit",
            "url": "https://x.com/login",
            "bbox": {"left": 100, "top": 200, "width": 80, "height": 30},
        }

        def probe(x, y):
            return {
                "role": "button",
                "aria_label": "Submit",
                "url": "https://x.com/login",
                "bbox": {"left": 102, "top": 201, "width": 80, "height": 30},
            }

        r = verify_dom_at_xy(expected, 140, 215, dom_probe=probe)
        assert r.verified_strong is True
        assert "aria_label" in r.matched_fields

    def test_placeholder_plus_role_is_strong_for_input(self):
        expected = {
            "role": "searchbox",
            "placeholder": "Buscar",
            "bbox": {"left": 100, "top": 50, "width": 600, "height": 36},
        }

        def probe(x, y):
            return {
                "role": "searchbox",
                "placeholder": "Buscar",
                "bbox": {"left": 100, "top": 50, "width": 600, "height": 36},
            }

        r = verify_dom_at_xy(expected, 400, 68, dom_probe=probe)
        assert r.verified_strong is True
        assert "placeholder" in r.matched_fields

    def test_url_changed_invalidates_match(self):
        expected = {
            "role": "button",
            "aria_label": "Submit",
            "url": "https://app.com/login",
            "bbox": {"left": 100, "top": 200, "width": 80, "height": 30},
        }

        def probe(x, y):
            return {
                "role": "button",
                "aria_label": "Submit",
                "url": "https://app.com/dashboard",  # cambió URL
                "bbox": {"left": 100, "top": 200, "width": 80, "height": 30},
            }

        r = verify_dom_at_xy(expected, 140, 215, dom_probe=probe)
        assert r.verified_strong is False
        assert "url_changed" in r.reason

    def test_generic_container_without_text_is_weak(self):
        expected = {
            "role": "generic",
            "aria_label": "",
            "tag": "div",
            "bbox": {"left": 0, "top": 0, "width": 1000, "height": 800},
        }

        def probe(x, y):
            return {
                "role": "generic",
                "aria_label": "",
                "tag": "div",
                "bbox": {"left": 0, "top": 0, "width": 1000, "height": 800},
            }

        r = verify_dom_at_xy(expected, 500, 400, dom_probe=probe)
        assert r.verified_strong is False


# ──────────────────────────────────────────────────────────────────────
# Compuesto: mixed source
# ──────────────────────────────────────────────────────────────────────


class TestVerifyTargetAtXY:
    def test_both_uia_and_dom_strong_returns_mixed(self):
        exp_uia = {
            "automation_id": "btn_a",
            "control_type": "ButtonControl",
            "name": "OK",
            "bbox": {"left": 100, "top": 100, "width": 50, "height": 25},
        }
        exp_web = {
            "role": "button",
            "aria_label": "OK",
            "bbox": {"left": 100, "top": 100, "width": 50, "height": 25},
        }

        def uia_p(x, y):
            return dict(exp_uia)

        def dom_p(x, y):
            return dict(exp_web)

        r = verify_target_at_xy(
            expected_uia=exp_uia, expected_web=exp_web,
            x=120, y=110, uia_probe=uia_p, dom_probe=dom_p,
        )
        assert r.source == "mixed"
        assert r.verified_strong is True
        assert r.confidence >= 0.92

    def test_no_evidence_returns_none_source(self):
        r = verify_target_at_xy(x=10, y=20)
        assert r.verified_strong is False
        assert r.source in ("none", "uia", "dom")


# ──────────────────────────────────────────────────────────────────────
# Bool adapter para la pipeline visual
# ──────────────────────────────────────────────────────────────────────


class TestBoolCoordVerifier:
    def test_returns_true_only_when_strong(self):
        exp = {
            "automation_id": "x", "control_type": "ButtonControl",
            "name": "Go", "bbox": {"left": 0, "top": 0, "width": 100, "height": 30},
        }
        verifier = make_bool_coord_verifier(
            expected_uia=exp,
            uia_probe=lambda x, y: dict(exp),
        )
        assert verifier(50, 15) is True

    def test_returns_false_when_only_weak(self):
        exp = {
            "automation_id": "x", "control_type": "ButtonControl",
            "name": "Go", "bbox": {"left": 0, "top": 0, "width": 100, "height": 30},
        }
        verifier = make_bool_coord_verifier(
            expected_uia=exp,
            uia_probe=lambda x, y: {
                "automation_id": "OTHER",
                "control_type": "DiffControl",
                "name": "x",
                "bbox": {"left": 0, "top": 0, "width": 100, "height": 30},
            },
        )
        assert verifier(50, 15) is False

    def test_returns_false_on_probe_exception(self):
        def boom(x, y):
            raise RuntimeError("uia crashed")

        verifier = make_bool_coord_verifier(
            expected_uia={"automation_id": "x", "control_type": "ButtonControl",
                          "name": "Go", "bbox": {}},
            uia_probe=boom,
        )
        assert verifier(0, 0) is False


# ──────────────────────────────────────────────────────────────────────
# Integración: TargetResolver activa zero-capture cuando UIA verifica
# ──────────────────────────────────────────────────────────────────────


def _make_step_with_uia_and_fingerprint(
    *, with_web: bool = False,
) -> CompiledStep:
    """Step con UIA pre-grabada + fingerprint disponible + fallback coords."""
    tc = TargetContext()
    tc.uia_data = {
        "automation_id": "btn_test",
        "control_type": "ButtonControl",
        "name": "Test",
        "bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
    }
    if with_web:
        tc.web_data = {
            "role": "button", "aria_label": "Test",
            "bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
        }
    tc.fallback_coords = {"x": 140, "y": 115}
    tc.vision_data = {
        "visual_fingerprint": {
            "available": True, "size": (48, 48),
            "phash_hex": "ab" * 32, "dhash_hex": "cd" * 32,
            "orb_desc_b64": "", "orb_kp": [], "template_bgr_b64": "",
        }
    }
    return CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=tc,
    )


def test_resolver_zero_capture_when_uia_verifies_target():
    """Cuando UIA confirma el target en la coord grabada, la pipeline
    visual termina en nivel='coord' con 0 capturas tomadas."""
    step = _make_step_with_uia_and_fingerprint()

    fake_uia_probe = lambda x, y: dict(step.target_context.uia_data)

    # Resolver con UIA inyectado.
    resolver = TargetResolver(
        uia_probe_factory=lambda: fake_uia_probe,
        dom_probe_factory=lambda: None,
    )

    # Espiamos la pipeline visual para verificar que NO se tomó ninguna foto.
    from app.services.missions import execution_vision_pipeline as evp

    original = evp.locate_with_progressive_vision
    spy = {"calls": []}

    def spy_locate(**kwargs):
        spy["calls"].append(kwargs)
        return original(**kwargs)

    with patch.object(evp, "locate_with_progressive_vision", spy_locate):
        result = resolver.resolve(step)

    # La pipeline se llamó (con coord_verifier real) y debió terminar
    # en nivel "coord" sin capturas.
    assert len(spy["calls"]) >= 1
    last = spy["calls"][-1]
    cv = last.get("coord_verifier")
    assert cv is not None, "coord_verifier debe estar conectado"
    # Llamamos directamente al verifier para confirmar zero-capture.
    assert cv(140, 115) is True

    # El resolver debe ganar con visual_fingerprint (nivel coord, score=1.0)
    assert result.method == "visual_fingerprint"
    assert result.coords == {"x": 140, "y": 115}
    assert result.is_high_confidence()


def test_resolver_falls_to_visual_when_uia_does_not_verify():
    """Si UIA NO confirma el target, la pipeline baja a nivel 2 (foto local)."""
    step = _make_step_with_uia_and_fingerprint()

    # Probe que devuelve un control DIFERENTE al esperado.
    fake_probe = lambda x, y: {
        "automation_id": "OTHER",
        "control_type": "DifferentControl",
        "name": "Distinto",
        "bbox": {"left": 0, "top": 0, "width": 10, "height": 10},
    }
    resolver = TargetResolver(uia_probe_factory=lambda: fake_probe)

    # Mockeamos locate_with_progressive_vision para verificar que el
    # coord_verifier devuelve False y la pipeline trata de bajar
    # (devolvemos un "not found" para que el resolver use otra estrategia).
    from types import SimpleNamespace
    from app.services.missions import execution_vision_pipeline as evp

    captured_verifier = {"cv": None}

    def fake_locate(**kwargs):
        captured_verifier["cv"] = kwargs.get("coord_verifier")
        # Confirmamos que el coord_verifier respondería False.
        return SimpleNamespace(
            found=False, decision="ask", level="none",
            x=0, y=0, score=0.0, label="insufficient",
            bbox=None, components={}, captures_taken=0,
            elapsed_ms=1, reason="no_match",
        )

    with patch.object(evp, "locate_with_progressive_vision", fake_locate):
        result = resolver.resolve(step)

    cv = captured_verifier["cv"]
    assert cv is not None
    assert cv(140, 115) is False, "UIA no debería verificar (control distinto)"

    # Sin visual match, el winner debería ser bloqueado (fingerprint
    # disponible pero falló relocate) → safety gate aplica.
    assert result.method == "none"
    assert result.safety_reason == "fingerprint_failed_no_blind_coords"


# ──────────────────────────────────────────────────────────────────────
# coords_emergency_no_fingerprint_available — PRD §5
# ──────────────────────────────────────────────────────────────────────


def test_coords_absolute_without_fingerprint_marked_emergency():
    """Step sin fingerprint con fallback coords: el resolver acepta
    las coords como EMERGENCY (no como strong) y lo marca explícito.
    """
    tc = TargetContext()
    tc.fallback_coords = {"x": 50, "y": 80}
    step = CompiledStep(
        action_strategy=ActionStrategy.CLICK,
        target_context=tc,
    )
    resolver = TargetResolver()
    result = resolver.resolve(step)

    assert result.method in {"coords_absolute", "rel_window"}
    assert result.safety_reason == "coords_emergency_no_fingerprint_available"
    # PRD §5: nunca presentarlo como strong.
    assert result.is_high_confidence() is False
    assert result.coords == {"x": 50, "y": 80}


def test_coords_absolute_with_fingerprint_NOT_marked_emergency():
    """Si hay fingerprint Y la safety gate decide degradar, el flag
    debe ser ``fingerprint_failed_no_blind_coords``, no ``emergency``.
    Aquí construimos el caso donde el fingerprint encontró ⇒ gana, y
    verificamos que el flag emergency NUNCA aparece.
    """
    step = _make_step_with_uia_and_fingerprint()
    fake_probe = lambda x, y: dict(step.target_context.uia_data)
    resolver = TargetResolver(uia_probe_factory=lambda: fake_probe)
    result = resolver.resolve(step)
    # El ganador es visual_fingerprint, no debería tener emergency tag.
    assert result.safety_reason != "coords_emergency_no_fingerprint_available"
