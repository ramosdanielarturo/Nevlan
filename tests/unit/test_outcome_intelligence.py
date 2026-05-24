"""
Nevlan — Outcome Intelligence (PRD 2026-05-10g)
================================================

Tests quirúrgicos (máx. 10) para la capability sistémica que enseña
a Nevlan a contestar:

    "¿qué cambió en el sistema después de la acción?"

Reglas que se prueban (estructurales, **sin hardcode** de Chrome /
Daniel / select_profile):

  1. Click en card del picker SIN transición real ⇒ ``select_profile``
     queda con ``needs_user_label=True``: bloqueo honesto.
  2. Texto visual cercano + bbox sin outcome ⇒ click NUNCA es
     ``strong``. Visual rescue solo: máximo ``medium``.
  3. Transición real (window/title/url cambian) ⇒ visual rescue se
     promueve a ``strong``.
  4. Misión sin outcome real preserva la regla "needs_review".
  5. Misión real ``mission_1778451248`` (raw_trace persistido en
     disco con cambios reales de title) usa el ``after_state`` del
     outcome — no inventa datos.
  6. Click sobre celda Excel (control_type DataItem) NO requiere
     transición de ventana — el reader sigue siendo agnóstico de app.
  7. La identidad visual generaliza entre apps con la misma forma.
  8. No hay reglas hardcoded de Chrome / Daniel / select_profile en
     ``outcome_intelligence`` ni ``screen_element_reader``.
  9. Click sobre target ambiguo (sin texto, sin bbox útil) queda en
     ``insufficient`` — bloqueo honesto.
 10. ``apply_outcome_to_classification`` exige outcome incluso cuando
     el classifier puso ``strong`` por visual rescue: sin outcome
     baja a ``medium``.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.contracts.mission import (
    EventType,
    Mission,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions import profile_resolver
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.outcome_intelligence import (
    OutcomeObservation,
    apply_outcome_to_classification,
    compute_outcome_for,
    enrich_truth_layer_with_outcomes,
    outcome_confirms_action,
)
from app.services.missions.recorder_truth_layer import (
    build_truth_layer,
    evaluate_event,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MISSION_PATH = (
    REPO_ROOT / "var" / "missions"
    / "cd87af59-6a2a-42c4-92a1-94dd2c03fc56.json"
)


@pytest.fixture(autouse=True)
def _isolate_alias_store(tmp_path, monkeypatch):
    fake = tmp_path / "profile_aliases.json"
    monkeypatch.setattr(
        profile_resolver, "_alias_store_path", lambda: fake,
    )
    profile_resolver.reset_alias_store()
    yield
    profile_resolver.reset_alias_store()


def _t(ms: int) -> datetime:
    base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(milliseconds=ms)


def _click(
    *,
    x: int,
    y: int,
    title: str,
    proc: str = "chrome.exe",
    hwnd: int = 100,
    metadata: Dict[str, Any] | None = None,
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


def _window(
    *,
    title: str,
    proc: str = "chrome.exe",
    hwnd: int = 100,
    offset_ms: int = 0,
) -> RawEvent:
    return RawEvent(
        event_type=EventType.WINDOW_ACTIVATE,
        timestamp=_t(offset_ms),
        offset_ms=offset_ms,
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
    )


def _card_metadata(
    *,
    bbox: Dict[str, int],
    primary: str,
    secondary: str = "",
    control_type: str = "GroupControl",
    name: str = "",
) -> Dict[str, Any]:
    ocr = "\n".join(s for s in (primary, secondary) if s)
    return {
        "desktop_uia": {
            "name": name,
            "control_type": control_type,
            "bbox": bbox,
        },
        "vision_data": {
            "anchor_bbox": bbox,
            "ocr_text_around": ocr,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# 1. Click sin outcome ⇒ needs_user_label
# ──────────────────────────────────────────────────────────────────────


def test_profile_click_requires_real_outcome_change() -> None:
    """Picker click + visual reader OK pero la ventana NO cambió tras el
    click ⇒ ``select_profile`` queda con ``needs_user_label=True``.
    Bloqueo honesto.
    """
    raw_trace = [
        _window(
            title="¿Quién eres? - Google Chrome",
            proc="chrome.exe", hwnd=11, offset_ms=0,
        ),
        _click(
            x=720, y=420,
            title="¿Quién eres? - Google Chrome",
            proc="chrome.exe", hwnd=11, offset_ms=200,
            metadata=_card_metadata(
                bbox={"x": 600, "y": 300, "width": 300, "height": 280},
                primary="Personal",
                secondary="Maria Lopez",
            ),
        ),
        # Sigue exactamente el mismo título — picker no se cerró.
        _window(
            title="¿Quién eres? - Google Chrome",
            proc="chrome.exe", hwnd=11, offset_ms=600,
        ),
    ]
    mission = Mission.model_validate({
        "id": "no-outcome-1",
        "name": "no outcome",
        "raw_trace": [e.model_dump() for e in raw_trace],
        "interpreted_steps": [],
        "compiled_execution_graph": [],
        "action_groups": [],
        "status": "compiled",
    })
    apply_collapse_to_mission(mission)
    sep = mission.semantic_execution_plan or {}
    sps = [s for s in (sep.get("steps") or [])
           if s.get("type") == "select_profile"]
    assert sps, "no se generó select_profile"
    sp = sps[0]
    # Sin transición observada, el step es confirmable, no fuerte.
    # Señales objetivas: needs_user_label=True y confidence < 0.85
    # (la rama "outcome not confirmed" baja a 0.78).
    assert bool(sp.get("needs_user_label")) is True
    assert float(sp.get("confidence") or 1.0) < 0.85


# ──────────────────────────────────────────────────────────────────────
# 2. Texto visual sin outcome ⇒ no strong
# ──────────────────────────────────────────────────────────────────────


def test_visual_text_alone_not_enough_for_strong_action() -> None:
    """Click con visual reader que recupera primary + secondary +
    click_inside_bbox, pero outcome NO observable. La regla
    apply_outcome_to_classification capa a "medium".
    """
    bbox = {"x": 100, "y": 100, "width": 250, "height": 250}
    ev = _click(
        x=200, y=200,
        title="Some App", proc="some_app.exe",
        metadata=_card_metadata(
            bbox=bbox,
            primary="Personal",
            secondary="Maria Lopez",
        ),
    )
    truth = evaluate_event(ev)
    # El primer pase ya capa visual rescue en "medium".
    assert truth.evidence_level in ("medium", "weak")
    # Aplicamos outcome vacío explícito → siguen "medium" como máximo.
    obs = OutcomeObservation()  # has_signal=False por default
    new_class = apply_outcome_to_classification(
        truth.classification, truth.target_identity, obs,
        intent_kind="selection_or_open",
    )
    assert new_class.get("evidence_level") in ("medium", "weak")
    # Y nunca strong.
    assert new_class.get("evidence_level") != "strong"


# ──────────────────────────────────────────────────────────────────────
# 3. Transición real ⇒ visual rescue sube a strong
# ──────────────────────────────────────────────────────────────────────


def test_window_transition_upgrades_evidence_strength() -> None:
    """Click con visual reader OK + cambio real de window/title después
    ⇒ ``apply_outcome_to_classification`` lo sube a "strong".
    """
    bbox = {"x": 100, "y": 100, "width": 250, "height": 250}
    raw_trace = [
        _click(
            x=200, y=200,
            title="My App — Login", proc="my_app.exe", hwnd=33,
            metadata=_card_metadata(
                bbox=bbox,
                primary="Personal",
                secondary="Maria Lopez",
            ),
        ),
        _window(
            title="My App — Dashboard",  # Cambió de Login a Dashboard.
            proc="my_app.exe", hwnd=44, offset_ms=400,
        ),
    ]
    truth_events = build_truth_layer(raw_trace)
    click_truth = truth_events[0]
    # build_truth_layer ya aplicó outcomes via enrich_truth_layer_with_outcomes.
    assert click_truth.evidence_level == "strong"
    assert click_truth.outcome.get("has_signal") is True
    assert click_truth.outcome.get("title_changed") is True
    assert "outcome.confirmed:selection_or_open" in (
        click_truth.classification.get("reasons") or []
    )


# ──────────────────────────────────────────────────────────────────────
# 4. Sin outcome ⇒ needs_review preservado
# ──────────────────────────────────────────────────────────────────────


def test_failed_outcome_keeps_step_needs_review() -> None:
    """End-to-end: misión sin outcome ⇒ select_profile queda
    ``needs_user_label`` (bloqueo honesto).
    """
    bbox = {"x": 100, "y": 100, "width": 250, "height": 250}
    raw_trace = [
        _window(
            title="¿Quién eres? - Google Chrome",
            proc="chrome.exe", hwnd=11, offset_ms=0,
        ),
        _click(
            x=200, y=200,
            title="¿Quién eres? - Google Chrome",
            proc="chrome.exe", hwnd=11, offset_ms=300,
            metadata=_card_metadata(
                bbox=bbox,
                primary="Personal",
                secondary="Maria Lopez",
            ),
        ),
        # No hay transición posterior.
    ]
    mission = Mission.model_validate({
        "id": "no-trans-1", "name": "no transition",
        "raw_trace": [e.model_dump() for e in raw_trace],
        "interpreted_steps": [], "compiled_execution_graph": [],
        "action_groups": [], "status": "compiled",
    })
    apply_collapse_to_mission(mission)
    sep = mission.semantic_execution_plan or {}
    sps = [s for s in (sep.get("steps") or [])
           if s.get("type") == "select_profile"]
    assert sps, "no select_profile generado"
    assert any(bool(s.get("needs_user_label")) for s in sps)


# ──────────────────────────────────────────────────────────────────────
# 5. Misión real (raw_trace persistido) ⇒ usa after_state real
# ──────────────────────────────────────────────────────────────────────


def test_real_profile_picker_runtime_flow_uses_after_state() -> None:
    """Carga ``mission_1778451248`` desde disco — raw_trace REAL,
    sin mocks. El recorder en producción a veces no persiste OCR /
    UIA enriquecido (este JSON solo trae window_context.title), así
    que outcome se valida usando lo que SÍ sobrevive: las
    transiciones de ``title``.

    Para los clicks que SÍ tienen una transición real de title en
    el siguiente evento, ``compute_outcome_for`` debe reportar
    ``has_signal=True`` y ``title_changed=True``. Esto demuestra
    que la pipeline consume el ``after_state`` real, no datos
    sintéticos.
    """
    if not REAL_MISSION_PATH.exists():
        pytest.skip(f"Misión real no disponible: {REAL_MISSION_PATH}")
    data = json.loads(REAL_MISSION_PATH.read_text(encoding="utf-8"))
    mission = Mission.model_validate(copy.deepcopy(data))
    raw = list(mission.raw_trace or [])
    assert raw, "fixture sin raw_trace"

    # Detectamos al menos una transición real entre títulos.
    transitions: List[int] = []
    for i in range(len(raw) - 1):
        a = (raw[i].window_context.title or "")
        b = (raw[i + 1].window_context.title or "")
        if a and b and a != b:
            transitions.append(i)
    assert transitions, (
        "fixture inesperado: no hay cambios de title entre eventos"
    )

    # Para cada transición real, outcome detecta el cambio.
    for idx in transitions:
        obs = compute_outcome_for(idx, raw)
        assert obs.has_signal is True, f"sin signal en idx {idx}"
        # Al menos uno de window/title/url cambió — exactamente el
        # tipo de evidencia que necesitamos para "selection_or_open".
        assert obs.title_changed or obs.window_changed or obs.url_changed
        assert outcome_confirms_action(obs, kind="selection_or_open") is True


# ──────────────────────────────────────────────────────────────────────
# 6. Click sobre celda Excel — no requiere transición de ventana
# ──────────────────────────────────────────────────────────────────────


def test_excel_cell_click_not_app_specific() -> None:
    """Click en una celda (control_type=DataItem) en cualquier app
    NO requiere transición de ventana. El visual reader la clasifica
    como "cell" y el outcome chequeable es ``focus_changed`` o
    ``new_content_visible``. Si solo cambió el foco UIA, el outcome
    confirma "input_focus"/"editing".
    """
    raw_trace = [
        # Click en una celda — control_type DataItem.
        _click(
            x=300, y=200,
            title="Book1 - Excel", proc="excel.exe", hwnd=55,
            metadata={
                "desktop_uia": {
                    "name": "B7",
                    "control_type": "DataItem",
                    "bbox": {"x": 290, "y": 190, "width": 80, "height": 25},
                    "automation_id": "Excel.Cell.B7",
                },
                "vision_data": {
                    "anchor_bbox": {
                        "x": 290, "y": 190, "width": 80, "height": 25,
                    },
                    "ocr_text_around": "B7",
                },
            },
        ),
        # Cambio de foco UIA (otra celda activa) — sin cambio de ventana.
        _click(
            x=400, y=200,
            title="Book1 - Excel", proc="excel.exe", hwnd=55,
            offset_ms=600,
            metadata={
                "desktop_uia": {
                    "name": "C7",
                    "control_type": "DataItem",
                    "bbox": {"x": 380, "y": 190, "width": 80, "height": 25},
                    "automation_id": "Excel.Cell.C7",
                },
                "vision_data": {
                    "anchor_bbox": {
                        "x": 380, "y": 190, "width": 80, "height": 25,
                    },
                    "ocr_text_around": "C7",
                },
            },
        ),
    ]
    truth = build_truth_layer(raw_trace)
    cell_truth = truth[0]
    # Tiene identity estructural (automation_id) — eso es ya "fuerte".
    assert cell_truth.evidence_level in ("strong", "medium")
    # El outcome registró el cambio de focus (la firma del UIA cambió).
    obs = compute_outcome_for(0, raw_trace)
    assert obs.has_signal is True
    assert obs.focus_changed is True
    # Y para "input_focus"/"editing" el outcome confirma.
    assert outcome_confirms_action(obs, kind="input_focus") is True
    assert outcome_confirms_action(obs, kind="editing") is True
    # Pero NO confirma "selection_or_open" — no cambió la ventana.
    assert outcome_confirms_action(obs, kind="selection_or_open") is False


# ──────────────────────────────────────────────────────────────────────
# 7. Identidad visual generaliza entre apps con la misma forma
# ──────────────────────────────────────────────────────────────────────


def test_cross_app_visual_identity_generalizes() -> None:
    """Misma card + misma transición observada en apps distintas
    (Slack / SAP / Excel / app interna) producen el mismo veredicto
    de outcome_confirms_action.
    """
    bbox = {"x": 100, "y": 100, "width": 240, "height": 220}
    apps = [
        ("slack.exe", "Slack", "Slack — Channel"),
        ("sap.exe", "SAP Logon Pad", "SAP — Easy Access"),
        ("excel.exe", "Book1 - Excel", "Sheet2 - Excel"),
        ("acme.exe", "Acme Internal", "Acme Internal — Home"),
    ]
    for proc, before, after in apps:
        raw_trace = [
            _click(
                x=200, y=200,
                title=before, proc=proc, hwnd=200,
                metadata=_card_metadata(
                    bbox=bbox,
                    primary="Personal",
                    secondary="Maria Lopez",
                ),
            ),
            _window(title=after, proc=proc, hwnd=201, offset_ms=400),
        ]
        truth = build_truth_layer(raw_trace)
        click_truth = truth[0]
        # Misma forma ⇒ mismo veredicto fuerte.
        assert click_truth.evidence_level == "strong", (
            f"{proc} no llegó a strong tras outcome"
        )
        obs = compute_outcome_for(0, raw_trace)
        assert outcome_confirms_action(obs, kind="selection_or_open") is True


# ──────────────────────────────────────────────────────────────────────
# 8. No reglas hardcoded de Chrome / Daniel / select_profile
# ──────────────────────────────────────────────────────────────────────


def test_no_hardcoded_chrome_rules() -> None:
    """Code-level guard: las reglas de outcome y de identidad visual
    NO deben contener literales de apps, navegadores, sitios o
    nombres propios. Forbidden:

      * Apps / navegadores: ``chrome``, ``edge``, ``msedge``,
        ``firefox``, ``chromium``.
      * Nombres propios / sitios concretos: ``Daniel``, ``Banxico``,
        ``YouTube``.

    Permitidos: nombres genéricos de tipos semánticos
    (``select_profile``, ``open_url``, ...) — son vocabulario
    transversal, no una rama por app.

    Las menciones dentro de docstrings (triple-quoted) se ignoran:
    explican qué NO debemos hardcodear, no implementan reglas.
    """
    repo = Path(__file__).resolve().parents[2]
    forbidden_words = (
        "chrome", "Chrome",
        "msedge", "edge", "Edge",
        "firefox", "Firefox",
        "chromium", "Chromium",
        "Daniel", "daniel",
        "Banxico", "banxico",
        "YouTube", "youtube",
    )
    for module in (
        "app/services/missions/outcome_intelligence.py",
        "app/services/missions/screen_element_reader.py",
    ):
        src = (repo / module).read_text(encoding="utf-8")
        in_doc = False
        for raw_line in src.splitlines():
            line = raw_line
            triple = line.count('"""') + line.count("'''")
            stripped = line.strip()
            # Salta líneas dentro de docstring multiline.
            if in_doc:
                if triple % 2 == 1:
                    in_doc = False
                continue
            if triple % 2 == 1:
                in_doc = True
                continue
            if triple >= 2:
                # Docstring en una sola línea: salta.
                continue
            if stripped.startswith("#"):
                continue
            for word in forbidden_words:
                if word in line:
                    # Comentario inline a la derecha: ignorar lo
                    # que va después de "#".
                    code_part = line.split("#", 1)[0]
                    if word not in code_part:
                        continue
                    pytest.fail(
                        f"{module} contiene la palabra prohibida "
                        f"{word!r} en código ejecutable: {stripped!r}"
                    )


