"""
Nevlan — Anti-Overfitting Suite (PRD 2026-05-08)
=================================================

Validamos que la estabilización Chrome → YouTube → Luis Miguel **NO**
esté hardcodeada al caso particular. Cada test usa entradas
completamente distintas y exige el mismo comportamiento de producto:

  1. Otra búsqueda en YouTube ("La incondicional de Luis Miguel") —
     mismo artista pero query diferente.
  2. Búsqueda en YouTube SIN Luis Miguel ("Historia de la inteligencia
     artificial en México") — múltiples stopwords protegidas.
  3. Búsqueda en Google (no YouTube) ("Banco del Bienestar informes
     anuales 2025") — el detector ``search_youtube`` NO debe disparar.
  4. Perfil distinto ("Trabajo Banco D Daniel Ramos") — el resolver
     extrae canonical genérico, no requiere la frase del E2E original.
  5. Verificación estática: no hay strings hardcodeados que
     condicionen lógica.
  6. El caso original Chrome+YouTube sigue siendo regression test
     (canary), nunca lógica especial.

Filosofía: si alguien renombra "Daniel Arturo Ramos" a "Mario
Pérez" en TODA la code base, los tests deben seguir pasando porque
el sistema nunca chequea por ese string concreto.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

from app.contracts.mission import (
    EventType, KeyboardAction, Mission, MouseAction, RawEvent,
    WindowContext,
)
from app.services.missions.intent_collapse_engine import (
    CollapseStatus, apply_collapse_to_mission, assess_query_fidelity,
    collapse_intent,
)
from app.services.missions.profile_resolver import (
    extract_canonical_profile_name, reset_alias_store,
)


_BASE = datetime(2026, 5, 8, 9, 0, 0, tzinfo=timezone.utc)


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
           extra_meta: dict = None,
           hwnd: int = 1) -> RawEvent:
    meta = {}
    if uia:
        meta["uia"] = uia
    if web:
        meta["web"] = web
    if extra_meta:
        meta.update(extra_meta)
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
               hwnd: int = 1) -> RawEvent:
    meta = {"final_text": text, "text": text}
    if user_typed_text is not None:
        meta["field_commit"] = {"user_typed_text": user_typed_text}
    return RawEvent(
        event_type=EventType.KEYBOARD_TYPE_TEXT,
        keyboard_action=KeyboardAction(keys=list(text)),
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


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    from app.core.config import settings
    var = tmp_path / "var"
    var.mkdir()
    monkeypatch.setattr(settings, "VAR_PATH", var)
    reset_alias_store()
    yield
    reset_alias_store()


def _build_youtube_search_trace(
    *,
    user_query: str,
    uia_value: str,
    profile_label: str = "Otra Persona",
) -> List[RawEvent]:
    """Trace genérico: open Chrome → YouTube → buscar query → Enter.
    Los textos son TODOS configurables: probamos canciones, temas,
    nombres distintos.
    """
    return [
        _activate(0, hwnd=10, title="¿Quién eres? - Google Chrome",
                  proc="chrome.exe"),
        _click(500, 400, ms=100, hwnd=10,
               title="¿Quién eres? - Google Chrome", proc="chrome.exe",
               uia={"name": "GroupControl",
                    "control_type": "GroupControl"},
               extra_meta={"profile_picker_item_text": profile_label}),
        _activate(300, hwnd=11, title="Nueva pestaña - Google Chrome",
                  proc="chrome.exe"),
        _click(300, 50, ms=400, hwnd=11,
               title="Nueva pestaña - Google Chrome", proc="chrome.exe",
               uia={"name": "Nueva pestaña",
                    "control_type": "ButtonControl"}),
        _activate(500, hwnd=12, title="YouTube - Google Chrome",
                  proc="chrome.exe"),
        _click(900, 120, ms=600, hwnd=12,
               title="YouTube - Google Chrome", proc="chrome.exe",
               uia={"name": "guide-service",
                    "control_type": "EditControl"},
               web={"url": "https://www.youtube.com/",
                    "domain": "youtube.com",
                    "role": "searchbox", "placeholder": "Buscar"}),
        _type_text(uia_value, ms=700, hwnd=12,
                   title="YouTube - Google Chrome", proc="chrome.exe",
                   user_typed_text=user_query),
        _enter(900, hwnd=12, title="YouTube - Google Chrome",
               proc="chrome.exe"),
    ]


# ──────────────────────────────────────────────────────────────────
# 1. Otra búsqueda en YouTube — mismo artista, query distinta
# ──────────────────────────────────────────────────────────────────


class TestOtherYoutubeQueryWithDe:
    """Prueba que el "de" sobreviva en una query DISTINTA al E2E."""

    def test_la_incondicional_preserves_de_and_la(self):
        """Query: "La incondicional de Luis Miguel".

        Debe quedar al final con:
          * "La" preservado
          * "de" preservado
          * acentos preservados si vinieron en el buffer
        """
        m = Mission(
            name="t",
            raw_trace=_build_youtube_search_trace(
                user_query="La incondicional de Luis Miguel",
                uia_value="incondicional luis miguel",  # UIA degradada
                profile_label="María González",
            ),
        )
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert sep is not None
        sy = next(
            s for s in sep["steps"]
            if s["type"] == "search_content"
            and str((s.get("params") or {}).get("provider", "")).lower()
            == "youtube"
        )
        q = sy["params"]["query"]
        toks = [t.lower() for t in re.findall(r"\w+", q, flags=re.UNICODE)]
        # Tokens funcionales preservados:
        assert "de" in toks, q
        assert "la" in toks, q
        # Estado fidelity ok (el buffer aporta la versión completa).
        assert sy["params"].get("query_fidelity_status", "ok") == "ok"
        assert sy["needs_user_label"] is False


# ──────────────────────────────────────────────────────────────────
# 2. Búsqueda YouTube SIN Luis Miguel — multiple stopwords
# ──────────────────────────────────────────────────────────────────


class TestYoutubeQueryMultipleStopwords:
    """Query con múltiples palabras pequeñas: "de", "la", "en"."""

    QUERY = "Historia de la inteligencia artificial en México"

    def test_multiple_stopwords_all_preserved(self):
        m = Mission(
            name="t",
            raw_trace=_build_youtube_search_trace(
                user_query=self.QUERY,
                uia_value="historia inteligencia artificial mexico",
                profile_label="Carlos Pérez",
            ),
        )
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert sep is not None
        sy = next(
            s for s in sep["steps"]
            if s["type"] == "search_content"
            and str((s.get("params") or {}).get("provider", "")).lower()
            == "youtube"
        )
        q = sy["params"]["query"]
        low_toks = [t.lower() for t in re.findall(r"\w+", q,
                                                   flags=re.UNICODE)]
        # TODOS los stopwords protegidos vinieron del buffer y deben
        # estar en la query final:
        for t in ("de", "la", "en"):
            assert t in low_toks, f"falta '{t}' en {q}"
        assert sy["params"].get("query_fidelity_status", "ok") == "ok"
        assert sy["needs_user_label"] is False

    def test_assessor_alone_handles_multiple_stopwords(self):
        """El assessor puro detecta la pérdida sin pasar por el ICE."""
        best, status, suspect = assess_query_fidelity(
            "historia inteligencia artificial mexico",
            candidate_queries=[self.QUERY],
        )
        assert status == "missing_short_token"
        assert suspect.lower() in {"de", "la", "en"}
        assert best == self.QUERY


# ──────────────────────────────────────────────────────────────────
# 3. Búsqueda en Google (no YouTube) — search_youtube NO dispara
# ──────────────────────────────────────────────────────────────────


def _build_google_search_trace() -> List[RawEvent]:
    """Misma forma que YouTube, pero el contexto siempre es google.com.
    El detector ``search_youtube`` jamás debe activarse.
    """
    return [
        _activate(0, hwnd=11, title="Nueva pestaña - Google Chrome",
                  proc="chrome.exe"),
        _activate(200, hwnd=12, title="Google", proc="chrome.exe"),
        _click(500, 300, ms=300, hwnd=12,
               title="Google", proc="chrome.exe",
               uia={"name": "Buscar", "control_type": "EditControl"},
               web={"url": "https://www.google.com/",
                    "domain": "google.com",
                    "role": "searchbox", "placeholder": "Buscar"}),
        _type_text(
            "Banco del Bienestar informes anuales 2025",
            ms=500, hwnd=12,
            title="Google", proc="chrome.exe",
            user_typed_text="Banco del Bienestar informes anuales 2025",
        ),
        _enter(700, hwnd=12, title="Google", proc="chrome.exe"),
    ]


class TestGoogleSearchDoesNotBecomeYoutube:
    """Ninguna búsqueda en google.com debe colapsar como
    ``search_youtube``. Es la garantía de "no hardcoded YouTube
    fuera del resolver de YouTube".
    """

    def test_google_search_does_not_collapse_to_search_youtube(self):
        m = Mission(name="g_search", raw_trace=_build_google_search_trace())
        plan = collapse_intent(mission=m)
        types = [s.type for s in plan.semantic_steps]
        assert "search_youtube" not in types, (
            f"search_youtube NO debe activarse para google.com — "
            f"steps detectados: {types}"
        )

    def test_google_search_keeps_query_with_stopwords(self):
        """Aunque por ahora no exista un detector ``search_google``
        canónico, el assessor puro DEBE preservar "del" en
        "Banco del Bienestar".
        """
        best, status, _ = assess_query_fidelity(
            "Banco del Bienestar informes anuales 2025",
            candidate_queries=[],
        )
        assert status == "ok"
        assert best == "Banco del Bienestar informes anuales 2025"

    def test_google_query_with_dirty_uia_recovers_del(self):
        best, status, suspect = assess_query_fidelity(
            "banco bienestar informes anuales 2025",
            candidate_queries=[
                "Banco del Bienestar informes anuales 2025",
            ],
        )
        assert status == "missing_short_token"
        assert suspect.lower() == "del"
        assert best == "Banco del Bienestar informes anuales 2025"


# ──────────────────────────────────────────────────────────────────
# 4. Perfil distinto — extracción canónica genérica
# ──────────────────────────────────────────────────────────────────


class TestDifferentProfilesGenericExtraction:
    """El extractor JAMÁS hardcodea "Daniel Arturo Ramos". Para
    cualquier label sucio "<App> <Inicial> <Nombre Apellido>"
    debe extraer el canonical correcto.
    """

    @pytest.mark.parametrize("dirty,expected", [
        ("Trabajo Banco D Daniel Ramos", "Daniel Ramos"),
        ("Personal Chrome M Maria Lopez", "Maria Lopez"),
        ("Mi Trabajo C Carlos Pérez", "Carlos Pérez"),
        ("Familia Chrome A Ana García-López", "Ana García-López"),
        ("Daniel Chrome D Daniel Arturo Ramos", "Daniel Arturo Ramos"),
        ("Sign in as Maria Lopez", "Maria Lopez"),
        ("Maria Lopez - Microsoft Edge", "Maria Lopez"),
    ])
    def test_extracts_canonical_for_arbitrary_dirty_labels(
        self, dirty, expected,
    ):
        assert extract_canonical_profile_name(dirty) == expected, dirty

    def test_collapse_engine_resolves_trabajo_banco_profile(self):
        """E2E: trace con perfil "Trabajo Banco D Daniel Ramos".
        El resolver debe extraer "Daniel Ramos".
        """
        m = Mission(
            name="otro_perfil",
            raw_trace=_build_youtube_search_trace(
                user_query="X",
                uia_value="X",
                profile_label="Trabajo Banco D Daniel Ramos",
            ),
        )
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        sp = next(
            (s for s in sep["steps"] if s["type"] == "select_profile"),
            None,
        )
        assert sp is not None
        # "Daniel Ramos" es la corrida más larga de tokens-nombre
        # tras filtrar "Trabajo Banco" (apps) y "D" (inicial).
        assert sp["params"]["profile_name"] == "Daniel Ramos"
        assert sp["needs_user_label"] is False


# ──────────────────────────────────────────────────────────────────
# 5. Verificación estática: ningún string concreto condiciona lógica
# ──────────────────────────────────────────────────────────────────


_APP_DIR = Path(__file__).resolve().parents[2] / "app"
_HARDCODED_STRINGS = [
    "Daniel Arturo Ramos",
    "Devuélveme el amor de Luis Miguel",
    "Devuelveme el amor de Luis Miguel",
    "Devuelveme el amor luis miguel",
    "devuelveme el amor luis miguel",
    "Luis Miguel",
]


def _find_conditional_uses(haystack: str, needle: str) -> List[str]:
    """Devuelve líneas que parezcan USAR ``needle`` como condición
    (no docstrings/comentarios). Heurística:

      * comparación ``==`` / ``!=``
      * pertenencia ``in {...}`` / ``in [...]``
      * uso como argumento dentro de ``startswith``/``endswith`` del
        propio runtime (no docstrings).

    Devuelve líneas SOSPECHOSAS para revisión humana.
    """
    suspects: List[str] = []
    pattern_eq = re.compile(rf'==\s*[\"\']{re.escape(needle)}[\"\']')
    pattern_in = re.compile(
        rf'in\s*[\(\[\{{].*[\"\']{re.escape(needle)}[\"\']'
    )
    pattern_starts = re.compile(
        rf'\.(startswith|endswith)\s*\(\s*[\"\']{re.escape(needle)}[\"\']'
    )
    for ln in haystack.splitlines():
        stripped = ln.strip()
        # Saltamos comentarios puros y docstrings (líneas que abren/
        # cierran con `"""` o que empiezan con `#` o que están
        # claramente dentro de una cadena de doc — heurística simple).
        if stripped.startswith("#"):
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        if pattern_eq.search(ln) or pattern_in.search(ln) \
                or pattern_starts.search(ln):
            suspects.append(ln.strip())
    return suspects


