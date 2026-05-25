"""
Nevlan — Intent Collapse Engine (Final)
=======================================

Convierte grabaciones humanas en **misiones semánticas limpias, compactas y
ejecutables**, eliminando residuos técnicos.

Filosofía
---------

    Si existe intención semántica clara, los eventos técnicos desaparecen.

El compiler V2 ya produce bloques. Pero los traces reales siguen sangrando
ruido: clicks sobre ``guide-service``, ``main`` y ``GroupControl`` vacíos,
``type_text`` huérfano, ``Enter`` separado, scrolls absolutos, fragmentos
``DevueDevue``... Resultado: 7+ pasos técnicos, confianza media 39%, 4
"reparar".

Eso no es aceptable para una app tipo hit mundial. Este engine corre como
**último filtro** del pipeline V2 y, cuando hay intención superior, descarta
todo lo de abajo.

Patrones de colapso
-------------------

A) Windows Search + Chrome result ⇒ ``open_app(app="chrome",
   method="windows_search")``  conf 0.95.
B) Chrome profile picker ⇒ ``select_profile(profile=<ocr>)``
   conf 0.75–0.95 (``needs_user_label=True`` si OCR no recuperó nombre).
C) Nueva pestaña (botón "+" / Ctrl+T / UIA "Nueva pestaña") ⇒
   ``open_new_tab(app="chrome", preferred_strategy="ctrl_t")`` conf 0.9.
D) Abrir YouTube (bookmark, click visual, address bar, title) ⇒
   ``open_url(url="youtube.com", name="YouTube")`` conf 0.95.
E) Búsqueda en YouTube (barra YouTube + texto + Enter, o
   ``guide-service`` + texto + Enter en YouTube) ⇒
   ``search_youtube(query=<final>, submit=True,
                    target="youtube_search_box")`` conf 0.95.
F) Scroll en resultados (después de ``search_youtube``, mouse_scroll en
   ventana YouTube) ⇒ ``scroll_results(direction="down",
                                       amount=<n_ticks>,
                                       context="youtube_results")``
   conf 0.85.

Block builder (orden canónico)
-------------------------------

    Bloque 1 "Abrir Chrome"          [open_app, select_profile]
    Bloque 2 "Abrir YouTube"         [open_new_tab, open_url]
    Bloque 3 "Buscar en YouTube"     [search_youtube]
    Bloque 4 "Explorar resultados"   [scroll_results]   (opcional)

Si un bloque queda vacío se omite. ``Explorar resultados`` se omite si no
hubo scroll.

Hard cleanup
------------

Tras el colapso corremos un *post-pass*: cualquier paso técnico
(``click_button``, ``type_text``, ``key_press enter``, scroll absoluto,
``guide-service``, ``main``, ``click_fallback``) que esté **cubierto** por
un paso semántico se elimina. Si quedan más de 6 pasos en la misión
ejemplo, se considera **fallo** del engine.

Repair questions
----------------

Máximo 1–2. Solo se permiten para:

* perfil desconocido (sin OCR ni etiqueta)
* target realmente ambiguo sin contexto

Prohibido preguntar por ``guide-service`` cuando hay texto + Enter en
YouTube, ``open_new_tab`` cuando la nueva pestaña ya se infiere, etc.

Confianza por intención
-----------------------

* ``open_app`` chrome por patrón completo (winsearch + chrome.exe): 0.95.
* ``open_app`` chrome implícito (chrome.exe activate sin winsearch): 0.90.
* ``select_profile`` con perfil leído por OCR: 0.90.
* ``select_profile`` sin perfil pero contexto picker claro: 0.75 +
  ``needs_user_label``.
* ``open_new_tab``: 0.90.
* ``open_url`` YouTube: 0.95.
* ``search_youtube`` con query + Enter + contexto YouTube: 0.95.
* ``scroll_results`` después de resultados: 0.85.

Status final
------------

``EXECUTABLE`` cuando hay 0 pasos rojos, 0 clicks genéricos, 0 text suelto,
0 Enter suelto, 0 ``guide-service`` / ``main``, 0 coords absolutas como
estrategia primaria, y como mucho 1 ``needs_user_label``.

``NEEDS_REVIEW`` cuando falta perfil, falta target real sin contexto, o
hay duda humana real.

``COMPILED_DRAFT`` cuando no se puede colapsar la misión.

API
---

>>> plan = collapse_intent(mission=m)
>>> plan.status, plan.confidence_avg, plan.repair_count

>>> # Para mutar la misión in-place (útil desde intent_compiler_v2):
>>> apply_collapse_to_mission(m)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    EventType,
    InterpretedStep,
    Mission,
    RawEvent,
    TargetContext,
    ValidationStrategy,
)
from app.core.logger import log
from app.services.missions.field_session_manager import (
    FieldSession,
    build_field_sessions,
)
from app.services.missions.invalid_target_resolver import (
    youtube_search_locators,
)
from app.services.missions.raw_event_sanitizer import sanitize_raw_trace


# ──────────────────────────────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────────────────────────────

# Tipos semánticos que el engine emite. Cualquier otro tipo proveniente
# del compiler legacy se considera "residuo" y se intenta eliminar en el
# post-pass.
SEMANTIC_TYPES = frozenset({
    "open_app",
    "select_profile",
    "open_new_tab",
    "open_url",
    "search_youtube",
    "scroll_results",
})

# Tipos que NUNCA pueden sobrevivir si hay una intención superior — esos
# son los "residuos técnicos" del compiler clásico.
RESIDUE_TYPES = frozenset({
    "click_button",
    "click",
    "type_text",
    "key_press",
    "key_press_enter",
    "send_hotkey",
    "submit_search_blank",
    "scroll",
    "scroll_absolute",
    "click_fallback",
})

# Nombres UIA "técnicos" que solo pueden sobrevivir como targets si la
# intención NO se pudo colapsar.
INVALID_UIA_NAMES = frozenset({
    "guide-service",
    "main",
    "click_fallback",
    "groupcontrol",
    "panecontrol",
    "documentcontrol",
})

# Browsers reconocidos por nombre de proceso.
_BROWSER_PROCS = ("chrome.exe", "msedge.exe", "brave.exe", "opera.exe")
_BROWSER_PROC_HINTS = ("chrome", "msedge", "brave", "opera")

# Títulos de profile picker (multi-idioma).
_PROFILE_PICKER_TITLE_HINTS = (
    "quién eres",
    "who are you",
    "elige tu perfil",
    "choose a profile",
    "select profile",
)

# Indicios de Windows Search en metadata.
#
# Windows 10/11 cambia el proceso de la barra de búsqueda según versión:
#   * SearchHost.exe — Win11 22H2+
#   * SearchApp.exe — Win10
#   * SearchUI.exe — Win10 más viejo (Cortana-era)
#   * StartMenuExperienceHost.exe — botón Windows en la taskbar (también
#     puede capturar texto cuando el menú de inicio se está usando como
#     buscador).
_WINSEARCH_PROC_HINTS = (
    "searchhost",
    "searchapp",
    "searchui",
    "startmenuexperiencehost",
)
_WINSEARCH_TITLE_HINTS = (
    "búsqueda",
    "busqueda",   # sin acento
    "search",
    "buscar",
    "windows search",
    "menú inicio",
    "menu inicio",
    "start",
)

# Apps típicas que la sesión de winsearch puede mencionar.
_KNOWN_APPS = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "firefox": "firefox",
    "edge": "edge",
    "microsoft edge": "edge",
    "msedge": "edge",
    "brave": "brave",
    "opera": "opera",
}


# ──────────────────────────────────────────────────────────────────────
# Modelos del plan
# ──────────────────────────────────────────────────────────────────────

@dataclass
class CollapsedStep:
    """Paso semántico compacto producto del Intent Collapse Engine.

    Campos:
        type: ``open_app`` | ``select_profile`` | ``open_new_tab`` |
              ``open_url`` | ``search_youtube`` | ``scroll_results``.
        params: parámetros canónicos del step (ver :func:`collapse_intent`).
        confidence: 0.0–1.0 — calculada por intención, no por evento.
        needs_user_label: ``True`` si la única forma de elevar la
            confianza es preguntando al usuario (perfil desconocido).
        label_prompt: pregunta humana para Quick Reinforcement.
        human_label: descripción para mostrar en Mission Review (vista
            simple).
        evidence: pequeño dict de telemetría (qué señales dispararon
            este step).
    """

    type: str
    params: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    needs_user_label: bool = False
    label_prompt: str = ""
    human_label: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "type": self.type,
            "params": dict(self.params),
            "confidence": round(float(self.confidence), 3),
        }
        if self.human_label:
            d["human_label"] = self.human_label
        if self.needs_user_label:
            d["needs_user_label"] = True
            if self.label_prompt:
                d["label_prompt"] = self.label_prompt
        return d


@dataclass
class CollapsedBlock:
    """Bloque humano de la misión (lo que el usuario ve en vista simple)."""

    name: str
    steps: List[CollapsedStep] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class RepairQuestion:
    """Pregunta máxima 1–2 que la UI puede mostrar al usuario.

    No es un bloqueante: si el usuario responde, el step gana confianza;
    si no, la misión sigue como ``NEEDS_REVIEW``.
    """

    code: str
    prompt: str
    step_type: str = ""
    block_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "prompt": self.prompt,
            "step_type": self.step_type,
            "block_name": self.block_name,
        }


class CollapseStatus:
    """Constantes de estado del plan.

    PRD 2026-05-07c §C: el estado terminal de producto es ``READY``.
    Lo emite el plan semántico cuando TODOS los invariantes se cumplen
    (perfil no vacío, query con fidelity ok, sin contaminación legacy
    dentro del plan, sin /temp/, etc.). Mantenemos ``EXECUTABLE`` como
    alias histórico para no romper código existente.
    """

    READY = "READY"
    EXECUTABLE = "EXECUTABLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    COMPILED_DRAFT = "COMPILED_DRAFT"


@dataclass
class MissionIntentPlan:
    """Plan final que el engine devuelve.

    Atributos:
        status: ``EXECUTABLE`` | ``NEEDS_REVIEW`` | ``COMPILED_DRAFT``.
        blocks: bloques humanos en orden canónico.
        semantic_steps: lista plana de pasos (espejo de los blocks).
        confidence_avg: media [0, 1] de las confianzas individuales.
        repair_questions: máximo 2 — solo se generan cuando hay duda
            humana real.
    """

    status: str = CollapseStatus.COMPILED_DRAFT
    blocks: List[CollapsedBlock] = field(default_factory=list)
    semantic_steps: List[CollapsedStep] = field(default_factory=list)
    confidence_avg: float = 0.0
    repair_questions: List[RepairQuestion] = field(default_factory=list)
    coverage_report: Optional["CoverageReport"] = None

    @property
    def repair_count(self) -> int:
        return len(self.repair_questions)

    @property
    def step_count(self) -> int:
        return len(self.semantic_steps)

    def as_dict(self) -> Dict[str, Any]:
        d = {
            "status": self.status,
            "confidence_avg": round(float(self.confidence_avg), 3),
            "repair_count": self.repair_count,
            "blocks": [b.to_dict() for b in self.blocks],
            "semantic_steps": [s.to_dict() for s in self.semantic_steps],
            "repair_questions": [q.to_dict() for q in self.repair_questions],
        }
        if self.coverage_report is not None:
            d["coverage_report"] = self.coverage_report.to_dict()
        return d


# ──────────────────────────────────────────────────────────────────────
# Detección de contexto
# ──────────────────────────────────────────────────────────────────────

@dataclass
class _Context:
    """Estado lógico que compone el engine al recorrer el trace."""

    chrome_opened: bool = False
    profile_picker_seen: bool = False
    profile_name_ocr: Optional[str] = None
    # Observación humana del flow ("Click en perfil que dice 'Daniel
    # Chrome D Daniel Arturo Ramos'"). Puede venir del raw_event
    # metadata (``human_observed_label`` / ``user_action_label``) o
    # de ``mission.user_observations`` cuando el caller las inyecta.
    profile_human_observation: Optional[str] = None
    # Window title del browser TRAS seleccionar perfil — Chrome lo
    # rellena con ``"<perfil> - Google Chrome"`` y es una de las
    # fuentes que el ``profile_resolver`` consulta.
    profile_window_title: Optional[str] = None
    # Precapture OCR collection metadata (Chrome profile tiles, etc.)
    ocr_collection_meta: Optional[Dict[str, Any]] = None
    new_tab_seen: bool = False
    new_tab_method: str = "ctrl_t"
    youtube_visited: bool = False
    youtube_url: Optional[str] = None
    youtube_bookmark_seen: bool = False
    first_browser: str = ""
    has_winsearch_session: bool = False
    winsearch_text: str = ""
    has_results_after_search: bool = False
    scroll_ticks_after_search: int = 0
    # ── Fallbacks sin field_sessions (PRD 2026-05-06d §6, §7) ─────
    # Cuando el field_session_manager no logra construir sesiones (típico
    # en grabaciones sin metadata UIA enriquecida), guardamos las
    # señales en bruto aquí y los detectores se apoyan en ellas.
    fallback_winsearch_text: str = ""        # tipeado en SearchUI/Host
    fallback_youtube_query: str = ""         # tipeado en chrome.exe + YouTube
    fallback_youtube_query_submitted: bool = False
    fallback_youtube_query_event_idx: int = -1
    fallback_youtube_results_title_seen: bool = False
    # Conteo de scrolls SOLO en chrome.exe + ventana YouTube. Actúa como
    # fallback de ``scroll_ticks_after_search`` cuando no hubo sesión
    # de búsqueda construida por field_session_manager.
    fallback_youtube_scroll_ticks: int = 0
    # PCOF fuerte en trace: ICE no emite select_profile legacy vacío.
    strong_pcof_entity_resolved: bool = False
    # Arrastre accidental en launcher de búsqueda antes de type_text.
    search_launcher_focus_trace: bool = False
    # Coverage audit (PRD 2026-05-06d §4): set de event_id raw que el
    # engine considera "importantes" (con peso semántico). Al final, el
    # ``_classify_status`` exige que TODOS estos estén cubiertos por
    # algún step semántico. Si alguno queda sin cobertura, status no
    # puede ser EXECUTABLE.
    important_event_ids: set = field(default_factory=set)
    # Mapa event_id → "covered_by:<type>" / "discarded:<reason>" /
    # "needs_review". Solo informativo (lo expone el plan vía to_dict).
    coverage_map: Dict[str, str] = field(default_factory=dict)


def _proc_of(ev: RawEvent) -> str:
    if not ev.window_context:
        return ""
    return (ev.window_context.process_name or "").lower()


def _title_of(ev: RawEvent) -> str:
    if not ev.window_context:
        return ""
    return (ev.window_context.title or "").lower()


def _is_browser_proc(proc: str) -> bool:
    return any(h in proc for h in _BROWSER_PROC_HINTS)


def _normalize_browser(proc: str) -> str:
    for hint, name in (
        ("chrome", "chrome"),
        ("msedge", "edge"),
        ("brave", "brave"),
        ("opera", "opera"),
    ):
        if hint in proc:
            return name
    return "chrome"


def _uia_data(ev: RawEvent) -> Dict[str, Any]:
    meta = ev.metadata or {}
    uia = meta.get("uia_data") or meta.get("uia") or meta.get("desktop_uia") or {}
    return uia if isinstance(uia, dict) else {}


def _web_data(ev: RawEvent) -> Dict[str, Any]:
    meta = ev.metadata or {}
    web = meta.get("web_data") or meta.get("web") or {}
    return web if isinstance(web, dict) else {}


def _vision_data(ev: RawEvent) -> Dict[str, Any]:
    meta = ev.metadata or {}
    vis = meta.get("vision_data") or meta.get("vision") or {}
    return vis if isinstance(vis, dict) else {}


def _is_winsearch_event(ev: RawEvent) -> bool:
    """¿El evento ocurrió en la barra de Windows Search?"""
    proc = _proc_of(ev)
    title = _title_of(ev)
    if any(h in proc for h in _WINSEARCH_PROC_HINTS):
        return True
    # Algunos sistemas reportan los clicks del botón Inicio bajo
    # ``explorer.exe`` (taskbar) con título de búsqueda; aceptamos
    # cualquier proc + título de búsqueda.
    if title and any(h in title for h in _WINSEARCH_TITLE_HINTS):
        return True
    uia = _uia_data(ev)
    name = str(uia.get("name") or "").lower()
    aid = str(uia.get("automation_id") or "").lower()
    if "searchtextbox" in aid or ("search" in aid and "text" in aid):
        return True
    if "escribe aquí para buscar" in name or "type here to search" in name:
        return True
    return False


def _is_keyboard_text_event(ev: RawEvent) -> bool:
    return ev.event_type == EventType.KEYBOARD_TYPE_TEXT


def _is_enter_event(ev: RawEvent) -> bool:
    if ev.event_type != EventType.KEYBOARD_KEY_PRESS:
        return False
    if not ev.keyboard_action:
        return False
    keys = [str(k).lower() for k in (ev.keyboard_action.keys or [])]
    return "enter" in keys or "return" in keys


def _typed_text_of(ev: RawEvent) -> str:
    """Extrae el texto tipeado de un ``KEYBOARD_TYPE_TEXT`` event.

    Soporta múltiples shapes históricos:
      * Nevlan V2 actual: ``metadata.final_text`` (preferido — incluye
        correcciones del usuario).
      * Sanitizer V2 mid: ``metadata.text`` / ``metadata.value`` /
        ``metadata.typed_text``.
      * Recorders antiguos: ``keyboard_action.keys`` (lista de chars).
      * ``metadata.field_commit.user_typed_text`` — fuente de verdad
        del Field Commit V2 (lo que el usuario tipeó tecla por tecla,
        independiente del valor leído por UIA/DOM).

    PRD 2026-05-07c §B (Query Fidelity): preferimos la fuente que
    contenga MÁS tokens. La heurística cubre el caso real donde
    UIA leyó "devuelveme el amor luis miguel" pero el buffer del
    usuario tenía la frase completa con "de" — el sistema NO puede
    perder palabras pequeñas como artículos o preposiciones.
    """
    meta = ev.metadata or {}
    candidates: List[str] = []

    # 1) Buffer del usuario (intención humana — máxima prioridad para
    #    fidelidad). Mira el field_commit.user_typed_text del recorder.
    fc = meta.get("field_commit")
    if isinstance(fc, dict):
        utt = fc.get("user_typed_text")
        if isinstance(utt, str) and utt.strip():
            candidates.append(utt.strip())

    # 2) Lectura DOM/UIA del valor final del campo.
    for k in ("final_text", "text", "value", "typed_text"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())

    # 3) Reconstrucción cruda desde keyboard_action (recorder legacy o
    #    fallback cuando no hay metadata enriquecida).
    if ev.keyboard_action and ev.keyboard_action.keys:
        joined = "".join(str(k) for k in ev.keyboard_action.keys)
        if joined.strip():
            candidates.append(joined.strip())

    if not candidates:
        return ""
    # Devolvemos la versión con MÁS palabras; si empatan, la más
    # larga; si todo empata, la primera (que ya es la más prioritaria).
    # Iteramos manualmente para que el desempate respete el orden
    # original de prioridad (max() con tuplas se queda con el primer
    # máximo encontrado, pero necesitamos un control explícito).
    best = candidates[0]
    best_score = (len(best.split()), len(best))
    for c in candidates[1:]:
        score = (len(c.split()), len(c))
        if score > best_score:
            best = c
            best_score = score
    return best


def _is_winsearch_session(sess: FieldSession) -> bool:
    """¿La sesión de campo ocurrió en Windows Search?"""
    if sess.field_label == "windows_search":
        return True
    proc = (sess.window.process_name or "").lower()
    title = (sess.window.title or "").lower()
    if any(h in proc for h in _WINSEARCH_PROC_HINTS):
        return True
    if "explorer" in proc and any(h in title for h in _WINSEARCH_TITLE_HINTS):
        return True
    return False


def _is_profile_picker_window(ev: RawEvent) -> bool:
    proc = _proc_of(ev)
    title = _title_of(ev)
    if not _is_browser_proc(proc):
        return False
    if any(h in title for h in _PROFILE_PICKER_TITLE_HINTS):
        return True
    # Chrome 122+ pone el profile picker como ventana con título plano
    # ``"Google Chrome"`` (sin barra de URL ni nombre de perfil aún).
    # Si vemos chrome.exe + título exactamente ``"Google Chrome"`` o
    # ``"Chrome"``, lo tratamos como picker.
    base_titles = ("google chrome", "chrome", "microsoft edge", "edge")
    if title.strip() in base_titles:
        return True
    return False


def _is_youtube_results_title(title: str) -> bool:
    """¿El título de ventana es la página de resultados de YouTube?

    Patrón: ``"<query> - YouTube - Google Chrome"`` (Chrome) o
    ``"<query> - YouTube - Microsoft Edge"`` (Edge).
    """
    if not title:
        return False
    low = title.lower()
    if "youtube" not in low:
        return False
    # Resultados típicos: hay un guion ANTES de "youtube" (que separa la
    # query del nombre del sitio).
    pre_youtube = low.split("youtube", 1)[0]
    return "-" in pre_youtube and len(pre_youtube.strip(" -")) > 0


def _ocr_around(ev: RawEvent) -> str:
    vis = _vision_data(ev)
    return str(vis.get("ocr_text_around") or vis.get("nearest_text_anchor") or "").strip()


def _extract_profile_from_ocr(text: str) -> str:
    """De un OCR cercano, intenta extraer el nombre de perfil.

    Heurística: las primeras 1-3 palabras con mayúscula inicial. Filtra
    palabras de UI ("Sincronizar", "Cuenta", etc.).
    """
    if not text:
        return ""
    BLOCKLIST = {
        "Sincronizar", "Cuenta", "Account", "Perfil", "Profile",
        "Iniciar", "Sign", "Cerrar", "Sesión", "Opciones",
    }
    cand: List[str] = []
    for tok in text.split():
        t = tok.strip(",.;:")
        if not t:
            continue
        if t in BLOCKLIST:
            continue
        if t[0].isupper() and t[1:2].islower():
            cand.append(t)
        if len(cand) >= 3:
            break
    return " ".join(cand).strip(",.;:")


def _extract_profile_from_window_title(title: str) -> str:
    """Extrae nombre del perfil del título de Chrome.

    Patrón aceptado: ``"Nombre Apellido - Google Chrome"`` con el
    "Nombre Apellido" en formato persona (1–4 tokens, primera letra
    mayúscula, sin conectores en minúscula).

    Devuelve ``""`` para todo lo que no encaja: títulos de pestañas
    (``"Avances de Nevlan - Google Chrome"``), sitios cargados
    (``"YouTube - Google Chrome"``), ``"Nueva pestaña - Google Chrome"``,
    ventana del picker (``"Google Chrome"``), etc.

    Conservador a propósito: preferimos un falso negativo (``""`` →
    ``needs_user_label``) a un falso positivo (etiquetar como perfil
    el título de una pestaña).
    """
    if not title:
        return ""
    t = title
    for sep in (" - Google Chrome", " - Chrome", " - Microsoft Edge", " - Edge"):
        if sep in t:
            t = t.split(sep)[0].strip()
            break
    if t == title:
        return ""
    low = t.lower()
    if not low:
        return ""
    if low.startswith("nueva pestaña") or low.startswith("new tab"):
        return ""
    # Títulos del profile picker (multi-idioma) — NO son nombres.
    if any(h in low for h in _PROFILE_PICKER_TITLE_HINTS):
        return ""
    # Sitios típicos cargados — el título no es el nombre de perfil.
    site_hints = (
        "youtube", "google", "facebook", "twitter", "github",
        "x.com", "instagram", "gmail", "drive", "outlook",
    )
    if any(h in low for h in site_hints):
        return ""
    # Conectores que delatan que el título es una frase de pestaña
    # (ej. "Avances de Nevlan"), no un nombre de persona.
    connectors = {
        "de", "del", "la", "el", "los", "las", "y", "para", "por",
        "en", "con", "sin", "un", "una", "unos", "unas",
        "of", "the", "and", "for", "with", "to", "in", "on", "at",
        "is", "are", "vs", "vs.",
    }
    tokens = [tok for tok in t.replace(",", " ").split() if tok]
    if not (1 <= len(tokens) <= 4):
        return ""
    for tok in tokens:
        # Cada token tiene que parecer un nombre propio: empieza por
        # mayúscula y no es un conector minúscula.
        if tok.lower() in connectors:
            return ""
        if not tok[0].isupper():
            return ""
        # Aceptamos guiones (apellidos compuestos) y apóstrofes.
        clean = tok.replace("-", "").replace("'", "").replace("´", "")
        if not clean.isalpha():
            return ""
    return t


def _build_context(
    raw_trace: List[RawEvent],
    field_sessions: List[FieldSession],
    *,
    mission_description: str = "",
    mission_user_observations: Optional[List[str]] = None,
) -> _Context:
    """Recorre el trace y compone el contexto lógico para los detectores.

    ``mission_description`` y ``mission_user_observations`` son canales
    opcionales para inyectar texto que el recorder no cazó: si el
    usuario escribió "Click en perfil 'Daniel Arturo Ramos'" en la
    descripción de la misión o agregó un tag ``profile:Daniel Arturo
    Ramos``, lo aprovechamos como una observación humana adicional
    para el resolver del perfil.
    """
    ctx = _Context()
    # Pre-llenamos profile_human_observation con la primera observación
    # mission-level disponible (si la hay). Los eventos del raw_trace
    # podrán sobrescribirla solo si traen una observación más
    # específica.
    if mission_user_observations:
        for obs in mission_user_observations:
            if obs and obs.strip():
                ctx.profile_human_observation = obs.strip()
                break
    if not ctx.profile_human_observation and mission_description:
        # Heurística simple: si la descripción menciona "perfil" o
        # "profile" Y contiene un nombre propio, lo usamos.
        desc = mission_description.strip()
        low = desc.lower()
        if "perfil" in low or "profile" in low:
            ctx.profile_human_observation = desc

    # Winsearch session (camino feliz: hay field_sessions ricos).
    for sess in field_sessions:
        if _is_winsearch_session(sess) and sess.final_text.strip():
            ctx.has_winsearch_session = True
            ctx.winsearch_text = sess.final_text.strip()
            break

    saw_browser = False
    last_browser_title = ""
    last_url = ""
    seen_search_yt = False
    after_search_idx = -1
    sessions_by_id = {sid: s for s in field_sessions for sid in s.raw_event_ids}

    # ── Fallback: tipos en winsearch sin field_session ──────────────
    # Buscamos cualquier KEYBOARD_TYPE_TEXT cuyo evento ocurra en una
    # ventana de Windows Search.
    for ev in raw_trace:
        if not _is_keyboard_text_event(ev):
            continue
        if _is_winsearch_event(ev):
            txt = _typed_text_of(ev)
            if txt:
                ctx.fallback_winsearch_text = txt
                # Marca el evento como importante para coverage audit.
                ctx.important_event_ids.add(ev.id)
                break

    # ── Fallback: tipo + Enter dentro de chrome.exe + YouTube ──────
    #
    # El sanitizer puede emitir varios ``KEYBOARD_TYPE_TEXT`` para la
    # misma sesión de tipeo (UIA lee el field varias veces y commitea
    # múltiples final_text). El primero suele tener un fragmento
    # corto ("devu"), el último tiene la query completa
    # ("devuelveme el amor luis miguel"), incluso si ese último evento
    # llega DESPUÉS del Enter (commit post-submit por re-lectura).
    #
    # Estrategia (PRD §6 — search finalizer):
    #   1. Detectar el Enter en chrome.exe + YouTube.
    #   2. Recolectar TODOS los KEYBOARD_TYPE_TEXT en chrome.exe +
    #      YouTube (antes y después del Enter).
    #   3. Quedarnos con el ``final_text`` más largo de la misma sesión
    #      (mismo ``field_session`` id si hay metadata, sino global).
    enter_event_idx = -1
    enter_event_id = ""
    text_events_in_youtube: List[Tuple[int, str, str]] = []  # (idx, text, sess_id)
    for i, ev in enumerate(raw_trace):
        proc = _proc_of(ev)
        title = _title_of(ev)
        if not _is_browser_proc(proc):
            continue
        if "youtube" not in title:
            continue
        if _is_keyboard_text_event(ev):
            txt = _typed_text_of(ev)
            if txt:
                # Usa metadata.field_session si existe (string id) para
                # agrupar fragmentos de la misma sesión.
                meta = ev.metadata or {}
                sess_id = ""
                fs = meta.get("field_session")
                if isinstance(fs, dict):
                    sess_id = str(fs.get("id") or fs.get("session_id") or "")
                elif isinstance(fs, str):
                    sess_id = fs
                text_events_in_youtube.append((i, txt, sess_id))
                ctx.important_event_ids.add(ev.id)
        elif _is_enter_event(ev) and enter_event_idx < 0:
            enter_event_idx = i
            enter_event_id = ev.id

    if enter_event_idx >= 0 and text_events_in_youtube:
        # Tomamos los textos cuya sess_id coincide con la sesión más
        # cercana (anterior o siguiente al Enter), o si no hay
        # sess_id, tomamos todos.
        anchor = min(
            text_events_in_youtube,
            key=lambda t: abs(t[0] - enter_event_idx),
        )
        anchor_sess = anchor[2]
        if anchor_sess:
            same_sess = [
                txt for (_, txt, sid) in text_events_in_youtube
                if sid == anchor_sess
            ]
        else:
            same_sess = [txt for (_, txt, _) in text_events_in_youtube]
        # PRD 2026-05-07c §B (Query Fidelity): priorizamos la versión
        # con MÁS palabras (que conserva tokens cortos como "de",
        # "la", "el"), no solo la más larga en chars. Empate por
        # palabras → la más larga (suele tener acentos preservados).
        final_query = ""
        if same_sess:
            best = same_sess[0]
            best_score = (len(best.split()), len(best))
            for txt in same_sess[1:]:
                score = (len(txt.split()), len(txt))
                if score > best_score:
                    best = txt
                    best_score = score
            final_query = best
        if final_query:
            ctx.fallback_youtube_query = final_query
            ctx.fallback_youtube_query_submitted = True
            ctx.fallback_youtube_query_event_idx = enter_event_idx
            ctx.important_event_ids.add(enter_event_id)

    # ── Fallback: scrolls posteriores a la búsqueda en YouTube ─────
    after_query = (
        ctx.fallback_youtube_query_event_idx
        if ctx.fallback_youtube_query_submitted
        else -1
    )
    for i, ev in enumerate(raw_trace):
        if ev.event_type != EventType.MOUSE_SCROLL:
            continue
        proc = _proc_of(ev)
        title = _title_of(ev)
        if not _is_browser_proc(proc):
            continue
        if "youtube" not in title:
            continue
        if after_query >= 0 and i <= after_query:
            continue
        meta = ev.metadata or {}
        try:
            count = int(meta.get("count") or 1)
        except Exception:
            count = 1
        ctx.fallback_youtube_scroll_ticks += max(1, count)
        ctx.important_event_ids.add(ev.id)
        # Si el título trae la query (ej. "<query> - YouTube") es señal
        # extra de que estamos en results.
        if _is_youtube_results_title(title):
            ctx.fallback_youtube_results_title_seen = True

    for i, ev in enumerate(raw_trace):
        proc = _proc_of(ev)
        title = _title_of(ev)
        # Cualquier evento en chrome.exe / msedge.exe / etc. cuenta como
        # "browser activo" — no exigimos un WINDOW_ACTIVATE explícito.
        if _is_browser_proc(proc):
            ctx.chrome_opened = True
            if not ctx.first_browser:
                ctx.first_browser = _normalize_browser(proc)
            saw_browser = True
            last_browser_title = title
            if _is_profile_picker_window(ev):
                ctx.profile_picker_seen = True
                ctx.important_event_ids.add(ev.id)
        if ev.event_type == EventType.WINDOW_ACTIVATE and _is_browser_proc(proc):
            if _is_profile_picker_window(ev):
                ctx.profile_picker_seen = True

        # Profile picker click — el sanitizer NO lo descarta porque
        # genera mucho ruido sin contexto, así que lo detectamos aquí.
        if ev.event_type in (
            EventType.MOUSE_CLICK,
            EventType.MOUSE_DOUBLE_CLICK,
        ) and _is_profile_picker_window(ev):
            ctx.profile_picker_seen = True
            # Intenta nombre por OCR cercano.
            if not ctx.profile_name_ocr:
                cand_ocr = _extract_profile_from_ocr(_ocr_around(ev))
                # PRD 2026-05-08d §A.2: filtrar OCR contaminado por
                # contenido web (e.g. snapshot capturado tarde
                # cuando ya se ve YouTube atrás del picker).
                if cand_ocr:
                    from app.services.missions.profile_resolver import (
                        is_invalid_profile_evidence,
                    )
                    if not is_invalid_profile_evidence(cand_ocr):
                        ctx.profile_name_ocr = cand_ocr
            # Precapture ring OCR candidates (weak UIA / profile tiles).
            if not ctx.profile_name_ocr:
                try:
                    from app.services.missions.collection_click_semantic import (
                        build_collection_semantic_step,
                    )
                    mx = my = None
                    if ev.mouse_action:
                        mx, my = ev.mouse_action.x, ev.mouse_action.y
                    col = build_collection_semantic_step(
                        ev.metadata or {},
                        click_xy=(mx, my) if mx is not None else None,
                    )
                    if col:
                        lbl = str(col.get("params", {}).get("label_text") or "")
                        if lbl:
                            ctx.profile_name_ocr = lbl
                            ctx.ocr_collection_meta = col.get("params")
                except Exception:
                    pass
            # PRD 2026-05-07c §A: capturar observación humana del flow.
            # Aceptamos cualquier label adjunto al click (el recorder
            # puede haberlo extraído del UIA del item del picker —
            # "Daniel Chrome D Daniel Arturo Ramos" — o de una
            # anotación del usuario). El profile_resolver decidirá
            # cómo extraer el canonical.
            if not ctx.profile_human_observation:
                meta_obs = (ev.metadata or {})
                obs = (
                    meta_obs.get("human_observed_label")
                    or meta_obs.get("user_action_label")
                    or meta_obs.get("profile_picker_item_text")
                )
                if not obs:
                    uia = _uia_data(ev)
                    n = str(uia.get("name") or "").strip()
                    # Nombres UIA basura típicos (GroupControl,
                    # video-stream, html5-main-video) los descartamos
                    # — no son observaciones humanas.
                    if n and n.lower() not in INVALID_UIA_NAMES \
                            and not n.lower().startswith(("video-stream",
                                                          "html5",
                                                          "group")):
                        obs = n
                # PRD 2026-05-08d §A.2: barrera dura — descartamos
                # cualquier observación que claramente sea contenido
                # web/Polymer (``ytd-*``, ``rendering-content``,
                # ``style-scope``, "YouTube", " - Google Chrome").
                # El recorder a veces captura el UIA POSTERIOR al
                # click del profile picker (porque el picker se
                # cierra demasiado rápido) y termina trayendo el
                # ad layout / guide service de YouTube. Eso jamás
                # es nombre de perfil.
                if isinstance(obs, str) and obs.strip():
                    from app.services.missions.profile_resolver import (
                        is_invalid_profile_evidence,
                    )
                    if is_invalid_profile_evidence(obs):
                        log.debug(
                            f"[collapse_engine] rechazo profile_human_obs"
                            f" inválida='{obs}'"
                        )
                    else:
                        ctx.profile_human_observation = obs.strip()

        # Profile picker by window title cambio (Chrome ya seleccionado).
        # Aceptamos el title como evidencia de perfil SOLO si pasa el
        # filtro conservador ``_extract_profile_from_window_title``
        # (que rechaza títulos de pestañas, sitios cargados como
        # YouTube/Google/etc, y "Nueva pestaña"). Así evitamos que el
        # ``window_title`` de la ventana de YouTube acabe filtrándose
        # como "perfil = YouTube".
        if (
            ev.event_type == EventType.WINDOW_ACTIVATE
            and _is_browser_proc(proc)
        ):
            cand_title = ev.window_context.title or ""
            if cand_title and not _is_profile_picker_window(ev):
                cand_name = _extract_profile_from_window_title(cand_title)
                # PRD 2026-05-08d §A.2: incluso si el extractor de
                # window_title cree haber sacado un nombre, lo
                # validamos contra la blacklist de evidencia inválida.
                if cand_name:
                    from app.services.missions.profile_resolver import (
                        is_invalid_profile_evidence,
                    )
                    if is_invalid_profile_evidence(cand_name) \
                            or is_invalid_profile_evidence(cand_title):
                        cand_name = ""
                if cand_name:
                    ctx.profile_window_title = cand_title
                    if not ctx.profile_name_ocr:
                        ctx.profile_name_ocr = cand_name

        # New tab via Ctrl+T
        if (
            ev.event_type == EventType.KEYBOARD_HOTKEY
            and ev.keyboard_action
        ):
            keys = [str(k).lower() for k in ev.keyboard_action.keys]
            mods = [str(m).lower() for m in ev.keyboard_action.modifiers]
            if "t" in keys and ("ctrl" in mods or "control" in mods):
                ctx.new_tab_seen = True
                ctx.new_tab_method = "ctrl_t"
        # New tab via title "Nueva pestaña"
        if (
            ev.event_type == EventType.WINDOW_ACTIVATE
            and _is_browser_proc(proc)
            and ("nueva pestaña" in title or "new tab" in title)
        ):
            ctx.new_tab_seen = True

        # New tab via UIA name "Nueva pestaña" / "New tab"
        if ev.event_type in (
            EventType.MOUSE_CLICK,
            EventType.MOUSE_DOUBLE_CLICK,
        ) and _is_browser_proc(proc):
            uia = _uia_data(ev)
            n = str(uia.get("name") or "").lower()
            if "nueva pestaña" in n or "new tab" in n or n == "+":
                ctx.new_tab_seen = True
                ctx.new_tab_method = "click"
            # Fallback: si el title de la ventana es exactamente
            # "Nueva pestaña - Google Chrome" o "New Tab - Google
            # Chrome", el click ocurrió en una pestaña vacía recién
            # abierta. Suficiente señal para inferir open_new_tab.
            elif title.startswith("nueva pestaña") or title.startswith("new tab"):
                ctx.new_tab_seen = True
                ctx.new_tab_method = "click"

        # YouTube visited (window/url/web)
        web = _web_data(ev)
        url_ev = str(web.get("url") or web.get("href") or "").lower()
        if url_ev:
            last_url = url_ev
            if "youtube.com" in url_ev or "youtu.be" in url_ev:
                ctx.youtube_visited = True
                ctx.youtube_url = url_ev
        if "youtube" in title:
            ctx.youtube_visited = True

        # YouTube bookmark click
        if ev.event_type in (
            EventType.MOUSE_CLICK,
            EventType.MOUSE_DOUBLE_CLICK,
        ):
            uia = _uia_data(ev)
            n = str(uia.get("name") or "").lower()
            href = str(web.get("href") or "").lower()
            text_link = str(web.get("text") or "").lower()
            if (
                ("youtube" in n or "youtube" in text_link)
                and ("youtube.com" in href or "youtube.com" in url_ev or "youtube" in n)
            ):
                ctx.youtube_bookmark_seen = True

        # Track search submission (search_youtube anchor for scroll detection)
        if ev.id in sessions_by_id and ev.event_type == EventType.KEYBOARD_KEY_PRESS:
            sess = sessions_by_id[ev.id]
            if sess.submitted and sess.site == "youtube":
                seen_search_yt = True
                after_search_idx = i

        # Scroll después de search
        if seen_search_yt and ev.event_type == EventType.MOUSE_SCROLL and i > after_search_idx:
            ctx.has_results_after_search = True
            count = int((ev.metadata or {}).get("count", 1))
            ctx.scroll_ticks_after_search += max(1, count)

    for i, ev in enumerate(raw_trace):
        if ev.event_type == EventType.MOUSE_DRAG and _is_winsearch_event(ev):
            if i + 1 < len(raw_trace):
                nxt = raw_trace[i + 1]
                if nxt.event_type == EventType.KEYBOARD_TYPE_TEXT:
                    ctx.search_launcher_focus_trace = True
                    break

    try:
        from app.services.runtime.universal_collection_entity_engine import (
            find_strongest_pcof_in_trace,
        )
        from app.services.runtime.pre_click_operational_freeze import (
            should_promote_pcof_freeze,
        )

        class _TraceMission:
            pass

        tm = _TraceMission()
        tm.raw_trace = raw_trace
        fr, _ = find_strongest_pcof_in_trace(tm)
        if fr is not None and should_promote_pcof_freeze(fr):
            ctx.strong_pcof_entity_resolved = True
    except Exception as e:
        log.debug("[collapse_engine] pcof trace scan: %s", e)

    try:
        from app.services.missions.freeze_first_execution_core import (
            coi_from_metadata,
        )
        for ev in raw_trace:
            meta = dict(getattr(ev, "metadata", None) or {})
            coi = coi_from_metadata(meta)
            if coi and coi.canonical_truth:
                ctx.strong_pcof_entity_resolved = True
                break
    except Exception as e:
        log.debug("[collapse_engine] ffec trace scan: %s", e)

    return ctx


# ──────────────────────────────────────────────────────────────────────
# Patrones de colapso
# ──────────────────────────────────────────────────────────────────────

def _detect_open_app(
    raw_trace: List[RawEvent],
    field_sessions: List[FieldSession],
    ctx: _Context,
) -> Optional[CollapsedStep]:
    """Patrón A — Windows Search + Chrome ⇒ open_app."""
    # Caso fuerte: hay una sesión de winsearch con texto + chrome.exe
    # detectada por el field_session_manager.
    winsearch_text = ""
    method_evidence = ""
    if ctx.has_winsearch_session:
        winsearch_text = (ctx.winsearch_text or "").strip()
        method_evidence = "field_session"
    elif ctx.fallback_winsearch_text:
        # Fallback PRD §6: cualquier KEYBOARD_TYPE_TEXT en SearchUI/Host
        # sin field_session, con browser activado después.
        winsearch_text = ctx.fallback_winsearch_text.strip()
        method_evidence = "fallback_typed_in_winsearch"

    if winsearch_text and ctx.chrome_opened:
        text_low = winsearch_text.lower()
        app = ""
        for k, v in _KNOWN_APPS.items():
            if k in text_low:
                app = v
                break
        if not app:
            # Si winsearch tipeó algo y el primer browser visto es
            # chrome.exe, asumimos chrome.
            if ctx.first_browser:
                app = ctx.first_browser
            else:
                app = text_low.split()[0]
        params: Dict[str, Any] = {"app": app, "method": "windows_search"}
        if ctx.search_launcher_focus_trace:
            params["launcher_input_focus"] = True
            params["launcher_action"] = "focus_search_launcher_input"
        return CollapsedStep(
            type="open_app",
            params=params,
            confidence=0.95 if method_evidence == "field_session" else 0.92,
            human_label=f"Abrir {app.title()}",
            evidence={
                "winsearch_text": winsearch_text,
                "method_evidence": method_evidence,
                "search_launcher_focus": ctx.search_launcher_focus_trace,
            },
        )

    # Caso débil pero suficiente: chrome.exe se abrió (profile picker
    # detectado, o primera ventana del trace ya es browser).
    if ctx.profile_picker_seen and ctx.first_browser:
        return CollapsedStep(
            type="open_app",
            params={"app": ctx.first_browser, "method": "implicit"},
            confidence=0.90,
            human_label=f"Abrir {ctx.first_browser.title()}",
            evidence={"reason": "profile_picker_seen"},
        )

    if ctx.chrome_opened and ctx.first_browser:
        # Se abrió chrome explícitamente (window_activate al inicio del
        # trace). El executor deduplicará si ya está abierto.
        if raw_trace and _is_browser_proc(_proc_of(raw_trace[0])):
            return CollapsedStep(
                type="open_app",
                params={"app": ctx.first_browser, "method": "implicit"},
                confidence=0.85,
                human_label=f"Abrir {ctx.first_browser.title()}",
                evidence={"reason": "browser_first_event"},
            )
    return None


def _detect_select_profile(ctx: _Context) -> Optional[CollapsedStep]:
    """Patrón B — Profile picker ⇒ select_profile.

    PRD 2026-05-07c §A: el detector NUNCA debe quedarse en
    ``profile=""``. Usa :func:`profile_resolver.resolve_profile` que
    consulta en orden:
      a) ctx.profile_name_ocr (OCR + window title heurístico legacy)
      b) ctx.profile_human_observation (label adjunto al click —
         "Daniel Chrome D Daniel Arturo Ramos")
      c) ctx.profile_window_title (post-selection)
      d) alias store local (perfiles confirmados antes)

    Si todo falla, dejamos ``needs_user_label=True`` con el mejor
    candidato disponible para que Mission Review lo preseleccione
    (NO regrabar — solo confirmar).
    """
    if not ctx.profile_picker_seen:
        return None
    if ctx.strong_pcof_entity_resolved:
        return None
    # Import diferido para evitar ciclos: profile_resolver no depende
    # del intent_collapse_engine, pero la cadena pasa por contracts.
    try:
        from app.services.missions.profile_resolver import (
            ProfileResolution,
            resolve_profile,
        )
    except Exception as e:
        log.debug(f"[collapse_engine] profile_resolver no disponible: {e}")
        ProfileResolution = None
        resolve_profile = None

    if resolve_profile is not None:
        try:
            res = resolve_profile(
                raw_profile=ctx.profile_name_ocr,
                human_observation=ctx.profile_human_observation,
                window_title=ctx.profile_window_title,
                ocr_observation=ctx.profile_name_ocr,
            )
        except Exception as e:
            log.warning(f"[collapse_engine] profile_resolver falló: {e}")
            res = None
    else:
        res = None

    if res is not None and res.is_resolved and res.profile_name:
        params: Dict[str, Any] = {
            "app": ctx.first_browser or "chrome",
            "profile": res.profile_name,
            "candidate_aliases": list(res.candidate_aliases),
        }
        if ctx.ocr_collection_meta:
            params.update({
                k: v for k, v in ctx.ocr_collection_meta.items()
                if k in (
                    "strategy", "label_text", "precapture_frames",
                    "collection_candidates", "candidates_digest", "bbox_hint",
                )
            })
        return CollapsedStep(
            type="select_profile",
            params=params,
            confidence=0.92 if res.source != "ocr" else 0.88,
            human_label=f"Seleccionar perfil {res.profile_name}",
            evidence={
                "source": res.source,
                "reason": res.reason,
            },
        )
    # Sin canonical — needs_user_label con prompt claro y candidato
    # preseleccionado si lo tenemos.
    candidate = ""
    candidate_aliases: List[str] = []
    if res is not None:
        candidate = res.profile_name or ""
        candidate_aliases = list(res.candidate_aliases)
    elif ctx.profile_name_ocr:
        candidate = ctx.profile_name_ocr
    return CollapsedStep(
        type="select_profile",
        params={
            "app": ctx.first_browser or "chrome",
            "profile": candidate,
            "candidate_aliases": candidate_aliases,
        },
        confidence=0.75 if not candidate else 0.80,
        needs_user_label=True,
        label_prompt=(
            f"¿Qué perfil debo usar? [{candidate}]" if candidate
            else "¿Qué perfil debo usar?"
        ),
        human_label="Seleccionar perfil",
        evidence={
            "source": (res.source if res is not None else "no_evidence"),
            "reason": (res.reason if res is not None else ""),
        },
    )


def _detect_open_new_tab(ctx: _Context) -> Optional[CollapsedStep]:
    """Patrón C — Ctrl+T / botón "+" / UIA "Nueva pestaña" ⇒ open_new_tab."""
    if not ctx.new_tab_seen:
        return None
    return CollapsedStep(
        type="open_new_tab",
        params={
            "app": ctx.first_browser or "chrome",
            "preferred_strategy": ctx.new_tab_method or "ctrl_t",
        },
        confidence=0.90,
        human_label="Abrir nueva pestaña",
        evidence={"method": ctx.new_tab_method},
    )


def _detect_open_url_youtube(
    raw_trace: List[RawEvent],
    field_sessions: List[FieldSession],
    ctx: _Context,
) -> Optional[CollapsedStep]:
    """Patrón D — Bookmark/click YouTube/title YouTube ⇒ open_url."""
    # Si el usuario tipeó la URL en el address bar.
    for sess in field_sessions:
        if sess.field_label == "browser_address_bar" and sess.submitted:
            text = sess.final_text.strip().lower()
            if "youtube.com" in text or "youtu.be" in text:
                return CollapsedStep(
                    type="open_url",
                    params={"url": "youtube.com", "name": "YouTube"},
                    confidence=0.95,
                    human_label="Ir a YouTube",
                    evidence={"source": "address_bar"},
                )
    if ctx.youtube_bookmark_seen or ctx.youtube_visited:
        # Confianza alta si vimos bookmark con web_data, alta también si
        # vimos el título de la ventana cambiar a YouTube (señal de que
        # YouTube efectivamente cargó tras alguna acción).
        return CollapsedStep(
            type="open_url",
            params={"url": "youtube.com", "name": "YouTube"},
            confidence=0.95 if ctx.youtube_bookmark_seen else 0.90,
            human_label="Abrir YouTube",
            evidence={"source": "bookmark" if ctx.youtube_bookmark_seen else "title"},
        )
    return None


# ──────────────────────────────────────────────────────────────────────
# Query Fidelity (PRD 2026-05-07c §B)
# ──────────────────────────────────────────────────────────────────────
#
# "La evidencia puede ser ruidosa. La intención ejecutable debe ser
# limpia." Para queries de búsqueda en YouTube/Google/etc, perder
# palabras pequeñas (preposiciones / artículos) cambia drásticamente el
# resultado:
#
#   "Devuélveme el amor de Luis Miguel"  vs.  "Devuélveme el amor luis miguel"
#
# El sistema NO debe degradar la intención del usuario. Si el buffer
# de tipeo crudo (keyboard.keys) contiene tokens que no están en el
# valor "final" leído por UIA/DOM, marcamos la query como
# ``query_fidelity_suspect``. El ApprovalGate lo bloquea hasta que el
# usuario confirme (NO regrabar — solo confirmar).

# Tokens cortos que JAMÁS deben perderse en una query natural si los
# vimos en alguna fuente. Solo los chequeamos en español/inglés
# básico — son los que la canción de Luis Miguel y otros casos
# análogos exhiben en la práctica.
_QUERY_PROTECTED_TOKENS: frozenset = frozenset({
    # Español
    "de", "del", "la", "el", "los", "las", "y", "a", "al", "mi",
    "tu", "su", "un", "una", "en", "con", "sin", "por", "para",
    # Inglés
    "of", "the", "a", "an", "and", "or", "to", "in", "on", "at",
    "for", "with", "by", "my", "your", "his", "her",
})


def _query_word_set(text: str) -> List[str]:
    """Tokeniza una query para comparación de fidelidad.

    Mantiene el orden y casing del usuario, pero baja a lower y
    quita puntuación común para comparar contra el set protegido.
    """
    if not text:
        return []
    out: List[str] = []
    for raw in text.strip().split():
        clean = raw.strip(",.;:?!\"'()[]{}").lower()
        if clean:
            out.append(clean)
    return out


_NAME_TOKEN_RE = None  # placeholder — defined lazily below


def _looks_like_name_token(tok: str) -> bool:
    """¿``tok`` parece un token de nombre propio (Capitalizado +
    alfa)? Reutiliza la misma heurística que ``profile_resolver``
    pero in-line para evitar import circular pesado.
    """
    if not tok or len(tok) < 2:
        return False
    first = tok[0]
    if not first.isalpha() or not first.isupper():
        return False
    cleaned = tok.replace("-", "").replace("'", "").replace("´", "")
    if not cleaned:
        return False
    return all(c.isalpha() for c in cleaned)


def detect_missing_connector_pattern(
    query: str,
) -> Tuple[bool, Optional[str], str]:
    """Heurística lingüística: detecta si ``query`` muy probablemente
    perdió un conector ("de", "del", "of", "by") entre una palabra
    de contenido y un nombre propio multi-token al final.

    Devuelve ``(is_suspect, suggested_query, suggested_connector)``.

    Ejemplos sospechosos (devuelve True):
        "Devuelveme el amor Luis Miguel"  → falta "de" antes de
            "Luis Miguel" — sugerencia: "Devuelveme el amor de Luis
            Miguel"
        "canción Shakira"                 → "canción de Shakira"
        "video Pedro Almodóvar"           → "video de Pedro Almodóvar"

    NO sospechoso (devuelve False):
        "Devuélveme el amor de Luis Miguel"  (ya tiene "de")
        "Pedro Almodóvar película"           (cap viene primero)
        "Banco del Bienestar 2025"           (termina en número)
        "Luis"                                (1 token solo)
        "Luis Miguel"                         (toda la query es el
                                                nombre)

    Esta función es PURE — el caller decide qué hacer (marcar
    suspect, pedir confirmación, etc.). NO depende de ningún
    nombre concreto del caso original — la heurística es
    completamente genérica.
    """
    if not query:
        return False, None, ""
    tokens = [t for t in query.strip().split() if t]
    if len(tokens) < 3:
        return False, None, ""

    # Encuentra la corrida final consecutiva de tokens-nombre
    # (multi-token capitalizado).
    cap_run_start = -1
    for i in range(len(tokens) - 1, -1, -1):
        if _looks_like_name_token(tokens[i].strip(",.;:?!\"'")):
            cap_run_start = i
        else:
            break
    if cap_run_start < 0:
        return False, None, ""
    if len(tokens) - cap_run_start < 2:
        return False, None, ""  # solo 1 token capitalizado
    if cap_run_start == 0:
        return False, None, ""  # toda la query es el nombre

    prev_tok = tokens[cap_run_start - 1].strip(",.;:?!\"'").lower()
    if not prev_tok:
        return False, None, ""
    # Si ya hay un conector inmediatamente antes, todo bien.
    if prev_tok in _QUERY_PROTECTED_TOKENS:
        return False, None, ""
    # Si el token previo también parece nombre (ej. "Pedro
    # Almodóvar" + algo), no es el patrón típico — probablemente
    # son varios nombres juntos.
    if _looks_like_name_token(tokens[cap_run_start - 1].strip(",.;:?!\"'")):
        return False, None, ""

    # Heurística de idioma: si la query parece ESPAÑOL (acentos o
    # palabras funcionales en español visibles), sugerimos "de";
    # si parece INGLÉS, "of". Default = "de".
    low_full = query.lower()
    es_signals = {"el", "la", "los", "las", "un", "una", "y", "para",
                  "por", "con"}
    en_signals = {"the", "a", "an", "and", "for", "with", "to"}
    es_hits = sum(1 for w in low_full.split() if w in es_signals)
    en_hits = sum(1 for w in low_full.split() if w in en_signals)
    has_accent = any(
        not c.isascii() and c.isalpha() for c in query
    )
    if has_accent or es_hits > en_hits:
        connector = "de"
    elif en_hits > 0:
        connector = "of"
    else:
        connector = "de"

    suggested_tokens = (
        tokens[:cap_run_start]
        + [connector]
        + tokens[cap_run_start:]
    )
    return True, " ".join(suggested_tokens), connector


def assess_query_fidelity(
    chosen_query: str,
    *,
    candidate_queries: Iterable[str] = (),
) -> Tuple[str, str, str]:
    """Compara la query elegida contra otras candidatas y reporta
    pérdidas sospechosas de tokens cortos protegidos.

    Devuelve una tupla ``(best_query, fidelity_status, suspect_token)``
    donde:

      * ``best_query``: la query con MÁS tokens protegidos conservados
        (empate → más palabras totales → más chars). Acentos/casing del
        ganador se respetan.
      * ``fidelity_status``: ``"ok"`` | ``"missing_short_token"`` |
        ``"empty"``.
      * ``suspect_token``: el primer token corto perdido (vacío si
        no hay sospecha).

    Semántica de detección:
        Si la query CHOSEN no contiene un token protegido que SÍ
        aparece en otra candidata → ``missing_short_token``. El caller
        debe ofrecer ``best_query`` como confirmación mínima al
        usuario (NO regrabar).

    Esta función es PURE (sin side-effects) — el caller decide qué
    hacer con la sospecha (bloquear, pedir confirmación, etc.).
    """
    chosen_norm = (chosen_query or "").strip()
    candidates: List[str] = []
    if chosen_norm:
        candidates.append(chosen_norm)
    for c in candidate_queries:
        s = str(c or "").strip()
        if s and s not in candidates:
            candidates.append(s)
    if not candidates:
        return "", "empty", ""

    # Calculamos el "súper conjunto" de tokens protegidos visto en
    # CUALQUIER candidato — esos son los que NO podemos perder.
    union_protected: Dict[str, int] = {}
    by_candidate_tokens: List[Tuple[str, List[str]]] = []
    for cand in candidates:
        toks = _query_word_set(cand)
        by_candidate_tokens.append((cand, toks))
        for t in toks:
            if t in _QUERY_PROTECTED_TOKENS:
                union_protected[t] = union_protected.get(t, 0) + 1

    def _has_accent_or_uppercase(text: str) -> int:
        """Cuenta caracteres "ricos" — acentuadas + mayúsculas. Útil
        como tie-breaker para preferir la versión más fiel a la
        intención del usuario cuando dos candidatas comparten el mismo
        set de palabras (ej. "devuelveme el amor de luis miguel"
        vs "Devuélveme el amor de Luis Miguel").
        """
        rich = 0
        for c in text:
            if c.isupper():
                rich += 1
            elif not c.isascii() and c.isalpha():
                rich += 1
        return rich

    def _score(text: str, toks: List[str]) -> Tuple[int, int, int, int]:
        # Mejor candidato:
        #   1) más tokens protegidos del super-set (no perder "de")
        #   2) más palabras totales
        #   3) más "richness" — mayúsculas + acentos preservadas
        #   4) más chars
        protected_kept = sum(
            1 for t in union_protected.keys() if t in toks
        )
        return (
            protected_kept,
            len(toks),
            _has_accent_or_uppercase(text),
            len(text),
        )

    best_text, best_toks = by_candidate_tokens[0]
    best_score = _score(best_text, best_toks)
    for cand, toks in by_candidate_tokens[1:]:
        s = _score(cand, toks)
        if s > best_score:
            best_text = cand
            best_toks = toks
            best_score = s

    # Sospecha de pérdida: si la query CHOSEN no preserva todos los
    # tokens protegidos vistos en el super-set, marcamos suspect y
    # devolvemos best_text como propuesta de confirmación.
    chosen_toks = _query_word_set(chosen_norm)
    chosen_set = set(chosen_toks)
    missing_token = ""
    for t in union_protected.keys():
        if t not in chosen_set:
            missing_token = t
            break

    if missing_token:
        return best_text, "missing_short_token", missing_token
    return best_text, "ok", ""


def _collect_query_candidates_for_youtube(
    raw_trace: List[RawEvent],
    field_sessions: List[FieldSession],
    ctx: _Context,
) -> List[str]:
    """Reúne TODAS las versiones de la query de YouTube vistas en el
    trace (sesiones, eventos sueltos, fallback ctx). El caller las
    pasa a :func:`assess_query_fidelity` para decidir.
    """
    out: List[str] = []
    for sess in field_sessions:
        if sess.site == "youtube" and sess.final_text.strip():
            out.append(sess.final_text.strip())
    for ev in raw_trace:
        if not _is_keyboard_text_event(ev):
            continue
        proc = _proc_of(ev)
        title = _title_of(ev)
        if not _is_browser_proc(proc):
            continue
        if "youtube" not in title:
            continue
        meta = ev.metadata or {}
        # Buffer del usuario (intención humana).
        fc = meta.get("field_commit") or {}
        if isinstance(fc, dict):
            utt = fc.get("user_typed_text")
            if isinstance(utt, str) and utt.strip():
                out.append(utt.strip())
        # final_text leído por UIA/DOM.
        for k in ("final_text", "text", "value", "typed_text"):
            v = meta.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        # keyboard_action.keys (recorder legacy).
        if ev.keyboard_action and ev.keyboard_action.keys:
            joined = "".join(str(k) for k in ev.keyboard_action.keys)
            if joined.strip():
                out.append(joined.strip())
    if ctx.fallback_youtube_query.strip():
        out.append(ctx.fallback_youtube_query.strip())
    # Dedup preservando orden.
    seen = set()
    uniq: List[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _detect_search_youtube(
    raw_trace: List[RawEvent],
    field_sessions: List[FieldSession],
    ctx: _Context,
) -> Optional[CollapsedStep]:
    """Patrón E — texto + Enter en YouTube ⇒ search_youtube.

    PRD 2026-05-07c §B (Query Fidelity): además de elegir la mejor
    fuente entre sessions/fallback, comparamos TODAS las versiones
    vistas en el trace y nos quedamos con la que MÁS tokens
    conserva. Si detectamos pérdida de tokens cortos protegidos
    ("de", "la", "el"…) marcamos ``needs_user_label=True`` con un
    prompt de confirmación (NO regrabar) que preselecciona la
    versión más completa.
    """
    # Reunimos todas las candidatas para el assessor.
    candidates = _collect_query_candidates_for_youtube(
        raw_trace, field_sessions, ctx,
    )
    if not candidates:
        return None

    # Camino feliz: hay una field_session de YouTube submitted.
    base_text = ""
    base_source = ""
    base_confidence = 0.0
    for sess in field_sessions:
        if sess.site != "youtube":
            continue
        if not sess.submitted:
            continue
        if sess.final_text.strip():
            base_text = sess.final_text.strip()
            base_source = "session_youtube_submitted"
            base_confidence = 0.95
            break

    # Fallback: tipo + Enter en chrome.exe + YouTube.
    if not base_text and ctx.fallback_youtube_query_submitted \
            and ctx.fallback_youtube_query.strip():
        base_text = ctx.fallback_youtube_query.strip()
        base_source = "fallback_type_enter_in_youtube"
        base_confidence = 0.93

    if not base_text:
        return None

    # Aplicamos el fidelity assessor para descubrir cuál es la
    # versión MÁS COMPLETA entre todas las candidatas.
    best, _, _ = assess_query_fidelity(
        base_text, candidate_queries=candidates,
    )
    final_text = best or base_text

    # Re-evaluamos la fidelidad del FINAL elegido — no del base_text.
    # Cuando el buffer (``user_typed_text``) traía la versión completa
    # con "de" + acentos, ``final_text`` ya contiene esa versión, así
    # que el status correcto es "ok" (no necesita confirmación). Solo
    # marcamos suspect cuando NI siquiera el mejor candidato preserva
    # los tokens cortos del super-set.
    _, final_status, suspect = assess_query_fidelity(
        final_text, candidate_queries=candidates,
    )

    needs_label = False
    label_prompt = ""
    evidence: Dict[str, Any] = {
        "source": base_source,
        "fidelity_status": final_status,
        "suspect_token": suspect,
        "candidate_count": len(candidates),
    }
    status = final_status
    if final_status == "missing_short_token":
        # Ni siquiera el mejor candidato tiene el token. Confirmación
        # mínima con la versión más rica preseleccionada (NO regrabar).
        needs_label = True
        label_prompt = (
            f"Detecté pérdida del token \"{suspect}\". "
            f"¿La búsqueda correcta es \"{final_text}\"?"
        )
        evidence["query_fidelity_suspect"] = True
        # Bajamos un poco la confianza pero no bloqueamos
        # definitivamente — es reparable con confirmación.
        base_confidence = max(base_confidence - 0.10, 0.70)
    elif final_status == "ok":
        # PRD 2026-05-08: aunque la asesoría entre candidatas dijo
        # "ok", aplicamos heurística lingüística pura. Si la query
        # parece "<contenido> + <Nombre Apellido>" sin conector
        # ("amor Luis Miguel" → falta "de"), marcamos suspect y
        # ofrecemos la sugerencia. La heurística es genérica y NO
        # depende de ningún string concreto del caso original.
        is_suspect, suggested, connector = detect_missing_connector_pattern(
            final_text,
        )
        if is_suspect and suggested:
            status = "suspect"
            needs_label = True
            label_prompt = (
                f"Buscar en YouTube: \"{final_text}\". "
                f"¿Es correcto o quisiste decir \"{suggested}\"?"
            )
            evidence.update({
                "fidelity_status": "suspect",
                "suspect_pattern": "missing_connector",
                "suspected_missing_connector": connector,
                "suggested_query": suggested,
            })
            base_confidence = max(base_confidence - 0.15, 0.65)

    cs_params: Dict[str, Any] = {
        "query": final_text,
        "submit": True,
        "target": "youtube_search_box",
        "locators": youtube_search_locators(),
        "candidate_queries": candidates,
        "query_fidelity_status": status,
    }
    # Propagar la sugerencia heurística para Mission Review.
    if evidence.get("suggested_query"):
        cs_params["suggested_query"] = evidence["suggested_query"]
    if evidence.get("suspected_missing_connector"):
        cs_params["suspected_missing_connector"] = (
            evidence["suspected_missing_connector"]
        )
    return CollapsedStep(
        type="search_youtube",
        params=cs_params,
        confidence=base_confidence,
        needs_user_label=needs_label,
        label_prompt=label_prompt,
        human_label=f"Buscar en YouTube: {final_text}",
        evidence=evidence,
    )


def _detect_scroll_results(ctx: _Context) -> Optional[CollapsedStep]:
    """Patrón F — scroll después de resultados ⇒ scroll_results.

    Camino feliz: el field_session_manager detectó submission y luego
    el _build_context contó scrolls. Camino fallback (PRD §7):
    cualquier ``MOUSE_SCROLL`` en ``chrome.exe`` con título YouTube,
    posterior al evento ``Enter`` que disparó la búsqueda.
    """
    ticks = max(
        int(ctx.scroll_ticks_after_search or 0),
        int(ctx.fallback_youtube_scroll_ticks or 0),
    )
    has_results = (
        ctx.has_results_after_search
        or ctx.fallback_youtube_results_title_seen
        or ctx.fallback_youtube_query_submitted
    )
    if not has_results or ticks <= 0:
        return None
    return CollapsedStep(
        type="scroll_results",
        params={
            "direction": "down",
            "amount": int(ticks),
            "context": "youtube_results",
        },
        confidence=0.85,
        human_label="Bajar resultados",
        evidence={
            "ticks": ticks,
            "source": (
                "field_session" if ctx.has_results_after_search
                else "fallback_scroll_in_youtube"
            ),
        },
    )


# ──────────────────────────────────────────────────────────────────────
# Block builder
# ──────────────────────────────────────────────────────────────────────

def _build_blocks(steps: List[CollapsedStep]) -> List[CollapsedBlock]:
    """Agrupa los pasos en bloques humanos canónicos.

    Orden:
        Bloque 1 "Abrir Chrome"        [open_app, select_profile]
        Bloque 2 "Abrir YouTube"       [open_new_tab, open_url]
        Bloque 3 "Buscar en YouTube"   [search_youtube]
        Bloque 4 "Explorar resultados" [scroll_results]
    """
    by_type: Dict[str, List[CollapsedStep]] = {}
    for s in steps:
        by_type.setdefault(s.type, []).append(s)

    blocks: List[CollapsedBlock] = []

    # Bloque 1.
    b1: List[CollapsedStep] = []
    b1.extend(by_type.get("open_app", []))
    b1.extend(by_type.get("select_profile", []))
    if b1:
        # Si no hay chrome explícitamente, llamar el bloque por la app.
        first = b1[0].params.get("app", "chrome") if b1 else "chrome"
        title = f"Abrir {str(first).title()}" if first else "Abrir aplicación"
        blocks.append(CollapsedBlock(name=title, steps=b1))

    # Bloque 2.
    b2: List[CollapsedStep] = []
    b2.extend(by_type.get("open_new_tab", []))
    b2.extend(by_type.get("open_url", []))
    if b2:
        # Etiqueta basada en el destino — YouTube / generic.
        url_step = next((s for s in b2 if s.type == "open_url"), None)
        dest = "destino"
        if url_step:
            dest = str(url_step.params.get("name") or url_step.params.get("url") or "destino")
            # Normaliza a "YouTube" (no "youtube.com")
            if "youtube" in dest.lower():
                dest = "YouTube"
        blocks.append(CollapsedBlock(name=f"Abrir {dest}", steps=b2))

    # Bloque 3.
    b3 = by_type.get("search_youtube", [])
    if b3:
        blocks.append(CollapsedBlock(name="Buscar en YouTube", steps=b3))

    # Bloque 4.
    b4 = by_type.get("scroll_results", [])
    if b4:
        blocks.append(CollapsedBlock(name="Explorar resultados", steps=b4))

    return blocks


# ──────────────────────────────────────────────────────────────────────
# Hard post-pass cleanup
# ──────────────────────────────────────────────────────────────────────

# Intenciones técnicas concretas y por qué un step semántico las cubre.
# El post-pass usa este mapa para nunca borrar un técnico cuando no
# existe el semántico correspondiente.
_RESIDUE_COVERED_BY: Dict[str, frozenset] = {
    # técnico -> conjunto de tipos semánticos que lo cubren
    "click_button":      frozenset({"open_app", "select_profile", "open_new_tab", "open_url", "search_youtube"}),
    "click":             frozenset({"open_app", "select_profile", "open_new_tab", "open_url", "search_youtube"}),
    "type_text":         frozenset({"open_app", "search_youtube"}),
    "key_press":         frozenset({"search_youtube"}),
    "key_press_enter":   frozenset({"search_youtube"}),
    "send_hotkey":       frozenset({"open_new_tab", "open_url"}),
    "submit_search_blank": frozenset({"search_youtube"}),
    "scroll":            frozenset({"scroll_results"}),
    "scroll_absolute":   frozenset({"scroll_results"}),
    "click_fallback":    frozenset({"open_app", "select_profile", "open_new_tab", "open_url", "search_youtube"}),
}


def hard_post_pass(steps: List[CollapsedStep]) -> List[CollapsedStep]:
    """Limpia residuos técnicos post-colapso.

    Regla PRD 2026-05-06d §3: un step técnico sólo se borra si EXISTE
    un step semántico que cubre su intención. Sin cobertura, se queda
    (para que Smart Executor o ApprovalGate lo vean y bloqueen).

    Esto evita que el engine "limpie demasiado" y deje misiones con
    sólo 2 pasos cuando había intenciones reales sin colapsar.
    """
    semantic_kinds = {s.type for s in steps if s.type in SEMANTIC_TYPES}
    out: List[CollapsedStep] = []
    for s in steps:
        t = s.type
        if t in SEMANTIC_TYPES:
            out.append(s)
            continue
        if t in RESIDUE_TYPES:
            covered_by = _RESIDUE_COVERED_BY.get(t, frozenset())
            if covered_by and (semantic_kinds & covered_by):
                # Hay un semántico que cubre esta intención → drop.
                continue
            # Sin cobertura: NO borramos (PRD §3). Lo dejamos pasar
            # para que el caller decida.
            out.append(s)
            continue
        out.append(s)
    return out


# ──────────────────────────────────────────────────────────────────────
# Coverage audit (PRD 2026-05-06d §4 + §5)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class CoverageReport:
    """Auditoría de cobertura del raw_trace.

    Attributes:
        expected_kinds: tipos semánticos que el ctx esperaba ver.
        emitted_kinds: tipos semánticos que el engine realmente emitió.
        missing_kinds: ``expected − emitted``. Si es no-vacío, hay
            intenciones detectadas que el engine no logró colapsar.
        important_event_ids: ids del raw_trace marcados como importantes.
        uncovered_event_ids: ids importantes que ningún step cubre.
        coverage_map: ``event_id`` → ``"covered_by:<type>"`` /
            ``"discarded:<reason>"`` / ``"needs_review"``.
    """

    expected_kinds: List[str] = field(default_factory=list)
    emitted_kinds: List[str] = field(default_factory=list)
    missing_kinds: List[str] = field(default_factory=list)
    important_event_ids: List[str] = field(default_factory=list)
    uncovered_event_ids: List[str] = field(default_factory=list)
    coverage_map: Dict[str, str] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return not self.missing_kinds and not self.uncovered_event_ids

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expected_kinds": list(self.expected_kinds),
            "emitted_kinds": list(self.emitted_kinds),
            "missing_kinds": list(self.missing_kinds),
            "uncovered_event_ids": list(self.uncovered_event_ids),
            "coverage_map": dict(self.coverage_map),
        }


# Mapa señal-en-ctx → kind semántico esperado.
_EXPECTED_KIND_FOR_SIGNAL: List[Tuple[str, str]] = [
    ("has_winsearch_session", "open_app"),
    ("fallback_winsearch_text", "open_app"),
    ("profile_picker_seen", "select_profile"),
    ("new_tab_seen", "open_new_tab"),
    ("youtube_visited", "open_url"),
    ("youtube_bookmark_seen", "open_url"),
    ("fallback_youtube_query_submitted", "search_youtube"),
    ("has_results_after_search", "scroll_results"),
]


def _expected_kinds_for_ctx(ctx: _Context) -> List[str]:
    """Lista canónica (en orden y deduplicada) de kinds esperados."""
    expected: List[str] = []
    seen = set()
    canonical_order = (
        "open_app", "select_profile", "open_new_tab",
        "open_url", "search_youtube", "scroll_results",
    )
    truth_by_kind: Dict[str, bool] = {k: False for k in canonical_order}
    for signal, kind in _EXPECTED_KIND_FOR_SIGNAL:
        if getattr(ctx, signal, False):
            truth_by_kind[kind] = True
    # Caso especial: scroll fallback puede activar sin field_session.
    if ctx.fallback_youtube_scroll_ticks > 0:
        truth_by_kind["scroll_results"] = True
    for k in canonical_order:
        if truth_by_kind.get(k) and k not in seen:
            expected.append(k)
            seen.add(k)
    return expected


def compute_coverage_audit(
    raw_trace: List[RawEvent],
    ctx: _Context,
    steps: List[CollapsedStep],
) -> CoverageReport:
    """Audita la cobertura del trace por los steps emitidos."""
    expected = _expected_kinds_for_ctx(ctx)
    emitted = [s.type for s in steps]
    missing = [k for k in expected if k not in set(emitted)]

    important = list(ctx.important_event_ids)
    coverage_map: Dict[str, str] = {}
    # Marcaje simple: si emitimos el kind esperado para una señal y el
    # evento que disparó esa señal está en important_event_ids, lo
    # marcamos como cubierto. Si la señal está pero el kind NO se
    # emitió, lo marcamos como needs_review.
    emitted_set = set(emitted)
    for ev in raw_trace:
        if ev.id not in ctx.important_event_ids:
            continue
        # Determinamos el kind esperado para este evento.
        exp_kind = _kind_for_event(ev, ctx)
        if not exp_kind:
            coverage_map[ev.id] = "discarded:no_kind"
            continue
        if exp_kind in emitted_set:
            coverage_map[ev.id] = f"covered_by:{exp_kind}"
        else:
            coverage_map[ev.id] = "needs_review"

    uncovered = [eid for eid, lbl in coverage_map.items() if lbl == "needs_review"]
    return CoverageReport(
        expected_kinds=expected,
        emitted_kinds=emitted,
        missing_kinds=missing,
        important_event_ids=important,
        uncovered_event_ids=uncovered,
        coverage_map=coverage_map,
    )


def _kind_for_event(ev: RawEvent, ctx: _Context) -> str:
    """Mapea un raw_event importante a su kind semántico esperado."""
    proc = _proc_of(ev)
    title = _title_of(ev)
    if _is_winsearch_event(ev) and _is_keyboard_text_event(ev):
        return "open_app"
    if _is_browser_proc(proc) and _is_profile_picker_window(ev):
        return "select_profile"
    if _is_browser_proc(proc) and "youtube" in title:
        if _is_keyboard_text_event(ev) or _is_enter_event(ev):
            return "search_youtube"
        if ev.event_type == EventType.MOUSE_SCROLL:
            return "scroll_results"
        if ev.event_type in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DOUBLE_CLICK,
        ):
            return "open_url"
    return ""


# ──────────────────────────────────────────────────────────────────────
# Repair questions y status
# ──────────────────────────────────────────────────────────────────────

def _build_repair_questions(
    steps: List[CollapsedStep],
    blocks: List[CollapsedBlock],
) -> List[RepairQuestion]:
    """Genera como mucho 2 preguntas humanas. Reglas estrictas.

    Solo se permite:
      * profile desconocido (``select_profile`` con
        ``needs_user_label=True``).
      * target realmente ambiguo (``search_youtube`` sin query — no
        debería pasar; defensivo).
    """
    questions: List[RepairQuestion] = []
    for s in steps:
        if s.needs_user_label and s.type == "select_profile":
            questions.append(RepairQuestion(
                code="profile_unknown",
                prompt=s.label_prompt or "¿Qué perfil seleccionaste?",
                step_type=s.type,
                block_name=_block_for(s, blocks),
            ))
        elif s.type == "search_youtube" and not s.params.get("query"):
            questions.append(RepairQuestion(
                code="ambiguous_target",
                prompt="¿Qué quieres buscar en YouTube?",
                step_type=s.type,
                block_name=_block_for(s, blocks),
            ))
        if len(questions) >= 2:
            break
    return questions


def _block_for(step: CollapsedStep, blocks: List[CollapsedBlock]) -> str:
    for b in blocks:
        if step in b.steps:
            return b.name
    return ""


def _classify_status(
    steps: List[CollapsedStep],
    blocks: List[CollapsedBlock],
    repair_questions: List[RepairQuestion],
    coverage: Optional[CoverageReport] = None,
) -> str:
    """Clasifica el plan según el spec.

    EXECUTABLE  — 0 pasos rojos, 0 residuos, ≤1 needs_user_label,
                  cobertura completa.
    NEEDS_REVIEW — falta perfil, target ambiguo, hay intenciones sin
                   cubrir o eventos importantes huérfanos.
    COMPILED_DRAFT — no se pudo colapsar (lista vacía o sólo residuos).
    """
    if not steps:
        return CollapseStatus.COMPILED_DRAFT
    # Cualquier residuo técnico (no semántico) → COMPILED_DRAFT.
    if any(s.type not in SEMANTIC_TYPES for s in steps):
        return CollapseStatus.COMPILED_DRAFT
    # PRD §5: si la cobertura está incompleta, el status NO puede ser
    # EXECUTABLE.
    if coverage is not None:
        if coverage.missing_kinds:
            # Faltan intenciones detectadas en el raw_trace que el
            # engine no logró colapsar → necesita atención humana.
            return CollapseStatus.NEEDS_REVIEW
        if coverage.uncovered_event_ids:
            return CollapseStatus.NEEDS_REVIEW
    needs_count = sum(1 for s in steps if s.needs_user_label)
    if needs_count >= 1 or repair_questions:
        return CollapseStatus.NEEDS_REVIEW
    return CollapseStatus.EXECUTABLE


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────

def collapse_intent(
    raw_trace: Optional[List[RawEvent]] = None,
    *,
    field_sessions: Optional[List[FieldSession]] = None,
    resolved_targets: Optional[List[CompiledStep]] = None,
    interpreted_steps: Optional[List[InterpretedStep]] = None,
    compiled_execution_graph: Optional[List[CompiledStep]] = None,
    web_data: Optional[Dict[str, Any]] = None,
    mission: Optional[Mission] = None,
) -> MissionIntentPlan:
    """Colapsa una grabación a una **MissionIntentPlan** semántica.

    Acepta tanto entradas explícitas (``raw_trace``, ``field_sessions``…)
    como una ``Mission`` completa de la que extrae los datos. Al menos
    uno de los dos debe estar presente.

    El engine es **puro** — no toca disco, no llama al LLM, no muta
    sus entradas. Para mutar la misión usar
    :func:`apply_collapse_to_mission`.
    """
    # 1. Resolver entradas.
    mission_description: str = ""
    mission_user_obs: List[str] = []
    if mission is not None:
        if raw_trace is None:
            raw_trace = list(mission.raw_trace or [])
        if compiled_execution_graph is None:
            compiled_execution_graph = list(mission.compiled_execution_graph or [])
        if interpreted_steps is None:
            interpreted_steps = list(mission.interpreted_steps or [])
        mission_description = (getattr(mission, "description", "") or "").strip()
        # ``user_observations`` es un canal opcional en mission.tags o
        # mission.aliases para inyectar observaciones humanas que el
        # recorder no cazó (ej. "Click en perfil 'Daniel Arturo
        # Ramos'"). El profile_resolver lo prefiere sobre OCR.
        for tag in (getattr(mission, "tags", None) or []):
            ts = str(tag or "").strip()
            if ts.startswith("profile:"):
                mission_user_obs.append(ts.split(":", 1)[1].strip())

    raw_trace = list(raw_trace or [])
    interpreted_steps = list(interpreted_steps or [])
    compiled_execution_graph = list(compiled_execution_graph or [])

    # 2. Sanitize defensivamente. Si el caller ya sanitizó, esto es
    #    idempotente (drops a 0).
    if raw_trace:
        try:
            cleaned, _ = sanitize_raw_trace(list(raw_trace))
            raw_trace = cleaned
        except Exception as e:
            log.debug(f"[collapse_engine] sanitize_raw_trace falló: {e}")

    # 3. Field sessions.
    if field_sessions is None:
        try:
            field_sessions = build_field_sessions(raw_trace)
        except Exception as e:
            log.debug(f"[collapse_engine] build_field_sessions falló: {e}")
            field_sessions = []
    field_sessions = list(field_sessions)

    # 4. Build context.
    ctx = _build_context(
        raw_trace, field_sessions,
        mission_description=mission_description,
        mission_user_observations=mission_user_obs,
    )

    # 5. Apply collapse patterns (orden canónico).
    candidates: List[Optional[CollapsedStep]] = [
        _detect_open_app(raw_trace, field_sessions, ctx),
        _detect_select_profile(ctx),
        _detect_open_new_tab(ctx),
        _detect_open_url_youtube(raw_trace, field_sessions, ctx),
        _detect_search_youtube(raw_trace, field_sessions, ctx),
        _detect_scroll_results(ctx),
    ]
    steps: List[CollapsedStep] = [s for s in candidates if s is not None]

    # 6. Hard cleanup: defensivo (engine ya solo emite semánticos).
    steps = hard_post_pass(steps)

    # 7. Build blocks.
    blocks = _build_blocks(steps)

    # 8. Confidence promedio.
    if steps:
        avg = sum(float(s.confidence) for s in steps) / float(len(steps))
    else:
        avg = 0.0

    # 9. Coverage audit (PRD §4 + §5).
    coverage = compute_coverage_audit(raw_trace, ctx, steps)

    # 10. Repair questions (max 2).
    questions = _build_repair_questions(steps, blocks)[:2]

    # 11. Status final (con coverage).
    status = _classify_status(steps, blocks, questions, coverage)

    plan = MissionIntentPlan(
        status=status,
        blocks=blocks,
        semantic_steps=steps,
        confidence_avg=round(float(avg), 4),
        repair_questions=questions,
        coverage_report=coverage,
    )
    log.info(
        f"[collapse_engine] status={plan.status} steps={plan.step_count} "
        f"blocks={len(plan.blocks)} avg_conf={plan.confidence_avg:.2f} "
        f"repair={plan.repair_count} "
        f"coverage_complete={coverage.is_complete} "
        f"missing={coverage.missing_kinds}"
    )
    return plan


# ──────────────────────────────────────────────────────────────────────
# Conversión a CompiledStep[] y mutación in-place
# ──────────────────────────────────────────────────────────────────────

_TYPE_TO_ACTION: Dict[str, ActionStrategy] = {
    "open_app": ActionStrategy.LAUNCH_APP,
    "select_profile": ActionStrategy.SELECT_PROFILE,
    "open_new_tab": ActionStrategy.OPEN_NEW_TAB,
    "open_url": ActionStrategy.OPEN_BOOKMARK,
    "search_youtube": ActionStrategy.SUBMIT_SEARCH,
    "scroll_results": ActionStrategy.SCROLL_UNTIL_VISIBLE,
}


def _step_to_compiled(step: CollapsedStep) -> CompiledStep:
    """Traduce un :class:`CollapsedStep` a :class:`CompiledStep` canónico."""
    a = _TYPE_TO_ACTION.get(step.type, ActionStrategy.AGENT_PROMPT)
    payload: Dict[str, Any] = dict(step.params)
    semantic: Dict[str, Any] = {
        "intent": step.type,
        "human_label": step.human_label,
        "confidence": round(float(step.confidence), 3),
    }

    if step.type == "open_app":
        payload = {
            "app_name": step.params.get("app") or "chrome",
            "method": step.params.get("method") or "windows_search",
        }
    elif step.type == "select_profile":
        payload = {
            "app": step.params.get("app") or "chrome",
            "profile": step.params.get("profile") or "",
        }
        if step.needs_user_label:
            payload["needs_user_label"] = True
            payload["label_prompt"] = step.label_prompt or "¿Qué perfil seleccionaste?"
    elif step.type == "open_new_tab":
        payload = {
            "app": step.params.get("app") or "chrome",
            "preferred_strategy": step.params.get("preferred_strategy") or "ctrl_t",
        }
    elif step.type == "open_url":
        payload = {
            "name": step.params.get("name") or "",
            "url": step.params.get("url") or "",
        }
    elif step.type == "search_youtube":
        payload = {
            "site": "youtube",
            "text": step.params.get("query") or "",
            "submit_after": True,
            "target_label": step.params.get("target") or "youtube_search_box",
            "locators": step.params.get("locators") or youtube_search_locators(),
        }
        semantic["site"] = "youtube"
        semantic["expected_state_after"] = "youtube_results_visible"
    elif step.type == "scroll_results":
        payload = {
            "direction": step.params.get("direction") or "down",
            "amount": int(step.params.get("amount") or 1),
            "context": step.params.get("context") or "youtube_results",
        }

    confidence_block = {
        "capture_score": int(round(step.confidence * 100)),
        "quality_level": _quality_from_confidence(step.confidence),
    }
    return CompiledStep(
        goal=step.human_label or step.type,
        action_strategy=a,
        action_payload=payload,
        target_context=TargetContext(
            semantic=semantic,
            confidence=confidence_block,
        ),
        validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    )


def _quality_from_confidence(conf: float) -> str:
    if conf >= 0.85:
        return "green"
    if conf >= 0.7:
        return "yellow"
    return "red"


def plan_to_compiled_steps(plan: MissionIntentPlan) -> List[CompiledStep]:
    """Convierte un plan en una lista plana de :class:`CompiledStep`."""
    return [_step_to_compiled(s) for s in plan.semantic_steps]


def apply_collapse_to_mission(
    mission: Mission,
    *,
    rewrite_compiled_graph: bool = False,
) -> MissionIntentPlan:
    """Colapsa la misión y persiste ``semantic_execution_plan``.

    Args:
        mission: misión a colapsar.
        rewrite_compiled_graph: si ``True``, sobrescribe
            ``compiled_execution_graph`` / ``interpreted_steps`` /
            ``action_groups`` con la versión semántica limpia (modo
            destructivo, usado por "Reparar débiles" en Mission
            Review). Default ``False``: el grafo legacy se mantiene
            intacto y solo se persiste el ``semantic_execution_plan``
            (modo handoff PRD 2026-05-07 §10).

    Si el ICE no logra emitir ningún paso (raw_trace muy degradado),
    no toca nada y devuelve un plan vacío.
    """
    plan = collapse_intent(mission=mission)
    compiled = plan_to_compiled_steps(plan)
    if not compiled:
        # Engine no encontró nada que colapsar. Plan vacío, grafo
        # legacy intacto.
        return plan

    # Linkea sucesores.
    for i in range(len(compiled) - 1):
        compiled[i].next_step_id_on_success = compiled[i + 1].id

    # Action groups por bloque.
    from app.contracts.mission import ActionGroup

    action_groups: List[ActionGroup] = []
    cursor = 0
    for blk in plan.blocks:
        ids: List[str] = []
        for _ in blk.steps:
            if cursor < len(compiled):
                ids.append(compiled[cursor].id)
                cursor += 1
        # Calidad agregada del bloque.
        confs = [s.confidence for s in blk.steps]
        avg = sum(confs) / len(confs) if confs else 0.0
        ql = _quality_from_confidence(avg)
        any_label = any(s.needs_user_label for s in blk.steps)
        action_groups.append(ActionGroup(
            group_title=blk.name,
            group_summary=blk.name,
            step_ids=ids,
            confidence_score=round(avg, 3),
            status="review" if any_label else "ok",
            quality_level=ql,
            expanded_default=False,
            semantic_tag=blk.steps[0].type if blk.steps else "",
        ))

    interpreted: List[InterpretedStep] = []
    for s, cs in zip(plan.semantic_steps, compiled):
        interpreted.append(InterpretedStep(
            description=s.human_label or s.type,
            inferred_goal=s.human_label or s.type,
            confidence=float(s.confidence),
            confidence_score_pct=round(float(s.confidence) * 100, 1),
            quality_level=_quality_from_confidence(s.confidence),
        ))

    if rewrite_compiled_graph:
        # Modo destructivo (Mission Review · "Reparar débiles").
        mission.compiled_execution_graph = compiled
        mission.interpreted_steps = interpreted
        mission.action_groups = action_groups

    # PRD 2026-05-06d §1 + 2026-05-07 §1: ``semantic_execution_plan``
    # NUNCA puede ser null en una misión semántica EXECUTABLE. Lo
    # construimos SIEMPRE, en modo destructivo o no.
    # Importación tardía para evitar ciclo (semantic_execution_plan
    # importa intent_collapse_engine.MissionIntentPlan).
    try:
        from app.services.missions.semantic_execution_plan import (  # type: ignore
            attach_semantic_plan_to_mission,
            build_semantic_execution_plan,
        )
        sep = build_semantic_execution_plan(
            mission, intent_plan=plan, use_existing=False,
        )
        if sep is not None and sep.step_count > 0:
            attach_semantic_plan_to_mission(mission, plan=sep, force=True)
            # PRD 2026-05-07 §10: el grafo legacy queda solo para
            # debug — el ejecutor consume el semantic_execution_plan.
            try:
                mission.legacy_compiled_graph_purpose = "legacy_debug_only"
            except Exception:
                pass
    except Exception as e:
        log.debug(f"[collapse_engine] no pude adjuntar semantic_execution_plan: {e}")

    return plan


__all__ = [
    "CollapsedStep",
    "CollapsedBlock",
    "RepairQuestion",
    "CollapseStatus",
    "MissionIntentPlan",
    "CoverageReport",
    "SEMANTIC_TYPES",
    "RESIDUE_TYPES",
    "INVALID_UIA_NAMES",
    "collapse_intent",
    "compute_coverage_audit",
    "plan_to_compiled_steps",
    "apply_collapse_to_mission",
    "hard_post_pass",
    "assess_query_fidelity",
]
