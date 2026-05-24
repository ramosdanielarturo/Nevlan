"""
Nevlan — Target ↔ Outcome Isolation (PRD 2026-05-10i)
======================================================

Regla central:

    target_identity = qué objeto recibió la acción (evidencia
                      capturada ANTES o DURANTE el click).
    outcome         = qué cambió DESPUÉS (validación, no identidad).

El recorder captura UIA/web/visual de manera secuencial y NO atómica;
entre el click y la captura UIA pueden pasar 10–200 ms. En ese gap el
sistema puede haber cambiado de ventana / título / app / URL — y la
UIA leída pertenecería al ESTADO POSTERIOR. Si esa UIA contamina
``target_identity``, la inteligencia downstream cree que el humano
actuó sobre el nuevo objeto, no sobre la card que vio.

Estos tests cubren la separación estricta:

  1. ``detect_state_change`` reporta los flags estructurales esperados.
  2. UIA capturada después de un cambio de hwnd/title/app se mueve a
     ``debug_after_state.uia_after`` y desaparece de ``metadata.uia``.
  3. Web capturada después de un cambio de URL se mueve a
     ``debug_after_state.web_after``.
  4. Captura limpia (estado estable) deja UIA en su sitio.
  5. ``recorder_truth_layer`` no ve la post-action UIA como identidad
     porque ya no está bajo ``metadata.uia`` / ``anchor_bbox``.
  6. Outcome valida pero NO inventa el texto del target: si la UIA
     fue migrada y no hay OCR/visual, ``target_identity.name`` queda
     vacío y el visual reader devuelve ``primary_text=""``.
  7. Audit ``target_identity_isolation`` queda escrito en metadata
     incluso cuando NO hay contaminación, para que la auditoría pueda
     ver "se verificó la frontera target ↔ outcome".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from app.contracts.mission import (
    EventType,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions.recorder_capture_contract import (
    TargetAnchor,
    build_capture_contract,
    capture_target_anchor,
    detect_state_change,
    enforce_target_identity_isolation,
)
from app.services.missions.recorder_truth_layer import evaluate_event
from app.services.missions.screen_element_reader import read_visual_target


def _t(ms: int) -> datetime:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(milliseconds=ms)


def _click(
    *,
    x: int = 200, y: int = 200,
    title: str = "App",
    proc: str = "app.exe",
    hwnd: int = 100,
    metadata: Optional[Dict[str, Any]] = None,
    offset_ms: int = 0,
) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        timestamp=_t(offset_ms),
        offset_ms=offset_ms,
        mouse_action=MouseAction(x=x, y=y, button="left"),
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        metadata=metadata or {},
    )


# ──────────────────────────────────────────────────────────────────────
# 1. detect_state_change: flags estructurales
# ──────────────────────────────────────────────────────────────────────


def test_detect_state_change_reports_each_kind() -> None:
    before = TargetAnchor(
        hwnd=100, title="Login", process="app.exe", url="https://a/x",
        captured_at_ms=1000,
    )
    # Cambio de title.
    after_title = TargetAnchor(
        hwnd=100, title="Dashboard", process="app.exe", url="https://a/x",
        captured_at_ms=1100,
    )
    flags = detect_state_change(before, after_title)
    assert flags["title_changed"] is True
    assert flags["any_changed"] is True
    assert flags["hwnd_changed"] is False

    # Cambio de hwnd.
    after_hwnd = TargetAnchor(
        hwnd=999, title="Login", process="app.exe", url="https://a/x",
        captured_at_ms=1100,
    )
    flags = detect_state_change(before, after_hwnd)
    assert flags["hwnd_changed"] is True
    assert flags["any_changed"] is True

    # Cambio de app.
    after_app = TargetAnchor(
        hwnd=100, title="Login", process="other.exe", url="https://a/x",
        captured_at_ms=1100,
    )
    flags = detect_state_change(before, after_app)
    assert flags["app_changed"] is True

    # Cambio de URL.
    after_url = TargetAnchor(
        hwnd=100, title="Login", process="app.exe", url="https://a/y",
        captured_at_ms=1100,
    )
    flags = detect_state_change(before, after_url)
    assert flags["url_changed"] is True
    assert flags["hwnd_changed"] is False

    # Sin cambios.
    flags = detect_state_change(before, before)
    assert flags["any_changed"] is False


# ──────────────────────────────────────────────────────────────────────
# 2. UIA post-action migrada a debug_after_state
# ──────────────────────────────────────────────────────────────────────


def test_uia_captured_after_state_change_moved_to_debug_after_state() -> None:
    """Caso real de Chrome: el usuario hace click sobre la card del
    picker → el picker se cierra → Chrome carga una nueva pestaña →
    UIA se ejecuta entonces y devuelve un GroupControl genérico
    cubriendo el documento de la NUEVA pestaña.

    El bbox que UIA reporta (3840×1677, casi pantalla completa) es
    señal estructural típica de "post-action contamination". La
    isolation lo migra a ``debug_after_state``.
    """
    metadata = {
        "uia": {
            "name": "",
            "control_type": "GroupControl",
            "automation_id": "",
            "bbox": {"left": 0, "top": 363, "width": 3840, "height": 1677},
        },
        "anchor_bbox": {"left": 0, "top": 363, "width": 3840, "height": 1677},
        "target_bbox": {"left": 1612, "top": 1083, "width": 80, "height": 80},
        "target_source": "uia",
        "quality_level": "medium",
    }
    before = TargetAnchor(
        hwnd=200, title="¿Quién eres? - Google Chrome",
        process="chrome.exe", url="", captured_at_ms=1000,
    )
    after = TargetAnchor(
        hwnd=300, title="Nueva pestaña - Daniel Arturo Ramos - Google Chrome",
        process="chrome.exe", url="", captured_at_ms=1080,
    )
    out = enforce_target_identity_isolation(
        metadata, before=before, after=after,
    )
    # UIA + bbox migraron a debug_after_state.
    assert "uia" not in out
    assert "anchor_bbox" not in out
    assert "target_bbox" not in out
    debug = out["debug_after_state"]
    assert debug["uia_after"]["control_type"] == "GroupControl"
    assert debug["anchor_bbox_after"]["width"] == 3840
    assert debug["target_bbox_after"]["width"] == 80
    # target_source se baja a click_fallback (más honesto).
    assert out["target_source"] == "click_fallback"
    assert out["quality_level"] == "low"
    iso = out["target_identity_isolation"]
    assert iso["contaminated"] is True
    assert iso["state_change"]["hwnd_changed"] is True
    assert iso["state_change"]["title_changed"] is True
    assert "uia" in iso["moved_to_debug_after_state"]


# ──────────────────────────────────────────────────────────────────────
# 3. URL cambiada → web migrado, UIA se queda
# ──────────────────────────────────────────────────────────────────────


def test_web_captured_after_url_change_moved_to_debug_after_state() -> None:
    """Caso web: la URL cambió DURANTE la captura (redirect / SPA
    navigation). La UIA del browser sigue siendo la misma ventana
    (mismo hwnd, mismo title), pero los locators del DOM ya
    apuntan a la página nueva. Migramos solo ``web``.
    """
    metadata = {
        "uia": {"name": "Dashboard button", "control_type": "ButtonControl"},
        "web": {"url": "https://a/dashboard", "tag_name": "div"},
    }
    before = TargetAnchor(
        hwnd=11, title="App", process="chrome.exe",
        url="https://a/login", captured_at_ms=1000,
    )
    after = TargetAnchor(
        hwnd=11, title="App", process="chrome.exe",
        url="https://a/dashboard", captured_at_ms=1080,
    )
    out = enforce_target_identity_isolation(
        metadata, before=before, after=after,
    )
    # web migró; uia se queda.
    assert "web" not in out
    assert "uia" in out
    debug = out["debug_after_state"]
    assert "web_after" in debug
    assert "uia_after" not in debug
    iso = out["target_identity_isolation"]
    assert iso["contaminated"] is True
    assert iso["state_change"]["url_changed"] is True
    assert "web" in iso["moved_to_debug_after_state"]


# ──────────────────────────────────────────────────────────────────────
# 4. Captura limpia: nada se migra
# ──────────────────────────────────────────────────────────────────────


def test_clean_capture_keeps_uia_in_target_identity() -> None:
    metadata = {
        "uia": {"name": "OK", "control_type": "ButtonControl"},
        "anchor_bbox": {"left": 100, "top": 100, "width": 80, "height": 30},
        "target_source": "uia",
        "quality_level": "high",
    }
    anchor = TargetAnchor(
        hwnd=11, title="App", process="app.exe", url="",
        captured_at_ms=1000,
    )
    out = enforce_target_identity_isolation(
        metadata, before=anchor, after=anchor,
    )
    assert out["uia"]["name"] == "OK"
    assert out["target_source"] == "uia"
    assert out["quality_level"] == "high"
    assert "debug_after_state" not in out
    iso = out["target_identity_isolation"]
    assert iso["contaminated"] is False
    assert iso["state_change"]["any_changed"] is False
    assert iso["moved_to_debug_after_state"] == []


# ──────────────────────────────────────────────────────────────────────
# 5. Truth layer no ve post-action UIA como identidad
# ──────────────────────────────────────────────────────────────────────


def test_truth_layer_does_not_see_post_action_uia_as_target() -> None:
    """Después de la migración, ``recorder_truth_layer._build_target_identity``
    devuelve ``name=""``, ``has_uia=False`` — la post-action UIA
    queda invisible para la identidad.
    """
    metadata = {
        "uia": {
            "name": "Dashboard root",
            "control_type": "GroupControl",
            "bbox": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        },
        "anchor_bbox": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        "target_source": "uia",
        "quality_level": "medium",
    }
    before = TargetAnchor(
        hwnd=11, title="Login", process="app.exe", url="",
        captured_at_ms=1000,
    )
    after = TargetAnchor(
        hwnd=22, title="Dashboard", process="app.exe", url="",
        captured_at_ms=1080,
    )
    enforce_target_identity_isolation(metadata, before=before, after=after)
    ev = _click(x=500, y=400, title="Login", proc="app.exe",
                hwnd=11, metadata=metadata)
    truth = evaluate_event(ev)
    assert truth.target_identity["name"] == ""
    assert truth.target_identity["has_uia"] is False
    # Y la post-action sigue accesible para auditoría.
    assert "debug_after_state" in (ev.metadata or {})


# ──────────────────────────────────────────────────────────────────────
# 6. Outcome valida pero NO inventa el texto del target
# ──────────────────────────────────────────────────────────────────────


def test_outcome_can_validate_but_does_not_invent_target_text() -> None:
    """Aunque haya transición real (window/title cambian), si el
    target_identity quedó sin texto (UIA migrada, sin OCR, sin
    visual reader), el visual reader y el truth layer NO inventan
    nombres a partir del outcome. ``primary_text=""`` y
    ``evidence_level`` no escala a ``strong``.
    """
    metadata = {
        # Lo que realmente llegó: GroupControl post-action con bbox enorme.
        "uia": {
            "name": "",
            "control_type": "GroupControl",
            "bbox": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        },
        "anchor_bbox": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        "target_source": "uia",
        "quality_level": "medium",
    }
    before = TargetAnchor(
        hwnd=11, title="¿Quién eres? - X", process="app.exe", url="",
        captured_at_ms=1000,
    )
    after = TargetAnchor(
        hwnd=22, title="Inicio - X",  # outcome: title cambió, picker cerró
        process="app.exe", url="", captured_at_ms=1080,
    )
    enforce_target_identity_isolation(metadata, before=before, after=after)
    ev = _click(x=500, y=400, title="¿Quién eres? - X",
                proc="app.exe", hwnd=11, metadata=metadata)
    vti = read_visual_target(ev)
    # Sin UIA limpia y sin OCR: el reader no inventa texto.
    assert vti.primary_text == ""
    assert vti.secondary_text == ""
    assert vti.evidence_level in ("weak", "insufficient")
    truth = evaluate_event(ev)
    # Y el truth layer no llega a strong.
    assert truth.evidence_level != "strong"


# ──────────────────────────────────────────────────────────────────────
# 7. Audit siempre presente (con o sin contaminación)
# ──────────────────────────────────────────────────────────────────────


def test_target_identity_isolation_audit_records_changes() -> None:
    metadata: Dict[str, Any] = {
        "uia": {"name": "Login", "control_type": "ButtonControl"},
    }
    before = TargetAnchor(
        hwnd=11, title="A", process="app.exe", url="",
        captured_at_ms=1000,
    )
    after = TargetAnchor(
        hwnd=11, title="A", process="app.exe", url="",
        captured_at_ms=1100,
    )
    enforce_target_identity_isolation(metadata, before=before, after=after)
    iso = metadata["target_identity_isolation"]
    assert iso["contaminated"] is False
    assert iso["before"]["title"] == "A"
    assert iso["after"]["title"] == "A"
    assert iso["trusted_pre_action_identity"] is False
    assert iso["identity_preserved_after_transition"] is False
    # Y el flag any_changed es False.
    assert iso["state_change"]["any_changed"] is False


def test_trusted_pre_action_preserves_uia_when_app_changes_after_click() -> None:
    """Windows Search → navegador: identidad PRE-click fuerte no se destruye."""
    metadata: Dict[str, Any] = {
        "uia": {
            "name": "Google Chrome",
            "control_type": "ListItemControl",
            "automation_id": "",
            "bbox": {"left": 100, "top": 200, "width": 400, "height": 48},
            "parent_chain": {
                "ancestors": [{"name": "Results"}],
                "siblings_near_click": [],
            },
        },
        "anchor_bbox": {"left": 100, "top": 200, "width": 400, "height": 48},
        "target_source": "uia",
        "quality_level": "high",
        "target_signature": {
            "environment": {"primary_screen_px": {"w": 1920, "h": 1080}},
        },
    }
    before = TargetAnchor(
        hwnd=1, title="Search", process="SearchUI.exe", url="",
        captured_at_ms=1000,
    )
    after = TargetAnchor(
        hwnd=2, title="Chrome", process="chrome.exe", url="",
        captured_at_ms=1100,
    )
    out = enforce_target_identity_isolation(
        metadata, before=before, after=after, click_xy=(250, 220),
    )
    assert "uia" in out
    assert out["uia"]["control_type"] == "ListItemControl"
    assert out["target_source"] == "uia"
    iso = out["target_identity_isolation"]
    assert iso["trusted_pre_action_identity"] is True
    assert iso["identity_preserved_after_transition"] is True
    assert iso["successful_state_transition"] is True
    assert iso["contaminated"] is False
    contract = build_capture_contract(
        pre_snapshot=None,
        post_state=None,
        uia=out.get("uia"),
        parent_chain=(out.get("uia") or {}).get("parent_chain"),
        ocr=None,
        target_source=out.get("target_source"),
        quality_level=out.get("quality_level"),
        target_identity_isolation=iso,
    )
    assert contract["sufficient_for_execution"] is True
    assert contract["trusted_pre_action_identity"] is True


def test_ambiguous_weak_uia_still_migrates_on_app_change() -> None:
    """Sin señales fuertes → comportamiento legacy (migración)."""
    metadata = {
        "uia": {
            "name": "Maybe",
            "control_type": "GroupControl",
            "automation_id": "",
            "bbox": {"left": 0, "top": 0, "width": 400, "height": 300},
        },
        "anchor_bbox": {"left": 0, "top": 0, "width": 400, "height": 300},
        "target_source": "uia",
        "quality_level": "medium",
        "target_signature": {
            "environment": {"primary_screen_px": {"w": 1920, "h": 1080}},
        },
    }
    before = TargetAnchor(
        hwnd=1, title="A", process="a.exe", url="", captured_at_ms=1,
    )
    after = TargetAnchor(
        hwnd=2, title="B", process="b.exe", url="", captured_at_ms=2,
    )
    out = enforce_target_identity_isolation(
        metadata, before=before, after=after, click_xy=(50, 50),
    )
    assert "uia" not in out
    assert out["target_identity_isolation"]["contaminated"] is True


# ──────────────────────────────────────────────────────────────────────
# 8. capture_target_anchor: helper acepta WindowContext y dict
# ──────────────────────────────────────────────────────────────────────


def test_capture_target_anchor_accepts_window_context_and_dict() -> None:
    wc = WindowContext(
        hwnd=42, title="X", process_name="app.exe",
    )
    a = capture_target_anchor(wc, {"url": "https://a/x"}, clock_ms=lambda: 1000)
    assert a.hwnd == 42
    assert a.title == "X"
    assert a.process == "app.exe"
    assert a.url == "https://a/x"
    assert a.captured_at_ms == 1000

    # También acepta dict (útil para tests / mocks).
    b = capture_target_anchor(
        {"hwnd": 7, "title": "Y", "process_name": "y.exe"},
        None, clock_ms=lambda: 2000,
    )
    assert b.hwnd == 7
    assert b.title == "Y"
    assert b.process == "y.exe"
    assert b.url == ""
    assert b.captured_at_ms == 2000

    # Y None devuelve un anchor vacío sin romper.
    z = capture_target_anchor(None, None, clock_ms=lambda: 3000)
    assert z.hwnd is None
    assert z.title == ""
    assert z.captured_at_ms == 3000
