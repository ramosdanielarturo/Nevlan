"""
Nevlan — Recorder Capture Contract (PRD 2026-05-10h)
=====================================================

Tests quirúrgicos (máx. 8) para garantizar la regla de producto:

    Si el humano lo ve, el recorder debe guardar evidencia de que
    estaba ahí.

Las pruebas usan **inyección de dependencias** sobre el módulo
``recorder_capture_contract``: nada de pynput, ImageGrab ni pytesseract
en runtime de tests. Los tests construyen sus propios *grabbers* y
*OCR runners* sintéticos para validar la mecánica del contract.

Cobertura:

  1. Click recibe pre-snapshot del ring buffer (frame ANTES del click).
  2. Último click obtiene post-action state aunque la grabación se
     detenga inmediatamente.
  3. UIA débil dispara OCR de región expandida sobre pre-snapshot.
  4. Click sobre GroupControl registra parent_chain (ancestros +
     siblings cercanos).
  5. ``capture_contract`` declara honestamente qué evidencia falta.
  6. ``RecordedTruthEvent`` con ``capture_complete=False`` pero
     ``sufficient_for_execution=False`` ⇒ cap truth a ``medium``;
     si ``sufficient_for_execution=True`` conserva ``strong``.
  7. Click sobre card de perfil con visual inputs ⇒ contract completo.
  8. Diagnóstico: contract reporta lista de missing en JSON exportable.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.contracts.mission import (
    EventType,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions import recorder_capture_contract as rcc
from app.services.missions.recorder_capture_contract import (
    PostActionScheduler,
    PostActionState,
    PreActionSnapshot,
    ScreenshotRingBuffer,
    apply_capture_contract_to_metadata,
    build_capture_contract,
    capture_uia_parent_chain,
    is_uia_weak,
    maybe_run_ocr,
)
from app.services.missions.recorder_truth_layer import evaluate_event


# ──────────────────────────────────────────────────────────────────────
# Helpers comunes
# ──────────────────────────────────────────────────────────────────────


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


# Fake UIA element compatible con capture_uia_parent_chain.
class _FakeRect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


class _FakeUIA:
    def __init__(
        self,
        *,
        name: str = "",
        control_type: str = "GroupControl",
        automation_id: str = "",
        rect: Tuple[int, int, int, int] = (0, 0, 100, 100),
        parent: Optional["_FakeUIA"] = None,
        children: Optional[List["_FakeUIA"]] = None,
    ) -> None:
        self.Name = name
        self.ControlTypeName = control_type
        self.AutomationId = automation_id
        self.BoundingRectangle = _FakeRect(*rect)
        self._parent = parent
        self._children = children or []

    def GetParentControl(self):
        return self._parent

    def GetChildren(self):
        return list(self._children)


# ──────────────────────────────────────────────────────────────────────
# 1. Pre-action snapshot del ring buffer
# ──────────────────────────────────────────────────────────────────────


def test_mouse_click_gets_pre_action_snapshot_from_buffer(tmp_path) -> None:
    """El buffer entrega el frame más reciente capturado ANTES del
    click, con su edad calculada respecto al timestamp del click.
    """
    buf = ScreenshotRingBuffer(
        tmp_path,
        capacity=3,
        period_ms=100,
        # Grabber se controla manualmente vía push().
        grabber=lambda d: None,
        clock_ms=lambda: 0,
    )
    # Inyectamos 3 frames pre-click.
    buf.push(str(tmp_path / "f1.png"), 1000)
    buf.push(str(tmp_path / "f2.png"), 1200)
    buf.push(str(tmp_path / "f3.png"), 1400)

    # Click a t=1500ms ⇒ usa f3 (más reciente antes del click).
    snap = buf.get_before(1500)
    assert snap is not None
    assert snap.full_path.endswith("f3.png")
    assert snap.captured_at_ms == 1400
    assert snap.age_ms == 100

    # Click muy lejano ⇒ pre-snapshot rechazado por edad excesiva.
    too_late = buf.get_before(10_000, max_age_ms=500)
    assert too_late is None


# ──────────────────────────────────────────────────────────────────────
# 2. Último click obtiene post-action state aunque la grabación termine
# ──────────────────────────────────────────────────────────────────────


def test_last_click_gets_finalization_after_state() -> None:
    """``PostActionScheduler`` ejecuta la captura diferida en hilo
    daemon. Si la "grabación" detiene inmediatamente y llamamos
    ``wait_all``, el último click recibe ``PostActionState`` con
    ``detected_change=True`` y la firma del estado posterior.
    """
    scheduler = PostActionScheduler()
    ts_clock = {"now": 0}
    received: List[PostActionState] = []

    def state() -> Dict[str, Any]:
        # Simula que tras 300ms la ventana ya cambió.
        if ts_clock["now"] >= 300:
            return {"hwnd": 999, "title": "Dashboard", "url": "https://x/y"}
        return {"hwnd": 100, "title": "Login", "url": ""}

    def sleep_ms(ms: int) -> None:
        ts_clock["now"] += ms

    def clock_ms() -> int:
        return ts_clock["now"]

    scheduler.schedule(
        event_id="evt-1",
        click_ts_ms=0,
        before_state={"hwnd": 100, "title": "Login", "url": ""},
        capture_state=state,
        on_complete=lambda ps: received.append(ps),
        sleep_ms=sleep_ms,
        clock_ms=clock_ms,
    )
    # Damos tiempo al worker (es un hilo real).
    deadline = time.time() + 2.0
    while not received and time.time() < deadline:
        time.sleep(0.01)
    scheduler.wait_all(timeout_ms=2000)
    assert len(received) == 1
    ps = received[0]
    assert ps.detected_change is True
    assert ps.window_title == "Dashboard"
    assert ps.window_hwnd == 999
    assert ps.latency_ms >= 300


# ──────────────────────────────────────────────────────────────────────
# 3. UIA débil dispara OCR sobre región expandida
# ──────────────────────────────────────────────────────────────────────


def test_weak_uia_triggers_ocr_on_expanded_pre_click_region(tmp_path) -> None:
    """``maybe_run_ocr`` se ejecuta solo cuando ``is_uia_weak`` lo
    confirma. El OCR runner inyectado devuelve líneas distintas según
    el tamaño de la región.
    """
    pre_path = tmp_path / "frame.png"
    pre_path.write_bytes(b"fake png")

    # is_uia_weak True para GroupControl vacío + click_fallback.
    weak = is_uia_weak(
        {"name": "", "control_type": "GroupControl", "automation_id": ""},
        target_source="click_fallback",
    )
    assert weak is True

    calls: List[Dict[str, Any]] = []

    def fake_runner(image_path, click_xy, region_bbox):
        calls.append(dict(region_bbox))
        # Solo entregamos texto cuando la región crece a ≥ 420 px de lado.
        if int(region_bbox.get("width", 0)) >= 420:
            return {"lines": ["Personal", "Maria Lopez"]}
        return None

    res = maybe_run_ocr(
        pre_snapshot_full=str(pre_path),
        click_xy=(500, 500),
        region_steps_px=(240, 420, 640),
        ocr_runner=fake_runner,
    )
    assert res is not None
    assert res["ocr_lines"] == ["Personal", "Maria Lopez"]
    assert res["ocr_text_around"] == "Personal\nMaria Lopez"
    assert res["ocr_source"] == "pre_action_expanded_region"
    # El runner se llamó al menos dos veces (240 → 420).
    assert len(calls) >= 2

    # Cuando el UIA es fuerte, NO corremos OCR.
    strong = is_uia_weak(
        {
            "name": "Login button",
            "control_type": "ButtonControl",
            "automation_id": "loginBtn",
        },
        target_source="uia",
        quality_level="high",
    )
    assert strong is False


# ──────────────────────────────────────────────────────────────────────
# 4. parent_chain capturado para GroupControl vacío
# ──────────────────────────────────────────────────────────────────────


def test_groupcontrol_click_records_parent_chain() -> None:
    """``capture_uia_parent_chain`` recoge ancestros + siblings cercanos
    al click usando una jerarquía UIA fake. Funciona para CUALQUIER
    app — solo opera sobre la API genérica de uiautomation.
    """
    sib_a = _FakeUIA(
        name="Personal", control_type="TextControl",
        rect=(110, 110, 240, 140),
    )
    sib_b = _FakeUIA(
        name="Maria Lopez", control_type="TextControl",
        rect=(110, 145, 240, 175),
    )
    parent = _FakeUIA(
        name="Card 1", control_type="GroupControl",
        rect=(100, 100, 300, 300),
        children=[sib_a, sib_b],
    )
    grand = _FakeUIA(
        name="Profile Picker", control_type="DocumentControl",
        rect=(0, 0, 1024, 768),
    )
    parent._parent = grand
    elem = _FakeUIA(
        name="", control_type="GroupControl",
        rect=(100, 100, 300, 300), parent=parent,
    )
    chain = capture_uia_parent_chain(elem, click_xy=(150, 150))
    assert chain is not None
    ancestors = chain["ancestors"]
    assert ancestors[0]["name"] == "Card 1"
    assert ancestors[1]["name"] == "Profile Picker"
    sibs = chain["siblings_near_click"]
    assert {s["name"] for s in sibs} == {"Personal", "Maria Lopez"}


# ──────────────────────────────────────────────────────────────────────
# 5. capture_contract declara missing honestamente
# ──────────────────────────────────────────────────────────────────────


def test_capture_contract_marks_missing_evidence() -> None:
    """Cuando NO llegó pre_snapshot, post_state, OCR ni parent_chain,
    el contract debe enumerar los missing y marcar
    ``capture_complete=False``.
    """
    contract = build_capture_contract(
        pre_snapshot=None,
        post_state=None,
        uia={"name": "", "control_type": "GroupControl"},
        parent_chain=None,
        ocr=None,
        target_source="click_fallback",
        quality_level="low",
    )
    assert contract["capture_complete"] is False
    assert contract["has_pre_snapshot"] is False
    assert contract["has_post_state"] is False
    assert contract["has_uia"] is False
    assert contract["has_parent_chain"] is False
    assert contract["has_ocr_or_text"] is False
    assert set(contract["missing"]) >= {
        "pre_snapshot", "post_state", "uia",
        "parent_chain", "ocr_or_text",
    }
    assert contract["target_source"] == "click_fallback"
    assert contract["quality_level"] == "low"


def test_capture_contract_stable_aid_executable_without_full_bundle() -> None:
    aid_only = build_capture_contract(
        pre_snapshot=None,
        post_state=None,
        uia={
            "name": "",
            "control_type": "ButtonControl",
            "automation_id": "launch_OK",
        },
        parent_chain=None,
        ocr=None,
        target_source="click_fallback",
        quality_level="low",
    )
    assert aid_only["capture_complete"] is False
    assert aid_only["sufficient_for_execution"] is True


def test_capture_contract_name_without_collateral_not_sufficient_alone() -> None:
    pre = PreActionSnapshot(
        full_path="/tmp/x.png",
        captured_at_ms=400,
        age_ms=120,
    )
    name_collateral = build_capture_contract(
        pre_snapshot=None,
        post_state=None,
        uia={"name": "Sólo texto", "control_type": "TextControl"},
        parent_chain=None,
        ocr=None,
    )
    assert name_collateral["has_uia"] is True
    assert name_collateral["collateral_execution"] is False
    assert name_collateral["stable_automation_id"] is False
    assert name_collateral["sufficient_for_execution"] is False

    with_pre = build_capture_contract(
        pre_snapshot=pre,
        post_state=None,
        uia={"name": "Sólo texto", "control_type": "TextControl"},
        parent_chain=None,
        ocr=None,
    )
    assert with_pre["sufficient_for_execution"] is True


# ──────────────────────────────────────────────────────────────────────
# 6. capture_contract: insufficient ejecución ⇒ cap truth; suficiente no
# ──────────────────────────────────────────────────────────────────────


def test_capture_incomplete_insufficient_caps_strong_truth() -> None:
    """Incompleto en sentido strict Y sin señal ``sufficient_for_execution``."""
    bbox = {"left": 100, "top": 100, "width": 80, "height": 30}
    base_meta = {
        "uia": {
            "name": "Iniciar sesión",
            "control_type": "ButtonControl",
            "automation_id": "loginBtn",
            "bbox": bbox,
        },
        "vision_data": {"anchor_bbox": bbox},
    }
    ev_ok = _click(
        x=140, y=115,
        title="Login - App", proc="app.exe", metadata=dict(base_meta),
    )
    assert evaluate_event(ev_ok).evidence_level == "strong"

    incomplete_meta = dict(base_meta)
    incomplete_meta["capture_contract"] = {
        "has_pre_snapshot": False,
        "has_post_state": False,
        "has_uia": True,
        "has_parent_chain": False,
        "has_ocr_or_text": True,
        "missing": ["pre_snapshot", "post_state", "parent_chain"],
        "capture_complete": False,
        "sufficient_for_execution": False,
    }
    ev_bad = _click(
        x=140, y=115,
        title="Login - App", proc="app.exe", metadata=incomplete_meta,
    )
    truth_bad = evaluate_event(ev_bad)
    assert truth_bad.evidence_level == "medium"
    assert any(
        r.startswith("capture_contract.incomplete:cap_to_medium")
        for r in (truth_bad.classification.get("reasons") or [])
    )


def test_capture_incomplete_but_sufficient_preserves_strong_truth() -> None:
    """Misma evidencia incompleta (strict), pero ejecutable ⇒ NO cap."""
    bbox = {"left": 100, "top": 100, "width": 80, "height": 30}
    base_meta = {
        "uia": {
            "name": "Iniciar sesión",
            "control_type": "ButtonControl",
            "automation_id": "loginBtn",
            "bbox": bbox,
        },
        "vision_data": {"anchor_bbox": bbox},
    }
    incomplete_meta = dict(base_meta)
    incomplete_meta["capture_contract"] = {
        "has_pre_snapshot": False,
        "has_post_state": False,
        "has_uia": True,
        "has_parent_chain": False,
        "has_ocr_or_text": True,
        "missing": ["pre_snapshot", "post_state", "parent_chain"],
        "capture_complete": False,
        "sufficient_for_execution": True,
        "stable_automation_id": True,
    }
    ev = _click(
        x=140, y=115,
        title="Login - App", proc="app.exe", metadata=incomplete_meta,
    )
    assert evaluate_event(ev).evidence_level == "strong"


# ──────────────────────────────────────────────────────────────────────
# 7. Click sobre card legible ⇒ contract completo
# ──────────────────────────────────────────────────────────────────────


def test_profile_card_click_capture_contract_has_visual_inputs() -> None:
    """Cuando llegan pre_snapshot + OCR + parent_chain + post_state,
    ``capture_complete=True``. Esto valida la regla general
    para cualquier card (Chrome, SAP, app interna, Excel, ...).
    """
    pre = PreActionSnapshot(
        full_path="/tmp/pre.png", captured_at_ms=1000, age_ms=120,
    )
    post = PostActionState(
        captured_at_ms=2200, latency_ms=400,
        window_hwnd=42, window_title="Dashboard",
        window_process="app.exe", url="",
        snapshot_full=None, detected_change=True,
    )
    parent_chain = {
        "ancestors": [{"name": "Card", "control_type": "GroupControl"}],
        "siblings_near_click": [
            {"name": "Personal", "control_type": "TextControl"},
            {"name": "Maria Lopez", "control_type": "TextControl"},
        ],
    }
    ocr = {
        "ocr_lines": ["Personal", "Maria Lopez"],
        "ocr_text_around": "Personal\nMaria Lopez",
        "ocr_region_bbox": {"left": 50, "top": 50, "width": 240, "height": 240},
        "ocr_source": "pre_action_expanded_region",
    }
    uia = {
        "name": "",
        "control_type": "GroupControl",
        "automation_id": "card-1",  # automation_id rescata has_uia
        "bbox": {"left": 100, "top": 100, "width": 240, "height": 240},
    }
    contract = build_capture_contract(
        pre_snapshot=pre, post_state=post, uia=uia,
        parent_chain=parent_chain, ocr=ocr,
    )
    assert contract["capture_complete"] is True
    assert contract["missing"] == []

    # Y apply_capture_contract_to_metadata escribe los keys que los
    # consumers buscan (vision_data.ocr_lines, uia.parent_chain).
    meta: Dict[str, Any] = {"uia": dict(uia)}
    apply_capture_contract_to_metadata(
        meta, pre_snapshot=pre, parent_chain=parent_chain, ocr=ocr,
    )
    assert meta["pre_action_snapshot_full"] == "/tmp/pre.png"
    assert meta["vision_data"]["ocr_lines"] == ["Personal", "Maria Lopez"]
    assert meta["vision_data"]["ocr_text_around"] == "Personal\nMaria Lopez"
    assert meta["uia"]["parent_chain"]["siblings_near_click"][0]["name"] \
        == "Personal"


# ──────────────────────────────────────────────────────────────────────
# 8. El diagnóstico exporta la lista de missing
# ──────────────────────────────────────────────────────────────────────


def test_diagnose_capture_contract_reports_missing_inputs() -> None:
    """El contract debe ser JSON-serializable y enumerar los keys
    faltantes. Esto cubre la promesa: "el sistema reporta qué le
    falta para que el siguiente recording pueda corregirlo".
    """
    import json

    contract = build_capture_contract(
        pre_snapshot=None,
        post_state=None,
        uia=None,
        parent_chain=None,
        ocr=None,
    )
    # Roundtrip a JSON sin perder info.
    s = json.dumps(contract)
    rt = json.loads(s)
    assert rt["capture_complete"] is False
    assert "pre_snapshot" in rt["missing"]
    assert "ocr_or_text" in rt["missing"]
    # Y el keys son justo los que el contract espone — útil para UI.
    expected_keys = {
        "has_pre_snapshot", "has_post_state", "has_uia",
        "has_parent_chain", "has_ocr_or_text",
        "collateral_execution", "stable_automation_id",
        "missing", "capture_complete",
        "sufficient_for_execution", "execution_missing",
    }
    assert expected_keys.issubset(set(rt.keys()))