# ──────────────────────────────────────────────────────────────────────
# 9. Visual ambiguo (sin texto, sin bbox útil) ⇒ insufficient
# ──────────────────────────────────────────────────────────────────────


def test_ambiguous_visual_target_remains_weak() -> None:
    """Click con UIA vacío + OCR vacío + bbox vacío ⇒ el reader
    queda en ``insufficient``. Aunque haya outcome posterior, el
    classifier no inventa identidad.
    """
    raw_trace = [
        _click(
            x=10, y=10,
            title="Some App", proc="some_app.exe", hwnd=77,
            metadata={
                "desktop_uia": {
                    "name": "",
                    "control_type": "GroupControl",
                    "bbox": {},
                },
                "vision_data": {
                    "anchor_bbox": {},
                    "ocr_text_around": "",
                },
            },
        ),
        _window(
            title="Different Page",
            proc="some_app.exe", hwnd=78, offset_ms=300,
        ),
    ]
    truth = build_truth_layer(raw_trace)
    click_truth = truth[0]
    # Sin identidad útil: la evidencia NO puede ser strong aunque haya
    # outcome posterior — ``apply_outcome`` solo sube si hay rescate
    # visual real o identidad estructural.
    assert click_truth.evidence_level in ("weak", "insufficient")
    assert click_truth.evidence_level != "strong"


