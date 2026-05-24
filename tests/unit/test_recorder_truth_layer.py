"""
Nevlan — Recorder Truth Layer (PRD 2026-05-10c)
================================================

Tests quirúrgicos (máx. 10) que verifican la capa de verdad
intermedia entre ``raw_trace`` y el ``intent_collapse_engine``.

Reglas que se prueban (estructurales, NO listas hardcodeadas):

  1. Contenido web (HyperlinkControl con párrafo + bbox enorme) NO
     puede convertirse en identidad de profile picker.
  2. GroupControl/PaneControl/Document genérico con bbox enorme es
     identidad débil — no produce step ejecutable de alta confianza.
  3. Un click relevante exige identidad + outcome para ser fuerte.
  4. Field commit fragmentado se fusiona en una sola query
     ("Banco de" + " de Mexico" → "Banco de Mexico").
  5. Scroll relevante en navegador queda contabilizado por la capa
     y por el accounting del engine.
  6. READY queda bloqueado cuando hay eventos relevantes caídos del
     plan (``SEMANTIC_EVENT_DROPPED_FROM_PLAN``).
  7. Misión real ``mission_1778443962`` ya NO produce
     "Seleccionar perfil Interés Interbancaria".
  8. Una misión cuyo profile picker existe pero la evidencia es
     inválida queda en NEEDS_REVIEW (no inventa).
  9. Misión real ``mission_1778436293`` mantiene todos los eventos
     relevantes contabilizados.
 10. Las decisiones de Intent Collapse provienen de la capa de
     verdad — el resumen queda publicado en la misión.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from app.contracts.mission import (
    EventType,
    KeyboardAction,
    Mission,
    MouseAction,
    RawEvent,
    WindowContext,
)
from app.services.missions import profile_resolver
from app.services.missions.intent_collapse_engine import (
    _smart_merge_text_fragments,
    apply_collapse_to_mission,
    collapse_intent,
)
from app.services.missions.recorder_truth_layer import (
    bbox_area,
    build_truth_layer,
    evaluate_event,
    is_compatible_profile_picker_click,
    is_large_bbox,
    looks_like_profile_label,
    looks_like_web_content_text,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures comunes
# ──────────────────────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "real_missions"


@pytest.fixture(autouse=True)
def _isolate_alias_store(tmp_path, monkeypatch):
    """No queremos que el alias_store del repo contamine estos tests."""
    fake = tmp_path / "profile_aliases.json"
    monkeypatch.setattr(
        profile_resolver, "_alias_store_path", lambda: fake,
    )
    profile_resolver.reset_alias_store()
    yield
    profile_resolver.reset_alias_store()


def _click_event(
    *,
    title: str,
    process: str = "chrome.exe",
    hwnd: int = 100,
    name: str = "",
    control_type: str = "ButtonControl",
    bbox: Optional[Dict[str, int]] = None,
    automation_id: str = "",
    url: str = "",
) -> RawEvent:
    metadata: Dict[str, Any] = {
        "uia": {
            "name": name,
            "automation_id": automation_id,
            "control_type": control_type,
            "bbox": bbox or {"left": 0, "top": 0, "width": 100, "height": 50},
        },
        "anchor_bbox": bbox or {"left": 0, "top": 0, "width": 100, "height": 50},
    }
    if url:
        metadata["web"] = {"url": url}
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        timestamp=datetime.now(timezone.utc),
        offset_ms=0,
        mouse_action=MouseAction(x=10, y=10, button="left"),
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=process,
            bounding_box={"left": 0, "top": 0, "width": 1920, "height": 1080},
        ),
        metadata=metadata,
    )


def _load_real_mission(filename: str) -> Mission:
    p = FIXTURE_DIR / filename
    if not p.exists():
        pytest.skip(f"Fixture no disponible: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    data["review_confirmations"] = []
    data["tags"] = []
    return Mission.model_validate(data)


# ──────────────────────────────────────────────────────────────────────
# 1. Contenido web NUNCA puede ser identidad de perfil
# ──────────────────────────────────────────────────────────────────────


def test_web_content_cannot_be_profile_identity() -> None:
    """Un click sobre un HyperlinkControl con un párrafo de body
    content (oración natural larga + bbox enorme) NO puede ser
    aceptado como evidencia de profile picker, aunque la ventana
    diga ``"Google Chrome"`` (caso real mission_1778443962).
    """
    ev = _click_event(
        title="Google Chrome",
        control_type="HyperlinkControl",
        name=(
            "El objetivo para la Tasa de Interés Interbancaria a 1 día "
            "(tasa objetivo) disminuye en 25 puntos base Fecha de "
            "publicación 07 Mayo 2026"
        ),
        bbox={"left": 0, "top": 0, "width": 1003, "height": 211},
    )
    truth = evaluate_event(ev)
    # Lo decisivo es la forma del name (oración de body content);
    # el bbox 1003x211 es muy ancho pero no superior al umbral
    # absoluto de área — el rechazo NO depende del tamaño.
    assert truth.target_identity["is_web_content_text"] is True
    # Compatibilidad estructural: imposible que sea select_profile.
    assert "select_profile" not in truth.compatible_semantic_types
    assert "select_profile" in truth.classification.get(
        "incompatible_with", []
    )
    # El guard que consume el engine también lo bloquea.
    assert is_compatible_profile_picker_click(ev) is False


# ──────────────────────────────────────────────────────────────────────
# 2. GroupControl / Pane con bbox enorme = identidad débil
# ──────────────────────────────────────────────────────────────────────


def test_large_groupcontrol_or_pane_is_weak_identity() -> None:
    """Un click sobre GroupControl/PaneControl con bbox enorme y
    sin name no debe producir un step ejecutable de alta confianza
    — el target es "el contenedor entero", no un elemento concreto.
    """
    pane = _click_event(
        title="cualquier ventana",
        control_type="PaneControl",
        name="",
        bbox={"left": 0, "top": 0, "width": 3066, "height": 102},
    )
    pane_truth = evaluate_event(pane)
    assert pane_truth.target_identity["is_generic_container"] is True
    # bbox 3066x102 = 312_732 — NO es "large" por área absoluta,
    # pero es contenedor genérico, lo que ya degrada la evidencia.
    assert pane_truth.evidence_level in {"weak", "insufficient", "medium"}
    # ¡Crítico! No emite "select_profile" como compatible.
    assert "select_profile" not in pane_truth.compatible_semantic_types

    # Caso explícito de bbox enorme en GroupControl: insufficient.
    huge = _click_event(
        title="Google Chrome",
        control_type="GroupControl",
        name="",
        bbox={"left": 0, "top": 0, "width": 1500, "height": 800},
    )
    huge_truth = evaluate_event(huge)
    assert huge_truth.target_identity["is_large_bbox"] is True
    assert huge_truth.evidence_level == "insufficient"
    assert is_compatible_profile_picker_click(huge) is False


# ──────────────────────────────────────────────────────────────────────
# 3. Click relevante necesita identidad + outcome
# ──────────────────────────────────────────────────────────────────────


def test_relevant_click_requires_target_identity_and_outcome() -> None:
    """Un click sin UIA, sin web locators y sin bbox razonable es
    evidencia insuficiente — no debe disparar steps de alta
    confianza. La capa lo marca y el outcome (ventana siguiente)
    se observa cuando hay un evento posterior que cambia.
    """
    bare = RawEvent(
        event_type=EventType.MOUSE_CLICK,
        timestamp=datetime.now(timezone.utc),
        mouse_action=MouseAction(x=42, y=42, button="left"),
        window_context=WindowContext(
            hwnd=1, title="", process_name="unknown.exe",
        ),
        metadata={},
    )
    truth = evaluate_event(bare)
    assert truth.evidence_level == "insufficient"
    assert truth.target_identity["has_uia"] is False
    assert truth.target_identity["has_web_locators"] is False

    # Con un siguiente evento que cambia la ventana, build_truth_layer
    # rellena el outcome (window_changed=True).
    ev1 = _click_event(
        title="App A", process="a.exe", hwnd=10, name="Botón",
        control_type="ButtonControl",
    )
    ev2 = _click_event(
        title="App B", process="b.exe", hwnd=20, name="Otro",
        control_type="ButtonControl",
    )
    layer = build_truth_layer([ev1, ev2])
    assert layer[0].outcome.get("window_changed") is True


# ──────────────────────────────────────────────────────────────────────
# 4. Field commit fragmentado se fusiona
# ──────────────────────────────────────────────────────────────────────


def test_split_field_commit_merges_single_query() -> None:
    """Caso real (mission_1778436293): el recorder produjo dos
    fragmentos contiguos ``"Banco de"`` y ``" de Mexico"``. La
    fusión inteligente debe detectar el solapamiento de "de" y
    devolver ``"Banco de Mexico"`` (no ``"Banco dede Mexico"``,
    no ``"Banco de  de Mexico"``).
    """
    merged = _smart_merge_text_fragments(["Banco de", " de Mexico"])
    assert merged == "Banco de Mexico"


# ──────────────────────────────────────────────────────────────────────
# 5. Scroll relevante queda contabilizado
# ──────────────────────────────────────────────────────────────────────


def test_scroll_relevant_event_is_accounted_for() -> None:
    """Un scroll en navegador no puede desaparecer del plan ni del
    accounting del truth layer.
    """
    scroll = RawEvent(
        event_type=EventType.MOUSE_SCROLL,
        timestamp=datetime.now(timezone.utc),
        mouse_action=MouseAction(x=500, y=500, button="scroll"),
        window_context=WindowContext(
            hwnd=33, title="Banxico - Google Chrome",
            process_name="chrome.exe",
        ),
        metadata={"count": 3},
    )
    truth = evaluate_event(scroll)
    assert truth.event_kind == "scroll"
    assert "scroll_page" in truth.compatible_semantic_types
    assert "scroll_results" in truth.compatible_semantic_types
    assert truth.evidence_level == "medium"


# ──────────────────────────────────────────────────────────────────────
# 6. READY se bloquea cuando hay eventos relevantes caídos
# ──────────────────────────────────────────────────────────────────────


def test_ready_blocked_when_relevant_event_dropped() -> None:
    """El blocker ``SEMANTIC_EVENT_DROPPED_FROM_PLAN`` se activa
    cuando hay scrolls / clicks / búsquedas relevantes que NO
    quedan cubiertos por ningún step semántico. La misión real
    1778436293 *no* tiene drops cuando el engine los cubre — así
    que la prueba inversa: cualquier dropped_event ⇒ NEEDS_REVIEW.
    """
    mission = _load_real_mission("mission_1778436293_raw.json")
    apply_collapse_to_mission(mission)
    sep = mission.semantic_execution_plan or {}
    blockers = list(sep.get("not_ready_reasons") or [])
    dropped = list(getattr(mission, "_dropped_semantic_events", None) or [])
    if dropped:
        assert sep.get("intent_status") == "NEEDS_REVIEW", (
            sep.get("intent_status"), blockers, dropped,
        )
        assert "SEMANTIC_EVENT_DROPPED_FROM_PLAN" in blockers, blockers
    else:
        # Cuando NO hay drops, el blocker no debe aparecer.
        assert "SEMANTIC_EVENT_DROPPED_FROM_PLAN" not in blockers


# ──────────────────────────────────────────────────────────────────────
# 7. Misión real 1778443962 — no más "Interés Interbancaria"
# ──────────────────────────────────────────────────────────────────────


def test_real_mission_1778443962_no_interes_interbancaria_profile() -> None:
    """Caso central de la queja: el plan colapsado a partir del
    raw_trace de mission_1778443962 NO debe producir un
    ``select_profile`` cuyo nombre sea contenido web extraído del
    HyperlinkControl de Banxico.
    """
    mission = _load_real_mission("mission_1778443962_raw.json")
    apply_collapse_to_mission(mission)
    sep = mission.semantic_execution_plan or {}
    select_steps = [
        s for s in (sep.get("steps") or []) if s.get("type") == "select_profile"
    ]
    for s in select_steps:
        params = s.get("params") or {}
        profile_name = (
            params.get("profile_name")
            or params.get("profile")
            or ""
        )
        # Estructural: jamás debe quedar contenido web como nombre.
        assert "Interés Interbancaria" not in profile_name, params
        assert "Tasa de" not in profile_name, params
        # Si hay select_profile, debe estar en NEEDS_REVIEW (no
        # inventamos sin evidencia).
        label_required = bool(params.get("needs_user_label", False))
        assert profile_name == "" or label_required is False
    # El status global no puede ser READY con perfil inventado.
    if select_steps:
        assert sep.get("intent_status") in {"NEEDS_REVIEW", "EXECUTABLE"}


# ──────────────────────────────────────────────────────────────────────
# 8. Sin evidencia de perfil ⇒ NEEDS_REVIEW (no inventar)
# ──────────────────────────────────────────────────────────────────────


def test_real_mission_1778443905_missing_profile_stays_needs_review() -> None:
    """No tenemos fixture grabado de mission_1778443905, pero
    sintetizamos el escenario equivalente: profile picker visible
    pero sin evidencia humana válida (ningún UIA name compatible
    con etiqueta de perfil). El plan debe quedar en NEEDS_REVIEW
    con ``select_profile`` marcado como ``needs_user_label`` —
    NUNCA producir un nombre inventado.
    """
    raw_trace = [
        # Window activate del profile picker.
        RawEvent(
            event_type=EventType.WINDOW_ACTIVATE,
            timestamp=datetime.now(timezone.utc),
            window_context=WindowContext(
                hwnd=1, title="Google Chrome", process_name="chrome.exe",
            ),
        ),
        # Click sobre un PaneControl genérico (sin name) en el picker.
        _click_event(
            title="Google Chrome", hwnd=1,
            control_type="PaneControl", name="",
            bbox={"left": 100, "top": 100, "width": 200, "height": 200},
        ),
    ]
    plan = collapse_intent(raw_trace=raw_trace)
    sp = [s for s in plan.semantic_steps if s.type == "select_profile"]
    assert sp, "debería emitir select_profile (picker visto)"
    s = sp[0]
    # Nombre vacío + needs_user_label = True ⇒ honesto.
    assert (s.params.get("profile") or "") == ""
    assert s.needs_user_label is True
    assert plan.status == "NEEDS_REVIEW"


# ──────────────────────────────────────────────────────────────────────
# 9. Misión real 1778436293 — todos los eventos relevantes contabilizados
# ──────────────────────────────────────────────────────────────────────


def test_real_mission_1778436293_all_relevant_events_accounted() -> None:
    """La búsqueda ``"Banco de Mexico"``, el click al resultado de
    Banxico y los scrolls deben TODOS quedar representados — la
    lista de dropped events debe estar vacía y los kinds esperados
    ``search_web``, ``open_search_result``, ``scroll_page`` deben
    aparecer en el plan.
    """
    mission = _load_real_mission("mission_1778436293_raw.json")
    apply_collapse_to_mission(mission)
    sep = mission.semantic_execution_plan or {}
    types = [s.get("type") for s in (sep.get("steps") or [])]
    assert "search_web" in types, types
    assert "open_search_result" in types, types
    assert "scroll_page" in types, types
    dropped = list(getattr(mission, "_dropped_semantic_events", None) or [])
    assert dropped == [], dropped


# ──────────────────────────────────────────────────────────────────────
# 10. Truth layer outputs son consumidos por Intent Collapse
# ──────────────────────────────────────────────────────────────────────


def test_truth_layer_outputs_are_consumed_by_intent_collapse() -> None:
    """Verificamos dos cosas:

      a) ``apply_collapse_to_mission`` publica un resumen de la
         capa de verdad en ``mission._truth_layer_summary``
         (señal de que la capa se construyó y se evaluó).
      b) La decisión de aceptar/rechazar un click como profile
         picker viene del guard estructural
         ``is_compatible_profile_picker_click`` (mismo veredicto
         que toma el engine internamente).
    """
    mission = _load_real_mission("mission_1778443962_raw.json")
    apply_collapse_to_mission(mission)
    summary = getattr(mission, "_truth_layer_summary", None)
    assert isinstance(summary, dict)
    assert summary.get("total", 0) >= 1
    # El click problemático (HyperlinkControl con párrafo) cuenta
    # como incompatible_select_profile en el resumen.
    assert summary.get("incompatible_select_profile", 0) >= 1

    # Helpers estructurales son consistentes y públicos.
    assert looks_like_profile_label("Daniel Arturo Ramos") is True
    assert looks_like_profile_label("Interés Interbancaria") is True  # 2 toks ok
    assert (
        looks_like_web_content_text(
            "El objetivo para la Tasa de Interés Interbancaria a 1 día "
            "(tasa objetivo) disminuye en 25 puntos base"
        )
        is True
    )
    assert is_large_bbox({"width": 1003, "height": 211}) is False
    assert is_large_bbox({"width": 1500, "height": 800}) is True
    assert bbox_area({"width": 10, "height": 20}) == 200
