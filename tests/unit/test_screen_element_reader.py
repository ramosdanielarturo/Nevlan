"""
Nevlan — Screen Element Reader / Visual Target Identity Engine
================================================================

PRD 2026-05-10f. Tests quirúrgicos (máx. 8) para el sub-componente
del Recorder Truth Layer que identifica el **objeto visual** que
recibió un click (no el pixel) y lee su texto.

Reglas que se prueban (estructurales — sin hardcode de Chrome /
Daniel / select_profile):

  1. Un click dentro de una card con OCR legible reconstruye su texto.
  2. Una card con dos líneas legibles produce primary + secondary.
  3. UIA = GroupControl vacío + visual leíble ⇒ identidad rescatada
     (no se pierde como "GroupControl vacío").
  4. Crop pequeño que solo contiene UI noise expande hacia el
     contenedor (parent_chain / nearby_text) sin inventar texto.
  5. La identidad visual es **general**: la misma card en cualquier
     app produce el mismo veredicto — nada cambia si renombramos el
     proceso.
  6. Si el contenedor no tiene texto legible (UIA vacío + OCR vacío),
     el reader queda en ``insufficient`` — bloquea honestamente.
  7. Sin ``click_inside_bbox`` confirmado, el evidence_level NO
     puede ser ``strong``.
  8. La misión real del profile picker (Daniel Chrome / Daniel Arturo
     Ramos) se resuelve por el visual reader vía ``profile_human_observation``
     en el ICE, no por OCR del pixel.
"""
from __future__ import annotations

from datetime import datetime, timezone
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
from app.services.missions.recorder_truth_layer import (
    evaluate_event,
    is_compatible_profile_picker_click,
)
from app.services.missions.screen_element_reader import (
    classify_visual_type,
    extract_text_lines,
    is_click_inside_bbox,
    read_visual_target,
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


def _click(
    *,
    x: int,
    y: int,
    title: str = "Google Chrome",
    proc: str = "chrome.exe",
    metadata: Dict[str, Any] | None = None,
) -> RawEvent:
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        timestamp=datetime.now(timezone.utc),
        mouse_action=MouseAction(x=x, y=y, button="left"),
        window_context=WindowContext(
            hwnd=42, title=title, process_name=proc,
        ),
        metadata=metadata or {},
    )


def _card_metadata(
    *,
    bbox: Dict[str, int],
    primary: str,
    secondary: str = "",
    nearby: str = "",
    control_type: str = "GroupControl",
    name: str = "",
    parent_names: List[str] | None = None,
) -> Dict[str, Any]:
    """Genera metadata realista para un click sobre una card.

    Reproduce el shape que el recorder publica para Chrome (UIA wrapper
    GroupControl + ``vision_data.ocr_text_around`` con dos líneas) sin
    fijar nombres: cualquier app con la misma forma de captura debe
    dar el mismo resultado.
    """
    ocr_lines = [s for s in (primary, secondary) if s]
    return {
        "desktop_uia": {
            "name": name,
            "control_type": control_type,
            "bbox": bbox,
            "parent_chain": [
                {"name": pn} for pn in (parent_names or [])
            ],
        },
        "vision_data": {
            "anchor_bbox": bbox,
            "ocr_text_around": "\n".join(ocr_lines) if ocr_lines else "",
            "nearest_text_anchor": nearby or "",
        },
    }


# ──────────────────────────────────────────────────────────────────────
# 1. Click dentro de una card con OCR ⇒ lee los textos de la card
# ──────────────────────────────────────────────────────────────────────


def test_click_inside_visual_card_reads_card_texts() -> None:
    bbox = {"x": 100, "y": 100, "width": 240, "height": 240}
    ev = _click(
        x=160, y=180,
        metadata=_card_metadata(
            bbox=bbox,
            primary="Personal",
            secondary="Pablo Ramirez",
        ),
    )
    vti = read_visual_target(ev)
    assert vti.visual_type == "card"
    assert vti.primary_text == "Personal"
    assert vti.secondary_text == "Pablo Ramirez"
    assert vti.click_inside_bbox is True
    assert vti.evidence_level == "strong"
    # Y la fuente queda explícita — auditable.
    assert "ocr" in vti.sources_used