# ──────────────────────────────────────────────────────────────────────
# 10. apply_outcome_to_classification respeta la regla "no outcome"
# ──────────────────────────────────────────────────────────────────────


def test_semantic_action_requires_outcome_confirmation() -> None:
    """Aunque el clasificador inicial reportara ``strong`` para un
    click sin identidad estructural, la pasada de outcome lo baja
    a ``medium`` cuando no hay observación posterior. Esto preserva
    la regla "lo que NO se puede demostrar, no se ejecuta".
    """
    # Construimos un classification "strong" artificial para validar
    # el cap por outcome.
    classification = {
        "evidence_level": "strong",
        "compatible_with": ["select_profile"],
        "reasons": ["uia.name", "visual.card_with_primary_text"],
    }
    target_identity = {
        # NO automation_id, NO web.locators ⇒ depende del rescate
        # visual.
        "automation_id": "",
        "has_web_locators": False,
        "visual": {
            "primary_text": "Personal",
            "click_inside_bbox": True,
        },
    }
    no_obs = OutcomeObservation()
    out = apply_outcome_to_classification(
        classification, target_identity, no_obs,
        intent_kind="selection_or_open",
    )
    assert out["evidence_level"] == "medium"
    assert "outcome.not_observed:cap_to_medium" in (out.get("reasons") or [])

    # Y con outcome confirmado, la misma config sube a strong.
    obs = OutcomeObservation(
        has_signal=True, title_changed=True, window_changed=True,
        title_before="A", title_after="B",
    )
    out_ok = apply_outcome_to_classification(
        classification, target_identity, obs,
        intent_kind="selection_or_open",
    )
    assert out_ok["evidence_level"] == "strong"
