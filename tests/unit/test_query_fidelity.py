"""
Nevlan — Tests de Field Commit / Query Fidelity (PRD 2026-05-07c §B)
====================================================================

La búsqueda final que el plan ejecuta NUNCA debe perder palabras
pequeñas (``"de"``, ``"la"``, ``"el"``, ``"of"``, ``"the"``, ...)
ni acentos del buffer original.

Esta suite cubre:

  * ``_typed_text_of`` — selección de la mejor fuente entre
    ``user_typed_text`` (buffer del usuario), ``final_text`` (UIA/DOM),
    y la reconstrucción cruda desde ``keyboard_action.keys``.
  * ``assess_query_fidelity`` — comparación entre candidatas y
    detección de pérdida de tokens cortos protegidos.
  * El detector ``_detect_search_youtube`` con ``needs_user_label=True``
    cuando se detecta sospecha (NO regrabar, solo confirmar).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from app.contracts.mission import (
    EventType, KeyboardAction, MouseAction, Mission, RawEvent,
    WindowContext,
)
from app.services.missions.intent_collapse_engine import (
    _typed_text_of,
    assess_query_fidelity,
    collapse_intent,
    detect_missing_connector_pattern,
)


# ──────────────────────────────────────────────────────────────────
# Helpers de construcción de RawEvent
# ──────────────────────────────────────────────────────────────────

_BASE = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)


def _ts(ms: int) -> datetime:
    return _BASE + timedelta(milliseconds=ms)


def _activate(ms: int, *, title: str, proc: str, hwnd: int = 1) -> RawEvent:
    return RawEvent(
        event_type=EventType.WINDOW_ACTIVATE,
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        timestamp=_ts(ms),
        sequence_id=ms,
    )


def _click(x: int, y: int, ms: int, *, title: str, proc: str,
           uia: dict = None, web: dict = None,
           hwnd: int = 1) -> RawEvent:
    meta = {}
    if uia:
        meta["uia"] = uia
    if web:
        meta["web"] = web
    return RawEvent(
        event_type=EventType.MOUSE_CLICK,
        mouse_action=MouseAction(x=x, y=y),
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        timestamp=_ts(ms),
        sequence_id=ms,
        metadata=meta,
    )


def _type_text(text: str, ms: int, *, title: str, proc: str = "chrome.exe",
               user_typed_text: str = None,
               keys: List[str] = None,
               hwnd: int = 1) -> RawEvent:
    """Emite KEYBOARD_TYPE_TEXT con metadata realista."""
    meta = {"final_text": text, "text": text}
    if user_typed_text is not None:
        meta["field_commit"] = {"user_typed_text": user_typed_text}
    return RawEvent(
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        keyboard_action=KeyboardAction(keys=list(keys or text)),
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        timestamp=_ts(ms),
        sequence_id=ms,
        metadata=meta,
    )


def _enter(ms: int, *, title: str, proc: str = "chrome.exe",
           hwnd: int = 1) -> RawEvent:
    return RawEvent(
        event_type=EventType.KEYBOARD_KEY_PRESS,
        keyboard_action=KeyboardAction(keys=["enter"]),
        window_context=WindowContext(
            hwnd=hwnd, title=title, process_name=proc,
        ),
        timestamp=_ts(ms),
        sequence_id=ms,
    )


# ──────────────────────────────────────────────────────────────────
# 1. _typed_text_of — selección de la mejor fuente
# ──────────────────────────────────────────────────────────────────

class TestTypedTextOf:
    def test_field_commit_prefers_user_typed_buffer_over_bad_uia_read(self):
        """``field_commit.user_typed_text`` (buffer del usuario) gana
        contra ``final_text`` corrupto que perdió tokens.
        """
        ev = _type_text(
            "devuelveme el amor luis miguel",   # final_text leído por UIA
            ms=100, title="YouTube - Google Chrome",
            user_typed_text="Devuélveme el amor de Luis Miguel",
        )
        out = _typed_text_of(ev)
        # "de" preserved → debe ganar la versión del buffer.
        assert "de" in out.lower().split()
        assert out == "Devuélveme el amor de Luis Miguel"

    def test_falls_back_to_final_text_when_no_buffer(self):
        ev = _type_text("hola mundo", ms=100,
                        title="YouTube - Google Chrome")
        assert _typed_text_of(ev) == "hola mundo"

    def test_falls_back_to_keys_reconstruction(self):
        """Sin metadata final_text/buffer, reconstruimos desde keys."""
        ev = RawEvent(
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            keyboard_action=KeyboardAction(keys=list("abcde")),
            window_context=WindowContext(
                title="x", process_name="chrome.exe",
            ),
            timestamp=_ts(0),
            sequence_id=0,
        )
        assert _typed_text_of(ev) == "abcde"

    def test_returns_empty_when_no_source(self):
        ev = RawEvent(
            event_type=EventType.KEYBOARD_TYPE_TEXT,
            window_context=WindowContext(title="x", process_name="x"),
            timestamp=_ts(0), sequence_id=0,
        )
        assert _typed_text_of(ev) == ""


# ──────────────────────────────────────────────────────────────────
# 2. assess_query_fidelity — sospecha de pérdida de tokens cortos
# ──────────────────────────────────────────────────────────────────

class TestAssessQueryFidelity:
    def test_query_fidelity_detects_missing_short_token(self):
        """El caso del PRD: la versión "limpia" perdió "de", la
        versión completa lo conserva. El assessor reporta
        ``missing_short_token`` y prefiere la completa.
        """
        best, status, suspect = assess_query_fidelity(
            "devuelveme el amor luis miguel",
            candidate_queries=["Devuélveme el amor de Luis Miguel"],
        )
        assert status == "missing_short_token"
        assert suspect.lower() == "de"
        assert best == "Devuélveme el amor de Luis Miguel"

    def test_query_fidelity_ok_when_complete(self):
        best, status, _ = assess_query_fidelity(
            "Devuélveme el amor de Luis Miguel",
            candidate_queries=[],
        )
        assert status == "ok"
        assert best == "Devuélveme el amor de Luis Miguel"

    def test_empty_query_status(self):
        best, status, _ = assess_query_fidelity("", candidate_queries=[])
        assert status == "empty"
        assert best == ""

    def test_query_fidelity_keeps_accents_when_available(self):
        best, status, _ = assess_query_fidelity(
            "devuelveme el amor de luis miguel",
            candidate_queries=["Devuélveme el amor de Luis Miguel"],
        )
        assert status == "ok"  # mismo set de tokens; gana la más larga
        assert best == "Devuélveme el amor de Luis Miguel"

    def test_query_fidelity_works_for_english_stopwords(self):
        best, status, suspect = assess_query_fidelity(
            "lord rings extended",
            candidate_queries=["The Lord of the Rings extended"],
        )
        assert status == "missing_short_token"
        # debe detectar al menos uno de los protegidos
        assert suspect.lower() in {"the", "of"}


# ──────────────────────────────────────────────────────────────────
# 3. _detect_search_youtube vía collapse_intent — E2E del detector
# ──────────────────────────────────────────────────────────────────

def _build_youtube_search_trace(
    *,
    user_typed_text: str = "Devuélveme el amor de Luis Miguel",
    uia_final_text: str = "devuelveme el amor luis miguel",
) -> List[RawEvent]:
    """Trace mínimo: activar YouTube, click en search box, escribir,
    Enter. Permite controlar buffer del usuario vs lectura UIA.
    """
    return [
        _activate(0, hwnd=10, title="YouTube - Google Chrome",
                  proc="chrome.exe"),
        _click(900, 120, ms=100, hwnd=10,
               title="YouTube - Google Chrome", proc="chrome.exe",
               uia={"name": "guide-service", "control_type": "EditControl"},
               web={"url": "https://www.youtube.com/",
                    "domain": "youtube.com",
                    "role": "searchbox", "placeholder": "Buscar"}),
        _type_text(uia_final_text, ms=200, hwnd=10,
                   title="YouTube - Google Chrome", proc="chrome.exe",
                   user_typed_text=user_typed_text),
        _enter(300, hwnd=10, title="YouTube - Google Chrome",
               proc="chrome.exe"),
    ]


class TestSearchYoutubeFidelity:
    def test_youtube_search_preserves_full_query_with_stopwords(self):
        events = _build_youtube_search_trace()
        m = Mission(name="qf", raw_trace=events)
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        assert sy.params["query"] == "Devuélveme el amor de Luis Miguel"

    def test_youtube_search_does_not_drop_de_before_luis_miguel(self):
        events = _build_youtube_search_trace()
        m = Mission(name="qf", raw_trace=events)
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        assert " de " in f" {sy.params['query'].lower()} "

    def test_query_fidelity_no_rerecord_only_confirm(self):
        """Cuando solo hay versión "sucia" (sin buffer), el sistema
        no debe pedir regrabar. Si la única evidencia es sin "de",
        eso es lo que hay — no asumimos lo contrario.
        """
        # Solo proveemos la versión sin "de" (sin user_typed_text).
        events = _build_youtube_search_trace(
            user_typed_text=None,
            uia_final_text="devuelveme el amor luis miguel",
        )
        # Eliminamos el field_commit del último evento.
        for ev in events:
            if ev.metadata and "field_commit" in ev.metadata:
                ev.metadata.pop("field_commit", None)
        m = Mission(name="qf", raw_trace=events)
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        # Sin evidencia de "de", el sistema acepta la query sucia
        # como ok — no debe pedir regrabar ni marcar suspect.
        assert sy.params.get("query_fidelity_status") in ("ok", None, "")

    def test_search_youtube_ready_when_query_exact(self):
        """Cuando el buffer del usuario coincide con UIA y no hay
        pérdida de tokens, la query queda ``ok`` y NO needs_label.
        """
        events = _build_youtube_search_trace(
            user_typed_text="Devuélveme el amor de Luis Miguel",
            uia_final_text="Devuélveme el amor de Luis Miguel",
        )
        m = Mission(name="qf", raw_trace=events)
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        assert sy.params["query"] == "Devuélveme el amor de Luis Miguel"
        assert sy.params.get("query_fidelity_status", "ok") == "ok"
        assert not sy.needs_user_label


# ──────────────────────────────────────────────────────────────────
# La heurística de conector faltante NUNCA auto-corrige (PRD revisión)
# ──────────────────────────────────────────────────────────────────


class TestHeuristicNeverAutoCorrects:
    """PRD 2026-05-08 (revisión §3): la heurística de detección de
    conector faltante debe quedarse SIEMPRE como sugerencia. No
    debe insertar ``de`` automáticamente en la query final.

    Comportamiento permitido:
        query                = literal lo que el usuario escribió
        query_fidelity_status = "suspect"
        suggested_query      = la sugerencia heurística
        needs_user_label     = True

    Comportamiento PROHIBIDO:
        query                = la sugerencia con "de" inyectada
        query_fidelity_status = "ok"
        needs_user_label     = False
    """

    def test_pure_function_returns_suggestion_only(self):
        """``detect_missing_connector_pattern`` es PURA: detecta y
        sugiere, nunca muta la entrada."""
        original = "Devuelveme el amor Luis Miguel"
        is_suspect, suggested, connector = (
            detect_missing_connector_pattern(original)
        )
        assert is_suspect is True
        assert suggested == "Devuelveme el amor de Luis Miguel"
        assert connector == "de"
        # La función NO muta su entrada (Python strs son inmutables
        # pero verificamos que devuelve la sugerencia separada).
        assert original == "Devuelveme el amor Luis Miguel"

    def test_query_value_in_plan_is_unchanged_when_suspect(self):
        """En el plan generado, ``params.query`` es exactamente lo
        que el usuario tipeó — la sugerencia va en
        ``suggested_query`` aparte."""
        events = _build_youtube_search_trace(
            user_typed_text="Devuelveme el amor Luis Miguel",
            uia_final_text="Devuelveme el amor Luis Miguel",
        )
        m = Mission(name="qf", raw_trace=events)
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        # La query se queda con lo TIPEADO, no con la sugerencia.
        assert sy.params["query"] == "Devuelveme el amor Luis Miguel"
        # La sugerencia vive en su propio campo.
        assert sy.params.get("suggested_query") == (
            "Devuelveme el amor de Luis Miguel"
        )

    def test_status_is_suspect_not_ok_when_pattern_detected(self):
        events = _build_youtube_search_trace(
            user_typed_text="Devuelveme el amor Luis Miguel",
            uia_final_text="Devuelveme el amor Luis Miguel",
        )
        m = Mission(name="qf", raw_trace=events)
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        assert sy.params.get("query_fidelity_status") == "suspect"
        assert sy.params.get("query_fidelity_status") != "ok"

    def test_needs_user_label_is_true_when_pattern_detected(self):
        events = _build_youtube_search_trace(
            user_typed_text="Devuelveme el amor Luis Miguel",
            uia_final_text="Devuelveme el amor Luis Miguel",
        )
        m = Mission(name="qf", raw_trace=events)
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        assert sy.needs_user_label is True
        # El prompt debe ofrecer la sugerencia sin imponerla.
        prompt = sy.label_prompt or ""
        assert "quisiste decir" in prompt.lower()

    def test_legitimate_query_without_connector_is_not_corrupted(self):
        """Si el usuario realmente quiere buscar algo sin conector
        (ej. "Pedro Almodóvar película"), no debemos marcar suspect.
        El nombre va PRIMERO, no AL FINAL — patrón distinto.
        """
        is_suspect, _, _ = detect_missing_connector_pattern(
            "Pedro Almod\u00f3var pel\u00edcula"
        )
        assert is_suspect is False

    def test_short_query_never_flagged_as_suspect(self):
        for q in ("Luis Miguel", "Luis", "amor"):
            is_suspect, _, _ = detect_missing_connector_pattern(q)
            assert is_suspect is False, (
                f"query corta {q!r} no debe ser flagged como suspect"
            )

    def test_query_with_connector_is_not_flagged(self):
        for q in (
            "amor de Luis Miguel",
            "canción de Shakira",
            "song by The Beatles",
        ):
            is_suspect, _, _ = detect_missing_connector_pattern(q)
            assert is_suspect is False, (
                f"query {q!r} ya tiene conector — no debe ser suspect"
            )

    def test_collapse_does_not_silently_inject_de(self):
        """Garantía dura: en NINGÚN punto del pipeline el ICE
        modifica el texto literal del usuario para inyectar 'de'."""
        events = _build_youtube_search_trace(
            user_typed_text="Devuelveme el amor Luis Miguel",
            uia_final_text="Devuelveme el amor Luis Miguel",
        )
        m = Mission(name="qf", raw_trace=events)
        plan = collapse_intent(mission=m)
        sy = next(
            (s for s in plan.semantic_steps if s.type == "search_youtube"),
            None,
        )
        assert sy is not None
        # NUNCA debe aparecer "de" inyectada en el texto final
        # automáticamente — eso solo lo decide el usuario.
        tokens = sy.params["query"].lower().split()
        idx_amor = tokens.index("amor") if "amor" in tokens else -1
        if idx_amor >= 0 and idx_amor + 1 < len(tokens):
            assert tokens[idx_amor + 1] != "de", (
                "El ICE inyectó 'de' silenciosamente — PROHIBIDO."
            )