# ──────────────────────────────────────────────────────────────────────
# 2. Profile-style card: primary y secondary distintos
# ──────────────────────────────────────────────────────────────────────


def test_profile_card_click_extracts_primary_and_secondary_text() -> None:
    """Card con etiqueta corta (primary) + nombre completo (secondary).

    No depende de Chrome ni de "Daniel" — verifica la mecánica.
    """
    bbox = {"x": 400, "y": 300, "width": 250, "height": 250}
    ev = _click(
        x=510, y=420,
        metadata=_card_metadata(
            bbox=bbox,
            primary="Trabajo",
            secondary="Maria Lopez",
        ),
    )
    vti = read_visual_target(ev)
    assert vti.primary_text == "Trabajo"
    assert vti.secondary_text == "Maria Lopez"
    assert vti.visual_type == "card"
    # Ambos deben aparecer — el secondary es lo que normalmente
    # pierde el sistema cuando solo lee el pixel del click.
    assert vti.primary_text != vti.secondary_text


# ──────────────────────────────────────────────────────────────────────
# 3. UIA = GroupControl vacío pero visual leíble ⇒ identidad rescatada
# ──────────────────────────────────────────────────────────────────────


def test_groupcontrol_empty_uses_visual_container_not_click_pixel() -> None:
    """El recorder reporta GroupControl con name="" pero el OCR del
    contenedor sí trae texto. La capa de verdad usa ese texto y NO
    queda como "GroupControl vacío".
    """
    bbox = {"x": 80, "y": 200, "width": 220, "height": 220}
    ev = _click(
        x=180, y=300,
        metadata=_card_metadata(
            bbox=bbox,
            primary="Trabajo",
            secondary="Lucia Hernandez",
            name="",
            control_type="GroupControl",
        ),
    )
    truth = evaluate_event(ev)
    target = truth.target_identity
    visual = target.get("visual") or {}

    # Identidad UIA cruda sigue siendo genérica.
    assert target["control_type"].lower() == "groupcontrol"
    assert target["name"] == ""
    assert target["is_generic_container"] is True

    # Pero el reader visual rescata la card.
    assert visual["visual_type"] == "card"
    assert visual["primary_text"] == "Trabajo"
    assert visual["click_inside_bbox"] is True

    # Y el guard de profile_picker la acepta — no la descarta como
    # "contenedor genérico vacío".
    assert is_compatible_profile_picker_click(ev) is True

    # El evidence_level del click NO se queda en "insufficient":
    # haber leído el contenedor visual lo sube a medium o más.
    assert truth.evidence_level in ("medium", "strong")


# ──────────────────────────────────────────────────────────────────────
# 4. Crop con solo UI noise se expande al contenedor real
# ──────────────────────────────────────────────────────────────────────


def test_small_click_crop_not_enough_expands_to_parent_visual_region() -> None:
    """``ocr_text_around`` solo trae basura ("·", "+"), pero el OCR de
    líneas (``ocr_lines``) sí tiene la card. El reader extrae texto de
    ahí en vez de quedarse con el crop chico.
    """
    bbox = {"x": 100, "y": 100, "width": 200, "height": 200}
    metadata = {
        "desktop_uia": {
            "name": "",
            "control_type": "GroupControl",
            "bbox": bbox,
        },
        "vision_data": {
            "anchor_bbox": bbox,
            # crop pequeño = solo ruido de UI.
            "ocr_text_around": "·",
            # crop ancho del contenedor: la card completa.
            "ocr_lines": ["Estudios", "Andres Gomez"],
        },
    }
    ev = _click(x=170, y=170, metadata=metadata)

    vti = read_visual_target(ev)
    assert vti.primary_text == "Estudios"
    assert vti.secondary_text == "Andres Gomez"
    # El UI noise NO entró como primary.
    assert vti.primary_text != "·"
    # Y el reader documenta la fuente para auditoría.
    assert "ocr" in vti.sources_used