class TestNoHardcodedStringsInLogic:
    """Auditoría: ningún módulo de ``app/`` debe usar las cadenas del
    caso real como condición de control de flujo.
    """

    def test_no_conditional_use_of_target_strings(self):
        violations: List[str] = []
        for py in _APP_DIR.rglob("*.py"):
            try:
                src = py.read_text(encoding="utf-8")
            except Exception:
                continue
            for needle in _HARDCODED_STRINGS:
                hits = _find_conditional_uses(src, needle)
                for h in hits:
                    violations.append(
                        f"{py.relative_to(_APP_DIR.parent)}: {h}"
                    )
        assert not violations, (
            "Hay strings hardcodeadas en lógica de control:\n  "
            + "\n  ".join(violations)
        )


# ──────────────────────────────────────────────────────────────────
# 6. Canary: el caso original sigue como regression test
# ──────────────────────────────────────────────────────────────────


class TestOriginalCaseStillWorks:
    """El caso del PRD §H (Chrome → YouTube → Luis Miguel) sigue
    funcionando, no porque haya código especial para él, sino porque
    es UN CASO MÁS del comportamiento genérico.
    """

    def test_original_case_still_reaches_ready(self):
        m = Mission(
            name="canary",
            raw_trace=_build_youtube_search_trace(
                user_query="Devuélveme el amor de Luis Miguel",
                uia_value="devuelveme el amor luis miguel",
                profile_label="Daniel Chrome D Daniel Arturo Ramos",
            ),
        )
        apply_collapse_to_mission(m)
        sep = m.semantic_execution_plan
        assert sep["intent_status"] == CollapseStatus.READY, (
            sep.get("not_ready_reasons")
        )
        sp = next(s for s in sep["steps"] if s["type"] == "select_profile")
        sy = next(
            s for s in sep["steps"]
            if s["type"] == "search_content"
            and str((s.get("params") or {}).get("provider", "")).lower()
            == "youtube"
        )
        assert sp["params"]["profile_name"] == "Daniel Arturo Ramos"
        assert sy["params"]["query"] == "Devuélveme el amor de Luis Miguel"