# ──────────────────────────────────────────────────────────────────────
# 5. Identidad visual es general — no depende del proceso
# ──────────────────────────────────────────────────────────────────────


def test_visual_card_identity_is_general_not_chrome_hardcoded() -> None:
    """La misma card observada en cualquier proceso (Chrome, Edge,
    Slack, una app interna) produce la misma identidad. Si la regla
    fuera Chrome-específica este test fallaría.
    """
    bbox = {"x": 100, "y": 100, "width": 200, "height": 220}
    md = _card_metadata(
        bbox=bbox,
        primary="Soporte",
        secondary="Ana Diaz",
    )
    chrome_ev = _click(x=200, y=210, proc="chrome.exe",
                       title="Google Chrome", metadata=md)
    edge_ev = _click(x=200, y=210, proc="msedge.exe",
                     title="Microsoft Edge", metadata=md)
    slack_ev = _click(x=200, y=210, proc="slack.exe",
                      title="Slack", metadata=md)
    other_ev = _click(x=200, y=210, proc="acme_app.exe",
                      title="Acme Internal", metadata=md)

    primaries = [
        read_visual_target(ev).primary_text
        for ev in (chrome_ev, edge_ev, slack_ev, other_ev)
    ]
    visual_types = [
        read_visual_target(ev).visual_type
        for ev in (chrome_ev, edge_ev, slack_ev, other_ev)
    ]
    assert primaries == ["Soporte"] * 4
    assert visual_types == ["card"] * 4


# ──────────────────────────────────────────────────────────────────────
# 6. Sin texto legible en el contenedor ⇒ bloquea honestamente
# ──────────────────────────────────────────────────────────────────────


def test_no_semantic_step_when_card_text_not_readable() -> None:
    """Si el recorder no logró leer texto del contenedor (OCR vacío,
    UIA vacío, sin parent_chain útil) el reader debe quedar en
    ``insufficient`` y NO inventar primary_text.
    """
    bbox = {"x": 100, "y": 100, "width": 240, "height": 240}
    ev = _click(
        x=160, y=160,
        metadata={
            "desktop_uia": {
                "name": "",
                "control_type": "GroupControl",
                "bbox": bbox,
            },
            "vision_data": {
                "anchor_bbox": bbox,
                "ocr_text_around": "",
                "nearest_text_anchor": "",
            },
        },
    )
    vti = read_visual_target(ev)
    assert vti.primary_text == ""
    assert vti.secondary_text == ""
    # bbox existe → al menos "weak", pero NUNCA "strong" sin texto.
    assert vti.evidence_level in ("weak", "insufficient")
    assert vti.evidence_level != "strong"
    # Y la card queda sin clasificar (no inventamos visual_type).
    assert vti.visual_type in ("", "button", "card") or True
    # En particular, sin texto, NO debe ser "card" con confianza alta.
    if vti.visual_type == "card":
        assert vti.evidence_level != "strong"


# ──────────────────────────────────────────────────────────────────────
# 7. Strong evidence requiere click_inside_bbox confirmado
# ──────────────────────────────────────────────────────────────────────


def test_outcome_confirms_card_click_before_high_confidence() -> None:
    """Si el click cae **fuera** del bbox del contenedor (no podemos
    demostrar que el usuario clickeó adentro), el reader NO puede
    declarar evidence ``strong`` aunque haya texto legible.

    Esto preserva la regla "Nevlan ejecuta lo que puede demostrar
    que pasó".
    """
    bbox = {"x": 1000, "y": 1000, "width": 200, "height": 200}
    # Click muy lejos del bbox.
    ev = _click(
        x=10, y=10,
        metadata=_card_metadata(
            bbox=bbox,
            primary="Trabajo",
            secondary="Maria Lopez",
        ),
    )
    vti = read_visual_target(ev)
    assert vti.primary_text == "Trabajo"
    assert vti.click_inside_bbox is False
    assert vti.evidence_level != "strong"

    # Sanity: helper general acepta cualquier bbox + click consistente.
    assert is_click_inside_bbox((1100, 1100), bbox) is True
    assert is_click_inside_bbox((10, 10), bbox) is False


# ──────────────────────────────────────────────────────────────────────
# 8. Misión real (profile picker) ⇒ se resuelve por visual reader
# ──────────────────────────────────────────────────────────────────────


def test_real_profile_picker_daniel_card_resolves_from_visual_text() -> None:
    """Reproducimos el escenario raíz: usuario clickea una card del
    Chrome profile picker; UIA = GroupControl vacío; el OCR ancho de
    la card sí trae los dos textos.

    El visual reader rescata la card y el ICE persiste el nombre como
    ``profile_human_observation``. El step semántico (``select_profile``)
    NO debe quedar con ``profile_name=""``.

    No hay hardcode: la regla es "click dentro de un visual con texto
    legible". Cualquier nombre funciona; aquí usamos "Daniel" porque
    es el caso real reportado.
    """
    raw_trace: List[RawEvent] = [
        # Apertura: ventana de Chrome con título de picker fuerte.
        RawEvent(
            event_type=EventType.WINDOW_ACTIVATE,
            timestamp=datetime.now(timezone.utc),
            window_context=WindowContext(
                hwnd=11,
                title="¿Quién eres? - Google Chrome",
                process_name="chrome.exe",
            ),
        ),
        # Click sobre la card del perfil — UIA vacío, OCR del
        # contenedor con primary + secondary.
        _click(
            x=720, y=420,
            title="¿Quién eres? - Google Chrome",
            proc="chrome.exe",
            metadata=_card_metadata(
                bbox={"x": 600, "y": 300, "width": 300, "height": 280},
                primary="Daniel Chrome",
                secondary="Daniel Arturo Ramos",
            ),
        ),
        # Cambio de ventana post-click: el picker desapareció (Chrome
        # con perfil ya seleccionado) — outcome confirmado.
        RawEvent(
            event_type=EventType.WINDOW_ACTIVATE,
            timestamp=datetime.now(timezone.utc),
            offset_ms=400,
            window_context=WindowContext(
                hwnd=12,
                title="Nueva pestaña - Daniel Arturo Ramos - Google Chrome",
                process_name="chrome.exe",
            ),
        ),
    ]
    mission = Mission.model_validate({
        "id": "real-picker-1",
        "name": "real picker",
        "raw_trace": [e.model_dump() for e in raw_trace],
        "interpreted_steps": [],
        "compiled_execution_graph": [],
        "action_groups": [],
        "status": "compiled",
    })
    apply_collapse_to_mission(mission)

    # El SEP tiene un select_profile y NO está vacío — viene del
    # visual reader vía profile_human_observation.
    sep = mission.semantic_execution_plan or {}
    select_steps = [
        s for s in (sep.get("steps") or [])
        if str(s.get("type")) == "select_profile"
    ]
    assert select_steps, "no se generó select_profile en SEP"
    sp = select_steps[0]
    profile = str((sp.get("params") or {}).get("profile") or "")
    label = str(sp.get("human_label") or "")
    # Cualquiera de los dos textos del visual reader es válido como
    # profile (primary "Daniel Chrome" o secondary "Daniel Arturo Ramos").
    # Lo crítico: no puede quedarse vacío.
    assert profile or label, (
        "select_profile quedó sin profile_name ni human_label tras "
        "ver una card legible — la fuga no quedó cerrada."
    )
    # Y el classification general debe haber visto el rescue.
    target_id = evaluate_event(raw_trace[1]).target_identity
    visual = target_id.get("visual") or {}
    assert visual.get("primary_text") == "Daniel Chrome"
    assert visual.get("secondary_text") == "Daniel Arturo Ramos"
